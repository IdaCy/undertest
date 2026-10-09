"""Publish one reviewed storage phase while preserving the preceding pointer."""

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys

from causes import campaign_handoff as h
from causes import campaign_storage as storage
from causes import campaign_storage_identity as identity
from causes import campaign_storage_observation as observation


ACTION = 'publish_storage_identity_phase'
_SOURCE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_PREVIOUS_PHASE = {
    'texas_waiting_mi_retired': None,
    'texas_running_mi_retired': 'texas_waiting_mi_retired',
    'mi_waiting': 'texas_running_mi_retired',
    'successors_running': 'mi_waiting',
}


def previous_bytes(pointer, expected):
    pointer = h.canonical_path(pointer)
    if expected is None:
        storage.require(not os.path.lexists(pointer), 'initial_phase_pointer_already_exists')
        return None
    storage.require(expected['pin']['path'] == str(pointer)
                    and h.stat_identity(pointer) == expected['stat'],
                    'previous_phase_pointer_identity_changed')
    raw = h.checked_bytes(expected['pin'])
    storage.require(h.stat_identity(pointer) == expected['stat'],
                    'previous_phase_pointer_changed_during_read')
    return raw


def write_bytes(path, raw):
    path = h.canonical_path(path)
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    h.sync_directory(path.parent)
    return h.pin(path, raw)


def atomic_pointer(pointer, value, expected, directory, before_commit):
    """Caller holds the dedicated publication lock; no automatic retry."""
    pointer, directory = h.canonical_path(pointer), h.canonical_path(directory)
    prior = previous_bytes(pointer, expected)
    archived = None if prior is None else write_bytes(directory / 'PREVIOUS.pointer.json', prior)
    candidate = h.save_new(directory, 'NEXT.pointer.json', value)
    storage.require(directory.stat().st_dev == pointer.parent.stat().st_dev,
                    'phase_publication_requires_same_filesystem')
    before_commit()
    previous_bytes(pointer, expected)
    if expected is None:
        os.link(candidate['path'], pointer, follow_symlinks=False)
    else:
        temporary = pointer.parent / (pointer.name + '.' + directory.name + '.candidate')
        os.link(candidate['path'], temporary, follow_symlinks=False)
        # Retain the complete candidate in the receipt directory after replacement.
        before_commit()
        previous_bytes(pointer, expected)
        os.replace(temporary, pointer)
    h.sync_directory(pointer.parent)
    storage.require(pointer.read_bytes() == h.checked_bytes(candidate),
                    'published_phase_pointer_changed')
    return {'pointer': h.pin(pointer), 'stat': h.stat_identity(pointer),
            'candidate': candidate, 'preserved_previous': archived}


def validate_candidate_owners(registration, manifest):
    for role, owner in registration['owners'].items():
        storage.require(owner['output_root'] == manifest['outputs'][role],
                        'phase_publication_owner_output_changed')
        if role.startswith('new_'):
            receipt = h.checked(owner['launch_owner_receipt'])
            storage.require(receipt['process'] == owner['process']
                            and receipt['controller_id'] == owner['controller_id']
                            and receipt['binding']['output'] == owner['output_root']
                            and receipt['binding']['storage_binding'] == owner['guard']['binding']
                            and receipt['guard_directory'] == owner['guard']['directory']
                            and receipt['guard_reporting_lock'] == owner['guard']['reporting_lock'],
                            'phase_publication_owner_receipt_changed')


def validate_phase_order(phase, previous_phase, *, interim=False):
    if interim:
        allowed = {('texas_running_mi_draining', None),
                   ('texas_running_mi_retired', None),
                   ('texas_running_mi_retired', 'texas_running_mi_draining')}
        storage.require((phase, previous_phase) in allowed,
                        'interim_phase_publication_order_changed')
    else:
        storage.require(phase in _PREVIOUS_PHASE, 'phase_publication_phase_unknown')
        storage.require(_PREVIOUS_PHASE[phase] == previous_phase,
                        'phase_publication_order_changed')


