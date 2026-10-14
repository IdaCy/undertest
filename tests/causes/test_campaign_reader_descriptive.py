import copy
import unittest

from causes.campaign_descriptive import TYPES, THRESHOLDS, READER_NAMES
from causes.campaign_reader_descriptive import project_saved_reader_awareness


def fixture(factors=('F1', 'F2')):
    assignments, epochs, results = [], {}, {}
    for factor in factors:
        members = {}
        for task in ('task1', 'task2'):
            members[task] = {}
            for arm, prefix in (('treatment', factor), ('control', 'B')):
                ids = [f'{task}_{prefix}_{epoch}' for epoch in ('old', 'new')]
                members[task][arm] = ids
                epochs.update(zip(ids, ('old', 'new')))
        assignments.append({'factor': factor, 'members': members})
    rows = []
    for number, (rid, epoch) in enumerate(epochs.items()):
        ref = {'path': f'/saved/{rid}/RESULT.json', 'bytes': 100 + number,
               'sha256': f'{number:064x}'}
        instruments = {}
        for level in THRESHOLDS:
            label = 'yes' if rid.startswith('task1_F') else 'no'
            instruments[level] = {
                'instrument': level, 'status': 'validated',
                'judgments': {kind: {'label': label, 'admissible_label': label,
                                     'technical_missing_reasoning': False} for kind in TYPES}}
            rows.append({'run_id': rid, 'epoch': epoch, 'readout': 'reader_' + level,
                         'status': 'observed', 'label': label, 'reason': None,
                         'evidence': {'result': copy.deepcopy(ref)}})
        results[ref['path']] = {'schema': 'caus.reader_context.v1',
                               'independent_instrument_calls': True,
                               'threshold_results_inferred_from_other_calls': False,
                               'instruments': instruments}
    return {'assignments': assignments, 'epochs': epochs,
            'expected_factors': list(factors)}, rows, results


