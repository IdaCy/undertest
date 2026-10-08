"""Fresh local observations for an explicitly registered storage succession."""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

from causes import campaign_handoff as h
from causes.campaign_dispatcher import _SUCCESSION_PREDECESSOR
from causes.campaign_storage import TERMINAL_EVENTS, drain_progress, observe_guard
from causes.campaign_storage import require, validate_mixed_phase


_SOURCE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _ownership(expected):
    process = expected['process']
    lock = h.canonical_path(expected['output_root']) / '.dispatcher.lock'
    require(h.stat_identity(lock) == expected['output_lock'],
            'storage_output_lock_identity_changed')
    if expected['observation_state'] == 'retired':
        value = h.retirement_state(process)
        require((value['absent'] or value.get('state') in ('Z', 'X', 'x'))
                and h.lock_free(lock), 'storage_previous_owner_not_retired')
        return value
    require(h.lock_owned_by(lock, process['pid']), 'storage_output_lock_owner_changed')
    return h.process_identity(process)


def _owner_claims(events, controller_id):
    claims, terminals = {}, set()
    for event in events:
        if event.get('controller_id') != controller_id:
            continue
        if event['event'] == 'claimed':
            rid = event['run_id']
            require(rid not in claims and rid not in terminals,
                    'storage_observation_repeated_or_post_terminal_claim')
            claims[rid] = event
        elif event['event'] in TERMINAL_EVENTS:
            rid = event['run_id']
            require(rid not in terminals, 'storage_observation_repeated_terminal')
            terminals.add(rid)
    return claims, terminals


def validate_owner_identity(expected, queue, controller, start, owner_events):
    """Recognize only an explicitly bound, previously authorized code succession."""
    current = {key: controller.get(key) for key in queue}
    if current == queue:
        require('code_succession' not in expected, 'storage_code_succession_not_applicable')
        require(all(start.get(key) == value for key, value in queue.items()),
                'storage_owner_start_identity_changed')
        return {}
    require(expected['observation_state'] in ('retired', 'draining')
            and 'code_succession' in expected, 'storage_owner_queue_controller_mismatch')
    reference = expected['code_succession']
    receipt = h.checked(reference)
    root = h.canonical_path(expected['output_root'])
    old_code, new_code = queue.get('execution_code', {}), current.get('execution_code', {})
    require(receipt.get('schema') == 'caus.dispatcher_code_succession.v1'
            and receipt.get('output_root') == str(root)
            and receipt.get('predecessor_queue_identity') == expected['queue_identity_pin']
            and receipt.get('successor_execution_code') == new_code
            and old_code.get('dispatcher_sha256') == _SUCCESSION_PREDECESSOR
            and {k: v for k, v in old_code.items() if k != 'dispatcher_sha256'} ==
                {k: v for k, v in new_code.items() if k != 'dispatcher_sha256'}
            and {k: v for k, v in queue.items() if k != 'execution_code'} ==
                {k: v for k, v in current.items() if k != 'execution_code'}
            and all(start.get(key) == value for key, value in current.items()),
            'storage_owner_code_succession_changed')
    review = h.checked(receipt['independent_review'])
    h.checked_bytes(receipt['admission_holds'])
    require(review.get('accepted_source_and_hold_membership') is True
            and review.get('holds') == receipt['admission_holds']
            and review['dispatcher']['sha256'] == new_code['dispatcher_sha256'],
            'storage_owner_code_succession_review_changed')
    saved = root / '.dispatcher/code_successions' / (reference['sha256'] + '.json')
    saved_pin = h.pin(saved)
    require(h.checked(saved_pin) == receipt, 'storage_saved_code_succession_changed')
    bound = [event for event in owner_events if event['event'] == 'admission_holds_bound']
    require(len(bound) == 1 and bound[0].get('code_succession') == reference
            and bound[0].get('admission_holds') == receipt['admission_holds']
            and bound[0].get('predecessor_queue_identity_retained') is True,
            'storage_owner_succession_event_changed')
    return {'code_succession': reference, 'saved_code_succession': saved_pin,
            'admission_holds': receipt['admission_holds'],
            'code_succession_review': receipt['independent_review']}