def validate_interim_drain(plan, registration, routing):
    """Recognize only the reviewed marker of this still-draining predecessor."""
    storage.require(registration['phase'] in ('texas_running_mi_draining',
                                              'texas_running_mi_retired')
                    and set(registration['owners']) == {'old_mi', 'old_texas', 'new_texas'},
                    'interim_phase_owner_scope_changed')
    pair = plan['interim_drain']
    result, review = h.checked(pair['evidence']), h.checked(pair['review'])
    storage.require(review.get('accepted_mi_natural_drain_actual') is True
                    and review.get('result') == pair['evidence']
                    and result['schema'] == 'causes.mi_natural_drain_started.v1'
                    and result['drain_requested'] is True
                    and result['signal_sent'] is False and result['successor_started'] is False,
                    'interim_drain_not_independently_accepted')
    owner, texas = registration['owners']['old_mi'], registration['owners']['new_texas']
    storage.require(owner['observation_state'] == ('draining' if registration['phase']
                    == 'texas_running_mi_draining' else 'retired')
                    and registration['owners']['old_texas']['observation_state'] == 'retired'
                    and texas['observation_state'] == 'running', 'interim_owner_state_changed')
    owned = result['owned_marker']
    marker = h.checked(owned['pin'])
    drain_plan = h.checked(result['plan'])
    storage.require(owned['origin'] == marker['origin'] == 'capacity_transition'
                    and owned['transition_id'] == marker['transition_id']
                    and owned['owner_process'] == marker['owner_process'] == owner['process']
                    and owned['controller_id'] == marker['controller_id'] == owner['controller_id']
                    and marker['plan'] == result['plan']
                    and marker['authority'] == result['authority']
                    and marker['preflight'] == result['preflight']
                    and drain_plan['pause_path'] == owned['pin']['path']
                    and drain_plan['existing_owned_drain'] == plan['owned_drain'],
                    'interim_drain_owner_or_marker_changed')
    storage.require(owner['guard']['binding']['admission_control']['pause_routing']
                    == plan['pause_routing']
                    and owned['pin']['path'] == owner['controller_fields']['pause_file']
                    == routing['successor_local_pauses']['mi']
                    and texas['controller_fields']['pause_file']
                    == routing['successor_local_pauses']['texas']
                    and owner['controller_fields']['global_pause_file']
                    == texas['controller_fields']['global_pause_file']
                    == routing['successor_global_pause'], 'interim_drain_routing_changed')
    storage.require(h.stat_identity(owned['pin']['path']) == owned['stat']
                    and h.checked_bytes(owned['pin']) == h.checked_bytes(owned['archived_marker']),
                    'interim_drain_marker_identity_changed')


