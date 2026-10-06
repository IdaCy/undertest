import json
import unittest
from unittest.mock import patch

from emergence.training.math_reward import MAX_ANSWER_CHARS
from emergence.training.math_reward import MAX_RESPONSE_CHARS
from emergence.training.math_reward import MAX_SCALAR_DIGITS
from emergence.training.math_reward import SCORER_VERSION
from emergence.training.math_reward import parse_scalar_answer
from emergence.training.math_reward import score_math_response


def score(response, gold="42", **metadata):
    return score_math_response(response, gold, **{"finish_reason": "stop", **metadata})


class ExactScalarTests(unittest.TestCase):
    def test_manual_equivalences_are_exact(self):
        for left, right, expected in [
            ("42", "+0042", "42"),
            ("-0", "0.000", "0"),
            (".125", r"\frac{1}{8}", "1/8"),
            ("-0.5", r"-\frac{2}{4}", "-1/2"),
            ("1/-2", r"\frac{-3}{6}", "-1/2"),
            ("-2/3", r"-\dfrac{2}{3}", "-2/3"),
            ("1/2", r"\tfrac{2}{4}", "1/2"),
            ("0.100000000000000000000000000001", "100000000000000000000000000001/1000000000000000000000000000000", "100000000000000000000000000001/1000000000000000000000000000000"),
        ]:
            with self.subTest(left=left, right=right):
                result = score("Final answer: " + left, right)
                self.assertEqual(result.status, "correct")
                self.assertEqual(result.reward, 1.0)
                self.assertEqual(result.normalized_prediction, expected)

    def test_scalar_math_and_gold_box_wrappers(self):
        for gold in (r"\boxed{42}", r"\(42\)", "$42$", r"$$\boxed{42}$$"):
            with self.subTest(gold=gold):
                self.assertEqual(score(r"\boxed{42}", gold).status, "correct")

    def test_nearby_decimals_do_not_use_float_tolerance(self):
        result = score("Final answer: 0.100000000000000000000000000001", "0.1")
        self.assertEqual(result.status, "incorrect")
        self.assertEqual(result.correctness, "incorrect")
        self.assertEqual(result.validity, "valid")
        self.assertEqual(result.reward, 0.0)

    def test_full_answer_is_required_not_a_substring(self):
        for answer in ("142", "420", "-42", "4.2", "42/10"):
            with self.subTest(answer=answer):
                self.assertEqual(score("Final answer: " + answer).status, "incorrect")
        for answer in ("42 cm", "42, 43", "x = 42", "42 or 7", "42%", "42 + 0"):
            with self.subTest(answer=answer):
                result = score("Final answer: " + answer)
                self.assertEqual(result.status, "unscorable")
                self.assertIsNone(result.reward)

    def test_unsupported_math_abstains(self):
        for value in (r"\sqrt{2}", r"\pi", "(1, 2)", "1,000", "1e3", "10^3", "NaN", "Infinity", "１", "−1"):
            with self.subTest(value=value):
                result = score("Final answer: " + value)
                self.assertEqual(result.status, "unscorable")
                self.assertIsNone(result.reward)

    def test_malformed_numbers_are_invalid(self):
        for value in ("--42", "++42", "+-42", "-+42", "4..2", "+ 42", "42.", "1/0", r"\frac{1}{0}", r"\dfrac{1}{0}", "1e+", "1e--2", "", "$42", r"\frac{1}{2", r"\tfrac{1}{2"):
            with self.subTest(value=value):
                result = score("Final answer: " + value)
                self.assertEqual(result.status, "invalid")
                self.assertEqual(result.reward, 0.0)
                self.assertEqual(result.correctness, "unscored")

    def test_resource_limits_abstain_before_integer_or_exponent_conversion(self):
        for value in ("9" * (MAX_SCALAR_DIGITS + 1), "9" * (MAX_ANSWER_CHARS + 1), "1e9999999999999999999999999999999"):
            with self.subTest(length=len(value)):
                result = score("Final answer: " + value)
                self.assertEqual(result.status, "unscorable")
                self.assertIsNone(result.reward)
        result = score("x" * MAX_RESPONSE_CHARS + r"\boxed{42}")
        self.assertEqual(result.reason, "response_resource_limit")
        nested = r"\boxed{" + "{" * 10 + "42" + "}" * 11
        self.assertEqual(score(nested).reason, "nesting_resource_limit")

    def test_gold_invalidity_is_unscored_even_for_a_truncated_response(self):
        for gold in ("", "1/0", "two", "1, 2", "1e3", None, "7" * (MAX_SCALAR_DIGITS + 1)):
            with self.subTest(gold=gold):
                result = score(r"\boxed{42}", gold, finish_reason="length")
                self.assertEqual(result.status, "unscorable")
                self.assertIsNone(result.reward)
                self.assertTrue(result.reason.startswith("gold_"))

    def test_parse_result_is_json_serializable(self):
        parsed = parse_scalar_answer(r"\frac{6}{4}")
        self.assertEqual(json.loads(json.dumps(parsed.to_dict())), {
            "status": "valid", "reason": "exact_scalar", "canonical": "3/2", "numerator": 3, "denominator": 2,
        })


