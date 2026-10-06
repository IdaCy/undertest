import copy
import unittest

from emergence.checkpoints.evidence import extract_evidence


def output(content, stop_reason="stop", **changes):
    value = {
        "model": "test-model",
        "choices": [{"message": {"role": "assistant", "content": content}, "stop_reason": stop_reason}],
    }
    value.update(changes)
    return value


class EvidenceTests(unittest.TestCase):
    def test_plain_text_is_preserved_without_inferred_reasoning(self):
        text = "This is probably an evaluation.\nACTION: B"
        evidence = extract_evidence(output(text))
        self.assertEqual(evidence["action_text"], text)
        self.assertEqual(evidence["raw_rendered_text"], text)
        self.assertEqual(evidence["reasoning_status"], "unavailable")
        self.assertFalse(evidence["reasoning_available"])
        self.assertIsNone(evidence["reasoning_complete"])

    def test_complete_think_does_not_supply_behavioral_action(self):
        evidence = extract_evidence(output("<think>ACTION: A is the tempting choice.</think>\nACTION: B"))
        self.assertEqual(evidence["action_text"], "ACTION: B")
        self.assertEqual(evidence["reasoning_text"], "ACTION: A is the tempting choice.")
        self.assertEqual(evidence["reasoning_status"], "available")
        self.assertTrue(evidence["reasoning_complete"])

    def test_unclosed_think_never_supplies_an_action(self):
        evidence = extract_evidence(output("<think>My rationale says ACTION: A", "max_tokens"))
        self.assertEqual(evidence["action_text"], "")
        self.assertEqual(evidence["reasoning_status"], "partial")
        self.assertFalse(evidence["reasoning_complete"])
        self.assertTrue(evidence["is_truncated"])
        self.assertEqual(evidence["completion_status"], "incomplete")

    def test_orphan_close_treats_prefix_as_partial_reasoning(self):
        evidence = extract_evidence(output("Maybe ACTION: A</think>\nACTION: B"))
        self.assertEqual(evidence["action_text"], "ACTION: B")
        self.assertEqual(evidence["reasoning_text"], "Maybe ACTION: A")
        self.assertEqual(evidence["reasoning_segments"][0]["status"], "missing_open")

    def test_verified_prompt_prefix_closes_without_inventing_generated_opening(self):
        text = "Consider ACTION: A</think>\nACTION: B"
        raw = output(text)
        evidence = extract_evidence(raw, reasoning_prefix_in_prompt=True)
        self.assertEqual(evidence["action_text"], "ACTION: B")
        self.assertEqual(evidence["reasoning_text"], "Consider ACTION: A")
        self.assertEqual(evidence["reasoning_status"], "available")
        self.assertTrue(evidence["reasoning_complete"])
        self.assertEqual(evidence["completion_status"], "complete")
        self.assertTrue(evidence["reasoning_prefix_in_prompt"])
        self.assertTrue(evidence["reasoning_prefix_applied"])
        self.assertEqual(evidence["reasoning_prefix_handling"], "prompt_prefix")
        self.assertEqual(evidence["reasoning_segments"][0]["opening_source"], "prompt")
        self.assertEqual(evidence["raw_rendered_text"], text)
        self.assertEqual(evidence["raw_output"], raw)
        unverified = extract_evidence(raw)
        self.assertEqual(unverified["reasoning_status"], "partial")
        self.assertFalse(unverified["reasoning_prefix_applied"])

    def test_unclosed_verified_prefix_treats_everything_as_unfinished_reasoning(self):
        for stop_reason in ("stop", "max_tokens", "unknown"):
            with self.subTest(stop_reason=stop_reason):
                evidence = extract_evidence(output("ACTION: A", stop_reason), reasoning_prefix_in_prompt=True)
                self.assertEqual(evidence["action_text"], "")
                self.assertEqual(evidence["reasoning_text"], "ACTION: A")
                self.assertEqual(evidence["reasoning_status"], "partial")
                self.assertFalse(evidence["reasoning_complete"])
                self.assertNotEqual(evidence["completion_status"], "complete")

    def test_nested_prompt_prefix_does_not_close_the_injected_outer_block(self):
        for preamble in ("", "Outer working.\n"):
            for nested in ("<think>nested</think>", "<think><think>deep</think>nested</think>"):
                for stop_reason in ("stop", "max_tokens", "model_length", "unknown"):
                    text = f"{preamble}{nested}\nACTION: B\n"
                    raw = output(text, stop_reason)
                    with self.subTest(preamble=preamble, nested=nested, stop_reason=stop_reason):
                        evidence = extract_evidence(raw, reasoning_prefix_in_prompt=True)
                        self.assertEqual(evidence["action_text"], "")
                        self.assertEqual(evidence["reasoning_text"], text.strip())
                        self.assertEqual(evidence["reasoning_status"], "partial")
                        self.assertFalse(evidence["reasoning_complete"])
                        self.assertNotEqual(evidence["completion_status"], "complete")
                        segment, = evidence["reasoning_segments"]
                        self.assertEqual(segment["status"], "missing_close")
                        self.assertEqual(segment["opening_source"], "prompt")
                        self.assertEqual((segment["start"], segment["end"]), (0, len(text)))
                        self.assertEqual(evidence["raw_output"], raw)
                        self.assertEqual(evidence["raw_rendered_text"], text)

    def test_nested_prompt_prefix_requires_all_closings_before_action(self):
        prefix = "outer<think>inner<think>deep</think>inner end</think>outer end"
        text = f"{prefix}</think>\nACTION: B"
        for stop_reason in ("stop", "max_tokens"):
            with self.subTest(stop_reason=stop_reason):
                evidence = extract_evidence(output(text, stop_reason), reasoning_prefix_in_prompt=True)
                self.assertEqual(evidence["action_text"], "ACTION: B")
                self.assertEqual(evidence["reasoning_text"], prefix)
                self.assertFalse(evidence["reasoning_has_partial_tags"])
                segment, = evidence["reasoning_segments"]
                self.assertEqual(segment["status"], "complete")
                self.assertEqual((segment["start"], segment["end"]), (0, len(prefix)))
                self.assertEqual(text[segment["start"]:segment["end"]], prefix)
                self.assertEqual(evidence["is_truncated"], stop_reason == "max_tokens")
                self.assertEqual(evidence["completion_status"], "complete" if stop_reason == "stop" else "incomplete")

    def test_nested_prefix_and_later_raw_blocks_preserve_original_offsets(self):
        prefix = "outer<think>inner</think>outer end"
        text = f"{prefix}</think>\n<think>later</think>\nACTION: B"
        evidence = extract_evidence(output(text), reasoning_prefix_in_prompt=True)
        self.assertEqual(evidence["action_text"], "ACTION: B")
        first, second = evidence["reasoning_segments"]
        self.assertEqual(first["opening_source"], "prompt")
        self.assertEqual(second["opening_source"], "generated")
        self.assertEqual(text[first["start"]:first["end"]], prefix)
        self.assertEqual(text[second["start"]:second["end"]], "later")
        self.assertEqual(evidence["raw_output"], output(text))

    def test_extra_closing_after_nested_outer_close_remains_partial_evidence(self):
        text = "<think>inner</think></think>orphan prefix</think>\nACTION: B"
        evidence = extract_evidence(output(text), reasoning_prefix_in_prompt=True)
        first, orphan = evidence["reasoning_segments"]
        self.assertEqual(first["status"], "complete")
        self.assertEqual(orphan["status"], "missing_open")
        self.assertEqual(evidence["reasoning_status"], "partial")
        self.assertFalse(evidence["reasoning_complete"])
        self.assertEqual(evidence["action_text"], "ACTION: B")

    def test_raw_nested_block_does_not_inherit_an_unrequested_outer_prefix(self):
        text = "<think>nested</think>\nACTION: B"
        raw = extract_evidence(output(text))
        prefixed = extract_evidence(output(text), reasoning_prefix_in_prompt=True)
        self.assertEqual(raw["action_text"], "ACTION: B")
        self.assertEqual(raw["reasoning_text"], "nested")
        self.assertEqual(raw["reasoning_segments"][0]["start"], len("<think>"))
        self.assertEqual(raw["reasoning_segments"][0]["end"], len("<think>nested"))
        self.assertEqual(raw["completion_status"], "complete")
        self.assertEqual(prefixed["action_text"], "")

    def test_typed_reasoning_precedence_survives_nested_native_content(self):
        blocks = [
            {"type": "reasoning", "reasoning": "<think>nested</think>\nACTION: A"},
            {"type": "text", "text": "<think>inline</think>\nACTION: B"},
        ]
        evidence = extract_evidence(output(blocks), reasoning_prefix_in_prompt=True)
        self.assertEqual(evidence["action_text"], "ACTION: B")
        self.assertFalse(evidence["reasoning_prefix_applied"])
        self.assertEqual(evidence["reasoning_prefix_handling"], "typed_channel")
        self.assertEqual(evidence["reasoning_sources"], ["typed", "think_tag"])
        self.assertFalse(evidence["reasoning_has_partial_tags"])
        self.assertEqual(evidence["completion_status"], "complete")
        self.assertEqual(evidence["raw_content"], blocks)

    def test_typed_channel_takes_precedence_over_prompt_prefix(self):
        evidence = extract_evidence(output([
            {"type": "reasoning", "reasoning": "Consider ACTION: A"},
            {"type": "text", "text": "ACTION: B"},
        ]), reasoning_prefix_in_prompt=True)
        self.assertEqual(evidence["action_text"], "ACTION: B")
        self.assertEqual(evidence["reasoning_text"], "Consider ACTION: A")
        self.assertEqual(evidence["reasoning_sources"], ["typed"])
        self.assertEqual(len(evidence["reasoning_segments"]), 1)
        self.assertTrue(evidence["reasoning_prefix_in_prompt"])
        self.assertFalse(evidence["reasoning_prefix_applied"])
        self.assertEqual(evidence["reasoning_prefix_handling"], "typed_channel")
        self.assertEqual(evidence["completion_status"], "complete")

    def test_empty_or_redacted_typed_channel_still_prevents_prefix_reinterpretation(self):
        for block in (
            {"type": "reasoning", "reasoning": ""},
            {"type": "reasoning", "reasoning": "opaque", "redacted": True},
        ):
            with self.subTest(block=block):
                evidence = extract_evidence(output([block, {"type": "text", "text": "ACTION: B"}]), reasoning_prefix_in_prompt=True)
                self.assertEqual(evidence["action_text"], "ACTION: B")
                self.assertFalse(evidence["reasoning_available"])
                self.assertFalse(evidence["reasoning_prefix_applied"])
                self.assertEqual(evidence["reasoning_prefix_handling"], "typed_channel")

    def test_prefix_and_later_generated_reasoning_keep_correct_span_provenance(self):
        text = "first</think>ACTION: B\n<think>second</think>"
        evidence = extract_evidence(output(text), reasoning_prefix_in_prompt=True)
        self.assertEqual(evidence["action_text"], "ACTION: B")
        self.assertEqual(evidence["reasoning_text"], "first\n\nsecond")
        self.assertEqual([segment["opening_source"] for segment in evidence["reasoning_segments"]], ["prompt", "generated"])
        for segment in evidence["reasoning_segments"]:
            self.assertEqual(text[segment["start"]:segment["end"]], segment["text"])

    def test_prefix_handling_is_independent_for_each_choice(self):
        raw = output("still thinking ACTION: A", "max_tokens")
        raw["choices"].extend(output([
            {"type": "reasoning", "reasoning": "finished reasoning"},
            {"type": "text", "text": "ACTION: B"},
        ])["choices"])
        evidence = extract_evidence(raw, reasoning_prefix_in_prompt=True)
        self.assertEqual(evidence["action_text"], "")
        self.assertEqual([choice["reasoning_prefix_applied"] for choice in evidence["choices"]], [True, False])
        self.assertEqual(evidence["choices"][1]["action_text"], "ACTION: B")

    def test_prefix_without_a_response_does_not_fabricate_a_reasoning_segment(self):
        evidence = extract_evidence({"choices": []}, reasoning_prefix_in_prompt=True)
        self.assertTrue(evidence["reasoning_prefix_in_prompt"])
        self.assertFalse(evidence["reasoning_prefix_applied"])
        self.assertEqual(evidence["reasoning_status"], "unavailable")
        self.assertEqual(evidence["reasoning_segments"], [])

    def test_prefix_flag_rejects_truthy_nonboolean_configuration(self):
        with self.assertRaisesRegex(TypeError, "boolean"):
            extract_evidence(output("ACTION: A"), reasoning_prefix_in_prompt="false")

    def test_nested_and_multiple_reasoning_blocks_are_excluded(self):
        evidence = extract_evidence(output("<think>first<think>ACTION: A</think>end</think><think>second</think>ACTION: B"))
        self.assertEqual(evidence["action_text"], "ACTION: B")
        self.assertEqual(evidence["reasoning_text"], "firstACTION: Aend\n\nsecond")
        self.assertEqual(len(evidence["reasoning_segments"]), 2)

    def test_removal_cannot_synthesize_an_action_keyword(self):
        evidence = extract_evidence(output("ACT<think>rationale</think>ION: B"))
        self.assertEqual(evidence["action_text"], "ACT\nION: B")

    def test_typed_reasoning_is_separate_and_raw_structure_survives(self):
        blocks = [
            {"type": "reasoning", "reasoning": "I considered ACTION: A", "redacted": False},
            {"type": "text", "text": "ACTION: B"},
        ]
        evidence = extract_evidence(output(blocks))
        self.assertEqual(evidence["action_text"], "ACTION: B")
        self.assertEqual(evidence["reasoning_text"], "I considered ACTION: A")
        self.assertEqual(evidence["raw_content"], blocks)
        self.assertEqual(evidence["reasoning_sources"], ["typed"])

    def test_empty_and_redacted_reasoning_are_not_available_cot(self):
        empty = extract_evidence(output([{"type": "reasoning", "reasoning": " \n"}]))
        self.assertEqual(empty["reasoning_status"], "empty")
        self.assertFalse(empty["reasoning_available"])
        blocks = [{"type": "reasoning", "reasoning": "opaque ACTION: A", "redacted": True, "summary": "ACTION: B"}]
        redacted = extract_evidence(output(blocks))
        self.assertEqual(redacted["reasoning_status"], "redacted")
        self.assertFalse(redacted["reasoning_available"])
        self.assertEqual(redacted["action_text"], "")
        self.assertEqual(redacted["reasoning_text"], "")
        self.assertEqual(redacted["raw_rendered_text"], "")
        self.assertEqual(redacted["reasoning_segments"][0]["summary"], "ACTION: B")
        self.assertEqual(redacted["raw_content"], blocks)

    def test_visible_plus_redacted_reasoning_is_partial(self):
        evidence = extract_evidence(output([
            {"type": "reasoning", "reasoning": "visible"},
            {"type": "reasoning", "reasoning": "opaque", "redacted": True},
            {"type": "text", "text": "ACTION: B"},
        ]))
        self.assertEqual(evidence["reasoning_status"], "partial")
        self.assertTrue(evidence["reasoning_available"])
        self.assertFalse(evidence["reasoning_complete"])

    def test_typed_reasoning_with_cap_or_unknown_finish_is_not_complete(self):
        blocks = [{"type": "reasoning", "reasoning": "still thinking"}]
        capped = extract_evidence(output(blocks, "max_tokens"))
        self.assertEqual(capped["reasoning_status"], "partial")
        self.assertFalse(capped["reasoning_complete"])
        unknown = extract_evidence(output(blocks, "unknown"))
        self.assertTrue(unknown["reasoning_available"])
        self.assertIsNone(unknown["reasoning_complete"])

    def test_actual_inspect_model_output_dump_is_supported(self):
        from inspect_ai.model import ChatCompletionChoice, ChatMessageAssistant, ContentReasoning, ContentText, ModelOutput

        model_output = ModelOutput(model="test-model", choices=[ChatCompletionChoice(
            message=ChatMessageAssistant(content=[
                ContentReasoning(reasoning="Consider ACTION: A"),
                ContentText(text="ACTION: B"),
            ]), stop_reason="stop",
        )])
        evidence = extract_evidence(model_output.model_dump(mode="json"))
        self.assertEqual(evidence["action_text"], "ACTION: B")
        self.assertEqual(evidence["reasoning_text"], "Consider ACTION: A")
        self.assertEqual(evidence["raw_output"]["completion"], "ACTION: B")

    def test_tags_split_across_adjacent_text_blocks_are_recognized(self):
        evidence = extract_evidence(output([
            {"type": "text", "text": "<thi"},
            {"type": "text", "text": "nk>ACTION: A</think>ACTION: B"},
        ]))
        self.assertEqual(evidence["action_text"], "ACTION: B")

    def test_typed_reasoning_cannot_join_an_action_keyword(self):
        evidence = extract_evidence(output([
            {"type": "text", "text": "ACT"},
            {"type": "reasoning", "reasoning": "rationale"},
            {"type": "text", "text": "ION: B"},
        ]))
        self.assertEqual(evidence["action_text"], "ACT\nION: B")

    def test_partial_tags_are_recorded_conservatively(self):
        for tail in ("<thi", "<think", "<THINK "):
            with self.subTest(tail=tail):
                evidence = extract_evidence(output(tail, "max_tokens"))
                self.assertEqual(evidence["action_text"], "")
                self.assertEqual(evidence["reasoning_status"], "partial")
        evidence = extract_evidence(output("ACTION: A</thi", "max_tokens"))
        self.assertEqual(evidence["action_text"], "")
        self.assertEqual(evidence["reasoning_text"], "ACTION: A")

    def test_every_choice_retains_termination_but_only_first_supplies_action(self):
        raw = output("<think>ACTION: A", "model_length")
        second = output("ACTION: B")["choices"][0]
        second["stop_details"] = {"description": "normal stop"}
        raw["choices"].append(second)
        evidence = extract_evidence(raw)
        self.assertEqual(evidence["choice_count"], 2)
        self.assertEqual(evidence["selected_choice"], 0)
        self.assertEqual(evidence["action_text"], "")
        self.assertEqual([choice["is_truncated"] for choice in evidence["choices"]], [True, False])
        self.assertEqual(evidence["choices"][1]["stop_details"], {"description": "normal stop"})

    def test_unknown_finish_and_error_do_not_become_completed(self):
        for stop in ("unknown", None, "unrecognized_vendor_reason"):
            with self.subTest(stop=stop):
                evidence = extract_evidence(output("ACTION: B", stop))
                self.assertIsNone(evidence["is_truncated"])
                self.assertEqual(evidence["completion_status"], "unknown")
        evidence = extract_evidence(output("ACTION: B", "stop", error="provider error"))
        self.assertIsNone(evidence["is_truncated"])
        self.assertEqual(evidence["completion_status"], "unknown")
        self.assertEqual(evidence["output_error"], "provider error")

    def test_no_choices_keeps_output_without_fabricating_an_action(self):
        evidence = extract_evidence({"choices": [], "completion": "ACTION: B", "error": "no choices"})
        self.assertIsNone(evidence["selected_choice"])
        self.assertEqual(evidence["action_text"], "")
        self.assertEqual(evidence["raw_output"]["completion"], "ACTION: B")
        self.assertEqual(evidence["completion_status"], "unknown")

    def test_extraction_is_pure_and_returned_copies_are_independent(self):
        raw = output([{"type": "reasoning", "reasoning": "thinking", "signature": "signature"}])
        before = copy.deepcopy(raw)
        evidence = extract_evidence(raw)
        self.assertEqual(raw, before)
        evidence["raw_content"][0]["reasoning"] = "changed"
        evidence["raw_output"]["choices"][0]["message"]["content"][0]["signature"] = "changed"
        self.assertEqual(raw, before)
        self.assertEqual(evidence["choices"][0]["raw_content"][0]["reasoning"], "thinking")


if __name__ == "__main__":
    unittest.main()
