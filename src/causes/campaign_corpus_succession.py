"""Explicit native corpus lineage; never treat pending work as completed."""

import hashlib
import json
from pathlib import Path

from causes import campaign_handoff as h
from causes.corpus_continuation import control_full_selection


POLICY_SCHEMA = 'causes.campaign_monitor_policy.v2'
ADMISSION_SCHEMA = 'causes.native_corpus_admission_policy.v2'
TEMPLATE_SCHEMA = 'causes.native_corpus_admission_policy_template.v2'


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(h.encoded(value)).hexdigest()


def closed_issued_history(jobs, config, raw, custody, blocks=(), allocator=None):
    """Check an exact issued subset and retain all unclaimed durable jobs.

    ``allocator`` must be a fresh instance of the pinned predecessor allocator.
    Blocks are its entire immutable persisted sequence, including any durable
    block whose publication event was not written before a stop.
    """
    require(raw and raw.endswith(b'\n'), 'closed_history_missing_or_torn')
    expected = {job['allocation']['run_id']: digest(job) for job in jobs}
    require(len(expected) == len(jobs), 'duplicate_predecessor_assignment')
    block_hashes = {}
    for index, block in enumerate(blocks):
        require(allocator is not None and block['block_index'] == index,
                'closed_history_block_sequence')
        allocator.accept(block)
        block_hashes[index] = digest(block)
        for job in block['jobs']:
            rid = job['allocation']['run_id']
            require(rid not in expected, 'duplicate_predecessor_block_assignment')
            expected[rid] = digest(job)
    claims, finals, published = {}, {}, []
    published_ids = set(job['allocation']['run_id'] for job in jobs)
    pause_seen = closed_at = None
    rows = [json.loads(line) for line in raw.splitlines()]
    for position, row in enumerate(rows):
        event = row['event']
        require(not event.startswith('unresolved_') and event != 'controller_error',
                'ambiguous_predecessor_history')
        if event == 'extension_block_created':
            index = row['block_index']
            require(index == len(published) and block_hashes.get(index) == row['block_sha256'],
                    'predecessor_published_block_changed')
            published.append(index)
            published_ids.update(job['allocation']['run_id'] for job in blocks[index]['jobs'])
        elif event == 'claimed':
            rid = row['run_id']
            require(pause_seen is None and rid in published_ids and rid not in claims
                    and expected.get(rid) == row['job_sha256']
                    and row['config_sha256'] == digest(config),
                    'predecessor_claim_changed_or_after_pause')
            claims[rid] = row
        elif event == 'episode_finished':
            rid = row['run_id']
            require(rid in claims and rid not in finals
                    and row['job_sha256'] == expected[rid]
                    and type(row.get('technical_failure')) is bool
                    and row.get('replayed') is False,
                    'predecessor_final_identity_or_disposition')
            finals[rid] = row
        elif event == 'existing_completed_summary':
            raise ValueError('unreviewed_predecessor_replay')
        elif event == 'pause_seen':
            require(pause_seen is None and row.get('admission_stopped') is True,
                    'predecessor_pause_changed')
            pause_seen = position
        elif event == 'controller_paused':
            require(pause_seen is not None and closed_at is None
                    and set(claims) == set(finals), 'predecessor_not_drained')
            closed_at = position
        if closed_at is not None:
            require(event in ('controller_paused', 'controller_finished'),
                    'predecessor_event_after_closed_issued_history')
    require(closed_at is not None and set(claims) == set(finals),
            'closed_issued_membership_incomplete')
    pending = sorted(set(expected) - set(claims))
    require(rows[closed_at]['pending'] == len(pending)
            and custody['never_claimed_ids'] == pending
            and custody['completed_ids'] == sorted(finals)
            and set(custody['summaries']) == set(finals),
            'closed_issued_or_pending_custody_changed')
    for rid, ref in custody['summaries'].items():
        require(Path(ref['path']).parent.name == rid
                and Path(ref['path']).name == 'summary.json'
                and ref['sha256'] == finals[rid]['summary_sha256'],
                'closed_issued_summary_changed')
        h.checked_bytes(ref)
    return {'assigned': len(expected), 'issued': len(claims),
            'finals': len(finals), 'never_claimed_ids': pending,
            'assignment_hashes': expected, 'published_blocks': published,
            'persisted_block_hashes': block_hashes,
            'final_journal_sha256': hashlib.sha256(raw).hexdigest()}


def accepted(pair, flag, key, reader=h.checked):
    value, review = reader(pair['evidence']), reader(pair['review'])
    require(review.get(flag) is True and review.get(key) == pair['evidence'],
            'unaccepted_native_succession_input:' + flag)
    return value


