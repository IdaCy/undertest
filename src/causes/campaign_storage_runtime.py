"""Record a storage-guarded owner behind an independently controlled barrier."""

import copy
import hashlib
import inspect
import os
from pathlib import Path

from causes import campaign_dispatcher as d
from causes import campaign_handoff as h
from causes import campaign_storage as s


_SOURCE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
START = 'start_storage_owner_behind_barrier'
_REQUIRED_OPTIONS = {'workers', 'max_full', 'pause_file', 'global_pause_file',
                     'start_barrier_file', 'admission_holds', 'admission_holds_sha256'}
_OPTIONAL_OPTIONS = {'extension_prototypes', 'corpus_continuation', 'corpus_continuation_sha256',
                     'system_prompts_by_run_id', 'prefix_extension'}


def _custom_dispatcher_binding(dispatcher_class, dispatcher_source):
    if dispatcher_class is None:
        s.require(dispatcher_source is None, 'dispatcher_source_requires_custom_class')
        return None
    s.require(isinstance(dispatcher_class, type) and issubclass(dispatcher_class, d.Dispatcher),
              'custom_dispatcher_subclass_required')
    s.require(type(dispatcher_source) is dict
              and set(dispatcher_source) == {'path', 'bytes', 'sha256'},
              'custom_dispatcher_source_required')
    try:
        source = inspect.getsourcefile(dispatcher_class)
    except TypeError:
        source = None
    if source is None:
        # A pinned caller may load a nested class without registering its module.
        methods = [value.__func__ if isinstance(value, (staticmethod, classmethod)) else value
                   for value in vars(dispatcher_class).values()]
        sources = {inspect.getsourcefile(method) for method in methods
                   if inspect.isfunction(method)
                   and method.__qualname__.startswith(dispatcher_class.__qualname__ + '.')}
        s.require(len(sources) == 1, 'custom_dispatcher_defining_source_required')
        source = sources.pop()
    s.require(source is not None and h.canonical_path(source)
              == h.canonical_path(dispatcher_source['path']), 'custom_dispatcher_source_mismatch')
    h.checked_bytes(dispatcher_source)
    return {'source': copy.deepcopy(dispatcher_source),
            'class_qualname': dispatcher_class.__qualname__}


def launch_binding(jobs, config, output, storage_binding, record_directory,
                   owned_barrier, options, *, dispatcher_class=None, dispatcher_source=None):
    """Describe exact launch inputs for a separately reviewed authority record."""
    binding = {'jobs_sha256': d.sha(jobs), 'config_sha256': d.sha(config),
            'output': str(h.canonical_path(output)), 'storage_binding': storage_binding,
            'record_directory': str(h.canonical_path(record_directory)),
            'owned_barrier': owned_barrier, 'dispatcher_options': options,
            'runtime_source_sha256': _SOURCE_SHA,
            'storage_source_sha256': s._SOURCE_SHA,
            'dispatcher_source_sha256': d._SOURCE_SHA}
    custom = _custom_dispatcher_binding(dispatcher_class, dispatcher_source)
    if custom is not None:
        binding['custom_dispatcher'] = custom
    return binding


