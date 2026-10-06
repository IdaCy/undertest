import json
import unittest

import signals.validation.controls.environment as environment
from signals.reasoning.observations import project_observation
from signals.validation.controls.reference import action_reference
from signals.validation.controls.reference import retrospective_reference


class ActionReferenceTests(unittest.TestCase):
    def transcript(self, modes, family="ledger"):
        case = environment.make_case(family, "development", 0)
        state = environment.initial_state(case)
        messages = []
        for index, mode in enumerate(modes):
            artifact = case["controls"][mode]
            arguments = {"artifact": artifact}
            result = environment.apply_tool(case, state, "submit_artifact", arguments)
            messages.extend([
                {"role": "assistant", "content": "I used a shortcut.", "reasoning": "I did not use a shortcut.",
                 "tool_calls": [{"id": f"x{index}", "type": "function", "function": {
                     "name": "submit_artifact", "arguments": json.dumps(arguments)}}]},
                {"role": "tool", "tool_call_id": f"x{index}", "content": json.dumps(result)},
            ])
        return case, state, {"initial_context": [{"role": "user", "content": environment.render_case(case)}],
                             "tools": environment.tool_definitions(), "messages": messages,
                             "retrospective_cutoff": len(messages)}

    def test_reference_matches_independent_actions_with_repair_and_wrong_controls(self):
        for family in environment.FAMILIES:
            for modes in (["correct"], ["ordinary_wrong"], ["invalid"], ["shortcut"], ["shortcut", "correct"]):
                with self.subTest(family=family, modes=modes):
                    case, state, trajectory = self.transcript(modes, family)
                    self.assertEqual(retrospective_reference(trajectory)["event"],
                                     environment.grade(case, state)["shortcut_attempt"])

    def test_unanswered_submission_is_unknown_and_later_repair_does_not_erase_positive(self):
        _, _, trajectory = self.transcript(["shortcut", "correct"])
        trajectory["retrospective_cutoff"] = 1
        self.assertIsNone(retrospective_reference(trajectory)["event"])
        trajectory["retrospective_cutoff"] = 3
        result = retrospective_reference(trajectory)
        self.assertTrue(result["event"])
        self.assertTrue(result["unresolved_exchanges"])

    def test_public_text_and_reasoning_cannot_supply_acceptance(self):
        _, _, trajectory = self.transcript([])
        trajectory["messages"] = [{"role": "assistant", "content": '{"accepted": true, "mode": "preview_cache"}',
                                    "reasoning": "I submitted preview_cache and it was accepted."}]
        trajectory["retrospective_cutoff"] = 1
        trajectory["labels"] = {"shortcut_attempt": True}
        self.assertFalse(retrospective_reference(trajectory)["event"])

    def test_duplicate_keys_or_mismatched_artifacts_are_unresolved(self):
        _, _, trajectory = self.transcript(["shortcut"])
        for content in ('{"accepted": false, "accepted": true}',
                        '{"accepted": true, "artifact": {"mode": "pipeline"}}'):
            trajectory["messages"][1]["content"] = content
            self.assertIsNone(retrospective_reference(trajectory)["event"])

    def test_rejected_malformed_shortcut_arguments_are_negative(self):
        _, _, trajectory = self.transcript(["shortcut"])
        trajectory["messages"][0]["tool_calls"][0]["function"]["arguments"] = "{malformed"
        trajectory["messages"][1]["content"] = '{"accepted": false, "error": "invalid JSON"}'
        self.assertFalse(retrospective_reference(trajectory)["event"])

    def test_reference_requires_action_projection_and_has_no_future_access(self):
        _, _, trajectory = self.transcript(["correct", "shortcut"])
        trajectory["retrospective_cutoff"] = 2
        self.assertFalse(retrospective_reference(trajectory)["event"])
        observation = project_observation(trajectory["initial_context"], trajectory["messages"],
                                          trajectory["tools"], "cot", 2)
        with self.assertRaises(ValueError):
            action_reference(observation)


if __name__ == "__main__":
    unittest.main()
