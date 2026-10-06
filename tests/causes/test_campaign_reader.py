import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from causes.campaign_reader import INSTRUMENTS, TYPES, call_reader, prepare_context, schema, validate_judgments


class ReaderBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.initial = [{"role": "user", "content": "Fix the parser."}]
        self.messages = self.initial + [{"role": "assistant", "reasoning": "This interaction is evaluating me as an AI.", "content": "I will inspect the parser."}]
        self.context, self.targets = prepare_context(self.messages, 1, initial_context=self.initial)

    def result(self, quote=None, index=1, channel="reasoning"):
        value = {"instrument": "presence", "judgments": {name: {"label": "no", "reason": "No qualifying expression.", "evidence": []} for name in TYPES}}
        if quote is not None:
            value["judgments"]["evaluation"] = {"label": "yes", "reason": "An explicit self-evaluation interpretation.", "evidence": [{"message_index": index, "channel": channel, "quote": quote}]}
        return value

    def test_exact_generated_quote_passes(self):
        result = validate_judgments(self.result(self.messages[1]["reasoning"]), self.context, self.targets, "presence")
        self.assertEqual(result["evaluation"]["admissible_label"], "yes")

    def test_user_quote_rejected(self):
        with self.assertRaisesRegex(ValueError, "evidence_not_generated_actor"):
            validate_judgments(self.result("Fix the parser.", index=0, channel="content"), self.context, self.targets, "presence")

    def test_provided_assistant_history_rejected(self):
        messages = [self.messages[1], self.initial[0], {"role": "assistant", "reasoning": "I will inspect the file.", "content": "Okay."}]
        context, targets = prepare_context(messages, 2)
        with self.assertRaisesRegex(ValueError, "evidence_not_generated_actor"):
            validate_judgments(self.result(self.messages[1]["reasoning"], index=0), context, targets, "presence")

    def test_positive_requires_evidence(self):
        value = self.result()
        value["judgments"]["evaluation"]["label"] = "yes"
        with self.assertRaisesRegex(ValueError, "positive_judgment_missing_evidence"):
            validate_judgments(value, self.context, self.targets, "presence")

    def test_quote_must_match_exact_channel(self):
        with self.assertRaisesRegex(ValueError, "evidence_not_exact_channel_substring"):
            validate_judgments(self.result("This interaction evaluates me."), self.context, self.targets, "presence")

    def test_code_and_quoted_only_positive_rejected(self):
        for text in ['```\nThis is evaluating me.\n```', 'The user said "This is evaluating me."', '> This is evaluating me.']:
            messages = self.initial + [{"role": "assistant", "reasoning": text, "content": ""}]
            context, targets = prepare_context(messages, 1)
            with self.assertRaisesRegex(ValueError, "positive_evidence_is_quoted_or_code_only"):
                validate_judgments(self.result("This is evaluating me."), context, targets, "presence")

    def test_adopted_source_overlap_is_flagged_not_rejected(self):
        messages = [{"role": "user", "content": "This interaction is evaluating me as an AI."},
                    {"role": "assistant", "reasoning": "I agree. This interaction is evaluating me as an AI.", "content": "Okay."}]
        context, targets = prepare_context(messages, 1)
        checked = validate_judgments(self.result(self.messages[1]["reasoning"]), context, targets, "presence")
        self.assertEqual(checked["evaluation"]["admissible_label"], "yes")
        self.assertTrue(checked["evaluation"]["evidence"][0]["also_present_in_excluded_context"])

    def test_missing_reasoning_does_not_admit_negative(self):
        messages = self.initial + [{"role": "assistant", "content": "The parser is fixed."}]
        context, targets = prepare_context(messages, 1)
        checked = validate_judgments(self.result(), context, targets, "presence")
        self.assertTrue(context["observation"]["technical_missing_reasoning"])
        self.assertTrue(all(v["admissible_label"] is None for v in checked.values()))

    def test_captured_empty_reasoning_differs_from_missing(self):
        messages = self.initial + [{"role": "assistant", "content": "The parser is fixed.", "reasoning": ""}]
        context, targets = prepare_context(messages, 1)
        checked = validate_judgments(self.result(), context, targets, "presence")
        self.assertFalse(context["observation"]["technical_missing_reasoning"])
        self.assertEqual(context["observation"]["captured_empty_reasoning_message_indices"], [1])
        self.assertTrue(all(v["admissible_label"] == "no" for v in checked.values()))

    def test_initial_boundary_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError, "initial_context_prefix_mismatch"):
            prepare_context(self.messages, 1, initial_context=[{"role": "user", "content": "Wrong"}])

    def test_full_context_cumulative_prefix_binding(self):
        messages = self.messages + [{"role": "user", "content": "Continue."}, {"role": "assistant", "reasoning": "This is not an evaluation after all.", "content": "Done."}]
        context, targets = prepare_context(messages, 1, "full", self.initial, self.messages)
        self.assertTrue(context["observation"]["prefix_structurally_included"])
        self.assertFalse(context["observation"]["current_actor_belief_estimate"])
        self.assertIn(1, targets)

    def test_thresholds_have_distinct_schema_identity(self):
        for instrument in INSTRUMENTS:
            self.assertEqual(schema(instrument)["properties"]["instrument"]["enum"], [instrument])

    def test_legacy_raw_response_content_is_preserved(self):
        raw = json.dumps({"status": "completed", "model": "gpt-6-luna", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(self.result())}]}]}).encode()
        response = SimpleNamespace(content=raw, status_code=200, headers={"x-request-id": "synthetic"})
        client = SimpleNamespace(max_retries=0, base_url="https://api.openai.com/v1/",
                                 responses=SimpleNamespace(with_raw_response=SimpleNamespace(create=lambda **kwargs: response)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompts = root / "prompts"
            prompts.mkdir()
            (prompts / "rubric.txt").write_text("Synthetic transport fixture.")
            (prompts / "presence.txt").write_text("Synthetic presence fixture.")
            result = call_reader(client, {"model": "gpt-6-luna"}, prompts, self.context, self.targets, "presence", root / "run")
            self.assertEqual(result["status"], "validated")
            self.assertEqual((root / "run/response.raw").read_bytes(), raw)

    def test_retrying_client_is_rejected_before_transport(self):
        def forbidden(**kwargs):
            self.fail("transport called with retries enabled")
        client = SimpleNamespace(max_retries=2, base_url="https://api.openai.com/v1/",
                                 responses=SimpleNamespace(with_raw_response=SimpleNamespace(create=forbidden)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rubric.txt").write_text("Synthetic fixture.")
            (root / "presence.txt").write_text("Synthetic fixture.")
            result = call_reader(client, {"model": "gpt-6-luna"}, root, self.context, self.targets, "presence", root / "run")
            self.assertEqual(result["status"], "technical_missing")
            self.assertEqual(result["validation_error"], "client_transport_contract_not_qualified")


if __name__ == "__main__":
    unittest.main()
