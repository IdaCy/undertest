import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import sys

from causes import campaign_handoff as h
from causes import campaign_storage as storage
from causes import campaign_storage_observation as observer


def inline_review(row):
    raw = base64.b64decode(row['base64'], validate=True)
    storage.require(len(raw) == row['pin']['bytes'] and hashlib.sha256(raw).hexdigest() == row['pin']['sha256'],
                    'inline_local_review_bytes_changed')
    return json.loads(raw)


def validate_hold(intent_ref, intent, held, stopped, marker, guard_result, trial, owner):
    binding = trial['capacity_guard']
    storage.require(intent['schema'] == 'causes.capacity_guard_hold.v1'
                    and intent['guard'] == guard_result['process']
                    and intent['trial_id'] == binding['trial_id']
                    and intent['output_root'] == binding['output_root'] == owner['output_root']
                    and intent['automatic_resume'] is False
                    and intent['reasons'] == ['candidate_first_admission_not_observed'],
                    'exact_startup_deadline_hold_required')
    storage.require(held['created'] is True and held['intent'] == intent_ref
                    and held['marker']['path'] == binding['pause_file'] == owner['controller_fields']['pause_file']
                    and marker == {'intent': intent_ref}, 'owned_guard_pause_binding_changed')
    storage.require(stopped['admission_held'] is True and stopped['pending_endpoint'] is None
                    and stopped['automatic_retry'] is False, 'guard_stop_incomplete')


