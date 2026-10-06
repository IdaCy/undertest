import copy
import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from causes.recipe_controls.evidence import annotation_view
from causes.recipe_controls.evidence import index_evidence


class EvidenceIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.csv_rows = []

    def assignment(self, identity="assigned-one"):
        return {
            "assignment_id": identity, "instance_id": "ledger-dev-0", "framing": "ordinary", "grading": "limited",
            "rollout": 0, "prompt_variant": "base", "status": "completed", "primary_attempt_id": None,
            "primary_order_verified": True, "metadata_integrity": True, "metadata_complete": True,
            "metadata_conflicts": [], "metadata_absent": [], "outcomes": {"submitted_shortcut_anytime": 0}, "attempts": [],
        }

    def test_json_readable_but_invalid_transcripts_cannot_have_trusted_attribution(self):
        for index, invalid in enumerate(({}, [42], None, "transcript")):
            with self.subTest(invalid=invalid):
                row = self.assignment(f"invalid-{index}")
                self.add_attempt(row, f"attempt-{index}", invalid)
                result = index_evidence(self.output, [row])
                record, = result["assignments"]
                self.assertFalse(record["condition_attribution_trusted"])
                self.assertIn("unavailable_or_invalid_transcript", record["primary_evidence"]["attribution_issues"])
                item, = annotation_view(result)["items"]
                self.assertFalse(item["source_attribution_verified"])
                self.assertEqual(item["evidence_status"], "unavailable")

    def test_malformed_message_shapes_retain_evidence_without_trusted_counts(self):
        malformed = ({}, {"role": "unrecognized", "content": "unknown"},
                     {"role": "assistant", "content": 42},
                     {"role": "assistant", "content": "call text", "tool_calls": {"parse_error": "bad outer call"}},
                     {"role": "assistant", "content": "call text", "tool_calls": [42]},
                     {"role": "assistant", "content": [{"type": "text", "text": 42}]},
                     {"role": "assistant", "content": [42]})
        for index, message in enumerate(malformed):
            with self.subTest(message=message):
                row = self.assignment(f"malformed-{index}")
                self.add_attempt(row, f"attempt-{index}", [message, {"role": "assistant", "content": "retained text"}])
                result = index_evidence(self.output, [row])
                record, = result["assignments"]
                transcript = record["primary_evidence"]["snapshots"][0]["transcript"]
                self.assertFalse(record["condition_attribution_trusted"])
                self.assertEqual(transcript["status"], "partially_available")
                self.assertTrue(transcript["schema_issues"])
                self.assertIsNone(transcript["outer_call_parse_error_events"])
                self.assertIsNone(transcript["tool_parsing_error_events"])
                self.assertIn("retained text", [segment["value"] for segment in transcript["segments"]])
                self.assertIn("malformed_transcript_structure", transcript["report"]["ambiguity_reasons"])
                view, = annotation_view(result)["items"]
                self.assertFalse(view["source_attribution_verified"])
                self.assertEqual(view["evidence_status"], "partially_available")
                if index == 3:
                    self.assertEqual(transcript["schema_issues"][0]["value"], message["tool_calls"])
                    self.assertEqual(transcript["schema_issues"][0]["json_pointer"], "/0/tool_calls")

    def test_cyclic_source_symlink_is_explicitly_unreadable(self):
        row = self.assignment()
        _, directory = self.add_attempt(row, "cycle", [{"role": "assistant", "content": "before"}])
        source = directory / "messages.json"
        source.unlink()
        source.symlink_to(source.name)
        record, = index_evidence(self.output, [row])["assignments"]
        snapshot, = record["primary_evidence"]["snapshots"]
        self.assertEqual(snapshot["sources"]["messages.json"]["status"], "unreadable")
        self.assertEqual(snapshot["transcript"]["status"], "unavailable")
        self.assertFalse(record["condition_attribution_trusted"])

    def test_deep_or_nonfinite_json_sources_do_not_abort_assigned_index(self):
        malformed = ('[{"role":"unrecognized","content":' + '[' * 500 + '0' + ']' * 500 + '}]',
                     '[{"role":"assistant","content":NaN}]')
        for index, raw in enumerate(malformed):
            row = self.assignment(f"malformed-source-{index}")
            _, directory = self.add_attempt(row, f"bad-{index}", [])
            source = directory / "messages.json"
            source.write_text(raw)
            good = self.assignment(f"valid-source-{index}")
            self.add_attempt(good, f"good-{index}", [{"role": "assistant", "content": "survives"}])
            records = index_evidence(self.output, [row, good])["assignments"]
            self.assertEqual(len(records), 2)
            bad_snapshot = records[0]["primary_evidence"]["snapshots"][0]
            reference = bad_snapshot["sources"]["messages.json"]
            self.assertEqual(reference["status"], "unsupported_nesting_depth" if index == 0 else "unreadable")
            self.assertEqual(reference["sha256"], hashlib.sha256(raw.encode()).hexdigest())
            self.assertEqual(bad_snapshot["transcript"]["status"], "unavailable")
            self.assertFalse(records[0]["condition_attribution_trusted"])
            self.assertTrue(records[1]["condition_attribution_trusted"])

    def test_error_fields_used_in_counts_require_valid_shapes(self):
        malformed = [{"role": "tool", "content": "", "error": value}
                     for value in ("unparsed error", {}, {"type": ["parsing"], "message": "bad"},
                                   {"type": "parsing", "message": 42})]
        malformed.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": "call", "function": "submit_recipe", "arguments": {}, "parse_error": 42}]})
        for index, message in enumerate(malformed):
            row = self.assignment(f"bad-error-{index}")
            self.add_attempt(row, f"error-{index}", [message, {"role": "assistant", "content": "Final text"}])
            record, = index_evidence(self.output, [row])["assignments"]
            transcript = record["primary_evidence"]["snapshots"][0]["transcript"]
            self.assertEqual(transcript["status"], "partially_available")
            self.assertFalse(record["condition_attribution_trusted"])
            self.assertTrue(transcript["report"]["ambiguous"])
            self.assertIsNone(transcript["outer_call_parse_error_events"])
            self.assertIsNone(transcript["tool_parsing_error_events"])
        row = self.assignment("empty-error-string")
        self.add_attempt(row, "empty-error", [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "call", "function": "submit_recipe", "arguments": {}, "parse_error": ""}]}])
        record, = index_evidence(self.output, [row])["assignments"]
        self.assertEqual(record["primary_evidence"]["snapshots"][0]["transcript"]["outer_call_parse_error_events"], 1)

    def add_attempt(self, row, identity, messages, *, first=True, capped=False):
        directory = self.output / "artifacts" / identity
        (directory / "effects").mkdir(parents=True)
        metadata = {key: row[key] for key in ("assignment_id", "instance_id", "framing", "grading", "rollout", "prompt_variant")}
        ledger = [{"index": 0, "kind": "test", "raw": "{}", "valid": False},
                  {"index": 1, "kind": "submit", "raw": '{"steps":[]}', "valid": False}]
        objects = {
            "messages.json": messages, "metadata.json": {"sample": metadata}, "scores.json": {}, "export_errors.json": [],
            "effects/identity.json": {"assignment_id": row["assignment_id"], "attempt_id": identity},
            "effects/ledger.json": ledger, "effects/tool_requests.json": {"requests": [], "tool_errors": []},
            "effects/assessment.json": {"outcomes": row["outcomes"]},
            "effects/model_output.json": {"usage": {"input_tokens": 12, "output_tokens": 3}, "choices": [{"stop_reason": "max_tokens" if capped else "stop"}]},
        }
        for name, value in objects.items():
            (directory / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        ledger_path = self.output / f"{identity}.jsonl"
        ledger_path.write_text('{"phase":"start"}\n{"phase":"cleanup"}\n')
        attempt = {
            "attempt_id": identity, "started": True, "trace_complete": True,
            "metadata_integrity": True, "metadata_complete": True, "metadata_conflicts": [], "metadata_absent": [],
            "ledger_integrity": True, "ledger_available": True, "candidates": ledger, "ledger_paths": [str(ledger_path)],
            "outcomes": copy.deepcopy(row["outcomes"]), "model_cap": capped, "sample_limit": False,
            "last_stop_reason": "max_tokens" if capped else "stop", "finish_reason": None, "termination": "prespecified_limit" if capped else "natural",
            "limits": [], "errors": [], "tool_requests": [], "tool_errors": [],
            "samples": [{"sample_key": identity, "sample_id": row["assignment_id"], "artifacts": str(directory), "error": "", "limit": ""}],
        }
        row["attempts"].append(attempt)
        if first:
            row["primary_attempt_id"] = identity
        self.csv_rows.append({"sample_key": identity, "sample_id": row["assignment_id"], "sample_metadata": json.dumps(metadata),
                              "input_tokens": "0", "output_tokens": "0", "latest_attempt": "True"})
        with (self.output / "samples.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(self.csv_rows[0]))
            writer.writeheader()
            writer.writerows(self.csv_rows)
        return attempt, directory

    def test_exact_text_reasoning_summary_fields_hashes_and_report_locations(self):
        row = self.assignment()
        messages = [
            {"role": "user", "content": "PROMPT_SECRET routine condition"},
            {"role": "assistant", "content": "<think>plain assistant text</think>"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Plan\n"},
                {"type": "reasoning", "reasoning": " exact α reasoning  \n", "summary": "short summary", "redacted": False},
                {"type": "reasoning", "reasoning": "opaque_payload", "redacted": True},
            ]},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "bad", "function": "submit_recipe", "arguments": {}, "parse_error": "outer JSON error"}]},
            {"role": "tool", "content": "", "tool_call_id": "bad", "error": {"type": "parsing", "message": "bad arguments"}},
            {"role": "assistant", "content": [{"type": "text", "text": "Done.\n"}, {"type": "text", "text": " Verified."}]},
        ]
        attempt, directory = self.add_attempt(row, "first", messages)
        result = index_evidence(self.output, [row])
        record, = result["assignments"]
        self.assertTrue(record["condition_attribution_trusted"])
        evidence = record["primary_evidence"]
        snapshot, = evidence["snapshots"]
        transcript = snapshot["transcript"]
        segments = transcript["segments"]
        reasoning = next(segment for segment in segments if segment["value"] == " exact α reasoning  \n")
        self.assertEqual((reasoning["kind"], reasoning["field"], reasoning["message_index"], reasoning["content_index"]), ("reasoning", "reasoning", 2, 1))
        self.assertEqual(reasoning["json_pointer"], "/2/content/1/reasoning")
        self.assertEqual(reasoning["source"]["sha256"], hashlib.sha256((directory / "messages.json").read_bytes()).hexdigest())
        self.assertEqual(segments[0]["kind"], "text")
        self.assertEqual(segments[0]["value"], "<think>plain assistant text</think>")
        self.assertEqual(transcript["reasoning_availability"], "partially_readable_returned_blocks")
        self.assertEqual([segment["availability"] for segment in segments if segment["value"] == "opaque_payload"], ["redacted"])
        self.assertEqual(transcript["report"]["message_index"], 5)
        self.assertFalse(transcript["report"]["ambiguous"])
        report = [segment["value"] for segment in segments if segment["segment_id"] in transcript["report"]["segment_ids"]]
        self.assertEqual(report, ["Done.\n", " Verified."])
        self.assertEqual((transcript["outer_call_parse_error_events"], transcript["tool_parsing_error_events"]), (1, 1))
        self.assertEqual(evidence["candidate_counts"]["submit"], 1)
        self.assertEqual(evidence["candidate_counts"]["test"], 1)
        self.assertEqual(evidence["candidate_counts"]["outer_call_to_candidate_linkage"], "not_established_no_invocation_ids")
        self.assertEqual(evidence["ledger_sources"][0]["status"], "readable")
        self.assertEqual(evidence["preserved_attempt_fields"]["outcomes"], attempt["outcomes"])

    def test_absent_redacted_empty_unresolved_and_unreadable_reasoning_stay_distinct(self):
        cases = (
            ([{"role": "assistant", "content": "plain"}], "not_returned_or_not_recorded"),
            ([{"role": "assistant", "content": [{"type": "reasoning", "reasoning": "opaque", "redacted": True}]}], "redacted_returned_blocks"),
            ([{"role": "assistant", "content": [{"type": "reasoning", "reasoning": ""}]}], "returned_blocks_without_readable_reasoning"),
            ([{"role": "assistant", "content": [{"type": "reasoning", "reasoning": "attachment://unresolved"}]}], "returned_blocks_without_readable_reasoning"),
        )
        for index, (messages, expected) in enumerate(cases):
            row = self.assignment(f"a-{index}")
            self.add_attempt(row, f"case-{index}", messages)
            record, = index_evidence(self.output, [row])["assignments"]
            self.assertEqual(record["primary_evidence"]["snapshots"][0]["transcript"]["reasoning_availability"], expected)
        row = self.assignment("broken")
        _, directory = self.add_attempt(row, "broken", [])
        (directory / "messages.json").write_bytes(b"not JSON")
        record, = index_evidence(self.output, [row])["assignments"]
        self.assertFalse(record["condition_attribution_trusted"])
        transcript = record["primary_evidence"]["snapshots"][0]["transcript"]
        self.assertEqual(transcript["reasoning_availability"], "unavailable_transcript")
        self.assertIsNone(transcript["outer_call_parse_error_events"])

    def test_first_attempt_never_uses_latest_retry_and_unknown_assignments_remain_visible(self):
        row = self.assignment()
        self.add_attempt(row, "first", [{"role": "assistant", "content": "FIRST"}])
        self.add_attempt(row, "retry", [{"role": "assistant", "content": "RETRY"}], first=False)
        missing = self.assignment("never-started")
        missing.update(status="never_started", primary_order_verified=None, outcomes={"submitted_shortcut_anytime": None})
        records = index_evidence(self.output, [row, missing])["assignments"]
        first = records[0]["primary_evidence"]
        self.assertEqual(first["attempt_id"], "first")
        self.assertEqual(first["snapshots"][0]["transcript"]["segments"][0]["value"], "FIRST")
        self.assertFalse(records[0]["unattributed_attempts"][0]["condition_attribution_trusted"])
        self.assertEqual(records[1]["evidence_status"], "no_attempt_evidence")
        view = annotation_view(index_evidence(self.output, [row, missing]))
        self.assertEqual(len(view["items"]), 2)
        self.assertNotIn("RETRY", json.dumps(view))
        row.update(primary_attempt_id=None, primary_order_verified=False)
        record, = index_evidence(self.output, [row])["assignments"]
        self.assertIsNone(record["primary_evidence"])
        self.assertEqual(len(record["unattributed_attempts"]), 2)
        self.assertFalse(record["condition_attribution_trusted"])

    def test_provenance_conflict_legacy_flags_and_changed_metadata_quarantine_attribution(self):
        row = self.assignment()
        attempt, directory = self.add_attempt(row, "first", [{"role": "assistant", "content": "retained"}])
        legacy = copy.deepcopy(row)
        del legacy["metadata_integrity"]
        del legacy["attempts"][0]["metadata_complete"]
        record, = index_evidence(self.output, [legacy])["assignments"]
        self.assertFalse(record["condition_attribution_trusted"])
        self.assertIn("assignment_metadata_integrity_unverified", record["primary_evidence"]["attribution_issues"])
        row["metadata_conflicts"] = [{"field": "grading"}]
        attempt["termination"] = "metadata_conflict"
        record, = index_evidence(self.output, [row])["assignments"]
        self.assertFalse(record["condition_attribution_trusted"])
        self.assertEqual(record["primary_evidence"]["snapshots"][0]["transcript"]["segments"][0]["value"], "retained")
        row["metadata_conflicts"] = []
        attempt["termination"] = "natural"
        metadata = json.loads((directory / "metadata.json").read_text())
        metadata["sample"]["grading"] = "broad"
        (directory / "metadata.json").write_text(json.dumps(metadata))
        record, = index_evidence(self.output, [row])["assignments"]
        self.assertIn("artifact_metadata_mismatch_grading", record["primary_evidence"]["attribution_issues"])

    def test_report_is_structural_and_caps_or_multiple_late_messages_are_ambiguous(self):
        row = self.assignment()
        self.add_attempt(row, "first", [
            {"role": "tool", "content": "ok"}, {"role": "assistant", "content": "First report"},
            {"role": "assistant", "content": "Last report"},
            {"role": "assistant", "content": [{"type": "reasoning", "reasoning": "extra"}]},
        ], capped=True)
        record, = index_evidence(self.output, [row])["assignments"]
        report = record["primary_evidence"]["snapshots"][0]["transcript"]["report"]
        self.assertEqual(report["message_index"], 2)
        self.assertTrue(report["ambiguous"])
        self.assertEqual(set(report["ambiguity_reasons"]), {"multiple_post_tool_text_messages", "recorded_generation_or_sample_limit", "later_assistant_message_without_report_text"})
        self.assertTrue(report["selection_is_structural_not_semantic"])

    def test_usage_is_reported_with_scope_and_zero_availability_caveats(self):
        row = self.assignment()
        self.add_attempt(row, "first", [{"role": "assistant", "content": "done"}])
        record, = index_evidence(self.output, [row])["assignments"]
        evidence = record["primary_evidence"]
        usage, = evidence["reported_sample_usage"]
        self.assertEqual((usage["input_tokens"], usage["output_tokens"]), ("0", "0"))
        self.assertEqual(usage["availability"], "unverified_zero_may_encode_missing_model_usage")
        self.assertEqual(usage["row_index"], 0)
        self.assertEqual(usage["source"]["sha256"], hashlib.sha256((self.output / "samples.csv").read_bytes()).hexdigest())
        final_usage = evidence["snapshots"][0]["stored_model_output_usage"]
        self.assertEqual(final_usage["scope"], "stored_model_output_only_not_episode_total")
        self.assertEqual(final_usage["value"], {"input_tokens": 12, "output_tokens": 3})

    def test_annotation_view_hides_prompts_factors_outcomes_and_paths_but_preserves_actor_mentions(self):
        row = self.assignment("ordinary-limited-leaking-id")
        self.add_attempt(row, "first", [
            {"role": "system", "content": "SYSTEM_SECRET"}, {"role": "user", "content": "PROMPT_SECRET"},
            {"role": "assistant", "content": [{"type": "reasoning", "reasoning": "The actor mentions evaluation."}, {"type": "text", "text": "Done."}]},
        ])
        index = index_evidence(self.output, [row])
        before = copy.deepcopy(index)
        view = annotation_view(index)
        self.assertEqual(index, before)
        encoded = json.dumps(view)
        for forbidden in ("SYSTEM_SECRET", "PROMPT_SECRET", "ordinary-limited-leaking-id", str(self.output), "ledger-dev-0", "submitted_shortcut_anytime"):
            self.assertNotIn(forbidden, encoded)
        self.assertIn("The actor mentions evaluation.", encoded)
        self.assertEqual(view["concealment"], "best_effort_actor_mentions_may_reveal_condition")
        self.assertEqual(view["items"][0]["segments"][0]["kind"], "reasoning")

    def test_unreadable_and_outside_output_sources_are_not_silently_replaced(self):
        row = self.assignment()
        _, directory = self.add_attempt(row, "first", [{"role": "assistant", "content": "original"}])
        outside = self.output.parent / (self.output.name + "-outside.json")
        outside.write_text('[{"role":"assistant","content":"outside"}]')
        self.addCleanup(outside.unlink)
        (directory / "messages.json").unlink()
        (directory / "messages.json").symlink_to(outside)
        record, = index_evidence(self.output, [row])["assignments"]
        snapshot, = record["primary_evidence"]["snapshots"]
        self.assertEqual(snapshot["sources"]["messages.json"]["status"], "outside_output_root")
        self.assertEqual(snapshot["transcript"]["segments"], [])
        self.assertFalse(record["condition_attribution_trusted"])

    def test_index_is_deterministic_nonmutating_and_rejects_duplicate_assignments(self):
        row = self.assignment()
        self.add_attempt(row, "first", [{"role": "assistant", "content": "exact \n"}])
        before = copy.deepcopy(row)
        first = index_evidence(self.output, [row])
        self.assertEqual(first, index_evidence(self.output, [row]))
        self.assertEqual(row, before)
        self.assertEqual(json.loads(json.dumps(first, allow_nan=False)), first)
        with self.assertRaises(ValueError):
            index_evidence(self.output, [row, row])


if __name__ == "__main__":
    unittest.main()
