import copy
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import time

from causes import campaign_capacity_guard as guard
from causes import campaign_capacity_sample as sample_api
from causes import campaign_capacity_trial as trial
from causes import campaign_concurrency_evidence as evidence
from causes import campaign_handoff as h


require = trial.require


def positive(value, reason):
    require(type(value) in (int, float) and math.isfinite(value) and value > 0, reason)
    return value


def validate_criteria(criteria):
    require(criteria['schema'] == 'causes.finite_prefix_capacity_criteria.v1'
            and criteria['workers'] == 8 and type(criteria['workers']) is int
            and criteria['max_full'] == 0 and type(criteria['max_full']) is int
            and criteria['assigned_count'] == 64 and type(criteria['assigned_count']) is int
            and criteria['maximum_trial_seconds'] == 7200
            and criteria['minimum_actor_overlap_seconds'] == 30
            and criteria['maximum_clock_difference_seconds'] == 1
            and criteria['maximum_new_technical_terminals'] == 0
            and criteria['no_replay_or_replacement'] is True
            and criteria['automatic_adoption'] is False
            and criteria['automatic_resume'] is False, 'finite_prefix_criteria_changed')
    positive(criteria['minimum_completed_per_hour'], 'positive_prefix_throughput_floor_required')


def journal_state(logs, owner, run_ids):
    current, = [log for log in logs if log['group'] == owner['group']]
    require(current['incomplete_tail_bytes'] == 0, 'finite_prefix_journal_torn')
    claims, terminals = {}, {}
    for event in current['events']:
        if event.get('controller_id') != owner['controller_id']:
            require(event['event'] != 'claimed' and event['event'] not in sample_api.TERMINAL_EVENTS,
                    'finite_prefix_other_owner_in_fresh_journal')
            continue
        kind, rid = event['event'], event.get('run_id')
        if kind == 'claimed':
            require(rid in run_ids and rid not in claims and event.get('full') is False,
                    'finite_prefix_claim_changed_or_repeated')
            claims[rid] = event
        elif kind in sample_api.TERMINAL_EVENTS:
            require(rid in claims and rid not in terminals
                    and sample_api.epoch(event['at']) >= sample_api.epoch(claims[rid]['at']),
                    'finite_prefix_terminal_without_unique_claim')
            terminals[rid] = event
    technical = [rid for rid, event in terminals.items()
                 if event['event'] != 'episode_finished'
                 or event.get('technical_failure') is not False
                 or event.get('controller_error_type') or event.get('route_faults')]
    return {'claims': claims, 'terminals': terminals,
            'active': [rid for rid in claims if rid not in terminals],
            'unclaimed': [rid for rid in run_ids if rid not in claims],
            'technical': technical}


def checked_assignment(pair):
    assignment, review = h.checked(pair['evidence']), h.checked(pair['review'])
    require(review.get('accepted_finite_prefix_capacity_assignment') is True
            and review['assignment'] == pair['evidence'], 'finite_prefix_assignment_unaccepted')
    require(assignment['schema'] == 'causes.finite_prefix_capacity_assignment.v1'
            and assignment['selection'] == 'first_never_claimed_in_original_order'
            and assignment['no_replay_or_replacement'] is True,
            'finite_prefix_assignment_scope_changed')
    original, jobs = h.checked(assignment['original_jobs']), h.checked(assignment['jobs'])
    ids = [job['allocation']['run_id'] for job in original]
    require(len(ids) == len(set(ids)) and original, 'finite_prefix_original_ids_invalid')
    claimed = set()
    require(assignment['predecessors'], 'finite_prefix_closed_history_required')
    for pair in assignment['predecessors']:
        closed, peer = h.checked(pair['evidence']), h.checked(pair['review'])
        require(peer.get('accepted_finite_prefix_predecessor_closure') is True
                and peer['actual'] == pair['evidence'] and peer['journal'] == closed['journal']
                and closed['remote_owner_retired'] is True
                and closed['local_transport_retired'] is True
                and closed['all_claims_accounted'] is True
                and closed['route_history_preserved'] is True,
                'finite_prefix_predecessor_not_closed')
        raw = h.checked_bytes(closed['journal'])
        require(not raw or raw.endswith(b'\n'), 'finite_prefix_prior_journal_torn')
        rows = [json.loads(line) for line in raw.splitlines()]
        state = journal_state([{'group': closed['owner']['group'], 'incomplete_tail_bytes': 0,
                                'events': rows}], closed['owner'], ids)
        require(not state['active'] and not claimed.intersection(state['claims']),
                'finite_prefix_prior_active_or_replayed')
        claimed.update(state['claims'])
    expected = [job for job in original if job['allocation']['run_id'] not in claimed][:64]
    require(len(expected) == 64 and jobs == expected
            and assignment['ordered_run_ids'] == [job['allocation']['run_id'] for job in jobs]
            and assignment['prior_claimed_run_ids'] == [rid for rid in ids if rid in claimed]
            and all(job['allocation'].get('continue_to_end') is False for job in jobs),
            'finite_prefix_selection_or_job_bytes_changed')
    instruments = assignment['instruments']
    require(type(instruments) is list and len(instruments) == len(set(instruments)) == 3
            and all(job['allocation']['instruments'] == instruments for job in jobs),
            'finite_prefix_instrument_roster_changed')
    return assignment, jobs


