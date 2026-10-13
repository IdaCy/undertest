"""Validate an explicit actor-history baseline change at a closed handoff.

This pure validator grants no execution authority. Its caller must bind the
transition to the closed predecessor, independently accepted journal custody,
the original actor identity, and the reviewed native replay implementation.
The existing fresh-history admission check remains required after validation.
"""

import copy
import hashlib
import json


SCHEMA = 'causes.shared_actor_history_successor.v1'
STATE_FIELDS = ('lifetime_route_failures', 'nonzero_failures', 'quarantines',
                'unresolved_events', 'unconsumed_tail_bytes')
FIELDS = {'schema', 'donor_id', 'previous_policy', 'successor_policy',
          'previous_current_journal', 'successor_current_journal',
          'required_pin_suffix'}


class InvalidSuccessor(ValueError):
    pass


def require(value, reason):
    if not value:
        raise InvalidSuccessor(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode()


def same(left, right):
    return encoded(left) == encoded(right)


def checked(reference, read_bytes):
    require(type(reference) is dict and set(reference) == {'path', 'bytes', 'sha256'}
            and type(reference['path']) is str and reference['path'].startswith('/')
            and type(reference['bytes']) is int and reference['bytes'] >= 0
            and type(reference['sha256']) is str, 'invalid_reference')
    raw = read_bytes(reference)
    require(type(raw) is bytes and len(raw) == reference['bytes']
            and hashlib.sha256(raw).hexdigest() == reference['sha256'],
            'reference_content_changed')
    return raw


def native_state(value):
    require(type(value) is dict and set(STATE_FIELDS) <= set(value),
            'incomplete_native_state')
    state = {key: value[key] for key in STATE_FIELDS}
    require(type(state['lifetime_route_failures']) is int
            and state['lifetime_route_failures'] >= 0, 'invalid_lifetime_failures')
    require(type(state['nonzero_failures']) is dict
            and type(state['quarantines']) is dict, 'invalid_route_state')
    for key in ('unresolved_events', 'unconsumed_tail_bytes'):
        require(type(state[key]) is int and state[key] >= 0, 'invalid_native_count')
    require(not state['nonzero_failures'] and not state['quarantines']
            and state['unresolved_events'] == state['unconsumed_tail_bytes'] == 0,
            'history_not_resolved')
    return state


def validate_successor(transition, *, read_bytes, replay):
    """Return verified states without modifying either policy or any journal.

    ``read_bytes`` supplies named immutable artifacts. ``replay`` is the caller's
    already qualified native history replay, not a count reconstructed here.
    Both current-journal artifacts must have separately established actor/cut
    custody. Only the named live donor may advance; sealed donors and all
    scientific, capacity, identity, and failure-admission settings stay exact.
    """
    require(type(transition) is dict and set(transition) == FIELDS
            and transition['schema'] == SCHEMA
            and type(transition['donor_id']) is str and transition['donor_id'],
            'transition_schema')
    old = json.loads(checked(transition['previous_policy'], read_bytes))
    new = json.loads(checked(transition['successor_policy'], read_bytes))
    require(type(old) is dict and type(new) is dict, 'policy_object_required')
    require(type(old.get('donors')) is list and old['donors']
            and type(new.get('donors')) is list, 'donor_roster_required')
    ids = [row['id'] for row in old['donors']]
    require(all(type(value) is str and value for value in ids)
            and len(ids) == len(set(ids))
            and ids == [row['id'] for row in new['donors']], 'donor_roster_changed')
    require(ids.count(transition['donor_id']) == 1, 'unknown_actor_donor')
    index = ids.index(transition['donor_id'])
    before, after = old['donors'][index], new['donors'][index]
    require(before['mode'] == after['mode'] == 'live_prefix'
            and 'history_progression' not in before
            and 'history_progression' not in after, 'strict_live_donor_required')
    require(type(old.get('initial_failures')) is int and old['initial_failures'] == 0,
            'initial_failures_not_zero')
    suffix = transition['required_pin_suffix']
    old_pins = old.get('required_pins')
    require(type(old_pins) is list and type(suffix) is list and suffix
            and same(new.get('required_pins'), old_pins + suffix),
            'required_pins_not_append_only')
    paths = [reference['path'] for reference in old_pins + suffix]
    require(len(paths) == len(set(paths)), 'duplicate_required_pin_path')
    require(after['baseline'] in suffix
            and transition['successor_current_journal'] in suffix,
            'successor_history_not_required')
    for reference in suffix:
        checked(reference, read_bytes)
    restored = copy.deepcopy(new)
    restored['required_pins'] = copy.deepcopy(old_pins)
    restored['donors'][index]['baseline'] = copy.deepcopy(before['baseline'])
    restored['donors'][index]['accepted_state'] = copy.deepcopy(before['accepted_state'])
    require(same(restored, old), 'unrelated_policy_change')

    baseline = checked(before['baseline'], read_bytes)
    successor = checked(after['baseline'], read_bytes)
    previous_current = checked(transition['previous_current_journal'], read_bytes)
    current = checked(transition['successor_current_journal'], read_bytes)
    require(all(raw and raw.endswith(b'\n') for raw in
                (baseline, successor, previous_current, current)),
            'incomplete_journal')
    require(baseline.endswith(previous_current)
            and current.startswith(previous_current), 'actor_journal_prefix_changed')
    extension = current[len(previous_current):]
    require(extension and successor == baseline + extension, 'full_baseline_not_exact_extension')
    before_state = native_state(replay(baseline))
    after_state = native_state(replay(successor))
    require(same(before_state, before['accepted_state'])
            and same(after_state, after['accepted_state']), 'native_state_not_bound')
    require(after_state['lifetime_route_failures'] >= before_state['lifetime_route_failures'],
            'lifetime_failures_decreased')
    return dict(schema='causes.verified_shared_actor_history_successor.v1',
                previous_policy=transition['previous_policy'],
                successor_policy=transition['successor_policy'],
                donor_id=transition['donor_id'],
                previous_baseline=before['baseline'], successor_baseline=after['baseline'],
                previous_current_journal=transition['previous_current_journal'],
                successor_current_journal=transition['successor_current_journal'],
                before_state=before_state, after_state=after_state,
                appended_journal_bytes=len(extension), native_replays=2,
                original_policy_preserved=True, history_progression_enabled=False,
                runtime_authorized=False)
