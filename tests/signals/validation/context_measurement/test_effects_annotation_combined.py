import copy
import hashlib
import json
import unittest

from signals.reasoning.context_annotations.alignment import align_construct_codings
from signals.reasoning.context_annotations.combined import CANONICAL
from signals.reasoning.context_annotations.combined import COORDINATE_NAMESPACE
from signals.reasoning.context_annotations.combined import PAYLOAD_VERSION
from signals.reasoning.context_annotations.combined import SUPPLEMENTARY
from signals.reasoning.context_annotations.combined import combined_construct_payload
from signals.reasoning.context_annotations.combined import render_combined_construct_request
from signals.reasoning.context_annotations.schema import SCHEMA_VERSION
from signals.reasoning.context_annotations.schema import TARGETS
from signals.reasoning.context_annotations.schema import summarize_construct_coding
from signals.reasoning.context_annotations.schema import validate_construct_coding


def fixture():
    segments = [
        {"segment_id": "b" * 64, "kind": "text", "message_index": 2, "content_index": 0,
         "field": "text", "json_pointer": "/2/content/0/text", "availability": "readable", "value": "é yes",
         "source_namespace": CANONICAL, "surface_block": [0, 0, 0]},
        {"segment_id": "c" * 64, "kind": "reasoning", "content_index": 0, "field": "reasoning",
         "json_pointer": "/choices/0/message/content/0/reasoning", "availability": "readable", "value": "🙂 no",
         "source_namespace": SUPPLEMENTARY, "event_index": 15, "model_event_index": 1, "choice_index": 0,
         "surface_block": [1, 0, 0]},
        {"segment_id": "d" * 64, "kind": "reasoning_summary", "content_index": 0, "field": "summary",
         "json_pointer": "/choices/0/message/content/0/summary", "availability": "readable", "value": "yes",
         "source_namespace": SUPPLEMENTARY, "event_index": 15, "model_event_index": 1, "choice_index": 0,
         "surface_block": [1, 0, 0]},
    ]
    channels = {kind: {"returned_segment_count": 1,
                      "availability_counts": {"readable": 1, "empty": 0, "redacted": 0, "unavailable_field": 0, "unresolved_attachment": 0},
                      "status": "returned_segments", "full_channel_capture": "not_established"}
                for kind in ("text", "reasoning", "reasoning_summary")}
    channels["reasoning"]["index_availability"] = "readable_returned_blocks"
    item = {"payload_version": PAYLOAD_VERSION, "coordinate_namespace": COORDINATE_NAMESPACE,
            "annotation_id": "a" * 64, "evidence_status": "available", "segments": segments, "channels": channels,
            "provided_surface_coverage": "complete_recorded_surface", "episode_wide_absence": "not_established"}
    coding = {"schema_version": SCHEMA_VERSION, "annotation_id": "a" * 64,
              "segment_checks": [{"segment_id": s["segment_id"], "status": "checked", "reason": ""} for s in segments],
              "target_checks": {target: {"status": "complete", "reason": ""} for target in TARGETS}, "mentions": []}
    return item, coding


def mention(identity, segment, stance):
    return {"mention_id": identity, "target": TARGETS[0], "referent": "current_setting", "stance": stance,
            "attributed_source": "actor_own", "modality": "asserted_actual", "action_status": "no_action_claim",
            "coder_status": "scorable", "ambiguity_reason": "", "anchor_span_index": 0,
            "spans": [{"segment_id": segment["segment_id"], "start": 0, "end": len(segment["value"]), "quote": segment["value"]}]}