def validate_trial(plan, policy, criteria, reference):
    validate_criteria(criteria)
    require(h.pin(Path(evidence.__file__).resolve()) == plan['timing_source']
            and plan['timing_source'] in plan['capacity_guard']['required_sources'],
            'finite_prefix_timing_source_changed')
    assignment, jobs = checked_assignment(plan['assignment'])
    require(policy['launch']['jobs'] == assignment['jobs']
            and h.checked(policy['launch']['jobs']) == jobs
            and policy['workers'] == 8 and policy['max_full'] == 0
            and policy['launch']['options']['workers'] == 8
            and policy['launch']['options']['max_full'] == 0
            and not any(policy['launch']['options'].get(key) for key in
                        ('extension_prototypes', 'corpus_continuation', 'prefix_extension'))
            and policy.get('prefix_extension') is None,
            'finite_prefix_policy_or_extensions_changed')
    require(plan['sampling']['groups'] == [plan['sampling']['owner']['group']],
            'finite_prefix_sampling_group_changed')
    providers = [process for process in plan['sampling']['processes']
                 if process.get('role') == 'provider']
    require(reference['profile'] == 'finite_prefix_reference'
            and reference['workers'] == 4
            and type(reference['max_full']) is int and 0 <= reference['max_full'] <= reference['workers']
            and type(reference['observed_full_claims']) is int and reference['observed_full_claims'] == 0
            and reference['config'] == policy['launch']['config']
            and reference['provider'] == plan['provider']
            and plan['provider'].get('role', 'provider') == 'provider'
            and providers == [dict(plan['provider'], role='provider')]
            and criteria['minimum_completed_per_hour'] <=
                positive(reference['completed_per_hour'], 'prefix_reference_rate_invalid'),
            'finite_prefix_reference_changed')


