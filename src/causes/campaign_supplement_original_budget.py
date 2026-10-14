"""Carry an original transfer's operational budgets across accepted clean holds.

This saved-only check grants no execution authority. The caller authenticates
the selected origin and proposed plan, owns the pinned reader and source policy,
and still supplies all live ownership, capacity and provider admission checks.
"""
import json
import math
from pathlib import PurePosixPath
import re

from causes import campaign_supplement as core
from causes import campaign_supplement_closed_scope as scope
from causes import campaign_supplement_completed_actor as actor
from causes import campaign_supplement_source_lineage as lineage


HOLD_REASONS = frozenset((
    "AdmissionHold('local_disk_below80GiB')",
    "IntegrityError('local_free_space_floor')",
    "AdmissionHold('retained_evidence_budget_reserved')"))
PRESERVED = ('family', 'model', 'mode', 'role', 'endpoint', 'constructor',
             'parent_policy', 'provider_identity', 'completed_actor_custody',
             'retirement_pause_pins', 'local_free_space_floor_bytes', 'retention',
             'max_clients')


def require(ok, reason):
    actor.require(ok, 'original_budget_' + reason)


def resolve(plan, *, origin, read, max_links=64,
            maximum_metadata_bytes=8 * 1024**2,
            maximum_bank_bytes=128 * 1024**2,
            maximum_total_bytes=256 * 1024**2):
    """Return exact untouched jobs and cumulative consumed/residual limits.

    ``origin`` is the accepted actual/review descriptor of the first held
    original transfer, never a later suffix. Its plan supplies total ceilings
    and positive integral per-job ratios; fractional policies are unsupported.
    ``plan`` is a bound proposed continuation, including the existing actor
    contract, owner identity and source epoch. Source/closure code is not loaded.
    Limits cover unique callback bytes, not decoded Python memory. The callback
    owns physical-file identity checks; each unique pin is read at most once.
    """
    require(callable(read) and type(max_links) is int and 1 <= max_links <= 64,
            'reader_or_depth')
    require(all(type(n) is int and n > 0 for n in
                (maximum_metadata_bytes, maximum_bank_bytes, maximum_total_bytes))
            and maximum_metadata_bytes <= maximum_bank_bytes <= maximum_total_bytes,
            'read_bounds')
    refs, raw_cache, decoded, banks = {}, {}, {}, set()
    total = 0

    def object_pairs(pairs):
        result = {}
        for key, item in pairs:
            require(key not in result, 'duplicate_json_key')
            result[key] = item
        return result

    def finite(text):
        number = float(text)
        require(math.isfinite(number), 'nonfinite_json')
        return number

    def constant(_):
        raise core.IntegrityError('original_budget_nonfinite_json')

    def raw(ref):
        nonlocal total
        require(type(ref) is dict and set(ref) == {'path', 'bytes', 'sha256'}, 'pin')
        path = ref['path']
        require(type(path) is str and path.startswith('/') and '\x00' not in path
                and str(PurePosixPath(path)) == path and '..' not in PurePosixPath(path).parts
                and type(ref['bytes']) is int and ref['bytes'] >= 0
                and type(ref['sha256']) is str and re.fullmatch('[0-9a-f]{64}', ref['sha256']),
                'pin_shape')
        require(path not in refs or refs[path] == ref, 'conflicting_pin')
        limit = maximum_bank_bytes if path in banks else maximum_metadata_bytes
        require(ref['bytes'] <= limit, 'file_bound')
        if path not in raw_cache:
            require(total + ref['bytes'] <= maximum_total_bytes, 'total_bound')
            data = actor.read_raw(ref, read)
            # Also protect JSON read internally by the composed helpers. Source
            # blobs may be non-JSON; their exact bytes are still authenticated.
            try:
                decoded[path] = json.loads(data, object_pairs_hook=object_pairs,
                    parse_float=finite, parse_constant=constant)
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            refs[path], raw_cache[path] = dict(ref), data
            total += len(data)
        return raw_cache[path]

    def value(ref, *, bank=False):
        if bank:
            require(type(ref) is dict and type(ref.get('path')) is str, 'bank_pin')
            banks.add(ref['path'])
        raw(ref)
        require(ref['path'] in decoded, 'json')
        return decoded[ref['path']]

    def accepted(descriptor):
        require(type(descriptor) is dict and set(descriptor) == {'actual', 'review'},
                'closed_descriptor')
        actual, peer = value(descriptor['actual']), value(descriptor['review'])
        require(type(actual) is dict and type(peer) is dict
                and actual['schema'] == 'causes.finite_supplement_tranche_closed.v1'
                and peer.get('accepted_finite_supplement_closed_custody') is True
                and peer.get('accepted_completed_actor_saved_closure') is True
                and peer['actual'] == descriptor['actual'] and peer['source'] == actual['source']
                and peer['findings'] == []
                and all(peer[k] is actual[k] is True for k in
                        ('all_original_attempts_retained', 'original_denominator_preserved'))
                and peer['qualification_findings'] == actual['qualification_findings'],
                'closed_peer')
        require(actual['clean_native_ending'] is True and type(actual['active']) is int
                and actual['active'] == 0 and actual['ambiguous'] == []
                and actual['unresolved_claimed_ids'] == [] and actual['quarantined'] is False
                and type(actual['consecutive_failures']) is int
                and 0 <= actual['consecutive_failures'] < 3, 'clean_ending')
        require(not any(k.endswith('_bank_extension') and k != 'original_bank_extension'
                        for k in actual), 'unknown_extension')
        return actual

    def suffix(candidate, first):
        require(candidate['continuation_scope'] == 'full_untouched_fixed_suffix'
                and 'original_reservation_transfer' not in candidate
                and 'derived_bank' not in candidate, 'same_bank_scope')
        require(candidate['sources'] == plan['sources'], 'suffix_sources')
        require(all(core.digest(candidate[k]) == core.digest(first[k]) for k in PRESERVED)
                and candidate.get('retain_admission_after_healthy_harvest') is True,
                'policy_changed')

    try:
        start = accepted(origin)
        first = value(start['plan'])
        require(first['continuation_scope'] == 'transferred_original_reservation'
                and 'original_reservation_transfer' in first and 'derived_bank' not in first
                and first['mode'] == actor.MODE
                and first.get('retain_admission_after_healthy_harvest') is True,
                'first_transfer_required')
        n, attempt_total, capture_total, uses = [first[k] for k in
            ('maximum_new_requests', 'maximum_admission_attempts',
             'maximum_captures', 'maximum_capture_uses')]
        require(all(type(x) is int and x > 0 for x in (n, attempt_total, capture_total, uses))
                and attempt_total % n == capture_total % n == 0
                and capture_total <= attempt_total, 'integral_origin_policy')
        attempt_rate, capture_rate = attempt_total // n, capture_total // n
        suffix(plan, first)
        dependencies = lambda s: {k: v for k, v in s.items() if k not in ('driver', 'source_lineage')}
        require(dependencies(plan['sources']) == dependencies(first['sources']), 'dependency_sources')
        base = accepted(first['predecessor_custody'])
        full = value(start['full_jobs'], bank=True)
        ids = scope.unique_ids([j['measurement_id'] for j in full])
        prior_count = len(first['occupied_ids'])
        require(base['unstarted_ids'] == [] and base['complete_ids'] == ids[:prior_count]
                and type(base['terminal']) is type(base['occupied']) is int
                and base['terminal'] == base['occupied'] == prior_count
                and first['occupied_ids'] == base['complete_ids'] and len(full) == prior_count + n
                and core.digest(value(base['full_jobs'], bank=True)) == core.digest(full[:prior_count])
                and core.digest(full[prior_count:]) == first['jobs_sha256'], 'origin_bank')
        physical = set()
        for job in full:
            require(job['measurement_id'] == '/'.join(job[k] for k in
                    ('family', 'model', 'run_id', 'stage', 'question_id'))
                    and job['model'] == first['model'] and job['stage'] == 'early'
                    and job['question_id'] in ('evaluation_strongly_favors',
                                              'evaluation_leans_towards_or_favors'), 'original_identity')
            key = (job['run_id'], job['stage'], job['question_id'])
            require(key not in physical, 'physical_alias')
            physical.add(key)
        extension = dict(schema=scope.EXTENSION_SCHEMA, previous_closed=first['predecessor_custody'],
            previous_full_jobs=base['full_jobs'], transfer=first['original_reservation_transfer'],
            source_family=first['family'], model=first['model'], prior_count=prior_count,
            appended_count=n, appended_jobs_sha256=core.digest(full[prior_count:]),
            appended_ids_sha256=core.digest(ids[prior_count:]), cumulative_count=len(full),
            cumulative_jobs_sha256=core.digest(full), full_jobs=start['full_jobs'],
            prior_prefix_unchanged=True, native_families_preserved=True)
        require(core.digest(start['original_bank_extension']) == core.digest(extension), 'origin_extension')
        descriptor, chain, seen = plan['predecessor_custody'], [], set()
        while True:
            require(len(chain) < max_links and descriptor['actual']['path'] not in seen,
                    'cycle_or_depth')
            seen.add(descriptor['actual']['path'])
            closed = accepted(descriptor)
            step = value(closed['plan'])
            prior_descriptor = dict(actual=closed['prior_closed'], review=closed['prior_closed_review'])
            prior = accepted(prior_descriptor)
            require(step['predecessor_custody'] == prior_descriptor
                    and step['occupied_ids'] == prior['complete_ids']
                    and type(step['initial_failures']) is int
                    and step['initial_failures'] == prior['consecutive_failures']
                    and step['output_root'] == closed['output'], 'predecessor')
            require(closed['full_jobs'] == start['full_jobs']
                    and closed['role'] == first['role'] and closed['mode'] == actor.MODE
                    and closed['stop_reason'] in HOLD_REASONS
                    and closed['current_tranche_qualified'] is True
                    and closed['new_invalid_ids'] == closed['current_qualification_findings'] == []
                    and core.digest(closed['inherited_qualification_findings']) == core.digest(prior['qualification_findings'])
                    and core.digest(closed['qualification_findings']) == core.digest(prior['qualification_findings'])
                    and closed['prior_invalid_ids'] == closed['cumulative_invalid_ids'] == prior['cumulative_invalid_ids'],
                    'hold_history')
            begin, end = len(step['occupied_ids']), len(closed['complete_ids'])
            require(prior_count <= begin <= end < len(full)
                    and step['occupied_ids'] == ids[:begin]
                    and closed['complete_ids'] == ids[:end] and closed['unstarted_ids'] == ids[end:]
                    and type(closed['terminal']) is type(closed['occupied']) is int
                    and closed['terminal'] == closed['occupied'] == end
                    and type(step['maximum_new_requests']) is int
                    and step['maximum_new_requests'] == len(full) - begin
                    and step['jobs_sha256'] == core.digest(full[begin:]), 'partition')
            invocations = closed['native_invocations']
            require(type(invocations) is list and len(invocations) == len(prior['native_invocations']) + 1
                    and core.digest(invocations[:-1]) == core.digest(prior['native_invocations']), 'invocation_prefix')
            current = invocations[-1]
            require(current['plan'] == closed['plan'] and current['source'] == step['sources']['driver']
                    and current['result'] == closed['native_result'] and current['process_absent'] is True
                    and current['family'] == step['family'] and current['model'] == step['model']
                    and current['jobs_sha256'] == step['jobs_sha256']
                    and current['original_reservation_transfer'] == step.get('original_reservation_transfer')
                    and 'derived_bank' not in current, 'invocation')
            for source in step['sources'].values():
                raw(source)
            chain.append((descriptor, closed, step))
            if descriptor == origin:
                require(step == first, 'origin_plan')
                break
            suffix(step, first)
            require(closed.get('original_bank_extension') is None
                    and prior['full_jobs'] == start['full_jobs'], 'second_extension')
            descriptor = prior_descriptor
        consumed_attempts = consumed_captures = 0
        for _, closed, step in reversed(chain):
            count = step['maximum_new_requests']
            a = min(attempt_rate * count, attempt_total - consumed_attempts)
            c = min(capture_rate * count, capture_total - consumed_captures)
            require(all(type(step[k]) is int for k in
                        ('maximum_admission_attempts', 'maximum_captures', 'maximum_capture_uses'))
                    and step['maximum_admission_attempts'] == a > 0
                    and step['maximum_captures'] == c > 0 and step['maximum_capture_uses'] == uses,
                    'prior_ceiling')
            wrapper = value(closed['native_result']); result = wrapper['result']
            attempts, captures = wrapper['attempts'], wrapper['captures']
            count_done = len(closed['complete_ids']) - len(step['occupied_ids'])
            require(type(attempts) is type(captures) is int
                    and count_done <= attempts <= a and 0 <= captures <= min(attempts, c)
                    and (captures > 0 or count_done == 0)
                    and all(type(result[k]) is int for k in
                            ('consecutive_failures', 'active', 'new_final', 'remaining_unclaimed'))
                    and result['active'] == 0 and result['consecutive_failures'] == closed['consecutive_failures']
                    and result['stop_reason'] == closed['stop_reason'] and result['new_final'] == count_done
                    and result['remaining_unclaimed'] == len(closed['unstarted_ids'])
                    and set(scope.unique_ids(result['completed_ids'])) == set(closed['complete_ids'][len(step['occupied_ids']):]),
                    'native_consumption')
            consumed_attempts += attempts; consumed_captures += captures
        head = chain[0][1]
        jobs = full[len(head['complete_ids']):]
        remaining_attempts = min(attempt_rate * len(jobs), attempt_total - consumed_attempts)
        remaining_captures = min(capture_rate * len(jobs), capture_total - consumed_captures)
        require(jobs and all(type(plan[k]) is int for k in
                            ('maximum_admission_attempts', 'maximum_captures', 'maximum_capture_uses'))
                and plan['maximum_new_requests'] == len(jobs)
                and plan['maximum_admission_attempts'] == remaining_attempts > 0
                and plan['maximum_captures'] == remaining_captures > 0
                and plan['maximum_capture_uses'] == uses, 'remaining_ceiling')
        require(scope.resolve(plan, head, read=raw)['jobs'] == jobs, 'scope')
        actor.validate_contract(plan, value(plan['contract']), jobs,
            initial_failures=head['consecutive_failures'], max_clients=plan['max_clients'], read=raw)
        sources = lineage.validate_continuation(plan['native_source_migration'],
            predecessor=plan['predecessor_custody'], sources=plan['sources'], read=raw, max_links=max_links)
        require(sources['original_migration'] == first['native_source_migration'], 'original_migration')
        for source in plan['sources'].values():
            raw(source)
        return dict(jobs=jobs, origin=origin, original_migration=sources['original_migration'],
            used_attempts=consumed_attempts, used_captures=consumed_captures,
            remaining_attempts=remaining_attempts, remaining_captures=remaining_captures,
            maximum_capture_uses=uses, links=[d for d, _, _ in reversed(chain)],
            required_pins=[refs[p] for p in sorted(refs)], read_bytes=total)
    except (KeyError, TypeError, IndexError) as error:
        raise core.IntegrityError('original_budget_shape') from error
