import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_consumer_ownership as own
from causes import campaign_isolated_consumer_run as runner
from causes import campaign_snapshot as snapshot
from causes.campaign_consumer_join import verify_snapshot_result


class IsolatedConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.old = self.base / 'shared'; self.old.mkdir()
        self.protected = self.old / 'configuration.json'
        self.protected.write_text('{"untouched": true}\n')
        self.contract = dict(layout=own.layout(self.base / 'trial'),
            protected_directories=[str(self.old)], protected_pins=[own.pin(self.protected)],
            protected_processes=[own.process(os.getpid())])
        self.plan = own.save(self.base / 'plan.json', {'fixture': 'plan'})
        self.adoption = own.save(self.base / 'adoption.json', {'fixture': 'authorization'})

    def receipts(self, workspace):
        result = {}
        for action in ('status', 'sync'):
            result[action] = own.save(Path(workspace.contract['layout']['execution_directory']) / (action + '.json'),
                                     dict(plan=self.plan, returncode=0, error=None))
        return result

    def test_configuration_changes_only_three_paths_and_rejects_shared_or_nested_outputs(self):
        old = dict(snapshot=dict(destination=str(self.old), roots=['runs/original']),
                   status_directory=str(self.old), transport_directory=str(self.old),
                   legacy={'pin': 'original'}, monitor={'policy': 'unchanged'})
        new = own.isolate_config(old, self.base / 'trial', [str(self.old)])
        own.configuration_layout(new, self.contract['layout'])
        new['snapshot']['destination'] = old['snapshot']['destination']
        for key in ('status_directory', 'transport_directory'): new[key] = old[key]
        self.assertEqual(new, old)
        for target in (self.old, self.old / 'subdir', self.base):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'overlaps'):
                own.isolate_config(old, target, [str(self.old)])
        alias = self.base / 'alias'; alias.symlink_to(self.old, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'noncanonical'):
            own.isolate_config(old, alias / 'new', [str(self.old)])

    def test_live_ownership_rechecks_process_lock_and_protected_bytes_and_refuses_replay(self):
        with own.Workspace(self.contract, self.plan, self.adoption) as workspace:
            refs = self.receipts(workspace); actual = workspace.seal(refs)
            checked = own.verify_live_ownership(actual, self.plan, self.contract, refs)
            self.assertEqual(checked['owner'], own.process(os.getpid()))
            self.assertFalse(checked['historical_stop_used'])
            # A separate real process can independently verify the parent's live flock.
            code = 'from causes.campaign_consumer_ownership import *; import json,sys; a=json.loads(sys.argv[1]); verify_live_ownership(*a)'
            child = subprocess.run([sys.executable, '-B', '-c', code,
                                    json.dumps([actual, self.plan, self.contract, refs])], capture_output=True)
            self.assertEqual(child.returncode, 0, child.stderr)
            with self.assertRaises(FileExistsError):
                with own.Workspace(self.contract, self.plan, self.adoption): pass
        with self.assertRaisesRegex(ValueError, 'not_currently_owned'):
            own.verify_live_ownership(actual, self.plan, self.contract, refs)
        with self.assertRaises(FileExistsError):
            with own.Workspace(self.contract, self.plan, self.adoption): pass
        self.assertTrue((self.base / 'trial/OWNERSHIP_END.json').exists())

    def test_changed_boundary_or_failed_child_cannot_seal_success(self):
        with own.Workspace(self.contract, self.plan, self.adoption) as workspace:
            refs = self.receipts(workspace)
            failed = own.save(self.base / 'failed.json', dict(plan=self.plan, returncode=7, error=None))
            with self.assertRaisesRegex(ValueError, 'consumer_execution_failed'):
                workspace.seal(dict(refs, sync=failed))
            original = self.protected.read_bytes(); self.protected.write_bytes(b'changed')
            try:
                with self.assertRaisesRegex(ValueError, 'protected_input_changed'):
                    workspace.seal(refs)
            finally:
                self.protected.write_bytes(original)
            actual = workspace.seal(refs)
            other = copy.deepcopy(self.contract); other['protected_processes'][0]['process_starttime_ticks'] = '0'
            with self.assertRaisesRegex(ValueError, 'binding_changed'):
                own.verify_live_ownership(actual, self.plan, other, refs)

    def test_failure_preserves_exclusive_workspace_and_truthful_end(self):
        with self.assertRaisesRegex(RuntimeError, 'retained refusal'):
            with own.Workspace(self.contract, self.plan, self.adoption):
                raise RuntimeError('retained refusal')
        end = json.loads((self.base / 'trial/OWNERSHIP_END.json').read_text())
        self.assertIsNone(end['actual'])
        self.assertEqual(end['failure']['type'], 'RuntimeError')
        self.assertTrue(end['lock_owned_before_release'])
        with self.assertRaises(FileExistsError):
            with own.Workspace(self.contract, self.plan, self.adoption): pass

    def test_original_failure_and_boundary_failure_are_both_retained(self):
        with self.assertRaisesRegex(RuntimeError, 'original child refusal'):
            with own.Workspace(self.contract, self.plan, self.adoption):
                self.protected.write_text('changed')
                raise RuntimeError('original child refusal')
        end = json.loads((self.base / 'trial/OWNERSHIP_END.json').read_text())
        self.assertEqual(end['failure']['message'], 'original child refusal')
        self.assertEqual(end['boundary_observation_error']['type'], 'ValueError')
        self.assertIsNone(end['protected_after'])
        self.assertEqual(own.lock_owners(self.base / 'trial/owner.lock'), [])

    def test_timeout_retires_only_new_process_group_including_grandchild(self):
        marker = self.base / 'child-started'
        code = ('import os,time; from pathlib import Path; p=os.fork(); '
                'Path(' + repr(str(marker)) + ').write_text(str(os.getpid())) if p==0 else None; time.sleep(60)')
        parent = own.process(os.getpid())
        result = runner.run_child([sys.executable, '-B', '-c', code],
            self.base / 'stdout', self.base / 'stderr', 0.15)
        self.assertTrue(marker.exists())
        self.assertEqual(result['error']['type'], 'TimeoutExpired')
        self.assertEqual(result['owned_group_remaining'], [])
        self.assertEqual(runner.group_members(result['process']['process_group']), [])
        self.assertEqual(own.process(os.getpid()), parent)

    def test_failed_child_has_real_exit_and_no_retry(self):
        result = runner.run_child([sys.executable, '-B', '-c', 'raise SystemExit(7)'],
            self.base / 'stdout', self.base / 'stderr', 5)
        self.assertEqual(result['returncode'], 7)
        self.assertIsNone(result['error'])
        self.assertFalse(result['automatic_retry'])
        self.assertEqual(result['owned_group_remaining'], [])

    def test_retained_mapping_requires_unique_complete_exact_original(self):
        original = dict(own.pin(self.protected), path='/remote/original.json')
        row = dict(remote=original, local=own.pin(self.protected))
        mapping = own.save(self.base / 'map.json', dict(files=[row]))
        self.assertEqual(own.mapped(mapping, original), (row, self.protected.read_bytes()))
        for name, rows in [('missing', []), ('duplicate', [row, row])]:
            ref = own.save(self.base / (name + '.json'), dict(files=rows))
            with self.assertRaisesRegex(ValueError, 'unique_original'):
                own.mapped(ref, original)
        changed = dict(original, sha256='0' * 64)
        ref = own.save(self.base / 'changed-original.json', dict(files=[dict(row, remote=changed)]))
        with self.assertRaisesRegex(ValueError, 'mapped_receipt_changed'):
            own.mapped(ref, changed)
        self.protected.write_text('modified bytes')
        with self.assertRaisesRegex(ValueError, 'retained_receipt_changed'):
            own.mapped(mapping, original)

    def test_real_native_snapshot_uses_isolated_index_and_preserves_shared_lock_and_index(self):
        old_index = self.old / 'accepted.jsonl'; old_index.write_text('{"run":"runs/old/original"}\n')
        old_lock = self.old / 'sync.lock'; old_lock.touch()
        original = (own.pin(old_index), own.lock_stat(old_lock))
        remote = self.base / 'remote'; root = 'runs/trial/family'
        run = remote / root / 'one'; run.mkdir(parents=True)
        (run / 'summary.json').write_text(json.dumps({'sandbox_closure': {'state': 'closed', 'overlay_mounted': False}}))
        (run / 'evidence.bin').write_bytes(b'exact fixture evidence')
        native_run = subprocess.run
        def local_transport(argv, **kwargs):
            if argv[0] == 'ssh':
                return native_run([sys.executable, '-B', '-c', snapshot.REMOTE_CODE], input=kwargs['input'], capture_output=True, text=True)
            self.assertEqual(argv[0], 'rsync')
            listing = Path(next(x.split('=', 1)[1] for x in argv if x.startswith('--files-from=')))
            for name in listing.read_bytes().split(b'\0'):
                if name:
                    source = remote / name.decode(); dest = Path(argv[-1]) / name.decode()
                    dest.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(source, dest)
            return subprocess.CompletedProcess(argv, 0)
        with own.Workspace(self.contract, self.plan, self.adoption) as workspace:
            config = snapshot.SnapshotConfig('fixture', str(remote), (root,),
                Path(self.contract['layout']['snapshot_destination']), free_space_floor_bytes=0)
            def authorize(c):
                return dict(host=c.host, remote_base=c.remote_base, roots=list(c.roots), evidence={'registration': 'fixture'})
            with patch.object(snapshot.subprocess, 'run', local_transport):
                result = snapshot.synchronize_completed(config, authorize)
            attempt = Path(result['attempt'])
            request, auth, manifest, verified = [json.loads((attempt / x).read_text()) for x in
                ('REQUEST.json', 'AUTHORIZATION.json', 'MANIFEST.json', 'VERIFIED.json')]
            verify_snapshot_result(result, request, auth, manifest, verified, own.pin(attempt / 'MANIFEST.json'),
                host=config.host, remote_base=config.remote_base, roots=list(config.roots), authorization_evidence=auth['evidence'])
            result_storage = own.verify_snapshot_storage(result, manifest, verified, self.contract['layout'])
            self.assertEqual(result_storage['files'], 2)
            self.assertEqual(result_storage['runs'], 1)
            self.assertEqual((own.pin(old_index), own.lock_stat(old_lock)), original)
            evidence = attempt / 'data' / root / 'one/evidence.bin'; evidence.write_bytes(b'bad')
            with self.assertRaisesRegex(ValueError, 'snapshot_custody_changed'):
                own.verify_snapshot_storage(result, manifest, verified, self.contract['layout'])
            evidence.write_bytes(b'exact fixture evidence')
            with (config.destination / 'accepted.jsonl').open('ab') as stream:
                stream.write(b'{"run":"runs/old/original"}\n')
            with self.assertRaisesRegex(ValueError, 'not_exact_new_receipts'):
                own.verify_snapshot_storage(result, manifest, verified, self.contract['layout'])


if __name__ == '__main__':
    unittest.main()