def validate_capacity_return(plan, registration, auth, manifest):
    """Remap only a reviewed, unstarted capacity candidate after natural retirement."""
    transition = plan['capacity_return']
    old_auth = h.checked(transition['predecessor_authorization'])
    old_manifest = identity.validate_authorization_value(old_auth)
    old_pointer, old_registration = identity.current_registration(old_auth, old_manifest)
    storage.require(plan['previous'] is not None
                    and old_registration['phase'] == 'mi_waiting'
                    and registration['phase'] == 'texas_running_mi_retired'
                    and set(old_registration['owners']) == {'old_mi', 'old_texas', 'new_mi', 'new_texas'}
                    and set(registration['owners']) == {'old_mi', 'old_texas', 'new_texas'},
                    'capacity_return_phase_scope_changed')
    expected_manifest = copy.deepcopy(old_manifest)
    expected_manifest['outputs']['old_mi'] = old_manifest['outputs']['new_mi']
    expected_manifest['outputs']['new_mi'] = manifest['outputs']['new_mi']
    storage.require(manifest == expected_manifest
                    and manifest['outputs']['new_mi'] not in old_manifest['outputs'].values()
                    and registration['identity_manifest'] == auth['manifest'],
                    'capacity_return_manifest_scope_changed')
    pair = transition['retirement']
    result, review = h.checked(pair['evidence']), h.checked(pair['review'])
    storage.require(review.get('accepted_capacity_startup_hold_retirement') is True
                    and review.get('result') == pair['evidence']
                    and result['schema'] == 'causes.capacity_startup_hold_retirement_result.v1'
                    and result['passed'] is True and result['runtime_actions'] is False
                    and result['process_signals'] is False and result['automatic_retry'] is False
                    and result['plan']['registration'] == old_pointer['registration']
                    and result['plan']['registration_review'] == old_pointer['independent_review']
                    and result['plan']['published_identity'] == plan['previous']['pin'],
                    'capacity_return_retirement_not_accepted')
    expected_owner = copy.deepcopy(old_registration['owners']['new_mi'])
    storage.require(expected_owner['observation_state'] == 'waiting',
                    'capacity_return_predecessor_not_waiting')
    expected_owner.update(observation_state='retired', journal_prefix=result['full_final_journal'])
    storage.require(registration['owners']['old_mi'] == result['owner'] == expected_owner
                    and all(registration['owners'][role] == old_registration['owners'][role]
                            for role in ('old_texas', 'new_texas')),
                    'capacity_return_owner_mapping_changed')
    observed = result['observation']
    storage.require(result['run_directories_before'] == result['run_directories_after'] == []
                    and type(observed['claims']) is type(observed['active_claims']) is int
                    and observed['claims'] == observed['active_claims'] == 0
                    and observed['live'] is False and observed['lock_owner'] is None
                    and observed['process'] == expected_owner['process']
                    and observed['output_root'] == expected_owner['output_root']
                    and observed['drain_progress']['pause_acknowledged'] is True
                    and type(observed['drain_progress']['new_claims_after_pause']) is int
                    and observed['drain_progress']['new_claims_after_pause'] == 0
                    and set(result['forbidden_records_present']) == {'FIRST_ADMISSION', 'WINDOW', 'FAILED'}
                    and all(value is False for value in result['forbidden_records_present'].values())
                    and result['barrier_verified'] is True and result['health_lock_free'] is True,
                    'capacity_return_requires_zero_admission_retirement')
    owned = result['owned_marker']
    intent = h.checked(owned['intent'])
    guard = h.checked(result['plan']['guard_result'])
    trial = h.checked(guard['trial_plan'])
    storage.require(intent['schema'] == 'causes.capacity_guard_hold.v1'
                    and intent['guard'] == guard['process']
                    and guard['passed'] is True and guard['barrier_release'] is False
                    and guard['candidate'] == expected_owner['process']
                    == trial['owner_process']
                    and intent['trial_id'] == trial['capacity_guard']['trial_id']
                    and intent['output_root'] == expected_owner['output_root']
                    and intent['reasons'] == ['candidate_first_admission_not_observed']
                    and intent['automatic_resume'] is False
                    and owned['pin']['path'] == expected_owner['controller_fields']['pause_file']
                    and h.checked(owned['pin']) == {'intent': owned['intent']}
                    and h.stat_identity(owned['pin']['path']) == owned['stat'],
                    'capacity_return_owned_hold_changed')
    h.checked_bytes(result['full_final_journal'])
    storage.require(result['owned_barrier'] == expected_owner['owned_barrier'],
                    'capacity_return_owned_barrier_changed')
    h.verify_owned_barrier(result['owned_barrier'], expected_owner['barrier_path'])
    routing = h.checked(plan['pause_routing'])
    retained = storage.retained_owned_pauses(routing)
    storage.require(owned['pin']['path'] in retained
                    and routing['predecessor_routing'] == expected_owner['guard']['binding']
                        ['admission_control']['pause_routing'],
                    'capacity_return_pause_ancestry_changed')
    if 'mi_succession_ancestry' in old_registration:
        validate_mi_ancestry(plan, registration, old_pointer, old_registration,
                             transition.get('ancestry'))
    return old_pointer, old_registration


def validate_mi_ancestry(plan, registration, old_pointer, old_registration, reference):
    """Append the exact displaced retiree and preserve every earlier ancestry pin."""
    storage.require(type(reference) is dict, 'natural_mi_succession_ancestry_required')
    ancestry = h.checked(reference)
    storage.require(ancestry == {
        'schema': 'causes.storage_mi_succession_ancestry.v1',
        'predecessor_pointer': plan['previous']['pin'],
        'predecessor_registration': old_pointer['registration'],
        'predecessor_review': old_pointer['independent_review'],
        'predecessor_adoption': old_pointer['root_adoption'],
        'retired_owner': old_registration['owners']['old_mi'],
        'previous': old_registration.get('mi_succession_ancestry')}
        and registration.get('mi_succession_ancestry') == reference
        and ancestry['retired_owner']['observation_state'] == 'retired',
        'natural_mi_succession_ancestry_changed')
    # The displaced retiree's full journal stays immutable; earlier registrations,
    # owners and routing proofs remain reachable through the preserved exact pins.
    retired_prefix = ancestry['retired_owner']['journal_prefix']
    storage.require(retired_prefix['path'] == str(h.canonical_path(
                    ancestry['retired_owner']['output_root']) / '.dispatcher/journal.jsonl'),
                    'natural_mi_succession_ancestry_journal_changed')
    h.checked_bytes(retired_prefix)
    if ancestry['previous'] is not None:
        h.checked_bytes(ancestry['previous'])


