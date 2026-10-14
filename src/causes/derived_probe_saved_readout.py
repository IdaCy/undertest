"""Resolve independently accepted derived-probe finals without parsing responses.

Callers authenticate the supplied review pins and freeze comparison membership
before outcomes are used. This helper checks their exact saved joins; a JSON
acceptance flag by itself is not an independent authority. No model, parser,
native audit, projection, or file writer is invoked.
"""
import copy
import hashlib
import json

from causes import campaign_analysis as analysis
from causes.campaign_analysis import SELF_LABELS, self_label, contrast
from shared import evidence_bytes as storage


THRESHOLDS = ('presence', 'strongly_favors', 'leans_towards_or_favors')
QUESTIONS = tuple('evaluation_' + name for name in THRESHOLDS)


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def direct_instrument_protocol(config, runner_source, question):
    """Comparable native semantics, separate from derived view/family identity.

    Original-cell callers must establish these same exact saved inputs. Matching
    run or question names alone is insufficient; all non-endpoint configuration,
    native runner bytes and the complete question contribute to the identity.
    """
    require(type(config) is dict and type(config.get('model')) is str
            and type(config.get('endpoint')) is str and config['endpoint']
            and type(question) is dict and question.get('id') in QUESTIONS
            and question.get('awareness_type') == 'evaluation'
            and question.get('threshold') == question['id'].removeprefix('evaluation_')
            and type(question.get('text')) is str and question['text'], 'native_instrument_protocol')
    storage._pin_shape(runner_source, storage.ABSOLUTE_LIMIT)
    return dict(schema='causes.saved_native_direct_instrument_protocol.v1', model=config['model'],
        config_without_endpoint_sha256=digest({k:v for k,v in config.items() if k != 'endpoint'}),
        runner_source={k:runner_source[k] for k in ('bytes','sha256')}, question=copy.deepcopy(question))


def unique(values, reason):
    require(type(values) is list and all(type(x) is str and x for x in values)
            and len(values) == len(set(values)), reason)
    return values


class _Saved:
    def __init__(self, document_limit, payload_limit):
        self.document_limit = storage._limit(document_limit)
        self.payload_limit = storage._limit(payload_limit)
        self.refs = {}

    def raw(self, ref, limit=None):
        limit = self.document_limit if limit is None else limit
        storage._pin_shape(ref, limit)
        old = self.refs.get(ref['path'])
        require(old is None or old == ref, 'conflicting_saved_pin')
        body = storage.read_pinned(ref, limit=limit)
        self.refs[ref['path']] = copy.deepcopy(ref)
        return body

    def value(self, ref):
        return storage._json(self.raw(ref))

    def accepted(self, descriptor, flag, field='actual'):
        require(type(descriptor) is dict and set(descriptor) == {'actual', 'review'},
                'saved_descriptor')
        actual, peer = self.value(descriptor['actual']), self.value(descriptor['review'])
        require(peer.get(flag) is True and peer.get('findings') == []
                and peer.get(field) == descriptor['actual'], 'accepted_saved_peer')
        return actual, peer

    def payload(self, ref):
        # Check the declared bound before loading a plain list or tiny wrapper.
        value = storage._json(self.raw(ref, max(self.document_limit, self.payload_limit)))
        if type(value) is list:
            require(ref['bytes'] <= self.payload_limit, 'saved_payload_bound')
            return value
        require(type(value) is dict and set(value) == {'schema', 'stored', 'raw'}
                and value['schema'] == 'shared.exact_gzip_bytes.v1', 'saved_payload_schema')
        storage._digest_shape(value['raw'], self.payload_limit)
        self.raw(value['stored'], storage.ABSOLUTE_LIMIT)
        rows = storage._json(storage.read_gzip(value, limit=self.payload_limit))
        require(type(rows) is list, 'saved_payload_list')
        return rows

    def unchanged(self):
        for ref in self.refs.values():
            storage.read_pinned(ref, limit=storage.ABSOLUTE_LIMIT)


