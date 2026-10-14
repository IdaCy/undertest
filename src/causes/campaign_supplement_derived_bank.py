"""Resolve an explicitly adopted bank of frozen derived probes without execution.

Input acceptance is separate from runtime authority. Shared baseline jobs have
one physical identity; comparison assignments and unavailable arms remain in
their original input documents. A bank transition never resets owner history.
"""
import copy
import hashlib
import json

from causes import campaign_supplement as core


SCHEMA = 'causes.derived_supplement_bank_adoption.v1'
QUESTIONS = ('evaluation_presence', 'evaluation_strongly_favors',
             'evaluation_leans_towards_or_favors')


def require(value, reason):
    if not value:
        raise core.IntegrityError(reason)


def resolve(plan, previous, *, read=core.pinned):
    """Return original jobs/rows and cumulative bank scope for one exact caller.

The accepted adoption names its initial execution predecessor. Later partial
continuations retain that adoption and must present the exact cumulative bank
and original ordered untouched suffix, along with their current failure state.
No source, measurement, owner, capacity or storage approval is created here.
"""
    refs = {}

    def raw(ref):
        require(type(ref) is dict and set(ref) == {'path', 'bytes', 'sha256'},
                'derived_bank_pin')
        core.canonical(ref['path'])
        require(ref['path'] not in refs or refs[ref['path']] == ref, 'derived_bank_pin_conflict')
        data = read(ref)
        require(type(data) is bytes and type(ref['bytes']) is int and len(data) == ref['bytes'] and
                hashlib.sha256(data).hexdigest() == ref['sha256'], 'derived_bank_pin_bytes')
        refs[ref['path']] = ref
        return data

    def value(ref):
        return json.loads(raw(ref))

    def accepted(descriptor, flag, field='actual'):
        require(type(descriptor) is dict and set(descriptor) == {'actual', 'review'},
                'derived_bank_descriptor')
        actual, peer = value(descriptor['actual']), value(descriptor['review'])
        require(peer.get(flag) is True and peer.get(field) == descriptor['actual'] and
                peer.get('findings') == [], 'derived_bank_independent_peer')
        return actual, peer

    def ids(jobs):
        require(type(jobs) is list and all(type(j) is dict and
                type(j.get('measurement_id')) is str and j['measurement_id'] for j in jobs),
                'derived_bank_job_identity')
        result = [j['measurement_id'] for j in jobs]
        require(len(result) == len(set(result)), 'derived_bank_duplicate_job')
        return result

    require('original_reservation_transfer' not in plan, 'derived_bank_ambiguous_transition')
    bank, _ = accepted(plan['derived_bank'], 'accepted_derived_supplement_bank')
    require(bank['schema'] == SCHEMA and bank['root_authorized'] is True and
            bank['target_role'] == plan['role'] == 'mi' and
            bank['family'] == plan['family'] and bank['model'] == plan['model'] and
            bank['family'].startswith('derived_probe_'), 'derived_bank_scope')
    base, base_peer = accepted(bank['execution_predecessor'], 'accepted_finite_supplement_closed_custody')
    current, current_peer = accepted(plan['predecessor_custody'], 'accepted_finite_supplement_closed_custody')
    require(current == previous, 'derived_bank_current_custody_changed')
    for closed, peer in ((base, base_peer), (current, current_peer)):
        require(closed['schema'] == 'causes.finite_supplement_tranche_closed.v1' and
                closed['role'] == 'mi' and type(closed['active']) is int and closed['active'] == 0 and
                closed['ambiguous'] == [] and closed['clean_native_ending'] is True and
                type(closed['consecutive_failures']) is int and closed['consecutive_failures'] >= 0 and
                peer['original_denominator_preserved'] is True and
                peer['all_original_attempts_retained'] is True and
                peer['qualification_findings'] == closed['qualification_findings'],
                'derived_bank_closed_history')
    require(base['unstarted_ids'] == [] and
            current['complete_ids'] == plan['occupied_ids'] and
            current['consecutive_failures'] == plan['initial_failures'],
            'derived_bank_failure_or_priority_history')
    owner_jobs = value(base['full_jobs']); owner_ids = ids(owner_jobs)
    require(owner_ids == base['complete_ids'], 'derived_bank_owner_partition')
    inputs = bank['inputs']
    require(type(inputs) is list and inputs and
            len({d['actual']['path'] for d in inputs}) == len(inputs), 'derived_bank_inputs')
    jobs, unique_rows, row_by_job, roles = [], {}, {}, {}
    common_protocol = None
    source_counts, comparisons = [], []
    for descriptor in inputs:
        prepared, peer = accepted(descriptor, 'accepted_frozen_derived_probe_inputs', 'preparation')
        require(prepared['schema'] == 'causes.frozen_derived_probe_preparation.v1' and
                prepared['runtime_authorized'] is False, 'derived_bank_input_schema')
        for key in ('assignments', 'pairs', 'protocol', 'rows', 'counts'):
            require(peer[key] == prepared[key], 'derived_bank_input_peer_join')
        assignments, rows, pairs, protocol = [value(prepared[k]) for k in ('assignments', 'rows', 'pairs', 'protocol')]
        require(core.digest(rows) == prepared['rows_canonical_sha256'] and
                type(rows) is list and len(rows) == len(assignments) and rows,
                'derived_bank_row_vector')
        require(protocol['schema'] == 'causes.derived_probe_protocol.v1' and
                protocol['family'] == bank['family'] and protocol['model'] == bank['model'],
                'derived_bank_protocol')
        if common_protocol is None:
            common_protocol = protocol
        require(protocol == common_protocol, 'derived_bank_scientific_protocol_changed')
        local_rows, local_roles, request_count, missing_count = {}, {}, 0, 0
        for assignment, row in zip(assignments, rows):
            require(type(assignment) is dict and set(assignment) == {'run_id', 'view_kind', 'stage'} and
                    row['schema'] == 'causes.derived_probe_input.v1' and
                    all(row[k] == v for k, v in assignment.items()) and
                    row['measurement_key'] not in local_rows and row['runtime_authorized'] is False,
                    'derived_bank_assignment_changed')
            key = row['measurement_key']; local_rows[key] = row
            if row['disposition'] == 'prepared':
                require([j['question_id'] for j in row['jobs']] == list(QUESTIONS),
                        'derived_bank_instrument_roster')
                for job in row['jobs']:
                    require(job['family'] == bank['family'] and job['model'] == bank['model'] and
                            job['run_id'] == key and job['original_run_id'] == row['run_id'] and
                            job['stage'] == row['stage'] and job['protocol_sha256'] == row['protocol_sha256'] and
                            job['measurement_id'] == '/'.join(job[k] for k in
                                ('family', 'model', 'run_id', 'stage', 'question_id')),
                            'derived_bank_native_identity_changed')
                request_count += len(row['jobs'])
            else:
                require(row['disposition'] == 'unavailable' and row['jobs'] == [] and
                        row['view'] is None and type(row['reason']) is str and row['reason'],
                        'derived_bank_missing_assignment_changed')
                missing_count += 1
        require(type(pairs) is list and len(pairs)*2 == len(rows), 'derived_bank_pair_count')
        pair_keys = set()
        for pair in pairs:
            pair_key = (pair['task_id'], pair['draw_key'], pair['variant'])
            require(pair_key not in pair_keys and set(pair['arms']) == {'baseline', 'treatment'},
                    'derived_bank_pair_identity')
            pair_keys.add(pair_key)
            for role, arm in pair['arms'].items():
                key = arm['measurement_key']
                require(key in local_rows and key not in local_roles and
                        arm == {k:local_rows[key][k] for k in ('run_id', 'measurement_key', 'disposition')},
                        'derived_bank_pair_assignment')
                local_roles[key] = role
        require(set(local_rows) == set(local_roles), 'derived_bank_unpaired_assignment')
        counts = dict(assigned=len(rows), ready=len(rows)-missing_count,
                      unavailable=missing_count, requests=request_count, missing_slots=missing_count*3)
        require(counts == prepared['counts'] and prepared['actual_jobs'] == request_count,
                'derived_bank_input_counts')
        added, shared = 0, 0
        for row in rows:
            key = row['measurement_key']
            if key in unique_rows:
                require(local_roles[key] == roles[key] == 'baseline' and row == unique_rows[key],
                        'derived_bank_shared_control_changed')
                shared += len(row['jobs'])
            else:
                unique_rows[key] = row; roles[key] = local_roles[key]
                for job in row['jobs']:
                    require(job['measurement_id'] not in row_by_job, 'derived_bank_job_alias')
                    jobs.append(job); row_by_job[job['measurement_id']] = row
                added += len(row['jobs'])
        source_counts.append(dict(preparation=descriptor['actual'], new_jobs=added, shared_jobs=shared,
                                  counts=counts))
        comparisons.append(dict(preparation=descriptor, assignments=prepared['assignments'],
                                pairs=prepared['pairs'], rows=prepared['rows'], counts=counts))
    bank_ids = ids(jobs)
    missing = sum(row['disposition'] == 'unavailable' for row in unique_rows.values())*3
    require(type(bank['job_count']) is int and len(jobs) == bank['job_count'] and
            core.digest(jobs) == bank['jobs_sha256'] and core.digest(bank_ids) == bank['ids_sha256'] and
            source_counts == bank['source_counts'] and
            bank['unique_assigned_slots'] == len(unique_rows)*3 and
            bank['unique_missing_slots'] == missing and len(jobs)+missing == len(unique_rows)*3 and
            not set(bank_ids).intersection(owner_ids), 'derived_bank_frozen_union_changed')
    cumulative = owner_jobs + jobs
    if plan['predecessor_custody'] == bank['execution_predecessor']:
        require(plan['output_root'] == bank['target_output'] and
                plan['continuation_scope'] == 'adopted_derived_bank', 'derived_bank_initial_output')
        remaining = jobs
    else:
        require(plan['continuation_scope'] == 'derived_bank_remaining' and
                current['derived_bank_extension']['adoption'] == plan['derived_bank'] and
                value(current['full_jobs']) == cumulative,
                'derived_bank_continuation_ancestry')
        n = len(current['complete_ids'])-len(owner_ids)
        require(0 <= n <= len(jobs) and current['complete_ids'] == owner_ids+bank_ids[:n] and
                current['unstarted_ids'] == bank_ids[n:], 'derived_bank_untouched_order')
        remaining = jobs[n:]
    require(type(plan['maximum_new_requests']) is int and
            0 < plan['maximum_new_requests'] <= len(remaining), 'derived_bank_request_budget')
    selected = remaining[:plan['maximum_new_requests']]
    require(core.digest(selected) == plan['jobs_sha256'], 'derived_bank_selected_digest')
    return dict(jobs=copy.deepcopy(selected), bank_jobs=jobs, cumulative_jobs=cumulative,
                prior_owner_jobs=owner_jobs, row_by_job=row_by_job, protocol=common_protocol,
                comparisons=comparisons, unique_missing_slots=missing, source_counts=source_counts,
                required_pins=list(refs.values()))
