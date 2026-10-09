"""Export one frozen supplementary journal cut without requests or inference.

The caller supplies reviewed source/input pins and a separate, immutable journal
copy for exactly one family/model. Original obligations and their order are
authoritative; readiness is a separate, explicitly partitioned input. A missing
original trajectory is retained as absence at the frozen cut, never filled from
later data. This collector does not relax any downstream completeness gate.

Available finals are checked against saved requests, receipts and the frozen
runner's parser. Its exchange/save boundaries are replaced only in a private
function globals dictionary; the caller's runner and original files are not
modified. Only the fresh export directory is written. Partial exports survive
failure and cannot be reused. Imports perform no I/O.
"""
import collections
import copy
import hashlib
import json
from pathlib import Path
import types

from causes import campaign_supplement as core


def require(value, reason):
    if not value:
        raise core.IntegrityError(reason)


def journal_index(rows, expected):
    claims, finals = {}, {}
    for row in rows:
        if row['event'] not in ('claimed', 'route_success', 'route_failure'):
            continue
        mid = row['measurement_id']
        if mid not in expected:
            raise ValueError('unregistered_supplement_measurement')
        if row['event'] == 'claimed':
            if mid in claims or mid in finals:
                raise ValueError('duplicate_claim')
            claims[mid] = row
        else:
            if mid not in claims or mid in finals:
                raise ValueError('unclaimed_or_duplicate_terminal')
            finals[mid] = row
    return claims, finals


def validate_inputs(binding, prepared, roster, obligations, config):
    family, model = binding['family'], binding['model']
    root = core.canonical(binding['supplement_root'])
    require((prepared['family'], prepared['model']) == (family, model), 'prepared_scope')
    require(prepared['output_root_proposed'] == str(root), 'prepared_output_scope')
    runs = [row['run_id'] for row in roster]
    require(runs and len(set(runs)) == len(runs), 'unique_original_roster_required')
    stages, questions = binding['stages'], binding['question_ids']
    require(stages and set(stages) <= {'early', 'end'} and len(set(stages)) == len(stages), 'stage_scope')
    require(questions and len(set(questions)) == len(questions), 'question_scope')
    configured = {q['id']: q for q in config['questions']}
    require(len(configured) == len(config['questions']), 'duplicate_config_question')
    for qid in questions:
        require(qid in configured and not qid.startswith('evaluation_') and
                configured[qid]['threshold'] in ('strongly_favors', 'leans_towards_or_favors'),
                'supplement_question_scope')
    ids = [job['measurement_id'] for job in obligations]
    expected = {(run, stage, qid) for run in runs for stage in stages for qid in questions}
    require(len(set(ids)) == len(ids) == len(expected), 'original_obligation_count_or_duplicate')
    require({(j['run_id'], j['stage'], j['question_id']) for j in obligations} == expected,
            'original_obligation_grid')
    by_id = dict(zip(ids, obligations))
    for job in obligations:
        core.measurement_path(root, job, family=family, model=model)
        q = configured[job['question_id']]
        require(job['threshold'] == q['threshold'] and job['awareness_type'] == q['awareness_type'],
                'original_question_metadata')
    require(prepared['all_original_measurement_ids'] == ids, 'original_order_changed')
    ready = prepared['ready_jobs']
    ready_ids = [j['measurement_id'] for j in ready]
    pending, unavailable = prepared['pending_measurement_ids'], prepared['unavailable_measurement_ids']
    partition = ready_ids + pending + unavailable
    require(len(partition) == len(set(partition)) == len(ids) and set(partition) == set(ids),
            'readiness_partition')
    for group in (ready_ids, pending, unavailable):
        require([mid for mid in ids if mid in set(group)] == group, 'partition_order_changed')
    require(prepared['ready_order'] == ready_ids, 'ready_order_changed')
    for job in ready:
        original = by_id[job['measurement_id']]
        require(all(k in job and core.encoded(job[k]) == core.encoded(v) for k, v in original.items()),
                'original_measurement_changed')
    contexts = {(r['run_id'], r['stage']): r for r in prepared['contexts']}
    require(len(contexts) == len(prepared['contexts']) and
            set(contexts) == {(j['run_id'], j['stage']) for j in ready}, 'ready_context_scope')
    return by_id, {j['measurement_id']: j for j in ready}, contexts