def run_recorded_dispatcher(*, jobs, config, output, storage_binding, record_directory,
                            owned_barrier, options, authority: h.Authority,
                            dispatcher_class=None, dispatcher_source=None):
    """Require exact authority and durable reporting; never release a barrier.

    The Authority policy callback must establish accepted scientific/custody
    membership, donor route/hold history, source adoption, control routing,
    resource qualification and registration policy. This primitive neither
    replaces those checks nor permits starting an old output a second time.
    A custom Dispatcher's defining source and qualified class name are bound
    explicitly; its captured policy still requires the Authority's validation.
    """
    s.require(isinstance(authority, h.Authority) and authority.action == START,
              'exact_storage_launch_authority_required')
    s.require(type(options) is dict and _REQUIRED_OPTIONS <= set(options)
              and set(options) <= _REQUIRED_OPTIONS | _OPTIONAL_OPTIONS,
              'exact_storage_dispatcher_options_required')
    options = copy.deepcopy(options)
    jobs, config, storage_binding = copy.deepcopy((jobs, config, storage_binding))
    for module, expected in ((s, s._SOURCE_SHA), (d, d._SOURCE_SHA)):
        s.require(hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() == expected,
                  'loaded_storage_runtime_dependency_changed')
    s.require(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == _SOURCE_SHA,
              'loaded_storage_runtime_source_changed')
    root = h.canonical_path(output)
    destination = h.canonical_path(record_directory)
    mount = h.canonical_path(storage_binding['mount_target'])
    s.require(root.is_dir() and not list(root.iterdir()), 'fresh_empty_storage_output_required')
    s.require(not destination.is_relative_to(mount), 'guard_records_must_survive_output_unmount')
    s.require(not destination.exists(), 'new_storage_launch_record_required')
    barrier = h.barrier_path(options['start_barrier_file'])
    owned = h.verify_owned_barrier(owned_barrier, barrier)
    s.require(owned['output_root'] == str(root), 'storage_owned_barrier_output_changed')
    pause_paths = {str(h.canonical_path(options[key])) for key in ('pause_file', 'global_pause_file')}
    s.require(len(pause_paths) == 2 and str(barrier) not in pause_paths
              and pause_paths <= {str(path) for path in authority.pause_paths},
              'storage_launch_pause_scope_changed')
    binding = launch_binding(jobs, config, str(root), storage_binding,
                             str(destination), owned_barrier, options,
                             dispatcher_class=dispatcher_class, dispatcher_source=dispatcher_source)
    value = authority.check()
    s.require(value.get('storage_launch_binding_sha256') == d.sha(binding),
              'storage_launch_authority_binding_changed')
    s.require(_custom_dispatcher_binding(dispatcher_class, dispatcher_source)
              == binding.get('custom_dispatcher'), 'custom_dispatcher_changed_before_construction')
    directory = h.new_record_directory(destination)
    owner = {'pid': os.getpid(), 'process_starttime_ticks':
             Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19],
             'argv': [item.decode() for item in Path('/proc/self/cmdline').read_bytes().split(b'\0') if item]}
    reporter = None
    started = False
    try:
        h.save_new(directory, 'INTENT.json', {'at': h.now(), 'authority': authority.reference,
                                             'process': owner, 'binding': binding})
        reporter = s.GuardJournal(directory / 'guard', storage_binding, owner)
        dispatcher, guard = s.guarded_dispatcher(
            d.Dispatcher if dispatcher_class is None else dispatcher_class, storage_binding=storage_binding,
            jobs=jobs, config=config, output=str(root), guard_reporter=reporter, **options)
        owner_receipt = h.save_new(directory, 'OWNER.json', {'at': h.now(), 'process': owner,
            'controller_id': dispatcher.controller_id, 'authority': authority.reference,
            'binding': binding, 'guard_directory': str(reporter.directory),
            'guard_reporting_lock': reporter.lock_identity,
            'barrier_released': False, 'admission_started': False})
        # Policy and user controls may have changed while the intent was persisted.
        value = authority.check()
        s.require(value.get('storage_launch_binding_sha256') == d.sha(binding),
                  'storage_launch_authority_binding_changed')
        s.require(_custom_dispatcher_binding(dispatcher_class, dispatcher_source)
                  == binding.get('custom_dispatcher'), 'custom_dispatcher_changed_before_run')
        h.verify_owned_barrier(owned_barrier, barrier)
        s.require(not list(root.iterdir()), 'storage_output_changed_before_start')
        s.require(guard.check() >= storage_binding['floor_bytes'], 'storage_launch_floor_not_met')
        started = True
        result = dispatcher.run()
        return h.save_new(directory, 'RESULT.json', {'at': h.now(), 'owner': owner_receipt,
                          'result': result, 'automatic_restart': False})
    except BaseException as error:
        h.save_new(directory, 'FAILED.json', {'at': h.now(), 'error_type': type(error).__name__,
                   'dispatcher_run_entered': started, 'automatic_restart': False,
                   'manual_reconciliation_required': True})
        raise
    finally:
        if reporter is not None:
            reporter.close()
