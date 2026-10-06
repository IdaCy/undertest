import copy
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from signals.reasoning.checkpoint_probes import awareness_v3, judgment_validation
from signals.validation.recognition_measurement.graded_tables import table
from emergence.checkpoints import run_probe_battery as worker

FIXTURES = Path(__file__).resolve().parents[2] / 'data/fixtures/checkpoint_awareness_v3'


class V3ValidationTests(unittest.TestCase):
    def setUp(self):
        self.packet = (FIXTURES / 'source_packet.json').read_bytes()
        self.schema = json.loads((FIXTURES / 'schema.json').read_bytes())
        self.response = next(iter(json.loads((FIXTURES / 'mock_script.json').read_text()).values()))[0]
        self.labels = json.loads(self.response['output'][0]['content'][0]['text'])

    def validate(self, labels):
        return awareness_v3.validate_submission(self.packet, json.dumps(labels).encode(), self.schema)

    def test_synthetic_packet_accepts_without_claiming_runtime_evidence(self):
        result = self.validate(self.labels)
        self.assertTrue(result)
        self.assertTrue(self.labels['source_only'])
        self.assertFalse(self.labels['cases'][0]['coverage']['actual_window_complete'])

    def test_fabricated_quote_and_packet_hash_refuse(self):
        labels = copy.deepcopy(self.labels)
        labels['cases'][0]['propositions'][0]['literal'] = 'fabricated quotation'
        with self.assertRaises(ValueError):
            self.validate(labels)
        labels = copy.deepcopy(self.labels)
        labels['packet_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            self.validate(labels)

    def test_adapter_checks_original_packet_bytes_before_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'packet.json'
            path.write_bytes(self.packet)
            metadata = {key: self.labels[key] for key in ('packet_sha256', 'view', 'source_only')}
            row = {'task_kind': 'awareness_v3', 'local_validation': {'packet_path': str(path), 'packet_sha256': hashlib.sha256(self.packet).hexdigest()}, 'judge_input': {'packet': json.loads(self.packet), 'metadata': metadata}}
            self.assertIn('preserved_v3', judgment_validation.validate(row, self.labels, self.schema))
            path.write_bytes(self.packet + b'\n')
            with self.assertRaisesRegex(ValueError, 'local_packet_hash_mismatch'):
                judgment_validation.validate(row, self.labels, self.schema)

    def test_mock_worker_persists_once_and_keeps_local_reference_out_of_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            packet = directory / 'packet.json'
            packet.write_bytes(self.packet)
            row = {'original_id': 'synthetic-v3-contract-example', 'branch_id': None, 'task_kind': 'awareness_v3',
                   'local_validation': {'packet_path': str(packet), 'packet_sha256': hashlib.sha256(self.packet).hexdigest()},
                   'judge_input': {'packet': json.loads(self.packet), 'metadata': {key: self.labels[key] for key in ('packet_sha256', 'view', 'source_only')}}}
            source = directory / 'inputs.jsonl'
            source.write_text(json.dumps(row) + '\n')
            script = {row['original_id']: [self.response]}
            config = worker.Config('mock-model-v1', 'synthetic-contract-v1', mode='mock', passes=1, concurrency=1, initial_concurrency=1)
            transport = worker.MockTransport(script)
            result = asyncio.run(worker.run(config, source, directory / 'output', 'Synthetic validator test.', self.schema, transport))
            self.assertEqual(result['statuses'], {'completed': 1})
            self.assertEqual(len(transport.calls), 1)
            self.assertNotIn(str(packet), json.dumps(transport.calls[0][2]))
            again = worker.MockTransport(script)
            result = asyncio.run(worker.run(config, source, directory / 'output', 'Synthetic validator test.', self.schema, again))
            self.assertEqual(result['statuses'], {'completed': 1})
            self.assertEqual(again.calls, [])


class GradedMissingnessTests(unittest.TestCase):
    def test_unavailable_is_never_graded_negative(self):
        result = table([('yes', 'leans_evaluation'), ('unavailable', 'unissued')])
        comparison = result['crosswalks']['either_evaluation_favoring']
        self.assertEqual(comparison['assigned_pairs'], 2)
        self.assertEqual(comparison['complete_pairs'], 1)
        self.assertEqual(comparison['assigned_difference_identification_bounds'], [-.5, .5])
