import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from causes import campaign_handoff as h
from causes import campaign_materializer_call as caller


class ClosureReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.local = h.save_new(self.root, 'retained.json', {'value': 3})
        self.original = {**self.local, 'path': str(self.root / 'absent-original.json')}

    def reader(self, rows):
        ref = h.save_new(self.root, 'custody.json', {
            'schema': caller.CUSTODY_SCHEMA, 'aliases': rows})
        return caller.ClosureReader(ref)

    def test_exact_copy_reads_without_creating_original(self):
        reader = self.reader([{'source': self.original, 'local': self.local}])
        self.assertEqual(reader(self.original), {'value': 3})
        self.assertFalse(Path(self.original['path']).exists())

    def test_present_but_undeclared_original_is_rejected(self):
        reader = self.reader([{'source': self.original, 'local': self.local}])
        other = h.save_new(self.root, 'other.json', {'value': 4})
        with self.assertRaisesRegex(ValueError, 'undeclared_closure_reference'):
            reader(other)

    def test_same_path_with_another_pin_is_rejected(self):
        reader = self.reader([{'source': self.original, 'local': self.local}])
        with self.assertRaisesRegex(ValueError, 'undeclared_closure_reference'):
            reader({**self.original, 'sha256': '0' * 64})

    def test_changed_retained_copy_is_rejected_at_read_and_finish(self):
        reader = self.reader([{'source': self.original, 'local': self.local}])
        Path(self.local['path']).write_text('{"value": 4}')
        with self.assertRaises(ValueError):
            reader(self.original)
        with self.assertRaises(ValueError):
            reader.verify()

    def test_duplicate_original_is_rejected(self):
        row = {'source': self.original, 'local': self.local}
        with self.assertRaisesRegex(ValueError, 'duplicate_custody_source'):
            self.reader([row, row])

    def test_content_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'custody_content_mismatch'):
            self.reader([{'source': {**self.original, 'bytes': 0}, 'local': self.local}])


class InvocationBoundaryTests(unittest.TestCase):
    def test_native_failure_is_called_once_and_preserved_without_replay(self):
        # The stub tests the call/receipt boundary, not scientific closure evidence.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = h.pin(caller.__file__)
            materializer = h.save_new(root, 'core.json', {'stub': True})
            binding = h.save_new(root, 'binding.json', {})
            preparation = h.save_new(root, 'preparation.json', {})
            inputs = {'delegated_adoption': None, 'binding': binding,
                      'preparation_review': preparation, 'choice': 'qualification',
                      'output_directory': str(root / 'native')}
            inputs_ref = h.save_new(root, 'inputs.json', inputs)
            request = {'materializer': materializer, 'inputs': inputs_ref,
                       'module_sources': {}, 'execution_directory': str(root / 'execution')}
            request_ref = h.save_new(root, 'request.json', request)
            review_ref = h.save_new(root, 'review.json', {
                'accepted_native_allocation_custody_caller': True,
                'request': request_ref, 'source': source, 'materializer': materializer})
            adoption_ref = h.save_new(root, 'adoption.json', {
                'root_authorized': True, 'action': 'one_native_allocation_materialization',
                'caller_request': request_ref, 'caller_review': review_ref,
                'caller_source': source, 'maximum_invocations': 1, 'automatic_retry': False,
                'inputs': binding, 'preparation_review': preparation, 'choice': 'qualification'})
            native_call = mock.Mock(side_effect=ValueError('native refusal'))
            native = types.SimpleNamespace(materialize=native_call)
            custody = types.SimpleNamespace(verify=mock.Mock())
            with mock.patch.object(caller, 'prepare', return_value=(request, inputs, custody, native, {})), \
                 mock.patch.object(caller, '_module_pins', return_value={}):
                with self.assertRaisesRegex(ValueError, 'native refusal'):
                    caller.run(request_ref, review_ref, adoption_ref)
                receipt = json.loads((root / 'execution/EXECUTION.json').read_bytes())
                self.assertEqual(receipt['native_invocations'], 1)
                self.assertFalse(receipt['completed'])
                self.assertEqual(receipt['exception']['message'], 'native refusal')
                self.assertEqual(native_call.call_args.kwargs, {'closure_reader': custody})
                self.assertEqual(native_call.call_args.args[0]['delegated_adoption'], adoption_ref)
                self.assertIsNone(inputs['delegated_adoption'])
                with self.assertRaises(Exception):
                    caller.run(request_ref, review_ref, adoption_ref)
                native_call.assert_called_once()


if __name__ == '__main__':
    unittest.main()