def closed_predecessor(row, reader=h.checked):
    """Resolve an independently accepted full closure, without a live claim."""
    value = accepted(row['closure'], 'accepted_closed_issued_corpus_history',
                     'closure', reader)
    require(value['schema'] == 'causes.closed_issued_corpus_history.v1',
            'native_closure_schema')
    for key in ('output_root', 'process', 'launch'):
        require(value[key] == row[key], 'native_predecessor_identity_changed')
    require(type(value['claims']) is int and type(value['finals']) is int
            and value['claims'] >= 0 and value['claims'] == value['finals']
            and type(value['active_claims']) is int and value['active_claims'] == 0
            and value['ambiguous_ids'] == [] and value['owner_absent'] is True
            and value['output_lock_free'] is True, 'native_predecessor_not_closed')
    pending = reader(value['never_claimed'])
    require(isinstance(pending, list) and pending == sorted(set(pending))
            and len(pending) == value['pending_count'], 'native_pending_list_changed')
    require(value['journal']['path'] == row['output_root'] + '/.dispatcher/journal.jsonl'
            and value['owned_pause']['path'] == row['pause_path'],
            'native_closure_path_changed')
    ownership = reader(value['pause_ownership'])
    require(ownership['process'] == row['process']
            and ownership['marker'] == value['owned_pause']
            and ownership['marker_stat'] == value['owned_pause_stat'],
            'native_pause_ownership_changed')
    return value


def live_closed_predecessor(row, reader=h.checked):
    """Recheck immutable custody, owned pause, absence and a free output lock."""
    value = closed_predecessor(row, reader)
    for key in ('journal', 'never_claimed', 'owned_pause', 'pause_ownership'):
        h.checked_bytes(value[key])
    require(h.stat_identity(value['owned_pause']['path']) == value['owned_pause_stat'],
            'native_owned_pause_replaced')
    require(h.retirement_state(row['process'])['absent']
            and h.lock_free(Path(row['output_root']) / '.dispatcher.lock'),
            'native_predecessor_still_live_or_locked')
    return value


def native_lineage(policy, registration, reader=h.checked):
    """Validate an appended native lineage against its unchanged v1 base."""
    lineage = policy['native_successions']
    base = reader(lineage['base_policy'])
    base_registration = reader(lineage['base_registration'])
    review = reader(lineage['base_review'])
    require(base['schema'] == 'causes.campaign_monitor_policy.v1'
            and review.get('accepted_native_succession_base') is True
            and review.get('policy') == lineage['base_policy']
            and review.get('registration') == lineage['base_registration'],
            'native_base_not_accepted')
    count = lineage['base_phase_count']
    require(count == len(base['phases']) and policy['phases'][:count] == base['phases']
            and policy['sync_roots'][:count] == base['sync_roots']
            and base_registration['phase'] == base['phases'][-1]['phase'],
            'native_base_root_order_changed')
    for key in ('controller_host', 'transfer_host', 'host_transfer', 'provider',
                'tunnels', 'reverse_listener_process', 'provider_port', 'relay_port',
                'model', 'model_key', 'max_model_len', 'holds', 'remote_base'):
        require(policy[key] == base[key], 'native_base_binding_changed:' + key)
    require(policy['minimum_gpu_free_mib'] >= base['minimum_gpu_free_mib']
            and policy['max_observation_age_seconds'] <= base['max_observation_age_seconds'],
            'native_observation_limits_weakened')
    links, phases = lineage['links'], policy['phases']
    require(len(links) == len(phases) - count and links
            and registration['phase'] == phases[-1]['phase'], 'native_active_tail_changed')
    for index, link in enumerate(links, count):
        phase, previous = phases[index], phases[index - 1]
        require(link['phase'] == phase['phase']
                and link['predecessor']['phase'] == previous['phase']
                and link['predecessor']['output_root'] == previous['output_root']
                and link['predecessor']['process'] == previous['process'],
                'native_predecessor_order_changed')
        closed_predecessor(link['predecessor'], reader)
        allocation = accepted(link['allocation'], 'accepted_native_corpus_allocation',
                              'allocation', reader)
        native_allocation(allocation, reader)
        adoption = reader(link['root_adoption'])
        require(adoption.get('root_accepted') is True
                and adoption.get('allocation') == link['allocation']['evidence']
                and adoption.get('allocation_review') == link['allocation']['review'],
                'native_allocation_not_adopted')
        mode = {'finite_qualification': 'finite', 'continuous_corpus': 'continuous'}
        require(allocation['family'] not in [p['runtime_cohort'] for p in phases[:index]], 'native_family_reused')
        require(allocation['origin'] == 'native_successor'
                and mode.get(allocation['target_mode']) == phase['mode']
                and allocation['output_root'] == phase['output_root']
                and allocation['family'] == phase['runtime_cohort']
                and allocation['jobs'] == phase['controller_jobs']
                and allocation['config'] == phase['controller_config'] == base['phases'][-1]['controller_config']
                and allocation['workers'] == phase['workers']
                and allocation['no_fixed_study_pooling'] is True
                and phase['capacity_required'] is True,
                'native_allocation_scope_changed')
        require(type(allocation['max_full']) is int
                and 0 <= allocation['max_full'] <= allocation['workers'],
                'native_full_capacity_invalid')
        if phase['mode'] == 'continuous':
            require(allocation['control'] == phase['continuation']['control'],
                    'native_continuation_control_changed')
            full_selection = control_full_selection(reader(allocation['control']))
            require(allocation['workload'] == ('mixed' if full_selection else 'prefix_only')
                    and ((allocation['max_full'] > 0) == bool(full_selection)),
                    'native_continuation_workload_changed')
        else:
            require(allocation['workload'] == 'mixed' and allocation['max_full'] > 0,
                    'native_qualification_workload_changed')
        if index == len(phases) - 1:
            argv = registration['controller']['process']['argv']
            require(registration['controller']['workers'] == allocation['workers']
                    and int(argv[argv.index('--max-full') + 1]) == allocation['max_full'],
                    'native_full_capacity_changed')
    scopes = policy['history']['donor_scopes']
    old_scopes = base['history']['donor_scopes']
    require(scopes[:len(old_scopes)] == old_scopes, 'native_base_donor_history_changed')
    local = [row for row in scopes if row['host'] == policy['controller_host']]
    require([row['output_root'] for row in local] == [p['output_root'] for p in phases[:-1]]
            and all(row['mode'] == 'sealed' and row['recorder_relevant'] is True for row in local),
            'native_closed_donor_scope_changed')
    require(registration['donor_histories'] == phases[-1]['donor_histories'],
            'native_donor_registration_changed')
    projection = [{key: row[key] for key in ('host', 'output_root', 'mode', 'recorder_relevant')}
                  for row in registration['donor_histories']]
    require(projection == scopes, 'native_donor_history_missing')
    for link in links:
        predecessor = link['predecessor']
        closure = closed_predecessor(predecessor, reader)
        donors = [row for row in registration['donor_histories'] if row['host'] == policy['controller_host'] and row['output_root'] == predecessor['output_root']]
        require(len(donors) == 1 and donors[0]['journal'] == closure['journal'], 'native_closed_history_pin_changed')
    return links[-1]


