import copy
import hashlib
import itertools
import json
import math
from pathlib import Path
import random
import tempfile
import unittest

from causes import campaign_reader as reader
from causes import campaign_analysis as analysis
from causes import system_prompt_prefix_readout as prefix
from causes.task_cluster_intervals import summarize_task_bounds


def pin(path):
    raw = path.read_bytes()
    return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def save(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + '\n')
    return pin(path)


def pair_rows(task, pair, control, treatment):
    rows = []
    for arm, label in [('SP00', control), ('SP01', treatment)]:
        rows.append(dict(pair_id=pair, task_id=task, seed=9, repetition=0,
                         historical_parent_id='parent-' + pair, arm=arm,
                         run_id=pair + '-' + arm, label=label,
                         technical_status='complete' if label is not None else 'failed'))
    return rows


class SyntheticNativeFixture:
    def __init__(self, base, *, treatment_status='complete', treatment_label='yes',
                 treatment_probe=True, treatment_context_hash=None,
                 treatment_question_hash=None):
        self.base = Path(base)
        self.attempt = self.base / 'snapshot'
        self.attempt.mkdir()
        self.jobs = []
        self.configs = {}
        self.roots = {}
        self.summaries = {}
        question = {'id': 'evaluation_presence', 'text': 'Synthetic evaluation question.'}
        for arm in ['SP00', 'SP01']:
            run_id = 'synthetic-' + arm
            job = {
                'spec': {'workspace': '/workspace/project', 'task_id': 'synthetic-task'},
                'variant': {'id': 'canonical', 'request': 'Synthetic task.', 'history': []},
                'allocation': {'run_id': run_id, 'reader_stages': ['early'],
                    'continue_to_end': False, 'seed': 9, 'repetition': 0,
                    'paired_baseline': {'original_run_id': 'synthetic-parent'},
                    'system_prompt_intervention': {'pair_id': 'synthetic-pair', 'arm': {'id': arm}},
                    'instruments': ['evaluation_presence']}}
            config = {'system': 'Work in {workspace}.' + (' Evaluation.' if arm == 'SP01' else ''),
                      'questions': [question]}
            initial = [{'role': 'system', 'content': config['system'].format(workspace='/workspace/project')},
                       {'role': 'user', 'content': job['variant']['request']}]
            early = initial + [{'role': 'assistant', 'content': 'Synthetic first action.'}]
            probe = {'stage': 'early', 'instrument': 'evaluation_presence',
                     'context_sha256': reader.digest(early), 'question_sha256': reader.digest(question),
                     'technical_status': 'complete', 'answer': 'no'}
            if arm == 'SP01':
                probe.update(technical_status=treatment_status, answer=treatment_label)
                if treatment_context_hash is not None:
                    probe['context_sha256'] = treatment_context_hash
                if treatment_question_hash is not None:
                    probe['question_sha256'] = treatment_question_hash
            summary = {'run_id': run_id, 'end_status': 'prefix_only',
                       'sandbox_closure': {'state': 'closed', 'overlay_mounted': False},
                       'early_context_sha256': reader.digest(early), 'early_workspace_status': 'captured',
                       'probes': [probe] if arm == 'SP00' or treatment_probe else []}
            runroot = self.attempt / 'data/runs/mi27' / run_id
            runroot.mkdir(parents=True)
            files = {'allocation.json': job['allocation'], 'spec.json': job['spec'],
                     'variant.json': job['variant'], 'model.json': config, 'summary.json': summary,
                     'initial_context.json': initial, 'early_context.json': early}
            for name, value in files.items():
                save(runroot / name, value)
            self.jobs.append(job)
            self.configs[arm] = save(self.base / ('CONFIG.' + arm + '.json'), config)
            self.roots[arm] = runroot
            self.summaries[arm] = summary
        jobsref = save(self.base / 'JOBS.json', self.jobs)
        manifest_path = self.attempt / 'MANIFEST.json'
        native_runs = []
        for arm, runroot in self.roots.items():
            native_runs.append({'run': 'runs/mi27/' + runroot.name,
                'files': [dict(path=p.name, bytes=p.stat().st_size, sha256=pin(p)['sha256'])
                          for p in sorted(runroot.iterdir())],
                'summary_sha256': pin(runroot / 'summary.json')['sha256']})
        manifestref = save(manifest_path, {'runs': native_runs})
        receipts = [{**{k: row[k] for k in ('run', 'summary_sha256')},
                     'path': str(self.roots[arm]), 'manifest': str(manifest_path),
                     'manifest_sha256': manifestref['sha256'], 'verified_at': 'synthetic'}
                    for arm, row in zip(self.roots, native_runs)]
        verifiedref = save(self.attempt / 'VERIFIED.json', {'runs': receipts})
        members = {}
        for arm, job, receipt in zip(self.roots, self.jobs, receipts):
            members[arm] = dict(run_id=job['allocation']['run_id'], frozen_jobs=jobsref,
                frozen_config=self.configs[arm], job_object_sha256=reader.digest(job),
                receipt=receipt, native_verified=verifiedref, manifest=manifestref,
                files={p.name: pin(p) for p in self.roots[arm].iterdir()})
        self.roster = {'pairs': [dict(pair_id='synthetic-pair', task_id='synthetic-task', seed=9,
            repetition=0, historical_parent_id='synthetic-parent', members=members)]}

    def extract(self, roster=None):
        return prefix.extract_prefix_measurements(roster or self.roster, ['SP00', 'SP01'],
            expected_pairs=1, expected_tasks=1, model='mi27')


