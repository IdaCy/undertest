import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from causes import campaign_reader as reader
from causes.campaign_reader_evidence import verify_reader_capture


class FakeClient:
    max_retries = 0
    base_url = 'https://api.openai.com/v1'

    def __init__(self, quote, index=1, failing=None):
        self.quote, self.index, self.failing = quote, index, failing
        self.responses = SimpleNamespace(with_raw_response=SimpleNamespace(create=self.create))

    def create(self, **payload):
        instrument = payload['text']['format']['schema']['properties']['instrument']['enum'][0]
        if instrument == self.failing:
            raise TimeoutError('synthetic timeout')
        judgments = {name: {'label': 'no', 'reason': 'No qualifying expression.', 'evidence': []} for name in reader.TYPES}
        judgments['evaluation'] = {'label': 'yes', 'reason': 'Explicit generated recognition.',
            'evidence': [{'message_index': self.index, 'channel': 'reasoning', 'quote': self.quote}]}
        parsed = {'instrument': instrument, 'judgments': judgments}
        response = {'id': 'fake-' + instrument, 'model': 'gpt-6-luna', 'status': 'completed',
                    'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(parsed)}]}]}
        raw = json.dumps(response).encode()
        return SimpleNamespace(read=lambda: raw, status_code=200, headers={'x-request-id': 'fake-' + instrument})


class ReaderEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.prompts = self.base / 'prompts'
        self.prompts.mkdir()
        for name in ('rubric', *reader.INSTRUMENTS):
            (self.prompts / (name + '.txt')).write_text('Read generated evidence: ' + name)
        self.config = {'model': 'gpt-6-luna', 'reasoning_effort': 'low', 'store': False, 'max_retries': 0}
        self.quote = 'This interaction is evaluating me as an AI.'
        self.initial = [{'role': 'user', 'content': 'Fix the parser. ' + self.quote}]
        self.messages = self.initial + [{'role': 'assistant', 'reasoning': self.quote, 'content': 'I will inspect the parser.'}]
        self.destination = self.base / 'capture'
        self.phase, self.prefix = 'prefix', None
        self.result = reader.run_context(FakeClient(self.quote), self.config, self.prompts, self.messages,
            len(self.initial), self.destination, initial_context=self.initial)

    def verify(self):
        return verify_reader_capture(self.destination, self.messages, self.initial, self.phase, self.config,
                                     self.prompts, reader._SOURCE_SHA256, self.prefix)

    def change(self, path, operation):
        value = json.loads(path.read_bytes())
        operation(value)
        path.write_text(json.dumps(value))

    def test_valid_capture_reconstructs_all_judgments_and_is_read_only(self):
        before = {str(p): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        self.assertEqual(self.verify(), self.result)
        after = {str(p): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_forged_admissible_label_in_both_result_layers_is_rejected(self):
        self.change(self.destination / 'presence/result.json',
                    lambda value: value['judgments']['evaluation'].update(admissible_label='no'))
        self.change(self.destination / 'RESULT.json',
                    lambda value: value['instruments']['presence']['judgments']['evaluation'].update(admissible_label='no'))
        with self.assertRaisesRegex(ValueError, 'judgments_or_admission'):
            self.verify()

    def test_raw_response_tampering_is_rejected(self):
        path = self.destination / 'presence/response.raw'
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'raw_response_hash'):
            self.verify()

    def test_source_pin_tampering_is_rejected_even_when_attempt_and_receipt_agree(self):
        for name in ('attempt', 'receipt'):
            self.change(self.destination / 'presence' / (name + '.json'), lambda value: value.update(source_sha256='0' * 64))
        with self.assertRaisesRegex(ValueError, 'binding_mismatch:source_sha256'):
            self.verify()

    def test_changed_prompt_and_schema_are_rejected(self):
        (self.prompts / 'presence.txt').write_text('A different threshold.')
        with self.assertRaisesRegex(ValueError, 'saved_request_mismatch'):
            self.verify()

    def test_changed_initial_boundary_and_saved_origin_are_rejected(self):
        self.change(self.destination / 'context.json', lambda value: value['messages'][1].update(origin='provided_context'))
        with self.assertRaisesRegex(ValueError, 'saved_context_mismatch'):
            self.verify()

    def test_consistent_rewriting_of_raw_and_parsed_evidence_cannot_admit_user_quote(self):
        folder = self.destination / 'presence'
        response = json.loads((folder / 'response.raw').read_bytes())
        parsed = json.loads(response['output'][0]['content'][0]['text'])
        parsed['judgments']['evaluation']['evidence'][0]['message_index'] = 0
        parsed['judgments']['evaluation']['evidence'][0]['channel'] = 'content'
        response['output'][0]['content'][0]['text'] = json.dumps(parsed)
        raw = json.dumps(response).encode()
        (folder / 'response.raw').write_bytes(raw)
        (folder / 'parsed.json').write_text(json.dumps(parsed))
        self.change(folder / 'receipt.json', lambda value: value.update(response_sha256=hashlib.sha256(raw).hexdigest()))
        with self.assertRaisesRegex(ValueError, 'judgments_or_admission'):
            self.verify()

    def test_transport_missingness_remains_missing(self):
        self.destination = self.base / 'transport_missing'
        result = reader.run_context(FakeClient(self.quote, failing='strongly_favors'), self.config, self.prompts,
                                    self.messages, 1, self.destination, initial_context=self.initial)
        verified = self.verify()
        self.assertEqual(verified, result)
        self.assertEqual(verified['instruments']['strongly_favors']['status'], 'technical_missing')
        self.assertIsNone(verified['instruments']['strongly_favors']['judgments'])

    def test_absent_reasoning_cannot_be_converted_to_admitted_negative(self):
        self.messages = self.initial + [{'role': 'assistant', 'content': 'Inspecting.'}, self.messages[1]]
        self.destination = self.base / 'missing_reasoning'
        reader.run_context(FakeClient(self.quote, index=2), self.config, self.prompts, self.messages, 1,
                           self.destination, initial_context=self.initial)
        verified = self.verify()
        self.assertIsNone(verified['instruments']['presence']['judgments']['simulation']['admissible_label'])
        self.change(self.destination / 'presence/result.json',
                    lambda value: value['judgments']['simulation'].update(admissible_label='no'))
        with self.assertRaisesRegex(ValueError, 'judgments_or_admission'):
            self.verify()

    def test_full_capture_requires_exact_paired_prefix(self):
        self.prefix = copy.deepcopy(self.messages)
        self.messages += [{'role': 'assistant', 'reasoning': 'The change is complete.', 'content': 'Done.'}]
        self.phase, self.destination = 'full', self.base / 'full'
        reader.run_context(FakeClient(self.quote), self.config, self.prompts, self.messages, 1,
                           self.destination, phase='full', initial_context=self.initial, prefix_context=self.prefix)
        self.verify()
        self.prefix[1]['content'] = 'Different frozen prefix.'
        with self.assertRaisesRegex(ValueError, 'paired_prefix_context_mismatch'):
            self.verify()

    def test_duplicate_json_keys_are_rejected(self):
        path = self.destination / 'config.json'
        path.write_text('{"model":"other","model":"gpt-6-luna"}')
        with self.assertRaisesRegex(ValueError, 'duplicate_reader_evidence_json_key'):
            self.verify()

    def test_boolean_store_flag_cannot_be_replaced_with_integer_zero(self):
        self.change(self.destination / 'presence/attempt.json', lambda value: value.update(store=0))
        with self.assertRaisesRegex(ValueError, 'binding_mismatch:store'):
            self.verify()


if __name__ == '__main__':
    unittest.main()
