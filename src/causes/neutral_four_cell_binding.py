"""Bind authenticated saved registries to four-cell arguments without I/O.

The caller authenticates all parsed inputs, source/peer pins and frozen input
order. This module joins their identities; it does not authenticate files,
rebuild requests, parse responses, qualify evidence or run an analysis.
"""
from collections import Counter
import copy
import hashlib
import json
import re

from causes.campaign_analysis import SELF_LABELS, READER_LABELS
from causes.neutral_four_cell import INSTRUMENTS, THRESHOLDS, STATUSES, require


FIELDS = {'id', 'original_run_id', 'stage', 'method', 'awareness_type', 'threshold',
          'context_sha256', 'protocol_sha256', 'label', 'status', 'reason'}


def _text(value):
    return type(value) is str and bool(value.strip())


def _hash(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _pin(ref):
    require(type(ref) is dict and set(ref) == {'path', 'bytes', 'sha256'}
            and _text(ref['path']) and type(ref['bytes']) is int and ref['bytes'] >= 0
            and _hash(ref['sha256']), 'input_pin_shape')


def _descriptor(ref):
    require(type(ref) is dict and set(ref) == {'data', 'review'}, 'input_descriptor')
    for pin in ref.values():
        _pin(pin)


def _records(rows):
    require(type(rows) is list, 'registry_rows')
    result = {}
    for row in rows:
        require(type(row) is dict and set(row) == FIELDS and _text(row['id'])
                and row['id'] not in result and _text(row['original_run_id'])
                and row['stage'] == 'early' and row['method'] in ('self', 'reader')
                and row['awareness_type'] == 'evaluation' and row['threshold'] in THRESHOLDS
                and _hash(row['protocol_sha256']), 'registry_identity')
        require(row['status'] in STATUSES and
                (row['context_sha256'] is None if row['status'] == 'unavailable'
                 else _hash(row['context_sha256'])), 'registry_status_context')
        labels = SELF_LABELS if row['method'] == 'self' else READER_LABELS
        require((row['status'] == 'observed' and row['label'] in labels and row['reason'] is None)
                or (row['status'] != 'observed' and row['label'] is None and _text(row['reason'])),
                'registry_native_label')
        result[row['id']] = row
    return result


def _membership(value):
    require(value.get('schema') == 'causes.frozen_neutral_matched_member_mapping.v1'
            and type(value.get('comparisons')) is list and value['comparisons'], 'membership_schema')
    variants, all_runs, included_runs, baseline_aliases, entries = set(), {}, set(), {}, []
    for c in value['comparisons']:
        require(_text(c.get('variant')) and c['variant'] not in variants
                and _text(c.get('baseline')) and c.get('stage') == 'early'
                and type(c.get('members')) is list and c['members'], 'frozen_comparison')
        variants.add(c['variant'])
        _pin(c['direct_rows'])
        pairs, actors, direct, reader = set(), set(), {}, {}
        views = {arm: set() for arm in ('baseline', 'treatment')}
        count = 0
        for m in c['members']:
            require(_text(m.get('task_id')) and type(m.get('draw_key')) is int
                    and m['draw_key'] >= 0, 'frozen_task_draw')
            pair = (m['task_id'], m['draw_key'])
            require(pair not in pairs, 'duplicate_frozen_pair')
            pairs.add(pair)
            if m.get('disposition') == 'included':
                require(set(m) == {'task_id', 'draw_key', 'disposition', 'arms'}
                        and type(m['arms']) is dict and set(m['arms']) == {'baseline', 'treatment'},
                        'included_member')
                runs = {arm: item['original_run_id'] for arm, item in m['arms'].items()}
                count += 1
                for arm, item in m['arms'].items():
                    require(set(item) == {'direct_key', 'reader_key', 'original_run_id',
                        'input_disposition', 'neutral_view'} and _text(item['direct_key'])
                        and _text(item['reader_key']) and _text(item['neutral_view'])
                        and item['neutral_view'] != 'original'
                        and item['input_disposition'] in ('prepared', 'unavailable'), 'frozen_arm')
                    require(item['direct_key'] not in direct and item['reader_key'] not in reader,
                            'duplicate_frozen_arm')
                    direct[item['direct_key']] = (arm, item)
                    reader[item['reader_key']] = (arm, item)
                    views[arm].add(item['neutral_view'])
                    included_runs.add(item['original_run_id'])
                    if arm == 'baseline':
                        key = (*pair, item['original_run_id'], item['neutral_view'])
                        require(key not in baseline_aliases or baseline_aliases[key] == item,
                                'shared_baseline_membership_changed')
                        baseline_aliases[key] = item
            else:
                require(set(m) == {'task_id', 'draw_key', 'disposition', 'reason',
                    'baseline_run_id', 'treatment_run_id'} and m['disposition'] == 'unsupported'
                    and _text(m['reason']), 'unsupported_member')
                runs = {arm: m[arm + '_run_id'] for arm in ('baseline', 'treatment')}
            require(runs['baseline'] != runs['treatment'], 'same_actor_both_arms')
            for run in runs.values():
                require(_text(run) and run not in actors, 'repeated_actor_in_comparison')
                actors.add(run)
                require(run not in all_runs or all_runs[run] == pair, 'actor_task_draw_changed')
                all_runs[run] = pair
        require(type(c.get('fixed_pairs')) is int and c['fixed_pairs'] == len(c['members'])
                and type(c.get('included_pairs')) is int and c['included_pairs'] == count
                and c.get('included_tasks') == len({m['task_id'] for m in c['members']
                                                   if m['disposition'] == 'included'})
                and all(len(v) == 1 for v in views.values()), 'frozen_counts_views')
        entries.append(dict(value=c, direct=direct, reader=reader,
                            views={arm: next(iter(v)) for arm, v in views.items()}))
    return entries, included_runs, baseline_aliases


def _protocols(membership, original, neutral):
    protocols = original['protocols']
    require(type(protocols) is dict and set(protocols) == {'self', 'reader'}
            and all(type(p) is dict and set(p) == set(THRESHOLDS) for p in protocols.values())
            and neutral['protocols'] == protocols['reader'], 'configured_protocol_roster')
    direct = membership['neutral_direct_semantic_protocols']
    require(type(direct) is list and len(direct) == len(THRESHOLDS), 'direct_protocol_roster')
    names = set()
    for item in direct:
        name = item['semantic_protocol']['question']['threshold']
        require(name in THRESHOLDS and name not in names and item['question_id'] == 'evaluation_' + name
                and item['semantic_protocol'] == protocols['self'][name]
                and item['sha256'] == _digest(item['semantic_protocol']), 'direct_protocol_join')
        names.add(name)
    for name, p in protocols['reader'].items():
        require(p.get('schema') == 'causes.saved_reader_instrument_protocol.v1'
                and p.get('instrument') == name and p.get('phase') == 'prefix'
                and p.get('awareness_type') == 'evaluation', 'reader_protocol_scope')
    return {method: {name: _digest(p) for name, p in values.items()} for method, values in protocols.items()}


def _original(value, runs, protocols):
    require(value.get('schema') == 'causes.saved_original_measurement_registry.v1'
            and value.get('inference_released') is False, 'original_registry_schema')
    rows = _records(value['measurements'])
    provenance = {}
    for p in value['provenance']:
        require(p['id'] in rows and p['id'] not in provenance, 'original_provenance_roster')
        physical = p['physical']; row = rows[p['id']]
        if row['method'] == 'self':
            require(set(physical) == {'kind', 'source', 'summary_sha256', 'stage', 'instrument'}
                    and physical['kind'] == 'native_snapshot_measurement'
                    and _text(physical['source']) and _hash(physical['summary_sha256'])
                    and physical['instrument'] == 'evaluation_' + row['threshold']
                    and p['native_context_sha256'] == row['context_sha256'], 'original_self_identity')
        else:
            require(set(physical) == {'kind', 'result', 'stage', 'instrument'}
                    and physical['kind'] == 'native_reader_result'
                    and physical['instrument'] == row['threshold']
                    and (p['native_context_sha256'] is None or _hash(p['native_context_sha256'])),
                    'original_reader_identity')
            _pin(physical['result'])
        require(physical['stage'] == 'early' and p['id'] == 'original_record_' + _digest(physical)
                and row['protocol_sha256'] == protocols[row['method']][row['threshold']],
                'original_record_protocol')
        provenance[p['id']] = p
    require(set(provenance) == set(rows), 'original_provenance_complete')
    ledger, used, contexts = {}, set(), {}
    for a in value['assignments']:
        require(set(a) == {'original_run_id', 'stage', 'method', 'threshold', 'disposition',
                          'measurement_id', 'record_state'} and a['stage'] == 'early', 'original_assignment')
        key = (a['original_run_id'], a['method'], a['threshold'])
        require(key not in ledger and key[0] in runs and key[1] + ':' + key[2] in INSTRUMENTS,
                'original_assignment_roster')
        identity = a['measurement_id']
        if a['record_state'] == 'present':
            require(a['disposition'] == 'assigned' and identity in rows and identity not in used,
                    'original_present_assignment')
            row = rows[identity]
            require((row['original_run_id'], row['method'], row['threshold']) == key, 'original_assignment_identity')
            used.add(identity)
            ck = key[:2]
            require(ck not in contexts or contexts[ck] == row['context_sha256'], 'original_context_conflict')
            contexts[ck] = row['context_sha256']
        else:
            require(identity is None and ((a['disposition'], a['record_state']) in
                    (('not_assigned', 'not_assigned'), ('assigned', 'absent_instance'))),
                    'original_nonphysical_assignment')
        ledger[key] = a
    require(set(ledger) == {(run, method, threshold) for run in runs
            for method in ('self', 'reader') for threshold in THRESHOLDS} and used == set(rows),
            'original_complete_assignments')
    return ledger, {k: dict(copy.deepcopy(v), identity_kind='native_record') for k, v in rows.items()}, contexts


def _neutral_reader(value, entries, protocols):
    require(value.get('schema') == 'causes.saved_neutral_reader_registry.v1'
            and value.get('inference_released') is False, 'neutral_reader_schema')
    rows = _records(value['measurements']); origins = value['origins']
    require(set(origins) == set(rows), 'reader_origin_roster')
    expected = {(entry['value']['variant'], key): item
                for entry in entries for key, (_, item) in entry['reader'].items()}
    assignments, used, aliases, normalized = {}, set(), Counter(), {}
    for a in value['assignments']:
        require(set(a) == {'variant', 'measurement_key', 'instruments'}
                and type(a['instruments']) is dict and set(a['instruments']) == set(THRESHOLDS),
                'reader_assignment_shape')
        key = (a['variant'], a['measurement_key'])
        require(key in expected and key not in assignments, 'reader_assignment_membership')
        arm = expected[key]
        for name, identity in a['instruments'].items():
            require(identity in rows, 'reader_assignment_record')
            row, origin = rows[identity], origins[identity]
            physical = origin['physical']
            require(row['original_run_id'] == arm['original_run_id'] and row['method'] == 'reader'
                    and row['threshold'] == name and row['protocol_sha256'] == protocols['reader'][name]
                    and physical['context_key'] == key[1] and physical['view_kind'] == arm['neutral_view'],
                    'reader_assignment_identity')
            if physical['request'] is None:
                require(arm['input_disposition'] == 'unavailable' and row['status'] == 'unavailable'
                        and physical['result'] is None and row['context_sha256'] is None
                        and identity == 'reader_unavailable_' + _digest([key[1], name]), 'reader_unavailable_identity')
                kind = 'assigned_slot'
            else:
                require(arm['input_disposition'] == 'prepared' and row['status'] != 'unavailable'
                        and identity == 'reader_physical_' + _digest(physical['request']), 'reader_request_identity')
                _pin(physical['request']); _pin(physical['result']); kind = 'physical_request'
            normalized[identity] = dict(copy.deepcopy(row), identity_kind=kind)
            used.add(identity); aliases[(identity, key[0])] += 1
        assignments[key] = a['instruments']
    require(set(assignments) == set(expected) and used == set(rows), 'reader_complete_membership')
    actual_aliases = Counter((identity, alias['variant']) for identity, origin in origins.items() for alias in origin['aliases'])
    require(actual_aliases == aliases, 'reader_alias_membership')
    by_key = {}
    for (_, key), ids in assignments.items():
        require(key not in by_key or ids == by_key[key], 'shared_reader_key_identity')
        by_key[key] = ids
    return by_key, normalized


def _direct_inputs(inputs, entries, original_protocols):
    require(type(inputs) is list and len(inputs) == len(entries), 'direct_inputs')
    by_variant = {e['value']['variant']: e for e in entries}
    seen, rows, canonical, aliases, job_ids, common = set(), {}, {}, {}, set(), None
    sources = []
    for index, item in enumerate(inputs):
        require(type(item) is dict and set(item) == {'variant', 'rows', 'preparation', 'review', 'value'}
                and item['variant'] in by_variant and item['variant'] not in seen
                and type(item['value']) is list, 'direct_input_descriptor')
        seen.add(item['variant']); entry = by_variant[item['variant']]
        for k in ('rows', 'preparation', 'review'):
            _pin(item[k])
        require(item['rows'] == entry['value']['direct_rows'], 'direct_rows_source')
        sources.append({k: copy.deepcopy(v) for k, v in item.items() if k != 'value'})
        local = set()
        for ri, row in enumerate(item['value']):
            key = row['measurement_key']
            require(key in entry['direct'] and key not in local and row['schema'] == 'causes.derived_probe_input.v1'
                    and row['stage'] == 'early' and row['runtime_authorized'] is False, 'direct_input_row')
            local.add(key); role, arm = entry['direct'][key]
            require(row['run_id'] == arm['original_run_id'] and row['view_kind'] == arm['neutral_view']
                    and row['disposition'] == arm['input_disposition'] and _hash(row['protocol_sha256']),
                    'direct_frozen_arm')
            if row['disposition'] == 'prepared':
                require(_hash(row['context_canonical_sha256']) and _hash(row['view_canonical_sha256'])
                        and type(row['original']['seed']) is int
                        and [j['question_id'] for j in row['jobs']] == ['evaluation_' + n for n in THRESHOLDS],
                        'direct_instrument_roster')
                for job, name in zip(row['jobs'], THRESHOLDS):
                    scope = (job['family'], job['model'], job['protocol_sha256'])
                    require(all(_text(x) for x in scope) and (common is None or scope == common), 'direct_job_scope')
                    common = scope
                    require(job['run_id'] == key and job['original_run_id'] == row['run_id']
                            and job['stage'] == 'early' and job['protocol_sha256'] == row['protocol_sha256']
                            and job['context_canonical_sha256'] == row['context_canonical_sha256']
                            and job['derived_view_canonical_sha256'] == row['view_canonical_sha256']
                            and job['question_canonical_sha256'] == _digest(original_protocols[name]['question'])
                            and type(job['seed']) is int and job['original_seed'] == row['original']['seed']
                            and type(job['original_seed']) is int and _hash(job['request_body_sha256'])
                            and _hash(job['request_file_sha256']) and type(job['request_body_bytes']) is int
                            and job['request_body_bytes'] > 0
                            and job['measurement_id'] == '/'.join(job[k] for k in
                                ('family', 'model', 'run_id', 'stage', 'question_id')), 'direct_job_identity')
            else:
                require(row['disposition'] == 'unavailable' and row['jobs'] == [] and row['view'] is None
                        and _text(row['reason']), 'direct_unavailable_input')
            if key in rows:
                require(role == canonical[key]['role'] == 'baseline' and row == rows[key], 'shared_direct_row_changed')
            else:
                rows[key] = row; canonical[key] = dict(role=role, source=index, row=ri)
                for job in row['jobs']:
                    require(job['measurement_id'] not in job_ids, 'duplicate_direct_job')
                    job_ids.add(job['measurement_id'])
            aliases.setdefault(key, []).append([index, ri, role])
        require(local == set(entry['direct']), 'direct_complete_input')
    require(seen == set(by_variant), 'direct_complete_cohorts')
    return rows, canonical, dict(sources=sources, rows=[dict(key=k, canonical=canonical[k], aliases=v)
        for k, v in aliases.items()], prepared_jobs=len(job_ids), unavailable_slots=3*sum(r['disposition'] == 'unavailable' for r in rows.values()))


def _direct_results(value, rows, protocols):
    if value is None:
        return {}, {}, []
    require(value.get('schema') == 'causes.saved_derived_direct_readout.v1'
            and value.get('inference_released') is False
            and value['instrument_protocols'] == {'evaluation_' + k: v for k, v in protocols.items()},
            'direct_result_protocol')
    index, registry, rendered_reasons = {}, {}, []
    scopes = {(j['family'], j['model']) for row in rows.values() for j in row['jobs']}
    for v in value['measurements']:
        key, name = v['measurement_key'], v['threshold']
        require(key in rows and name in THRESHOLDS and (key, name) not in index, 'direct_result_roster')
        row = rows[key]; job = row['jobs'][THRESHOLDS.index(name)] if row['disposition'] == 'prepared' else None
        require(v['original_run_id'] == row['run_id'] and v['view_kind'] == row['view_kind']
                and v['stage'] == 'early' and v['method'] == 'self' and v['awareness_type'] == 'evaluation'
                and v['job'] == job and v['derived_protocol_sha256'] == row['protocol_sha256']
                and v['instrument_protocol_sha256'] == _digest(protocols[name])
                and v['context_canonical_sha256'] == (job['context_canonical_sha256'] if job else None),
                'direct_result_input_identity')
        require(_text(v['measurement_id']) and v['measurement_id'] not in registry
                and (v['measurement_id'] == job['measurement_id'] if job is not None else
                     v['measurement_id'].endswith('/' + key + '/early/evaluation_' + name))
                and (job is not None or not scopes or v['measurement_id'] ==
                     '/'.join((*next(iter(scopes)), key, 'early', 'evaluation_' + name))), 'direct_result_id')
        if v['status'] in ('pending', 'unavailable'):
            require(v['origin'] is None and (v['status'] == 'unavailable') == (job is None), 'direct_assigned_slot')
            kind = 'assigned_slot'
        else:
            require(job is not None and type(v['origin']) is dict and type(v['origin']['source_index']) is int
                    and 0 <= v['origin']['source_index'] < len(value['origins']), 'direct_native_final')
            _pin(v['origin']['final']); kind = 'native_record'
        reason = v['reason']
        if type(reason) in (list, dict):
            require(bool(reason) and v['status'] != 'observed', 'structured_direct_missing_reason')
            reason = 'native_reason_json:' + _encoded(reason).decode()
            rendered_reasons.append(dict(id=v['measurement_id'], native_reason=copy.deepcopy(v['reason']),
                                         consumer_reason=reason))
        normalized = dict(id=v['measurement_id'], original_run_id=v['original_run_id'], stage='early',
            method='self', awareness_type='evaluation', threshold=name, context_sha256=v['context_canonical_sha256'],
            protocol_sha256=v['instrument_protocol_sha256'], label=v['label'], status=v['status'], reason=reason)
        _records([normalized])
        registry[normalized['id']] = dict(normalized, identity_kind=kind)
        index[key, name] = normalized['id']
    require(set(index) == {(k, name) for k in rows for name in THRESHOLDS}, 'direct_result_complete_membership')
    return index, registry, rendered_reasons


def bind_four_cells(membership, original, neutral_reader, direct_inputs, *, evidence,
                    neutral_direct=None, maximum_result_bytes=64*1024**2):
    """Return consumer arguments and compact provenance from authenticated values.

    ``evidence`` contains membership/original/neutral_reader descriptors
    {data,review}, qualification=pin, and neutral_direct=None or {data,review}.
    Each ordered direct_inputs item has variant, rows/preparation/review pins,
    and value=the already authenticated parsed ROWS array. Freeze this input
    order separately: it selects canonical first baseline rows; comparison
    display order remains membership order. Whole individual shared rows must
    agree; different outer source descriptors are retained as aliases.

    Existing original records are native_record, never presumed HTTP requests.
    Neutral reader request pins identify physical_request; unavailable records
    are assigned_slot. Without an accepted direct result, prepared direct cells
    are null pending and frozen missing inputs are null unavailable. Optional
    later direct results use actual native-final or assigned-slot identity.
    No null aliases are inferred. Context hashes stay in each producer's domain.
    This function does not call the consumer or read/qualify any evidence.
    """
    require(type(maximum_result_bytes) is int and maximum_result_bytes > 0, 'binding_result_limit')
    require(type(evidence) is dict and set(evidence) == {'membership', 'original', 'neutral_reader',
                                                     'neutral_direct', 'qualification'}, 'binding_evidence')
    for key in ('membership', 'original', 'neutral_reader'):
        _descriptor(evidence[key])
    _pin(evidence['qualification'])
    require((neutral_direct is None) == (evidence['neutral_direct'] is None), 'direct_result_descriptor')
    if neutral_direct is not None:
        _descriptor(evidence['neutral_direct'])
    require(original['membership'] == {'actual': evidence['membership']['data'], 'review': evidence['membership']['review']}
            and neutral_reader['membership'] == evidence['membership']['data']
            and neutral_reader['membership_review'] == evidence['membership']['review']
            and original['comparisons'] == neutral_reader['comparisons'] == membership['comparisons'],
            'literal_registry_membership')
    entries, runs, baseline_aliases = _membership(membership)
    protocols = _protocols(membership, original, neutral_reader)
    ledger, original_rows, original_contexts = _original(original, runs, protocols)
    reader_index, reader_rows = _neutral_reader(neutral_reader, entries, protocols)
    direct_rows, canonical, direct_provenance = _direct_inputs(direct_inputs, entries, original['protocols']['self'])
    for entry in entries:
        for _, arm in entry['reader'].values():
            row = direct_rows[arm['direct_key']]
            for identity in reader_index[arm['reader_key']].values():
                physical = neutral_reader['origins'][identity]['physical']
                require(physical['view_sha256'] == row.get('view_canonical_sha256'), 'neutral_view_hash_bridge')
    direct_index, direct_records, rendered_reasons = _direct_results(neutral_direct, direct_rows, original['protocols']['self'])
    records = {}
    for table in (original_rows, reader_rows, direct_records):
        for identity, row in table.items():
            require(identity not in records or records[identity] == row, 'cross_registry_id_conflict')
            records[identity] = row
    selected, comparisons = {}, []

    def cell(run, view, method, name, state, reason, context, source, identity=None, qualified=True):
        if identity is not None:
            row = records[identity]
            require((row['original_run_id'], row['method'], row['threshold']) == (run, method, name)
                    and row['protocol_sha256'] == protocols[method][name], 'bound_record_identity')
            selected[identity] = copy.deepcopy(row)
            state, reason, context = 'present', None, row['context_sha256']
        return dict(id=identity, identity_kind=records[identity]['identity_kind'] if identity else None,
            state=state, reason=reason, context_sha256=context, protocol_sha256=protocols[method][name],
            slot=dict(key=json.dumps([run, view, 'early', method, name], separators=(',', ':')),
                      run_id=run, view=view, stage='early', method=method, threshold=name, source=copy.deepcopy(source)),
            qualification=dict(state='qualified' if qualified else 'unresolved',
                proof=copy.deepcopy(evidence['qualification']) if qualified else None,
                reason=None if qualified else reason), logical_alias=None)

    for entry in entries:
        c = entry['value']; members = []
        for m in c['members']:
            included = m['disposition'] == 'included'
            arms = m['arms'] if included else {arm: baseline_aliases.get((m['task_id'], m['draw_key'],
                m[arm + '_run_id'], entry['views'][arm])) if arm == 'baseline' else None
                for arm in ('baseline', 'treatment')}
            runs_by_arm = {arm: m['arms'][arm]['original_run_id'] if included else m[arm + '_run_id']
                           for arm in ('baseline', 'treatment')}
            instruments = {}
            for instrument in INSTRUMENTS:
                method, name = instrument.split(':'); cells = {}
                for arm in ('baseline', 'treatment'):
                    run = runs_by_arm[arm]; a = ledger.get((run, method, name))
                    state = a['record_state'] if a else 'not_in_resolved_inventory'
                    reason = ('not_assigned_in_frozen_original_allocation' if state == 'not_assigned' else
                              'assigned_original_instance_absent' if a else 'original_run_not_in_accepted_inventory')
                    cells[arm + '_original'] = cell(run, 'original', method, name, state, reason,
                        original_contexts.get((run, method)), evidence['original']['data'],
                        identity=a['measurement_id'] if a else None, qualified=a is not None)
                    frozen = arms[arm]; view = entry['views'][arm]
                    if frozen is None:
                        cells[arm + '_neutral'] = cell(run, view, method, name, 'unsupported', m['reason'],
                            None, evidence['membership']['data'], qualified=False)
                    elif method == 'reader':
                        key = frozen['reader_key']
                        cells[arm + '_neutral'] = cell(run, view, method, name, 'present', None, None,
                            evidence['neutral_reader']['data'], identity=reader_index[key][name])
                    else:
                        key = frozen['direct_key']; row = direct_rows[key]
                        require(row['run_id'] == run and row['view_kind'] == view, 'bound_direct_view')
                        if neutral_direct is not None:
                            cells[arm + '_neutral'] = cell(run, view, method, name, 'present', None, None,
                                evidence['neutral_direct']['data'], identity=direct_index[key, name])
                        else:
                            missing = row['disposition'] == 'unavailable'
                            source = direct_provenance['sources'][canonical[key]['source']]['rows']
                            cells[arm + '_neutral'] = cell(run, view, method, name,
                                'unavailable' if missing else 'pending', row['reason'] if missing else
                                'no_accepted_neutral_direct_registry_bound', None if missing else row['context_canonical_sha256'], source)
                instruments[instrument] = cells
            members.append(dict(task_id=m['task_id'], draw_key=m['draw_key'],
                baseline_run_id=runs_by_arm['baseline'], treatment_run_id=runs_by_arm['treatment'],
                disposition=m['disposition'], reason=None if included else m['reason'], instruments=instruments))
        comparisons.append(dict(factor=c['variant'], baseline=c['baseline'], neutral_view=entry['views']['treatment'],
            neutral_views_by_arm=copy.deepcopy(entry['views']),
            scope=f"Frozen {c['variant']} membership: {c['fixed_pairs']} fixed and {c['included_pairs']} coverage-qualified pairs; unsupported reasons retained.",
            members=members))
    require(set(selected) == set(records), 'unmapped_registry_records')
    primary = [dict(baseline=c['baseline'], variant=c['factor'], readout=method + '_evaluation_presence')
               for c in comparisons for method in ('self', 'reader')]
    result = dict(schema='causes.four_cell_assignment_binding.v1', arguments=dict(comparisons=comparisons,
        measurements=list(selected.values()), primary_family=primary), provenance=dict(evidence=copy.deepcopy(evidence),
        direct_inputs=direct_provenance, fixed_pairs=sum(c['fixed_pairs'] for c in membership['comparisons']),
        included_pairs=sum(c['included_pairs'] for c in membership['comparisons']),
        original_assignment_counts=dict(Counter(a['record_state'] for a in ledger.values())),
        existing_registry_counts=dict(Counter(r['identity_kind'] for r in selected.values())),
        neutral_direct_result_bound=neutral_direct is not None, null_aliases_created=0,
        context_domains={'self': 'native_message_array', 'reader': 'canonical_prepared_object'},
        consumer_invocations=0, evidence_reads=0, model_or_parser_calls=0, inference_released=False))
    if rendered_reasons:
        result['provenance']['direct_reason_renderings'] = rendered_reasons
    require(len(_encoded(result)) <= maximum_result_bytes, 'binding_serialized_bound')
    return result
