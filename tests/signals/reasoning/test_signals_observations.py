import copy
import json
import unittest

from signals.reasoning.observations import project_observation


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.context = [
            {"role": "system", "content": "Follow the task."},
            {"role": "user", "content": "Repair the ledger."},
        ]
        self.tools = [{"type": "function", "function": {
            "name": "inspect_records", "description": "Read visible records.",
            "parameters": {"type": "object", "properties": {
                "source": {"type": "string", "enum": ["current"]},
            }, "required": ["source"], "additionalProperties": False},
        }}]
        self.messages = [
            {"role": "assistant", "content": "PUBLIC_PLAN", "reasoning_content": "PRIVATE_THOUGHT",
             "tool_calls": [{"id": "cohort-secret-run-42", "type": "function", "function": {
                 "name": "inspect_records", "arguments": '{"source":"ACTION_ARGUMENT"}',
             }}]},
            {"role": "tool", "tool_call_id": "cohort-secret-run-42", "content": "TOOL_RESULT"},
            {"role": "assistant", "content": "FINAL_PUBLIC_PROSE", "reasoning": "FINAL_REASONING"},
        ]

    def project(self, channel, cutoff=3, **changes):
        arguments = dict(initial_context=self.context, messages=self.messages,
                         tools=self.tools, channel=channel, cutoff=cutoff)
        arguments.update(changes)
        return project_observation(**arguments)

    def test_channels_share_context_and_have_exact_visibility(self):
        projected = {channel: self.project(channel) for channel in ("context", "cot", "action", "combined")}
        for view in projected.values():
            self.assertEqual(view["initial_context"], self.context)
            self.assertEqual(view["tools"], self.tools)
            self.assertEqual(view["cutoff"], 3)
            self.assertNotIn("cohort-secret-run-42", json.dumps(view))
        self.assertEqual(projected["context"]["messages"], [])
        self.assertNotIn("reasoning_status", projected["context"])
        self.assertNotIn("reasoning_status", projected["action"])
        cot = json.dumps(projected["cot"])
        action = json.dumps(projected["action"])
        combined = json.dumps(projected["combined"])
        for marker in ("PRIVATE_THOUGHT", "FINAL_REASONING"):
            self.assertIn(marker, cot)
            self.assertNotIn(marker, action)
            self.assertIn(marker, combined)
        for marker in ("PUBLIC_PLAN", "ACTION_ARGUMENT", "TOOL_RESULT", "FINAL_PUBLIC_PROSE"):
            self.assertNotIn(marker, cot)
            self.assertIn(marker, action)
            self.assertIn(marker, combined)
        self.assertEqual(projected["action"]["messages"][0]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(projected["action"]["messages"][1]["tool_call_id"], "call_1")

    def test_post_cutoff_objects_are_never_read(self):
        later = self.messages + [{"role": "checker", "content": "SECRET_OUTCOME"}, object()]
        for channel in ("context", "cot", "action", "combined"):
            self.assertEqual(self.project(channel), self.project(channel, messages=later))
            self.assertNotIn("SECRET_OUTCOME", json.dumps(self.project(channel, messages=later)))
        pending = self.project("combined", cutoff=1)
        self.assertIn("ACTION_ARGUMENT", json.dumps(pending))
        self.assertNotIn("TOOL_RESULT", json.dumps(pending))

    def test_unknown_metadata_is_removed_at_every_supported_level(self):
        context, messages, tools = copy.deepcopy((self.context, self.messages, self.tools))
        context[0]["outcome"] = "SECRET_OUTCOME"
        context[1]["run_id"] = "SECRET_RUN"
        messages[0]["scores"] = {"gaming": "SECRET_SCORE"}
        messages[0]["metadata"] = {"cohort": "SECRET_COHORT"}
        messages[0]["tool_calls"][0]["outcome"] = "SECRET_CALL"
        messages[0]["tool_calls"][0]["function"]["scores"] = "SECRET_FUNCTION"
        messages[1]["name"] = "SECRET_FAMILY"
        tools[0]["label"] = "SECRET_TOOL_LABEL"
        tools[0]["function"]["outcome"] = "SECRET_TOOL_OUTCOME"
        tools[0]["function"]["parameters"]["score"] = "SECRET_SCHEMA"
        tools[0]["function"]["parameters"]["properties"]["source"]["metadata"] = "SECRET_NESTED"
        for channel in ("context", "cot", "action", "combined"):
            actual = self.project(channel, initial_context=context, messages=messages, tools=tools)
            self.assertEqual(actual, self.project(channel))
            self.assertNotIn("SECRET_", json.dumps(actual))

    def test_typed_blocks_are_projected_without_signatures_or_redacted_text(self):
        messages = [{"role": "assistant", "content": [
            {"type": "reasoning", "reasoning": "TYPED_THOUGHT", "signature": "SECRET_SIGNATURE"},
            {"type": "text", "text": "VISIBLE_TEXT", "score": "SECRET_SCORE"},
            {"type": "reasoning", "text": "SECOND_THOUGHT"},
            {"type": "reasoning", "reasoning": "SECRET_REDACTED", "redacted": True,
             "summary": "SECRET_SUMMARY"},
        ]}]
        cot = self.project("cot", 1, messages=messages)
        action = self.project("action", 1, messages=messages)
        self.assertEqual(cot["messages"], [{"role": "assistant", "reasoning": ["TYPED_THOUGHT", "SECOND_THOUGHT"]}])
        self.assertEqual(action["messages"], [{"role": "assistant", "content": [{"type": "text", "text": "VISIBLE_TEXT"}]}])
        for channel in ("context", "cot", "action", "combined"):
            self.assertNotIn("SECRET_", json.dumps(self.project(channel, 1, messages=messages)))

    def test_reasoning_availability_is_not_an_awareness_label(self):
        cases = [
            ({"content": "done"}, "unavailable"),
            ({"content": "done", "reasoning_content": None}, "unavailable"),
            ({"content": "done", "reasoning_content": ""}, "absent"),
            ({"content": "done", "reasoning": "   "}, "absent"),
            ({"content": "done", "reasoning_content": "Arithmetic only."}, "present"),
            ({"content": [{"type": "reasoning", "reasoning": "", "redacted": True}]}, "unavailable"),
            ({"content": [{"type": "reasoning", "summary": "Summary only."}]}, "unavailable"),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                messages = [{"role": "assistant", **body}]
                for channel in ("cot", "combined"):
                    self.assertEqual(self.project(channel, 1, messages=messages)["reasoning_status"], expected)
        mixed = [{"role": "assistant", "reasoning": ""}, {"role": "assistant", "content": "done"}]
        self.assertEqual(self.project("cot", 2, messages=mixed)["reasoning_status"], "unavailable")
        self.assertEqual(self.project("cot", 0)["reasoning_status"], "unavailable")

    def test_plain_prose_and_think_tags_are_not_extracted_as_reasoning(self):
        prose = "<think>I know this is an evaluation.</think> Final answer."
        messages = [{"role": "assistant", "content": prose}]
        cot = self.project("cot", 1, messages=messages)
        self.assertEqual(cot["messages"], [])
        self.assertEqual(cot["reasoning_status"], "unavailable")
        self.assertEqual(self.project("action", 1, messages=messages)["messages"][0]["content"], prose)

    def test_multiple_calls_keep_local_references_without_original_ids(self):
        messages = copy.deepcopy(self.messages[:2])
        second = copy.deepcopy(messages[0]["tool_calls"][0])
        second["id"] = "SECRET_SECOND_CALL"
        messages[0]["tool_calls"].append(second)
        messages.append({"role": "tool", "tool_call_id": "SECRET_SECOND_CALL", "content": "second result"})
        action = self.project("action", 3, messages=messages)
        self.assertEqual([call["id"] for call in action["messages"][0]["tool_calls"]], ["call_1", "call_2"])
        self.assertEqual([item["tool_call_id"] for item in action["messages"][1:]], ["call_1", "call_2"])
        self.assertNotIn("SECRET_SECOND_CALL", json.dumps(action))
        messages[0]["tool_calls"][1]["id"] = messages[0]["tool_calls"][0]["id"]
        with self.assertRaises(ValueError):
            self.project("action", 3, messages=messages)

    def test_hidden_channel_changes_do_not_change_observations(self):
        other = copy.deepcopy(self.messages)
        other[0]["reasoning_content"] = {"unreadable": "SECRET_THOUGHT"}
        other[2]["reasoning"] = None
        self.assertEqual(self.project("action"), self.project("action", messages=other))
        other = copy.deepcopy(self.messages)
        other[0]["tool_calls"] = "SECRET_MALFORMED_HIDDEN_ACTION"
        other[0]["content"] = "DIFFERENT_PUBLIC_TEXT"
        other[1]["content"] = {"SECRET_TOOL_DATA": object()}
        other[1]["tool_call_id"] = None
        other[2]["content"] = "DIFFERENT_FINAL_PROSE"
        self.assertEqual(self.project("cot"), self.project("cot", messages=other))
        self.assertEqual(self.project("context"), self.project("context", messages=[object()] * 3))

    def test_input_and_returned_values_do_not_alias(self):
        before = copy.deepcopy((self.context, self.messages, self.tools))
        result = self.project("combined")
        result["initial_context"][0]["content"] = "changed"
        result["tools"][0]["function"]["parameters"]["properties"]["source"]["enum"].append("changed")
        result["messages"][0]["tool_calls"][0]["function"]["arguments"] = "changed"
        self.assertEqual((self.context, self.messages, self.tools), before)

    def test_invalid_cutoffs_channels_and_visible_protocol_fail_closed(self):
        for cutoff in (-1, 4, True, 1.0, "1", None):
            with self.subTest(cutoff=cutoff), self.assertRaises(ValueError):
                self.project("action", cutoff=cutoff)
        for channel in ("all", "CoT", "", None):
            with self.subTest(channel=channel), self.assertRaises(ValueError):
                self.project(channel)
        for messages in ([{"role": "checker", "content": "hidden"}],
                         [{"role": "tool", "tool_call_id": "unknown", "content": "result"}],
                         [{"role": "assistant", "tool_calls": "not a list"}]):
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                self.project("action", 1, messages=messages)
        with self.assertRaises(ValueError):
            self.project("context", initial_context=[{"role": "assistant", "content": "wrong"}])


if __name__ == "__main__":
    unittest.main()