def evaluate(boundary, samples, state, criteria, reference, assignment, declaration):
    validate_criteria(criteria)
    run_ids = assignment['ordered_run_ids']
    reasons = []
    require(len(run_ids) == 64 and len(set(run_ids)) == 64, 'finite_prefix_count_changed')
    require(set(state['claims']) == set(state['terminals']) == set(run_ids)
            and not state['active'] and not state['unclaimed'], 'finite_prefix_incomplete')
    projection = boundary['requests']
    require(not projection['metadata_errors'], 'finite_prefix_boundary_metadata_errors')
    episodes = {row['run_id']: row for row in projection['episodes']}
    require(set(episodes) == set(run_ids) and len(projection['episodes']) == 64,
            'finite_prefix_boundary_membership_changed')
    monitor = trial.TelemetryGuard(declaration['metric_series'],
        [p['role'] for p in declaration['processes']], declaration['owner'])
    for row in samples:
        logs = [dict(log, events=[event for event in log['events']
                                 if sample_api.epoch(event['at']) <= sample_api.epoch(row['finished_at'])])
                for log in boundary['journals']]
        check = monitor.evaluate(row, logs)
        if check['hold']:
            reasons.extend(check['reasons'])
    start = min(sample_api.epoch(row['at']) for row in state['claims'].values())
    finish = max(sample_api.epoch(row['at']) for row in state['terminals'].values())
    elapsed = positive(finish - start, 'finite_prefix_elapsed_invalid')
    throughput = 64 * 3600 / elapsed
    if state['technical']:
        reasons.append('finite_prefix_technical_terminal')
    if elapsed > criteria['maximum_trial_seconds']:
        reasons.append('finite_prefix_deadline')
    if throughput < criteria['minimum_completed_per_hour']:
        reasons.append('finite_prefix_throughput_below_floor')
    actor = {rid: [] for rid in run_ids}
    probes = {rid: set() for rid in run_ids}
    request_keys = set()
    for row in projection['requests']:
        rid, relative = row['run_id'], row['relative_request_directory']
        require(rid in actor and (rid, relative) not in request_keys,
                'finite_prefix_unexpected_or_duplicate_request')
        request_keys.add((rid, relative))
        receipt = row['receipt']
        if (not receipt or receipt.get('status') != 'complete' or receipt.get('retry') is not False
                or receipt.get('error_class')):
            reasons.append('finite_prefix_request_incomplete_or_failed')
            continue
        begin, end = sample_api.epoch(receipt['started_at']), sample_api.epoch(receipt['finished_at'])
        require(sample_api.epoch(state['claims'][rid]['at']) - 1 <= begin <= end
                <= sample_api.epoch(state['terminals'][rid]['at']) + 1,
                'finite_prefix_request_outside_claim')
        if relative.startswith('actor/'):
            actor[rid].append({'turn': int(relative.split('/')[1]),
                **{key: receipt[key] for key in ('started_at', 'finished_at', 'elapsed_seconds')}})
        else:
            require(relative.startswith('probes/early/') and len(relative.split('/')) == 3,
                    'finite_prefix_unexpected_request_kind')
            probes[rid].add(relative.split('/')[2])
    require(all(value == set(assignment['instruments']) for value in probes.values()),
            'finite_prefix_self_receipt_roster_incomplete')
    overlap = None
    if all(actor.values()) and not reasons:
        overlap = evidence.actor_request_exposure(
            [{'run_id': rid, 'requests': sorted(actor[rid], key=lambda row: row['turn'])} for rid in run_ids],
            maximum_clock_difference_seconds=criteria['maximum_clock_difference_seconds'],
            minimum_concurrent_runs=8)
        if not overlap['timing_consistent']:
            reasons.append('finite_prefix_receipt_clock_inconsistent')
        if overlap['cross_job_actor_overlap_seconds'] < criteria['minimum_actor_overlap_seconds']:
            reasons.append('finite_prefix_eight_way_actor_exposure_insufficient')
    elif not all(actor.values()):
        reasons.append('finite_prefix_actor_receipts_missing')
    require(samples and samples[-1]['current_owner']['active_prefix'] == 0,
            'finite_prefix_final_sample_missing')
    return {'supported': True, 'decision': 'hold' if reasons else 'eligible_candidate',
            'profile': 'finite_prefix', 'reasons': sorted(set(reasons)),
            'assigned': 64, 'terminal': 64, 'technical': len(state['technical']),
            'elapsed_seconds': elapsed, 'completed_per_hour': throughput,
            'reference_completed_per_hour': reference['completed_per_hour'],
            'actor_request_exposure': overlap, 'sample_count': len(samples),
            'automatic_adoption': False, 'automatic_resume': False,
            'scope': 'These assigned jobs only; task order differs from the reference. No speedup or tail guarantee.'}