def validate_natural_mi_succession(plan, registration, auth, manifest):
    """Remap an actually draining or retired MI owner without discarding its predecessors."""
    transition = plan['natural_mi_succession']
    old_auth = h.checked(transition['predecessor_authorization'])
    old_manifest = identity.validate_authorization_value(old_auth)
    old_pointer, old_registration = identity.current_registration(old_auth, old_manifest)
    state = {'texas_running_mi_draining': 'draining',
             'texas_running_mi_retired': 'retired'}.get(registration['phase'])
    storage.require(plan['previous'] is not None and state is not None
                    and old_registration['phase'] == 'successors_running'
                    and set(old_registration['owners']) == {'old_mi', 'old_texas', 'new_mi', 'new_texas'}
                    and set(registration['owners']) == {'old_mi', 'old_texas', 'new_texas'},
                    'natural_mi_succession_phase_scope_changed')
    expected_manifest = copy.deepcopy(old_manifest)
    expected_manifest['outputs']['old_mi'] = old_manifest['outputs']['new_mi']
    expected_manifest['outputs']['new_mi'] = manifest['outputs']['new_mi']
    storage.require(manifest == expected_manifest
                    and manifest['outputs']['new_mi'] not in old_manifest['outputs'].values()
                    and registration['identity_manifest'] == auth['manifest'],
                    'natural_mi_succession_manifest_scope_changed')
    validate_mi_ancestry(plan, registration, old_pointer, old_registration,
                         transition.get('ancestry'))
    previous_owner = old_registration['owners']['new_mi']
    storage.require(previous_owner['observation_state'] == 'running'
                    and plan['pause_routing'] == previous_owner['guard']['binding']
                        ['admission_control']['pause_routing']
                    and all(registration['owners'][role] == old_registration['owners'][role]
                            for role in ('old_texas', 'new_texas')),
                    'natural_mi_succession_predecessor_or_routing_changed')
    validate_interim_drain(plan, registration, h.checked(plan['pause_routing']))
    drain = h.checked(plan['interim_drain']['evidence'])
    drain_plan, preflight = h.checked(drain['plan']), h.checked(drain['preflight'])
    storage.require(drain_plan['registration'] == old_pointer['registration']
                    and drain_plan['current_pointer'] == plan['previous']['pin']
                    and preflight['plan'] == drain['plan']
                    and preflight['mi_observation']['process'] == previous_owner['process']
                    and preflight['mi_observation']['output_root'] == previous_owner['output_root'],
                    'natural_mi_succession_drain_predecessor_changed')
    boundary = preflight['journal_boundary']['pin']
    raw = h.checked_bytes(preflight['journal_copy'])
    prior = previous_owner['journal_prefix']
    storage.require(boundary['path'] == prior['path'] == str(h.canonical_path(
                    previous_owner['output_root']) / '.dispatcher/journal.jsonl')
                    and type(prior['bytes']) is int and type(boundary['bytes']) is int
                    and 0 < prior['bytes'] <= boundary['bytes'] == len(raw)
                    and raw.endswith(b'\n') and raw[:prior['bytes']].endswith(b'\n')
                    and hashlib.sha256(raw).hexdigest() == boundary['sha256']
                    and hashlib.sha256(raw[:prior['bytes']]).hexdigest() == prior['sha256'],
                    'natural_mi_succession_journal_prefix_changed')
    expected_owner = copy.deepcopy(previous_owner)
    expected_owner.update(observation_state=state, journal_prefix=boundary)
    storage.require(('retirement' in transition) == (state == 'retired'),
                    'natural_mi_succession_retirement_scope_changed')
    if state == 'retired':
        pair = transition['retirement']
        result, review = h.checked(pair['evidence']), h.checked(pair['review'])
        storage.require(review.get('accepted_mi_retirement_metadata') is True
                        and review.get('result') == pair['evidence']
                        and review.get('journal') == result['full_final_journal']
                        and result['schema'] == 'causes.mi_retirement_metadata_result.v1'
                        and result['passed'] is True and result['runtime_actions'] is False
                        and result['process_signals'] is False and result['automatic_retry'] is False
                        and result['plan']['registration'] == old_pointer['registration']
                        and result['plan']['registration_review'] == old_pointer['independent_review']
                        and result['plan']['published_identity'] == plan['previous']['pin']
                        and result['plan']['begin_drain'] == plan['interim_drain']
                        and result['owned_marker'] == drain['owned_marker'],
                        'natural_mi_succession_retirement_not_accepted')
        final = result['full_final_journal']
        final_raw = h.checked_bytes(final)
        storage.require(final['path'] == boundary['path'] and final_raw.endswith(b'\n')
                        and final_raw[:len(raw)] == raw,
                        'natural_mi_succession_final_history_changed')
        expected_owner['journal_prefix'] = final
        observed = result['observation']
        storage.require(result['owner'] == expected_owner
                        and observed['process'] == previous_owner['process']
                        and observed['output_root'] == previous_owner['output_root']
                        and observed['metadata_pins']['journal'] == final
                        and observed['live'] is False and observed['lock_owner'] is None
                        and type(observed['claims']) is type(observed['active_claims']) is int
                        and observed['claims'] >= 0 and observed['active_claims'] == 0
                        and observed['drain_progress']['pause_acknowledged'] is True
                        and observed['drain_progress']['new_claims_after_pause'] == 0,
                        'natural_mi_succession_owner_not_retired')
    storage.require(registration['owners']['old_mi'] == expected_owner,
                    'natural_mi_succession_owner_mapping_changed')
    return old_pointer, old_registration


