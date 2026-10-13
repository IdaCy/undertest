"""Explicit native corpus lineage; never treat pending work as completed."""

import copy
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


STACK_BINDING_KEYS = ('controller_host', 'transfer_host', 'host_transfer', 'provider',
                      'tunnels', 'reverse_listener_process', 'provider_port', 'relay_port',
                      'model', 'model_key', 'max_model_len', 'holds', 'remote_base')
STACK_MUTABLE_KEYS = {'controller_host', 'provider', 'tunnels',
                      'reverse_listener_process', 'provider_port', 'relay_port'}


def external_self_custody(exclusion, provider_host, provider, read, read_bytes, accept):
    rows = exclusion.get('external_self_owners', [])
    require(isinstance(rows, list) and len({row['id'] for row in rows}) == len(rows)
            and len({(row['host'], row['output_root']) for row in rows}) == len(rows),
            'external_self_owner_inventory_changed')
    require(all(row.get('host') == provider_host for row in exclusion['owners'])
            and not ({row['id'] for row in rows} & {row['id'] for row in exclusion['owners']}),
            'external_self_local_partition_changed')
    pins = []
    for row in rows:
        require(isinstance(row['host'], str) and row['host'] and row['host'] != provider_host
                and row['kind'] == 'self' and row['lock_paths']
                and len(set(row['lock_paths'])) == len(row['lock_paths']),
                'external_self_execution_host_required')
        closed = accept(row['closure'], 'accepted_native_owner_closure_projection', 'closure')
        plan = read(row['self_plan'])
        jobs = read(plan['jobs'])
        require(closed['schema'] == 'causes.native_owner_closure_projection.v1'
                and all(closed[key] == row[key] for key in
                        ('id', 'host', 'kind', 'process', 'output_root', 'lock_paths', 'self_plan'))
                and closed['provider_host'] == plan['host'] == provider_host
                and closed['provider'] == provider
                and closed['output_root'] == plan['output_root']
                and closed['reservation_jobs'] == plan['jobs']
                and type(closed['assigned']) is int and closed['assigned'] == len(jobs) > 0
                and closed['terminal'] == closed['assigned']
                and closed['whole_reservation_complete'] is True
                and closed['owner_absent'] is True and closed['all_locks_free'] is True
                and closed['active_claims'] == 0 and closed['attempts_preserved'] is True
                and closed['dispositions_preserved'] is True,
                'external_self_closure_changed')
        custody = accept(row['execution_custody'], 'accepted_native_external_self_execution_custody', 'custody')
        require(custody['schema'] == 'causes.native_external_self_execution_custody.v1'
                and custody['execution_host'] == row['host']
                and custody['provider_host'] == provider_host and custody['provider'] == provider
                and custody['closure'] == row['closure'] and custody['self_plan'] == row['self_plan']
                and custody['reservation_jobs'] == plan['jobs']
                and all(custody[key] == row[key] for key in ('process', 'output_root', 'lock_paths'))
                and custody['closed_on_execution_host'] is True
                and custody['future_self_launch_excluded'] is True
                and custody['original_execution_evidence'] and custody['original_execution_reviews'],
                'external_self_execution_custody_changed')
        refs = [*row['closure'].values(), *row['execution_custody'].values(), row['self_plan'], plan['jobs'],
                *custody['original_execution_evidence'], *custody['original_execution_reviews']]
        for ref in refs:
            read_bytes(ref)
        pins.extend(refs)
    return {'owners': rows, 'pins': pins}


