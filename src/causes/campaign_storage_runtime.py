"""Record a storage-guarded owner behind an independently controlled barrier."""

import copy
import hashlib
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


def launch_binding(jobs, config, output, storage_binding, record_directory,
                   owned_barrier, options):
    """Describe exact launch inputs for a separately reviewed authority record."""
    return {'jobs_sha256': d.sha(jobs), 'config_sha256': d.sha(config),
            'output': str(h.canonical_path(output)), 'storage_binding': storage_binding,
            'record_directory': str(h.canonical_path(record_directory)),
            'owned_barrier': owned_barrier, 'dispatcher_options': options,
            'runtime_source_sha256': _SOURCE_SHA,
            'storage_source_sha256': s._SOURCE_SHA,
            'dispatcher_source_sha256': d._SOURCE_SHA}


def run_recorded_dispatcher(*, jobs, config, output, storage_binding, record_directory,
                            owned_barrier, options, authority: h.Authority):
    """Require exact authority and durable reporting; never release a barrier.

    The Authority policy callback must establish accepted scientific/custody
    membership, donor route/hold history, source adoption, control routing,
    resource qualification and registration policy. This primitive neither
    replaces those checks nor permits starting an old output a second time.
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
                             str(destination), owned_barrier, options)
    value = authority.check()
    s.require(value.get('storage_launch_binding_sha256') == d.sha(binding),
              'storage_launch_authority_binding_changed')
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
        dispatcher, guard = s.guarded_dispatcher(d.Dispatcher, storage_binding=storage_binding,
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
