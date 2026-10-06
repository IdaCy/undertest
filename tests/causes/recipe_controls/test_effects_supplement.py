import copy
import hashlib
import itertools
import json
import unittest

from causes.recipe_controls.supplement import digest
from causes.recipe_controls.supplement import inventory_episode
from causes.recipe_controls.supplement import message_segments
from causes.recipe_controls.supplement import monotone_match


def message(text, identity=None):
    value = {"role": "assistant", "content": text, "tool_calls": None}
    if identity is not None:
        value["id"] = identity
    return value


def fixture(canonical=None, returned=None):
    canonical = canonical if canonical is not None else [message("first", "m0")]
    returned = returned if returned is not None else canonical + [message("extra", "m1")]
    events = [{"event": "model", "uuid": f"event-{i}",
               "output": {"choices": [{"message": value, "stop_reason": "stop"}]}}
              for i, value in enumerate(returned)]
    segments = []
    for i, value in enumerate(canonical):
        for segment in message_segments(value)[0]:
            segments.append({"segment_id": f"canonical-{len(segments)}", "json_pointer": f"/{i}" + segment["message_pointer"],
                             **{key: segment[key] for key in ("kind", "availability", "value")}})
    return {"raw_sample": {"id": "assignment", "uuid": "sample-uuid", "events": events},
            "canonical_messages": canonical,
            "canonical_item": {"annotation_id": "canonical-item", "segments": segments, "evidence_status": "partial"},
            "raw_log_reference": {"path": "/raw.eval", "bytes": 1, "sha256": "a" * 64},
            "sample_index": 0, "first_attempt_binding_verified": True, "source_order_verified": True}


class MonotoneMatchingTests(unittest.TestCase):
    def test_exhaustive_binary_sequences_against_combinations(self):
        sequences = [seq for n in range(5) for seq in itertools.product("ab", repeat=n)]
        right_sequences = [seq for n in range(6) for seq in itertools.product("ab", repeat=n)]
        for left in sequences:
            for right in right_sequences:
                oracle = [indices for indices in itertools.combinations(range(len(right)), len(left))
                          if tuple(right[index] for index in indices) == left]
                actual = monotone_match(list(left), list(right), order_verified=True)
                self.assertEqual(actual["mapping_count_capped_at_two"], min(2, len(oracle)), (left, right))
                self.assertEqual(actual["mapping"], list(oracle[0]) if len(oracle) == 1 else None)
                self.assertEqual(actual["feasible_candidates"], [sorted({indices[i] for indices in oracle}) for i in range(len(left))])

    def test_identical_repeated_outputs_are_ambiguous(self):
        a = message("repeat")
        result = monotone_match([a], [a, a], order_verified=True)
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["equal_candidates"], [[0, 1]])
        self.assertIsNone(result["mapping"])

    def test_repeated_outputs_with_distinct_ids_match_identity(self):
        a, b = message("repeat", "id-a"), message("repeat", "id-b")
        self.assertEqual(monotone_match([b], [a, b], order_verified=True)["mapping"], [1])

    def test_global_order_uniquely_matches_two_equal_occurrences(self):
        a = message("repeat")
        self.assertEqual(monotone_match([a, a], [a, a], order_verified=True)["mapping"], [0, 1])

    def test_no_greedy_early_selection(self):
        self.assertEqual(monotone_match(["a", "b"], ["a", "b", "b"], order_verified=True)["status"], "ambiguous")

    def test_reordered_and_whitespace_changed_messages_disagree(self):
        self.assertEqual(monotone_match(["b", "a"], ["a", "b"], order_verified=True)["status"], "disagreement")
        self.assertEqual(monotone_match([message("a ")], [message("a")], order_verified=True)["status"], "disagreement")

    def test_transport_formatting_only_preserves_message_identity(self):
        a = message("é\n")
        b = json.loads(json.dumps(a, indent=4, ensure_ascii=True))
        b = dict(reversed(list(b.items())))
        self.assertEqual(monotone_match([a], [b], order_verified=True)["mapping"], [0])
        self.assertNotEqual(hashlib.sha256(json.dumps(a).encode()).hexdigest(), hashlib.sha256(json.dumps(b, indent=4).encode()).hexdigest())

    def test_unknown_order_never_selects(self):
        result = monotone_match(["a"], ["a"], order_verified=False)
        self.assertEqual(result["status"], "unknown_order")
        self.assertIsNone(result["mapping"])

    def test_tool_and_metadata_difference_is_not_text_equality(self):
        a, b = message("a"), message("a")
        b["tool_calls"] = [{"id": "a", "function": "submit", "arguments": {}}]
        self.assertEqual(monotone_match([a], [b], order_verified=True)["status"], "disagreement")
        b = dict(a, metadata={"x": 1})
        self.assertEqual(monotone_match([a], [b], order_verified=True)["status"], "disagreement")


