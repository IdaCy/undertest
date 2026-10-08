"""Publish one reviewed storage phase while preserving the preceding pointer."""

import argparse
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


def publish(plan_reference, review_reference, authority_reference):
    plan, review = h.checked(plan_reference), h.checked(review_reference)
    storage.require(plan['schema'] == 'causes.storage_phase_publication_plan.v1'
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
    storage.require(phase in _PREVIOUS_PHASE, 'phase_publication_phase_unknown')
    pointer = h.canonical_path(manifest['pointer'])
    directory = h.canonical_path(plan['receipt_directory'])
    storage.require(directory.is_relative_to(h.canonical_path(manifest['campaign_root']))
                    and directory != pointer.parent and not directory.exists(),
                    'new_phase_publication_receipt_directory_required')
    previous = plan['previous']
    previous_bytes(pointer, previous)
    if previous is None:
        storage.require(_PREVIOUS_PHASE[phase] is None, 'phase_publication_requires_predecessor')
    else:
        old_pointer, old_registration = identity.current_registration(auth, manifest)
        storage.require(old_registration['phase'] == _PREVIOUS_PHASE[phase],
                        'phase_publication_order_changed')
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
        routing = h.checked(plan['pause_routing'])
        marker = h.checked(plan['owned_drain'])
        storage.require(h.stat_identity(marker['pin']['path']) == marker['stat']
                        and h.checked_bytes(marker['pin']) == h.checked_bytes(marker['archived_marker']),
                        'phase_publication_owned_drain_changed')
        present = {p: os.path.lexists(p) for p in routing['user_pause_fanout']}
        for role in ('mi', 'texas'):
            storage.validate_pause_routing(routing, marker, present, admitting_role=role)
        identity.validate_authorization_value(auth)
        h.checked_bytes(plan_reference)
        return True

    routing = h.checked(plan['pause_routing'])
    pauses = tuple(h.canonical_path(p) for p in
                   (routing['successor_global_pause'], *routing['successor_local_pauses'].values()))
    authority = h.Authority(authority_reference, ACTION, pauses, validate)
    authority.check()
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
