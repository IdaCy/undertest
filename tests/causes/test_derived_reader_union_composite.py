import copy
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_derived_reader_composite as base_fixture

composite, fixture, readout = base_fixture.composite, base_fixture.fixture, base_fixture.readout


class InvalidCitationClient(fixture.FakeClient):
    def create(self, **payload):
        response = super().create(**payload)
        instrument = payload['text']['format']['schema']['properties']['instrument']['enum'][0]
        if instrument != 'strongly_favors':
            return response
        value = json.loads(response.read())
        parsed = json.loads(value['output'][0]['content'][0]['text'])
        parsed['judgments']['evaluation'] = dict(label='yes', reason='Synthetic unsupported quotation.',
            evidence=[dict(message_index=1, channel='reasoning', quote='This quote never appeared.')])
        value['output'][0]['content'][0]['text'] = json.dumps(parsed)
        raw = json.dumps(value).encode()
        return SimpleNamespace(read=lambda: raw, status_code=200, headers=response.headers)


class UnionCompositeTests(unittest.TestCase):
    def setUp(self):
        self.base = base_fixture.CompositeTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.root = self.base.root
        self.rows = readout.checked(self.base.treatment_prep['ready'])
        self.keys = [row['measurement_key'] for row in self.rows]
        self.old = self.root / 'stopped'
        self.tail = self.root / 'continuation'
        self.old.mkdir()
        self.tail.mkdir()
        self.invalid_key = self.keys[1]
        journals = [[], []]
        for i, row in enumerate(self.rows):
            root = self.old if i < 2 else self.tail
            key = row['measurement_key']
            if key == self.invalid_key:
                view = readout.inputs.prepare_reader(row, self.base.treatment_prep, self.base.base.config,
                                                      {'campaign_reader': readout.reader})
                readout.reader.run_context(InvalidCitationClient(self.base.base.quote), self.base.base.config,
                    self.base.treatment_prep['prompts_directory'], view['messages'], len(view['initial_context']),
                    root / key, initial_context=view['initial_context'])
                terminal = dict(event='unresolved_current_reader_attempt', key=key)
            else:
                shutil.copytree(self.base.treatment_root / key, root / key)
                terminal = next(event for event in self.base.treatment_journal
                                if event['key'] == key and event['event'] == 'finished')
            journals[int(i >= 2)].extend([dict(event='claimed', key=key), terminal])
        cuts = []
        for i, (root, journal) in enumerate(zip((self.old, self.tail), journals)):
            path = self.root / ('CUT' + str(i) + '.jsonl')
            path.write_bytes(b''.join(readout.encoded(event) for event in journal))
            cut = dict(root=str(root), journal=readout.pin(path), journal_original=readout.pin(path),
                plan=self.base.save('CUT_PLAN' + str(i) + '.json', {}),
                runtime_result=self.base.save('CUT_RESULT' + str(i) + '.json', {}),
                claimed_keys=[event['key'] for event in journal if event['event'] == 'claimed'])
            cuts.append(self.base.save('CUT' + str(i) + '.json', cut))
        dummy = self.base.save('BINDING.json', {})
        self.union = dict(schema='causes.derived_reader_closed_union.v1', source=readout.pin(composite.__file__),
            source_review=dummy, binding=dummy, binding_review=dummy, continuation_actual=cuts[1],
            continuation_review=dummy, stopped_actual=cuts[0],
            inputs=self.base.treatment['preparation'], inputs_review=self.base.treatment['input_review'],
            counts=self.base.treatment_prep['counts'], original_unresolved_key=self.invalid_key,
            original_missing_instruments=['strongly_favors'], qualification=dummy,
            context_locations=[dict(key=key, root=str(self.old if i < 2 else self.tail))
                               for i, key in enumerate(self.keys)])
        self.expected = copy.deepcopy(self.union)
        # The public continuation module has its own closure/repair tests. This
        # fixture isolates composition while still using native capture parsing.
        module = SimpleNamespace(closed_union=lambda *args: copy.deepcopy(self.expected))
        patcher = patch.object(composite, '_load_continuation', return_value=module)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.publish()

    def publish(self):
        reference = self.base.save('UNION.json', self.union)
        peer = self.base.save('UNION_REVIEW.json', dict(accepted_derived_reader_closed_union=True,
            union=reference, findings=[], **{key: self.union[key] for key in
            ('source', 'binding', 'binding_review', 'continuation_actual', 'continuation_review')}))
        self.base.selection['sources']['treatment'] = dict(self.base.treatment, closed=reference, closed_review=peer)
        self.base.publish_mapping()

    def run_readout(self):
        return self.base.run_composite()

    def test_native_union_preserves_unresolved_event_and_other_instruments(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(readout.reader, 'run_context', side_effect=AssertionError('no new requests')):
            result = self.run_readout()
        item = next(row for row in result['rows'] if row['measurement_key'] == self.invalid_key)
        self.assertEqual(item['state'], 'unresolved_current_reader_attempt')
        self.assertEqual(item['instruments']['presence']['label'], 'yes')
        self.assertEqual(item['instruments']['leans_towards_or_favors']['label'], 'yes')
        self.assertIsNone(item['instruments']['strongly_favors']['label'])
        self.assertEqual(result['counts']['assigned_arms'], 8)
        self.assertEqual(result['dispositions'], dict(finished=5, unresolved_current_reader_attempt=2, unavailable=1))
        self.assertEqual(result['model_calls'], 0)
        self.assertFalse(result['inference_released'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_union_rebuild_cannot_be_replaced_by_a_matching_peer(self):
        self.union['original_missing_instruments'] = ['presence']
        self.publish()
        with self.assertRaisesRegex(ValueError, 'closed_union_changed'):
            self.run_readout()

    def test_union_root_swap_rejects_before_reading_captures(self):
        self.union['context_locations'][0]['root'] = str(self.tail)
        self.expected = copy.deepcopy(self.union)
        self.publish()
        with patch.object(readout.evidence, 'verify_reader_capture', side_effect=AssertionError('early response read')):
            with self.assertRaisesRegex(ValueError, 'union_key_root_changed'):
                self.run_readout()

    def test_union_peer_join_must_match(self):
        source = self.base.selection['sources']['treatment']
        peer = readout.checked(source['closed_review'])
        peer['binding'] = source['preparation']
        source['closed_review'] = self.base.save('UNION_REVIEW.json', peer)
        self.base.publish_mapping()
        with self.assertRaisesRegex(ValueError, 'peer_join_changed'):
            self.run_readout()

    def test_reordered_union_membership_refuses(self):
        self.union['context_locations'].reverse()
        self.expected = copy.deepcopy(self.union)
        self.publish()
        with self.assertRaisesRegex(ValueError, 'union_location_roster_changed'):
            self.run_readout()

    def test_unqualified_instrument_cannot_be_recovered(self):
        self.union['original_missing_instruments'] = ['presence']
        self.expected = copy.deepcopy(self.union)
        self.publish()
        with self.assertRaisesRegex(ValueError, 'qualified_instrument_missingness_changed'):
            self.run_readout()

    def test_citation_response_corruption_refuses(self):
        path = self.old / self.invalid_key / 'strongly_favors/response.raw'
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'raw_response_hash'):
            self.run_readout()

    def test_original_journal_mutation_during_response_read_refuses(self):
        verify = readout.evidence.verify_reader_capture
        changed = []
        def mutate(*args):
            value = verify(*args)
            if not changed:
                cut = readout.checked(self.union['stopped_actual'])
                path = Path(cut['journal']['path'])
                path.write_bytes(path.read_bytes() + b'\n')
                changed.append(True)
            return value
        with patch.object(readout.evidence, 'verify_reader_capture', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'custody_changed_during_readout'):
                self.run_readout()


if __name__ == '__main__':
    unittest.main()