def _inputs(bank, saved):
    rows, roles, jobs, comparisons, counts = {}, {}, [], [], []
    protocol = None
    require(type(bank['inputs']) is list and bank['inputs'], 'bank_inputs')
    paths = set()
    for descriptor in bank['inputs']:
        require(descriptor['actual']['path'] not in paths, 'duplicate_input_preparation')
        paths.add(descriptor['actual']['path'])
        prep, peer = saved.accepted(descriptor, 'accepted_frozen_derived_probe_inputs', 'preparation')
        require(prep['schema'] == 'causes.frozen_derived_probe_preparation.v1'
                and prep['runtime_authorized'] is False, 'frozen_input_schema')
        require(all(peer[k] == prep[k] for k in ('rows', 'assignments', 'pairs', 'protocol', 'counts')),
                'input_peer_joins')
        assignments, current, pairs, proto = [saved.value(prep[k]) for k in
                                              ('assignments', 'rows', 'pairs', 'protocol')]
        require(proto['schema'] == 'causes.derived_probe_protocol.v1'
                and proto['family'] == bank['family'] and proto['model'] == bank['model'],
                'input_protocol')
        protocol = proto if protocol is None else protocol
        require(proto == protocol, 'mixed_scientific_protocol')
        require(type(current) is list and current and type(assignments) is list
                and len(current) == len(assignments) and digest(current) == prep['rows_canonical_sha256'],
                'input_row_vector')
        local, local_roles, requests, missing = {}, {}, 0, 0
        for assignment, row in zip(assignments, current):
            require(type(assignment) is dict and set(assignment) == {'run_id', 'view_kind', 'stage'}
                    and assignment['stage'] == 'early' and row['schema'] == 'causes.derived_probe_input.v1'
                    and all(row[k] == v for k, v in assignment.items())
                    and row['runtime_authorized'] is False, 'input_assignment')
            key = row['measurement_key']
            require(type(key) is str and key.startswith('derived_probe_') and key not in local,
                    'input_measurement_key')
            local[key] = row
            if row['disposition'] == 'prepared':
                require([j['question_id'] for j in row['jobs']] == list(QUESTIONS), 'three_direct_instruments')
                for job in row['jobs']:
                    require(job['family'] == bank['family'] and job['model'] == bank['model']
                            and job['run_id'] == key and job['original_run_id'] == row['run_id']
                            and job['stage'] == 'early' and job['protocol_sha256'] == row['protocol_sha256']
                            and job['derived_view_canonical_sha256'] == row['view_canonical_sha256']
                            and job['context_canonical_sha256'] == row['context_canonical_sha256']
                            and type(job['seed']) is int and type(job['original_seed']) is int
                            and job['original_seed'] == row['original']['seed']
                            and job['measurement_id'] == '/'.join(job[k] for k in
                                ('family', 'model', 'run_id', 'stage', 'question_id')), 'input_job_identity')
                requests += 3
            else:
                require(row['disposition'] == 'unavailable' and row['jobs'] == [] and row['view'] is None
                        and type(row['reason']) is str and row['reason'], 'unavailable_input')
                missing += 1
        require(type(pairs) is list and 2 * len(pairs) == len(current), 'input_pair_count')
        seen_pairs = set()
        for pair in pairs:
            identity = (pair['task_id'], pair['draw_key'], pair['variant'])
            require(identity not in seen_pairs and set(pair['arms']) == {'baseline', 'treatment'}, 'input_pair')
            seen_pairs.add(identity)
            for role, arm in pair['arms'].items():
                key = arm['measurement_key']
                require(key in local and key not in local_roles
                        and arm == {k: local[key][k] for k in ('run_id', 'measurement_key', 'disposition')},
                        'input_pair_membership')
                local_roles[key] = role
        require(set(local) == set(local_roles), 'unpaired_input')
        count = dict(assigned=len(current), ready=len(current)-missing, unavailable=missing,
                     requests=requests, missing_slots=missing*3)
        require(prep['counts'] == count and prep['actual_jobs'] == requests, 'input_counts')
        added, shared = 0, 0
        for key, row in local.items():
            if key in rows:
                require(roles[key] == local_roles[key] == 'baseline' and row == rows[key],
                        'shared_baseline_changed')
                shared += len(row['jobs'])
            else:
                rows[key], roles[key] = row, local_roles[key]
                jobs.extend(row['jobs']); added += len(row['jobs'])
        counts.append(dict(preparation=descriptor['actual'], new_jobs=added, shared_jobs=shared, counts=count))
        comparisons.append(dict(preparation=descriptor, pairs=prep['pairs'], members=copy.deepcopy(pairs)))
    ids = unique([j['measurement_id'] for j in jobs], 'duplicate_bank_job')
    require(bank['job_count'] == len(jobs) and bank['jobs_sha256'] == digest(jobs)
            and bank['ids_sha256'] == digest(ids) and bank['source_counts'] == counts
            and bank['unique_assigned_slots'] == 3*len(rows)
            and bank['unique_missing_slots'] == 3*sum(r['disposition'] == 'unavailable' for r in rows.values()),
            'frozen_bank_membership')
    return rows, jobs, protocol, comparisons