def native_fault_applicability(transition, failed_histories, policy, reader=h.checked):
    """Bind relevance to one archived donor; never rewrite its replay state."""
    value = transition['value']
    pair = value.get('fault_applicability')
    if pair is None:
        return []
    decision = accepted(pair, 'accepted_native_archived_model_fault_applicability',
                        'applicability', reader)
    require(decision['schema'] == 'causes.native_archived_model_fault_applicability.v1'
            and all(decision[key] == value[key] for key in
                    ('source_host', 'target_host', 'base_policy', 'archived_custody',
                     'history_ledger', 'actor_stack', 'current_exclusion')),
            'native_fault_applicability_transition_changed')
    qualification = accepted(decision['current_actor_qualification'],
                             'accepted_native_actor_protocol_qualification', 'qualification', reader)
    stack = transition['stack']
    require(qualification['schema'] == 'causes.native_actor_protocol_qualification.v1'
            and qualification['host'] == value['target_host']
            and qualification['provider'] == stack['bindings']['provider']
            and qualification['config'] == stack['config']
            and qualification['native_actor_sources'] == stack['native_actor_sources']
            and qualification['workload_kind'] == 'prefix_actor_tool'
            and qualification['workers'] == 4 and qualification['max_full'] == 0
            and qualification['assigned'] == qualification['terminal'] == 8
            and qualification['technical_failures'] == 0 and qualification['replayed'] is False
            and qualification['owner_absent'] is True and qualification['all_locks_free'] is True
            and qualification['whole_self_closed_before_bootstrap'] is True,
            'native_current_actor_qualification_changed')
    for ref in qualification['native_actor_sources']:
        h.checked_bytes(ref)
    for key in ('jobs', 'journal', 'owner_closure', 'guard_evidence',
                'saved_prefix_tool_evidence', 'saved_prefix_tool_review', 'current_token_custody'):
        h.checked_bytes(qualification[key])
    jobs = reader(qualification['jobs'])
    require(len(jobs) == 8 and len({job['allocation']['run_id'] for job in jobs}) == 8
            and all(job['allocation']['continue_to_end'] is False for job in jobs),
            'native_actor_qualification_jobs_changed')
    coverage = qualification['observed_tool_coverage']
    require(set(coverage) == {'read', 'bash'}
            and all(set(row) == {'observed_calls', 'verified_executions', 'complete_exchanges'}
                    and all(type(count) is int and count >= 0 for count in row.values())
                    and row['complete_exchanges'] <= row['verified_executions'] <= row['observed_calls']
                    for row in coverage.values())
            and type(qualification['complete_native_tool_exchanges']) is int
            and qualification['complete_native_tool_exchanges'] >= 1
            and qualification['complete_native_tool_exchanges']
                == sum(row['complete_exchanges'] for row in coverage.values()),
            'native_actor_tool_exchange_not_qualified')
    scopes = [(policy['workers'], policy['max_full'], policy['resident_config'])] if 'workers' in policy else [
        (allocation['workers'], allocation['max_full'], allocation['config'])
        for allocation in (reader(row['allocation']['evidence']) for row in policy['native_successions']['links'])]
    require(all(workers <= qualification['workers'] and max_full == 0 and config == qualification['config']
                for workers, max_full, config in scopes),
            'native_actor_qualification_scope_exceeded')
    archived = transition['archive']['donor_histories']
    aliases = {h.encoded(row['original']): row['copy'] for row in transition['archive']['files']}
    archived_reader = lambda ref: reader(aliases.get(h.encoded(ref), ref))
    require(decision['rows'] and len({row['donor']['output_root'] for row in decision['rows']})
            == len(decision['rows']), 'native_fault_applicability_duplicate_or_empty')
    result = []
    for row in decision['rows']:
        donor = row['donor']
        histories = [old for old in failed_histories if old['output_root'] == donor['output_root']
                     and old['host'] == donor['host'] and old['journal'] == donor['journal']]
        require(donor in archived and donor['host'] == value['source_host']
                and donor['mode'] == 'sealed' and donor['recorder_relevant'] is True
                and len(histories) == 1, 'native_fault_applicability_not_failed_archived_donor')
        history = histories[0]
        attribution = accepted(row['provider_binding'], 'accepted_native_archived_actor_provider_binding',
                               'binding', archived_reader)
        require(attribution['schema'] == 'causes.native_archived_actor_provider_binding.v1'
                and attribution['donor'] == donor
                and attribution['original_custody'] == history['original_custody']
                and attribution['failed_history_id'] == history['id'],
                'native_archived_provider_attribution_changed')
        original = archived_reader(attribution['owner_policy'])
        require(original['schema'] == ADMISSION_SCHEMA
                and original['controller_host'] == donor['host']
                and original['output_root'] == donor['output_root']
                and original['resident_config'] == attribution['config']
                and original['provider'] == attribution['provider']
                and {'host': donor['host'], 'provider': original['provider']}
                    != {'host': qualification['host'], 'provider': qualification['provider']},
                'native_archived_provider_policy_changed')
        config = archived_reader(attribution['config'])
        model_route = 'model:' + digest([config.get('model'), config.get('endpoint')])
        require(row['model_routes'] == [model_route] and model_route in policy['history']['model_aliases'],
                'native_fault_applicability_route_changed')
        raw = h.checked_bytes(aliases[h.encoded(donor['journal'])])
        events = [json.loads(line) for line in raw.splitlines()]
        claims = [event for event in events if event['event'] == 'claimed']
        require(claims and all(claim['config_sha256'] == digest(config) for claim in claims),
                'native_archived_claim_provider_config_changed')
        from causes.campaign_monitor import replay_history, history_blockers
        replay = replay_history(raw)
        history_blockers(replay, policy['history']['model_aliases'],
                         policy['history']['task_routes'], donor['recorder_relevant'])
        require(replay == history['journal_summary'] and replay['unconsumed_tail_bytes'] == 0
                and row['retained_journal_summary'] == replay,
                'native_fault_applicability_replay_changed')
        result.append({'donor': donor, 'model_routes': [model_route],
                       'applicability': pair, 'qualification': decision['current_actor_qualification']})
    return result


