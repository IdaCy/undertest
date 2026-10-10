import os
from pathlib import Path
import subprocess
import sys
import time

from causes import campaign_handoff as h
from causes import campaign_storage as storage
from causes import campaign_storage_observation as observation
from causes import campaign_capacity_trial as trial


def launch(plan_ref, review_ref, authority_ref, *, deployment_directory,
           runtime_directory, campaign_directory):
    """Start one reviewed guard behind an existing owned admission barrier.

    The caller supplies trusted deployment scope; the pinned plan, review and fresh authority must agree. An uncertain launch is retained without retry.
    """
    deployment = h.canonical_path(deployment_directory)
    runtime = h.canonical_path(runtime_directory)
    campaign = h.canonical_path(campaign_directory)
    storage.require(deployment.is_relative_to(campaign)
                    and runtime.is_relative_to(deployment),
                    'capacity_guard_launch_scope_changed')
    plan, review = h.checked(plan_ref), h.checked(review_ref)
    storage.require(plan['schema'] == 'causes.capacity_guard_launch_plan.v1'
                    and review['accepted_capacity_guard_launch_preparation'] is True
                    and review['plan'] == plan_ref and h.pin(Path(__file__)) == plan['launcher'],
                    'capacity_guard_launch_unaccepted')
    adoption = h.checked(plan['root_adoption'])
    storage.require(adoption['authorized_candidate_guard_supervision'] is True
                    and adoption['trial_plan'] == plan['trial_plan']
                    and adoption['guard_launch_source'] == plan['launcher'],
                    'capacity_guard_launch_not_adopted')
    h.checked_bytes(plan['python'])
    storage.require(str(Path(sys.executable).resolve()) == plan['python']['path'],
                    'capacity_guard_launch_python_changed')
    value, _, _, _ = trial.load_trial(plan['trial_plan'])
    binding = value['capacity_guard']
    registration = h.checked(plan['waiting_registration']['evidence'])
    waiting_review = h.checked(plan['waiting_registration']['review'])
    storage.require(waiting_review['accepted_actual_waiting_owner'] is True
                    and waiting_review['accepted_storage_phase_registration'] is True
                    and waiting_review['registration'] == plan['waiting_registration']['evidence'],
                    'capacity_guard_waiting_owner_unaccepted')
    owner = registration['owners']['new_mi']
    storage.require(owner['observation_state'] == 'waiting' and owner['process'] == value['owner_process']
                    and owner['guard']['binding'] == value['sampling']['guard']['binding']
                    and owner['owned_barrier'] == binding['owned_startup_barrier'],
                    'capacity_guard_launch_owner_changed')
    command = [plan['python']['path'], '-B', '-s', '-m', 'causes.campaign_capacity_trial',
               '--plan', plan['trial_plan']['path']]
    storage.require(command == binding['guard_argv'] and plan['pythonpath'] == str(runtime)
                    and plan['cwd'] == str(campaign),
                    'capacity_guard_launch_command_changed')
    pauses = tuple(Path(path) for path in value['sampling']['controls_absent'])

    def validate(authority):
        storage.require(authority['plan'] == plan_ref and authority['independent_review'] == review_ref
                        and authority['root_adoption'] == plan['root_adoption'], 'capacity_guard_launch_authority_changed')
        h.checked_bytes(plan['root_adoption'])
        h.checked_bytes(plan['trial_plan'])
        h.verify_owned_barrier(binding['owned_startup_barrier'], owner['barrier_path'])
        observation.observe_owner(owner)
        return True

    authority = h.Authority(authority_ref, 'start_capacity_guard', pauses, validate)
    authority.check()
    directory = h.canonical_path(plan['record_directory'])
    storage.require(directory.is_relative_to(deployment) and not os.path.lexists(directory)
                    and not os.path.lexists(value['output'])
                    and not os.path.lexists(Path(binding['lease_path']).parent),
                    'fresh_capacity_guard_launch_paths_required')
    h.new_record_directory(directory)
    intent = {'at': h.now(), 'plan': plan_ref, 'review': review_ref, 'authority': authority_ref,
              'trial_plan': plan['trial_plan'], 'argv': command, 'pythonpath': plan['pythonpath'],
              'candidate': owner['process'], 'owned_barrier': binding['owned_startup_barrier'],
              'automatic_retry': False, 'barrier_release': False}
    h.save_new(directory, 'INTENT.json', intent)
    try:
        authority.check()
        with (directory / 'guard.stdout').open('xb') as stdout, (directory / 'guard.stderr').open('xb') as stderr:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                cwd=plan['cwd'], env={**os.environ, 'PYTHONPATH': plan['pythonpath'], 'PYTHONDONTWRITEBYTECODE': '1'},
                start_new_session=True)
        time.sleep(.2)
        proc = Path('/proc') / str(process.pid)
        live = proc.is_dir() and process.poll() is None
        process_ref = {'pid': process.pid, 'process_starttime_ticks':
            (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19] if live else None, 'argv': command}
        h.save_new(directory, 'SPAWN.json', {**intent, 'at': h.now(), 'process': process_ref,
            'live_after_launch': live, 'returncode_if_exited': process.poll()})
        storage.require(live, 'capacity_guard_exited_reconcile_no_retry')
        h.process_identity(process_ref)
        until = time.monotonic() + 30
        lease = None
        while time.monotonic() < until:
            h.process_identity(process_ref)
            if os.path.lexists(binding['lease_path']):
                lease = storage.validate_capacity_guard(binding, owner['process'], require_healthy=True)
                break
            time.sleep(.25)
        storage.require(lease is not None and lease['guard'] == process_ref,
                        'capacity_guard_initial_healthy_lease_unproven')
        h.verify_owned_barrier(binding['owned_startup_barrier'], owner['barrier_path'])
        result = h.save_new(directory, 'RESULT.json', {**intent, 'at': h.now(), 'process': process_ref,
            'initial_lease': lease, 'live_after_launch': True, 'passed': True,
            'barrier_release': False, 'actual_guard_review_pending': True})
        return {'passed': True, 'result': result}
    except BaseException as error:
        h.save_new(directory, 'FAILED.json', {'at': h.now(), 'error_type': type(error).__name__,
            'error': str(error), 'partial_or_detached_state_must_be_reconciled': True, 'automatic_retry': False})
        raise
