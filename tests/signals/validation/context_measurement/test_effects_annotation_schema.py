import copy
import unittest

from signals.reasoning.context_annotations.schema import SCHEMA_VERSION
from signals.reasoning.context_annotations.schema import TARGETS
from signals.reasoning.context_annotations.schema import compare_construct_extractions
from signals.reasoning.context_annotations.schema import summarize_construct_coding
from signals.reasoning.context_annotations.schema import validate_construct_coding


def fixture(text="🙂 yes; no", status="available"):
    item = {"annotation_id": "episode", "evidence_status": status,
            "provided_surface_coverage": "complete_recorded_surface" if status == "available" else "partial_or_unestablished",
            "segments": [{"segment_id": "s1", "kind": "reasoning", "message_index": 1, "content_index": 0,
                          "availability": "readable", "value": text}] if status != "unavailable" else []}
    coding = {"schema_version": SCHEMA_VERSION, "annotation_id": "episode", "mentions": [],
              "segment_checks": [{"segment_id": "s1", "status": "checked", "reason": ""}] if item["segments"] else [],
              "target_checks": {target: {"status": "complete", "reason": ""} for target in TARGETS}}
    if status == "unavailable":
        coding["target_checks"] = {target: {"status": "unavailable", "reason": "No readable source."} for target in TARGETS}
    return item, coding


def mention(text="🙂 yes; no", start=2, end=5, **changes):
    value = {"mention_id": "m1", "target": "evaluation_assessment", "referent": "current_setting",
             "stance": "affirm", "attributed_source": "actor_own", "modality": "asserted_actual",
             "action_status": "no_action_claim", "coder_status": "scorable", "ambiguity_reason": "",
             "anchor_span_index": 0,
             "spans": [{"segment_id": "s1", "start": start, "end": end, "quote": text[start:end]}]}
    value.update(changes)
    return value