def native_stack_transition(policy, reader=h.checked):
    """Resolve an explicit stack/host transition without relabeling old owners.

    The three independent evidence contracts separate archived source-host
    custody, the versioned current actor stack, and complete current-host owner
    exclusion. A self qualification is never accepted as actor capacity here.
    Native allocation, fresh foundation and actor-capacity gates remain separate.
    """
    pair = policy.get('stack_transition')
    if pair is None:
        return None
    require(policy['schema'] in (POLICY_SCHEMA, ADMISSION_SCHEMA), 'native_stack_requires_native_policy')
    value = accepted(pair, 'accepted_native_corpus_stack_transition', 'transition', reader)
    require(value['schema'] == 'causes.native_corpus_stack_transition.v1'
            and value['no_fixed_study_pooling'] is True, 'native_stack_transition_scope')
    predecessor_monitor = None
    if 'native_successions' in policy:
        base_ref = policy['native_successions']['base_policy']
    else:
        predecessor_monitor = reader(policy['predecessor_monitor']['evidence'])
        if predecessor_monitor.get('stack_transition') is not None:
            require(predecessor_monitor.get('stack_transition') == pair, 'native_stack_successor_transition_changed')
            base_ref = predecessor_monitor['native_successions']['base_policy']
        else:
            base_ref = policy['predecessor_monitor']['evidence']
    require(value['base_policy'] == base_ref, 'native_stack_base_changed')
    base = reader(base_ref)
    base_registration = reader(value['base_registration'])
    require(base_registration['phase'] == base['phases'][-1]['phase']
            and base_registration['controller_host'] == base['controller_host']
            and base_registration['provider'] == base['provider']
            and base_registration['controller_config'] == base['phases'][-1]['controller_config']
            and ('native_successions' not in policy or value['base_registration'] == policy['native_successions']['base_registration']),
            'native_stack_base_registration_changed')
    require(base['schema'] in ('causes.campaign_monitor_policy.v1', POLICY_SCHEMA)
            and base.get('stack_transition') is None
            and value['source_host'] == base['controller_host']
            and value['target_host'] == policy['controller_host']
            and value['source_host'] != value['target_host'], 'native_stack_host_scope')
    archive = accepted(value['archived_custody'], 'accepted_native_archived_host_custody',
                       'custody', reader)
    stack = accepted(value['actor_stack'], 'accepted_native_corpus_actor_stack', 'stack', reader)
    exclusion = accepted(value['current_exclusion'], 'accepted_native_current_owner_exclusion',
                         'exclusion', reader)
    require(archive['schema'] == 'causes.native_archived_host_custody.v1'
            and archive['base_policy'] == base_ref
            and archive['base_registration'] == value['base_registration']
            and archive['source_host'] == value['source_host']
            and archive['phases'] == base['phases']
            and archive['source_owners_closed'] is True
            and archive['future_source_launch_excluded'] is True,
            'native_archived_host_custody_changed')
    require(stack['schema'] == 'causes.native_corpus_actor_stack.v1'
            and stack['workload_kind'] == 'actor'
            and isinstance(stack['version'], str) and bool(stack['version'])
            and stack['config'] == value['current_config']
            and set(stack['bindings']) == set(STACK_BINDING_KEYS), 'native_actor_stack_scope')
    binding = stack['bindings']
    for key in STACK_BINDING_KEYS:
        if key not in STACK_MUTABLE_KEYS:
            require(binding[key] == base[key], 'native_stack_immutable_binding:' + key)
        if key in policy:
            require(policy[key] == binding[key], 'native_stack_current_binding:' + key)
    old_ref = base['phases'][-1]['controller_config']
    require(value['old_config'] == old_ref, 'native_stack_old_config_changed')
    old, current = reader(old_ref), reader(value['current_config'])
    expected = copy.deepcopy(old)
    old_floor, new_floor = old['dispatcher']['disk_floor_bytes'], current['dispatcher']['disk_floor_bytes']
    require(type(old_floor) is int and type(new_floor) is int and new_floor >= old_floor,
            'native_stack_disk_floor_weakened')
    expected['dispatcher']['disk_floor_bytes'] = new_floor
    require(current == expected and value['configuration_change'] == {
        'path': '/dispatcher/disk_floor_bytes', 'before': old_floor, 'after': new_floor},
        'native_stack_undeclared_configuration_change')
    require(exclusion['schema'] == 'causes.native_current_owner_exclusion.v1'
            and exclusion['host'] == value['target_host']
            and exclusion['actor_stack'] == value['actor_stack']['evidence']
            and exclusion['roster_complete'] is True
            and exclusion['future_owner_launch_excluded'] is True,
            'native_current_owner_exclusion_scope')
    external = external_self_custody(exclusion, value['target_host'], stack['bindings']['provider'],
                                    reader, h.checked_bytes,
                                    lambda pair, flag, key: accepted(pair, flag, key, reader))
    require(all(row['host'] != value['source_host'] for row in external['owners'])
            and not ({row['id'] for row in external['owners']}
                     & {row['id'] for row in exclusion['owners']}),
            'native_external_self_partition_changed')
    external_entries = [{'id': row['id'], 'host': row['host'], 'kind': 'self', 'evidence': row['closure']}
                        for row in external['owners']]
    ledger = accepted(value['history_ledger'], 'accepted_native_intervening_owner_history', 'history', reader)
    require(ledger['schema'] == 'causes.native_intervening_owner_history.v1'
            and ledger['base_policy'] == base_ref
            and ledger['source_host'] == value['source_host'] and ledger['target_host'] == value['target_host']
            and ledger['complete'] is True
            and archive['history_ledger'] == exclusion['history_ledger'] == value['history_ledger']['evidence'],
            'native_intervening_history_scope_changed')
    entries = ledger['entries']
    require(entries and len({row['id'] for row in entries}) == len(entries), 'native_intervening_history_incomplete')
    failed_histories = []
    for row in entries:
        if row in external_entries:
            continue  # Exact execution-host custody was validated; this is not a local owner.
        require(row['host'] in (value['source_host'], value['target_host'])
                and row['kind'] in ('failed_qualification', 'actor', 'self'), 'native_intervening_history_kind')
        if row['kind'] == 'failed_qualification':
            require(row['host'] == value['source_host'], 'native_failed_qualification_host_changed')
            failed = accepted(row['evidence'], 'accepted_failed_qualification_custody', 'actual', reader)
            require(failed.get('qualification_accepted') is False
                    and reader(row['evidence']['review']).get('qualification_accepted') is False,
                    'native_failed_qualification_reclassified')
            history = accepted(row['history'], 'accepted_native_failed_qualification_history', 'history', reader)
            require(history['schema'] == 'causes.native_failed_qualification_history.v1'
                    and all(history[key] == row[key] for key in ('id', 'host'))
                    and history['original_custody'] == row['evidence']
                    and history['qualification_accepted'] is False
                    and history['owner_absent'] is True and history['all_locks_free'] is True
                    and history['attempts_preserved'] is True and history['dispositions_preserved'] is True,
                    'native_failed_qualification_history_changed')
            failed_histories.append(history)
        else:
            require(row['host'] == value['target_host'], 'native_unsupported_archived_owner_kind')
            closed = accepted(row['evidence'], 'accepted_native_owner_closure_projection', 'closure', reader)
            require(closed['schema'] == 'causes.native_owner_closure_projection.v1'
                    and all(closed[key] == row[key] for key in ('id', 'host', 'kind'))
                    and closed['owner_absent'] is True and closed['all_locks_free'] is True
                    and type(closed['active_claims']) is int and closed['active_claims'] == 0
                    and closed['attempts_preserved'] is True and closed['dispositions_preserved'] is True,
                    'native_intervening_owner_not_closed')
            for ref in closed['original_evidence'] + closed['original_reviews']:
                reader(ref)
    owners = exclusion['owners']
    require([{'id': row['id'], 'host': value['target_host'], 'kind': row['kind'], 'evidence': row['closure']} for row in owners]
            == [row for row in entries if row['host'] == value['target_host'] and row['kind'] in ('actor', 'self')],
            'native_current_owner_history_dropped')
    require(archive['additional_history'] == [row for row in entries if row['host'] == value['source_host']],
            'native_archived_additional_history_dropped')
    require([row for row in entries if row['host'] not in (value['source_host'], value['target_host'])]
            == external_entries, 'native_external_self_history_dropped')
    require(isinstance(owners, list) and bool(owners)
            and len({row['id'] for row in owners}) == len(owners)
            and len({row['output_root'] for row in owners}) == len(owners)
            and any(row['kind'] == 'self' for row in owners + external['owners']), 'native_current_owner_roster_incomplete')
    actor_journals = {}
    for row in owners:
        require(row['kind'] in ('actor', 'self') and row['lock_paths']
                and len(set(row['lock_paths'])) == len(row['lock_paths']), 'native_current_owner_shape')
        for path in [row['output_root'], *row['lock_paths']]:
            require(Path(path).is_absolute() and str(Path(path)) == path and '..' not in Path(path).parts,
                    'native_current_owner_path')
        closed = accepted(row['closure'], 'accepted_native_owner_closure_projection', 'closure', reader)
        require(closed['schema'] == 'causes.native_owner_closure_projection.v1'
                and all(closed[key] == row[key] for key in ('id', 'kind', 'output_root', 'process', 'lock_paths'))
                and closed['host'] == value['target_host']
                and closed['owner_absent'] is True and closed['all_locks_free'] is True
                and type(closed['active_claims']) is int and closed['active_claims'] == 0
                and closed['original_evidence'] and closed['original_reviews']
                and closed['attempts_preserved'] is True and closed['dispositions_preserved'] is True,
                'native_current_owner_not_closed')
        # Original private/self schemas remain original; a reviewed projection
        # joins their complete evidence rather than fabricating native phases.
        for ref in closed['original_evidence'] + closed['original_reviews']:
            reader(ref)
        if row['kind'] == 'actor':
            actor_journals[row['output_root']] = closed['journal']
        if row['kind'] == 'self':
            require(closed['whole_reservation_complete'] is True
                    and type(closed['assigned']) is int and closed['assigned'] > 0
                    and type(closed['terminal']) is int and closed['terminal'] == closed['assigned'], 'native_self_reservation_incomplete')
    files = archive['files']
    require(files and len({row['original']['path'] for row in files}) == len(files),
            'native_archive_duplicate_or_empty_custody')
    for row in files:
        require(all(row['original'][key] == row['copy'][key] for key in ('bytes', 'sha256')),
                'native_archive_bytes_changed')
        h.checked_bytes(row['copy'])
    last = archive['last_predecessor']
    require(all(last[key] == base['phases'][-1][key] for key in ('phase', 'output_root', 'process')),
            'native_archived_last_predecessor_changed')
    aliases = {h.encoded(row['original']): row['copy'] for row in files}
    archived_reader = lambda ref: reader(aliases.get(h.encoded(ref), ref))
    if base['schema'] == POLICY_SCHEMA:
        # The historical native suffix is validated before the new stack's
        # suffix. Its original bindings and configuration remain unchanged.
        native_lineage(base, base_registration, archived_reader)
    closed = closed_predecessor(last, archived_reader)
    required_custody = [last['launch'], *last['closure'].values()] + [closed[key] for key in
                        ('journal', 'never_claimed', 'owned_pause', 'pause_ownership')]
    require(all(h.encoded(ref) in aliases for ref in required_custody)
            and archived_reader(last['launch'])['process'] == last['process'],
            'native_archived_closure_custody_incomplete')
    if 'native_successions' in policy:
        require(policy['native_successions']['links'][0]['predecessor'] == last,
                'native_archived_first_link_changed')
    else:
        require(policy['predecessors'][len(base['phases']) - 1] == last,
                'native_archived_admission_predecessor_changed')
    archived_donors = archive['donor_histories']
    expected_archived_roots = ([row['output_root'] for row in base['phases']]
                               + [row['output_root'] for row in failed_histories])
    require(len(set(expected_archived_roots)) == len(expected_archived_roots)
            and [row['output_root'] for row in archived_donors] == expected_archived_roots
            and all(row['host'] == value['source_host'] and row['mode'] == 'sealed'
                    and row['recorder_relevant'] is True for row in archived_donors),
            'native_archived_donor_scope_changed')
    for row in archived_donors:
        require(row['journal']['path'] == row['output_root'] + '/.dispatcher/journal.jsonl'
                and row['journal'] in [item['original'] for item in files], 'native_archived_journal_missing')
    for history in failed_histories:
        matches = [row for row in archived_donors if row['output_root'] == history['output_root']]
        require(len(matches) == 1 and matches[0]['journal'] == history['journal'],
                'native_failed_qualification_donor_missing')
        raw = h.checked_bytes(aliases[h.encoded(history['journal'])])
        from causes.campaign_monitor import replay_history
        require(replay_history(raw) == history['journal_summary']
                and history['journal_summary']['unconsumed_tail_bytes'] == 0,
                'native_failed_qualification_replay_changed')
        rows = [json.loads(line) for line in raw.splitlines()]
        claims = [row for row in rows if row['event'] == 'claimed']
        finals = [row for row in rows if row['event'] == 'episode_finished']
        claim_hashes = {row['run_id']: row['job_sha256'] for row in claims}
        require(all(type(history[key]) is int and history[key] >= 0 for key in
                    ('issued', 'terminal', 'technical_failures', 'never_claimed'))
                and len(claim_hashes) == len(claims)
                and {row['run_id'] for row in claims} == {row['run_id'] for row in finals}
                and len(finals) == len(claims) == history['issued'] == history['terminal']
                and all(type(row.get('technical_failure')) is bool and row.get('replayed') is False for row in finals)
                and all(claim_hashes[row['run_id']] == row['job_sha256'] for row in finals)
                and sum(row['technical_failure'] for row in finals) == history['technical_failures']
                and rows[-1]['event'] in ('controller_paused', 'controller_finished')
                and rows[-1]['pending'] == history['never_claimed'],
                'native_failed_qualification_attempt_accounting_changed')
    donors = exclusion['donor_histories']
    require([row['output_root'] for row in donors] == [row['output_root'] for row in owners if row['kind'] == 'actor']
            and all(row['host'] == value['target_host'] and row['mode'] == 'sealed'
                    and row['recorder_relevant'] is True and row['journal'] == actor_journals[row['output_root']]
                    for row in donors), 'native_current_actor_history_missing')
    if 'native_successions' in policy:
        require(policy['phases'][:len(base['phases'])] == base['phases'], 'native_archived_phase_prefix_changed')
    else:
        require([{key: row[key] for key in ('output_root', 'process')} for row in policy['predecessors']]
                == [{key: row[key] for key in ('output_root', 'process')} for row in predecessor_monitor['phases']],
                'native_archived_predecessor_prefix_changed')
    transition = dict(value=value, base=base, archive=archive, stack=stack, exclusion=exclusion,
                      external_self=external)
    transition['fault_applicability'] = native_fault_applicability(transition, failed_histories, policy, reader)
    return transition