def owned_retirement_progress(expected, events, prefix_event_count, claims, terminals):
    """Certify an idle owned drain without reinterpreting earlier pause history."""
    require(expected['observation_state'] == 'retired', 'owned_drain_requires_retired_owner')
    proof = expected['owned_retirement']
    preflight = h.checked(proof['preflight'])
    previous = preflight['owners'][proof['role']]
    require(previous['binding'] == {k: v for k, v in expected.items() if k != 'owned_retirement'}
            and previous['active_claims'] == 0 and previous['children'] == []
            and previous['claims'] == len(claims), 'owned_drain_preflight_changed')
    marker = h.checked(proof['owned_marker'])
    value = h.checked(marker['pin'])
    require(marker['origin'] == value['origin'] == 'storage_transition'
            and marker['transition_id'] == value['transition_id']
            and marker['pin']['path'] == expected['controller_fields']['pause_file']
            and h.checked_bytes(marker['pin']) == h.checked_bytes(marker['archived_marker'])
            and h.stat_identity(marker['pin']['path']) == marker['stat'], 'owned_drain_marker_changed')
    authority = h.checked(value['authority'])
    review = h.checked(authority['independent_review'])
    require(authority['root_authorized'] is True and authority['no_user_revocation'] is True
            and authority['action'] == 'storage_owned_idle_drain' and authority['plan'] == value['plan']
            and authority['root_adoption'] == value['root_adoption']
            and review['accepted_staging_and_drain_preparation'] is True
            and h.checked(review['preparation'])['plan'] == value['plan'],
            'owned_drain_authority_changed')
    issued, expires, marked = (dt.datetime.fromisoformat(value) for value in
        (authority['issued_at'], authority['expires_at'], value['at']))
    require(all(value.tzinfo is not None for value in (issued, expires, marked))
            and 0 < (expires - issued).total_seconds() <= 180 and issued <= marked < expires,
            'owned_drain_historical_authority_not_fresh')
    suffix = events[prefix_event_count:]
    require(suffix and all(row.get('controller_id') == expected['controller_id']
                           and row['event'] in ('pause_seen', 'controller_paused') for row in suffix)
            and sum(row['event'] == 'controller_paused' for row in suffix) == 1
            and suffix[-1]['event'] == 'controller_paused'
            and all(row.get('active') == 0 and row.get('admission_stopped') is True
                    for row in suffix if row['event'] == 'pause_seen')
            and not set(claims) - terminals, 'owned_drain_suffix_not_idle_retirement')
    times = [dt.datetime.fromisoformat(preflight['at']), dt.datetime.fromisoformat(value['at']),
             dt.datetime.fromisoformat(suffix[-1]['at'])]
    require(all(at.tzinfo is not None for at in times) and times == sorted(times),
            'owned_drain_timestamp_order_changed')
    return {'claims': len(claims), 'active_claims': 0, 'pause_acknowledged': True,
            'acknowledgement_event': 'controller_paused', 'new_claims_after_pause': 0,
            'scope': 'exact_owned_idle_drain_after_pinned_preflight',
            'historical_pause_rows_retained': True, 'proof': proof}


