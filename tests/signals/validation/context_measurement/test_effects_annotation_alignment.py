import copy
import unittest

from signals.reasoning.context_annotations.alignment import ADJUDICATION_VERSION
from signals.reasoning.context_annotations.alignment import align_construct_codings
from signals.reasoning.context_annotations.alignment import validate_adjudication_accounting
from signals.reasoning.context_annotations.schema import SCHEMA_VERSION
from signals.reasoning.context_annotations.schema import TARGETS


TEXT = "🙂 We are being evaluated. We are being evaluated. This is hypothetical."
SEGMENT = "b" * 64


def item():
    states = ("readable", "empty", "redacted", "unavailable_field", "unresolved_attachment")
    channels = {kind: {"returned_segment_count": int(kind == "text"),
                       "availability_counts": {state: int(kind == "text" and state == "readable") for state in states},
                       "status": "returned_segments" if kind == "text" else "not_returned_or_not_recorded",
                       "full_channel_capture": "not_established"}
                for kind in ("text", "reasoning", "reasoning_summary")}
    channels["reasoning"]["index_availability"] = "not_returned_or_not_recorded"
    return {"annotation_id": "a" * 64, "evidence_status": "available",
            "segments": [{"segment_id": SEGMENT, "kind": "text", "message_index": 2,
                          "content_index": None, "field": "content", "json_pointer": "/2/content",
                          "availability": "readable", "value": TEXT}],
            "channels": channels, "provided_surface_coverage": "complete_recorded_surface",
            "episode_wide_absence": "not_established"}


def span(start, end):
    return {"segment_id": SEGMENT, "start": start, "end": end, "quote": TEXT[start:end]}


def mention(identity, spans, **changes):
    result = {"mention_id": identity, "target": "evaluation_assessment", "referent": "current_setting",
              "stance": "affirm", "attributed_source": "actor_own", "modality": "asserted_actual",
              "action_status": "no_action_claim", "coder_status": "scorable", "ambiguity_reason": "",
              "spans": spans, "anchor_span_index": 0}
    result.update(changes)
    return result


def coding(*mentions):
    return {"schema_version": SCHEMA_VERSION, "annotation_id": "a" * 64,
            "segment_checks": [{"segment_id": SEGMENT, "status": "checked", "reason": ""}],
            "target_checks": {target: {"status": "complete", "reason": ""} for target in TARGETS},
            "mentions": list(mentions)}


def decision(identity, first, second, relation):
    return {"decision_id": identity, "first": first, "second": second, "relation": relation,
            "reason": "A supplied conditional decision about these exact source clauses.", "spans": [span(2, 25)]}


def adjudication(first, second, decisions):
    return {"schema_version": ADJUDICATION_VERSION, "annotation_id": "a" * 64,
            "alignment_sha256": align_construct_codings(item(), first, second)["alignment_sha256"],
            "decisions": decisions}