def native_custody_reader(transition, reader=h.checked):
    if transition is None:
        return reader
    aliases = {h.encoded(row['original']): row['copy'] for row in transition['archive']['files']}
    return lambda ref: reader(aliases.get(h.encoded(ref), ref))


def native_predecessor_host(policy, predecessor, transition=None):
    if transition is not None and any(all(predecessor[key] == phase[key] for key in ('output_root', 'process'))
                                      for phase in transition['base']['phases']):
        return transition['value']['source_host']
    return policy['controller_host']


def native_local_owners(policy, transition=None, reader=h.checked):
    transition = native_stack_transition(policy, reader) if transition is None else transition
    phases = policy['phases'][:-1] if 'phases' in policy else policy['predecessors']
    if transition is None:
        return phases
    count = len(transition['base']['phases'])
    return transition['exclusion']['owners'] + phases[count:]


def native_owner_locks(row):
    return row.get('lock_paths', [row['output_root'] + '/.dispatcher.lock'])


def recheck_native_owners(policy):
    transition = native_stack_transition(policy)
    for old in native_local_owners(policy, transition):
        if transition is None:
            require(h.checked(old['launch'])['process'] == old['process'], 'native_prior_launch_changed')
        if 'closure' in old and old.get('kind') not in ('actor', 'self'):
            live_closed_predecessor(old)
        else:
            state = h.retirement_state(old['process'])
            require((state['absent'] or state.get('state') in ('Z', 'X', 'x'))
                    and all(h.lock_free(Path(path)) for path in
                            (native_owner_locks(old) if transition else [old['output_root'] + '/.dispatcher.lock'])),
                    'native_old_owner_live')
    return transition


