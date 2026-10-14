import copy
import importlib.util
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


readout = load('causes.derived_reader_readout', ROOT / 'src/causes/derived_reader_readout.py')
composite = load('composite_under_test', ROOT / 'src/causes/derived_reader_composite.py')
fixture = load('composite_native_fixture', ROOT / 'tests/causes/test_derived_reader_readout.py')


class CompositeTests(unittest.TestCase):
    def setUp(self):
        self.base = fixture.ReadoutTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.root = self.base.root
        self.prep = copy.deepcopy(self.base.prep)
        self.prep['counts'] = dict(assigned=8, ready=7, unavailable=1, requests=21)
        self.base.prep = self.prep
        self.base.publish()
        self.args = {key: self.base.args[key] for key in
                     ('specification', 'specification_review', 'preparation', 'input_review', 'evidence_source')}
        self.global_rows = self.base.rows
        self.sources = {'baseline': {key: self.base.args[key] for key in composite.SOURCE_FIELDS}}
        self.treatment = self.make_treatment_source()
        self.sources['treatment'] = self.treatment
        self.selection = dict(schema=composite.MAPPING_SCHEMA, specification=self.args['specification'],
            preparation=self.args['preparation'], sources=self.sources,
            rows=[dict(measurement_key=row['measurement_key'], source='baseline' if index % 2 == 0 else 'treatment')
                  for index, row in enumerate(self.global_rows) if row['disposition'] == 'prepared'])
        self.publish_mapping()
        modules = dict(readout.checked(self.base.args['source_review'])['modules'],
                       derived_reader_readout=readout.pin(readout.__file__))
        self.args['source_review'] = self.save('COMPOSITE_SOURCE_REVIEW.json', dict(
            accepted_derived_reader_composite_source=True, source=readout.pin(composite.__file__),
            modules=modules, findings=[]))

    def save(self, name, value):
        return self.base.save(name, value)

    def make_treatment_source(self):
        rows = self.global_rows[1::2]
        prep = copy.deepcopy(self.prep)
        prep.update(assignments=self.save('T_ASSIGNMENTS.json',
            [{key: row[key] for key in readout.IDENTITY} for row in rows]),
            ready=self.save('T_READY.json', [row for row in rows if row['disposition'] == 'prepared']),
            unavailable=self.save('T_UNAVAILABLE.json', [row for row in rows if row['disposition'] == 'unavailable']),
            counts=dict(assigned=4, ready=3, unavailable=1, requests=9))
        self.treatment_prep = prep
        keys = {row['measurement_key'] for row in rows}
        self.treatment_journal = [event for event in self.base.journal if event['key'] in keys]
        root = self.root / 'treatment_captures'
        root.mkdir()
        for row in rows:
            old = self.base.capture / row['measurement_key']
            if old.exists():
                shutil.copytree(old, root / row['measurement_key'])
        self.treatment_root = root
        return self.publish_treatment()

    def publish_treatment(self):
        prep = self.save('T_PREPARATION.json', self.treatment_prep)
        ip = self.save('T_INPUT_REVIEW.json', dict(accepted_frozen_derived_reader_inputs=True,
            preparation=prep, source=self.treatment_prep['source'], findings=[]))
        plan = self.save('T_PLAN.json', dict(schema='causes.derived_reader_plan.v1', prepared=prep,
            previous_cohort_claimed_keys=[], additive_ready=self.treatment_prep['ready']))
        journal = self.root / 'T_JOURNAL.jsonl'
        journal.write_bytes(b''.join(readout.encoded(row) for row in self.treatment_journal))
        counts = dict(assigned=4, ready=3, claimed=3, finished_or_unresolved=3,
                      active=0, unclaimed=0, all_claims_accounted=True)
        actual = dict(schema='causes.finite_reader_route_closed_cut.v1', prepared=prep,
            root=str(self.treatment_root), plan=plan, journal=readout.pin(journal),
            claimed_keys=[event['key'] for event in self.treatment_journal if event['event'] == 'claimed'], **counts)
        closed = self.save('T_ACTUAL.json', actual)
        cp = self.save('T_CLOSED_REVIEW.json', dict(accepted_derived_reader_batch_closed_cut=True,
            accepted_finite_reader_route_closed_cut=True, cohort_kind='derived', actual=closed,
            prepared=prep, root=actual['root'], plan=plan, journal=actual['journal'], findings=[], **counts))
        return dict(preparation=prep, input_review=ip, closed=closed, closed_review=cp)

    def publish_mapping(self):
        mapping = self.save('MAPPING.json', self.selection)
        self.args['mapping'] = mapping
        self.args['mapping_review'] = self.save('MAPPING_REVIEW.json', dict(
            accepted_derived_reader_composite_mapping=True, mapping=mapping,
            specification=self.args['specification'], preparation=self.args['preparation'], findings=[]))

    def run_composite(self):
        return composite.build_composite_readout(**self.args)

    def test_native_treatment_only_and_prior_baseline_roundtrip(self):
        reference = self.base.run_readout()
        before = {str(path): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with patch.object(readout.reader, 'run_context', side_effect=AssertionError('must not issue requests')):
            result = self.run_composite()
        self.assertEqual(result['contrasts'], reference['contrasts'])
        self.assertEqual(result['dispositions'], dict(finished=6, unresolved_current_reader_attempt=1, unavailable=1))
        self.assertEqual(result['source_usage'], dict(baseline=4, treatment=3))
        self.assertEqual(len(result['rows']), 8)
        self.assertEqual(result['rows'][0]['instruments']['presence']['label'], 'yes')
        self.assertIsNone(result['rows'][0]['instruments']['strongly_favors']['label'])
        self.assertEqual(result['rows'][1]['instruments']['strongly_favors']['label'], 'no')
        self.assertTrue(all(label['label'] is None for row in result['rows'][4:6]
                            for label in row['instruments'].values()))
        self.assertEqual(result['contrasts']['presence']['assigned'], dict(control=4, treatment=4))
        self.assertEqual(result['contrasts']['presence']['missing_assigned_sample_effect_bounds'], [-.25, .25])
        self.assertFalse(result['inference_released'])
        self.assertFalse(result['direct_self_report_included'])
        self.assertIsNone(result['contrasts']['presence']['approximate_p'])
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.root.rglob('*') if path.is_file()})

    def test_only_selected_prior_captures_are_read(self):
        seen = []
        original = readout.evidence.verify_reader_capture
        def check(path, *args):
            seen.append(Path(path))
            return original(path, *args)
        with patch.object(readout.evidence, 'verify_reader_capture', side_effect=check):
            self.run_composite()
        self.assertEqual(len(seen), 6)
        self.assertTrue(all(path.parent == self.treatment_root for path in seen if
            path.name in {r['measurement_key'] for r in self.global_rows[1::2]}))

    def test_entirely_missing_assignment_needs_no_fabricated_source(self):
        rows = [readout.inputs.unavailable_row(row['run_id'], row['view_kind'], row['stage'],
            'saved actor unavailable', row['provenance'], self.prep['protocol']) for row in self.global_rows]
        by_run = {row['run_id']: row for row in rows}
        pairs = readout.checked(self.prep['pairs'])
        members = {}
        for pair in pairs:
            arms = members.setdefault(pair['task_id'], dict(control=[], treatment=[]))
            for arm, target in [('baseline', 'control'), ('treatment', 'treatment')]:
                row = by_run[pair['arms'][arm]['run_id']]
                pair['arms'][arm].update(measurement_key=row['measurement_key'], disposition='unavailable')
                arms[target].append(row['measurement_key'])
        self.prep.update(ready=self.save('ALL_MISSING_READY.json', []),
            unavailable=self.save('ALL_MISSING_UNAVAILABLE.json', rows),
            pairs=self.save('ALL_MISSING_PAIRS.json', pairs),
            counts=dict(assigned=8, ready=0, unavailable=8, requests=0))
        self.base.specification.update(**{k:self.prep[k] for k in ('ready', 'unavailable', 'pairs')},
            counts=dict(assigned_arms=8, pairs=4, ready_arms=0, tasks=2, unavailable_arms=8))
        self.base.specification['assignment']['members'] = members
        self.base.publish()
        self.args.update({key:self.base.args[key] for key in
                         ('specification', 'specification_review', 'preparation', 'input_review')})
        self.selection.update(specification=self.args['specification'], preparation=self.args['preparation'],
                              sources={}, rows=[])
        self.publish_mapping()
        with patch.object(readout.evidence, 'verify_reader_capture', side_effect=AssertionError('no capture exists')):
            result = self.run_composite()
        self.assertEqual(result['dispositions'], dict(unavailable=8))
        self.assertEqual(result['source_usage'], {})
        self.assertEqual(result['contrasts']['presence']['missing_assigned_sample_effect_bounds'], [-1, 1])
        self.assertEqual(result['contrasts']['presence']['assigned'], dict(control=4, treatment=4))

    def test_mapping_missing_duplicate_foreign_reordered_or_unavailable_key_refuses(self):
        original = copy.deepcopy(self.selection['rows'])
        mutations = [original[:-1], original + [original[0]],
            [dict(original[0], measurement_key='foreign'), *original[1:]], list(reversed(original)),
            original + [dict(measurement_key=self.global_rows[5]['measurement_key'], source='treatment')]]
        for rows in mutations:
            with self.subTest(rows=rows):
                self.selection['rows'] = rows
                self.publish_mapping()
                with patch.object(readout.evidence, 'verify_reader_capture', side_effect=AssertionError('early response read')):
                    with self.assertRaisesRegex(ValueError, 'composite_mapping_not_exact_ready_order'):
                        self.run_composite()

    def test_mapping_peer_exact_scope_and_source_dependency_required(self):
        for target, field in [('mapping_review', 'preparation'), ('source_review', 'modules')]:
            original = self.args[target]
            peer = readout.checked(original)
            peer.pop(field)
            self.args[target] = self.save('bad_' + target + '.json', peer)
            with patch.object(readout.evidence, 'verify_reader_capture', side_effect=AssertionError('early response read')):
                with self.assertRaises(ValueError):
                    self.run_composite()
            self.args[target] = original

    def test_source_input_or_closed_peer_cannot_be_substituted(self):
        for field in ('input_review', 'closed_review'):
            original = self.selection['sources']['treatment'][field]
            self.selection['sources']['treatment'][field] = self.sources['baseline'][field]
            self.publish_mapping()
            with self.assertRaisesRegex(ValueError, 'peer_join_changed'):
                self.run_composite()
            self.selection['sources']['treatment'][field] = original

    def test_unselected_source_and_unknown_source_refuse(self):
        self.selection['sources']['unused'] = copy.deepcopy(self.sources['baseline'])
        self.publish_mapping()
        with self.assertRaisesRegex(ValueError, 'source_set_changed'):
            self.run_composite()
        del self.selection['sources']['unused']
        self.selection['rows'][0]['source'] = 'unknown'
        self.publish_mapping()
        with self.assertRaisesRegex(ValueError, 'source_set_changed'):
            self.run_composite()

    def test_selected_key_must_exist_in_chosen_source(self):
        self.selection['rows'][0]['source'] = 'treatment'
        self.publish_mapping()
        with self.assertRaisesRegex(ValueError, 'selected_source_key_missing'):
            self.run_composite()

    def test_same_run_with_changed_view_payload_or_protocol_is_not_reusable(self):
        ready = readout.checked(self.treatment_prep['ready'])
        for field in ('view_canonical_sha256', 'context_canonical_sha256', 'targets_sha256',
                      'protocol_sha256', 'payloads'):
            changed = copy.deepcopy(ready)
            changed[0][field] = {} if field == 'payloads' else 'f' * 64
            self.treatment_prep['ready'] = self.save('T_READY.json', changed)
            self.selection['sources']['treatment'] = self.publish_treatment()
            self.publish_mapping()
            with patch.object(readout.evidence, 'verify_reader_capture', side_effect=AssertionError('early response read')):
                with self.assertRaisesRegex(ValueError, 'selected_request_identity_changed'):
                    self.run_composite()

    def test_full_view_rebuild_rejects_forged_matching_digest_fields(self):
        ready = readout.checked(self.treatment_prep['ready'])
        view = readout.inputs.read_view(ready[0]['view'])
        view['messages'][-1]['content'] = 'Changed actor tail.'
        ready[0]['view'] = dict(file=self.save('changed_view.json', view), selector=[])
        self.treatment_prep['ready'] = self.save('T_READY.json', ready)
        self.selection['sources']['treatment'] = self.publish_treatment()
        self.publish_mapping()
        with self.assertRaisesRegex(ValueError, 'derived_reader_row_changed'):
            self.run_composite()

    def test_equal_view_from_distinct_provenance_file_is_reusable(self):
        ready = readout.checked(self.treatment_prep['ready'])
        ready[0]['view'] = dict(file=self.save('same_view.json', readout.inputs.read_view(ready[0]['view'])), selector=[])
        ready[0]['provenance'] = [self.save('second_derivation.json', {'exact_saved_derivation': True})]
        self.treatment_prep['ready'] = self.save('T_READY.json', ready)
        self.selection['sources']['treatment'] = self.publish_treatment()
        self.publish_mapping()
        self.assertEqual(self.run_composite()['source_usage']['treatment'], 3)

    def test_bad_source_journal_counts_and_native_status_refuse(self):
        original = copy.deepcopy(self.treatment_journal)
        self.treatment_journal.append(original[0])
        self.selection['sources']['treatment'] = self.publish_treatment()
        self.publish_mapping()
        with self.assertRaisesRegex(ValueError, 'source_duplicate_claim'):
            self.run_composite()
        self.treatment_journal = original
        self.treatment_journal[1]['instrument_statuses']['presence'] = 'technical_missing'
        self.selection['sources']['treatment'] = self.publish_treatment()
        self.publish_mapping()
        with self.assertRaisesRegex(ValueError, 'native_terminal_status_changed'):
            self.run_composite()

    def test_source_protocol_change_refuses_before_response_reads(self):
        config = readout.checked(self.treatment_prep['protocol']['checker_config'])
        config['model'] = 'a-different-reader'
        self.treatment_prep['protocol']['checker_config'] = self.save('different_config.json', config)
        self.selection['sources']['treatment'] = self.publish_treatment()
        self.publish_mapping()
        with patch.object(readout.evidence, 'verify_reader_capture', side_effect=AssertionError('early response read')):
            with self.assertRaisesRegex(ValueError, 'source_protocol_changed'):
                self.run_composite()

    def test_raw_response_corruption_refuses(self):
        key = self.global_rows[1]['measurement_key']
        path = self.treatment_root / key / 'presence/response.raw'
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'raw_response_hash'):
            self.run_composite()

    def test_source_metadata_change_during_capture_verification_refuses(self):
        original = readout.evidence.verify_reader_capture
        done = []
        def changed(path, *args):
            value = original(path, *args)
            if not done:
                Path(self.treatment['preparation']['path']).write_bytes(b'{}')
                done.append(True)
            return value
        with patch.object(readout.evidence, 'verify_reader_capture', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'composite_custody_changed_during_readout'):
                self.run_composite()

    def test_mapping_does_not_permit_arm_swap_or_confirmatory_release(self):
        for mutate in ('swap', 'release'):
            spec = copy.deepcopy(self.base.specification)
            if mutate == 'swap':
                arms = spec['assignment']['members']['repo__one-1']
                arms['control'], arms['treatment'] = arms['treatment'], arms['control']
            else:
                spec['function_parameters']['release'] = True
            self.args['specification'] = self.save('BAD_SPEC.json', spec)
            self.args['specification_review'] = self.save('BAD_SPEC_REVIEW.json', dict(
                accepted_frozen_derived_reader_readout_specification=True, specification=self.args['specification'],
                preparation=self.args['preparation'], findings=[]))
            self.selection['specification'] = self.args['specification']
            self.publish_mapping()
            with self.assertRaises(ValueError):
                self.run_composite()

    def test_output_size_bound(self):
        with patch.object(composite, 'MAXIMUM_RESULT_BYTES', 1024):
            with self.assertRaisesRegex(ValueError, 'composite_readout_output_bound'):
                self.run_composite()


if __name__ == '__main__':
    unittest.main()