class ReaderDescriptiveTests(unittest.TestCase):
    def setUp(self):
        self.assignments, self.rows, self.results = fixture()
        self.loads = []

    def load(self, ref):
        self.loads.append(ref['path'])
        return self.results[ref['path']]

    def project(self, **kwargs):
        return project_saved_reader_awareness(self.assignments, iter(self.rows), self.load, **kwargs)

    def judgment(self, row, kind):
        level = row['readout'][7:]
        return self.results[row['evidence']['result']['path']]['instruments'][level]['judgments'][kind]

    def test_shared_controls_exact_membership_weights_epochs_and_no_inference(self):
        original = copy.deepcopy((self.assignments, self.rows, self.results))
        kinds = [kind for kind in TYPES if kind != 'evaluation']
        value = self.project(awareness_types=kinds)
        self.assertEqual((len(value['pooled']), len(value['epochs']), len(value['physical'])),
                         (36, 72, 18))
        self.assertEqual(len(self.loads), 12)
        self.assertEqual(len(set(self.loads)), 12)
        self.assertEqual(value['provenance']['physical_runs'], 12)
        for row in value['physical']:
            self.assertEqual(row['assigned'], 12)
            self.assertEqual(row['categories']['yes'], 4)
        for row in value['pooled'] + value['epochs']:
            self.assertEqual(row['task_count'], 2)
            self.assertEqual(row['equal_task_effect'], .5)
            self.assertEqual(row['missing_assigned_sample_effect_bounds'], [.5, .5])
            self.assertEqual(row['category_equal_task_weights']['treatment']['yes'], .5)
            self.assertFalse(any('p_value' in key or 'interval' in key or 'inference' in key
                                 or 'holm' in key or 'approximate' in key or key.startswith('se_')
                                 for key in row))
        self.assertEqual(original, (self.assignments, self.rows, self.results))

    def test_per_type_admissibility_does_not_copy_evaluation_missingness(self):
        row = self.rows[0]
        self.results[row['evidence']['result']['path']]['instruments']['presence']['status'] = \
            'validated_partial_observation'
        for kind, raw in [('evaluation', 'yes'), ('oversight', 'no'), ('simulation', 'yes')]:
            self.judgment(row, kind).update(label=raw, admissible_label=raw if raw == 'yes' else None,
                                           technical_missing_reasoning=True)
        row['label'] = 'yes'
        out = self.project(awareness_types=['oversight', 'simulation'], thresholds=['presence'])
        by = {x['awareness_type']: x for x in out['physical']}
        self.assertEqual(by['oversight']['observed'], 11)
        self.assertEqual(by['oversight']['dispositions']['missing_generated_actor_reasoning'], 1)
        self.assertEqual(by['simulation']['observed'], 12)

    def test_independent_thresholds_remain_nonmonotone(self):
        rid = self.rows[0]['run_id']
        for row in self.rows:
            if row['run_id'] == rid:
                raw = 'yes' if row['readout'] == 'reader_strongly_favors' else 'no'
                self.judgment(row, 'simulation').update(label=raw, admissible_label=raw)
        out = self.project(awareness_types=['simulation'])
        by = {x['threshold']: x['categories']['yes'] for x in out['physical']}
        self.assertEqual(by['strongly_favors'], by['presence'] + 1)
        self.assertEqual(by['presence'], by['leans_towards_or_favors'])

    def test_technical_instrument_failure_keeps_fixed_denominator(self):
        row = self.rows[0]
        native = self.results[row['evidence']['result']['path']]['instruments']['presence']
        native.update(status='technical_missing', judgments=None, technical_reason='saved_http_failure')
        row.update(status='missing', label=None, reason='saved_http_failure')
        out = self.project(awareness_types=['grading'], thresholds=['presence'])
        self.assertEqual(out['physical'][0]['assigned'], 12)
        self.assertEqual(out['physical'][0]['observed'], 11)
        self.assertEqual(out['physical'][0]['dispositions']['saved_http_failure'], 1)

    def test_unavailable_context_is_missing_for_every_type_without_load(self):
        rid = self.rows[0]['run_id']
        for row in self.rows:
            if row['run_id'] == rid:
                row.update(status='missing', label=None, reason='original_context_unavailable', evidence={})
        out = self.project()
        self.assertEqual(len(self.loads), 11)
        self.assertEqual(out['provenance']['unavailable_contexts'], 1)
        self.assertTrue(all(x['assigned'] == 12 and x['observed'] == 11 for x in out['physical']))

    def test_semantic_categories_are_not_technical_missing(self):
        for label in ('not directly interpretable as such', 'insufficient evidence to tell', 'not understandable'):
            with self.subTest(label=label):
                self.judgment(self.rows[0], 'grading').update(label=label, admissible_label=label)
                out = self.project(awareness_types=['grading'], thresholds=['presence'])
                self.assertEqual(out['physical'][0]['observed'], 12)
                self.assertEqual(out['physical'][0]['categories'][READER_NAMES[label]], 1)
                self.assertEqual(out['pooled'][0]['missing']['treatment'], 0)

    def test_all_five_categories_and_missingness_account_for_every_physical_run(self):
        presence = [row for row in self.rows if row['readout'] == 'reader_presence']
        for row, label in zip(presence, list(READER_NAMES) * 2 + [None, None]):
            self.judgment(row, 'grading').update(
                label=label or 'no', admissible_label=label,
                technical_missing_reasoning=label is None)
        out = self.project(awareness_types=['grading'], thresholds=['presence'])
        physical = out['physical'][0]
        self.assertEqual(physical['categories'], {
            'yes': 2, 'no': 2, 'not_directly_interpretable': 2,
            'insufficient_evidence': 2, 'not_understandable': 2})
        self.assertEqual(sum(physical['categories'].values()), physical['observed'])
        self.assertEqual(physical['observed'] +
                         physical['dispositions']['missing_generated_actor_reasoning'], 12)
        self.assertEqual(sum(physical['dispositions'].values()), 12)

    def test_fixed_thirty_one_contrast_membership_and_skipped_self_payloads(self):
        self.assignments, self.rows, self.results = fixture([f'F{i}' for i in range(31)])
        class UnreadableSelf(dict):
            def get(self, key, *args):
                if key != 'readout':
                    raise AssertionError('self_payload_read')
                return 'self_strongly_favors'
        self.rows.append(UnreadableSelf())
        kinds = [kind for kind in TYPES if kind != 'evaluation']
        out = self.project(awareness_types=kinds)
        self.assertEqual((len(out['pooled']), len(out['epochs']), len(out['physical'])),
                         (558, 1116, 18))
        self.assertEqual(out['provenance']['factors'], self.assignments['expected_factors'])
        self.assertEqual(len(self.loads), 128)

    def test_evaluation_crosscheck_refuses_changed_original(self):
        self.rows[0]['label'] = 'no'
        with self.assertRaisesRegex(ValueError, 'evaluation_custody_disagreement'):
            self.project()

    def test_changed_native_admissibility_refuses(self):
        self.judgment(self.rows[0], 'grading')['admissible_label'] = None
        with self.assertRaisesRegex(ValueError, 'native_admissibility_changed'):
            self.project()

    def test_present_aggregate_requires_terminal_instrument_record(self):
        row = self.rows[0]
        row.update(status='missing', label=None, reason='native_reader_technical_missing')
        for invalid in (None, [], 'missing'):
            with self.subTest(invalid=invalid):
                self.results[row['evidence']['result']['path']]['instruments']['presence'] = invalid
                with self.assertRaisesRegex(ValueError, 'terminal_instrument_record_required'):
                    self.project()

    def test_original_evaluation_status_and_reason_are_preserved(self):
        row = self.rows[0]
        row['reason'] = 'fabricated_observed_reason'
        with self.assertRaisesRegex(ValueError, 'evaluation_custody_reason_disagreement'):
            self.project()
        native = self.results[row['evidence']['result']['path']]['instruments']['presence']
        native.update(status='technical_missing', judgments=None, technical_reason='saved_http_failure')
        row.update(status='missing', label=None, reason='different_reason')
        with self.assertRaisesRegex(ValueError, 'evaluation_custody_reason_disagreement'):
            self.project()
        row['reason'] = 'saved_http_failure'
        self.project()
        self.assignments, self.rows, self.results = fixture()
        row = self.rows[0]
        native = self.results[row['evidence']['result']['path']]['instruments']['presence']
        native['status'] = 'validated_partial_observation'
        self.judgment(row, 'evaluation').update(
            label='no', admissible_label=None, technical_missing_reasoning=True)
        row.update(status='missing', label=None, reason='native_reader_technical_missing')
        with self.assertRaisesRegex(ValueError, 'native_evaluation_status_disagreement'):
            self.project()

    def test_duplicate_foreign_and_incomplete_slots_refuse(self):
        for mutation, reason in [('duplicate', 'duplicate_or_foreign'), ('foreign', 'duplicate_or_foreign'),
                                 ('missing', 'incomplete_fixed')]:
            with self.subTest(mutation=mutation):
                self.assignments, self.rows, self.results = fixture()
                if mutation == 'duplicate':
                    self.rows.append(copy.deepcopy(self.rows[0]))
                elif mutation == 'foreign':
                    self.rows[0]['run_id'] = 'foreign'
                else:
                    self.rows.pop()
                with self.assertRaisesRegex(ValueError, reason):
                    self.project()

    def test_result_reference_json_number_drift_refuses(self):
        self.rows[1]['evidence']['result']['bytes'] = float(self.rows[1]['evidence']['result']['bytes'])
        with self.assertRaisesRegex(ValueError, 'reader_result_reference_disagreement'):
            self.project()

    def test_epoch_member_or_unbalanced_epoch_refuses(self):
        self.assignments['epochs'][self.rows[0]['run_id']] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'invalid_terminal_reader_slot'):
            self.project()
        self.assignments, self.rows, self.results = fixture()
        for row in self.rows:
            if row['run_id'] == self.rows[0]['run_id']:
                row['epoch'] = 'new'
        self.assignments['epochs'][self.rows[0]['run_id']] = 'new'
        with self.assertRaisesRegex(ValueError, 'unbalanced_task_arm_assignment'):
            self.project()

    def test_unknown_or_duplicate_selections_and_factor_order_refuse(self):
        for kwargs in ({'awareness_types': ['evaluation', 'evaluation']},
                       {'awareness_types': ['new_type']}, {'thresholds': []}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.project(**kwargs)
        self.assignments['expected_factors'].reverse()
        with self.assertRaisesRegex(ValueError, 'frozen_factor_order_changed'):
            self.project()


if __name__ == '__main__':
    unittest.main()