def native_allocation(value, reader=h.checked):
    """Verify a finite selected queue or an unmodified continuous allocator."""
    from causes.corpus_continuation import CorpusContinuation
    require(value['schema'] == 'causes.native_corpus_allocation.v1'
            and value['origin'] == 'native_successor'
            and value['no_fixed_study_pooling'] is True, 'native_allocation_origin_changed')
    require(type(value['workers']) is int and value['workers'] > 0
            and type(value['max_full']) is int and 0 <= value['max_full'] <= value['workers'],
            'native_allocation_capacity_changed')
    control = reader(value['allocation_control'])
    prototypes, config = reader(control['prototypes']), reader(control['config'])
    allocator = CorpusContinuation.from_control(control, prototypes, config)
    jobs, extensions = reader(value['jobs']), reader(value['extensions'])
    require(control['family'] == value['family'] and control['config'] == value['config']
            and control['model_key'] == value['model_key']
            and control['task_host'] == control['provider_host'] == value['controller_host'],
            'native_allocation_route_changed')
    full = control_full_selection(control)
    require(value['workload'] == ('mixed' if full else 'prefix_only')
            and bool(full) == (value['max_full'] > 0), 'native_allocation_workload_changed')
    if value['target_mode'] == 'finite_qualification':
        require(full and value['control'] is None and extensions == []
                and value['selection']['block_index'] == 0, 'native_finite_scope_changed')
        block = allocator.block(0)
        by_id = {job['allocation']['run_id']: job for job in block['jobs']}
        selected = value['selection']['run_ids']
        require(isinstance(selected, list) and selected and len(set(selected)) == len(selected)
                and all(rid in by_id for rid in selected)
                and h.encoded(jobs) == h.encoded([by_id[rid] for rid in selected]),
                'native_finite_selection_changed')
        modes = [job['allocation']['continue_to_end'] for job in jobs]
        require(True in modes and False in modes, 'native_finite_exposure_scope_empty')
    else:
        require(value['target_mode'] == 'continuous_corpus' and jobs == []
                and value['selection'] is None and value['control'] == value['allocation_control']
                and value['extensions'] == control['prototypes'], 'native_continuous_scope_changed')
    return jobs, config, extensions, allocator