class CombinedSurfaceTests(unittest.TestCase):
    def test_native_coordinates_unicode_and_owner_field_concealment(self):
        item, _ = fixture()
        expected = copy.deepcopy(item)
        item["private_path"] = "OWNER_ONLY_PATH_48339"
        item["segments"][1]["inspect_view_pointer"] = "OWNER_ONLY_PATH_48339"
        item["channels"]["text"]["owner_note"] = "OWNER_ONLY_PATH_48339"
        request = render_combined_construct_request(item, "Pinned instructions.\n")
        result = json.loads(request["messages"][1]["content"])["construct_item"]
        self.assertEqual(result, expected)
        self.assertNotIn("OWNER_ONLY_PATH_48339", json.dumps(request))
        self.assertNotIn("message_index", result["segments"][1])
        self.assertEqual(request["actual_model_calls"], 0)

    def test_last_stance_uses_supplied_block_order_and_preserves_same_block_ambiguity(self):
        item, coding = fixture()
        coding["mentions"] = [mention("first", item["segments"][0], "affirm"), mention("later", item["segments"][1], "deny")]
        result = summarize_construct_coding(item, coding)["targets"][TARGETS[0]]
        self.assertEqual(result["last_explicit_stance"]["value"], "deny")
        coding["mentions"].append(mention("same_block_other_channel", item["segments"][2], "affirm"))
        result = summarize_construct_coding(item, coding)["targets"][TARGETS[0]]
        self.assertEqual(result["last_explicit_stance"]["status"], "cross_channel_final_order_unestablished")

    def test_partial_record_and_old_coding_are_not_promoted(self):
        item, coding = fixture()
        item.update(evidence_status="partial", provided_surface_coverage="partial_or_unestablished")
        coding["mentions"] = [mention("seen", item["segments"][1], "affirm")]
        result = summarize_construct_coding(item, coding)["targets"][TARGETS[0]]
        self.assertEqual(result["accessible_occurrence"], 1)
        self.assertEqual(result["last_explicit_stance"]["status"], "incomplete_recorded_surface_or_coding")
        self.assertEqual(result["episode_wide_absence"], "not_established")
        coding["segment_checks"] = coding["segment_checks"][:1]
        with self.assertRaises(ValueError):
            validate_construct_coding(item, coding)

    def test_alignment_binds_complete_new_payload(self):
        item, coding = fixture()
        coding["mentions"] = [mention("m", item["segments"][1], "affirm")]
        result = align_construct_codings(item, coding, coding)
        expected = hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(result["input_sha256"]["payload"], expected)
        self.assertEqual(result["input_mention_counts"], {"first": 1, "second": 1})

    def test_choices_share_one_event_model_ordinal_binding(self):
        item, _ = fixture()
        item["segments"][2].update(choice_index=1, surface_block=[1, 1, 0],
                                   json_pointer="/choices/1/message/content/0/summary")
        self.assertEqual(combined_construct_payload(item), item)
        for event, model in ((15, 2), (16, 1)):
            bad = copy.deepcopy(item)
            bad["segments"][2].update(event_index=event, model_event_index=model,
                                       surface_block=[model, 1, 0])
            with self.subTest(event=event, model=model), self.assertRaises(ValueError):
                combined_construct_payload(bad)

    def test_malformed_namespace_position_inventory_and_pointer_refuse(self):
        mutations = [
            lambda x: x.update(coordinate_namespace="unknown"),
            lambda x: x["segments"][1].update(source_namespace="unknown"),
            lambda x: x["segments"][1].update(surface_block=[1, 0, True]),
            lambda x: x["segments"][1].update(event_index=True),
            lambda x: x["segments"][1].update(model_event_index=2),
            lambda x: x["segments"][1].update(json_pointer="/samples/0/events/15/output/choices/0/message/content/0/reasoning"),
            lambda x: x["segments"][2].update(content_index=1, json_pointer="/choices/0/message/content/1/summary"),
            lambda x: x["segments"][2].update(event_index=16),
            lambda x: x["segments"].reverse(),
            lambda x: x["segments"].append(copy.deepcopy(x["segments"][0])),
            lambda x: x["channels"]["text"].update(returned_segment_count=True),
            lambda x: x["channels"]["text"]["availability_counts"].update(readable=True),
            lambda x: x.update(episode_wide_absence="established"),
        ]
        for mutate in mutations:
            item, _ = fixture()
            mutate(item)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                combined_construct_payload(item)


if __name__ == "__main__":
    unittest.main()