def verify_final(job, terminal, *, root, family, model, contexts, config, runner, record):
    destination = core.measurement_path(root, job, family=family, model=model)
    require(terminal['final']['path'] == str(destination / 'FINAL.json'), 'terminal_path_mismatch')
    final = json.loads(record(terminal['final'], read=True))
    require(final['measurement_id'] == job['measurement_id'] and
            final['input_sha256'] == core.digest(job), 'final_input_identity')
    require(core.encoded(json.loads(record(destination / 'INPUT.json', read=True))) == core.encoded(job),
            'saved_input_identity')
    require(final['technical_status'] == terminal['technical_status'] and
            (terminal['event'] == 'route_success') == (final['technical_status'] == 'complete'),
            'terminal_status_identity')
    require(final['admissible'] is (final['technical_status'] == 'complete') and
            final['retry'] is False, 'final_admission_or_retry')
    require(not final['integrity_failure'], 'final_integrity_failure')
    require(core.encoded(json.loads(record(job['frozen_config'], read=True))) == core.encoded(config),
            'frozen_config_identity')
    for item in contexts[(job['run_id'], job['stage'])]['original_bindings'].values():
        if isinstance(item, dict) and set(item) == {'path', 'bytes', 'sha256'}:
            record(item)
    record(job['context'])
    context, question = core.prepare_request(job, contexts, config, runner)
    capture = destination / 'capture'
    pins = {}
    for item in final['capture_files']:
        path = core.canonical(item['path'])
        require(path.parent == capture and path.name not in pins, 'capture_path_or_duplicate')
        record(item)
        pins[path.name] = item
    request_raw = record(pins['request.json'], read=True) if 'request.json' in pins else None
    if request_raw is not None:
        require(hashlib.sha256(request_raw).hexdigest() == job['request_file_sha256'], 'actual_payload_identity')
    if 'attempt.json' in pins:
        attempt = json.loads(record(pins['attempt.json'], read=True))
        require(attempt.get('request_sha256') == job['request_body_sha256'] and attempt.get('retry') is False,
                'attempt_identity_or_retry')
    parsed = final.get('result')
    replay = 'technical_missing_no_parsed_result'
    if parsed is not None:
        require({'request.json', 'attempt.json', 'receipt.json', 'response.raw', 'result.json'} <= pins.keys(),
                'parsed_result_missing_capture')
        raw = record(pins['response.raw'], read=True)
        response = json.loads(raw)
        receipt = json.loads(record(pins['receipt.json'], read=True))
        require(receipt.get('http_status') == 200 and receipt.get('status') == 'complete' and
                receipt.get('retry') is False, 'parsed_result_transport_conflict')
        require(receipt['response_sha256'] == hashlib.sha256(raw).hexdigest() and
                receipt['request_sha256'] == job['request_body_sha256'], 'response_or_request_receipt_identity')
        saves, exchanges = {}, []

        def offline_exchange(endpoint, payload, path, timeout):
            require(endpoint == config['endpoint'] and timeout == config['request_timeout_seconds'] and
                    runner.encoded(payload) + b'\n' == request_raw and Path(path) == capture,
                    'offline_request_reconstruction')
            exchanges.append(True)
            return copy.deepcopy(response)

        def offline_save(path, value):
            require(Path(path) == capture / 'result.json' and not saves, 'offline_save_scope')
            saves['result.json'] = copy.deepcopy(value)

        scope = dict(runner.probe.__globals__, exchange=offline_exchange, save=offline_save)
        probe = types.FunctionType(runner.probe.__code__, scope, runner.probe.__name__,
                                   runner.probe.__defaults__, runner.probe.__closure__)
        reconstructed = probe(config, copy.deepcopy(context), question, job['seed'], capture)
        require(len(exchanges) == 1 and core.encoded(reconstructed) == core.encoded(parsed) and
                core.encoded(reconstructed) == core.encoded(json.loads(record(pins['result.json'], read=True))) and
                core.encoded(saves) == core.encoded({'result.json': reconstructed}), 'frozen_parser_reconstruction')
        require(reconstructed['technical_status'] == final['technical_status'], 'parsed_technical_status')
        replay = 'exact_frozen_request_render_and_parser_reconstructed'
    else:
        require(final['technical_status'] != 'complete', 'complete_without_parsed_result')
    label = parsed.get('answer') if parsed and final['technical_status'] == 'complete' else None
    return {'disposition': 'final_verified', 'technical_status': final['technical_status'],
            'admissible_label': label, 'label': label, 'raw_parsed_label': parsed.get('answer') if parsed else None,
            'reason': parsed.get('reason') if parsed else None, 'raw_result': parsed,
            'failure': final['failure'], 'integrity_failure': final['integrity_failure'],
            'final': terminal['final'], 'input_canonical_sha256': core.digest(job),
            'context': job['context'], 'context_canonical_sha256': job['context_canonical_sha256'],
            'question_canonical_sha256': job['question_canonical_sha256'], 'seed': job['seed'],
            'request_body_sha256': job['request_body_sha256'], 'capture_files': final['capture_files'],
            'offline_verification': replay}


