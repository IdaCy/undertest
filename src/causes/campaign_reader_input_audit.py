from __future__ import annotations

from pathlib import Path
from collections import Counter
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import sys

SCHEMA = 'causes.saved_reader_input_audit_descriptor.v1'
FIELDS = {
    'schema', 'request', 'execution', 'preparation', 'pending_request',
    'metadata_preparation', 'metadata_review', 'closed_actual', 'closed_review',
    'previous_plan', 'previous_preparation', 'previous_review', 'interpreter',
}
OPTIONAL_FIELDS = {'source_binding'}

def require(ok, reason):
    if not ok:
        raise ValueError(reason)

def pin(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and not path.is_symlink(), 'canonical_saved_file_required')
    body = path.read_bytes()
    return dict(path=str(path), bytes=len(body), sha256=hashlib.sha256(body).hexdigest())

def raw(ref):
    require(pin(ref['path']) == ref, 'saved_pin_changed')
    return Path(ref['path']).read_bytes()

def ck(ref):
    return json.loads(raw(ref))

def local(path):
    return ck(pin(path))

def module(ref, name):
    raw(ref)
    spec = importlib.util.spec_from_file_location(name, ref['path'])
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    require(pin(value.__file__) == ref, 'native_module_changed')
    return value

def invocation_binding(settings, execution, intent, native_argv, native_source):
    """Distinguish a real wrapper invocation from optional offline reconstruction."""
    reference = settings.get('source_binding')
    wrapped = execution.get('source_binding') is not None
    if reference is None:
        require(not wrapped and intent.get('source_binding') is None
                and execution['argv'] == native_argv, 'exact_native_invocation')
        return None
    descriptor = ck(reference)
    entry_peer = ck(descriptor['entrypoint_review'])
    require(entry_peer.get('accepted_pinned_native_source_entrypoint') is True
            and not entry_peer.get('findings')
            and entry_peer['source'] == descriptor['entrypoint']
            and entry_peer['loader_source'] == descriptor['loader_source'], 'accepted_source_entrypoint')
    entry = module(descriptor['entrypoint'], 'saved_input_source_entrypoint')
    descriptor, loader, binding = entry.configuration(reference)
    require(descriptor['source'] == native_source and descriptor['native_argv'] == native_argv
            and descriptor['strategy'] == 'saved_parent', 'exact_input_constructor_binding')
    if wrapped:
        require(execution['source_binding'] == intent.get('source_binding') == reference
                and execution.get('native_argv') == intent.get('native_argv') == native_argv
                and execution['argv'] == entry.process_argv(reference, descriptor),
                'exact_bound_native_invocation')
    else:
        require(intent.get('source_binding') is None and execution['argv'] == native_argv,
                'original_direct_invocation_preserved')
    return descriptor, loader, binding

def review_metadata_template(settings):
    metadata_review = ck(settings['metadata_review'])
    require(metadata_review.get('accepted_fresh_reader_next_batch_metadata_preparation') is True
            and not metadata_review.get('findings')
            and metadata_review.get('preparation') == settings['metadata_preparation'],
            'accepted_next_batch_metadata')
    metadata = ck(settings['metadata_preparation'])
    templates = metadata.get('templates')
    require(isinstance(templates, dict)
            and templates.get('materialization') == settings['pending_request'],
            'accepted_next_batch_metadata')
    if 'templates' in metadata_review:
        require(metadata_review['templates'] == templates, 'metadata_template_copy_mismatch')
    raw(settings['pending_request'])
    return metadata_review


