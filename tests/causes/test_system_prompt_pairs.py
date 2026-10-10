import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from causes.system_prompt_pairs import build_from_manifest, build_pairs


def job(run_id='original_0', variant_id='B00L00', full=False, seed=17):
    return {'spec': {'task_id': 'task_a', 'request': 'Repair the function.', 'workspace': '/workspace',
                     'seed_path': '/immutable/task_a', 'base_commit': 'commit_a',
                     'image': 'image@sha256:abc', 'actor_env': ['LANG=C.UTF-8']},
            'variant': {'id': variant_id, 'task_id': 'task_a', 'request': 'Repair the function.',
                        'control': 'natural_original', 'factor': 'B00', 'level': 0, 'eligible': True},
            'allocation': {'run_id': run_id, 'seed': seed, 'task_seed': 23, 'model_seed': 31,
                           'continue_to_end': full, 'canonical_variant': variant_id,
                           'stage': 'original_study', 'primary_memberships': [{'contrast': 'factor_a'}],
                           'instruments': ['evaluation_presence'], 'reader_stages': ['early', 'end'] if full else ['early']}}


class SystemPromptPairsTests(unittest.TestCase):
    def setUp(self):
        self.jobs = [job(), job('original_1', full=True, seed=19), job('carrier_0', 'F03L00'), job('carrier_1', 'F07L00')]
        self.config = {'model': 'model_a', 'system': 'Work in {workspace}.', 'sampling': {'temperature': 1.0},
                       'tools': [{'name': 'shell'}], 'endpoint': 'http://example.invalid/model', 'max_actor_turns': 128}
        self.arms = [{'id': 'arm_' + str(i), 'text': 'Intervention ' + str(i), 'primary': i == 0} for i in range(5)]

    def build(self, **overrides):
        arguments = {'jobs': self.jobs, 'config': self.config, 'arms': self.arms,
                     'namespace': 'paired_study', 'expected_model': 'model_a', 'expected_arm_count': 5}
        arguments.update(overrides)
        return build_pairs(**arguments)

    def test_exact_natural_selection_and_balanced_five_arms(self):
        result = self.build()
        self.assertEqual({p['original_run_id'] for p in result['pairs']}, {'original_0', 'original_1'})
        self.assertEqual(len(result['arms']), 5)
        self.assertTrue(all(len(arm['jobs']) == 2 for arm in result['arms']))
        self.assertEqual(len({run for pair in result['pairs'] for run in pair['runs'].values()}), 10)

    def test_preserves_inputs_environment_seed_capture_and_provenance(self):
        before = copy.deepcopy((self.jobs, self.config, self.arms))
        result = self.build()
        self.assertEqual((self.jobs, self.config, self.arms), before)
        for output in result['arms']:
            for original, derived in zip(self.jobs[:2], output['jobs']):
                self.assertEqual(derived['spec'], original['spec'])
                self.assertEqual(derived['variant'], original['variant'])
                allocation = derived['allocation']
                for key in ('seed', 'task_seed', 'model_seed', 'continue_to_end', 'instruments', 'reader_stages'):
                    self.assertEqual(allocation[key], original['allocation'][key])
                self.assertEqual(allocation['primary_memberships'], [])
                self.assertFalse(allocation['fixed_study_inference'])
                self.assertEqual(allocation['paired_baseline']['original_allocation'], original['allocation'])
                self.assertNotEqual(allocation['stage'], original['allocation']['stage'])
            config = copy.deepcopy(output['config'])
            self.assertEqual(config.pop('system'), self.config['system'] + '\n\n' + output['arm']['text'])
            self.assertEqual(config, {k: v for k, v in self.config.items() if k != 'system'})
        result['arms'][0]['jobs'][0]['spec']['actor_env'].append('OTHER=1')
        self.assertEqual((self.jobs, self.config, self.arms), before)
        self.assertEqual(result['arms'][1]['jobs'][0]['spec'], self.jobs[0]['spec'])

    def test_deterministic_identities_and_namespace_separation(self):
        self.assertEqual(self.build(), self.build())
        first = {r for p in self.build()['pairs'] for r in p['runs'].values()}
        other = {r for p in self.build(namespace='other_study')['pairs'] for r in p['runs'].values()}
        self.assertFalse(first & other)

    def test_changed_intervention_cannot_reuse_its_old_run_identity(self):
        original = self.build()
        arms = copy.deepcopy(self.arms)
        arms[0]['text'] = 'A different intervention.'
        changed = self.build(arms=arms)
        for before, after in zip(original['pairs'], changed['pairs']):
            self.assertEqual(before['pair_id'], after['pair_id'])
            self.assertNotEqual(before['runs']['arm_0'], after['runs']['arm_0'])
            self.assertEqual(before['runs']['arm_1'], after['runs']['arm_1'])

    def test_derived_jobs_are_not_reinterpreted_as_unprompted_originals(self):
        generated = self.build()['arms'][0]['jobs']
        with self.assertRaisesRegex(ValueError, 'original_already_has_intervention'):
            self.build(jobs=generated)

    def test_duplicate_ids_text_and_main_arms_refuse(self):
        for key, value in [('id', self.arms[0]['id']), ('text', self.arms[0]['text']), ('primary', True)]:
            with self.subTest(key=key):
                arms = copy.deepcopy(self.arms)
                arms[1][key] = value
                with self.assertRaises(ValueError):
                    self.build(arms=arms)
        with self.assertRaisesRegex(ValueError, 'arm_count'):
            self.build(arms=self.arms + [self.arms[0]])

    def test_bad_natural_baselines_are_rejected(self):
        mutations = [('variant', 'control', 'carrier'), ('variant', 'request', 'Changed task'),
                     ('variant', 'files', [{'path': 'cue', 'content': 'evaluation'}]),
                     ('variant', 'workspace', '/different'), ('allocation', 'seed', True),
                     ('allocation', 'continue_to_end', 'yes'), ('allocation', 'canonical_variant', 'F01L00')]
        for section, key, value in mutations:
            with self.subTest(key=key):
                jobs = copy.deepcopy(self.jobs)
                jobs[0][section][key] = value
                with self.assertRaises(ValueError):
                    self.build(jobs=jobs)

    def test_no_baselines_or_duplicate_original_ids_refuse(self):
        with self.assertRaisesRegex(ValueError, 'no_natural_baselines'):
            self.build(jobs=self.jobs[2:])
        with self.assertRaisesRegex(ValueError, 'duplicate_or_invalid_original_id'):
            self.build(jobs=self.jobs + [self.jobs[0]])

    def test_model_namespace_and_arm_path_validation(self):
        for overrides in [{'expected_model': 'other_model'}, {'namespace': '../escape'},
                          {'arms': [{'id': '../escape', 'text': 'text'}], 'expected_arm_count': 1}]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.build(**overrides)

    def test_intervention_braces_are_literal_in_runner_template(self):
        arms = [{'id': 'literal', 'text': 'Preserve {these} and {workspace} literally.'}]
        config = self.build(arms=arms, expected_arm_count=1)['arms'][0]['config']
        self.assertEqual(config['system'].format(workspace='/workspace'),
                         'Work in /workspace.\n\nPreserve {these} and {workspace} literally.')

    def _manifest(self, root):
        def save(name, value):
            data = json.dumps(value).encode()
            (root / name).write_bytes(data)
            return {'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        manifest = {'schema': 'causes.system_prompt_pair_input.v1', 'jobs': save('jobs.json', self.jobs),
                    'config': save('config.json', self.config), 'arms': self.arms, 'namespace': 'paired_study',
                    'model': 'model_a', 'expected_arm_count': 5, 'expected_baseline_count': 2}
        path = root / 'input.json'
        path.write_text(json.dumps(manifest))
        return path

    def test_offline_bundle_and_exclusive_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(root)
            result = build_from_manifest(manifest, root / 'output')
            saved = json.loads((root / 'output' / result['path']).read_text())
            self.assertEqual(len(saved['pairs']), 2)
            for arm in saved['arms']:
                for field in ('config', 'jobs'):
                    pin = arm[field]
                    data = (root / 'output' / pin['path']).read_bytes()
                    self.assertEqual((len(data), hashlib.sha256(data).hexdigest()), (pin['bytes'], pin['sha256']))
            before = {p.name: p.read_bytes() for p in (root / 'output').iterdir()}
            with self.assertRaises(FileExistsError):
                build_from_manifest(manifest, root / 'output')
            self.assertEqual(before, {p.name: p.read_bytes() for p in (root / 'output').iterdir()})

    def test_changed_frozen_input_and_wrong_count_create_no_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._manifest(root)
            (root / 'jobs.json').write_text('[]')
            with self.assertRaisesRegex(ValueError, 'input_pin_mismatch'):
                build_from_manifest(manifest, root / 'changed')
            self.assertFalse((root / 'changed').exists())
            manifest = self._manifest(root)
            body = json.loads(manifest.read_text())
            body['expected_baseline_count'] = 3
            manifest.write_text(json.dumps(body))
            with self.assertRaisesRegex(ValueError, 'baseline_count_mismatch'):
                build_from_manifest(manifest, root / 'wrong_count')
            self.assertFalse((root / 'wrong_count').exists())


if __name__ == '__main__':
    unittest.main()
