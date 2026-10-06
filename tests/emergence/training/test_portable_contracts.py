import math
import unittest

from emergence.training.evaluation.normalize import CLOSE, EOS, normalize
from emergence.training.losses.dpo import dpo_derivative
from emergence.training.rewards import terminal_legacy, terminal_v5
from emergence.training.trainer.exposure_schedule import lr_at, visits_at
from emergence.training.trainer.main_audit_bounds import summarize, upper_nonpass_count


class RewardVersionTests(unittest.TestCase):
    def test_correction_is_explicit_and_legacy_is_unchanged(self):
        final = 'Cost $5. Then $$x=42$$\n' + r'\boxed{42}'
        self.assertEqual(terminal_legacy.extract_answer(final).status, 'invalid')
        corrected = terminal_v5.extract_answer(final)
        self.assertEqual((corrected.status, corrected.answer), ('valid', '42'))
        for parser in [terminal_legacy, terminal_v5]:
            self.assertEqual(parser.extract_answer(r'\boxed{42}').answer, '42')
            self.assertNotEqual(parser.extract_answer(r'\boxed{42} and more').status, 'valid')

    def test_normalization_never_recovers_answer_from_reasoning(self):
        words = {1: 'thinking', 2: '42', CLOSE: '</think>', EOS: '<|im_end|>'}
        decode = lambda ids: ''.join(words[i] for i in ids)
        result = normalize({'generated_token_ids': [1, EOS], 'finish_reason': 'eos', 'transport_status': 'ok'}, decode)
        self.assertIsNone(result['final_content'])
        result = normalize({'generated_token_ids': [1, CLOSE, 2, EOS], 'finish_reason': 'eos', 'transport_status': 'ok'}, decode)
        self.assertEqual(result['final_content'], '42')
        with self.assertRaises(ValueError):
            normalize({'generated_token_ids': [1, EOS], 'finish_reason': 'length', 'transport_status': 'ok'}, decode)


class NumericalContractTests(unittest.TestCase):
    def test_stable_dpo_derivative_agrees_with_finite_difference(self):
        for margin in [-10000, -2, 0, 2, 10000]:
            loss, derivative = dpo_derivative(margin, 0, 0, .1)
            self.assertTrue(math.isfinite(loss))
            epsilon = 1e-4
            finite = (dpo_derivative(margin + epsilon, 0, 0, .1)[0] - dpo_derivative(margin - epsilon, 0, 0, .1)[0]) / (2 * epsilon)
            self.assertAlmostEqual(derivative, finite, places=7)

    def test_original_schedule_and_incomplete_audit(self):
        self.assertEqual(visits_at(128), 2048)
        self.assertEqual(visits_at(512), 8192)
        with self.assertRaises(ValueError):
            visits_at(513)
        self.assertEqual(upper_nonpass_count(20, 20, 7), 7)
        self.assertEqual(upper_nonpass_count(4096, 384, 0), 37)
        self.assertFalse(summarize(4096, 384, 0, 0, all_double_reviews_complete=False, unresolved_systemic_defect=False)['audit_gate_pass'])