class ConstructSchemaTests(unittest.TestCase):
    def test_exact_unicode_and_no_source_mutation(self):
        item, coding = fixture()
        coding["mentions"] = [mention()]
        original = copy.deepcopy((item, coding))
        self.assertEqual(validate_construct_coding(item, coding), coding)
        self.assertEqual((item, coding), original)
        for changes in ({"start": True}, {"end": 100}, {"quote": "yes!"}, {"segment_id": "missing"}, {"start": -1}, {"end": 2}):
            changed = copy.deepcopy(coding)
            changed["mentions"][0]["spans"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_construct_coding(item, changed)

    def test_exact_fields_targets_enums_and_population_identity(self):
        item, coding = fixture()
        coding["mentions"] = [mention()]
        mutations = [lambda x: x.update(extra=True), lambda x: x.update(annotation_id="wrong"),
                     lambda x: x["target_checks"].pop(TARGETS[-1]), lambda x: x.update(segment_checks=[]),
                     lambda x: x["mentions"][0].update(stance="perhaps"),
                     lambda x: x["mentions"][0].update(spans=[]), lambda x: x["mentions"][0].pop("action_status")]
        for mutate in mutations:
            changed = copy.deepcopy(coding)
            mutate(changed)
            with self.assertRaises(ValueError):
                validate_construct_coding(item, changed)

    def test_positive_survives_partial_capture_and_retraction(self):
        item, coding = fixture(status="partial")
        coding["mentions"] = [mention(), mention(start=7, end=9, mention_id="m2", stance="deny")]
        row = summarize_construct_coding(item, coding)["targets"][TARGETS[0]]
        self.assertEqual(row["accessible_occurrence"], 1)
        self.assertTrue(row["both_affirmation_and_denial"])
        self.assertIsNone(row["last_explicit_stance"]["value"])
        item["provided_surface_coverage"] = "complete_recorded_surface"
        self.assertEqual(summarize_construct_coding(item, coding)["targets"][TARGETS[0]]["last_explicit_stance"]["value"], "deny")

    def test_quotation_intention_and_denial_are_not_affirmations(self):
        item, coding = fixture()
        for changes in ({"attributed_source": "quotation_unendorsed"}, {"stance": "deny"},
                        {"modality": "actual_conditional_intention"}, {"referent": "described_scenario"},
                        {"stance": "uncertain"}):
            coding["mentions"] = [mention(**changes)]
            row = summarize_construct_coding(item, coding)["targets"][TARGETS[0]]
            self.assertEqual(row["accessible_occurrence"], 0)
            self.assertEqual(row["episode_wide_absence"], "not_established")

    def test_silence_unavailable_and_ambiguity_remain_distinct(self):
        item, coding = fixture()
        self.assertEqual(summarize_construct_coding(item, coding)["targets"][TARGETS[0]]["occurrence_status"], "accessible_surface_zero")
        coding["target_checks"][TARGETS[0]] = {"status": "ambiguous", "reason": "Unresolved reference."}
        self.assertIsNone(summarize_construct_coding(item, coding)["targets"][TARGETS[0]]["accessible_occurrence"])
        item, coding = fixture(status="unavailable")
        self.assertTrue(all(row["accessible_occurrence"] is None for row in summarize_construct_coding(item, coding)["targets"].values()))
        coding["target_checks"][TARGETS[0]] = {"status": "complete", "reason": ""}
        with self.assertRaises(ValueError):
            validate_construct_coding(item, coding)

    def test_ambiguous_segment_cannot_claim_complete_extraction(self):
        item, coding = fixture()
        coding["segment_checks"][0].update(status="ambiguous", reason="Ambiguous passage.")
        with self.assertRaises(ValueError):
            validate_construct_coding(item, coding)

    def test_duplicate_mentions_and_spans_rejected(self):
        item, coding = fixture()
        coding["mentions"] = [mention(), mention(mention_id="m2")]
        with self.assertRaises(ValueError):
            validate_construct_coding(item, coding)
        coding["mentions"] = [mention()]
        coding["mentions"][0]["spans"] *= 2
        with self.assertRaises(ValueError):
            validate_construct_coding(item, coding)

    def test_overlapping_conflicting_final_spans_are_unknown(self):
        item, coding = fixture()
        coding["mentions"] = [mention(), mention(start=3, end=5, mention_id="m2", stance="deny")]
        row = summarize_construct_coding(item, coding)["targets"][TARGETS[0]]
        self.assertEqual(row["accessible_occurrence"], 1)
        self.assertEqual(row["last_explicit_stance"]["status"], "overlapping_conflicting_final_spans")

    def test_independent_extraction_retains_missing_split_and_disagreement(self):
        item, first = fixture()
        first["mentions"] = [mention()]
        second = copy.deepcopy(first)
        second["mentions"][0].update(mention_id="second", stance="deny")
        compared = compare_construct_extractions(item, first, second)
        self.assertFalse(compared["exact_span_matches"][0]["axis_agreement"]["stance"])
        second["mentions"] = [mention(start=2, end=3, mention_id="split1"), mention(start=3, end=5, mention_id="split2")]
        compared = compare_construct_extractions(item, first, second)
        self.assertEqual(len(compared["potential_overlap_matches"]), 2)
        self.assertEqual(compared["unmatched_first"], ["m1"])
        self.assertEqual(compared["unmatched_second"], ["split1", "split2"])
        second["mentions"] = []
        self.assertEqual(compare_construct_extractions(item, first, second)["unmatched_first"], ["m1"])

    def test_contextual_support_does_not_move_assertion_past_retraction(self):
        text = "yes; no; later context"
        item, coding = fixture(text)
        affirmation = mention(text, 0, 3)
        affirmation["spans"].append({"segment_id": "s1", "start": 9, "end": len(text), "quote": text[9:]})
        coding["mentions"] = [affirmation, mention(text, 5, 7, mention_id="m2", stance="deny")]
        row = summarize_construct_coding(item, coding)["targets"][TARGETS[0]]
        self.assertEqual(row["accessible_occurrence"], 1)
        self.assertEqual(row["last_explicit_stance"]["value"], "deny")
        for index in (True, -1, 2, "0"):
            coding["mentions"][0]["anchor_span_index"] = index
            with self.assertRaises(ValueError):
                validate_construct_coding(item, coding)

    def test_unresolved_axes_cannot_establish_zero_or_last_stance(self):
        item, coding = fixture()
        for changes in ({"referent": "other_or_unresolved"}, {"attributed_source": "unresolved"}, {"modality": "unresolved"}):
            coding["mentions"] = [mention(**changes)]
            row = summarize_construct_coding(item, coding)["targets"][TARGETS[0]]
            self.assertIsNone(row["accessible_occurrence"])
            self.assertIsNone(row["last_explicit_stance"]["value"])

    def test_entirely_redacted_surface_cannot_be_coded_complete_or_zero(self):
        item, coding = fixture(status="partial")
        item["segments"][0].update(availability="redacted", value=None)
        coding["segment_checks"] = []
        with self.assertRaises(ValueError):
            validate_construct_coding(item, coding)
        coding["target_checks"] = {target: {"status": "unavailable", "reason": "No readable source."} for target in TARGETS}
        self.assertTrue(all(row["accessible_occurrence"] is None for row in summarize_construct_coding(item, coding)["targets"].values()))


if __name__ == "__main__":
    unittest.main()