def validate_post_admission_return(plan, registration, auth, manifest):
    """Remap a released candidate only after an accepted guard-held retirement."""
    transition = plan['post_admission_return']
    old_auth = h.checked(transition['predecessor_authorization'])
    old_manifest = identity.validate_authorization_value(old_auth)
    old_pointer, old_registration = identity.current_registration(old_auth, old_manifest)
    expected_state = {'mi_waiting': 'waiting', 'successors_running': 'running'}.get(
        old_registration['phase'])
    storage.require(plan['previous'] is not None and expected_state is not None
                    and registration['phase'] == 'texas_running_mi_retired'
                    and set(old_registration['owners']) == {'old_mi', 'old_texas', 'new_mi', 'new_texas'}
                    and set(registration['owners']) == {'old_mi', 'old_texas', 'new_texas'},
                    'post_admission_return_phase_scope_changed')
    expected_manifest = copy.deepcopy(old_manifest)
    expected_manifest['outputs']['old_mi'] = old_manifest['outputs']['new_mi']
    expected_manifest['outputs']['new_mi'] = manifest['outputs']['new_mi']
    storage.require(manifest == expected_manifest
                    and manifest['outputs']['new_mi'] not in old_manifest['outputs'].values()
                    and registration['identity_manifest'] == auth['manifest'],
                    'post_admission_return_manifest_scope_changed')
    validate_mi_ancestry(plan, registration, old_pointer, old_registration,
                         transition.get('ancestry'))
    previous_owner = old_registration['owners']['new_mi']
    storage.require(previous_owner['observation_state'] == expected_state,
                    'post_admission_return_predecessor_state_changed')
    pair = transition['retirement']
    result, review = h.checked(pair['evidence']), h.checked(pair['review'])
    storage.require(review.get('accepted_mi_post_admission_hold_retirement') is True
                    and review.get('result') == pair['evidence']
                    and review.get('journal') == result['full_final_journal']
                    and review.get('owned_marker') == result['owned_marker']
                    and result['schema'] == 'causes.capacity_post_admission_hold_retirement_result.v1'
                    and result['passed'] is True and result['runtime_actions'] is False
                    and result['process_signals'] is False and result['automatic_retry'] is False
                    and result['plan']['registration'] == old_pointer['registration']
                    and result['plan']['registration_review'] == old_pointer['independent_review']
                    and result['plan']['published_identity'] == plan['previous']['pin'],
                    'post_admission_return_retirement_not_accepted')
    final = result['full_final_journal']
    raw, prior = h.checked_bytes(final), previous_owner['journal_prefix']
    storage.require(final['path'] == prior['path'] == str(h.canonical_path(
                    previous_owner['output_root']) / '.dispatcher/journal.jsonl')
                    and type(prior['bytes']) is int and 0 < prior['bytes'] <= len(raw)
                    and raw.endswith(b'\n') and raw[:prior['bytes']].endswith(b'\n')
                    and hashlib.sha256(raw[:prior['bytes']]).hexdigest() == prior['sha256'],
                    'post_admission_return_final_history_changed')
    expected_owner = copy.deepcopy(previous_owner)
    expected_owner.update(observation_state='retired', journal_prefix=final)
    storage.require(registration['owners']['old_mi'] == result['owner'] == expected_owner
                    and all(registration['owners'][role] == old_registration['owners'][role]
                            for role in ('old_texas', 'new_texas')),
                    'post_admission_return_owner_mapping_changed')
    observed = result['observation']
    events = [json.loads(line) for line in raw.splitlines()]
    claims, terminals = observation._owner_claims(events, expected_owner['controller_id'])
    storage.require(type(observed['claims']) is type(observed['active_claims']) is int
                    and observed['claims'] == len(claims) > 0 and observed['active_claims'] == 0
                    and set(claims) == terminals
                    and observed['live'] is False and observed['lock_owner'] is None
                    and observed['process'] == expected_owner['process']
                    and observed['output_root'] == expected_owner['output_root']
                    and observed['metadata_pins']['journal'] == final
                    and observed['drain_progress']['pause_acknowledged'] is True
                    and type(observed['drain_progress']['new_claims_after_pause']) is int
                    and observed['drain_progress']['new_claims_after_pause'] == 0
                    and result['run_directories_before'] == result['run_directories_after']
                    and type(result['run_directories_before']) is list
                    and len(set(result['run_directories_before'])) == len(result['run_directories_before'])
                    and set(result['run_directories_before']) <= set(claims)
                    and result['claims_without_run_directory'] ==
                        sorted(set(claims) - set(result['run_directories_before'])),
                    'post_admission_return_requires_complete_positive_retirement')
    guard = h.checked(result['plan']['guard_result'])
    trial = h.checked(guard['trial_plan'])
    binding = trial['capacity_guard']
    owned = result['owned_marker']
    intent = h.checked(owned['intent'])
    hold = h.checked(result['hold_payloads']['HOLD_RESULT'])
    storage.require(guard['passed'] is True and guard['barrier_release'] is False
                    and guard['candidate'] == trial['owner_process'] == expected_owner['process']
                    and guard['owned_barrier'] == result['owned_barrier'] == expected_owner['owned_barrier']
                    and intent['schema'] == 'causes.capacity_guard_hold.v1'
                    and intent['guard'] == guard['process']
                    and intent['trial_id'] == binding['trial_id']
                    and intent['output_root'] == binding['output_root'] == expected_owner['output_root']
                    and type(intent['reasons']) is list and intent['reasons']
                    and all(type(reason) is str and reason for reason in intent['reasons'])
                    and intent['automatic_resume'] is False
                    and hold['created'] is True and hold['intent'] == owned['intent']
                    and hold['marker'] == owned['pin'] and hold['stat'] == owned['stat']
                    and owned['pin']['path'] == binding['pause_file']
                        == expected_owner['controller_fields']['pause_file']
                    and h.checked(owned['pin']) == {'intent': owned['intent']}
                    and h.stat_identity(owned['pin']['path']) == owned['stat'],
                    'post_admission_return_owned_hold_changed')
    release = h.checked(result['release_result'])
    released = h.checked(result['barrier_release'])
    storage.require(result['release_result'] == result['plan']['release_result']
                    and release['passed'] is True and release['automatic_retry'] is False
                    and release['release'] == result['barrier_release']
                    and released['released_owned_barrier'] == expected_owner['owned_barrier']
                    and released['barrier_path'] == expected_owner['barrier_path']
                    and released['no_pause_marker_removed'] is True
                    and result['barrier_absent'] is True
                    and not os.path.lexists(expected_owner['barrier_path']),
                    'post_admission_return_release_changed')
    presence = result['optional_records_present']
    storage.require(set(presence) == {'STOPPED', 'FAILED', 'FIRST_ADMISSION', 'WINDOW'}
                    and all(type(value) is bool for value in presence.values())
                    and (presence['STOPPED'] or presence['FAILED'])
                    and set(result['hold_payloads']) == {'HOLD_INTENT', 'HOLD_RESULT', 'PAUSE', 'FINAL_LEASE'}
                        | {name for name, present in presence.items() if present}
                    and result['hold_payloads']['HOLD_INTENT'] == owned['intent']
                    and result['hold_payloads']['PAUSE'] == owned['pin'],
                    'post_admission_return_terminal_evidence_changed')
    for reference in result['hold_payloads'].values():
        h.checked_bytes(reference)
    lease_ref = result['hold_payloads']['FINAL_LEASE']
    lease = h.checked(lease_ref)
    health_lock = h.canonical_path(binding['health_lock_path'])
    lease_pointer = h.checked(h.pin(binding['lease_path']))
    storage.require(lease_pointer['record'] == lease_ref
                    and lease['state'] == 'held' and lease['guard'] == guard['process']
                    and lease['candidate'] == expected_owner['process']
                    and lease['plan'] == guard['trial_plan'] and lease['binding'] == binding
                    and lease['reason'] == intent['reasons']
                    and result['health_lock_free'] is True
                    and result['health_lock'] == lease['health_lock'] == h.stat_identity(health_lock)
                    and h.lock_free(health_lock), 'post_admission_return_guard_lease_changed')
    guard_state = h.retirement_state(guard['process'])
    storage.require(guard_state['absent'] or guard_state.get('state') in ('Z', 'X', 'x'),
                    'post_admission_return_guard_not_retired')
    routing = h.checked(plan['pause_routing'])
    retained = storage.retained_owned_pauses(routing)
    storage.require(owned['pin']['path'] in retained
                    and routing['predecessor_routing'] == previous_owner['guard']['binding']
                        ['admission_control']['pause_routing'],
                    'post_admission_return_pause_ancestry_changed')
    preserved = h.checked(routing['retained_owned_pauses'][-1]['evidence'])
    storage.require(preserved['marker'] == owned['pin'] and preserved['stat'] == owned['stat'],
                    'post_admission_return_retained_marker_changed')
    return old_pointer, old_registration


