import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_scientific_owner_census as m


class ScientificOwnerCensusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scope = dict(host='execution', entrypoints=['/code/actor.py', '/code/self.py'], descriptor_entrypoints=[], modules=['causes.actor'], request_paths=['/control/request.json'], output_roots=['/runs/fixed'])
        self.owner = dict(pid=10, process_starttime_ticks='100', argv=['python', '/code/actor.py', '--request', '/control/request.json'])
        self.write(1, ['unrelated', 'private-token'], 0)
        self.write(10, self.owner['argv'], 1)

    def write(self, pid, argv, parent, start='100', state='S'):
        d = self.root / str(pid)
        d.mkdir(exist_ok=True)
        fields = [state, str(parent)] + ['0'] * 17 + [start] + ['0'] * 5
        (d / 'stat').write_text(str(pid) + ' (name with ) parentheses) ' + ' '.join(fields))
        (d / 'cmdline').write_bytes(b'\0'.join(x.encode() for x in argv) + b'\0')

    def test_exact_owner_and_sandbox_child_without_foreign_argv(self):
        self.write(11, ['sandbox', 'private-child-data'], 10)
        result = m.census(self.scope, [self.owner], proc_root=self.root)
        self.assertTrue(result['complete_for_declared_scope'])
        self.assertFalse(result['arbitrary_network_client_absence_established'])
        text = json.dumps(result)
        self.assertNotIn('private-token', text)
        self.assertNotIn('private-child-data', text)
        self.assertEqual([r['pid'] for r in result['after']['related_process_tree']], [1, 10, 11])

    def test_extra_same_source_with_another_endpoint_refuses(self):
        self.write(12, ['python', '/code/self.py', '--endpoint', 'http://other'], 1)
        with self.assertRaisesRegex(ValueError, 'unexpected_or_missing'):
            m.census(self.scope, [self.owner], proc_root=self.root)

    def test_matching_descendant_is_not_exempt(self):
        self.write(12, ['python', '/code/actor.py'], 10)
        with self.assertRaisesRegex(ValueError, 'unexpected_or_missing'):
            m.census(self.scope, [self.owner], proc_root=self.root)

    def test_unknown_entrypoint_referencing_exact_request_refuses(self):
        self.write(12, ['python', '/unknown.py', '--request', json.dumps({'path': '/control/request.json'})], 1)
        with self.assertRaisesRegex(ValueError, 'unexpected_or_missing'):
            m.census(self.scope, [self.owner], proc_root=self.root)

    def test_output_boundary_and_embedded_code(self):
        self.assertEqual(m.matches(['python', '-c', "print('/code/actor.py')"], self.scope), [])
        self.assertEqual(m.matches(['--output=/runs/fixed-other'], self.scope), [])
        self.assertEqual(m.matches(['--output=/runs/fixed/subdir'], self.scope), ['output_root'])
        self.assertEqual(m.matches(['python', '-m', 'causes.actor'], self.scope), ['module'])

    def test_missing_reused_pid_or_changed_argv_refuses(self):
        for expected in [[], [{**self.owner, 'process_starttime_ticks': '99'}], [{**self.owner, 'argv': ['different']}]]:
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                m.census(self.scope, expected, proc_root=self.root)

    def test_second_scan_new_producer_refuses(self):
        original = m.snapshot
        count = 0
        def changed(scope, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                self.write(12, ['python', '/code/self.py'], 1)
            return original(scope, **kwargs)
        with patch.object(m, 'snapshot', changed), self.assertRaisesRegex(ValueError, 'unexpected_or_missing'):
            m.census(self.scope, [self.owner], proc_root=self.root)

    def test_reparented_owner_between_scans_refuses(self):
        first = m.snapshot(self.scope, proc_root=self.root)
        second = copy.deepcopy(first)
        second['matched'][0]['ppid'] = 2
        with patch.object(m, 'snapshot', side_effect=[first, second]), self.assertRaisesRegex(ValueError, 'parent_changed'):
            m.census(self.scope, [self.owner], proc_root=self.root)

    def test_still_running_owner_cannot_be_zombie(self):
        self.write(10, self.owner['argv'], 1, state='Z')
        with self.assertRaisesRegex(ValueError, 'unexpected_or_missing'):
            m.census(self.scope, [self.owner], proc_root=self.root)

    def test_unreadable_process_is_not_silently_excluded(self):
        original = Path.read_bytes
        def blocked(path):
            if path == self.root / '1' / 'cmdline':
                raise PermissionError('fixture')
            return original(path)
        with patch.object(Path, 'read_bytes', blocked), self.assertRaisesRegex(ValueError, 'unreadable_pid:1'):
            m.census(self.scope, [self.owner], proc_root=self.root)

    def loader(self, producer):
        def file(name, data):
            p = self.root / name
            p.write_bytes(data)
            return dict(path=str(p), bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        entry = file('loader.py', b'# synthetic loader\n')
        source = file('target.py', b'# synthetic target\n')
        self.scope['descriptor_entrypoints'] = [entry['path']]
        if producer:
            self.scope['entrypoints'] = sorted(self.scope['entrypoints'] + [source['path']])
        descriptor = file('descriptor.json', json.dumps(dict(schema='causes.pinned_native_source_entrypoint.v1', strategy='module', source=source, entrypoint=entry, native_argv=['python', source['path']])).encode())
        argv = ['python', entry['path'], '--descriptor', json.dumps(descriptor)]
        self.write(12, argv, 1)
        return descriptor

    def test_loader_target_is_resolved_and_extra_producer_refused(self):
        self.loader(True)
        with self.assertRaisesRegex(ValueError, 'unexpected_or_missing'):
            m.census(self.scope, [self.owner], proc_root=self.root)

    def test_unrelated_loader_target_is_explicit_and_byte_checked(self):
        descriptor = self.loader(False)
        result = m.census(self.scope, [self.owner], proc_root=self.root)
        self.assertEqual(result['after']['resolved_nonproducers'][0]['descriptor'], descriptor)
        (self.root / 'target.py').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'descriptor_bytes'):
            m.census(self.scope, [self.owner], proc_root=self.root)

    def test_unknown_or_missing_loader_descriptor_refuses(self):
        descriptor = self.loader(False)
        Path(descriptor['path']).unlink()
        with self.assertRaises(FileNotFoundError):
            m.census(self.scope, [self.owner], proc_root=self.root)


if __name__ == '__main__':
    unittest.main()
