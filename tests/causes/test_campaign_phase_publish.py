import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_handoff as h
from causes import campaign_phase_publish as publisher


class AtomicPhasePointerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pointer = self.root / 'CURRENT.json'
        self.records = self.root / 'records'
        self.records.mkdir()

    def expected(self):
        return {'pin': h.pin(self.pointer), 'stat': h.stat_identity(self.pointer)}

    def test_first_publication_is_complete_and_exclusive(self):
        value = {'phase': 'first'}
        result = publisher.atomic_pointer(self.pointer, value, None, self.records, lambda: None)
        self.assertEqual(json.loads(self.pointer.read_bytes()), value)
        self.assertEqual(h.checked_bytes(result['candidate']), self.pointer.read_bytes())
        other = self.root / 'other'
        other.mkdir()
        with self.assertRaisesRegex(ValueError, 'already_exists'):
            publisher.atomic_pointer(self.pointer, {'phase': 'second'}, None, other, lambda: None)
        self.assertEqual(json.loads(self.pointer.read_bytes()), value)

    def test_replacement_preserves_exact_previous_bytes(self):
        original = b'{ "phase" : "first" }\n'
        self.pointer.write_bytes(original)
        result = publisher.atomic_pointer(self.pointer, {'phase': 'second'}, self.expected(),
                                          self.records, lambda: None)
        self.assertEqual(h.checked_bytes(result['preserved_previous']), original)
        self.assertEqual(json.loads(self.pointer.read_bytes()), {'phase': 'second'})
        self.assertTrue(Path(result['candidate']['path']).is_file())

    def test_equal_content_new_inode_is_not_the_owned_predecessor(self):
        self.pointer.write_bytes(b'{}\n')
        expected = self.expected()
        replacement = self.root / 'replacement'
        replacement.write_bytes(self.pointer.read_bytes())
        replacement.replace(self.pointer)
        with self.assertRaisesRegex(ValueError, 'identity_changed'):
            publisher.atomic_pointer(self.pointer, {'new': 1}, expected, self.records, lambda: None)
        self.assertEqual(self.pointer.read_bytes(), b'{}\n')

    def test_revocation_preserves_pointer_and_candidate(self):
        self.pointer.write_bytes(b'{"old":1}\n')
        expected = self.expected()
        def revoked():
            raise ValueError('revoked')
        with self.assertRaisesRegex(ValueError, 'revoked'):
            publisher.atomic_pointer(self.pointer, {'new': 1}, expected, self.records, revoked)
        self.assertEqual(h.pin(self.pointer), expected['pin'])
        self.assertTrue((self.records / 'NEXT.pointer.json').is_file())
        self.assertTrue((self.records / 'PREVIOUS.pointer.json').is_file())

    def test_changed_pointer_during_authority_check_is_not_overwritten(self):
        self.pointer.write_bytes(b'{"old":1}\n')
        expected = self.expected()
        def changed():
            self.pointer.write_bytes(b'{"foreign":1}\n')
        with self.assertRaisesRegex(ValueError, 'identity_changed'):
            publisher.atomic_pointer(self.pointer, {'new': 1}, expected, self.records, changed)
        self.assertEqual(self.pointer.read_bytes(), b'{"foreign":1}\n')

    def test_symlink_pointer_rejected_without_touching_target(self):
        target = self.root / 'target'
        target.write_bytes(b'{}')
        self.pointer.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'noncanonical'):
            publisher.atomic_pointer(self.pointer, {'new': 1}, None, self.records, lambda: None)
        self.assertEqual(target.read_bytes(), b'{}')

    def test_unregistered_output_refuses_before_publication(self):
        with self.assertRaisesRegex(ValueError, 'owner_output_changed'):
            publisher.validate_candidate_owners({'owners': {'old_mi': {'output_root': '/wrong'}}},
                                                {'outputs': {'old_mi': '/expected'}})
        self.assertFalse(self.pointer.exists())

    def test_new_owner_receipt_must_bind_exact_process_and_storage(self):
        owner = {'output_root': '/expected', 'process': {'pid': 1}, 'controller_id': 'c',
                 'guard': {'binding': {'device': 1}, 'directory': '/guard', 'reporting_lock': {'ino': 1}}}
        receipt = {'process': owner['process'], 'controller_id': 'c',
                   'binding': {'output': '/expected', 'storage_binding': owner['guard']['binding']},
                   'guard_directory': '/guard', 'guard_reporting_lock': {'ino': 1}}
        owner['launch_owner_receipt'] = h.save_new(self.records, 'OWNER.json', receipt)
        registration = {'owners': {'new_texas': owner}}
        publisher.validate_candidate_owners(registration, {'outputs': {'new_texas': '/expected'}})
        owner['process'] = {'pid': 2}
        with self.assertRaisesRegex(ValueError, 'owner_receipt_changed'):
            publisher.validate_candidate_owners(registration, {'outputs': {'new_texas': '/expected'}})
        self.assertFalse(self.pointer.exists())


if __name__ == '__main__':
    unittest.main()