class InventoryTests(unittest.TestCase):
    def test_unique_extra_source_and_canonical_pointer_binding(self):
        data = fixture()
        original = copy.deepcopy(data)
        result = inventory_episode(**data)
        self.assertEqual(data, original)
        self.assertEqual(result["selection_issues"], [])
        self.assertEqual(result["events"][0]["choices"][0]["canonical_segment_bindings"][0]["canonical_segment_id"], "canonical-0")
        self.assertEqual([s["value"] for s in result["projection"]["segments"]], ["extra"])
        self.assertEqual(result["projection"]["segments"][0]["inspect_view_pointer"], "/samples/0/events/1/output/choices/0/message/content")

    def test_ambiguous_duplicate_outputs_retained_without_supplement(self):
        a = message("repeat")
        result = inventory_episode(**fixture([a], [a, a]))
        self.assertEqual(len(result["events"]), 2)
        self.assertIn("canonical_event_mapping_ambiguous", result["selection_issues"])
        self.assertEqual(result["projection"]["segments"], [])
        self.assertNotEqual(result["events"][0]["choices"][0]["segments"][0]["segment_id"], result["events"][1]["choices"][0]["segments"][0]["segment_id"])

    def test_deterministic_identity_changes_with_raw_bytes_and_occurrence(self):
        data = fixture()
        first = inventory_episode(**data)["projection"]["segments"][0]["segment_id"]
        self.assertEqual(first, inventory_episode(**data)["projection"]["segments"][0]["segment_id"])
        data["raw_log_reference"]["path"] = "/copy.eval"
        self.assertEqual(first, inventory_episode(**data)["projection"]["segments"][0]["segment_id"])
        data["raw_log_reference"]["sha256"] = "b" * 64
        self.assertNotEqual(first, inventory_episode(**data)["projection"]["segments"][0]["segment_id"])

    def test_unknown_primary_or_order_preserves_inventory(self):
        for flag in ("first_attempt_binding_verified", "source_order_verified"):
            data = fixture()
            data[flag] = False
            result = inventory_episode(**data)
            self.assertEqual(len(result["events"]), 2)
            self.assertEqual(result["projection"]["selection_status"], "unknown")
            self.assertEqual(result["projection"]["segments"], [])

    def test_missing_canonical_assistant_blocks_the_empty_injection(self):
        for canonical in ([], [{"role": "user", "content": "prompt"}]):
            for state in ("available", "partial", "unavailable"):
                with self.subTest(canonical=canonical, evidence_status=state):
                    data = fixture(canonical=canonical, returned=[message("returned", "m1")])
                    data["canonical_item"]["evidence_status"] = state
                    result = inventory_episode(**data)
                    self.assertEqual(result["matching"]["status"], "unique")
                    self.assertEqual(result["matching"]["mapping"], [])
                    self.assertIn("missing_canonical_assistant", result["selection_issues"])
                    self.assertEqual(result["projection"]["selection_status"], "unknown")
                    self.assertEqual(result["projection"]["segments"], [])
                    self.assertEqual(result["events"][0]["output"], data["raw_sample"]["events"][0]["output"])
                    self.assertEqual(result["events"][0]["choices"][0]["segments"][0]["value"], "returned")

    def test_unavailable_or_unknown_canonical_status_blocks_with_assistants(self):
        for status in ("unavailable", "unknown", None, True, 0):
            with self.subTest(status=status):
                data = fixture()
                data["canonical_item"]["evidence_status"] = status
                result = inventory_episode(**data)
                self.assertIn("unavailable_or_unknown_canonical_evidence", result["selection_issues"])
                self.assertEqual(result["matching"]["mapping"], [0])
                self.assertEqual(result["projection"]["selection_status"], "unknown")
                self.assertEqual(result["projection"]["segments"], [])
                self.assertEqual(len(result["events"]), 2)
                self.assertEqual(result["projection"]["canonical_evidence_status"], status)
        data = fixture()
        del data["canonical_item"]["evidence_status"]
        self.assertIn("unavailable_or_unknown_canonical_evidence", inventory_episode(**data)["selection_issues"])

    def test_available_and_partial_canonical_statuses_preserve_admission(self):
        for status in ("available", "partial"):
            data = fixture()
            data["canonical_item"]["evidence_status"] = status
            result = inventory_episode(**data)
            self.assertEqual(result["selection_issues"], [])
            self.assertEqual([segment["value"] for segment in result["projection"]["segments"]], ["extra"])

    def test_malformed_event_discriminators_block_without_reindexing(self):
        for bad_event in ({}, {"event": None}, {"event": 5}, {"event": True}, {"event": ""}, {"event": "  "}):
            with self.subTest(bad_event=bad_event):
                data = fixture()
                data["raw_sample"]["events"].insert(1, bad_event)
                original = copy.deepcopy(data)
                result = inventory_episode(**data)
                self.assertEqual(data, original)
                self.assertIn("malformed_event_discriminator", result["selection_issues"])
                self.assertEqual(result["projection"]["selection_status"], "unknown")
                self.assertEqual(result["projection"]["segments"], [])
                self.assertEqual([event["event_index"] for event in result["events"]], [0, 2])
                self.assertEqual([event["model_event_ordinal"] for event in result["events"]], [0, 1])
                self.assertEqual([event["output"] for event in result["events"]],
                                 [data["raw_sample"]["events"][index]["output"] for index in (0, 2)])

    def test_well_formed_nonmodel_event_preserves_admission(self):
        data = fixture()
        data["raw_sample"]["events"].insert(1, {"event": "tool", "uuid": "tool-1"})
        result = inventory_episode(**data)
        self.assertEqual(result["selection_issues"], [])
        self.assertEqual([segment["value"] for segment in result["projection"]["segments"]], ["extra"])
        self.assertEqual(result["events"][1]["event_index"], 2)

    def test_duplicate_and_missing_event_uuid_block_selection(self):
        for uuid in ("event-0", None):
            data = fixture()
            data["raw_sample"]["events"][1]["uuid"] = uuid
            result = inventory_episode(**data)
            self.assertEqual(result["projection"]["segments"], [])
            self.assertTrue(result["selection_issues"])

    def test_multiple_choices_keep_every_choice_and_block_selection(self):
        data = fixture()
        data["raw_sample"]["events"][1]["output"]["choices"].append({"message": message("other")})
        result = inventory_episode(**data)
        self.assertEqual(len(result["events"][1]["choices"]), 2)
        self.assertEqual(result["projection"]["segments"], [])

    def test_no_choice_malformed_choice_and_model_error_block_selection(self):
        for output in (None, {"choices": []}, {"choices": [None]}, {"choices": [{"message": message("extra")}], "error": {"message": "failed"}}):
            data = fixture()
            data["raw_sample"]["events"][1]["output"] = output
            result = inventory_episode(**data)
            self.assertEqual(result["events"][1]["output"], output)
            self.assertEqual(result["projection"]["segments"], [])

    def test_recorded_event_error_and_pending_event_are_unknown(self):
        for key, value in (("error", "failure"), ("pending", True)):
            data = fixture()
            data["raw_sample"]["events"][1][key] = value
            self.assertEqual(inventory_episode(**data)["projection"]["selection_status"], "unknown")

    def test_canonical_segment_conflict_or_missing_mapping_blocks(self):
        data = fixture()
        data["canonical_item"]["segments"][0]["value"] = "different"
        self.assertIn("canonical_segment_binding_disagreement", inventory_episode(**data)["selection_issues"])
        data = fixture()
        data["canonical_item"]["segments"].append(dict(data["canonical_item"]["segments"][0], segment_id="extra", json_pointer="/100/content"))
        self.assertIn("incomplete_canonical_segment_binding", inventory_episode(**data)["selection_issues"])

    def test_sample_limits_output_caps_and_tool_intentions_are_retained(self):
        data = fixture()
        data["raw_sample"]["limit"] = {"type": "token", "limit": 10}
        data["raw_sample"]["events"][1]["output"]["choices"][0]["stop_reason"] = "max_tokens"
        data["raw_sample"]["events"][1]["output"]["choices"][0]["message"]["tool_calls"] = [{"function": "submit_recipe"}]
        result = inventory_episode(**data)
        self.assertEqual(result["projection"]["canonical_evidence_status"], "partial")
        self.assertEqual(result["preserved_sample_limits_and_errors"]["limit"], {"type": "token", "limit": 10})
        self.assertEqual(result["events"][1]["output"]["choices"][0]["stop_reason"], "max_tokens")
        self.assertEqual(result["projection"]["report_completeness"], "not_established")
        self.assertNotIn("invocations", result)

    def test_all_source_ordinals_include_nonmodel_events(self):
        data = fixture()
        data["raw_sample"]["events"].insert(1, {"event": "tool", "uuid": "tool-1"})
        result = inventory_episode(**data)
        self.assertEqual(result["events"][1]["event_index"], 2)
        self.assertEqual(result["events"][1]["model_event_ordinal"], 1)

    def test_unicode_empty_redacted_summary_and_unknown_content(self):
        value = {"role": "assistant", "content": [{"type": "text", "text": "é🙂"}, {"type": "text", "text": ""},
                 {"type": "reasoning", "reasoning": "secret", "redacted": True, "summary": "summary"},
                 {"type": "text", "text": "attachment://unresolved"}, {"type": "reasoning"}, {"type": "image", "image": "x"}]}
        segments, unsupported = message_segments(value)
        self.assertEqual([s["availability"] for s in segments], ["readable", "empty", "redacted", "readable", "unresolved_attachment", "unavailable_field"])
        self.assertEqual(segments[0]["characters"], 2)
        self.assertEqual(segments[0]["utf8_bytes"], 6)
        self.assertEqual(segments[0]["utf8_sha256"], hashlib.sha256("é🙂".encode()).hexdigest())
        self.assertIsNone(segments[2]["value"])
        self.assertEqual(len(unsupported), 1)
        result = inventory_episode(**fixture(returned=[message("first", "m0"), value]))
        self.assertEqual(result["projection"]["channels"]["reasoning"]["full_channel_capture"], "not_established")

    def test_boolean_sample_index_is_rejected(self):
        data = fixture()
        data["sample_index"] = True
        with self.assertRaises(ValueError):
            inventory_episode(**data)

    def test_transport_digest_rejects_nan(self):
        with self.assertRaises(ValueError):
            digest(float("nan"))


if __name__ == "__main__":
    unittest.main()