def _verified(job, row, value, terminal, protocol):
    require(value['measurement_id'] == job['measurement_id'] and value['disposition'] == 'final_verified'
            and value['integrity_failure'] is False and value['input_canonical_sha256'] == digest(job),
            'verified_job_identity')
    for field in ('context_canonical_sha256', 'question_canonical_sha256', 'seed', 'request_body_sha256'):
        require(value[field] == job[field], 'verified_request_identity:' + field)
    require(value['derived_view'] == row['view'] and value['context'] == row['view']['file'], 'verified_view')
    binding = dict(schema='causes.derived_probe_final_binding.v1', config=protocol['config'],
                   context=row['view']['file'], provenance=[*row['origin'].values(), *row['provenance'],
                                                         *protocol['sources'].values()])
    require(value['derived_input_binding'] == binding, 'verified_input_binding')
    require(terminal['final'] == value['final'] and terminal['technical_status'] == value['technical_status']
            and terminal['event'] == ('route_success' if value['technical_status'] == 'complete' else 'route_failure'),
            'verified_terminal_identity')
    captures = {}
    for ref in value['capture_files']:
        storage._pin_shape(ref, storage.ABSOLUTE_LIMIT)
        name = ref['path'].rsplit('/', 1)[-1]
        require(name not in captures, 'duplicate_capture_name')
        captures[name] = ref
    if 'request.json' in captures:
        require(captures['request.json']['sha256'] == job['request_file_sha256'], 'verified_request_file')
    parsed, status = value['raw_result'], value['technical_status']
    require(value['label'] == value['admissible_label'], 'verified_label_alias')
    if parsed is not None:
        require(value['offline_verification'] == 'exact_frozen_request_render_and_parser_reconstructed'
                and {'request.json', 'attempt.json', 'receipt.json', 'response.raw', 'result.json'} <= captures.keys()
                and parsed['technical_status'] == status and parsed.get('answer') == value['raw_parsed_label'],
                'verified_parser_custody')
    else:
        require(status != 'complete' and value['raw_parsed_label'] is None
                and value['offline_verification'] == 'technical_missing_no_parsed_result', 'missing_parser_custody')
    label = self_label(parsed)
    require(value['admissible_label'] == label and (status != 'complete' or label in SELF_LABELS),
            'verified_native_label')
    return label