def native_transition_foundation(policy, foundation):
    transition = native_stack_transition(policy)
    if transition is not None:
        require(foundation['stack_transition'] == policy['stack_transition']
                and foundation['current_owners'] == transition['exclusion']['owners']
                and foundation['current_owners_absent_and_unlocked'] is True
                and foundation['archived_source_custody'] == transition['value']['archived_custody'],
                'native_transition_foundation_changed')
        if transition['external_self']['owners']:
            require(foundation['external_self_owners'] == transition['external_self']['owners']
                    and foundation['external_execution_exclusions_current'] is True,
                    'native_fresh_external_self_exclusion_required')
    return transition


def native_remote_transition(policy, registration, reader=h.checked):
    """Build only the owner/custody projection of the validated shared contract."""
    transition = native_stack_transition(policy, reader)
    if transition is None:
        return None
    require(registration.get('stack_transition') == policy['stack_transition']
            and registration['prior_local_owners'] == native_local_owners(policy, transition, reader),
            'native_remote_transition_registration_changed')
    return {'pair': policy['stack_transition'], 'source_host': transition['value']['source_host'],
            'archived_phases': transition['base']['phases'],
            'files': transition['archive']['files'],
            'archived_donors': transition['archive']['donor_histories'],
            'external_self_custody_pins': transition['external_self']['pins'],
            'fault_applicability': transition['fault_applicability']}


