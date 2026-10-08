"""Isolated bridge from an existing observer to reviewed storage owner phases."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


_SOURCE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_REQUIRED_SOURCES = {'__init__.py', 'campaign_storage_identity.py',
                     'campaign_storage_observation.py', 'campaign_storage.py',
                     'campaign_dispatcher.py', 'campaign_handoff.py'}


def require(value, message):
    if not value:
        raise ValueError(message)


def canonical(value):
    path = Path(value)
    require(path.is_absolute() and path.resolve() == path
            and not any(p.is_symlink() for p in (path, *path.parents)),
            'noncanonical_storage_identity_path')
    return path


def pinned(reference):
    path = canonical(reference['path'])
    raw = path.read_bytes()
    require(type(reference['bytes']) is int and len(raw) == reference['bytes']
            and hashlib.sha256(raw).hexdigest() == reference['sha256'],
            'storage_identity_pin_changed')
    return raw


def checked(reference):
    return json.loads(pinned(reference))


def validate_authorization_value(auth):
    require(auth['schema'] == 'causes.storage_identity_authorization.v1',
            'storage_identity_authorization_schema')
    require(auth['overlay_module']['path'] == str(Path(__file__).resolve())
            and auth['overlay_module']['sha256'] == _SOURCE_SHA
            and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == _SOURCE_SHA,
            'loaded_storage_identity_source_changed')
    pinned(auth['overlay_module'])
    manifest = checked(auth['manifest'])
    review = checked(auth['manifest_review'])
    require(manifest['schema'] == 'causes.storage_identity_manifest.v1'
            and review.get('accepted_storage_identity_manifest') is True
            and review.get('manifest') == auth['manifest'],
            'unaccepted_storage_identity_manifest')
    base = canonical(manifest['campaign_root'])
    root = canonical(manifest['pythonpath'])
    require(root.is_relative_to(base) and canonical(manifest['pointer']).is_relative_to(base),
            'storage_identity_paths_escape_campaign')
    require(_REQUIRED_SOURCES <= set(manifest['source_inventory'])
            and {'old_mi', 'old_texas', 'new_mi', 'new_texas'} <= set(manifest['outputs'])
            and len(set(manifest['outputs'].values())) == len(manifest['outputs']),
            'storage_identity_inventory_required')
    for name, reference in manifest['source_inventory'].items():
        require(Path(name).name == name
                and reference['path'] == str(root / 'causes' / name),
                'storage_identity_source_inventory_scope')
        pinned(reference)
    require(manifest['source_inventory']['campaign_storage_identity.py'] == auth['overlay_module'],
            'storage_identity_bundle_module_changed')
    for output in manifest['outputs'].values():
        require(canonical(output).is_relative_to(base / 'runs'),
                'storage_identity_output_escape')
    pinned(manifest['python'])
    return manifest


def validate_authorization(path):
    auth = json.loads(canonical(path).read_bytes())
    validate_authorization_value(auth)
    return auth


def current_registration(auth, manifest):
    pointer = json.loads(canonical(manifest['pointer']).read_bytes())
    require(pointer['schema'] == 'causes.storage_identity_pointer.v1'
            and pointer['manifest'] == auth['manifest'], 'storage_identity_pointer_changed')
    registration = checked(pointer['registration'])
    review = checked(pointer['independent_review'])
    adoption = checked(pointer['root_adoption'])
    require(review.get('accepted_storage_phase_registration') is True
            and review.get('registration') == pointer['registration'],
            'storage_phase_not_independently_accepted')
    require(adoption.get('root_accepted') is True
            and adoption.get('registration') == pointer['registration']
            and adoption.get('independent_review') == pointer['independent_review']
            and adoption.get('manifest') == auth['manifest'],
            'storage_phase_not_adopted')
    require(registration['schema'] == 'causes.storage_controller_phase.v1'
            and registration['identity_manifest'] == auth['manifest'],
            'storage_phase_manifest_changed')
    for role, owner in registration['owners'].items():
        require(owner['output_root'] == manifest['outputs'][role],
                'storage_identity_owner_output_changed')
        if role.startswith('new_'):
            receipt = checked(owner['launch_owner_receipt'])
            require(receipt['process'] == owner['process']
                    and receipt['controller_id'] == owner['controller_id']
                    and receipt['binding']['output'] == owner['output_root']
                    and receipt['binding']['storage_binding'] == owner['guard']['binding']
                    and receipt['guard_directory'] == owner['guard']['directory']
                    and receipt['guard_reporting_lock'] == owner['guard']['reporting_lock'],
                    'storage_owner_launch_provenance_changed')
    return pointer, registration


def observed_resolution(auth):
    """Worker entry: import the separately pinned bundle in a fresh interpreter."""
    manifest = validate_authorization_value(auth)
    require(str(Path(sys.executable).resolve()) == manifest['python']['path'],
            'storage_identity_interpreter_changed')
    require(not any(name == 'causes' or name.startswith('causes.') for name in sys.modules),
            'storage_identity_worker_requires_fresh_imports')
    pointer, registration = current_registration(auth, manifest)
    sys.path.insert(0, manifest['pythonpath'])
    from causes import campaign_storage_observation as observation
    for name, module in tuple(sys.modules.items()):
        if name == 'causes' or name.startswith('causes.'):
            path = Path(module.__file__).resolve()
            require(path.name in manifest['source_inventory']
                    and str(path) == manifest['source_inventory'][path.name]['path'],
                    'storage_identity_import_outside_bundle')
    phase = observation.observe_phase(registration)
    for reference in manifest['source_inventory'].values():
        pinned(reference)
    require(current_registration(auth, manifest)[0] == pointer,
            'storage_identity_pointer_changed_during_observation')
    controllers = []
    for role, owner in registration['owners'].items():
        if owner['observation_state'] == 'retired':
            continue
        model = 'mi27' if role.endswith('_mi') else 'tex9'
        controllers.append({'model': model, 'role': role, **owner['process'],
                            'output_root': owner['output_root'],
                            'identity_evidence': pointer['registration']})
    controllers.sort(key=lambda value: value['model'])
    require(controllers and len({row['model'] for row in controllers}) == len(controllers),
            'storage_identity_duplicate_model_owners')
    return {'controllers': controllers, 'phase': registration['phase'],
            'manifest_sha256': auth['manifest']['sha256'],
            'identity_receipt': pointer['registration'],
            'succession_observations': [phase],
            'runtime_actions': False}


def resolve_controllers(auth):
    """Avoid mixing the old observer's imported package with successor sources."""
    manifest = validate_authorization_value(auth)
    before, _ = current_registration(auth, manifest)
    command = [manifest['python']['path'], '-I', str(Path(__file__).resolve()),
               '--worker', json.dumps(auth, sort_keys=True)]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=45,
                            env={'LANG': 'C.UTF-8', 'PATH': '/usr/bin:/bin'})
    require(result.returncode == 0 and len(result.stdout) <= 8 * 1024**2,
            'storage_identity_worker_failed')
    value = json.loads(result.stdout)
    require(value['identity_receipt'] == before['registration']
            and value['manifest_sha256'] == auth['manifest']['sha256']
            and value['runtime_actions'] is False
            and current_registration(auth, manifest)[0] == before,
            'storage_identity_resolution_changed')
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', required=True)
    args = parser.parse_args()
    print(json.dumps(observed_resolution(json.loads(args.worker)), sort_keys=True))


if __name__ == '__main__':
    main()