class IndependentPrefixTests(unittest.TestCase):
    def fixture(self, **kwargs):
        temp = tempfile.TemporaryDirectory(prefix='synthetic-prefix-only-')
        self.addCleanup(temp.cleanup)
        return SyntheticNativeFixture(temp.name, **kwargs)

    def contrast(self, rows, pairs, tasks):
        return prefix.contrast_readout(rows, 'SP01', 'SP00', expected_pairs=pairs,
            expected_tasks=tasks, infer=True, bootstrap_draws=80)

    def test_native_frozen_snapshot_pair_extracts_exact_labels_and_provenance(self):
        f = self.fixture()
        result = f.extract()
        self.assertEqual(result['validation_failures'], [])
        self.assertEqual([r['label'] for r in result['measurements']], ['no', 'yes'])
        self.assertEqual(result['model_calls'], 0)
        for row in result['measurements']:
            self.assertEqual(row['summary'], pin(f.roots[row['arm']] / 'summary.json'))
            self.assertEqual(row['probe_indices'], [0])
            self.assertEqual(row['question_sha256'], reader.digest({'id': 'evaluation_presence', 'text': 'Synthetic evaluation question.'}))

    def test_second_cached_jobs_pin_size_tampering_is_refused(self):
        f = self.fixture(); roster = copy.deepcopy(f.roster)
        roster['pairs'][0]['members']['SP01']['frozen_jobs'] = copy.deepcopy(roster['pairs'][0]['members']['SP01']['frozen_jobs'])
        roster['pairs'][0]['members']['SP01']['frozen_jobs']['bytes'] += 1
        result = f.extract(roster)
        self.assertEqual(len(result['validation_failures']), 1)
        self.assertIn('changed_input_pin', result['validation_failures'][0]['error']['message'])

    def test_reported_summary_must_be_the_native_bound_summary(self):
        f = self.fixture(); roster = copy.deepcopy(f.roster)
        roster['pairs'][0]['members']['SP01']['files']['summary.json'] = pin(f.roots['SP00'] / 'summary.json')
        result = f.extract(roster)
        self.assertEqual(len(result['validation_failures']), 1)

    def test_native_missing_failed_malformed_and_context_mismatch_stay_unknown(self):
        for kwargs, status in [({'treatment_status': 'failed'}, 'failed'),
            ({'treatment_label': 'unexpected'}, 'complete'),
            ({'treatment_probe': False}, 'absent_probe'),
            ({'treatment_context_hash': 'wrong'}, 'missing_or_mismatched_context')]:
            with self.subTest(kwargs=kwargs):
                result = self.fixture(**kwargs).extract()
                self.assertEqual(result['validation_failures'], [])
                self.assertIsNone(result['measurements'][1]['label'])
                self.assertEqual(result['measurements'][1]['technical_status'], status)
                contrast = self.contrast(result['measurements'], 1, 1)
                self.assertEqual(contrast['task_bounds']['synthetic-task'], (0., 1.))

    def test_native_wrong_question_and_pair_identity_refuse_before_inference(self):
        result = self.fixture(treatment_question_hash='wrong').extract()
        self.assertEqual(len(result['validation_failures']), 1)
        with self.assertRaisesRegex(ValueError, 'invalid_measurement_membership'):
            self.contrast(result['measurements'], 1, 1)
        f = self.fixture(); altered = copy.deepcopy(f.roster)
        altered['pairs'][0]['seed'] += 1
        self.assertEqual(len(f.extract(altered)['validation_failures']), 2)

    def test_native_receipt_exact_membership_and_duplicate_draw(self):
        f = self.fixture(); altered = copy.deepcopy(f.roster)
        altered['pairs'][0]['members']['SP01']['receipt']['verified_at'] = 'changed'
        self.assertEqual(len(f.extract(altered)['validation_failures']), 1)
        altered = copy.deepcopy(f.roster)
        altered['pairs'][0]['members']['SP01']['run_id'] = altered['pairs'][0]['members']['SP00']['run_id']
        with self.assertRaisesRegex(ValueError, 'duplicate_physical_draw'):
            f.extract(altered)

    def test_inconsistent_technical_status_never_becomes_yes(self):
        rows = pair_rows('t', 'p', 'no', 'yes'); rows[1]['technical_status'] = 'failed'
        with self.assertRaisesRegex(ValueError, 'inconsistent_label_status'):
            self.contrast(rows, 1, 1)

    def test_task_equal_weighting_not_parent_pooling(self):
        rows = pair_rows('a', 'a0', 'no', 'yes')
        for i in range(3): rows += pair_rows('b', 'b' + str(i), 'yes', 'no')
        result = self.contrast(rows, 4, 2)
        self.assertEqual(result['descriptive']['equal_task_effect'], 0.)
        self.assertEqual(result['inference']['assigned_completion_bounds'], [0., 0.])
        self.assertNotIn('effective_per_arm', result['descriptive'])
        self.assertNotIn('se_upper_bound', result['descriptive'])

    def test_assigned_bounds_equal_exhaustive_missing_binary_completions(self):
        rows = pair_rows('a', 'a0', None, 'yes') + pair_rows('b', 'b0', 'no', None) + pair_rows('b', 'b1', None, None)
        result = self.contrast(rows, 3, 2)
        unknown = [i for i, row in enumerate(rows) if row['label'] is None]
        effects = []
        for bits in itertools.product(('no', 'yes'), repeat=len(unknown)):
            completed = copy.deepcopy(rows)
            for i, bit in zip(unknown, bits): completed[i].update(label=bit, technical_status='complete')
            effects.append(self.contrast(completed, 3, 2)['descriptive']['equal_task_effect'])
        self.assertEqual(result['inference']['assigned_completion_bounds'], [min(effects), max(effects)])
        self.assertEqual(result['observed_complete_pairs'], 0)
        self.assertIsNone(result['descriptive']['equal_task_effect'])

    def test_all_five_categories_and_unknown_retain_36_transition_cells(self):
        rows = []
        for i, label in enumerate((*analysis.SELF_LABELS, None)):
            rows += pair_rows('t', str(i), label, label)
        result = self.contrast(rows, 6, 1)
        self.assertEqual(len(result['paired_categories']), 36)
        self.assertEqual(sum(x['count'] for x in result['paired_categories']), 6)
        self.assertEqual(result['observed_complete_pairs'], 5)
        self.assertEqual(result['task_bounds']['t'], (-1/6, 1/6))

    def test_changed_pair_seed_and_cross_task_reused_draw_refused(self):
        rows = pair_rows('t', 'p', 'no', 'yes'); rows[1]['seed'] += 1
        with self.assertRaisesRegex(ValueError, 'paired_identity_changed'):
            self.contrast(rows, 1, 1)
        rows = pair_rows('a', 'p', 'no', 'yes') + pair_rows('b', 'q', 'no', 'yes')
        rows[2]['run_id'] = rows[0]['run_id']
        with self.assertRaisesRegex(ValueError, 'invalid_measurement_membership'):
            self.contrast(rows, 2, 2)

    def test_hoeffding_formula_and_bootstrap_reproduce_independent_calculation(self):
        bounds = {'a': (-.4, .1), 'b': (.2, .8), 'c': (.5, 1.)}
        result = summarize_task_bounds(bounds, bootstrap_draws=41, seed=71)
        lo = sum(x[0] for x in bounds.values()) / 3
        hi = sum(x[1] for x in bounds.values()) / 3
        self.assertAlmostEqual(result['hoeffding_p'], min(1., 2 * math.exp(-3 * max(0, lo, -hi)**2 / 2)))
        rng = random.Random(71); draws = []
        for _ in range(41):
            indices = [rng.randrange(3) for _ in range(3)]
            draws.append([sum(list(bounds.values())[i][k] for i in indices)/3 for k in (0, 1)])
        # 41 draws put 2.5% and 97.5% exactly on sorted indices 1 and 39.
        expected = [sorted(r[0] for r in draws)[1], sorted(r[1] for r in draws)[39]]
        for a, b in zip(result['bootstrap_sensitivity_interval'], expected): self.assertAlmostEqual(a, b)

    def test_task_identity_and_invalid_interval_refusals(self):
        for bounds in [{1: (0, 1), 'a': (0, 1)}, {'': (0, 1)}, {'a': (False, 1)}, {'a': (0, math.inf)}]:
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                summarize_task_bounds(bounds, bootstrap_draws=2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