def review_saved_inputs(settings):
    """Audit saved input construction with the original pinned native helpers."""
    require(type(settings) is dict and set(settings) in (FIELDS, FIELDS | OPTIONAL_FIELDS)
            and settings['schema'] == SCHEMA, 'exact_descriptor_required')
    for key in FIELDS - {'schema', 'interpreter'}:
        raw(settings[key])
    interpreter = Path(settings['interpreter'])
    require(interpreter.is_absolute(), 'absolute_interpreter_required')
    request_ref = settings['request']
    request = ck(request_ref)
    O = Path(request['output'])
    prep_ref = settings['preparation']
    require(prep_ref == pin(O / 'PREPARATION.v1.json'), 'preparation_output_join')
    prep = ck(prep_ref)
    require(type(prep_ref['sha256']) is str, 'root_saved_actual_pin')
    source_peer = ck(request['source_review'])
    require(source_peer['accepted_fresh_candidate_reader_materializer'] is True
            and source_peer['source'] == request['source'] == prep['source']
            and source_peer['known'] == request['known'] == prep['known'], 'accepted_materializer_source')
    pending = ck(settings['pending_request'])
    inverse = json.loads(json.dumps(request))
    inverse['root_authorized'] = False
    require(len(inverse['feeds']) >= len(pending['feeds']), 'original_feed_descriptors_retained')
    for current, prior_descriptor in zip(inverse['feeds'], pending['feeds']):
        current['cut'] = prior_descriptor['cut']
        current['cut_review'] = prior_descriptor['cut_review']
    inverse['feeds'] = inverse['feeds'][:len(pending['feeds'])]
    require(inverse == pending and request['root_authorized'] is True, 'only_late_cut_authority_and_reviewed_appended_feed_fills')
    metadata_review_ref = settings['metadata_review']
    metadata_review = review_metadata_template(settings)
    # Each descriptor is independently checked by native feed_rows below, including
    # exact plan/source/registration/index peer joins. There is no role-only lookup.
    expected_copied = 0
    for descriptor in request['feeds']:
        cut, peer = ck(descriptor['cut']), ck(descriptor['cut_review'])
        require(peer.get('accepted_individual_variant_reader_feed_startup') is True
                or peer.get('accepted_individual_variant_reader_feed_index_cut') is True, 'accepted_feed_cut')
        require(peer['actual'] == descriptor['cut'] and peer['plan'] == descriptor['plan']
                and peer['index'] == cut['index']['saved'] and peer['accepted'] == cut['accepted'], 'exact_dated_projection')
        expected_copied += cut['accepted']
    require(prep['schema'] == 'causes.fresh_candidate_reader_inputs.v1' and prep['feeds'] == request['feeds'], 'fresh_input_schema_feeds')

    execution_ref = settings['execution']
    execution = ck(execution_ref)
    native_intent_ref = pin(O / 'INTENT.v1.json')
    native_intent = ck(native_intent_ref)
    require(native_intent['request'] == request_ref, 'native_intent_request')
    authority_ref = native_intent['authority']
    authority = ck(authority_ref)
    require(authority['request'] == request_ref and authority['source'] == prep['source'] and authority['output'] == str(O), 'native_authority_exact_scope')
    require(authority['root_authorized'] is True and authority['action'] == 'prepare_fresh_candidate_reader_inputs' and authority['automatic_retry'] is False, 'genuine_root_materialization_authority')
    require(execution['returncode'] == 0 and raw(execution['stderr']) == b'' and ck(execution['stdout']) == prep_ref, 'successful_native_materialization_result')
    native_argv = [settings['interpreter'], '-B', prep['source']['path'], '--request', request_ref['path'], '--authority', authority_ref['path']]
    root_intent_ref = pin(Path(execution_ref['path']).with_name('INTENT.v1.json'))
    root_intent = ck(root_intent_ref)
    source_binding = invocation_binding(settings, execution, root_intent, native_argv, prep['source'])
    require(root_intent['request'] == execution['request'] == request_ref and root_intent['authority'] == execution['authority'] == authority_ref and root_intent['argv'] == execution['argv'], 'root_intent_execution_join')
    require(root_intent['maximum_invocations'] == 1 and root_intent['automatic_retry'] is False and root_intent['metadata_review'] == metadata_review_ref, 'single_root_invocation')
    require(execution['host_calls'] == execution['model_calls'] == execution['reader_launches'] == 0, 'input_only_execution')
    clock = datetime.datetime.fromisoformat
    # The preceding batch must have an accepted closure before materialization.
    closed_ref, closed_peer_ref = settings['closed_actual'], settings['closed_review']
    closed, closed_peer = ck(closed_ref), ck(closed_peer_ref)
    require(closed_peer['accepted_finite_reader_route_closed_cut'] is True
            and closed_peer['actual'] == closed_ref and closed_peer['findings'] == []
            and all(closed_peer[k] == closed[k] for k in ('plan', 'reader_source', 'root', 'journal', 'runtime_result')), 'accepted_previous_batch_closed')
    require(closed['plan'] == settings['previous_plan']
            and closed['active'] == closed['unclaimed'] == 0 and closed['all_claims_accounted'] is True
            and closed['stop_reason'] is None and closed['owned_pause'] is None and closed['quarantine'] is None,
            'genuine_natural_predecessor_closure')
    require(clock(closed['observed_at']) <= clock(closed_peer['at']) <= clock(execution['started_at']),
            'closure_peer_precedes_materialization')
    require(clock(authority['issued_at']) <= clock(execution['started_at']) <= clock(execution['finished_at']) <= clock(authority['expires_at']), 'dated_authority_valid_for_saved_invocation')

    fresh = module(prep['source'], 'fresh_reader_actual_review_source')
    known = ck(prep['known'])
    require(known == fresh.known() and fresh.KNOWN_REFERENCE == prep['known'], 'frozen_known')
    native = fresh.parent()
    if source_binding is None:
        modules = native.native()
    else:
        _, loader, binding = source_binding
        require(known['input_parent'] == binding['materializer'], 'exact_input_parent_binding')
        with loader.bind_constructor(native, binding['materializer'], binding):
            modules = native.native()
    reader, watch = modules['campaign_reader'], modules['campaign_reader_watch']
    require(pin(reader.__file__) == known['reader_source'] == prep['reader_source'], 'native_reader_source')
    require(pin(watch.__file__) == native.KNOWN['modules']['campaign_reader_watch'], 'native_snapshot_binder')
    for name, ref in known['prompts'].items():
        raw(ref)
    require(prep['checker_config'] == known['checker_config'] and prep['prompts'] == known['prompts']
            and prep['prompts_directory'] == known['prompts_directory'], 'checker_prompt_identity')
    checker = ck(known['checker_config'])
    allocation, jobs, placement = fresh.global_inputs(known)
    require(prep['cohort'] == known['allocation'] and prep['cohort_review'] == known['allocation_review']
            and prep['placement'] == known['placement'], 'frozen_global_roster')
    ordered = [job['allocation']['run_id'] for job in jobs]
    require(ordered and len(ordered) == len(set(ordered)) and ordered == allocation['ordered_run_ids'], 'exact_original_order')
    selected_count = len(ordered)
    instrument_count = len(known['reader_instruments'])
    require(instrument_count > 0, 'reader_instruments_required')
    jobs_by = dict(zip(ordered, jobs))
    copied = {}
    feed_checks = []
    descriptor_keys = []
    for descriptor in request['feeds']:
        descriptor_keys.append((descriptor['role'], descriptor['plan']['path'], descriptor['plan']['sha256']))
        rows = fresh.feed_rows(descriptor, jobs_by, placement, known)
        require(not copied.keys() & rows.keys(), 'copied_physical_run_repeated_across_feeds')
        copied.update(rows)
        feed_checks.append(dict(role=descriptor['role'], plan=descriptor['plan'], source_review=descriptor['source_review'], cut=descriptor['cut'], cut_review=descriptor['cut_review'], copied=len(rows)))
    require(len(descriptor_keys) == len(set(descriptor_keys)) and len(copied) == expected_copied, 'exact_accepted_cut_scope')
    ready, unavailable, pending_rows, self_jobs = (ck(prep[key]) for key in ('ready', 'unavailable', 'pending', 'self_jobs'))
    require(self_jobs == [] and prep['runtime_authorized'] is False and prep['model_calls'] == 0, 'input_preparation_only')
    require(all(prep[key] is True for key in ('original_allocations_unchanged', 'treatments_preserved', 'payloads_contain_no_analysis_labels', 'cumulative_ready_requires_closed_history_filter', 'pending_is_not_missing')), 'frozen_input_guarantees')
    ready_by = {row['run_id']: row for row in ready}
    unavailable_by = {row['run_id']: row for row in unavailable}
    require(len(ready_by) == len(ready) and len(unavailable_by) == len(unavailable)
            and not ready_by.keys() & unavailable_by.keys() and ready_by.keys() | unavailable_by.keys() == copied.keys(), 'copied_partition')
    require([row['run_id'] for row in ready] == [run for run in ordered if run in ready_by]
            and [row['run_id'] for row in unavailable] == [run for run in ordered if run in unavailable_by], 'ready_unavailable_original_order')
    expected_pending = [dict(run_id=run, stage='early', measurement_key=fresh.measurement_key(run), disposition='pending', reason='no_accepted_reader_input_copy_at_cut') for run in ordered if run not in copied]
    require(pending_rows == expected_pending, 'uncopied_remains_pending')
    payload_count = 0
    missing_contexts = []
    role_counts = Counter()
    custody_cache = {}
    for run in ordered:
        if run not in copied:
            continue
        feed = copied[run]
        job = jobs_by[run]
        receipt = feed['receipt']
        manifest_path = receipt['manifest']
        if manifest_path not in custody_cache:
            manifest_ref = pin(manifest_path)
            verified_ref = pin(Path(manifest_path).with_name('VERIFIED.json'))
            verified = ck(verified_ref)
            require(verified['schema'] == 'caus.reader_input_verified.v1', 'native_verified_schema')
            verified_by = {row['run']: row for row in verified['runs']}
            require(len(verified_by) == len(verified['runs']), 'duplicate_verified_receipt')
            custody_cache[manifest_path] = (manifest_ref, verified_ref, verified_by)
        manifest, verified_ref, verified_by = custody_cache[manifest_path]
        require(verified_by.get(receipt['run']) == receipt, 'whole_receipt_in_native_VERIFIED')
        frozen = dict(model='mi27', job=job, config=feed['config'], jobs_file_sha256=feed['jobs_reference']['sha256'], model_file_sha256=feed['config_reference']['sha256'])
        binding, loaded = watch.bind_snapshot(receipt, frozen, Path(feed['snapshots_root']), dict(reader_source=known['reader_source'], cohort=known['allocation']))
        require(manifest['sha256'] == receipt['manifest_sha256'], 'whole_native_manifest_identity')
        original_refs, files = native.source_files(dict(manifest=manifest, index_row=receipt, run_id=run))
        require({name: loaded[name + '.json'] for name in ('allocation', 'spec', 'variant')} == job, 'literal_original_job')
        common = dict(measurement_key=fresh.measurement_key(run), run_id=run, stage='early', original_refs=original_refs,
                      native_job_sha256=reader.digest(job), original_binding=binding, producer_role=feed['role'],
                      feed_cut=feed['feed_cut'], feed_cut_review=feed['feed_cut_review'], feed_plan=feed['feed_plan'])
        if 'initial_context.json' not in original_refs or 'early_context.json' not in original_refs:
            expected = dict(common, disposition='unavailable', reason='required_stage_or_initial_context_not_captured', closed_native_snapshot=True)
            require(unavailable_by.get(run) == expected and run not in ready_by, 'native_missing_context_preserved')
            missing_contexts.append(dict(run_id=run, absent=[name for name in ('initial_context.json', 'early_context.json') if name not in original_refs], manifest=manifest))
            role_counts[(feed['role'], 'unavailable')] += 1
            continue
        messages, initial, prefix, phase = watch.stage_context('early', loaded)
        require(phase == 'prefix' and prefix is None, 'original_early_prefix_stage')
        context, targets = reader.prepare_context(messages, len(initial), phase, initial, prefix)
        expected_messages = []
        for index, message in enumerate(messages):
            actor = index >= len(initial) and message['role'] == 'assistant'
            reasoning = message.get('reasoning') if message.get('reasoning') is not None else message.get('reasoning_content')
            value = dict(message_index=index, role=message['role'], origin='generated_actor' if actor else 'provided_context' if index < len(initial) else 'non_actor_context', content=message.get('content'), reasoning=reasoning)
            if message.get('tool_calls'):
                value['tool_calls'] = message['tool_calls']
            expected_messages.append(value)
        require(set(context) == {'messages', 'observation', 'context_sha256'} and context['messages'] == expected_messages, 'only_original_message_channels_and_native_boundaries')
        require(targets == {row['message_index']: {key: row[key] for key in ('content', 'reasoning')} for row in expected_messages if row['origin'] == 'generated_actor'}, 'original_actor_channels_preserved')
        payloads = {}
        for instrument in known['reader_instruments']:
            payload, pins = reader.make_payload(checker, Path(known['prompts_directory']), context, instrument)
            require(payload['store'] is False and payload['input'][1] == dict(role='user', content=reader.encoded(context).decode()), 'payload_has_only_native_context')
            payloads[instrument] = dict(native.payload_identity(payload, reader.encoded), pins=pins)
            payload_count += 1
        expected = dict(common, disposition='prepared', view=original_refs['early_context.json'], context_canonical_sha256=reader.digest(messages), judge_context_sha256=reader.digest(context), targets_sha256=reader.digest(targets), payloads=payloads, reader_unattempted=None, runtime_authorized=False)
        require(ready_by.get(run) == expected and run not in unavailable_by, 'native_ready_row_payload_identity')
        role_counts[(feed['role'], 'ready')] += 1
    # Keep every accepted old scientific identity. Dated feed-cut pointers may advance;
    # the native receipt, producer plan, original files and all payload identities may not.
    prior_prepared_ref = settings['previous_preparation']
    prior_review_ref = settings['previous_review']
    prior_prepared, prior_review = ck(prior_prepared_ref), ck(prior_review_ref)
    prior_plan = ck(settings['previous_plan'])
    require(prior_plan['prepared'] == prior_prepared_ref and prior_plan['prepared_review'] == prior_review_ref,
            'predecessor_input_join')
    require(prior_review['accepted_fresh_candidate_reader_inputs'] is True
            and prior_review['findings'] == [] and prior_review['preparation'] == prior_prepared_ref,
            'accepted_previous_inputs')
    for key in ('source', 'cohort', 'ready', 'self_jobs', 'unavailable', 'pending', 'reader_source'):
        require(prior_review[key] == prior_prepared[key], 'previous_review_join:' + key)
    prior_counts = {}
    for field, current_rows in (('ready', ready_by), ('unavailable', unavailable_by)):
        previous_rows = ck(prior_prepared[field])
        prior_counts[field] = len(previous_rows)
        for previous_row in previous_rows:
            current_row = current_rows.get(previous_row['run_id'])
            require(current_row is not None, 'previous_disposition_removed_or_changed')
            strip_cut = lambda row: {key: value for key, value in row.items() if key not in ('feed_cut', 'feed_cut_review')}
            require(strip_cut(current_row) == strip_cut(previous_row), 'previous_native_identity_changed')
    require(prior_counts == dict(ready=prior_prepared['counts']['reader_ready'], unavailable=prior_prepared['counts']['unavailable']), 'previous_partition_counts')
    counts = dict(selected=selected_count, copied=len(copied), reader_ready=len(ready), reader_requests=instrument_count * len(ready), unavailable=len(unavailable), pending=len(pending_rows), self_requests=0)
    require(counts == prep['counts'] and len(ready) + len(unavailable) + len(pending_rows) == selected_count and payload_count == instrument_count * len(ready), 'complete_partition_counts')
    review = dict(schema='causes.fresh_candidate_reader_inputs_independent_review.v1', accepted_fresh_candidate_reader_inputs=True,
                  preparation=prep_ref, source=prep['source'], known=prep['known'], cohort=prep['cohort'], ready=prep['ready'], self_jobs=prep['self_jobs'], unavailable=prep['unavailable'], pending=prep['pending'],
                  source_review=request['source_review'], metadata_review=metadata_review_ref, request=request_ref, root_authority=authority_ref, execution=execution_ref, native_intent=native_intent_ref, root_intent=root_intent_ref,
                  reader_source=known['reader_source'], native_snapshot_source=pin(watch.__file__), input_parent=known['input_parent'],
                  counts=counts, role_counts=[dict(role=k[0], disposition=k[1], count=v) for k, v in sorted(role_counts.items())], feed_checks=feed_checks,
                  preserved_unavailable=missing_contexts, prepared_payloads_reconstructed=payload_count,
                  previous_preparation=prior_prepared_ref, previous_review=prior_review_ref, preserved_previous_counts=prior_counts,
                  preceding_closed_actual=closed_ref, preceding_closed_review=closed_peer_ref, closure_before_materialization=True,
                  whole_snapshot_custody=[dict(manifest=v[0], verified=v[1]) for _, v in sorted(custody_cache.items())],
                  checks=['Exact single successful root request, authority, source and saved preparation joined.', 'The frozen original job order and masks are unchanged; copied physical IDs are disjoint across accepted feed parts.', 'Every copied row passed whole native snapshot manifest/VERIFIED custody and exact original job/config binding.', 'Every eligible early context, target set and all frozen reader payload identities were reconstructed with the accepted native helpers.', 'Payload input contains only the native reader context; original treatment text remains preserved and no external allocation/analysis metadata is inserted.', 'Copied missing contexts remain unavailable with their original evidence; uncopied assignments remain pending.', 'All prior ready and unavailable rows retain literal native identity and payloads; only dated feed-cut references may advance.', 'No native materializer, model call, raw response reader or runtime launcher was invoked by this review.'],
                  findings=[], runtime_authorized=False, model_calls=0, host_calls=0, materializer_invocations=0, response_or_label_reads=0,
                  at=datetime.datetime.now(datetime.timezone.utc).isoformat(), review_source=pin(__file__))

    if source_binding is not None:
        review['source_binding'] = settings['source_binding']
        review['native_invocation_used_source_binding'] = execution.get('source_binding') is not None
        review['original_native_source'] = prep['source']
    return review


