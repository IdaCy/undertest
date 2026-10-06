import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from emergence.checkpoints.choice import build_panel
from emergence.checkpoints.evidence import extract_evidence
from emergence.checkpoints.response import score_response


class ResponseEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.metadata = next(row["metadata"] for row in build_panel()["samples"] if row["metadata"]["sample_kind"] == "main")
        self.identifier = next(iter(self.metadata["choices"]))

    def assess(self, content, prefix=False, stop_reason="stop"):
        evidence = extract_evidence({"choices": [{"stop_reason": stop_reason, "message": {"content": content}}]}, reasoning_prefix_in_prompt=prefix)
        return score_response(self.metadata, evidence)

    def test_malformed_action_never_marks_visible_working_absent(self):
        for suffix in (f"action: {self.identifier}", "ACTION:", "", "I choose the first."):
            text = "This may be an evaluation of my behavior.\n" + suffix
            outcomes, annotation = self.assess(text)
            self.assertFalse(outcomes["format_valid"])
            self.assertIsNone(outcomes["shortcut_selection"])
            self.assertTrue(annotation["visible_text_available"])
            self.assertIn("evaluation", annotation["visible_text_for_annotation"])
            self.assertIsNone(annotation["elicited_working_available"])
            self.assertEqual(annotation["elicited_working_status"], "unsegmented")

    def test_inline_post_action_reasoning_is_not_a_terminal_action(self):
        text = f"ACTION: {self.identifier}\n<think>This looks like an evaluation.</think>"
        outcomes, annotation = self.assess(text)
        self.assertFalse(outcomes["format_valid"])
        self.assertEqual(outcomes["parser_status"], "nonterminal_action_in_raw_response")
        self.assertIsNone(outcomes["shortcut_selection"])
        self.assertEqual(annotation["reasoning_timing"], "contains_post_action_content")

    def test_repeated_terminal_id_inside_unfinished_reasoning_is_not_selected(self):
        for prefix in ("", "Post-choice thought.\n", "<think>nested thought</think>\n"):
            for newline in ("\n", "\r\n"):
                for suffix in ("", newline, "  \t" + newline):
                    text = f"ACTION: {self.identifier}{newline}<think>{newline}{prefix}ACTION: {self.identifier}{suffix}"
                    with self.subTest(prefix=prefix, newline=newline, suffix=suffix):
                        outcomes, annotation = self.assess(text, stop_reason="max_tokens")
                        self.assertFalse(outcomes["format_valid"])
                        self.assertFalse(outcomes["behavior_observed"])
                        self.assertIsNone(outcomes["selected_id"])
                        self.assertIsNone(outcomes["shortcut_selection"])
                        self.assertEqual(outcomes["parser_status"], "nonterminal_action_in_raw_response")
                        self.assertEqual(outcomes["recoverable_nonterminal_id"], self.identifier)
                        self.assertFalse(annotation["action_terminal_in_raw_response"])
                        self.assertTrue(annotation["raw_terminal_action_in_reasoning"])
                        self.assertEqual(annotation["reasoning_timing"], "contains_post_action_content")
                        candidate = annotation["raw_terminal_action_candidate"]
                        self.assertEqual(text[candidate["start"]:candidate["end"]], f"ACTION: {self.identifier}")
                        self.assertGreater(candidate["start"], text.index("<think>"))

    def test_same_id_in_completed_pre_action_reasoning_does_not_hide_valid_action(self):
        text = f"<think>\nACTION: {self.identifier}\n</think>\nACTION: {self.identifier}\n"
        outcomes, annotation = self.assess(text, stop_reason="max_tokens")
        self.assertTrue(outcomes["format_valid"])
        self.assertTrue(outcomes["behavior_observed"])
        self.assertTrue(annotation["action_terminal_in_raw_response"])
        self.assertFalse(annotation["raw_terminal_action_in_reasoning"])
        self.assertEqual(annotation["reasoning_timing"], "pre_action")
        self.assertGreater(annotation["raw_terminal_action_candidate"]["start"], text.index("</think>"))

    def test_repeated_id_in_post_action_reasoning_across_text_blocks_is_rejected(self):
        for content, prefix in (
            ([
                {"type": "text", "text": f"ACTION: {self.identifier}\n<thi"},
                {"type": "text", "text": f"nk>\nACTION: {self.identifier}"},
            ], False),
            ([
                {"type": "reasoning", "reasoning": "Pre-choice working."},
                {"type": "text", "text": f"ACTION: {self.identifier}\n<think>\nACTION: {self.identifier}"},
            ], True),
        ):
            with self.subTest(content=content):
                outcomes, annotation = self.assess(content, prefix=prefix, stop_reason="max_tokens")
                self.assertFalse(outcomes["behavior_observed"])
                self.assertFalse(annotation["action_terminal_in_raw_response"])
                self.assertTrue(annotation["raw_terminal_action_in_reasoning"])
                self.assertEqual(annotation["reasoning_timing"], "contains_post_action_content")

    def test_typed_post_action_reasoning_stays_post_action_even_if_redacted(self):
        for reasoning in ({"type": "reasoning", "reasoning": "Reviewing choice."}, {"type": "reasoning", "redacted": True}):
            outcomes, annotation = self.assess([
                {"type": "text", "text": f"ACTION: {self.identifier}"}, reasoning,
            ])
            self.assertFalse(outcomes["format_valid"])
            self.assertEqual(annotation["reasoning_timing"], "contains_post_action_content")

    def test_typed_repeated_id_followed_only_by_empty_text_is_still_post_action(self):
        for final_text in ("", "\n \t"):
            outcomes, annotation = self.assess([
                {"type": "text", "text": f"ACTION: {self.identifier}"},
                {"type": "reasoning", "reasoning": f"ACTION: {self.identifier}"},
                {"type": "text", "text": final_text},
            ], stop_reason="max_tokens")
            self.assertFalse(outcomes["behavior_observed"])
            self.assertFalse(annotation["action_terminal_in_raw_response"])
            self.assertEqual(annotation["reasoning_timing"], "contains_post_action_content")

    def test_pre_action_inline_typed_and_prompt_prefix_remain_valid(self):
        for content, prefix in (
            (f"<think>Working.</think>\nACTION: {self.identifier}", False),
            (f"Working.</think>\nACTION: {self.identifier}", True),
            ([{"type": "reasoning", "reasoning": "Working."}, {"type": "text", "text": f"ACTION: {self.identifier}"}], True),
        ):
            outcomes, annotation = self.assess(content, prefix)
            self.assertTrue(outcomes["format_valid"])
            self.assertEqual(annotation["reasoning_timing"], "pre_action")

    def test_valid_final_action_remains_observed_at_token_cap_or_unknown_finish(self):
        for stop_reason in ("max_tokens", "model_length", "unknown"):
            with self.subTest(stop_reason=stop_reason):
                outcomes, annotation = self.assess(f"Working.\nACTION: {self.identifier}", stop_reason=stop_reason)
                self.assertTrue(outcomes["format_valid"])
                self.assertTrue(outcomes["behavior_observed"])
                self.assertTrue(annotation["action_terminal_in_raw_response"])

    def test_orphan_tag_choice_separates_literal_and_segmented_endpoints(self):
        text = f"Working.\nACTION: {self.identifier}\n</think>\nWorking.\nACTION: {self.identifier}"
        outcomes, annotation = self.assess(text)
        self.assertTrue(outcomes["segmented_format_valid"])
        self.assertTrue(outcomes["behavior_observed"])
        self.assertFalse(outcomes["raw_text_format_valid"])
        self.assertEqual(outcomes["raw_text_parser_status"], "multiple_action_lines")
        self.assertTrue(outcomes["choice_depends_on_segmentation"])
        self.assertFalse(outcomes["conservative_behavior_observed"])
        self.assertIsNone(outcomes["conservative_shortcut_selection"])
        self.assertIsNone(outcomes["conservative_task_success"])
        self.assertEqual(annotation["inline_segmentation_status"], "partial_or_unknown_opening")

    def test_boundary_sensitivity_includes_explicit_tagged_action_mentions(self):
        text = f"<think>Example:\nACTION: {self.identifier}\n</think>\nACTION: {self.identifier}"
        outcomes, annotation = self.assess(text)
        self.assertTrue(outcomes["segmented_format_valid"])
        self.assertFalse(outcomes["raw_text_format_valid"])
        self.assertTrue(outcomes["choice_depends_on_segmentation"])
        self.assertIsNone(outcomes["conservative_shortcut_selection"])
        self.assertEqual(annotation["inline_segmentation_status"], "complete")

    def test_partial_reasoning_without_action_mention_does_not_change_choice(self):
        text = f"Working.</think>\nACTION: {self.identifier}"
        outcomes, annotation = self.assess(text)
        self.assertTrue(outcomes["segmented_format_valid"])
        self.assertTrue(outcomes["raw_text_format_valid"])
        self.assertFalse(outcomes["choice_depends_on_segmentation"])
        self.assertTrue(outcomes["conservative_behavior_observed"])
        self.assertEqual(outcomes["conservative_shortcut_selection"], outcomes["shortcut_selection"])
        self.assertEqual(annotation["inline_segmentation_status"], "partial_or_unknown_opening")

    def test_unknown_actions_remain_unknown_in_conservative_endpoint(self):
        outcomes, _ = self.assess("Working but no final selection.")
        self.assertFalse(outcomes["segmented_format_valid"])
        self.assertFalse(outcomes["raw_text_format_valid"])
        self.assertFalse(outcomes["conservative_behavior_observed"])
        self.assertIsNone(outcomes["conservative_shortcut_selection"])

    def test_native_nested_prefix_requires_outer_closure_before_observed_behavior(self):
        for outer_closed in (False, True):
            for stop_reason in ("stop", "max_tokens", "unknown"):
                text = "<think>nested</think>" + ("</think>" if outer_closed else "")
                text += f"\nACTION: {self.identifier}"
                with self.subTest(outer_closed=outer_closed, stop_reason=stop_reason):
                    outcomes, annotation = self.assess(text, prefix=True, stop_reason=stop_reason)
                    self.assertEqual(outcomes["format_valid"], outer_closed)
                    self.assertEqual(outcomes["behavior_observed"], outer_closed)
                    self.assertEqual(annotation["action_terminal_in_raw_response"], outer_closed)
                    self.assertEqual(annotation["raw_terminal_action_in_reasoning"], not outer_closed)
                    if not outer_closed:
                        self.assertIsNone(outcomes["selected_id"])
                        self.assertNotEqual(annotation["reasoning_timing"], "pre_action")


class ResponseExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_retain_and_export_preserve_unknown_behavior_and_original_output(self):
        from inspect_ai.log import EvalSample
        from inspect_ai.model import ModelOutput
        from inspect_ai.util import Store
        from shared.results import export_sample

        root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location("development_response_test_runner", root / "src/emergence/checkpoints/calibration/run_panel.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        metadata = next(row["metadata"] for row in build_panel()["samples"] if row["metadata"]["sample_kind"] == "main")
        identifier = next(iter(metadata["choices"]))
        text = f"ACTION: {identifier}\n<think>\nACTION: {identifier}"
        model_output = ModelOutput.from_content(model="mockllm/model", content=text, stop_reason="max_tokens")
        raw_output = model_output.model_dump(mode="json")
        state = SimpleNamespace(metadata=metadata, output=model_output)
        saved = Store()
        with patch.object(runner, "store", return_value=saved):
            await runner.retain_evidence(state)
        sample = EvalSample(
            id="repeated-id", epoch=1, input="synthetic parser check", target="",
            messages=[model_output.message], output=model_output,
            metadata=metadata, store=dict(saved.items()),
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory, errors = export_sample(Path(temporary), "repeated-id", sample, None, {})
            self.assertEqual(errors, [])
            outcomes = json.loads((directory / "development/outcomes.json").read_text())
            evidence = json.loads((directory / "development/evidence.json").read_text())
            self.assertFalse(outcomes["format_valid"])
            self.assertFalse(outcomes["behavior_observed"])
            self.assertIsNone(outcomes["shortcut_selection"])
            self.assertFalse(evidence["action_terminal_in_raw_response"])
            self.assertTrue(evidence["raw_terminal_action_in_reasoning"])
            self.assertEqual(evidence["reasoning_timing"], "contains_post_action_content")
            self.assertEqual(evidence["completion_status"], "incomplete")
            self.assertTrue(evidence["is_truncated"])
            self.assertEqual(evidence["raw_output"], raw_output)
            self.assertEqual(json.loads((directory / "development/model_output.json").read_text()), raw_output)


if __name__ == "__main__":
    unittest.main()
