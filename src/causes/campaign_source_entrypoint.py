"""Invoke a pinned native main with an explicitly reviewed source-copy binding.

The original native program retains its own authority, output and source checks.
This entrypoint introduces no runtime authority and preserves actual argv identity.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


SCHEMA = 'causes.pinned_native_source_entrypoint.v1'
FIELDS = {'schema', 'source', 'source_review', 'source_acceptance_flag',
          'entrypoint', 'entrypoint_review', 'loader_source', 'loader_review',
          'binding', 'strategy', 'native_argv'}


def require(value, reason):
    if not value:
        raise ValueError(reason)


def pin(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path
            and not any(x.is_symlink() for x in (path, *path.parents)), 'canonical_source_path')
    body = path.read_bytes()
    return dict(path=str(path), bytes=len(body), sha256=hashlib.sha256(body).hexdigest())


def checked(ref):
    require(pin(ref['path']) == ref, 'entrypoint_pin_changed')
    return json.loads(Path(ref['path']).read_bytes())


def module(ref, name):
    require(pin(ref['path']) == ref, 'entrypoint_source_changed')
    spec = importlib.util.spec_from_file_location(name, ref['path'])
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    require(pin(value.__file__) == ref, 'loaded_source_identity')
    return value


def peer(ref, flag, **joins):
    value = checked(ref)
    require(value.get(flag) is True and not value.get('findings')
            and all(value.get(k) == v for k, v in joins.items()), 'entrypoint_source_peer')
    return value


def configuration(descriptor_ref):
    descriptor = checked(descriptor_ref)
    require(type(descriptor) is dict and set(descriptor) == FIELDS
            and descriptor['schema'] == SCHEMA, 'exact_entrypoint_descriptor')
    require(descriptor['entrypoint'] == pin(__file__), 'exact_entrypoint_source')
    require(descriptor['strategy'] in ('module', 'saved_parent'), 'constructor_strategy')
    native = descriptor['native_argv']
    require(type(native) is list and len(native) >= 3
            and all(type(arg) is str for arg in native)
            and Path(native[0]).is_absolute() and native[1] == '-B'
            and native[2] == descriptor['source']['path'], 'exact_native_argv')
    flag = descriptor['source_acceptance_flag']
    require(type(flag) is str and flag.startswith('accepted_'), 'native_source_flag')
    peer(descriptor['source_review'], flag, source=descriptor['source'])
    peer(descriptor['entrypoint_review'], 'accepted_pinned_native_source_entrypoint',
         source=descriptor['entrypoint'], loader_source=descriptor['loader_source'])
    peer(descriptor['loader_review'], 'accepted_constructor_source_binding_helper',
         source=descriptor['loader_source'], binding=descriptor['binding'])
    loader = module(descriptor['loader_source'], 'explicit_native_source_binding')
    binding = checked(descriptor['binding'])
    # Verify physical bytes before importing any native constructor.
    loader.physical_source(binding['relocation']['original'], binding['relocation'])
    if descriptor['strategy'] == 'module':
        require(descriptor['source'] in binding['constructors'], 'undeclared_native_constructor')
    return descriptor, loader, binding


def process_argv(descriptor_ref, descriptor):
    return [descriptor['native_argv'][0], '-B', descriptor['entrypoint']['path'],
            '--descriptor', json.dumps(descriptor_ref, sort_keys=True, separators=(',', ':'))]


def saved_parent(original, original_ref, binding):
    """Use BIND4's own pinned, cached parent factory without copying its body."""
    require(pin(original.__file__) == original_ref
            and original.parent.__globals__ is vars(original)
            and Path(original.parent.__code__.co_filename) == Path(original_ref['path']),
            'original_parent_factory_required')
    require(original.known()['input_parent'] == binding['materializer'], 'exact_saved_parent')
    value = original.parent()
    require(pin(value.__file__) == binding['materializer'], 'loaded_saved_parent_identity')
    return value


def run_native(descriptor_ref):
    descriptor, loader, binding = configuration(descriptor_ref)
    require(Path(sys.executable).resolve() == Path(descriptor['native_argv'][0]).resolve(),
            'native_interpreter_changed')
    original = module(descriptor['source'], 'explicit_original_native_entry')
    require(original.main.__globals__ is vars(original)
            and Path(original.main.__code__.co_filename) == Path(descriptor['source']['path']),
            'original_native_main_required')
    target, target_ref = original, descriptor['source']
    if descriptor['strategy'] == 'saved_parent':
        target, target_ref = saved_parent(original, descriptor['source'], binding), binding['materializer']
    previous_argv, previous_bytecode = sys.argv, sys.dont_write_bytecode
    try:
        sys.argv = descriptor['native_argv'][2:]
        sys.dont_write_bytecode = True
        with loader.bind_constructor(target, target_ref, binding):
            return original.main()
    finally:
        sys.argv, sys.dont_write_bytecode = previous_argv, previous_bytecode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--descriptor', type=json.loads, required=True)
    args = parser.parse_args()
    run_native(args.descriptor)


if __name__ == '__main__':
    main()