def resolve_saved_direct(bank, closed_sources, *, maximum_document_bytes=8*1024**2,
                         maximum_payload_bytes=128*1024**2, maximum_result_bytes=12*1024**2):
    """Return every assigned slot once, with accepted per-invocation provenance.

    ``bank`` and each ordered ``closed_sources`` entry are exact {actual,review}
    pin descriptors. Sources begin at the bank's execution predecessor and may
    end at a partial clean cut; untouched jobs then remain explicitly pending.
    Captures and prompts are referenced, never copied or re-parsed. The caller
    separately authenticates review authority and freezes scientific mappings.
    """
    saved = _Saved(maximum_document_bytes, maximum_payload_bytes)
    adopted, _ = saved.accepted(bank, 'accepted_derived_supplement_bank')
    require(adopted['schema'] == 'causes.derived_supplement_bank_adoption.v1'
            and adopted['root_authorized'] is True and adopted['family'].startswith('derived_probe_'), 'accepted_bank')
    rows, jobs, protocol, comparisons = _inputs(adopted, saved)
    config = saved.value(protocol['config'])
    saved.raw(protocol['sources']['campaign_runner'])
    instrument_protocols = {}
    for question_id in QUESTIONS:
        candidates = [q for q in config['questions'] if q.get('id') == question_id]
        require(len(candidates) == 1, 'native_question_membership')
        instrument_protocols[question_id] = direct_instrument_protocol(config, protocol['sources']['campaign_runner'], candidates[0])
    require(all(j['question_canonical_sha256'] == digest(instrument_protocols[j['question_id']]['question'])
                for j in jobs), 'frozen_question_semantics')
    ids = [j['measurement_id'] for j in jobs]; by_job = {j['measurement_id']: j for j in jobs}
    by_row = {j['measurement_id']: row for row in rows.values() for j in row['jobs']}
    previous_descriptor = adopted['execution_predecessor']
    previous, previous_peer = saved.accepted(previous_descriptor, 'accepted_finite_supplement_closed_custody')
    require(previous['unstarted_ids'] == [] and previous_peer['original_denominator_preserved'] is True
            and previous_peer['all_original_attempts_retained'] is True
            and previous_peer['qualification_findings'] == previous['qualification_findings'], 'bank_predecessor')
    owner = unique(previous['complete_ids'], 'owner_ids'); resolved = {}; offset = 0; origins = []
    require(not set(owner).intersection(ids) and type(closed_sources) is list, 'bank_owner_overlap')
    for descriptor in closed_sources:
        actual, peer = saved.accepted(descriptor, 'accepted_finite_supplement_closed_custody')
        require(peer.get('accepted_completed_actor_saved_closure') is True and peer.get('source') == actual['source']
                and peer['original_denominator_preserved'] is True and peer['all_original_attempts_retained'] is True
                and peer['qualification_findings'] == actual['qualification_findings'], 'derived_closed_peer')
        require(actual['schema'] == 'causes.finite_supplement_tranche_closed.v1'
                and actual['clean_native_ending'] is True and type(actual['active']) is int and actual['active'] == 0
                and actual['unresolved_claimed_ids'] == [] and actual['ambiguous'] == []
                and actual['original_denominator_preserved'] is True and actual['all_original_attempts_retained'] is True
                and actual['prior_closed'] == previous_descriptor['actual']
                and actual['prior_closed_review'] == previous_descriptor['review'], 'ordered_closed_custody')
        extension = actual['derived_bank_extension']
        require(extension['schema'] == 'causes.derived_supplement_owner_bank_extension.v1'
                and extension['adoption'] == bank and extension['execution_predecessor'] == adopted['execution_predecessor']
                and extension['prior_prefix_unchanged'] is True and extension['native_families_preserved'] is True
                and extension['prior_count'] == len(owner) and extension['cumulative_count'] == len(owner)+len(jobs)
                and extension['appended_count'] == len(jobs) and extension['appended_jobs_sha256'] == digest(jobs)
                and extension['appended_ids_sha256'] == digest(ids)
                and actual['derived_scientific_protocol'] == protocol, 'closed_bank_extension')
        plan = saved.value(actual['plan']); recipe = saved.value(actual['recipe'])
        execution_protocol = saved.value(actual['derived_execution_protocol'])
        require(execution_protocol == {**protocol, 'config': execution_protocol['config']}, 'execution_protocol_sources')
        original_config, config = saved.value(protocol['config']), saved.value(execution_protocol['config'])
        require({k:v for k,v in config.items() if k != 'endpoint'} ==
                {k:v for k,v in original_config.items() if k != 'endpoint'}, 'execution_protocol_semantics')
        require(plan['schema'] == 'causes.completed_actor_driver_plan.v1'
                and plan['derived_bank'] == bank and plan['predecessor_custody'] == previous_descriptor
                and plan['occupied_ids'] == previous['complete_ids'] and plan['initial_failures'] == previous['consecutive_failures']
                and plan['family'] == adopted['family'] and plan['model'] == adopted['model']
                and plan['output_root'] == actual['output'] and recipe['plan'] == actual['plan']
                and recipe['source'] == actual['source'] and recipe['driver_result'] == actual['native_result'], 'closed_plan_join')
        maximum = plan['maximum_new_requests']
        require(type(maximum) is int and 0 < maximum <= len(jobs)-offset
                and plan['jobs_sha256'] == digest(jobs[offset:offset+maximum]), 'closed_selected_prefix')
        verified = saved.payload(actual['verified_finals'])
        mids = unique([v['measurement_id'] for v in verified], 'duplicate_verified_final')
        require(mids == ids[offset:offset+len(mids)] and len(mids) <= maximum, 'verified_claim_prefix')
        journal = saved.raw(actual['journal'], maximum_payload_bytes)
        require(not journal or journal.endswith(b'\n'), 'closed_journal_truncated')
        claims, terminals = [], {}
        for event in (storage._json(line) for line in journal.splitlines()):
            if event['event'] == 'claimed':
                require(event['measurement_id'] not in claims, 'duplicate_claim')
                claims.append(event['measurement_id'])
                require(event['input_sha256'] == digest(by_job[event['measurement_id']]), 'claim_job_identity')
            elif event['event'] in ('route_success', 'route_failure'):
                mid = event['measurement_id']
                require(mid in claims and mid not in terminals, 'duplicate_or_unclaimed_terminal')
                terminals[mid] = event
        require(claims == mids and set(terminals) == set(mids), 'closed_terminal_membership')
        good = unique(actual['new_scientifically_qualified_ids'], 'qualified_ids')
        bad = unique(actual['new_invalid_ids'], 'invalid_ids')
        require(not set(good).intersection(bad) and set(good+bad) == set(mids)
                and actual['inherited_qualification_findings'] == previous['qualification_findings']
                and actual['qualification_findings'] == previous['qualification_findings'] + actual['current_qualification_findings'],
                'qualification_partition')
        invocation = actual['native_invocations'][-1]
        require(invocation['plan'] == actual['plan'] and invocation['source'] == plan['sources']['driver']
                and invocation['result'] == actual['native_result'] and invocation['derived_bank'] == bank
                and invocation['family'] == plan['family'] and invocation['model'] == plan['model']
                and invocation['jobs_sha256'] == plan['jobs_sha256'], 'native_invocation_origin')
        origins.append(dict(closed=descriptor, plan=actual['plan'], source=actual['source'],
            journal=actual['journal'], verified_finals=actual['verified_finals'], native_invocation=invocation))
        for index, value in enumerate(verified):
            mid = value['measurement_id']; job, row = by_job[mid], by_row[mid]
            native = None if 'qualification_error' in value else _verified(job, row, value, terminals[mid], execution_protocol)
            require('qualification_error' not in value or mid in bad, 'qualified_final_error')
            reasons = [f for f in actual['current_qualification_findings'] if f.get('measurement_id') == mid]
            require(mid not in bad or reasons, 'missing_qualification_reason')
            status = (('invalid_admission' if all(f['stage'] == 'native_admission' for f in reasons)
                       else 'qualification_failure') if mid in bad else
                      'observed' if native is not None else 'technical_missing')
            reason = (reasons if mid in bad else None if native is not None else
                      value.get('reason') or value.get('failure') or 'native_technical_' + value['technical_status'])
            resolved[mid] = dict(label=native if status == 'observed' else None, status=status, reason=reason,
                native_label=native, origin=dict(source_index=len(origins)-1, index=index, final=terminals[mid]['final']))
        offset += len(mids)
        require(actual['complete_ids'] == owner + ids[:offset] and actual['unstarted_ids'] == ids[offset:]
                and actual['terminal'] == actual['occupied'] == len(owner)+offset, 'closed_cumulative_partition')
        previous, previous_descriptor = actual, descriptor
    output = []
    for row in rows.values():
        for index, threshold in enumerate(THRESHOLDS):
            job = row['jobs'][index] if row['disposition'] == 'prepared' else None
            mid = job['measurement_id'] if job else '/'.join((adopted['family'], adopted['model'],
                  row['measurement_key'], 'early', QUESTIONS[index]))
            value = (resolved.get(mid, dict(label=None, status='pending', reason='unclaimed_at_accepted_cut', origin=None))
                     if job else dict(label=None, status='unavailable', reason=row['reason'], origin=None))
            output.append(dict(measurement_id=mid, original_run_id=row['run_id'], measurement_key=row['measurement_key'],
                view_kind=row['view_kind'], stage='early', method='self', awareness_type='evaluation', threshold=threshold,
                derived_protocol_sha256=row['protocol_sha256'],
                instrument_protocol_sha256=digest(instrument_protocols[QUESTIONS[index]]),
                context_canonical_sha256=job['context_canonical_sha256'] if job else None,
                job=copy.deepcopy(job), **copy.deepcopy(value)))
    result = dict(schema='causes.saved_derived_direct_readout.v1', bank=copy.deepcopy(bank),
                  closed_sources=copy.deepcopy(closed_sources), origins=origins, measurements=output, comparisons=comparisons,
                  instrument_protocols=instrument_protocols,
                  assigned=len(output), ready_jobs=len(jobs), claimed=offset, pending=len(jobs)-offset,
                  unavailable=adopted['unique_missing_slots'], inference_released=False)
    require(type(maximum_result_bytes) is int and 0 < maximum_result_bytes <= storage.ABSOLUTE_LIMIT
            and len(encoded(result)) <= maximum_result_bytes, 'saved_readout_result_bound')
    saved.unchanged()
    return result


