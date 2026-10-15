"""Resolve an unchanged, never-attempted suffix from accepted closed custody.

This is a saved-byte validator, not an executor or a drain operation. The caller
still owns fresh source/ownership/capacity checks, exclusive locks, fresh output
creation, and one-use execution authority. Nothing here resets an attempt budget,
reclassifies a failed measurement, or authorizes reuse of an output directory.
"""
import copy
import hashlib
import json
from pathlib import PurePosixPath
import re


POLICY = 'causes.original_owned_suffix_policy.v1'
TRANSITION = 'causes.original_owned_suffix_transition.v1'
SCOPE = 'full_untouched_fixed_suffix'
OPERATIONAL_SOURCES = frozenset(('driver', 'budget', 'owned_suffix', 'inventory_provider'))
PRESERVED_FIELDS = (
    'role', 'mode', 'family', 'model', 'endpoint', 'constructor', 'parent_policy',
    'provider_identity', 'completed_actor_custody', 'retirement_pause_pins',
    'retention_root', 'retention', 'storage_allocation', 'storage_floor_override',
    'local_free_space_floor_bytes', 'max_clients', 'exclusive_locks',
    'remote_pause_paths', 'predecessor_pause',
)
OPERATOR_REASONS = frozenset((
    "AdmissionHold('operator_pause')", 'concurrent_fault_or_operator_pause',
    "IntegrityError('pause_or_revocation')",
))
CAPACITY_REASONS = frozenset((
    "AdmissionHold('local_disk_below80GiB')", "IntegrityError('local_free_space_floor')",
    "AdmissionHold('retained_evidence_budget_reserved')",
))


class SuffixError(ValueError):
    """Closed custody does not prove the exact resumable untouched suffix."""


def require(ok, reason):
    if not ok:
        raise SuffixError(reason)


def integer(value, reason, minimum=0):
    require(type(value) is int and value >= minimum, reason)
    return value


