import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from causes import campaign_analysis as analysis
from causes import campaign_reader as reader
from causes import campaign_reader_evidence as evidence
from causes import derived_reader_inputs as inputs

SOURCE = Path(__file__).resolve().parents[2] / 'src/causes/derived_reader_readout.py'
spec = importlib.util.spec_from_file_location('readout_under_test', SOURCE)
readout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readout)


class FakeClient:
    max_retries = 0
    base_url = 'https://api.openai.com/v1'

    def __init__(self, quote, failing=None):
        self.quote, self.failing = quote, failing
        self.responses = SimpleNamespace(with_raw_response=SimpleNamespace(create=self.create))

    def create(self, **payload):
        instrument = payload['text']['format']['schema']['properties']['instrument']['enum'][0]
        if instrument == self.failing:
            raise TimeoutError('synthetic transport failure')
        judgments = {name: dict(label='no', reason='No qualifying expression.', evidence=[]) for name in reader.TYPES}
        if instrument != 'strongly_favors':
            judgments['evaluation'] = dict(label='yes', reason='Explicit generated recognition.',
                evidence=[dict(message_index=1, channel='reasoning', quote=self.quote)])
        response = dict(id='fake-' + instrument, model='gpt-6-luna', status='completed',
            output=[dict(type='message', content=[dict(type='output_text',
                text=json.dumps(dict(instrument=instrument, judgments=judgments)))])])
        raw = json.dumps(response).encode()
        return SimpleNamespace(read=lambda: raw, status_code=200, headers={'x-request-id': 'fake-' + instrument})


class ReadoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.capture = self.root / 'captures'
        self.capture.mkdir()
        config = dict(model='gpt-6-luna', reasoning_effort='low', max_retries=0, store=False)
        self.config = config
        prompts = self.root / 'prompts'
        prompts.mkdir()
        refs = {}
        for name in ('rubric', *reader.INSTRUMENTS):
            path = prompts / (name + '.txt')
            path.write_text('Read generated evidence: ' + name)
            refs[name] = readout.pin(path)
        protocol = dict(schema=inputs.PROTOCOL_SCHEMA, checker_config=self.save('config.json', config),
            prompts=refs, prompts_directory=str(prompts), reader_source=readout.pin(reader.__file__))
        self.quote = 'This interaction is evaluating me as an AI.'
        initial = [dict(role='user', content='Fix the parser.')]
        view = dict(initial_context=initial, messages=initial + [dict(role='assistant', reasoning=self.quote, content='Inspecting.')],
                    phase='prefix', prefix_context=None)
        vr = dict(file=self.save('view.json', view), selector=[])
        provenance = [self.save('provenance.json', dict(private_arm_cue='Never sent to reader.'))]
        self.rows, assignments, pairs, members, journal = [], [], [], {}, []
        for task in ('repo__one-1', 'repo__two-2'):
            members[task] = dict(control=[], treatment=[])
            for draw in range(2):
                arms = {}
                for arm, target in (('baseline', 'control'), ('treatment', 'treatment')):
                    index = len(self.rows)
                    identity = dict(run_id='run-' + str(index), view_kind='neutral_v1', stage='early')
                    if index == 5:
                        row = inputs.unavailable_row(**identity, reason='saved actor unavailable', provenance=provenance, protocol=protocol)
                    else:
                        row = inputs.build_row(**identity, reference=vr, provenance=provenance, protocol=protocol)
                    self.rows.append(row)
                    assignments.append(identity)
                    key = row['measurement_key']
                    arms[arm] = dict(disposition=row['disposition'], measurement_key=key, run_id=row['run_id'])
                    members[task][target].append(key)
                    if index != 5:
                        journal.append(dict(event='claimed', key=key))
                        if index == 4:
                            journal.append(dict(event='unresolved_current_reader_attempt', key=key))
                        else:
                            result = reader.run_context(FakeClient(self.quote, 'strongly_favors' if index == 0 else None),
                                config, prompts, view['messages'], len(initial), self.capture / key, initial_context=initial)
                            journal.append(dict(event='finished', key=key,
                                instrument_statuses={k: v['status'] for k, v in result['instruments'].items()}))
                pairs.append(dict(task_id=task, draw_key=draw, variant='F07L05', arms=arms))
        self.journal = journal
        self.prep = dict(schema='causes.frozen_derived_reader_preparation.v1', source=readout.pin(inputs.__file__),
            assignments=self.save('ASSIGNMENTS.json', assignments),
            ready=self.save('READY.json', [r for r in self.rows if r['disposition'] == 'prepared']),
            unavailable=self.save('UNAVAILABLE.json', [r for r in self.rows if r['disposition'] == 'unavailable']),
            pairs=self.save('PAIRS.json', pairs), protocol=protocol, prompts_directory=str(prompts))
        self.specification = dict(schema='causes.frozen_derived_reader_readout_specification.v1',
            function='contrast', function_parameters=dict(release=False, family_size=10),
            inference_released=False, planned_family_size=10, separate_direct_self_report_required=True,
            measurement_type='independent_verbalized_awareness_reader', awareness_type='evaluation',
            thresholds=list(reader.INSTRUMENTS), analysis_source=readout.pin(analysis.__file__),
            assignment=dict(factor='F07L05__neutral_v1', members=members),
            counts=dict(assigned_arms=8, pairs=4, ready_arms=7, tasks=2, unavailable_arms=1),
            analysis_contract=dict(scope='Descriptive selected cohort only; no power claim.'),
            **{k:self.prep[k] for k in ('ready', 'unavailable', 'pairs')})
        self.publish()

    def save(self, name, value):
        path = self.root / name
        path.write_bytes(readout.encoded(value))
        return readout.pin(path)

    def publish(self):
        prep = self.save('PREPARATION.json', self.prep)
        sr = self.save('SPECIFICATION.json', self.specification)
        input_review = self.save('input_review.json', dict(accepted_frozen_derived_reader_inputs=True,
            preparation=prep, source=self.prep['source'], findings=[]))
        spec_review = self.save('spec_review.json', dict(accepted_frozen_derived_reader_readout_specification=True,
            specification=sr, preparation=prep, findings=[]))
        plan = self.save('PLAN.json', dict(schema='causes.derived_reader_plan.v1', prepared=prep,
            previous_cohort_claimed_keys=[], additive_ready=self.prep['ready']))
        path = self.root / 'JOURNAL.jsonl'
        path.write_bytes(b''.join(readout.encoded(row) for row in self.journal))
        counts = dict(assigned=8, ready=7, claimed=7, finished_or_unresolved=7, active=0, unclaimed=0, all_claims_accounted=True)
        actual = dict(schema='causes.finite_reader_route_closed_cut.v1', prepared=prep, root=str(self.capture),
            plan=plan, journal=readout.pin(path), claimed_keys=[r['measurement_key'] for r in self.rows if r['disposition']=='prepared'], **counts)
        closed = self.save('ACTUAL.json', actual)
        closed_review = self.save('closed_review.json', dict(accepted_derived_reader_batch_closed_cut=True,
            accepted_finite_reader_route_closed_cut=True, cohort_kind='derived', actual=closed,
            prepared=prep, root=actual['root'], plan=plan, journal=actual['journal'], findings=[], **counts))
        modules = dict(campaign_analysis=readout.pin(analysis.__file__), campaign_reader=readout.pin(reader.__file__),
            derived_reader_inputs=readout.pin(inputs.__file__), campaign_reader_evidence=readout.pin(evidence.__file__))
        source_review = self.save('source_review.json', dict(accepted_derived_reader_readout_source=True,
            source=readout.pin(SOURCE), modules=modules, findings=[]))
        self.args = dict(specification=sr, specification_review=spec_review, preparation=prep,
            input_review=input_review, closed=closed, closed_review=closed_review,
            source_review=source_review, evidence_source=modules['campaign_reader_evidence'])

    def run_readout(self):
        return readout.build_readout(**self.args)

    def test_native_roundtrip_keeps_all_assigned_missing_slots_and_independent_instruments(self):
        result = self.run_readout()
        self.assertEqual(result['dispositions'], dict(finished=6, unresolved_current_reader_attempt=1, unavailable=1))
        self.assertEqual(len(result['rows']), 8)
        for row in result['rows'][4:6]:
            self.assertTrue(all(x['label'] is None for x in row['instruments'].values()))
        self.assertEqual(result['rows'][0]['instruments']['presence']['label'], 'yes')
        self.assertIsNone(result['rows'][0]['instruments']['strongly_favors']['label'])
        self.assertEqual(result['rows'][1]['instruments']['strongly_favors']['label'], 'no')
        for contrast in result['contrasts'].values():
            self.assertEqual(contrast['assigned'], dict(control=4, treatment=4))
            self.assertIsNone(contrast['approximate_p'])
            self.assertFalse(contrast['inference_released'])
            self.assertIsNone(contrast['equal_task_effect'])
        self.assertEqual(result['contrasts']['presence']['missing_assigned_sample_effect_bounds'], [-.25,.25])
        self.assertEqual(result['repository_task_counts'], {'repo__one':1, 'repo__two':1})
        self.assertLess(len(readout.encoded(result)), 2*1024*1024)

    def test_readout_is_read_only(self):
        before = {str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.run_readout()
        self.assertEqual(before, {str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_missing_closed_peer_refuses_before_response_verification(self):
        peer = readout.checked(self.args['closed_review'])
        peer['accepted_derived_reader_batch_closed_cut'] = False
        self.args['closed_review'] = self.save('closed_review.json', peer)
        with patch.object(evidence, 'verify_reader_capture', side_effect=AssertionError('must not read responses')):
            with self.assertRaisesRegex(ValueError, 'accepted_peer_required'):
                self.run_readout()

    def test_source_dependency_join_is_mandatory_before_response_verification(self):
        peer = readout.checked(self.args['source_review'])
        del peer['modules']['campaign_reader_evidence']
        self.args['source_review'] = self.save('source_review.json', peer)
        with patch.object(evidence, 'verify_reader_capture', side_effect=AssertionError('must not read responses')):
            with self.assertRaisesRegex(ValueError, 'dependency_review_changed'):
                self.run_readout()

    def test_baseline_control_mapping_cannot_be_swapped(self):
        arms = self.specification['assignment']['members']['repo__one-1']
        arms['control'], arms['treatment'] = arms['treatment'], arms['control']
        self.publish()
        with self.assertRaisesRegex(ValueError, 'contrast_assignment_changed'):
            self.run_readout()

    def test_no_inference_release_or_direct_outcome_substitution(self):
        self.specification['function_parameters']['release'] = True
        self.publish()
        with self.assertRaisesRegex(ValueError, 'descriptive_independent_reader_contract'):
            self.run_readout()

    def test_duplicate_claim_or_terminal_and_foreign_claim_refuse(self):
        original = copy.deepcopy(self.journal)
        for event in (original[0], original[1], dict(event='claimed',key='foreign')):
            with self.subTest(event=event):
                self.journal = original + [event]
                self.publish()
                with self.assertRaises(ValueError):
                    self.run_readout()

    def test_instrument_statuses_must_match_reconstructed_native_result(self):
        self.journal[1]['instrument_statuses']['presence']='technical_missing'
        self.publish()
        with self.assertRaisesRegex(ValueError, 'native_terminal_status_changed'):
            self.run_readout()

    def test_raw_response_and_forged_parsed_label_are_rejected(self):
        key = self.rows[1]['measurement_key']
        path = self.capture/key/'presence/response.raw'
        path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError, 'raw_response_hash'):
            self.run_readout()

    def test_duplicate_json_key_in_pinned_specification_refuses(self):
        path = Path(self.args['specification']['path'])
        path.write_bytes(path.read_bytes().rstrip()[:-1]+b',"function":"contrast"}\n')
        self.args['specification']=readout.pin(path)
        with self.assertRaisesRegex(ValueError, 'duplicate_json_key'):
            self.run_readout()

    def test_capture_change_during_native_verification_refuses(self):
        original = evidence.verify_reader_capture
        def changed(destination,*args):
            result=original(destination,*args)
            (Path(destination)/'extra.json').write_text('{}')
            return result
        with patch.object(evidence,'verify_reader_capture',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'capture_changed_during_verification'):
                self.run_readout()

    def test_missing_output_byte_allowance_refuses(self):
        with patch.object(readout,'MAXIMUM_RESULT_BYTES',1024):
            with self.assertRaisesRegex(ValueError,'readout_output_bound'):
                self.run_readout()


if __name__ == '__main__':
    unittest.main()
