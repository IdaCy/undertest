import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import signals.reasoning.context_annotations.base as annotation
from causes.recipe_controls.evidence import index_evidence


class AnnotationProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def assignment(self, identity="assignment-secret"):
        return {"assignment_id": identity, "instance_id": "instance-secret", "family": "ledger",
                "framing": "framing-secret", "grading": "grading-secret", "rollout": 0,
                "prompt_variant": "prompt-secret", "status": "completed", "primary_attempt_id": None,
                "primary_order_verified": True, "metadata_integrity": True, "metadata_complete": True,
                "metadata_conflicts": [], "metadata_absent": [], "outcomes": {"outcome-secret": 1}, "attempts": []}

    def messages(self, text=" exact α text\n"):
        return [{"role": "system", "content": "system-prompt-secret"},
                {"role": "user", "content": "user-prompt-secret"},
                {"role": "tool", "content": "tool-outcome-secret"},
                {"role": "assistant", "content": text}]

    def attempt(self, row, identity="attempt-secret", *, messages=None, primary=True):
        directory = self.root / "artifacts" / identity
        (directory / "effects").mkdir(parents=True)
        metadata = {key: row[key] for key in ("assignment_id", "instance_id", "family", "framing", "grading", "rollout", "prompt_variant")}
        for path, value in {
            "messages.json": self.messages() if messages is None else messages,
            "metadata.json": {"sample": metadata},
            "effects/identity.json": {"assignment_id": row["assignment_id"], "attempt_id": identity},
        }.items():
            (directory / path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        attempt = {"attempt_id": identity, "started": True, "trace_complete": True,
                   "termination": "natural", "model_cap": False, "sample_limit": False, "errors": [],
                   "last_stop_reason": "stop", "finish_reason": None, "limits": [],
                   "metadata_integrity": True, "metadata_complete": True, "metadata_conflicts": [], "metadata_absent": [],
                   "ledger_integrity": True, "ledger_available": True, "candidates": [],
                   "outcomes": {"attempt-outcome-secret": 1},
                   "samples": [{"sample_key": identity, "artifacts": str(directory), "sample_id": row["assignment_id"]}]}
        row["attempts"].append(attempt)
        if primary:
            row["primary_attempt_id"] = identity
        return attempt, directory

    def duplicate(self, attempt, directory, name="a-copy"):
        target = directory.parent / name
        shutil.copytree(directory, target)
        attempt["samples"].append({"sample_key": name, "artifacts": str(target)})
        return target

    def project(self, rows, assigned=None):
        index = index_evidence(self.root, rows)
        schedule = {"schedule_sha256": "a" * 64, "assignment_ids": assigned or [row["assignment_id"] for row in rows]}
        before = copy.deepcopy(index)
        result = annotation.project_annotation_episodes(index, schedule)
        self.assertEqual(index, before)
        return result, index, schedule

    def test_every_scheduled_episode_survives_missing_unstarted_and_unknown_primary(self):
        complete, unstarted, unknown = [self.assignment(value) for value in ("complete", "unstarted", "unknown")]
        self.attempt(complete)
        unstarted["status"] = "assigned_unstarted"
        self.attempt(unknown, "unknown-attempt", messages=self.messages("do not select this"))
        unknown["primary_order_verified"] = False
        assigned = ["unknown", "missing-index-row", "complete", "unstarted"]
        result, _, _ = self.project([complete, unstarted, unknown], assigned)
        items = result["construct_view"]["items"]
        audits = result["owner_audit"]["episodes"]
        self.assertEqual(result["construct_view"]["assigned_count"], 4)
        self.assertEqual([row["assignment_id"] for row in audits], assigned)
        self.assertEqual(len({row["annotation_id"] for row in items}), 4)
        self.assertEqual([row["evidence_status"] for row in items], ["unavailable", "unavailable", "available", "unavailable"])
        self.assertIsNone(audits[1]["original_index_row"])
        self.assertEqual(audits[1]["selection_issues"], ["missing_index_record"])
        self.assertEqual(items[0]["segments"], [])

    def test_identical_compatible_exports_deduplicate_by_lexical_path(self):
        row = self.assignment()
        attempt, directory = self.attempt(row, "z-first")
        duplicate = self.duplicate(attempt, directory)
        result, index, schedule = self.project([row])
        item, = result["construct_view"]["items"]
        audit, = result["owner_audit"]["episodes"]
        self.assertEqual(len(item["segments"]), 1)
        self.assertEqual(audit["canonical_source"]["path"], str(duplicate / "messages.json"))
        self.assertTrue(audit["native_linkage_eligible"])
        self.assertEqual([value["disposition"] for value in audit["snapshot_dispositions"]], ["identical_compatible_duplicate", "canonical_representative"])
        self.assertEqual(len(audit["original_index_row"]["primary_evidence"]["snapshots"]), 2)
        index["assignments"][0]["primary_evidence"]["snapshots"].reverse()
        reordered = annotation.project_annotation_episodes(index, schedule)
        self.assertEqual(reordered["construct_view"], result["construct_view"])
        self.assertEqual(reordered["owner_audit"]["episodes"][0]["canonical_source"], audit["canonical_source"])

    def test_identical_text_in_distinct_episodes_retains_distinct_weights_and_ids(self):
        one, two = self.assignment("one"), self.assignment("two")
        self.attempt(one, "first-one")
        self.attempt(two, "first-two")
        result, _, _ = self.project([one, two])
        items = result["construct_view"]["items"]
        audits = result["owner_audit"]["episodes"]
        self.assertEqual(len(items), 2)
        self.assertEqual(audits[0]["canonical_source"]["sha256"], audits[1]["canonical_source"]["sha256"])
        self.assertNotEqual(items[0]["annotation_id"], items[1]["annotation_id"])
        self.assertNotEqual(items[0]["segments"][0]["segment_id"], items[1]["segments"][0]["segment_id"])
        self.assertEqual(items[0]["segments"][0]["value"], items[1]["segments"][0]["value"])

    def test_formatting_only_transcript_changes_reject_canonical_selection(self):
        row = self.assignment()
        attempt, directory = self.attempt(row)
        duplicate = self.duplicate(attempt, directory)
        (duplicate / "messages.json").write_text(json.dumps(self.messages(), indent=2, ensure_ascii=False))
        result, _, _ = self.project([row])
        item, = result["construct_view"]["items"]
        audit, = result["owner_audit"]["episodes"]
        self.assertEqual(item["segments"], [])
        self.assertFalse(audit["native_linkage_eligible"])
        self.assertIsNone(audit["canonical_source"])
        self.assertEqual(len(audit["readable_transcript_sha256s"]), 2)
        self.assertIn("differing_readable_transcript_bytes", audit["selection_issues"])
        self.assertEqual(len(audit["snapshot_dispositions"]), 2)

    def test_identical_bytes_cannot_repair_conflicting_metadata_or_identity(self):
        for changed_file in ("metadata.json", "effects/identity.json"):
            with self.subTest(changed_file=changed_file):
                row = self.assignment(changed_file)
                attempt, directory = self.attempt(row, changed_file.replace("/", "-").replace(".", "-"))
                duplicate = self.duplicate(attempt, directory, directory.name + "-copy")
                path = duplicate / changed_file
                value = json.loads(path.read_text())
                if changed_file == "metadata.json":
                    value["sample"]["grading"] = "conflicting-condition"
                else:
                    value["attempt_id"] = "another-attempt"
                path.write_text(json.dumps(value))
                result, _, _ = self.project([row])
                item, = result["construct_view"]["items"]
                audit, = result["owner_audit"]["episodes"]
                self.assertEqual(len(audit["readable_transcript_sha256s"]), 1)
                self.assertEqual(item["segments"], [])
                self.assertIsNone(audit["canonical_source"])
                self.assertFalse(audit["native_linkage_eligible"])

    def test_retry_never_replaces_unreadable_first_attempt(self):
        row = self.assignment()
        first, directory = self.attempt(row, "first")
        (directory / "messages.json").write_text("{")
        self.attempt(row, "retry", messages=self.messages("favorable retry text"), primary=False)
        result, _, _ = self.project([row])
        item, = result["construct_view"]["items"]
        audit, = result["owner_audit"]["episodes"]
        self.assertEqual(item["segments"], [])
        self.assertEqual(audit["verified_attempt_id"], first["attempt_id"])
        self.assertEqual(audit["unattributed_attempt_count"], 1)
        self.assertIn("favorable retry text", json.dumps(audit))
        self.assertNotIn("favorable retry text", json.dumps(item))

    def test_family_incompleteness_stays_unlinked_without_a_receipt(self):
        row = self.assignment()
        attempt, _ = self.attempt(row)
        for value in (row, attempt):
            value["metadata_complete"] = False
            value["metadata_absent"] = [{"source": "assignments.json", "fields": ["family"]}]
        del row["family"]
        result, index, _ = self.project([row])
        item, = result["construct_view"]["items"]
        audit, = result["owner_audit"]["episodes"]
        self.assertTrue(item["segments"])
        self.assertEqual(audit["original_index_row"], index["assignments"][0])
        self.assertFalse(audit["original_index_row"]["assignment_provenance"]["metadata_complete"])
        self.assertFalse(audit["native_index_attribution_trusted"])
        self.assertFalse(audit["native_linkage_eligible"])
        self.assertFalse(audit["family_eligibility_override_applied"])
        self.assertEqual(audit["separate_receipt_gate"], {"status": "missing_not_implemented", "verified": False})

    def test_channels_remain_typed_with_exact_unicode_and_availability(self):
        row = self.assignment()
        messages = self.messages()
        messages[-1]["content"] = [
            {"type": "text", "text": "<think>ordinary text</think>"},
            {"type": "reasoning", "reasoning": " exact α reasoning\n", "summary": "a summary"},
            {"type": "reasoning", "reasoning": "redacted-payload", "redacted": True},
            {"type": "reasoning", "reasoning": "attachment://unresolved"},
        ]
        self.attempt(row, messages=messages)
        result, _, _ = self.project([row])
        item, = result["construct_view"]["items"]
        self.assertEqual([segment["kind"] for segment in item["segments"]], ["text", "reasoning", "reasoning_summary", "reasoning", "reasoning"])
        self.assertEqual(item["segments"][1]["value"], " exact α reasoning\n")
        self.assertEqual(item["segments"][1]["json_pointer"], "/3/content/1/reasoning")
        self.assertEqual(item["segments"][2]["field"], "summary")
        self.assertIsNone(item["segments"][3]["value"])
        self.assertEqual(item["segments"][3]["availability"], "redacted")
        self.assertEqual(item["segments"][4]["availability"], "unresolved_attachment")
        self.assertEqual(item["evidence_status"], "partial")
        self.assertEqual(item["channels"]["reasoning"]["full_channel_capture"], "not_established")
        self.assertEqual(item["channels"]["reasoning_summary"]["returned_segment_count"], 1)
        self.assertEqual(item["episode_wide_absence"], "not_established")
        self.assertIn("redacted-payload", json.dumps(result["owner_audit"]))
        self.assertNotIn("redacted-payload", json.dumps(result["construct_view"]))

    def test_absent_reasoning_is_not_an_observed_negative(self):
        row = self.assignment()
        self.attempt(row)
        result, _, _ = self.project([row])
        item, = result["construct_view"]["items"]
        self.assertEqual(item["channels"]["reasoning"]["status"], "not_returned_or_not_recorded")
        self.assertEqual(item["channels"]["reasoning"]["index_availability"], "not_returned_or_not_recorded")
        self.assertEqual(item["channels"]["reasoning"]["full_channel_capture"], "not_established")
        self.assertEqual(item["episode_wide_absence"], "not_established")

    def test_malformed_transcript_retains_readable_spans_as_partial_unlinked(self):
        row = self.assignment()
        messages = self.messages()
        messages.insert(3, {"role": "assistant", "content": 42})
        self.attempt(row, messages=messages)
        result, _, _ = self.project([row])
        item, = result["construct_view"]["items"]
        audit, = result["owner_audit"]["episodes"]
        self.assertEqual(item["evidence_status"], "partial")
        self.assertEqual([segment["value"] for segment in item["segments"]], [" exact α text\n"])
        self.assertFalse(audit["native_linkage_eligible"])
        self.assertEqual(item["report"]["status"], "unavailable_ambiguous_or_incomplete")

    def test_caps_and_multiple_report_candidates_cannot_choose_a_final_report(self):
        for mode in ("cap", "multiple", "later-reasoning"):
            row = self.assignment(mode)
            messages = self.messages("first candidate")
            if mode == "multiple":
                messages.append({"role": "assistant", "content": "second candidate"})
            if mode == "later-reasoning":
                messages.append({"role": "assistant", "content": [{"type": "reasoning", "reasoning": "later"}]})
            attempt, _ = self.attempt(row, mode, messages=messages)
            if mode == "cap":
                attempt.update(model_cap=True, termination="prespecified_limit")
            result, _, _ = self.project([row])
            item, = result["construct_view"]["items"]
            audit, = result["owner_audit"]["episodes"]
            with self.subTest(mode=mode):
                self.assertEqual(item["report"]["status"], "unavailable_ambiguous_or_incomplete")
                self.assertEqual(item["report"]["segment_ids"], [])
                self.assertEqual(len(audit["snapshot_dispositions"][0]["structural_report_candidates"]), 2 if mode == "multiple" else 1)

    def test_no_report_requires_complete_natural_end(self):
        for complete in (True, False):
            row = self.assignment(str(complete))
            attempt, _ = self.attempt(row, str(complete), messages=self.messages()[:-1])
            attempt["trace_complete"] = complete
            result, _, _ = self.project([row])
            item, = result["construct_view"]["items"]
            self.assertEqual(item["report"]["status"], "no_report" if complete else "unavailable_ambiguous_or_incomplete")
            self.assertEqual(item["episode_wide_absence"], "not_established")

    def test_missing_optional_duplicate_retains_partial_readable_evidence(self):
        row = self.assignment()
        attempt, directory = self.attempt(row)
        duplicate = self.duplicate(attempt, directory)
        (duplicate / "messages.json").unlink()
        result, _, _ = self.project([row])
        item, = result["construct_view"]["items"]
        audit, = result["owner_audit"]["episodes"]
        self.assertTrue(item["segments"])
        self.assertEqual(item["evidence_status"], "partial")
        self.assertEqual(audit["selection_issues"], [])
        self.assertEqual(len(audit["readable_transcript_sha256s"]), 1)
        self.assertFalse(audit["native_linkage_eligible"])
        self.assertEqual(audit["snapshot_dispositions"][1]["disposition"], "missing_or_unreadable_optional_snapshot")

    def test_unresolved_snapshot_limits_coverage_and_unverified_duplicates_do_not_deduplicate(self):
        row = self.assignment()
        attempt, directory = self.attempt(row)
        attempt["samples"].append({"sample_key": "invalid/key"})
        result, _, _ = self.project([row])
        item, = result["construct_view"]["items"]
        self.assertTrue(item["segments"])
        self.assertEqual(item["evidence_status"], "partial")
        self.assertEqual(item["report"]["status"], "unavailable_ambiguous_or_incomplete")
        attempt["samples"].pop()
        duplicate = self.duplicate(attempt, directory)
        (duplicate / "metadata.json").unlink()
        result, _, _ = self.project([row])
        self.assertEqual(result["construct_view"]["items"][0]["segments"], [])
        self.assertIn("duplicate_provenance_compatibility_unverified", result["owner_audit"]["episodes"][0]["selection_issues"])

    def test_contradictory_native_flags_do_not_issue_linkage(self):
        row = self.assignment()
        self.attempt(row)
        _, index, schedule = self.project([row])
        index["assignments"][0]["assignment_provenance"]["metadata_complete"] = False
        result = annotation.project_annotation_episodes(index, schedule)
        audit, = result["owner_audit"]["episodes"]
        self.assertTrue(audit["native_index_attribution_trusted"])
        self.assertFalse(audit["native_linkage_eligible"])
        self.assertIn("native_attribution_flags_inconsistent", audit["selection_issues"])
        self.assertFalse(audit["original_index_row"]["assignment_provenance"]["metadata_complete"])

    def test_same_hash_cannot_hide_inconsistent_extraction(self):
        row = self.assignment()
        attempt, directory = self.attempt(row)
        self.duplicate(attempt, directory)
        _, index, schedule = self.project([row])
        index["assignments"][0]["primary_evidence"]["snapshots"][1]["transcript"]["segments"][0]["value"] = "different indexed text"
        result = annotation.project_annotation_episodes(index, schedule)
        self.assertEqual(result["construct_view"]["items"][0]["segments"], [])
        self.assertIn("same_hash_inconsistent_index_projection", result["owner_audit"]["episodes"][0]["selection_issues"])

    def test_construct_view_has_no_owner_metadata_paths_outcomes_or_prompts(self):
        row = self.assignment()
        self.attempt(row, messages=self.messages("Actor mentions a limited grading condition."))
        result, _, _ = self.project([row])
        text = json.dumps(result["construct_view"])
        for secret in ("assignment-secret", "instance-secret", "attempt-secret", "framing-secret", "grading-secret",
                       "prompt-secret", "outcome-secret", "system-prompt-secret", "user-prompt-secret", "tool-outcome-secret", str(self.root)):
            self.assertNotIn(secret, text)
        item, = result["construct_view"]["items"]
        self.assertEqual(set(item["segments"][0]), {"segment_id", "kind", "message_index", "content_index", "field", "json_pointer", "availability", "value"})
        self.assertIn("Actor mentions a limited grading condition.", text)
        self.assertEqual(result["construct_view"]["concealment"], "best_effort_actor_mentions_may_reveal_condition")
        self.assertIn("system-prompt-secret", (self.root / "artifacts/attempt-secret/messages.json").read_text())

    def test_opaque_ids_bind_schedule_assignment_verified_attempt_and_version(self):
        row = self.assignment()
        self.attempt(row)
        result, index, schedule = self.project([row])
        ids = [result["construct_view"]["items"][0]["annotation_id"]]
        changed = annotation.project_annotation_episodes(index, {**schedule, "schedule_sha256": "b" * 64})
        ids.append(changed["construct_view"]["items"][0]["annotation_id"])
        with patch.object(annotation, "PROJECTOR_VERSION", "next-version"):
            ids.append(annotation.project_annotation_episodes(index, schedule)["construct_view"]["items"][0]["annotation_id"])
        different_attempt = self.assignment()
        self.attempt(different_attempt, "different-primary")
        another, _, _ = self.project([different_attempt])
        ids.append(another["construct_view"]["items"][0]["annotation_id"])
        expected = hashlib.sha256(json.dumps([annotation.PROJECTOR_VERSION, schedule["schedule_sha256"], row["assignment_id"], row["primary_attempt_id"]], sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(ids[0], expected)
        self.assertEqual(len(set(ids)), 4)

    def test_invalid_population_and_segment_binding_reject_instead_of_dropping(self):
        row = self.assignment()
        self.attempt(row)
        _, index, schedule = self.project([row])
        duplicate = copy.deepcopy(index)
        duplicate["assignments"].append(copy.deepcopy(duplicate["assignments"][0]))
        for bad_index, bad_schedule in (
            (duplicate, schedule), (index, {**schedule, "assignment_ids": []}),
            (index, {**schedule, "assignment_ids": [row["assignment_id"]] * 2}),
            (index, {**schedule, "schedule_sha256": True}), (dict(index, version="unknown"), schedule),
        ):
            with self.assertRaises(ValueError):
                annotation.project_annotation_episodes(bad_index, bad_schedule)
        damaged = copy.deepcopy(index)
        damaged["assignments"][0]["primary_evidence"]["snapshots"][0]["transcript"]["segments"][0]["json_pointer"] = "/0/content"
        result = annotation.project_annotation_episodes(damaged, schedule)
        self.assertEqual(result["construct_view"]["assigned_count"], 1)
        self.assertEqual(result["construct_view"]["items"][0]["segments"], [])
        self.assertIn("invalid_indexed_snapshot", result["owner_audit"]["episodes"][0]["selection_issues"])


if __name__ == "__main__":
    unittest.main()
