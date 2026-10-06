import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_restore as restore
from causes import campaign_sandbox as sandbox


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, entries, name='archive.tar'):
        path = self.root / name
        with tarfile.open(path, 'w', format=tarfile.PAX_FORMAT) as out:
            root = tarfile.TarInfo('.')
            root.type, root.mode, root.uid, root.gid, root.mtime = tarfile.DIRTYPE, 0o755, os.getuid(), os.getgid(), 1000
            out.addfile(root)
            for name, kind, data in entries:
                member = tarfile.TarInfo(name)
                member.uid, member.gid, member.mode, member.mtime = os.getuid(), os.getgid(), 0o644, 1000
                if kind == 'file':
                    member.size = len(data)
                    out.addfile(member, io.BytesIO(data))
                elif kind == 'symlink':
                    member.type, member.mode, member.linkname = tarfile.SYMTYPE, 0o777, data
                    out.addfile(member)
                elif kind == 'hardlink':
                    member.type, member.linkname = tarfile.LNKTYPE, data
                    out.addfile(member)
                elif kind == 'whiteout':
                    member.type, member.devmajor, member.devminor = tarfile.CHRTYPE, 0, 0
                    out.addfile(member)
                elif kind == 'xattr':
                    member.pax_headers = data
                    out.addfile(member)
        return path

    def plan(self):
        seed = self.root / 'seed'
        seed.mkdir()
        (seed / 'base').write_text('immutable base')
        spec = {'seed_path': str(seed), 'task_root': str(self.root / 'task'), 'workspace': '/workspace/project',
                'base_commit': 'a' * 40, 'image': 'example@sha256:' + 'b' * 64, 'actor_env': [],
                'actor_uid': 65532, 'memory_bytes': 1024**3, 'cpus': 1, 'pids_limit': 64}
        variant = {'id': 'example', 'request': 'example', 'files': []}
        upper = self.archive([('./changed', 'file', b'early')], 'upper.tar')
        tmp = self.archive([('./temporary', 'file', b'early temporary')], 'tmp.tar')
        binding = {'spec_sha256': hashlib.sha256(restore.encoded(spec)).hexdigest(), 'base_commit': spec['base_commit'], 'archives': []}
        checkpoint = {'schema': 'caus.sandbox_checkpoint.v1', 'complete': True, 'errors': [], 'unpause_returncode': 0,
                      'workspace_method': 'overlayfs', 'helper_sha256': restore.SANDBOX_SHA256,
                      'apparmor_profile': sandbox._APPARMOR_PROFILE, 'apparmor_profile_sha256': sandbox._APPARMOR_PROFILE_SHA256,
                      'seed_path': str(seed), 'seed_binding': binding, 'workspace': spec['workspace'],
                      'archives': [{'role': role, **restore.file_pin(path)} for role, path in [('workspace_upper', upper), ('tmpfs_tmp', tmp)]]}
        state = {'seed_binding': binding, 'workspace': spec['workspace'], 'helper_sha256': restore.SANDBOX_SHA256,
                 'image_id': 'sha256:example', 'cue_files': [],
                 'operations': [{'argv': ['tar', '-cf', str(path)], 'returncode': 0, 'timeout': False,
                                 'stderr_bytes': 0} for path in [upper, tmp]]}
        values = {'spec': spec, 'variant': variant, 'checkpoint': checkpoint, 'state': state,
                  'lower_manifest': restore.tree_manifest(seed)}
        inputs = {}
        for key, value in values.items():
            path = self.root / (key + '.json')
            path.write_bytes(restore.encoded(value))
            inputs[key] = restore.file_pin(path)
        return {'schema': 'caus.filesystem_restore_plan.v1', 'process_state_restoration': False,
                'background_state_required': False, 'kernel_release': os.uname().release, 'machine': os.uname().machine,
                'inputs': inputs, 'archives': {'workspace_upper': restore.file_pin(upper), 'tmpfs_tmp': restore.file_pin(tmp)}}

    def construct(self, plan):
        path = self.root / 'plan.json'
        path.write_bytes(restore.encoded(plan))
        return restore.RestoredSandbox(path, restore.digest_file(path), self.root / 'output', 'restore_test')

    def replace_input(self, plan, key, operation):
        path = Path(plan['inputs'][key]['path'])
        data = json.loads(path.read_bytes())
        operation(data)
        path.write_bytes(restore.encoded(data))
        plan['inputs'][key] = restore.file_pin(path)

    def test_valid_plan_only_prepares_and_never_starts_docker(self):
        box = self.construct(self.plan())
        self.assertFalse(box.started)
        self.assertFalse(box.restore_admitted)
        with self.assertRaisesRegex(RuntimeError, 'not_admitted'):
            box.run(['true'])

    def test_archive_hash_tampering_fails_before_output(self):
        plan = self.plan()
        with Path(plan['archives']['workspace_upper']['path']).open('ab') as stream:
            stream.write(b'tamper')
        with self.assertRaisesRegex(ValueError, 'hash_mismatch'):
            self.construct(plan)
        self.assertFalse((self.root / 'output').exists())

    def test_changed_lower_fails_even_with_same_declared_commit(self):
        plan = self.plan()
        (self.root / 'seed/base').write_text('different bytes')
        with self.assertRaisesRegex(ValueError, 'exact_retained_lower'):
            self.construct(plan)

    def test_incomplete_checkpoint_is_rejected(self):
        plan = self.plan()
        self.replace_input(plan, 'checkpoint', lambda value: value.update(complete=False))
        with self.assertRaisesRegex(ValueError, 'incomplete_filesystem'):
            self.construct(plan)

    def test_missing_tmpfs_is_rejected(self):
        plan = self.plan()
        del plan['archives']['tmpfs_tmp']
        with self.assertRaisesRegex(ValueError, 'archive_set'):
            self.construct(plan)

    def test_capture_warning_cannot_be_silently_treated_as_complete(self):
        plan = self.plan()
        self.replace_input(plan, 'state', lambda value: value['operations'][0].update(stderr_bytes=42))
        with self.assertRaisesRegex(ValueError, 'capture_archive_warning'):
            self.construct(plan)

    def test_relative_cue_lineage_retained_without_reapplying_bytes(self):
        box = self.construct(self.plan())
        box.files = [{'path': 'changed-cue', 'content': 'original'}]
        box.restore_source_state['cue_files'] = [{'path': 'changed-cue', 'sha256': 'original'}]
        calls = []
        with patch.object(sandbox.Sandbox, '_cue_files', lambda current: calls.append(current.files[:])):
            box._cue_files()
        self.assertEqual(calls, [[]])
        self.assertEqual(box.record['cue_files'], box.restore_source_state['cue_files'])

    def test_changed_variant_cue_is_rejected(self):
        plan = self.plan()
        self.replace_input(plan, 'variant', lambda value: value.update(files=[{'path': '/meta/cue', 'content': 'changed'}]))
        with self.assertRaisesRegex(ValueError, 'variant_cue_binding'):
            self.construct(plan)

    def test_background_process_restoration_is_rejected(self):
        plan = self.plan()
        plan['background_state_required'] = True
        with self.assertRaisesRegex(ValueError, 'unsupported_restore_plan'):
            self.construct(plan)

    def test_path_traversal_and_absolute_members_rejected(self):
        for index, name in enumerate(['../escape', '/escape', './a/../../escape']):
            with self.subTest(name=name):
                path = self.archive([(name, 'file', b'bad')], str(index) + '.tar')
                with self.assertRaisesRegex(ValueError, 'unsafe_restore'):
                    restore.inspect_archive(path, 'workspace_upper')

    def test_symlink_parent_cannot_redirect_extraction(self):
        path = self.archive([('./alias', 'symlink', '/tmp'), ('./alias/escape', 'file', b'bad')])
        with self.assertRaisesRegex(ValueError, 'nondirectory'):
            restore.inspect_archive(path, 'workspace_upper')

    def test_hardlink_escape_and_forward_links_rejected(self):
        path = self.archive([('./alias', 'hardlink', '../outside')])
        with self.assertRaisesRegex(ValueError, 'unsafe_restore'):
            restore.inspect_archive(path, 'workspace_upper')

    def test_whiteout_only_allowed_in_upper_and_never_extracted_here(self):
        path = self.archive([('./deleted', 'whiteout', None)])
        self.assertEqual(restore.inspect_archive(path, 'workspace_upper')['deleted']['kind'], 'whiteout')
        with self.assertRaisesRegex(ValueError, 'special_file'):
            restore.inspect_archive(path, 'tmpfs_tmp')

    def test_unqualified_overlay_redirect_is_rejected(self):
        path = self.archive([('./redirect', 'xattr', {'SCHILY.xattr.trusted.overlay.redirect': '/other'})])
        with self.assertRaisesRegex(ValueError, 'unsupported_restore_xattr'):
            restore.inspect_archive(path, 'workspace_upper')

    def test_acl_capture_is_explicitly_deferred(self):
        path = self.archive([('./file', 'xattr', {'SCHILY.acl.access': 'user::rw-'})])
        with self.assertRaisesRegex(ValueError, 'acl_restore'):
            restore.inspect_archive(path, 'workspace_upper')

    def test_regular_symlink_and_hardlink_roundtrip_and_content_verification(self):
        path = self.archive([('./file', 'file', b'early state'), ('./link', 'symlink', '/unfollowed/target'),
                             ('./hard', 'hardlink', './file')])
        expected = restore.inspect_archive(path, 'workspace_upper')
        destination = self.root / 'extract'
        destination.mkdir()
        subprocess.run(['tar', '-xpf', str(path), '-C', str(destination)], check=True, capture_output=True)
        restore.verify_extracted(destination, expected)
        (destination / 'file').write_bytes(b'later state')
        with self.assertRaisesRegex(ValueError, 'metadata_mismatch|content_mismatch'):
            restore.verify_extracted(destination, expected)


if __name__ == '__main__':
    unittest.main()