class AlignmentTests(unittest.TestCase):
    def test_exact_spans_keep_axes_anchor_and_context_order_separate(self):
        first = coding(mention("left", [span(2, 25), span(50, 70)]))
        second = coding(mention("right", [span(50, 70), span(2, 25)], stance="deny"))
        before = copy.deepcopy((item(), first, second))
        result = align_construct_codings(item(), first, second)
        edge = result["unique_exact_evidence_candidates"][0]
        self.assertFalse(edge["axis_agreement"]["stance"])
        self.assertFalse(edge["anchor_agreement"])
        self.assertFalse(edge["evidence_order_agreement"])
        self.assertEqual((item(), first, second), before)

    def test_duplicate_evidence_with_conflicting_axes_is_not_greedily_matched(self):
        first = coding(mention("a", [span(2, 25)]), mention("b", [span(2, 25)], stance="deny"))
        second = coding(mention("a", [span(2, 25)]))
        result = align_construct_codings(item(), first, second)
        self.assertEqual(result["unique_exact_evidence_candidates"], [])
        self.assertEqual(len(result["ambiguous_exact_evidence_keys"]), 1)
        self.assertEqual(result["groups"][0]["first"], ["a", "b"])
        self.assertEqual(result["groups"][0]["second"], ["a"])
        self.assertEqual(len(result["groups"][0]["edges"]), 2)

    def test_split_merge_and_context_bridges_remain_candidate_groups(self):
        first = coding(mention("whole", [span(2, 49)]))
        second = coding(mention("part1", [span(2, 25)]), mention("part2", [span(26, 49)]))
        for left, right in ((first, second), (second, first)):
            result = align_construct_codings(item(), left, right)
            self.assertEqual(len(result["groups"]), 1)
            self.assertEqual(result["groups"][0]["shape"], "many_to_many_or_split_merge_candidate")
            self.assertEqual(result["unique_exact_evidence_candidates"], [])
        first = coding(mention("first-clause", [span(2, 25), span(50, 70)]))
        second = coding(mention("second-clause", [span(26, 49), span(50, 70)]))
        result = align_construct_codings(item(), first, second)
        self.assertEqual(len(result["groups"]), 1)
        self.assertFalse(result["groups"][0]["edges"][0]["anchor_agreement"])

    def test_repeated_quotes_disjoint_and_touching_intervals_stay_distinct(self):
        self.assertEqual(TEXT[2:25], TEXT[26:49])
        first = coding(mention("same-id", [span(2, 25)]))
        for start, end in ((26, 49), (25, 26)):
            result = align_construct_codings(item(), first, coding(mention("same-id", [span(start, end)])))
            self.assertEqual(len(result["groups"]), 2)
            self.assertEqual({g["shape"] for g in result["groups"]}, {"first_only", "second_only"})

    def test_cross_target_overlaps_are_visible_without_automatic_alignment(self):
        first = coding(mention("left", [span(2, 25)]))
        second = coding(mention("right", [span(2, 25)], target="observation_coverage"))
        result = align_construct_codings(item(), first, second)
        self.assertEqual(len(result["groups"]), 2)
        self.assertEqual(len(result["cross_target_overlaps"]), 1)
        self.assertEqual(result["unique_exact_evidence_candidates"], [])

    def test_row_permutation_preserves_inventory_but_exact_input_binding_changes(self):
        first = coding(mention("left1", [span(2, 25)]), mention("left2", [span(26, 49)]))
        second = coding(mention("right", [span(2, 25)]))
        original = align_construct_codings(item(), first, second)
        first["mentions"].reverse()
        permuted = align_construct_codings(item(), first, second)
        for key in ("groups", "unique_exact_evidence_candidates", "ambiguous_exact_evidence_keys", "cross_target_overlaps"):
            self.assertEqual(original[key], permuted[key])
        self.assertNotEqual(original["alignment_sha256"], permuted["alignment_sha256"])

    def test_structurally_failed_coding_is_never_an_empty_success(self):
        first = coding(mention("left", [span(2, 25)]))
        for mutate in (lambda c: c["mentions"][0]["spans"][0].update(start=True),
                       lambda c: c["mentions"][0]["spans"][0].update(quote="wrong"),
                       lambda c: c.pop("target_checks")):
            bad = copy.deepcopy(first)
            mutate(bad)
            with self.assertRaises(ValueError):
                align_construct_codings(item(), bad, coding())

    def test_all_conditional_relation_shapes_account_for_each_original(self):
        shapes = {"same_mention": (1, 1), "split_merge": (1, 2), "distinct_mentions": (1, 1),
                  "first_only": (2, 0), "second_only": (0, 2), "unresolved": (1, 0)}
        for relation, (left, right) in shapes.items():
            with self.subTest(relation=relation):
                first = coding(*(mention(str(i), [span(2, 25)], stance=("affirm", "deny")[i]) for i in range(left)))
                second = coding(*(mention(str(i), [span(2, 25)], stance=("affirm", "deny")[i]) for i in range(right)))
                d = decision("d", [str(i) for i in range(left)], [str(i) for i in range(right)], relation)
                output = validate_adjudication_accounting(item(), first, second, adjudication(first, second, [d]))
                self.assertEqual(output["consumed_mention_counts"], {"first": left, "second": right})
                self.assertEqual(output["relation_decision_counts"][relation], 1)
                self.assertFalse(output["semantic_validation_established"])
                self.assertFalse(output["original_codings_replaced"])

    def test_missing_duplicate_unknown_and_stale_decisions_refuse(self):
        first = coding(mention("x", [span(2, 25)]))
        second = coding(mention("x", [span(2, 25)]))
        good = adjudication(first, second, [decision("d", ["x"], ["x"], "same_mention")])
        mutations = [lambda a: a.update(decisions=[]), lambda a: a.update(alignment_sha256="wrong"),
                     lambda a: a["decisions"][0].update(first=["unknown"]),
                     lambda a: a["decisions"][0].update(first=["x", "x"]),
                     lambda a: a["decisions"].append(decision("d2", ["x"], ["x"], "same_mention")),
                     lambda a: a["decisions"][0].update(relation="split_merge"),
                     lambda a: a["decisions"][0].update(reason=" "),
                     lambda a: a["decisions"][0].update(spans=[]),
                     lambda a: a["decisions"][0]["spans"][0].update(start=True),
                     lambda a: a["decisions"][0]["spans"][0].update(quote="invented"),
                     lambda a: a["decisions"][0]["spans"][0].update(segment_id="private-source")]
        for index, mutate in enumerate(mutations):
            bad = copy.deepcopy(good)
            mutate(bad)
            with self.subTest(index=index), self.assertRaises(ValueError):
                validate_adjudication_accounting(item(), first, second, bad)

    def test_empty_inventories_preserve_zero_inputs_without_claiming_no_omission(self):
        first, second = coding(), coding()
        alignment = align_construct_codings(item(), first, second)
        output = validate_adjudication_accounting(item(), first, second, adjudication(first, second, []))
        self.assertEqual(alignment["groups"], [])
        self.assertEqual(output["decision_count"], 0)
        self.assertFalse(output["semantic_validation_established"])
        self.assertIn("shared_omissions_not_detectable_from_two_inventories", alignment["limitations"])


if __name__ == "__main__":
    unittest.main()
