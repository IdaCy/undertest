import hashlib
import json
import unittest

from emergence.training.math_reward import score_math_response
from emergence.training.protocol import ANSWER_FORMAT_INSTRUCTION
from emergence.training.protocol import GRPO_POLICY_ID
from emergence.training.protocol import PROMPT_CONTRACT_ID
from emergence.training.protocol import assess_grpo_group
from emergence.training.protocol import render_scalar_prompt


def sample(sample_id, response="Final answer: 2", **metadata):
    return {
        "sample_id": sample_id,
        "task_id": "task_fixture",
        "group_id": "group_fixture",
        "policy_version": "policy_update_0",
        "response": response,
        "score": score_math_response(response, "2", **{"finish_reason": "stop", **metadata}).to_dict(),
        "raw_channels": {"final": response},
    }


def gate(rollouts, expected=("a", "b")):
    return assess_grpo_group(
        group_id="group_fixture", task_id="task_fixture", policy_version="policy_update_0",
        expected_sample_ids=expected, rollouts=rollouts,
    )


class ScalarPromptTests(unittest.TestCase):
    def test_preserves_source_and_never_serializes_private_metadata(self):
        text = "  Find x.\r\n\nPreserve e\u0301, whitespace and \\frac{1}{2}.  "
        task = {
            "task_id": "task_fixture", "source_text": text,
            "source_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "private_gold": {"answer": "GOLD_CANARY_7f09"},
            "messages": [{"role": "assistant", "content": "GOLD_CANARY_7f09"}],
            "split": "PRIVATE_SPLIT_2b64", "reward": "PRIVATE_REWARD_d031",
        }
        rendered = render_scalar_prompt(task)
        payload = rendered.to_dict()
        self.assertEqual(payload["messages"], [
            {"role": "system", "content": ANSWER_FORMAT_INSTRUCTION},
            {"role": "user", "content": text},
        ])
        self.assertEqual(payload["contract_id"], PROMPT_CONTRACT_ID)
        self.assertEqual(payload["qualification_status"], "proposed_unqualified")
        self.assertEqual(payload["tokenization_status"], "unqualified_no_tokenizer_applied")
        encoded = json.dumps(payload, allow_nan=False)
        for canary in ("GOLD_CANARY_7f09", "PRIVATE_SPLIT_2b64", "PRIVATE_REWARD_d031"):
            self.assertNotIn(canary, encoded)
        messages = rendered.model_messages()
        messages[1]["content"] = "mutated caller copy"
        self.assertEqual(rendered.model_messages()[1]["content"], text)
        self.assertEqual(render_scalar_prompt(task).messages_sha256, rendered.messages_sha256)

    def test_source_hash_mismatch_stops_rendering(self):
        with self.assertRaisesRegex(ValueError, "unchanged source text"):
            render_scalar_prompt({"task_id": "task_fixture", "source_text": "Text", "source_text_sha256": "stale"})

    def test_instruction_examples_fit_actual_scorer_contract(self):
        for answer, gold in (("-2", "-2"), ("0.125", "1/8"), ("-3/2", "-1.5")):
            with self.subTest(answer=answer):
                self.assertEqual(score_math_response("Reasoning.\nFinal answer: " + answer, gold, finish_reason="stop").status, "correct")


