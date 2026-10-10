"""Local orchestration fixtures; no scientific foundation or host observation."""
import copy
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from causes import campaign_consumer_ownership as own
from causes import campaign_isolated_consumer_run as runner


class LateAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.shared = self.base / 'shared'; self.shared.mkdir()
        protected = own.save(self.shared / 'original.json', {'preserved': True})
        self.layout = own.layout(self.base / 'pair')
        self.contract = dict(layout=self.layout, protected_directories=[str(self.shared)],
            protected_pins=[protected], protected_processes=[own.process(os.getpid())])
        self.binding = self.base / 'foundation.json'
        self.binding_review = self.base / 'foundation-peer.json'
        self.authority = self.base / 'assembly-adoption.json'
        self.capture = self.base / 'capture'; self.capture.mkdir()
        self.gate = {key: own.save(self.base / (key + '.json'), {'fixture': key}) for key in
                     ('configuration', 'configuration_review', 'source', 'policy', 'launch')}
        self.gate.update(path=str(self.capture / 'REPORT.json'), host='fixture-host', wait_seconds=5)
        self.late = dict(foundation_binding=str(self.binding),
            foundation_binding_review=str(self.binding_review), adoption=str(self.authority), wait_seconds=5,
            status_after_report=self.gate)
        self.source = own.pin(Path(runner.__file__).resolve())

    def records(self, late=True):
        assembly = self.base / 'assembly.py'
        assembly.write_text('''import argparse,json,os
from pathlib import Path
from causes import campaign_consumer_ownership as o
p=argparse.ArgumentParser()
for k in ('plan','review','output','foundation-binding','foundation-binding-review'): p.add_argument('--'+k,required=True)
p.add_argument('--assembly-adoption'); a=p.parse_args()
pr=o.pin(a.plan); plan=o.checked(pr); actual=o.pin(Path(plan['ownership']['layout']['workspace'])/'OWNERSHIP_ACTUAL.json')
x=o.checked(actual)
o.verify_live_ownership(actual,pr,plan['ownership'],x['receipts'])
if a.assembly_adoption:
 o.verify_assembly_adoption(o.pin(a.assembly_adoption),pr,o.pin(a.review),x['adoption'],o.pin(a.foundation_binding),o.pin(a.foundation_binding_review),actual,x['receipts'])
o.save(a.output,{'local_orchestration_fixture':True,'owner_live':True})
''')
        one = self.base / 'one.py'
        one.write_text('''import argparse,time,datetime as dt
from pathlib import Path
from causes import campaign_consumer_ownership as o
p=argparse.ArgumentParser()
for k in ('plan','review','action'): p.add_argument('--'+k,required=True)
a=p.parse_args(); plan=o.checked(o.pin(a.plan)); root=Path(plan['execution_directory']); out=root/a.action; out.mkdir()
started=dt.datetime.now(dt.timezone.utc).isoformat()
o.save(out/'BEGAN.json',{'action':a.action})
time.sleep(0.18 if a.action=='sync' else 0.06)
o.save(out/'EXECUTION.v1.json',{'plan':o.pin(a.plan),'returncode':0,'error':None,'started_at':started})
''')
        config = own.save(self.base / 'observation.json', dict(
            snapshot=dict(destination=self.layout['snapshot_destination']),
            status_directory=self.layout['status_directory'], transport_directory=self.layout['transport_directory']))
        entries = {}
        for action in ('status', 'sync'):
            source = self.base / (action + '.py'); source.write_text('# exact unused entrypoint fixture\n')
            adjacent = own.save(source.with_suffix('.config.json'), dict(action=action, observation_config=config))
            entries[action] = dict(source=own.pin(source), configuration=adjacent)
        plan = dict(wrapper_source=self.source, assembly_source=own.pin(assembly), source=own.pin(one),
            native=dict(policy=self.gate['policy'], launch=self.gate['launch'], foundation_source=self.gate['source']),
            workflow={'fixture': 'bounded local pair'}, pause_paths=[], required_pins=[config],
            interpreter=own.pin(Path(sys.executable).resolve()), ownership_source=own.pin(Path(own.__file__).resolve()),
            observation_configuration=config, ownership=self.contract,
            execution_directory=self.layout['execution_directory'], entries=entries)
        if late: plan['late_assembly'] = self.late
        plan_ref = own.save(self.base / 'plan.json', plan)
        review_ref = own.save(self.base / 'review.json', dict(accepted_storage_phase_consumer_execution=True,
            accepted_isolated_consumer_execution=True, plan=plan_ref, wrapper_source=self.source))
        adoption = dict(root_authorized=True, action='one_isolated_status_sync_pair', plan=plan_ref,
            review=review_ref, source=self.source, maximum_invocations=1, workflow=plan['workflow'],
            at=dt.datetime.now(dt.timezone.utc).isoformat())
        if late: adoption['late_assembly'] = self.late
        else:
            adoption['foundation_binding'] = own.save(self.binding, {'fixture': 'immediate foundation'})
            adoption['foundation_binding_review'] = own.save(self.binding_review, {'fixture': 'immediate peer'})
        adoption_ref = own.save(self.base / 'pair-adoption.json', adoption)
        return plan, plan_ref, review_ref, adoption_ref

    def capture_report(self, **changes):
        stamp = dt.datetime.now(dt.timezone.utc).isoformat()
        request = own.save(self.capture / 'REQUEST.json', dict(configuration=self.gate['configuration'],
            review=self.gate['configuration_review'], action='foundation', host=self.gate['host'],
            stage='waiting_release', waiting=dict(policy=self.gate['policy'], launch=self.gate['launch'])))
        intent = own.save(self.capture / 'INTENT.json', dict(request=request, source=self.gate['source'],
            review=self.gate['configuration_review'], read_only=True, no_automatic_retry=True))
        raw = dict(action='foundation', host=self.gate['host'], stage='waiting_release', started_at=stamp, at=stamp,
            observation=dict(waiting_launch=self.gate['launch'], waiting=dict(at=stamp,
                policy=self.gate['policy'], launch=self.gate['launch'])), files=[], read_only=True)
        raw.update(changes)
        stdout = own.save(self.capture / 'stdout', raw)
        (self.capture / 'stderr').write_bytes(b'')
        execution = own.save(self.capture / 'EXECUTION.json', dict(at=dt.datetime.now(dt.timezone.utc).isoformat(),
            intent=intent, returncode=0, timed_out=False, remote_completion_unknown=False,
            stdout=stdout, stderr=own.pin(self.capture / 'stderr')))
        mapping = own.save(self.capture / 'RECEIPT_MAP.json', {'files': []})
        return own.save(self.capture / 'REPORT.json', dict(
            {key: value for key, value in raw.items() if key != 'files'},
            configuration=self.gate['configuration'], execution=execution, receipt_map=mapping))

    def seal_fixture_pair(self, workspace, plan_ref):
        report = self.capture_report()
        if not self.binding.exists(): own.save(self.binding, {'waiting_report': report})
        if not self.binding_review.exists(): own.save(self.binding_review, {'fixture': 'peer'})
        own.save(Path(self.layout['workspace']) / 'STATUS_GATE.json', own.verify_status_report(self.gate, report))
        receipts = {a: own.save(Path(self.layout['execution_directory']) / (a + '.json'),
            dict(plan=plan_ref, returncode=0, error=None, started_at=dt.datetime.now(dt.timezone.utc).isoformat()))
            for a in ('status', 'sync')}
        return receipts, workspace.seal(receipts)

    def command(self, refs, late=True):
        _, plan, review, adoption = refs
        argv = [sys.executable, '-B', str(Path(runner.__file__).resolve()), '--plan', plan['path'],
            '--review', review['path'], '--adoption', adoption['path'], '--foundation-binding', str(self.binding),
            '--foundation-binding-review', str(self.binding_review)]
        if late: argv += ['--assembly-adoption', str(self.authority)]
        return argv

    def wait_path(self, path):
        until = time.monotonic() + 8
        while not path.exists():
            if time.monotonic() >= until: raise TimeoutError(str(path))
            time.sleep(0.01)

    def publish_authority(self, refs, actual_ref, **changes):
        plan, plan_ref, review_ref, pair_adoption = refs
        actual = own.checked(actual_ref)
        value = dict(root_authorized=True, action='one_isolated_consumer_assembly', maximum_invocations=1,
            automatic_retry=False, at=dt.datetime.now(dt.timezone.utc).isoformat(), plan=plan_ref, review=review_ref,
            pair_adoption=pair_adoption, source=self.source, assembly_source=plan['assembly_source'],
            workflow=plan['workflow'], foundation_binding=own.pin(self.binding),
            foundation_binding_review=own.pin(self.binding_review), ownership=actual_ref, receipts=actual['receipts'],
            status_gate=own.pin(Path(self.layout['workspace']) / 'STATUS_GATE.json'))
        value.update(changes)
        return value

    def test_real_pair_precedes_foundation_and_lock_survives_until_late_assembly(self):
        refs = self.records(); errors = []; observations = {}
        def late_publisher():
            try:
                self.wait_path(Path(self.layout['execution_directory']) / 'sync/BEGAN.json')
                observations['foundation_initially_absent'] = not self.binding.exists()
                observations['status_waited_for_report'] = not (Path(self.layout['execution_directory']) / 'status').exists()
                report = self.capture_report()
                own.save(self.binding, {'fixture': 'foundation published during active pair', 'waiting_report': report})
                own.save(self.binding_review, {'fixture': 'independent peer'})
                path = Path(self.layout['workspace']) / 'OWNERSHIP_ACTUAL.json'
                self.wait_path(path); actual_ref = own.pin(path)
                actual = own.verify_live_ownership(actual_ref, refs[1], self.contract, own.checked(actual_ref)['receipts'])
                observations['sole_owner'] = own.lock_owners(Path(self.layout['ownership_lock'])) == [actual['owner']['pid']]
                observations['no_assembly_yet'] = not (Path(self.layout['execution_directory']) / 'READINESS.v1.json').exists()
                own.save(self.authority, self.publish_authority(refs, actual_ref))
            except BaseException as error: errors.append(error)
        thread = threading.Thread(target=late_publisher); thread.start()
        result = subprocess.run(self.command(refs), capture_output=True, timeout=15)
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(observations, dict(foundation_initially_absent=True, status_waited_for_report=True,
                                          sole_owner=True, no_assembly_yet=True))
        root = Path(self.layout['workspace'])
        completion = own.checked(own.pin(root / 'RESULT.json'))
        self.assertEqual(completion['assembly_adoption'], own.pin(self.authority))
        self.assertIsNone(own.checked(own.pin(root / 'OWNERSHIP_END.json'))['failure'])
        self.assertEqual(own.lock_owners(Path(self.layout['ownership_lock'])), [])
        original = own.pin(root / 'RESULT.json')
        replay = subprocess.run(self.command(refs), capture_output=True, timeout=5)
        self.assertNotEqual(replay.returncode, 0)
        self.assertEqual(own.pin(root / 'RESULT.json'), original)

    def test_legacy_immediate_cli_still_runs_without_late_authority(self):
        refs = self.records(late=False)
        result = subprocess.run(self.command(refs, late=False), capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(self.authority.exists())
        final = own.checked(own.pin(Path(self.layout['workspace']) / 'RESULT.json'))
        self.assertIsNone(final['assembly_adoption'])

    def test_exact_authority_refusals_preserve_live_lock_and_receipts(self):
        refs = self.records(); plan, plan_ref, review_ref, adoption_ref = refs
        with own.Workspace(self.contract, plan_ref, adoption_ref) as workspace:
            receipts, actual = self.seal_fixture_pair(workspace, plan_ref)
            good = self.publish_authority(refs, actual)
            cases = [dict(receipts={}), dict(pair_adoption=review_ref), dict(ownership=plan_ref),
                dict(at=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=181)).isoformat()),
                dict(maximum_invocations=True), dict(maximum_invocations=1.0), dict(automatic_retry=True)]
            for update in cases:
                with self.subTest(update=update):
                    own.save(self.authority, dict(good, **update))
                    with self.assertRaises(ValueError):
                        own.verify_assembly_adoption(own.pin(self.authority), plan_ref, review_ref, adoption_ref,
                            own.pin(self.binding), own.pin(self.binding_review), actual, receipts)
                    self.authority.unlink()  # Test fixture only; no runtime retry or evidence cleanup.
                    own.verify_live_ownership(actual, plan_ref, self.contract, receipts)
            own.save(self.authority, good)
            own.verify_assembly_adoption(own.pin(self.authority), plan_ref, review_ref, adoption_ref,
                own.pin(self.binding), own.pin(self.binding_review), actual, receipts)
        with self.assertRaisesRegex(ValueError, 'not_currently_owned'):
            own.verify_assembly_adoption(own.pin(self.authority), plan_ref, review_ref, adoption_ref,
                own.pin(self.binding), own.pin(self.binding_review), actual, receipts)

    def test_missing_authority_times_out_without_release_or_receipt_replay(self):
        refs = self.records(); plan, plan_ref, review_ref, adoption_ref = refs
        with own.Workspace(self.contract, plan_ref, adoption_ref) as workspace:
            receipts, actual = self.seal_fixture_pair(workspace, plan_ref)
            from unittest.mock import patch
            with patch.object(runner.time, 'monotonic', side_effect=[0, 6]):
                with self.assertRaisesRegex(TimeoutError, 'not_published'):
                    runner.await_assembly(plan, review_ref, plan_ref, adoption_ref, self.binding,
                        self.binding_review, self.authority, actual, receipts)
            own.verify_live_ownership(actual, plan_ref, self.contract, receipts)
            self.assertFalse(self.authority.exists())

    def test_late_contract_rejects_other_paths_and_unbounded_wait(self):
        plan = {'late_assembly': self.late, 'native': dict(policy=self.gate['policy'], launch=self.gate['launch'],
                                                        foundation_source=self.gate['source'])}
        adoption = {'late_assembly': copy.deepcopy(self.late)}
        for wait in (0, 181, True, 5.0):
            candidate = copy.deepcopy(plan); candidate['late_assembly']['wait_seconds'] = wait
            with self.subTest(wait=wait), self.assertRaises(ValueError):
                own.late_assembly_paths(candidate, adoption, self.binding, self.binding_review, self.authority)
        with self.assertRaisesRegex(ValueError, 'paths_changed'):
            own.late_assembly_paths(plan, adoption, self.binding_review, self.binding, self.authority)
        with self.assertRaisesRegex(ValueError, 'delegation_changed'):
            own.late_assembly_paths(plan, {'late_assembly': {}}, self.binding, self.binding_review, self.authority)

    def test_existing_late_authority_is_refused_before_workspace_creation(self):
        refs = self.records(); own.save(self.authority, {'fixture': 'already consumed or premature'})
        result = subprocess.run(self.command(refs), capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(Path(self.layout['workspace']).exists())

    def test_report_gate_rejects_wrong_waiting_owner_and_changed_raw_stream(self):
        report = self.capture_report()
        good = own.verify_status_report(self.gate, report)
        self.assertTrue(good['scheduling_only']); self.assertFalse(good['foundation_accepted'])
        other = dict(self.gate, launch=self.gate['policy'])
        with self.assertRaisesRegex(ValueError, 'request_changed'):
            own.verify_status_report(other, report)
        original = (self.capture / 'stdout').read_bytes()
        (self.capture / 'stdout').write_bytes(original + b'\n')
        with self.assertRaisesRegex(ValueError, 'stream_changed'):
            own.verify_status_report(self.gate, report)

    def test_report_gate_rejects_future_waiting_time(self):
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=60)).isoformat()
        report = self.capture_report(observation=dict(waiting_launch=self.gate['launch'], waiting=dict(
            at=future, policy=self.gate['policy'], launch=self.gate['launch'])))
        with self.assertRaisesRegex(ValueError, 'time_invalid'):
            own.verify_status_report(self.gate, report)

    def test_pure_plan_binding_changes_only_the_late_contract(self):
        plan = dict(native=dict(policy=self.gate['policy'], launch=self.gate['launch'],
                    foundation_source=self.gate['source'], foundation=None, waiting_report=None),
                    preserved={'source': self.source, 'science': [4, 1]})
        configuration = own.save(self.base / 'actual-config.json', dict(policy=self.gate['policy'],
            source=self.gate['source'], native_host=self.gate['host']))
        peer = own.save(self.base / 'actual-config-peer.json', dict(accepted_native_foundation_configuration=True,
            configuration=configuration, source=self.gate['source']))
        result = own.with_late_assembly(plan, configuration, peer, self.gate['path'],
            self.binding, self.binding_review, self.authority, wait_seconds=5)
        late = result.pop('late_assembly')
        self.assertEqual(result, plan)
        self.assertEqual(late['status_after_report']['launch'], self.gate['launch'])
        self.assertFalse(self.binding.exists()); self.assertFalse(self.authority.exists())
        wrong = copy.deepcopy(plan); wrong['native']['policy'] = self.gate['source']
        with self.assertRaisesRegex(ValueError, 'configuration_changed'):
            own.with_late_assembly(wrong, configuration, peer, self.gate['path'],
                self.binding, self.binding_review, self.authority)

    def retained_launch_plan(self):
        original = self.gate['launch']
        retained = self.base / 'retained-launch.json'
        Path(original['path']).rename(retained)
        local = own.pin(retained)
        return dict(native=dict(policy=self.gate['policy'], launch=original,
            foundation_source=self.gate['source'], foundation=None, waiting_report=None),
            launch_custody=dict(source=original, local=local), required_pins=[local])

    def test_retained_launch_preserves_original_in_plan_and_status_gate(self):
        plan = self.retained_launch_plan()
        configuration = own.save(self.base / 'retained-config.json', dict(policy=self.gate['policy'],
            source=self.gate['source'], native_host=self.gate['host']))
        peer = own.save(self.base / 'retained-config-peer.json', dict(accepted_native_foundation_configuration=True,
            configuration=configuration, source=self.gate['source']))
        result = own.with_late_assembly(plan, configuration, peer, self.gate['path'],
            self.binding, self.binding_review, self.authority, wait_seconds=5)
        self.assertFalse(Path(self.gate['launch']['path']).exists())
        self.assertEqual(result['native']['launch'], self.gate['launch'])
        self.assertEqual(result['late_assembly']['status_after_report']['launch'], self.gate['launch'])
        own.late_assembly_paths(result, {'late_assembly': result['late_assembly']},
            self.binding, self.binding_review, self.authority)
        for ref in result['required_pins']:
            self.assertEqual(own.pin(ref['path']), ref)
        self.assertEqual(own.local_launch(result), {'fixture': 'launch'})

    def test_retained_launch_rejects_wrong_source_bytes_and_local_projection(self):
        plan = self.retained_launch_plan()
        cases = []
        wrong = copy.deepcopy(plan); wrong['launch_custody']['source'] = self.gate['policy']; cases.append(wrong)
        wrong = copy.deepcopy(plan); wrong['launch_custody']['local']['sha256'] = '0' * 64; cases.append(wrong)
        wrong = copy.deepcopy(plan); wrong['launch_custody']['local']['bytes'] += 1; cases.append(wrong)
        wrong = copy.deepcopy(plan); wrong['required_pins'] = []; cases.append(wrong)
        wrong = copy.deepcopy(plan); wrong['required_pins'].append(self.gate['launch']); cases.append(wrong)
        wrong = copy.deepcopy(plan); wrong['launch_custody']['local'] = self.gate['launch']; cases.append(wrong)
        for number, wrong in enumerate(cases):
            with self.subTest(number=number), self.assertRaises(ValueError):
                own.local_launch(wrong)
        Path(plan['launch_custody']['local']['path']).write_text('changed retained bytes')
        with self.assertRaisesRegex(ValueError, 'workspace_input_changed'):
            own.local_launch(plan)

    def test_retained_launch_mapping_does_not_allow_a_changed_gate_identity(self):
        plan = self.retained_launch_plan(); plan['late_assembly'] = copy.deepcopy(self.late)
        plan['late_assembly']['status_after_report']['launch'] = plan['launch_custody']['local']
        with self.assertRaisesRegex(ValueError, 'status_gate_native_scope_changed'):
            own.late_assembly_paths(plan, {'late_assembly': plan['late_assembly']},
                self.binding, self.binding_review, self.authority)


if __name__ == '__main__':
    unittest.main()