def write_review(descriptor_ref, output):
    """Write one review after an offline audit; never overwrite saved evidence."""
    sys.dont_write_bytecode = True
    descriptor = ck(descriptor_ref)
    allowed_write = Path(output)
    require(allowed_write.is_absolute() and allowed_write.resolve() == allowed_write
            and not allowed_write.exists(), 'exclusive_review_output_required')

    def audit(event, arguments):
        if event.startswith(('socket.', 'subprocess.')) or event in (
            'os.system', 'os.kill', 'os.fork', 'os.exec', 'os.posix_spawn', 'os.remove', 'os.rename'
        ):
            raise RuntimeError('saved_input_review_only')
        if event == 'open' and isinstance(arguments[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(arguments[0])).resolve()
            mode, flags = arguments[1:3]
            writing = (isinstance(mode, str) and any(x in mode for x in 'wax+')) or (
                isinstance(flags, int) and flags & (
                    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
                )
            )
            require(not writing or path == allowed_write, 'review_write_outside_receipt')
            require('/probes/' not in str(path) and '/responses/' not in str(path)
                    and path.name not in ('.env', 'response.raw', 'parsed.json', 'result.json',
                                          'FINAL.json', 'READOUTS.v1.json', 'MEASUREMENT_CUSTODY.v1.json'),
                    'scientific_response_read_blocked')

    sys.addaudithook(audit)
    review = review_saved_inputs(descriptor)
    review['descriptor'] = descriptor_ref
    with allowed_write.open('x') as handle:
        json.dump(review, handle, sort_keys=True, indent=2)
        handle.write('\n')
    return pin(allowed_write)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--descriptor', required=True, type=json.loads)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(write_review(args.descriptor, args.output), sort_keys=True))


if __name__ == '__main__':
    main()
