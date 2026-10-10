"""Once caller for two pinned consumers and isolated readiness assembly.

Only a concrete, independently accepted plan and fresh explicit adoption may
enter the workspace. Preserve failed workspaces; no retries or cleanup.
"""
import argparse
import concurrent.futures
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from causes.campaign_consumer_ownership import checked, pin, save


def module(ref, name):
    assert pin(ref['path']) == ref
    spec = importlib.util.spec_from_file_location(name, ref['path'])
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


def group_members(group):
    members = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit(): continue
        try:
            state = (path / 'stat').read_text().rsplit(')', 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(state[2]) == group and state[0] not in ('Z', 'X', 'x'):
            members.append(dict(pid=int(path.name), start=state[19]))
    return members


def stop_group(group):
    """Retire only the new session/process group created for this child."""
    for sig, seconds in ((signal.SIGTERM, 2), (signal.SIGKILL, 3)):
        if not group_members(group): return []
        try: os.killpg(group, sig)
        except ProcessLookupError: return []
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if not group_members(group): return []
            time.sleep(0.02)
    return group_members(group)


def run_child(argv, out, err, timeout):
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    rc = None; error = None; process_info = None; remaining = []
    with out.open('xb') as stdout, err.open('xb') as stderr:
        child = None
        try:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                start_new_session=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            process_info = dict(pid=child.pid, session=child.pid, process_group=child.pid)
            rc = child.wait(timeout=timeout)
            if group_members(child.pid):
                raise RuntimeError('consumer_descendants_survived_parent')
        except BaseException as exc:
            error = dict(type=type(exc).__name__, message=str(exc))
        finally:
            if child is not None:
                remaining = stop_group(child.pid)
                try: rc = child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    error = dict(type='OwnedGroupRetirementFailure', message='owned child did not retire')
    if remaining:
        error = dict(type='OwnedGroupRetirementFailure', message=json.dumps(remaining))
    return dict(argv=argv, started_at=started, ended_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                returncode=rc, error=error, stdout=pin(out), stderr=pin(err),
                process=process_info, owned_group_remaining=remaining, automatic_retry=False)


def main():
    parser = argparse.ArgumentParser()
    for field in ('plan', 'review', 'adoption', 'foundation-binding', 'foundation-binding-review'):
        parser.add_argument('--' + field, required=True)
    args = parser.parse_args()
    plan_ref, review_ref, adoption_ref = [pin(Path(getattr(args, x))) for x in ('plan', 'review', 'adoption')]
    plan, review, adoption = map(checked, (plan_ref, review_ref, adoption_ref))
    assert plan['wrapper_source'] == pin(Path(__file__).resolve())
    assert review['accepted_storage_phase_consumer_execution'] is True and review['plan'] == plan_ref
    assert review['accepted_isolated_consumer_execution'] is True
    assert review['wrapper_source'] == plan['wrapper_source']
    assert adoption['root_authorized'] is True and adoption['action'] == 'one_isolated_status_sync_pair'
    assert adoption['plan'] == plan_ref and adoption['review'] == review_ref
    assert adoption['source'] == plan['wrapper_source'] and adoption['maximum_invocations'] == 1
    assert adoption['workflow'] == plan['workflow']
    assert adoption['foundation_binding'] == pin(Path(args.foundation_binding))
    assert adoption['foundation_binding_review'] == pin(Path(args.foundation_binding_review))
    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(adoption['at'])).total_seconds()
    assert 0 <= age <= 180
    for path in plan['pause_paths']:
        assert not Path(path).exists()
    for ref in plan['required_pins']:
        assert pin(ref['path']) == ref
    assert plan['interpreter'] == pin(Path(sys.executable).resolve())
    import causes.campaign_consumer_ownership as owner
    assert plan['ownership_source'] == pin(Path(owner.__file__).resolve())
    config = checked(plan['observation_configuration'])
    owner.configuration_layout(config, plan['ownership']['layout'])
    assert plan['execution_directory'] == plan['ownership']['layout']['execution_directory']
    for action in ('status', 'sync'):
        entry = plan['entries'][action]
        assert pin(entry['source']['path']) == entry['source']
        adjacent = Path(entry['source']['path']).with_suffix('.config.json')
        assert entry['configuration'] == pin(adjacent)
        entry_config = checked(entry['configuration'])
        assert entry_config['action'] == action and entry_config['observation_config'] == plan['observation_configuration']
    with owner.Workspace(plan['ownership'], plan_ref, adoption_ref) as workspace:
        root = Path(plan['ownership']['layout']['workspace'])
        save(root / 'INTENT.json', dict(at=dt.datetime.now(dt.timezone.utc).isoformat(),
             plan=plan_ref, review=review_ref, adoption=adoption_ref,
             argv=sys.argv, owner=owner.process(os.getpid()), automatic_retry=False))
        outcomes = {}
        def invoke(name, argv, timeout):
            out, err = root / (name + '.stdout'), root / (name + '.stderr')
            record = run_child(argv, out, err, timeout)
            outcomes[name] = save(root / (name + '.EXECUTION.json'), record)
            return record
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(invoke, action,
                    [sys.executable, '-B', plan['source']['path'], '--plan', args.plan, '--review', args.review,
                     '--action', action], 170 if action == 'status' else 920) for action in ('status', 'sync')]
                results = [future.result() for future in futures]
            assert all(x['returncode'] == 0 and x['error'] is None and x['stderr']['bytes'] == 0 for x in results)
            receipts = {key: pin(Path(plan['execution_directory']) / key / 'EXECUTION.v1.json') for key in ('status', 'sync')}
            workspace.seal(receipts)
            argv = [sys.executable, '-B', plan['assembly_source']['path'], '--plan', args.plan,
                '--review', args.review, '--output', str(Path(plan['execution_directory']) / 'READINESS.v1.json'),
                '--foundation-binding', args.foundation_binding,
                '--foundation-binding-review', args.foundation_binding_review]
            result = invoke('assembly', argv, 120)
            assert result['returncode'] == 0 and result['error'] is None and result['stderr']['bytes'] == 0
            for ref in plan['required_pins']:
                assert pin(ref['path']) == ref
            final = save(root / 'RESULT.json', dict(passed=True, plan=plan_ref, review=review_ref,
                adoption=adoption_ref, executions=outcomes, ownership=workspace.actual,
                readiness=pin(Path(plan['execution_directory']) / 'READINESS.v1.json'),
                independent_actual_review_required=True, runtime_release_authorized=False))
            print(json.dumps(final))
        except BaseException as exc:
            save(root / 'FAILED.json', dict(plan=plan_ref, review=review_ref, adoption=adoption_ref,
                executions=outcomes, error=dict(type=type(exc).__name__, message=str(exc)), automatic_retry=False))
            raise


if __name__ == '__main__':
    main()