def publication_controls(plan, registration):
    """Keep live global/Texas controls effective during a reviewed MI drain."""
    schema = plan['schema']
    storage.require(schema in ('causes.storage_phase_publication_plan.v1',
                              'causes.storage_phase_publication_plan.v2',
                              'causes.storage_phase_publication_plan.v3',
                              'causes.storage_phase_publication_plan.v4',
                              'causes.storage_phase_publication_plan.v5'),
                    'phase_publication_plan_schema')
    interim = schema in ('causes.storage_phase_publication_plan.v2',
                         'causes.storage_phase_publication_plan.v4')
    storage.require(('interim_drain' in plan) == interim,
                    'explicit_interim_publication_scope_required')
    storage.require(('capacity_return' in plan) ==
                    (schema == 'causes.storage_phase_publication_plan.v3'),
                    'explicit_capacity_return_scope_required')
    storage.require(('natural_mi_succession' in plan) ==
                    (schema == 'causes.storage_phase_publication_plan.v4'),
                    'explicit_natural_mi_succession_scope_required')
    storage.require(('post_admission_return' in plan) ==
                    (schema == 'causes.storage_phase_publication_plan.v5'),
                    'explicit_post_admission_return_scope_required')
    routing, marker = h.checked(plan['pause_routing']), h.checked(plan['owned_drain'])
    storage.require(h.stat_identity(marker['pin']['path']) == marker['stat']
                    and h.checked_bytes(marker['pin']) == h.checked_bytes(marker['archived_marker']),
                    'phase_publication_owned_drain_changed')
    if interim:
        validate_interim_drain(plan, registration, routing)
    present = {p: os.path.lexists(p) for p in routing['user_pause_fanout']}
    roles = ('texas',) if interim else ('mi', 'texas')
    for role in roles:
        storage.validate_pause_routing(routing, marker, present, admitting_role=role)
    return tuple(h.canonical_path(p) for p in
                 (routing['successor_global_pause'],
                  *(routing['successor_local_pauses'][role] for role in roles)))


