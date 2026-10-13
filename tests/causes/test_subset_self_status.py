import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).parents[2] / 'src/causes/campaign_subset_self_status.py'
SPEC = importlib.util.spec_from_file_location('self_status_under_test', SOURCE)
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


class SelfStatusAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.serial = 0
        self.prep = self.put({'source': capture.pin(SOURCE), 'durable_source': {}})
        self.peer = self.put(dict(
            accepted_subset_self_status_capture_preparation=True,
            preparation=self.prep, source=capture.pin(SOURCE), findings=[]))
        self.request = dict(schema='causes.subset_self_status_request.v1',
                            authorized=True, maximum_captures=1, automatic_retry=False,
                            source_preparation=self.prep,
                            source_preparation_review=self.peer,
                            feed_directory=str(self.base), output=str(self.base/'cut'))

    def put(self, value):
        self.serial += 1
        path = self.base / f'{self.serial}.json'
        path.write_text(json.dumps(value))
        return capture.pin(path)

    def refuse_before_native_load(self):
        with patch.object(capture, 'load', side_effect=RuntimeError('native import forbidden')) as load:
            with self.assertRaises(AssertionError):
                capture.capture(self.put(self.request))
            load.assert_not_called()
        self.assertFalse((self.base/'cut').exists())

    def test_authority_false(self):
        self.request['authorized'] = False
        self.refuse_before_native_load()

    def test_retry_refused(self):
        self.request['automatic_retry'] = True
        self.refuse_before_native_load()

    def test_multiple_capture_budget_refused(self):
        self.request['maximum_captures'] = 2
        self.refuse_before_native_load()

    def test_output_cannot_escape_feed(self):
        self.request['output'] = str(self.base.parent/'elsewhere')
        self.refuse_before_native_load()

    def test_wrong_source_peer_refused(self):
        peer = json.loads(Path(self.peer['path']).read_bytes())
        peer['source'] = dict(capture.pin(SOURCE), sha256='0'*64)
        self.request['source_preparation_review'] = self.put(peer)
        self.refuse_before_native_load()

    def test_changed_request_pin_refused(self):
        ref = self.put(self.request)
        Path(ref['path']).write_text('{}')
        with patch.object(capture, 'load') as load:
            with self.assertRaises(AssertionError):
                capture.capture(ref)
            load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