def native_lineage(policy, registration, reader=h.checked):
    """Validate an appended native lineage against its unchanged v1 base."""
    lineage = policy['native_successions']
    base = reader(lineage['base_policy'])
    base_registration = reader(lineage['base_registration'])
    review = reader(lineage['base_review'])
    require((base['schema'] == 'causes.campaign_monitor_policy.v1'
             or (policy.get('stack_transition') is not None and base['schema'] == POLICY_SCHEMA))
            and review.get('accepted_native_succession_base') is True
            and review.get('policy') == lineage['base_policy']
            and review.get('registration') == lineage['base_registration'],
            'native_base_not_accepted')
    count = lineage['base_phase_count']
    require(count == len(base['phases']) and policy['phases'][:count] == base['phases']
            and policy['sync_roots'][:count] == base['sync_roots']
            and base_registration['phase'] == base['phases'][-1]['phase'],
            'native_base_root_order_changed')
    transition = native_stack_transition(policy, reader)
    require(registration.get('stack_transition') == policy.get('stack_transition'), 'native_registration_transition_changed')
    binding = transition['stack']['bindings'] if transition else base
    current_config = transition['value']['current_config'] if transition else base['phases'][-1]['controller_config']
    custody_reader = native_custody_reader(transition, reader)
    for key in STACK_BINDING_KEYS:
        require(policy[key] == binding[key], 'native_base_binding_changed:' + key)
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
        closed_predecessor(link['predecessor'], custody_reader)
        allocation = accepted(link['allocation'], 'accepted_native_corpus_allocation',
                              'allocation', reader)
        native_allocation(allocation, reader, _control_reader=reader)
        if transition is not None:
            require(allocation['controller_host'] == policy['controller_host'], 'native_stack_allocation_host_changed')
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
                and allocation['config'] == phase['controller_config'] == current_config
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
            require((allocation['workload'] == 'mixed' and allocation['max_full'] > 0)
                    or (allocation['workload'] == 'prefix_only' and allocation['max_full'] == 0),
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
    expected_local = ([p['output_root'] for p in phases[:-1]] if transition is None else
                      [p['output_root'] for p in transition['exclusion']['donor_histories']] +
                      [p['output_root'] for p in phases[count:-1]])
    require([row['output_root'] for row in local] == expected_local
            and all(row['mode'] == 'sealed' and row['recorder_relevant'] is True for row in local),
            'native_closed_donor_scope_changed')
    if transition is not None:
        archived = [row for row in registration['donor_histories'] if row['host'] == transition['value']['source_host']]
        require(archived == transition['archive']['donor_histories'], 'native_archived_donors_changed')
        require([row for row in registration['donor_histories'] if row['host'] == policy['controller_host']][:len(transition['exclusion']['donor_histories'])]
                == transition['exclusion']['donor_histories'], 'native_current_donors_changed')
    require(registration['donor_histories'] == phases[-1]['donor_histories'],
            'native_donor_registration_changed')
    projection = [{key: row[key] for key in ('host', 'output_root', 'mode', 'recorder_relevant')}
                  for row in registration['donor_histories']]
    require(projection == scopes, 'native_donor_history_missing')
    for link in links:
        predecessor = link['predecessor']
        closure = closed_predecessor(predecessor, custody_reader)
        host = native_predecessor_host(policy, predecessor, transition)
        donors = [row for row in registration['donor_histories'] if row['host'] == host and row['output_root'] == predecessor['output_root']]
        require(len(donors) == 1 and donors[0]['journal'] == closure['journal'], 'native_closed_history_pin_changed')
    return links[-1]


class NativeAllocationCache:
    """Reuse one successful reconstruction, rechecking every input byte.

    Only the canonical file reader is supported. Alias readers retain the
    uncached interface. A failed recheck permanently invalidates this instance;
    it never adopts replacement files or a changed allocation.
    """

    def __init__(self):
        self._value = self._reader = self._result = None
        self._references = ()
        self._invalid = False

    def _verified_result(self, value, reader, *, dependency_sink=None):
        require(not self._invalid, 'native_allocation_cache_invalid')
        encoded = h.encoded(value)
        if self._result is not None:
            try:
                require(reader is h.checked, 'native_allocation_cache_reader_changed')
                require(encoded == self._value and reader is self._reader,
                        'native_allocation_cache_scope_changed')
                for ref in self._references:
                    h.checked_bytes(ref)
            except BaseException:
                self._invalid = True
                raise
            if dependency_sink is not None:
                for ref in self._references:
                    dependency_sink(copy.deepcopy(ref))
            return self._result, True

        require(reader is h.checked, 'native_allocation_cache_reader_changed')
        references = {}

        def read(ref):
            saved = copy.deepcopy(ref)
            previous = references.setdefault(saved['path'], saved)
            require(previous == saved, 'native_allocation_cache_conflicting_pin')
            return reader(saved)

        result = native_allocation(value, read, _control_reader=read)
        try:
            snapshot = copy.deepcopy(result)
            # Catch replacement during reconstruction before publishing a result.
            require(h.encoded(value) == encoded, 'native_allocation_cache_scope_changed')
            for ref in references.values():
                h.checked_bytes(ref)
        except BaseException:
            self._invalid = True
            raise
        self._value, self._reader = encoded, reader
        self._references, self._result = tuple(references.values()), snapshot
        if dependency_sink is not None:
            for ref in self._references:
                dependency_sink(copy.deepcopy(ref))
        return result, False

    def verify(self, value, reader, *, dependency_sink=None):
        result, cached = self._verified_result(value, reader, dependency_sink=dependency_sink)
        return copy.deepcopy(result) if cached else result

    def components(self, value, reader, *, dependency_sink=None):
        """Return independent queue inputs and binding, without allocator state."""
        result, _ = self._verified_result(value, reader, dependency_sink=dependency_sink)
        jobs, config, extensions, allocator = result
        return copy.deepcopy((jobs, config, extensions, allocator.binding))

    def validate(self, value, reader, *, dependency_sink=None):
        """Recheck the full proof without exposing or copying its result."""
        self._verified_result(value, reader, dependency_sink=dependency_sink)
        return True


def native_allocation_components(value, reader=h.checked, *, cache=None, dependency_sink=None):
    """Verify allocation and return queue inputs plus the allocator binding."""
    if cache is not None:
        require(isinstance(cache, NativeAllocationCache), 'native_allocation_cache_scope_changed')
        return cache.components(value, reader, dependency_sink=dependency_sink)
    require(dependency_sink is None, 'native_allocation_dependency_cache_required')
    jobs, config, extensions, allocator = native_allocation(value, reader)
    return jobs, config, extensions, copy.deepcopy(allocator.binding)


def validate_native_allocation(value, reader=h.checked, *, cache=None, dependency_sink=None):
    """Verify allocation without returning its mutable reconstruction."""
    if cache is not None:
        require(isinstance(cache, NativeAllocationCache), 'native_allocation_cache_scope_changed')
        return cache.validate(value, reader, dependency_sink=dependency_sink)
    require(dependency_sink is None, 'native_allocation_dependency_cache_required')
    native_allocation(value, reader)
    return True


def native_allocation(value, reader=h.checked, *, cache=None, _control_reader=None, dependency_sink=None):
    """Verify a finite selected queue or an unmodified continuous allocator."""
    if cache is not None:
        require(_control_reader is None and isinstance(cache, NativeAllocationCache),
                'native_allocation_cache_scope_changed')
        return cache.verify(value, reader, dependency_sink=dependency_sink)
    require(dependency_sink is None, 'native_allocation_dependency_cache_required')
    from causes.corpus_continuation import CorpusContinuation
    require(value['schema'] == 'causes.native_corpus_allocation.v1'
            and value['origin'] == 'native_successor'
            and value['no_fixed_study_pooling'] is True, 'native_allocation_origin_changed')
    require(type(value['workers']) is int and value['workers'] > 0
            and type(value['max_full']) is int and 0 <= value['max_full'] <= value['workers'],
            'native_allocation_capacity_changed')
    control = reader(value['allocation_control'])
    prototypes, config = reader(control['prototypes']), reader(control['config'])
    allocator = CorpusContinuation.from_control(control, prototypes, config,
                                                reader=_control_reader)
    jobs, extensions = reader(value['jobs']), reader(value['extensions'])
    require(control['family'] == value['family'] and control['config'] == value['config']
            and control['model_key'] == value['model_key']
            and control['task_host'] == control['provider_host'] == value['controller_host'],
            'native_allocation_route_changed')
    full = control_full_selection(control)
    require(value['workload'] == ('mixed' if full else 'prefix_only')
            and bool(full) == (value['max_full'] > 0), 'native_allocation_workload_changed')
    if value['target_mode'] == 'finite_qualification':
        require(value['control'] is None and extensions == []
                and value['selection']['block_index'] == 0, 'native_finite_scope_changed')
        block = allocator.block(0)
        by_id = {job['allocation']['run_id']: job for job in block['jobs']}
        selected = value['selection']['run_ids']
        require(isinstance(selected, list) and selected and len(set(selected)) == len(selected)
                and all(rid in by_id for rid in selected)
                and h.encoded(jobs) == h.encoded([by_id[rid] for rid in selected]),
                'native_finite_selection_changed')
        modes = [job['allocation']['continue_to_end'] for job in jobs]
        require(False in modes and ((True in modes) == bool(full)),
                'native_finite_exposure_scope_empty')
    else:
        require(value['target_mode'] == 'continuous_corpus' and jobs == []
                and value['selection'] is None and value['control'] == value['allocation_control']
                and value['extensions'] == control['prototypes'], 'native_continuous_scope_changed')
    return jobs, config, extensions, allocator