def ensure_pointer_parent(plan, pointer, campaign_root):
    """Create only an explicitly authorized interim metadata directory."""
    if pointer.parent.is_dir():
        return
    storage.require(plan['schema'] == 'causes.storage_phase_publication_plan.v2'
                    and plan.get('create_pointer_parent') is True
                    and pointer.parent.parent == h.canonical_path(campaign_root) / 'controls'
                    and pointer.parent.parent.is_dir(),
                    'phase_pointer_parent_creation_not_authorized')
    pointer.parent.mkdir(mode=0o700)
    h.sync_directory(pointer.parent.parent)


def publish(plan_reference, review_reference, authority_reference):
    plan, review = h.checked(plan_reference), h.checked(review_reference)
    storage.require(plan['schema'] in ('causes.storage_phase_publication_plan.v1',
                                      'causes.storage_phase_publication_plan.v2',
                                      'causes.storage_phase_publication_plan.v3',
                                      'causes.storage_phase_publication_plan.v4',
                                      'causes.storage_phase_publication_plan.v5')
                    and review.get('accepted_storage_phase_publication_plan') is True
                    and review.get('plan') == plan_reference,
                    'unaccepted_phase_publication_plan')
    storage.require(plan['publisher'] == h.pin(Path(__file__).resolve())
                    and plan['publisher']['sha256'] == _SOURCE_SHA,
                    'phase_publisher_source_changed')
    auth = h.checked(plan['identity_authorization'])
    manifest = identity.validate_authorization_value(auth)
    storage.require(str(Path(sys.executable).resolve()) == manifest['python']['path'],
                    'phase_publisher_interpreter_changed')
    for name, module in tuple(sys.modules.items()):
        if (name == 'causes' or name.startswith('causes.')) and module is not sys.modules[__name__]:
            reference = manifest['source_inventory'].get(Path(module.__file__).name)
            storage.require(reference == h.pin(Path(module.__file__).resolve()),
                            'phase_publisher_import_outside_bundle')
    registration = h.checked(plan['registration']['evidence'])
    phase_review = h.checked(plan['registration']['review'])
    adoption = h.checked(plan['adoption'])
    storage.require(phase_review.get('accepted_storage_phase_registration') is True
                    and phase_review.get('registration') == plan['registration']['evidence']
                    and adoption.get('root_accepted') is True
                    and adoption.get('registration') == plan['registration']['evidence']
                    and adoption.get('independent_review') == plan['registration']['review']
                    and adoption.get('manifest') == auth['manifest']
                    and registration['identity_manifest'] == auth['manifest'],
                    'phase_publication_registration_not_adopted')
    validate_candidate_owners(registration, manifest)
    phase = registration['phase']
    interim = plan['schema'] == 'causes.storage_phase_publication_plan.v2'
    capacity_return = plan['schema'] == 'causes.storage_phase_publication_plan.v3'
    natural_succession = plan['schema'] == 'causes.storage_phase_publication_plan.v4'
    post_admission_return = plan['schema'] == 'causes.storage_phase_publication_plan.v5'
    pointer = h.canonical_path(manifest['pointer'])
    directory = h.canonical_path(plan['receipt_directory'])
    storage.require(directory.is_relative_to(h.canonical_path(manifest['campaign_root']))
                    and directory != pointer.parent and not directory.exists(),
                    'new_phase_publication_receipt_directory_required')
    previous = plan['previous']
    previous_bytes(pointer, previous)
    if post_admission_return:
        validate_post_admission_return(plan, registration, auth, manifest)
    elif natural_succession:
        validate_natural_mi_succession(plan, registration, auth, manifest)
    elif capacity_return:
        validate_capacity_return(plan, registration, auth, manifest)
    elif previous is None:
        validate_phase_order(phase, None, interim=interim)
    else:
        old_pointer, old_registration = identity.current_registration(auth, manifest)
        validate_phase_order(phase, old_registration['phase'], interim=interim)
        if 'mi_succession_ancestry' in old_registration:
            storage.require(registration.get('mi_succession_ancestry') ==
                            old_registration['mi_succession_ancestry'],
                            'phase_publication_mi_ancestry_changed')
        for role, old_owner in old_registration['owners'].items():
            storage.require(role in registration['owners']
                            and registration['owners'][role]['process'] == old_owner['process']
                            and registration['owners'][role]['output_root'] == old_owner['output_root'],
                            'phase_publication_owner_replaced')
    value = {'schema': 'causes.storage_identity_pointer.v1', 'manifest': auth['manifest'],
             'registration': plan['registration']['evidence'],
             'independent_review': plan['registration']['review'], 'root_adoption': plan['adoption']}

    def validate(authority):
        storage.require(authority.get('plan') == plan_reference
                        and authority.get('independent_review') == review_reference
                        and authority.get('root_adoption') == plan['root_adoption'],
                        'phase_publication_authority_changed')
        h.checked_bytes(plan['root_adoption'])
        publication_controls(plan, registration)
        identity.validate_authorization_value(auth)
        if capacity_return:
            validate_capacity_return(plan, registration, auth, manifest)
        if natural_succession:
            validate_natural_mi_succession(plan, registration, auth, manifest)
        if post_admission_return:
            validate_post_admission_return(plan, registration, auth, manifest)
        h.checked_bytes(plan_reference)
        return True

    pauses = publication_controls(plan, registration)
    authority = h.Authority(authority_reference, ACTION, pauses, validate)
    authority.check()
    ensure_pointer_parent(plan, pointer, manifest['campaign_root'])
    lock_path = pointer.with_name(pointer.name + '.publish.lock')
    h.canonical_path(lock_path)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous_bytes(pointer, previous)
        h.new_record_directory(directory)
        try:
            observed = observation.observe_phase(registration)
            h.save_new(directory, 'INTENT.json', {'at': h.now(), 'plan': plan_reference,
                'review': review_reference, 'authority': authority_reference,
                'previous': previous, 'fresh_observation': observed, 'value': value})
            committed = atomic_pointer(pointer, value, previous, directory, authority.check)
            storage.require(identity.current_registration(auth, manifest)[0] == value,
                            'phase_publication_postcheck_changed')
            return h.save_new(directory, 'RESULT.json', {'at': h.now(), **committed,
                'phase': phase, 'passed': True, 'actor_admission': False, 'automatic_retry': False})
        except BaseException as error:
            h.save_new(directory, 'FAILED.json', {'at': h.now(), 'error': repr(error),
                'pointer_may_have_changed': True, 'automatic_retry': False,
                'manual_reconciliation_required': True})
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('plan', 'review', 'authority'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    print(json.dumps(publish(*(json.loads(getattr(args, key)) for key in ('plan', 'review', 'authority')))))


if __name__ == '__main__':
    main()