def collect(binding_reference, output, *, runner):
    """Collect a pinned cut; callers retain review/adoption and source-import duties.

    JSON inputs are prepared_inputs, roster and config; original_obligations and
    trajectories are JSONL and are filtered to the bound model. journal_cut must
    be an already retained immutable copy, not the active .control journal.
    The resulting summary is descriptive custody, never analysis authorization.
    """
    binding = json.loads(core.pinned(binding_reference))
    require(binding['schema'] == 'causes.supplement_export_binding.v1', 'binding_schema')
    root, output = core.canonical(binding['supplement_root']), core.canonical(output)
    require(not output.exists() and not output.is_relative_to(root) and not root.is_relative_to(output),
            'fresh_separate_output_required')
    require(not core.canonical(binding['journal_cut']['path']).is_relative_to(root), 'immutable_journal_copy_required')
    all_pins = {}

    def record(value, read=False):
        item = core.pin(value) if isinstance(value, Path) else value
        previous = all_pins.get(item['path'])
        require(previous is None or previous == item, 'source_pin_conflict')
        raw = core.pinned(item) if read or previous is None else None
        all_pins[item['path']] = item
        return raw if read else item

    record(binding_reference)
    for module, key in ((core, 'core_source'), (runner, 'runner_source')):
        require(core.pin(Path(module.__file__).resolve()) == binding[key], 'imported_source_mismatch')
        record(binding[key])
    require(core.pin(Path(__file__).resolve()) == binding['collector_source'], 'collector_source_mismatch')
    record(binding['collector_source'])
    for item in binding['required_pins']:
        record(item)
    prepared, roster, config = [json.loads(record(binding[k], read=True)) for k in ('prepared_inputs', 'roster', 'config')]
    obligations = [json.loads(line) for line in record(binding['original_obligations'], read=True).splitlines()]
    obligations = [j for j in obligations if j['model'] == binding['model']]
    by_id, ready, contexts = validate_inputs(binding, prepared, roster, obligations, config)
    require(prepared['frozen_config'] == binding['config'] and prepared['frozen_runner'] == binding['runner_source'],
            'prepared_source_identity')
    original_rows = {}
    for line in record(binding['trajectories'], read=True).splitlines():
        row = json.loads(line)
        if row['model'] == binding['model'] and row['run_id'] in {r['run_id'] for r in roster}:
            require(row['run_id'] not in original_rows, 'original_analysis_identity')
            original_rows[row['run_id']] = row
    raw_journal = record(binding['journal_cut'], read=True)
    require(not raw_journal or raw_journal.endswith(b'\n'), 'partial_journal_cut')
    history = [json.loads(line) for line in raw_journal.splitlines()]
    claims, finals = journal_index(history, by_id)
    require(set(claims) <= ready.keys(), 'unready_obligation_attempted')
    for mid, claim in claims.items():
        require(claim['input_sha256'] == core.digest(ready[mid]), 'journal_claim_input_mismatch')
    output.mkdir()  # Exclusive reservation; a failed export never becomes reusable.
    core.save(output / 'INTENT.json', {'at': core.now(), 'binding': binding_reference,
                                      'journal_cut': binding['journal_cut'], 'no_network_or_inference': True})
    try:
        with (output / 'CONSUMED_JOURNAL.jsonl').open('xb') as stream:
            stream.write(raw_journal)
        measurements = []
        pending, unavailable = set(prepared['pending_measurement_ids']), set(prepared['unavailable_measurement_ids'])
        for original in obligations:
            mid = original['measurement_id']
            row = {k: copy.deepcopy(original[k]) for k in ('measurement_id', 'model', 'run_id', 'stage',
                                                          'question_id', 'awareness_type', 'threshold')}
            row.update(original_obligation=copy.deepcopy(original), source_family=binding['family'])
            if mid in finals:
                row.update(verify_final(ready[mid], finals[mid], root=root, family=binding['family'],
                                        model=binding['model'], contexts=contexts, config=config, runner=runner, record=record))
            else:
                state = ('pending_original_context_at_cut' if mid in pending else
                         'assigned_stage_unavailable' if mid in unavailable else
                         'attempted_pending_or_unresolved_at_cut' if mid in claims else 'unclaimed_at_journal_cut')
                row.update(disposition=state, technical_status=None, admissible_label=None, label=None,
                           raw_parsed_label=None, reason=None)
            measurements.append(row)
        grouped = collections.defaultdict(list)
        for row in measurements:
            grouped[row['run_id']].append(row)
        (output / 'trajectories').mkdir()
        index = []
        for allocation in roster:
            run = allocation['run_id']
            original = original_rows.get(run)
            if original is not None:
                require(core.encoded(original['allocation']) == core.encoded(allocation), 'original_allocation_join')
                for value in original['reader'].values():
                    require(record(Path(value['path']))['sha256'] == value['result_sha256'], 'original_reader_result_changed')
            combined = copy.deepcopy(original['self']) if original is not None else {}
            for row in grouped[run]:
                stage = combined.setdefault(row['stage'], {})
                require(row['question_id'] not in stage, 'supplement_cannot_replace_original_instrument')
                stage[row['question_id']] = copy.deepcopy(row)
            trajectory = {'run_id': run, 'model': binding['model'], 'allocation': allocation,
                          'original_trajectory': original,
                          'original_trajectory_canonical_sha256': core.digest(original) if original is not None else None,
                          'original_analysis_source': binding['trajectories'],
                          'original_data_availability': {'trajectory_in_frozen_cut': original is not None,
                              'self_stages_present': list(original['self']) if original is not None else [],
                              'reader_stages_present': list(original['reader']) if original is not None else []},
                          'supplement_measurements': grouped[run], 'self_joined_by_stage_and_question': combined,
                          'original_reader_unchanged': original['reader'] if original is not None else None,
                          'reader_dispositions_unchanged': original['reader_dispositions'] if original is not None else None,
                          'interpretation': 'Separate descriptive supplement; missing original rows/stages remain absent at the frozen cut. No original outcome replacement or inference release.'}
            path = output / 'trajectories' / (core.namespace_component(run) + '.json')
            core.save(path, trajectory)
            index.append({'run_id': run, 'model': binding['model'], 'trajectory': core.pin(path),
                          'original_trajectory_available': original is not None, 'supplement_assigned': len(grouped[run])})
        with (output / 'MEASUREMENTS.jsonl').open('xb') as stream:
            stream.write(b''.join(core.encoded(row) + b'\n' for row in measurements))
        core.save(output / 'INDEX.json', index)
        for item in all_pins.values():
            core.pinned(item)
        core.save(output / 'SOURCE_PINS.json', list(all_pins.values()))
        counts = dict(collections.Counter(r['disposition'] for r in measurements))
        summary = {'at': core.now(), 'family': binding['family'], 'model': binding['model'],
                   'original_runs': len(roster), 'original_rows_available_at_cut': len(original_rows),
                   'original_stage_obligations': len(roster) * len(binding['stages']),
                   'supplement_assigned': len(obligations), 'ready_assigned': len(ready),
                   'pending_original_context': len(pending), 'assigned_unavailable': len(unavailable),
                   'dispositions': counts,
                   'technical_final_statuses': dict(collections.Counter(r['technical_status'] for r in measurements if r['disposition'] == 'final_verified')),
                   'all_ready_final': len(finals) == len(ready),
                   'all_obligations_final_or_unavailable': len(finals) + len(unavailable) == len(obligations),
                   'claims_at_consumed_cut': len(claims), 'terminals_at_consumed_cut': len(finals),
                   'consumed_journal': core.pin(output / 'CONSUMED_JOURNAL.jsonl'),
                   'index': core.pin(output / 'INDEX.json'), 'measurements': core.pin(output / 'MEASUREMENTS.jsonl'),
                   'source_pins': core.pin(output / 'SOURCE_PINS.json'),
                   'analysis_authorized': False, 'no_network_calls': True, 'no_original_files_modified': True}
        core.save(output / 'SUMMARY.json', summary)
        return core.pin(output / 'SUMMARY.json')
    except BaseException as error:
        core.save(output / 'ERROR.json', {'at': core.now(), 'error': repr(error), 'accepted_export': False})
        raise