class ExtractionAndMetadataTests(unittest.TestCase):
    def test_terminal_box_contract(self):
        for response in (
            r"Reasoning leads to \boxed{42}",
            r"Reasoning leads to \boxed{42}.",
            r"Reasoning leads to $\boxed{42}$.",
            r"Reasoning leads to $$\boxed{42}$$",
            r"Reasoning leads to \(\boxed{42}\)",
            r"Reasoning leads to \[\boxed{42}\].",
            "Reasoning\nFinal answer: " + r"$\boxed{42}$.",
            "Reasoning\nFinal answer: " + r"\boxed{42}",
            "Reasoning\nFINAL ANSWER: 42",
        ):
            with self.subTest(response=response):
                self.assertEqual(score(response).status, "correct")

    def test_multiple_or_conflicting_explicit_answers_are_invalid(self):
        for response in (
            r"\boxed{7}, then \boxed{42}",
            r"\boxed{42}, then \boxed{42}",
            "Final answer: 7\nFinal answer: 42",
            "Final answer: 7\n" + r"\boxed{42}",
            "Final answer: definitely " + r"\boxed{42}",
            "Final answer: 42\nMore text",
        ):
            with self.subTest(response=response):
                result = score(response)
                self.assertEqual(result.status, "invalid")
                self.assertEqual(result.reward, 0.0)

    def test_missing_or_malformed_marker_is_invalid(self):
        for response in ("42", "The result is 42", r"\boxed{42", r"\boxed42", r"\boxed{}", r"$\boxed{42}", r"\\boxed{42}"):
            with self.subTest(response=response):
                self.assertEqual(score(response).status, "invalid")

    def test_box_trailing_content_cannot_win_credit(self):
        for response in (r"\boxed{42} cm", r"\boxed{42} or 7", r"\boxed{42} -- ignore", r"\boxed{42}$"):
            with self.subTest(response=response):
                result = score(response)
                self.assertEqual(result.status, "unscorable")
                self.assertIsNone(result.reward)

    def test_truncation_overrides_even_a_correct_or_malformed_answer(self):
        for finish_reason in ("length", "max_tokens", "max_length"):
            for response in (r"\boxed{42}", "Final answer: 42", "garbage"):
                with self.subTest(finish_reason=finish_reason, response=response):
                    result = score(response, finish_reason=finish_reason)
                    self.assertEqual(result.status, "truncated")
                    self.assertEqual(result.reward, 0.0)
                    self.assertEqual(result.correctness, "unscored")
                    self.assertEqual(result.parse_status, "not_checked")
                    self.assertIsNone(result.extracted_answer)

    def test_missing_unknown_and_filtered_finish_states_abstain(self):
        for finish_reason in (None, "unknown", "content_filter", "error", "STOP", "tool_calls"):
            with self.subTest(finish_reason=finish_reason):
                result = score(r"\boxed{42}", finish_reason=finish_reason)
                self.assertEqual(result.status, "unscorable")
                self.assertIsNone(result.reward)

    def test_transport_failure_is_not_a_negative_example(self):
        result = score(r"\boxed{42}", transport_status="timeout", finish_reason="length")
        self.assertEqual(result.status, "scorer_error")
        self.assertEqual(result.transport_status, "timeout")
        self.assertEqual(result.reason, "transport_failure")
        self.assertIsNone(result.reward)

    def test_invalid_api_types_are_scorer_errors(self):
        for response, metadata in [(None, {}), ("Final answer: 42", {"finish_reason": []}), ("Final answer: 42", {"transport_status": None})]:
            with self.subTest(response=response, metadata=metadata):
                result = score(response, **metadata)
                self.assertEqual(result.status, "scorer_error")
                self.assertIsNone(result.reward)

    def test_scorer_exception_is_not_a_negative_example(self):
        with patch("emergence.training.math_reward.parse_scalar_answer", side_effect=RuntimeError("private details")):
            result = score(r"\boxed{42}")
        self.assertEqual(result.status, "scorer_error")
        self.assertEqual(result.reason, "internal_exception:RuntimeError")
        self.assertNotIn("private", json.dumps(result.to_dict()))
        self.assertIsNone(result.reward)

    def test_result_preserves_explicit_serializable_status(self):
        result = score(r"\boxed{42}", finish_reason="end_turn")
        loaded = json.loads(json.dumps(result.to_dict()))
        self.assertEqual(loaded["scorer_version"], SCORER_VERSION)
        self.assertEqual(loaded["status"], "correct")
        self.assertEqual(loaded["correctness"], "correct")
        self.assertEqual(loaded["validity"], "valid")
        self.assertEqual(loaded["parse_status"], "valid")
        self.assertEqual(loaded["transport_status"], "ok")
        self.assertEqual(loaded["finish_reason"], "end_turn")


if __name__ == "__main__":
    unittest.main()
