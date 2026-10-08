"""Run a pinned observation action from an adjacent, private configuration file."""
from pathlib import Path
import hashlib
import importlib
import json
import sys


def canonical(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('noncanonical_entrypoint_path')
    return path


def strict_json(raw):
    def unique(pairs):
        out = {}
        for key,value in pairs:
            if key in out: raise ValueError('duplicate_entrypoint_key')
            out[key] = value
        return out
    def constant(value): raise ValueError('nonfinite_entrypoint_json')
    return json.loads(raw,object_pairs_hook=unique,parse_constant=constant)


def checked(ref):
    raw = canonical(ref['path']).read_bytes()
    if len(raw) != ref['bytes'] or hashlib.sha256(raw).hexdigest() != ref['sha256']:
        raise ValueError('entrypoint_input_changed')
    return raw


def prepare(config_path, source_path):
    config = strict_json(canonical(config_path).read_bytes())
    if config['schema'] != 'causes.campaign_observation_entrypoint.v1' or config['action'] not in ('status','sync'):
        raise ValueError('entrypoint_action_scope')
    raw = canonical(source_path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != config['entrypoint_sha256']:
        raise ValueError('entrypoint_source_changed')
    directory = canonical(config['code_directory'])
    names = ('__init__.py','campaign_monitor.py','campaign_snapshot.py','campaign_observation.py')
    if set(config['sources']) != set(names): raise ValueError('entrypoint_source_inventory')
    for name in names:
        ref = config['sources'][name]
        if ref['path'] != str(directory/'causes'/name): raise ValueError('entrypoint_source_location')
        checked(ref)
    observer = strict_json(checked(config['observation_config']))
    if observer['sources'] != {name:config['sources'][name] for name in names[1:]}:
        raise ValueError('entrypoint_observation_source_mismatch')
    return config


def main():
    if len(sys.argv) != 1: raise ValueError('entrypoint_accepts_only_adjacent_configuration')
    source = canonical(Path(__file__).absolute())
    path = source.with_name(source.stem + '.config.json')
    config = prepare(path,source)
    sys.path.insert(0,config['code_directory'])
    module = importlib.import_module('causes.campaign_observation')
    for name in ('causes','causes.campaign_monitor','causes.campaign_snapshot','causes.campaign_observation'):
        loaded = sys.modules[name]
        filename = '__init__.py' if name == 'causes' else name.rsplit('.',1)[1] + '.py'
        ref = config['sources'][filename]
        if str(canonical(loaded.__file__)) != ref['path']: raise ValueError('entrypoint_import_location_changed')
        checked(ref)
    ref = config['observation_config']
    sys.argv = [str(source),config['action'],'--config',ref['path'],'--config-sha256',ref['sha256']]
    module.main()


if __name__ == '__main__':
    main()