class FixedGRPOGroupTests(unittest.TestCase):
    def test_mixed_group_uses_declared_order_and_preserves_every_record(self):
        rows = [sample("b", "Final answer: 3"), sample("a")]
        result = gate(rows)
        self.assertEqual(result.action, "valid_mixed_reward")
        self.assertEqual(result.rewards, (1.0, 0.0))
        self.assertEqual(result.observed_sample_ids, ("b", "a"))
        self.assertEqual(result.reward_variance, 0.25)
        self.assertTrue(result.update_permitted_by_group_policy)
        self.assertFalse(result.retry_permitted)
        self.assertFalse(result.resampling_permitted)
        self.assertEqual(list(result.preserved_rollouts), rows)
        rows[0]["response"] = "later mutation"
        self.assertEqual(result.preserved_rollouts[0]["response"], "Final answer: 3")
        encoded = result.to_dict()
        self.assertEqual(encoded["policy_id"], GRPO_POLICY_ID)
        self.assertEqual(encoded["qualification_status"], "proposed_unqualified")
        self.assertEqual(len(encoded["group_policy"]["policy_sha256"]), 64)
        json.dumps(encoded, allow_nan=False)

    def test_all_equal_groups_are_valid_without_resampling(self):
        for response in ("Final answer: 2", "Final answer: 3"):
            with self.subTest(response=response):
                result = gate([sample("a", response), sample("b", response)])
                self.assertEqual(result.action, "valid_zero_variance")
                self.assertTrue(result.zero_variance)
                self.assertTrue(result.update_permitted_by_group_policy)
                self.assertEqual(result.reward_variance, 0.0)
                self.assertFalse(result.resampling_permitted)
                self.assertEqual(len(result.preserved_rollouts), 2)

    def test_model_invalid_and_truncated_zero_stay_in_fixed_denominator(self):
        rows = [sample("a"), sample("b", "missing final marker"), sample("c", finish_reason="length")]
        result = gate(rows, expected=("a", "b", "c"))
        self.assertEqual(result.action, "valid_mixed_reward")
        self.assertEqual(result.rewards, (1.0, 0.0, 0.0))
        self.assertEqual(result.reward_mean, 1 / 3)
        self.assertEqual([row["score"]["status"] for row in result.preserved_rollouts], ["correct", "invalid", "truncated"])

    def test_missing_member_stops_whole_group_without_retry(self):
        result = gate([sample("a")])
        self.assertEqual(result.action, "stop_and_preserve")
        self.assertFalse(result.update_permitted_by_group_policy)
        self.assertFalse(result.retry_permitted)
        self.assertIn("missing_sample_id:b", result.blockers)
        self.assertIsNone(result.rewards)
        self.assertIsNone(result.reward_mean)
        self.assertEqual(len(result.preserved_rollouts), 1)

    def test_null_scorer_failure_transport_or_unknown_completion_stops_group(self):
        failures = [
            sample("b", "Final answer: x"),
            sample("b", transport_status="timeout"),
            sample("b", finish_reason=None),
        ]
        for failed in failures:
            with self.subTest(status=failed["score"]["status"]):
                result = gate([sample("a"), failed])
                self.assertEqual(result.action, "stop_and_preserve")
                self.assertFalse(result.update_permitted_by_group_policy)
                self.assertFalse(result.retry_permitted)
                self.assertFalse(result.resampling_permitted)
                self.assertIsNone(result.rewards)
                self.assertIsNone(result.preserved_rollouts[1]["score"]["reward"])
                self.assertEqual(len(result.preserved_rollouts), 2)

    def test_duplicate_extra_or_foreign_policy_members_are_preserved_and_stop(self):
        foreign = sample("b")
        foreign["policy_version"] = "policy_update_1"
        for rows in ([sample("a"), sample("a")], [sample("a"), sample("b"), sample("c")], [sample("a"), foreign]):
            with self.subTest(rows=rows):
                result = gate(rows)
                self.assertEqual(result.action, "stop_and_preserve")
                self.assertEqual(list(result.preserved_rollouts), rows)
                self.assertIsNone(result.rewards)

    def test_nonfinite_boolean_and_inconsistent_rewards_never_become_zero(self):
        for value in (float("nan"), float("inf"), True, 0.0, None):
            with self.subTest(value=value):
                wrong = sample("b")
                wrong["score"]["reward"] = value
                result = gate([sample("a"), wrong])
                self.assertEqual(result.action, "stop_and_preserve")
                self.assertIsNone(result.rewards)
                self.assertFalse(result.update_permitted_by_group_policy)
                json.dumps(result.to_dict(), allow_nan=False)
        failed = sample("b")
        failed["score"]["reward"] = float("nan")
        self.assertEqual(gate([sample("a"), failed]).preserved_rollouts[1]["score"]["reward"], {"__nonfinite_float_evidence__": "nan"})

    def test_unusable_group_specification_raises_before_any_action(self):
        for expected in (("a",), ("a", "a"), "ab"):
            with self.subTest(expected=expected):
                with self.assertRaises(ValueError):
                    gate([], expected=expected)

    def test_malformed_metadata_produces_a_preserved_stop_receipt(self):
        for name, value in (("status", []), ("finish_reason", {}), ("reward", 10 ** 400)):
            with self.subTest(name=name):
                failed = sample("b")
                failed["score"][name] = value
                result = gate([sample("a"), failed])
                self.assertEqual(result.action, "stop_and_preserve")
                self.assertIsNone(result.rewards)
                self.assertEqual(result.preserved_rollouts[1]["score"][name], value)

    def test_empty_returned_answer_cannot_reuse_a_correct_score(self):
        stale = sample("b")
        stale["response"] = ""
        result = gate([sample("a"), stale])
        self.assertEqual(result.action, "stop_and_preserve")
        self.assertIn("member[1]:empty_response_with_scored_answer", result.blockers)
        self.assertEqual(result.preserved_rollouts[1]["response"], "")


if __name__ == "__main__":
    unittest.main()
