import copy
import unittest

from causes import campaign_reader as reader
from causes.measurement_view import prepare_judge_context, prepare_probe_context
from causes.neutral_summary_single_view import build_neutral_arm
from causes.neutral_summary_view import build_neutral_pair
from test_neutral_summary_view import fixture, rebind_condition


class NeutralSummarySingleViewTests(unittest.TestCase):
    def test_single_arms_equal_existing_pairs_for_every_supported_recipe(self):
        for family in ('F05', 'F07', 'F08', 'F09', 'F11'):
            for level in range(1, 6):
                for phase in ('prefix', 'full'):
                    args = fixture(family + 'L' + str(level).zfill(2), phase=phase)
                    before = reader.encoded(args)
                    views, proofs = build_neutral_pair(*args)
                    for arm, episode in zip(('baseline', 'treatment'), args[:2]):
                        with self.subTest(family=family, level=level, phase=phase, arm=arm):
                            view, proof = build_neutral_arm(episode, args[2], args[3],
                                arm=arm, baseline_config=args[0]['config'])
                            self.assertEqual(reader.encoded(view), reader.encoded(views[arm]))
                            self.assertEqual(proof['derivation'], proofs['arms'][arm])
                            self.assertFalse(proof['counterpart_context_supplied'])
                            self.assertEqual(reader.encoded(args), before)

    def test_treatment_needs_no_baseline_context_and_retains_generated_cues(self):
        baseline, treatment, summary, construction = fixture('F11L05')
        config = baseline['config']
        del baseline
        view, proof = build_neutral_arm(treatment, summary, construction,
            arm='treatment', baseline_config=config)
        self.assertEqual(view['messages'][2:], treatment['messages'][len(treatment['initial_context']):])
        self.assertIn('/opt/evaluator/config.yaml', view['messages'][-1]['content'])
        self.assertFalse(proof['external_tool_neutralization_supported'])
        self.assertEqual(set(view), {'messages', 'initial_context', 'prefix_context', 'phase'})

    def test_wrong_arm_or_episode_is_not_substituted(self):
        baseline, treatment, summary, construction = fixture()
        for arm, episode in (('other', treatment), ('baseline', treatment), ('treatment', baseline)):
            with self.subTest(arm=arm), self.assertRaises(ValueError):
                build_neutral_arm(episode, summary, construction,
                    arm=arm, baseline_config=baseline['config'])

    def test_explicit_original_baseline_system_is_required(self):
        baseline, treatment, summary, construction = fixture('F08L04')
        for config in (None, {}, {'system': None}, {'system': 'Different common instruction.'}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                build_neutral_arm(treatment, summary, construction, arm='treatment', baseline_config=config)
        view, _ = build_neutral_arm(treatment, summary, construction,
            arm='treatment', baseline_config=baseline['config'])
        self.assertEqual(view['initial_context'][0], baseline['initial_context'][0])

    def test_forged_recipe_refuses_even_after_rehash(self):
        for variant, field, value in [('F05L05', 'request', 'Unrelated request.'),
                                      ('F08L04', 'workspace', '/wrong')]:
            args = fixture(variant)
            args[3]['condition_variant'][field] = value
            rebind_condition(args)
            with self.assertRaisesRegex(ValueError, 'initial_recipe'):
                build_neutral_arm(args[1], args[2], args[3], arm='treatment', baseline_config=args[0]['config'])

    def test_summary_and_construction_identity_required(self):
        for target, field in ((2, 'summary_sha256'), (2, 'baseline_request_sha256'),
                              (3, 'condition_variant_sha256'), (3, 'baseline_variant_sha256')):
            args = fixture()
            args[target][field] = '0' * 64
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_neutral_arm(args[1], args[2], args[3], arm='treatment', baseline_config=args[0]['config'])

    def test_original_boundary_and_private_message_metadata_refuse(self):
        for change in ('boundary', 'metadata', 'extra_system'):
            args = fixture()
            if change == 'boundary':
                args[1]['initial_context'] = copy.deepcopy(args[1]['initial_context'])
                args[1]['initial_context'][0]['content'] += ' changed'
            elif change == 'metadata':
                args[1]['messages'][-1]['condition_id'] = 'private'
            else:
                args[1]['messages'].append({'role': 'system', 'content': 'extra'})
            with self.subTest(change=change), self.assertRaises(ValueError):
                build_neutral_arm(args[1], args[2], args[3], arm='treatment', baseline_config=args[0]['config'])

    def test_missing_reasoning_and_judge_target_mapping_survive(self):
        args = fixture('F05L05')
        del args[1]['messages'][4]['reasoning']
        view, proof = build_neutral_arm(args[1], args[2], args[3],
            arm='treatment', baseline_config=args[0]['config'])
        context, targets = prepare_judge_context(view)
        self.assertTrue(context['observation']['technical_missing_reasoning'])
        self.assertEqual(list(targets), [2])
        self.assertEqual(proof['derivation']['contexts']['messages']['original_to_derived_index'], [0, None, None, 1, 2, 3])
        probe = prepare_probe_context(view)
        self.assertNotIn('original_episode_sha256', reader.encoded(probe).decode())
        self.assertNotIn('treatment-run', reader.encoded(probe).decode())


if __name__ == '__main__':
    unittest.main()