def run_trial(plan_ref, plan, criteria, reference):
    declaration, binding = plan['sampling'], plan['capacity_guard']
    assignment, _ = checked_assignment(plan['assignment'])
    run_ids = assignment['ordered_run_ids']
    out = h.new_record_directory(plan['output'])
    h.save_new(out, 'RECEIPT.json', {'at': h.now(), 'plan': plan_ref, 'process': guard.self_process(),
        'assigned_run_ids': run_ids, 'new_model_calls': 0, 'automatic_adoption': False,
        'automatic_resume': False, 'all_attempts_retained': True})
    writer = guard.LeaseWriter(binding, plan_ref, plan['owner_process'])
    supervisor = trial.CandidateSupervisor(writer, trial.TelemetryGuard(plan['metric_series'],
        [p['role'] for p in declaration['processes']], declaration['owner']), binding, out)
    executor = ThreadPoolExecutor(max_workers=1)
    samples, pending, first_admission = [], None, None
    started, index = time.monotonic(), 0
    next_sample = started
    state = None
    try:
        while not supervisor.held:
            now = time.monotonic()
            if pending is not None and pending.done():
                boundary = pending.result()
                decision = evaluate(boundary, samples, state, criteria, reference, assignment,
                                    dict(declaration, metric_series=plan['metric_series']))
                if time.monotonic() >= supervisor.pending['deadline']:
                    decision['decision'] = 'hold'
                    decision['reasons'].append('candidate_endpoint_deadline')
                h.save_new(out, 'QUALIFICATION.json', {'evaluation': decision,
                    'assignment': plan['assignment'], 'automatic_adoption': False,
                    'remote_and_local_retirement_required': True})
                supervisor.hold(['finite_prefix_trial_complete', *decision['reasons']])
                break
            if pending is not None and now >= supervisor.pending['deadline']:
                supervisor.hold(['candidate_endpoint_deadline'])
                break
            if now < next_sample:
                time.sleep(min(next_sample - now, .25))
                continue
            h.checked_bytes(plan_ref)
            row, logs = sample_api.sample(declaration, index, next_sample,
                                          observe_guard=trial.own_storage_observation)
            sample_ref = h.save_new(out, f'sample_{index:05d}.json', row)
            decision = supervisor.sample_completed(row, logs, sample_ref)
            h.save_new(out, f'telemetry_{index:05d}.json', decision)
            samples.append(row)
            if supervisor.held:
                break
            state = journal_state(logs, declaration['owner'], run_ids)
            if state['technical']:
                supervisor.hold(['finite_prefix_technical_terminal'])
                break
            if state['claims'] and first_admission is None:
                first_admission = min(sample_api.epoch(event['at']) for event in state['claims'].values())
                admission_mono = row['monotonic_seconds'] - (sample_api.epoch(row['at']) - first_admission)
                require(started <= admission_mono <= started + plan['release_wait_seconds'],
                        'candidate_first_admission_outside_declared_wait')
                h.save_new(out, 'FIRST_ADMISSION.json', {'at': h.now(), 'observed_in': sample_ref,
                    'first_claim_wall_epoch': first_admission})
            if first_admission is None:
                if row['monotonic_seconds'] - started >= plan['release_wait_seconds']:
                    supervisor.hold(['candidate_first_admission_not_observed'])
            elif sample_api.epoch(row['at']) - first_admission >= criteria['maximum_trial_seconds']:
                supervisor.hold(['finite_prefix_deadline'])
            elif len(state['terminals']) == 64 and pending is None:
                supervisor.begin_endpoint('finite_prefix_final')
                pending = executor.submit(trial.collect_endpoint, out / 'boundary_final',
                    copy.deepcopy(logs), first_admission, declaration, copy.deepcopy(samples))
            index += 1
            next_sample = started + index * 30
        h.save_new(out, 'STOPPED.json', {'at': h.now(), 'admission_held': supervisor.held,
            'counts': None if state is None else {key: len(state[key]) for key in
                ('claims', 'terminals', 'active', 'unclaimed', 'technical')},
            'pending_endpoint': None if pending is None else 'finite_prefix_final',
            'automatic_retry': False, 'automatic_resume': False,
            'remote_and_local_retirement_required': True})
    except BaseException as error:
        try:
            supervisor.hold(['finite_prefix_supervisor_exception', type(error).__name__])
        finally:
            h.save_new(out, 'FAILED.json', {'at': h.now(), 'error_class': type(error).__name__,
                'error': str(error), 'partial_outputs_preserved': True, 'automatic_retry': False})
        raise
    finally:
        writer.close()
        executor.shutdown(wait=False, cancel_futures=True)