def digest(value):
    # The native queue also uses this encoding for bank-extension ID hashes.
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def resolve_original_owned_suffix(plan, *, read, max_links=64,
                                  maximum_distinct_bytes=512 * 1024**2):
    """Return exact remaining jobs and consumed counters; read only pinned bytes.

    An independently accepted source transition binds a private origin policy.
    The origin fixes the first transfer and its budgets; every closed descendant
    must retain its bank, histories, source lineage and original job ordering.
    Final failures remain occupied. Ambiguous/nonterminal claims are refused,
    never returned to the untouched list. Native queue and actor sources cannot
    change; reviewed operational driver/budget/validator sources may change.
    """
    require(callable(read), 'suffix_reader')
    require(type(max_links) is int and 1 <= max_links <= 64, 'suffix_depth_bound')
    require(type(maximum_distinct_bytes) is int and
            0 < maximum_distinct_bytes <= 512 * 1024**2, 'suffix_read_bound')
    refs, documents, raw_cache = {}, {}, {}
    total = 0

    def raw(ref):
        nonlocal total
        require(type(ref) is dict and set(ref) == {'path', 'bytes', 'sha256'}, 'suffix_pin')
        path, size, sha = ref['path'], ref['bytes'], ref['sha256']
        require(type(path) is str and path.startswith('/') and '\x00' not in path and
                str(PurePosixPath(path)) == path and '..' not in PurePosixPath(path).parts and
                type(size) is int and 0 <= size <= 128 * 1024**2 and
                type(sha) is str and re.fullmatch('[0-9a-f]{64}', sha) is not None,
                'suffix_pin_shape')
        require(path not in refs or refs[path] == ref, 'suffix_conflicting_pin')
        if path not in refs:
            require(len(refs) < 4096 and total + size <= maximum_distinct_bytes,
                    'suffix_distinct_read_bound')
            data = read(ref)
            require(type(data) is bytes and len(data) == size and
                    hashlib.sha256(data).hexdigest() == sha, 'suffix_pin_bytes')
            refs[path] = dict(ref); raw_cache[path] = data; total += size
        return raw_cache[path]

    def object_pairs(pairs):
        obj = {}
        for key, value in pairs:
            require(key not in obj, 'suffix_duplicate_json_key')
            obj[key] = value
        return obj

    def value(ref):
        data = raw(ref)
        if ref['path'] not in documents:
            def bad_constant(_):
                raise SuffixError('suffix_nonfinite_json')
            documents[ref['path']] = json.loads(data, object_pairs_hook=object_pairs,
                                               parse_constant=bad_constant)
        return documents[ref['path']]

    def closed(descriptor):
        require(type(descriptor) is dict and set(descriptor) == {'actual', 'review'},
                'suffix_closed_descriptor')
        actual, peer = value(descriptor['actual']), value(descriptor['review'])
        require(peer.get('accepted_finite_supplement_closed_custody') is True and
                peer.get('accepted_completed_actor_saved_closure') is True and
                peer['actual'] == descriptor['actual'] and peer['source'] == actual['source'] and
                peer['findings'] == [] and peer['all_original_attempts_retained'] is True and
                peer['original_denominator_preserved'] is True and
                peer['qualification_findings'] == actual['qualification_findings'],
                'suffix_closed_peer')
        raw(actual['source'])
        require(type(actual['terminal']) is int and actual['terminal'] >= 0 and
                type(actual['occupied']) is int and actual['occupied'] == actual['terminal'] and
                type(actual['complete_ids']) is list and type(actual['unstarted_ids']) is list and
                len(actual['complete_ids']) == len(set(actual['complete_ids'])) == actual['terminal'] and
                len(actual['unstarted_ids']) == len(set(actual['unstarted_ids'])) and
                not set(actual['complete_ids']) & set(actual['unstarted_ids']), 'suffix_closed_partition')
        require(actual['clean_native_ending'] is True and type(actual['active']) is int and
                actual['active'] == 0 and actual['ambiguous'] == [] and
                actual['unresolved_claimed_ids'] == [] and actual['quarantined'] is False and
                actual['all_original_attempts_retained'] is True and
                actual['original_denominator_preserved'] is True and
                type(actual['consecutive_failures']) is int and
                0 <= actual['consecutive_failures'] < 3,
                'suffix_nonterminal_or_quarantined')
        return actual

    def source_binding(current, transition, policy_ref):
        peer, adoption = value(transition['source_review']), value(transition['root_source_adoption'])
        selected = current['sources']
        require(peer.get('accepted_original_owned_suffix_runtime_source') is True and
                peer['sources'] == selected and peer['suffix_policy'] == policy_ref and
                peer['findings'] == [] and
                adoption.get('accepted_original_owned_suffix_runtime_source') is True and
                adoption['runtime_authorized'] is False and
                adoption['sources'] == selected and adoption['suffix_policy'] == policy_ref and
                adoption['independent_review'] == transition['source_review'] and
                adoption['findings'] == [], 'suffix_source_acceptance')
        for ref in selected.values():
            raw(ref)

    def unchanged(current, first):
        require('derived_bank' not in current and
                current.get('retain_admission_after_healthy_harvest') is True,
                'suffix_original_healthy_scope')
        for key in PRESERVED_FIELDS:
            require(current[key] == first[key], 'suffix_preserved_' + key)
        base = {k: v for k, v in first['sources'].items() if k not in OPERATIONAL_SOURCES}
        proposed = {k: v for k, v in current['sources'].items() if k not in OPERATIONAL_SOURCES}
        require(proposed == base and current['sources']['native_queue'] == first['sources']['native_queue']
                and current['sources']['completed_actor'] == first['sources']['completed_actor'],
                'suffix_native_or_dependency_sources_changed')

    try:
        require(type(plan) is dict and plan['continuation_scope'] == SCOPE and
                'original_reservation_transfer' not in plan and 'derived_bank' not in plan,
                'suffix_no_readoption')
        transition = value(plan['original_suffix_transition'])
        policy_ref = transition['policy']; policy = value(policy_ref)
        require(policy['schema'] == POLICY and policy['runtime_authorized'] is False,
                'suffix_policy')
        first_ref = policy['first_transfer_plan']; first = value(first_ref)
        prior_count = integer(policy['prior_count'], 'suffix_prior_count')
        added = integer(policy['appended_count'], 'suffix_appended_count', 1)
        total_count = integer(policy['full_count'], 'suffix_full_count', 1)
        attempt_cap = integer(policy['maximum_admission_attempts'], 'suffix_attempt_cap', 1)
        capture_cap = integer(policy['maximum_captures'], 'suffix_capture_cap', 1)
        require(total_count == prior_count + added and
                first['continuation_scope'] == 'transferred_original_reservation' and
                first['maximum_new_requests'] == added and
                type(first['maximum_new_requests']) is int and
                first['maximum_admission_attempts'] == attempt_cap == 4 * added and
                first['maximum_captures'] == capture_cap == added and
                type(first['maximum_admission_attempts']) is int and
                type(first['maximum_captures']) is int and
                type(first['maximum_capture_uses']) is int and first['maximum_capture_uses'] == 8 and
                len(first['occupied_ids']) == prior_count and first['role'] == 'mi',
                'suffix_origin_scope')
        previous = closed(plan['predecessor_custody'])
        full_ref = previous['full_jobs']; full = value(full_ref)
        require(type(full) is list and len(full) == total_count, 'suffix_full_bank')
        ids, physical = [], set()
        for job in full:
            mid = job['measurement_id']
            require(type(mid) is str and mid == '/'.join(job[k] for k in
                    ('family', 'model', 'run_id', 'stage', 'question_id')) and
                    job['model'] == first['model'] and job['stage'] == 'early' and
                    job['question_id'] in ('evaluation_strongly_favors',
                                          'evaluation_leans_towards_or_favors'),
                    'suffix_job_identity')
            key = (job['run_id'], job['stage'], job['question_id'])
            require(key not in physical, 'suffix_physical_alias')
            physical.add(key); ids.append(mid)
        require(len(set(ids)) == total_count and ids[:prior_count] == first['occupied_ids'] and
                digest(full[prior_count:]) == first['jobs_sha256'], 'suffix_origin_jobs')

        chain, seen, outputs = [], set(), set()
        descriptor, child = plan['predecessor_custody'], plan
        while True:
            require(len(chain) < max_links and descriptor['actual']['path'] not in seen,
                    'suffix_cycle_or_depth')
            seen.add(descriptor['actual']['path'])
            actual = closed(descriptor); step = value(actual['plan'])
            prior_descriptor = dict(actual=actual['prior_closed'], review=actual['prior_closed_review'])
            prior = closed(prior_descriptor)
            unchanged(step, first); unchanged(child, first)
            t = value(child['original_suffix_transition'])
            require(t['schema'] == TRANSITION and t['policy'] == policy_ref and
                    t['predecessor'] == child['predecessor_custody'] == descriptor and
                    t['predecessor_stop_reason'] == actual['stop_reason'] and
                    t['current_predecessor_pause'] == child['current_predecessor_pause'],
                    'suffix_transition')
            source_binding(child, t, policy_ref)
            reason = actual['stop_reason']
            pause = child['current_predecessor_pause']; pause_path = str(PurePosixPath(step['output_root']) / 'PAUSE')
            allowed_capacity = set(CAPACITY_REASONS) | {
                "AdmissionHold('local_disk_below_floor:" + str(first['local_free_space_floor_bytes']) + "')"}
            technical = t.get('technical_ending_review')
            if reason == "IntegrityError('remote_transport_or_source_refusal')":
                require(technical is not None, 'suffix_technical_ending_review_required')
                recovery = value(technical)
                native = value(actual['native_result'])
                execution = value(recovery['execution']); transport = execution['transport']
                transport_closed = value(recovery['transport_closed'])
                terminal = value(recovery['terminal_peer'])
                raw(recovery['durable_complete'])
                require(recovery.get('accepted_exact_technical_ending') is True and
                        recovery['driver_result'] == actual['native_result'] and
                        recovery['durable_complete'] == actual['durable_complete'] and
                        recovery['live_sources_modified'] is False and
                        recovery['no_new_science_or_provider_calls'] is True and
                        recovery['not_authority_to_reinvoke_old_binder'] is True and
                        execution['source_error'] is None and execution['model_calls'] == 0 and
                        type(execution['model_calls']) is int and execution['automatic_retry'] is False and
                        transport['reason'] == 'timeout' and transport['complete'] is False and
                        transport['child_reaped'] is True and transport['cleanup_errors'] == [] and
                        type(transport['returncode']) is int and transport['returncode'] != 0 and
                        transport_closed['reason'] == 'remote_failure' and
                        transport_closed['cause'] == reason and transport_closed['cleanup_error'] is None and
                        type(transport_closed['supervisor_returncode']) is int and
                        transport_closed['supervisor_returncode'] == 0 and
                        transport_closed['reconnect'] is False and transport_closed['automatic_retry'] is False and
                        native['control_transport'] == recovery['transport_closed'] and
                        recovery['transport_closed']['path'] ==
                            str(PurePosixPath(step['output_root']) / 'control_transport/CLOSED.json') and
                        recovery['execution']['path'] ==
                            str(PurePosixPath(step['output_root']) / 'remote_records' /
                                ('call_%06d' % integer(native['remote_calls'], 'suffix_remote_calls', 1)) /
                                '000004_execution') and
                        terminal['terminal_refs']['DRIVER_RESULT.json'] == actual['native_result'] and
                        terminal['journal'] == actual['original_journal'],
                        'suffix_technical_ending_review')
                source_adoption = value(t['root_source_adoption'])
                require(source_adoption.get('technical_ending_review') == technical,
                        'suffix_technical_ending_adoption')
            else:
                require(technical is None and (reason in OPERATOR_REASONS or reason in allowed_capacity),
                        'suffix_stop_reason')
            if pause is None:
                require(reason in allowed_capacity or technical is not None, 'suffix_operator_pause_unbound')
            else:
                require(pause['path'] == pause_path and pause in child['execution_pins'],
                        'suffix_pause_binding')
                raw(pause)
            require(pause_path in step['pause_paths'] and child['pause_paths'] ==
                    [p for p in step['pause_paths'] if p != pause_path] +
                    [str(PurePosixPath(child['output_root']) / 'PAUSE')], 'suffix_pause_paths')
            require(actual['role'] == first['role'] and actual['mode'] == first['mode'] and
                    actual['full_jobs'] == full_ref and actual['native_invocations'] and
                    actual['native_invocations'][-1]['plan'] == actual['plan'] and
                    actual['native_invocations'][:-1] == prior['native_invocations'] and
                    step['output_root'] == actual['output'] and
                    step['predecessor_custody'] == prior_descriptor and
                    step['occupied_ids'] == prior['complete_ids'] and
                    type(step['initial_failures']) is int and
                    step['initial_failures'] == prior['consecutive_failures'] and
                    actual['inherited_qualification_findings'] == prior['qualification_findings'] and
                    actual['qualification_findings'] == prior['qualification_findings'] +
                    actual['current_qualification_findings'], 'suffix_history_changed')
            occupied = len(step['occupied_ids']); terminal = integer(actual['terminal'], 'suffix_terminal')
            require(type(actual['occupied']) is int and actual['occupied'] == terminal and
                    prior_count <= occupied <= terminal < total_count and
                    step['occupied_ids'] == ids[:occupied] and actual['complete_ids'] == ids[:terminal] and
                    actual['unstarted_ids'] == ids[terminal:] and
                    type(step['maximum_new_requests']) is int and
                    step['maximum_new_requests'] == total_count - occupied and
                    step['jobs_sha256'] == digest(full[occupied:]), 'suffix_partition')
            require(type(actual['new_invalid_ids']) is list and
                    len(set(actual['new_invalid_ids'])) == len(actual['new_invalid_ids']) and
                    set(actual['new_invalid_ids']) <= set(ids[occupied:terminal]) and
                    type(actual['current_tranche_qualified']) is bool and
                    (not actual['new_invalid_ids'] or actual['current_tranche_qualified'] is False),
                    'suffix_invalid_partition')
            require(step['output_root'] not in outputs and child['output_root'] != step['output_root'],
                    'suffix_output_reuse')
            outputs.add(step['output_root']); chain.append((actual, step))
            if actual['plan'] == first_ref:
                extension = actual['original_bank_extension']
                require(step == first and prior['unstarted_ids'] == [] and
                        prior['complete_ids'] == ids[:prior_count] and prior['terminal'] == prior_count and
                        value(prior['full_jobs']) == full[:prior_count] and
                        extension['schema'] == 'causes.original_supplement_owner_bank_extension.v1' and
                        extension['previous_closed'] == first['predecessor_custody'] and
                        extension['previous_full_jobs'] == prior['full_jobs'] and
                        extension['transfer'] == first['original_reservation_transfer'] and
                        extension['full_jobs'] == full_ref and
                        extension['prior_count'] == prior_count and extension['appended_count'] == added and
                        extension['cumulative_count'] == total_count and
                        all(type(extension[k]) is int for k in ('prior_count', 'appended_count', 'cumulative_count')) and
                        extension['appended_jobs_sha256'] == digest(full[prior_count:]) and
                        extension['appended_ids_sha256'] == digest(ids[prior_count:]) and
                        extension['cumulative_jobs_sha256'] == digest(full) and
                        extension['prior_prefix_unchanged'] is True and
                        extension['native_families_preserved'] is True, 'suffix_origin_extension')
                transfer_descriptor = first['original_reservation_transfer']
                transfer, transfer_peer = value(transfer_descriptor['actual']), value(transfer_descriptor['review'])
                require(transfer_peer.get('accepted_original_supplement_reservation_transfer') is True and
                        transfer_peer['actual'] == transfer_descriptor['actual'] and transfer_peer['findings'] == [] and
                        transfer['execution_predecessor'] == first['predecessor_custody'] and
                        transfer['target_output'] == first['output_root'], 'suffix_original_transfer_custody')
                break
            require(step['continuation_scope'] == SCOPE and 'original_reservation_transfer' not in step and
                    actual.get('original_bank_extension') is None, 'suffix_repeated_transfer')
            descriptor, child = prior_descriptor, step

        attempts_used = captures_used = 0
        for actual, step in reversed(chain):
            count = step['maximum_new_requests']
            attempts = min(4 * count, attempt_cap - attempts_used)
            captures = min(count, capture_cap - captures_used)
            require(all(type(step[k]) is int for k in
                        ('maximum_admission_attempts', 'maximum_captures', 'maximum_capture_uses')) and
                    step['maximum_admission_attempts'] == attempts > 0 and
                    step['maximum_captures'] == captures > 0 and step['maximum_capture_uses'] == 8,
                    'suffix_prior_budget')
            native = value(actual['native_result']); result = native['result']
            used = integer(native['attempts'], 'suffix_consumed_attempts')
            captured = integer(native['captures'], 'suffix_consumed_captures')
            require(used <= attempts and captured <= min(used, captures) and
                    type(result['active']) is int and result['active'] == 0 and
                    type(result['consecutive_failures']) is int and
                    result['consecutive_failures'] == actual['consecutive_failures'] and
                    result['stop_reason'] == actual['stop_reason'] and
                    type(result['remaining_unclaimed']) is int and
                    result['remaining_unclaimed'] == len(actual['unstarted_ids']) and
                    type(result['new_final']) is int and
                    result['new_final'] == actual['terminal'] - len(step['occupied_ids']),
                    'suffix_consumption_or_result')
            attempts_used += used; captures_used += captured
        require(plan['output_root'] not in outputs and plan['occupied_ids'] == previous['complete_ids'] and
                type(plan['initial_failures']) is int and
                plan['initial_failures'] == previous['consecutive_failures'], 'suffix_current_predecessor')
        jobs = full[len(previous['complete_ids']):]
        attempts_left = min(4 * len(jobs), attempt_cap - attempts_used)
        captures_left = min(len(jobs), capture_cap - captures_used)
        require(jobs and [j['measurement_id'] for j in jobs] == previous['unstarted_ids'] and
                all(type(plan[k]) is int for k in
                    ('maximum_new_requests', 'maximum_admission_attempts', 'maximum_captures', 'maximum_capture_uses')) and
                plan['maximum_new_requests'] == len(jobs) and plan['jobs_sha256'] == digest(jobs) and
                plan['maximum_admission_attempts'] == attempts_left > 0 and
                plan['maximum_captures'] == captures_left > 0 and plan['maximum_capture_uses'] == 8,
                'suffix_remaining_budget_and_jobs')
        require(all(ref in plan['execution_pins'] for ref in refs.values()), 'suffix_incomplete_roster')
        return dict(jobs=copy.deepcopy(jobs), full_jobs=dict(full_ref),
                    prior_count=prior_count, full_count=total_count,
                    complete_ids=list(previous['complete_ids']), unstarted_ids=list(previous['unstarted_ids']),
                    consecutive_failures=previous['consecutive_failures'],
                    qualification_findings=copy.deepcopy(previous['qualification_findings']),
                    used_attempts=attempts_used, used_captures=captures_used,
                    remaining_attempts=attempts_left, remaining_captures=captures_left,
                    required_pins=[refs[path] for path in sorted(refs)],
                    distinct_read_bytes=total, runtime_authorized=False)
    except (KeyError, TypeError, IndexError, UnicodeError, json.JSONDecodeError) as error:
        raise SuffixError('suffix_shape') from error