def observe_owner(expected, *, max_journal_bytes=256 * 1024**2):
    """Read actual process, lock and metadata state; do not infer retirement."""
    require(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == _SOURCE_SHA,
            'loaded_storage_observation_source_changed')
    state = expected['observation_state']
    require(state in ('retired', 'draining', 'waiting', 'running'),
            'explicit_storage_observation_state_required')
    process = expected['process']
    root = h.canonical_path(expected['output_root'])
    started = h.now()
    before = _ownership(expected)
    references = {key: expected[key] for key in
                  ('queue_identity_pin', 'controller_identity_pin')}
    require(references['queue_identity_pin']['path'] == str(root / '.dispatcher/queue_identity.json')
            and references['controller_identity_pin']['path'] ==
            str(root / '.dispatcher/controllers' / expected['controller_id'] / 'identity.json'),
            'storage_owner_metadata_scope_changed')
    queue = h.checked(references['queue_identity_pin'])
    require(queue == expected['queue_identity'], 'storage_owner_queue_changed')
    controller = h.checked(references['controller_identity_pin'])
    require(controller == expected['controller_fields'], 'storage_owner_controller_fields_changed')
    journal_path = h.canonical_path(root / '.dispatcher/journal.jsonl')
    require(type(max_journal_bytes) is int and max_journal_bytes > 0,
            'bounded_storage_journal_required')
    require(journal_path.stat().st_size <= max_journal_bytes, 'storage_journal_too_large')
    with journal_path.open('rb') as stream:
        raw = stream.read(max_journal_bytes + 1)
    require(raw and len(raw) <= max_journal_bytes and raw.endswith(b'\n'),
            'storage_journal_missing_torn_or_too_large')
    prefix = expected['journal_prefix']
    require(prefix['path'] == str(journal_path) and type(prefix['bytes']) is int
            and 0 < prefix['bytes'] <= len(raw)
            and raw[:prefix['bytes']].endswith(b'\n')
            and hashlib.sha256(raw[:prefix['bytes']]).hexdigest() == prefix['sha256'],
            'storage_owner_journal_prefix_changed')
    events = [json.loads(line) for line in raw.splitlines()]
    owner_events = [event for event in events
                    if event.get('controller_id') == expected['controller_id']]
    starts = [event for event in owner_events if event['event'] == 'controller_started']
    require(len(starts) == 1, 'storage_owner_start_identity_changed')
    succession = validate_owner_identity(expected, queue, controller, starts[0], owner_events)
    claims, terminals = _owner_claims(events, expected['controller_id'])
    active = len(set(claims) - terminals)
    result = {'process': process, 'output_root': str(root), 'queue_identity': queue,
              'live': state != 'retired', 'lock_owner': process['pid'] if state != 'retired' else None,
              'claims': len(claims), 'active_claims': active, 'journal': owner_events,
              'observed_from': started, 'process_before': before,
              'metadata_pins': {**references, **succession, 'journal': h.pin(journal_path, raw)}}
    if state in ('retired', 'draining'):
        progress = (owned_retirement_progress(expected, events,
                        len(raw[:prefix['bytes']].splitlines()), claims, terminals)
                    if 'owned_retirement' in expected else drain_progress(events, expected['controller_id']))
        require(progress['active_claims'] == active, 'storage_owner_activity_changed')
        if state == 'retired':
            require(active == 0, 'retired_storage_owner_has_unresolved_claim')
        result['drain_progress'] = progress
    else:
        barrier = h.barrier_path(expected['barrier_path'])
        require(controller['start_barrier_file'] == str(barrier),
                'storage_owner_barrier_path_changed')
        result['barrier_present'] = os.path.lexists(barrier)
        if state == 'waiting':
            h.verify_owned_barrier(expected['owned_barrier'], barrier)
            children = (Path('/proc') / str(process['pid']) / 'task' /
                        str(process['pid']) / 'children').read_text().split()
            require(not children and not claims and not terminals,
                    'waiting_storage_owner_issued_work')
            allowed = {'controller_started', 'admission_holds_bound', 'admission_barrier_waiting'}
            require(all(event['event'] in allowed for event in owner_events),
                    'waiting_storage_owner_faulted_or_issued_work')
            require(any(event['event'] == 'admission_barrier_waiting'
                        and event['active'] == 0
                        and event['pending'] == expected['initial_pending'] for event in owner_events),
                    'waiting_storage_barrier_not_observed')
            require(not [path for path in root.iterdir()
                         if path.is_dir() and path.name != '.dispatcher'],
                    'waiting_storage_owner_created_attempt')
        else:
            require(not result['barrier_present'], 'running_storage_owner_still_barred')
        blocks = list((root / '.dispatcher/blocks').glob('*'))
        result['persisted_blocks'] = len(blocks)
        require(state != 'waiting' or not blocks, 'waiting_storage_owner_created_block')
        guard = expected['guard']
        require(guard['binding']['output'] == str(root), 'storage_guard_owner_output_changed')
        result['guard'] = observe_guard(guard['directory'], guard['binding'], process,
                                        guard['reporting_lock'])
        require(result['guard']['healthy'], 'storage_guard_observation_unhealthy')
    result['process_after'] = _ownership(expected)
    for reference in references.values():
        h.checked_bytes(reference)
    result['observed_until'] = h.now()
    require(0 <= (dt.datetime.fromisoformat(result['observed_until']) -
             dt.datetime.fromisoformat(started)).total_seconds() <= 180,
            'storage_owner_observation_too_slow')
    return result


def observe_phase(registration):
    """Validate a mixed phase using freshly collected local observations only."""
    started = h.now()
    observations = {role: observe_owner(owner) for role, owner in registration['owners'].items()}
    decision = validate_mixed_phase(registration, observations)
    final_ownership = {role: _ownership(owner) for role, owner in registration['owners'].items()}
    # Keep exact journal pins in the report; journal bodies remain at their originals.
    for value in observations.values():
        value.pop('journal')
    finished = h.now()
    require(0 <= (dt.datetime.fromisoformat(finished) -
             dt.datetime.fromisoformat(started)).total_seconds() <= 180,
            'storage_phase_observation_too_slow')
    return {'schema': 'causes.storage_phase_observation.v1',
            'observed_from': started, 'observed_until': finished,
            'source_sha256': _SOURCE_SHA, 'decision': decision,
            'observations': observations, 'final_ownership': final_ownership,
            'runtime_actions': False}
