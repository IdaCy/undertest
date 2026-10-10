import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from causes import campaign_handoff as h
from causes import campaign_storage as s
from causes import campaign_storage_policy as policy_module
from causes import campaign_storage_runtime as runtime
from causes import campaign_storage_observation as observation


def start(plan_ref, authority_ref):
    """Start one detached owner under its owned barrier and retain uncertain outcomes."""
    plan = h.checked(plan_ref)
    s.require(plan['schema'] == 'causes.storage_waiting_start_plan.v2'
              and plan['source'] == h.pin(Path(__file__)), 'waiting_start_source_changed')
    working_directory = h.canonical_path(plan['cwd'])
    package_root = h.canonical_path(plan['pythonpath'])
    s.require(working_directory.is_dir()
              and package_root == Path(policy_module.__file__).resolve().parent.parent,
              'waiting_start_runtime_scope_changed')
    for ref in plan['sources']:
        h.checked_bytes(ref)
    policy = h.checked(plan['policy'])
    s.require(plan['role'] == policy['role'] and plan['role'] in ('mi', 'texas'),
              'waiting_start_role_changed')
    operator = policy_module.StorageAdmission(plan['policy'], plan['readiness'], authority_ref)
    authority = operator.authority(runtime.START)
    value = authority.check()
    accepted = h.checked(value['start_preparation_review'])
    s.require(value['start_plan'] == plan_ref and value['root_adoption'] == plan['root_adoption']
              and accepted['accepted_waiting_owner_start_preparation'] is True
              and accepted['plan'] == plan_ref, 'waiting_start_authority_changed')
    h.checked_bytes(plan['python'])
    s.require(plan['python']['path'] == str(Path(sys.executable).resolve()),
              'waiting_start_interpreter_changed')
    directory = h.new_record_directory(plan['receipt_directory'])
    process = None
    try:
        launch = policy['launch']
        output = Path(policy['output_root'])
        s.require(not list(output.iterdir()) and not Path(launch['record_directory']).exists(),
                  'waiting_start_destination_not_fresh')
        command = [plan['python']['path'], '-B', '-s', '-P', '-m', 'causes.campaign_storage_policy', 'run',
                   '--policy', json.dumps(plan['policy']), '--readiness', json.dumps(plan['readiness']),
                   '--authority', json.dumps(authority_ref), '--execute']
        environment = dict(os.environ, PYTHONPATH=str(package_root), PYTHONDONTWRITEBYTECODE='1')
        h.save_new(directory, 'INTENT.json', {'at': h.now(), 'plan': plan_ref, 'authority': authority_ref,
            'argv': command, 'cwd': str(working_directory), 'pythonpath': str(package_root),
            'automatic_restart': False, 'barrier_release': False})
        authority.check()
        with (directory / 'owner.stdout').open('xb') as stdout, (directory / 'owner.stderr').open('xb') as stderr:
            process = subprocess.Popen(command, cwd=working_directory, env=environment,
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True)
        actual = {'pid': process.pid, 'process_starttime_ticks':
                  (Path('/proc') / str(process.pid) / 'stat').read_text().rsplit(')', 1)[1].split()[19],
                  'argv': command}
        h.save_new(directory, 'SPAWNED.json', {'at': h.now(), 'process': actual,
                                             'no_signal_or_retry_on_uncertainty': True})
        deadline = time.monotonic() + 60
        receipt = Path(launch['record_directory']) / 'OWNER.json'
        journal = output / '.dispatcher/journal.jsonl'
        while True:
            s.require(process.poll() is None, 'waiting_owner_exited_before_acceptance')
            if receipt.is_file() and journal.is_file():
                events = [json.loads(line) for line in journal.read_bytes().splitlines()]
                if any(row['event'] == 'admission_barrier_waiting' for row in events):
                    break
            s.require(time.monotonic() < deadline, 'waiting_owner_observation_timeout_no_retry')
            time.sleep(.2)
        owner_receipt = h.pin(receipt)
        owner = h.checked(owner_receipt)
        s.require(owner['process'] == actual, 'waiting_owner_spawn_identity_changed')
        controller_path = output / '.dispatcher/controllers' / owner['controller_id'] / 'identity.json'
        queue_path = output / '.dispatcher/queue_identity.json'
        queue, controller = h.checked(h.pin(queue_path)), h.checked(h.pin(controller_path))
        expected = {'observation_state': 'waiting', 'process': actual, 'output_root': str(output),
            'output_lock': h.stat_identity(output / '.dispatcher.lock'),
            'controller_id': owner['controller_id'], 'queue_identity': queue,
            'queue_identity_pin': h.pin(queue_path), 'controller_identity_pin': h.pin(controller_path),
            'controller_fields': controller, 'journal_prefix': h.pin(journal),
            'barrier_path': launch['options']['start_barrier_file'],
            'owned_barrier': launch['owned_barrier'], 'initial_pending': len(h.checked(launch['jobs'])),
            'launch_owner_receipt': owner_receipt,
            'guard': {'binding': launch['storage_binding'], 'directory': owner['guard_directory'],
                      'reporting_lock': owner['guard_reporting_lock']}}
        observed = observation.observe_owner(expected)
        observed.pop('journal')
        h.process_identity(actual)
        result = h.save_new(directory, 'RESULT.json', {'at': h.now(), 'plan': plan_ref,
            'owner': expected, 'observation': observed, 'passed': True,
            'barrier_released': False, 'claims': 0, 'blocks': 0})
        return {'passed': True, 'result': result, 'process': actual}
    except BaseException as error:
        h.save_new(directory, 'FAILED.json', {'at': h.now(), 'error_type': type(error).__name__,
            'error': str(error), 'spawned_pid': None if process is None else process.pid,
            'no_retry': True, 'no_signal_sent': True, 'barrier_not_released': True})
        raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', required=True)
    parser.add_argument('--authority', required=True)
    args = parser.parse_args(argv)
    print(json.dumps(start(json.loads(args.plan), json.loads(args.authority))))


if __name__ == '__main__':
    main()