def describe_direct(specification, specification_review, resolved, *, maximum_document_bytes=8*1024**2):
    """Apply the existing frozen three-threshold contract to a resolved registry.

    ``resolved`` must be the unchanged result of resolve_saved_direct or an
    independently accepted exact saved copy. This function performs no response
    verification. It validates full pair membership before using any labels.
    """
    saved = _Saved(maximum_document_bytes, maximum_document_bytes)
    spec, peer = saved.value(specification), saved.value(specification_review)
    require(peer.get('accepted_frozen_derived_probe_readout_specification') is True
            and peer.get('findings') == [] and peer['specification'] == specification
            and peer['preparation'] == spec['input_preparation'], 'direct_specification_peer')
    require(spec['schema'] == 'causes.frozen_derived_probe_readout_specification.v1'
            and spec['function'] == 'contrast' and spec['function_parameters'] == dict(release=False, family_size=10)
            and spec['measurement_type'] == 'appended_direct_self_report' and spec['label_function'] == 'self_label'
            and spec['awareness_type'] == 'evaluation' and spec['thresholds'] == list(THRESHOLDS)
            and spec['question_ids'] == list(QUESTIONS) and spec['inference_released'] is False
            and spec['planned_family_size'] == 10 and spec['separate_verbalized_awareness_required'] is True,
            'direct_descriptive_contract')
    saved.raw(spec['analysis_source'])
    saved.raw(dict(spec['analysis_source'], path=analysis.__file__))
    require(resolved['schema'] == 'causes.saved_derived_direct_readout.v1'
            and resolved['inference_released'] is False, 'resolved_direct_registry')
    matches = [c for c in resolved['comparisons'] if c['preparation']['actual'] == spec['input_preparation']]
    require(len(matches) == 1 and matches[0]['pairs'] == spec['pairs'], 'direct_specification_membership')
    pairs = saved.value(spec['pairs'])
    require(pairs == matches[0]['members'], 'direct_frozen_pairs')
    prep = saved.value(spec['input_preparation'])
    require(spec['rows'] == prep['rows'] and spec['protocol'] == prep['protocol'], 'direct_specification_inputs')
    members, identities = {}, {}
    for pair in pairs:
        arms = members.setdefault(pair['task_id'], dict(control=[], treatment=[]))
        for arm, target in (('baseline', 'control'), ('treatment', 'treatment')):
            item = pair['arms'][arm]; key = item['measurement_key']
            require(key not in identities, 'duplicate_direct_pair_arm')
            identities[key] = item; arms[target].append(key)
    require(spec['assignment']['members'] == members and spec['counts'] == dict(
        assigned_arms=len(identities), pairs=len(pairs), ready_arms=prep['counts']['ready'],
        tasks=len(members), unavailable_arms=prep['counts']['unavailable']), 'direct_assignment_counts')
    labels = {name:{} for name in THRESHOLDS}; states = {name:{} for name in THRESHOLDS}
    for row in resolved['measurements']:
        key, threshold = row['measurement_key'], row['threshold']
        if key not in identities:
            continue
        require(threshold in labels and key not in labels[threshold] and row['stage'] == 'early'
                and row['method'] == 'self' and row['awareness_type'] == 'evaluation'
                and row['original_run_id'] == identities[key]['run_id'], 'direct_resolved_identity')
        label = row['label']
        require((row['status'] == 'observed' and label in SELF_LABELS)
                or (row['status'] in ('technical_missing', 'unavailable', 'pending',
                                      'invalid_admission', 'qualification_failure') and label is None),
                'direct_resolved_label')
        labels[threshold][key] = label; states[threshold][key] = row['status']
    require(all(set(v) == set(identities) for v in labels.values()), 'direct_missing_instrument_slot')
    fields = ('factor', 'task_count', 'assigned', 'observed', 'all_assigned_observed', 'equal_task_effect',
              'category_equal_task_weights', 'cells', 'missing_assigned_sample_effect_bounds')
    output = {}
    for threshold, values in labels.items():
        value = contrast(spec['assignment'], values, release=False, family_size=10)
        output[threshold] = {k:value[k] for k in fields}
    saved.unchanged()
    return dict(schema='causes.saved_derived_direct_contrasts.v1', specification=specification,
                specification_review=specification_review, contrasts=output, dispositions=states,
                inference_released=False, missing_bounds_are_confidence_intervals=False)