def main():
    plan = json.loads(sys.stdin.buffer.read())
    storage.require(plan['schema'] == 'causes.capacity_startup_hold_retirement_capture.v1', 'capture_schema')
    for ref in plan['sources']:
        h.checked_bytes(ref)
    registration = h.checked(plan['registration'])
    reviewed = h.checked(plan['registration_review'])
    storage.require(registration['phase'] == 'mi_waiting'
                    and reviewed['accepted_storage_phase_registration'] is True
                    and reviewed['accepted_actual_waiting_owner'] is True
                    and reviewed['registration'] == plan['registration'], 'accepted_waiting_owner_required')
    pointer = h.checked(plan['published_identity'])
    storage.require(pointer['registration'] == plan['registration']
                    and pointer['independent_review'] == plan['registration_review'], 'capture_phase_changed')
    guard_result = h.checked(plan['guard_result'])
    guard_review = inline_review(plan['guard_review'])
    storage.require(guard_review['accepted_actual_capacity_guard'] is True
                    and guard_review['result'] == plan['guard_result'] and guard_result['passed'] is True
                    and guard_result['barrier_release'] is False, 'accepted_guard_start_required')
    trial = h.checked(guard_result['trial_plan'])
    owner = copy.deepcopy(registration['owners']['new_mi'])
    storage.require(owner['process'] == guard_result['candidate'] == trial['owner_process']
                    and owner['owned_barrier'] == guard_result['owned_barrier'], 'capture_owner_changed')
    output = h.canonical_path(trial['output'])
    storage.require(plan['hold_records'] == {name: str(output / (name + '.json'))
                    for name in ('HOLD_INTENT', 'HOLD_RESULT', 'STOPPED')}, 'hold_record_scope_changed')
    started = h.now()
    payloads = {}
    def capture(name, path):
        reference = h.pin(path)
        raw = h.checked_bytes(reference)
        payload = {'role': name, 'source': reference, 'stat': h.stat_identity(path),
                   'base64': base64.b64encode(raw).decode()}
        payloads[name] = payload
        print(json.dumps({'kind': 'payload', **payload}), flush=True)
        return reference, json.loads(raw)
    intent_ref, intent = capture('HOLD_INTENT', plan['hold_records']['HOLD_INTENT'])
    _, held = capture('HOLD_RESULT', plan['hold_records']['HOLD_RESULT'])
    _, stopped = capture('STOPPED', plan['hold_records']['STOPPED'])
    storage.require(held['created'] is True and held['marker']['path'] == trial['capacity_guard']['pause_file']
                    == owner['controller_fields']['pause_file'], 'bound_owned_marker_required')
    marker_ref, marker = capture('PAUSE', held['marker']['path'])
    storage.require(marker_ref == held['marker'] and h.stat_identity(marker_ref['path']) == held['stat'],
                    'guard_owned_marker_changed')
    validate_hold(intent_ref, intent, held, stopped, marker, guard_result, trial, owner)
    forbidden = {name: os.path.lexists(output / (name + '.json')) for name in
                 ('FIRST_ADMISSION', 'WINDOW', 'FAILED')}
    storage.require(not any(forbidden.values()), 'startup_hold_scope_has_admission_or_failure')
    barrier = h.verify_owned_barrier(owner['owned_barrier'], owner['barrier_path'])
    guard_before = h.retirement_state(guard_result['process'])
    health_lock = h.canonical_path(trial['capacity_guard']['health_lock_path'])
    lease_pointer = h.checked(h.pin(trial['capacity_guard']['lease_path']))
    lease_path = h.canonical_path(lease_pointer['record']['path'])
    storage.require(lease_path.parent == health_lock.parent and lease_path.name.startswith('lease_')
                    and lease_path.name.endswith('.json') and lease_path.stem[6:].isdigit(),
                    'final_lease_scope_changed')
    lease_ref, lease = capture('FINAL_LEASE', lease_pointer['record']['path'])
    storage.require(lease_ref == lease_pointer['record'] and lease['state'] == 'held'
                    and lease['guard'] == guard_result['process'] and lease['candidate'] == owner['process']
                    and lease['plan'] == guard_result['trial_plan'] and lease['binding'] == trial['capacity_guard']
                    and lease['reason'] == intent['reasons'], 'final_guard_lease_changed')
    storage.require((guard_before['absent'] or guard_before.get('state') in ('Z', 'X', 'x'))
                    and h.lock_free(health_lock) and h.stat_identity(health_lock) == lease['health_lock'],
                    'guard_not_retired')
    texas_before = observer._ownership(registration['owners']['new_texas'])
    def run_directories():
        root = h.canonical_path(owner['output_root'])
        return sorted(child.name for child in root.iterdir()
                      if child.name != '.dispatcher' and child.is_dir())
    directories_before = run_directories()
    print(json.dumps({'kind': 'inventory', 'at': h.now(), 'position': 'before',
                      'output_root': owner['output_root'], 'run_directories': directories_before}), flush=True)
    storage.require(directories_before == [], 'zero_admission_run_directory_present')
    owner['observation_state'] = 'retired'
    observed = observer.observe_owner(owner)
    journal_ref = observed['metadata_pins']['journal']
    journal_raw = h.checked_bytes(journal_ref)
    print(json.dumps({'kind': 'payload', 'role': 'FULL_FINAL_JOURNAL', 'source': journal_ref,
                      'base64': base64.b64encode(journal_raw).decode()}), flush=True)
    storage.require(journal_raw.endswith(b'\n') and observed['claims'] == observed['active_claims'] == 0
                    and observed['drain_progress']['pause_acknowledged'] is True
                    and observed['drain_progress']['new_claims_after_pause'] == 0,
                    'zero_admission_complete_retirement_required')
    observed.pop('journal')
    owner['journal_prefix'] = journal_ref
    candidate_after = observer._ownership(owner)
    directories_after = run_directories()
    print(json.dumps({'kind': 'inventory', 'at': h.now(), 'position': 'after',
                      'output_root': owner['output_root'], 'run_directories': directories_after}), flush=True)
    storage.require(directories_after == directories_before == [], 'zero_admission_run_directories_changed')
    texas_after = observer._ownership(registration['owners']['new_texas'])
    guard_after = h.retirement_state(guard_result['process'])
    storage.require((guard_after['absent'] or guard_after.get('state') in ('Z', 'X', 'x'))
                    and h.lock_free(health_lock) and h.stat_identity(health_lock) == lease['health_lock'],
                    'guard_retirement_changed')
    h.verify_owned_barrier(owner['owned_barrier'], owner['barrier_path'])
    for payload in payloads.values():
        h.checked_bytes(payload['source'])
        storage.require(h.stat_identity(payload['source']['path']) == payload['stat'], 'hold_payload_changed')
    h.checked_bytes(journal_ref)
    h.checked_bytes(plan['published_identity'])
    for ref in plan['sources']:
        h.checked_bytes(ref)
    result = {'schema': 'causes.capacity_startup_hold_retirement_result.v1', 'started_at': started,
              'finished_at': h.now(), 'plan': plan, 'owner': owner, 'observation': observed,
              'guard_before': guard_before, 'guard_after': guard_after, 'health_lock': lease['health_lock'],
              'health_lock_free': True, 'candidate_after': candidate_after,
              'texas_before': texas_before, 'texas_after': texas_after,
              'owned_marker': {'pin': marker_ref, 'stat': held['stat'], 'intent': intent_ref},
              'owned_barrier': owner['owned_barrier'], 'barrier_verified': True,
              'hold_payloads': {key: value['source'] for key, value in payloads.items()},
              'full_final_journal': journal_ref, 'forbidden_records_present': forbidden,
              'run_directories_before': directories_before, 'run_directories_after': directories_after,
              'passed': True, 'runtime_actions': False, 'process_signals': False,
              'automatic_retry': False, 'retired_phase_publication_pending': True}
    print(json.dumps({'kind': 'result', 'value': result}), flush=True)


if __name__ == '__main__':
    main()
