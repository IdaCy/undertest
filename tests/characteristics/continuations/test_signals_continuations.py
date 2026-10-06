import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import signals.validation.controls.environment as environment
import signals.validation.controls.runner as runner
from characteristics.continuations.continuations import freeze_branch_ledger
from characteristics.continuations.continuations import prepare_branches
from characteristics.continuations.continuations import read_frozen_checkpoint
from characteristics.continuations.continuations import restore_checkpoint
from characteristics.continuations.continuations import retain_branch_results


def response(inspect=False):
    message = {"role": "assistant", "content": None if inspect else "Done.",
               "reasoning_content": "I should inspect the current artifact before proceeding."}
    if inspect:
        message["tool_calls"] = [{"id": "call-0", "type": "function",
                                  "function": {"name": "inspect_artifact", "arguments": "{}"}}]
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if inspect else "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10}}


def expectations(record):
    payload = record["payload"]
    result = {"checkpoint_sha256": record["sha256"]}
    result.update({f"{key}_sha256": runner.digest(payload[key])
                   for key in ("case", "assignment", "initial_context", "tools")})
    result.update({key: copy.deepcopy(payload[key]) for key in
                   ("model", "model_revision", "generation", "limits", "seed_derivation_version")})
    for name, module in (("environment", environment), ("restoration_runner", runner)):
        result[f"{name}_source_sha256"] = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    return result


class ContinuationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as directory:
            answers = iter([response(True), response()])
            cls.case = environment.make_case("ledger", "development", 0)
            cls.assignment = runner.assignment_ledger(["ledger"], "development", 1, 1, 17)[0]
            cls.assignment["case_id"] = cls.case["case_id"]
            output = Path(directory) / "actor"
            runner.run_episode(
                case=cls.case, assignment=cls.assignment, output=output, model="fake/model",
                model_revision="exact-revision", generation={"temperature": 1, "top_p": 0.95},
                call=lambda request: next(answers), environment=environment,
            )
            cls.original = json.loads((output / "checkpoint.json").read_text())

    def setUp(self):
        self.record = copy.deepcopy(self.original)
        self.expected = expectations(self.record)

    def restore(self, record=None, expected=None):
        return restore_checkpoint(record or self.record, expected=expected or self.expected, environment=environment)

    def rebind(self, record):
        record["sha256"] = runner.digest(record["payload"])
        return expectations(record)

    def assignment_rows(self, *, explicit=False):
        rows = []
        for condition, replacement in (("unchanged", None), ("sham", "First I should"),
                                       ("criterion_text", "The task says to"), ("reasoning_text", "I will")):
            edits = [] if condition == "unchanged" else [{
                "path": ["messages", 0, "reasoning_content"], "start": 0, "end": 8,
                "before": "I should", "after": replacement,
            }]
            rows.append({
                "branch_id": f"parent-r0-{condition}", "parent_checkpoint_sha256": self.record["sha256"],
                "comparison_id": "comparison-1", "replicate_id": "r0", "condition": condition,
                "edits": edits, "seed_mode": "explicit" if explicit else "original",
                "request_seeds": [101, 102, 103, 104, 105] if explicit else None,
            })
        return rows

    def ledger(self, rows=None):
        return freeze_branch_ledger(rows or self.assignment_rows(), protocol_sha256="a" * 64, selection_sha256="b" * 64)

    def prepare(self, ledger):
        parent_hash = self.record["sha256"]
        return prepare_branches(
            ledger, expected_ledger_sha256=ledger["sha256"], parents={parent_hash: self.record},
            parent_expectations={parent_hash: self.expected}, environment=environment,
        )

    def test_roundtrip_preserves_budget_history_identity_and_exact_next_seed(self):
        restored = self.restore()
        self.assertEqual(restored["resume_payload"], self.record["payload"])
        self.assertEqual(restored["next_step"], 1)
        self.assertEqual(restored["remaining_calls"], 5)
        self.assertEqual(restored["remaining_completion_tokens"], 15990)
        self.assertEqual(restored["remaining_tool_calls"], 11)
        self.assertEqual(restored["next_generation_seed"], runner.request_seed(self.assignment, 1))
        self.assertEqual(restored["request_seeds"], [runner.request_seed(self.assignment, step) for step in range(1, 6)])
        restored["resume_payload"]["messages"][0]["reasoning_content"] = "changed"
        self.assertNotEqual(restored["resume_payload"], self.record["payload"])
        self.assertEqual(self.record, self.original)

    def test_v2_restore_regenerates_saved_interface_with_original_total_budgets(self):
        for family in environment.FAMILIES:
            case = environment.make_case(family, "development", 0, public_interface_version=environment.PUBLIC_INTERFACE_V2)
            assignment = runner.assignment_ledger([family], "development", 1, 1, 17)[0]
            assignment["case_id"] = case["case_id"]
            with tempfile.TemporaryDirectory() as temporary:
                answers = iter([response(True), response()])
                output = Path(temporary) / "actor"
                runner.run_episode(case=case, assignment=assignment, output=output, model="fake/model",
                    model_revision="exact-revision", generation={"temperature": 1}, call=lambda request: next(answers),
                    environment=environment, max_calls=8, per_call_tokens=512, max_completion_tokens=1400)
                record = json.loads((output / "checkpoint.json").read_text())
            restored = restore_checkpoint(record, expected=expectations(record), environment=environment)
            self.assertEqual(restored["resume_payload"], record["payload"])
            self.assertEqual(restored["remaining_calls"], 7)
            self.assertEqual(restored["remaining_completion_tokens"], 1390)
            public = json.loads(restored["resume_payload"]["initial_context"][1]["content"])
            self.assertEqual(public["model_response_budget"]["max_calls"], 8)
            self.assertEqual(public["model_response_budget"]["max_completion_tokens"], 1400)
            changed = copy.deepcopy(record)
            changed["payload"]["initial_context"][1]["content"] = environment.render_case(
                case, max_calls=7, per_call_tokens=512, max_completion_tokens=1390,
            )
            changed["sha256"] = runner.digest(changed["payload"])
            with self.assertRaisesRegex(ValueError, "context/tools"):
                restore_checkpoint(changed, expected=expectations(changed), environment=environment)

    def test_checkpoint_file_requires_exact_bytes_and_strict_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            body = json.dumps(self.record).encode()
            path.write_bytes(body)
            kwargs = {"expected_bytes": len(body), "expected_file_sha256": hashlib.sha256(body).hexdigest()}
            self.assertEqual(read_frozen_checkpoint(path, **kwargs), self.record)
            with self.assertRaises(ValueError):
                read_frozen_checkpoint(path, **{**kwargs, "expected_bytes": len(body) + 1})
            with self.assertRaises(ValueError):
                read_frozen_checkpoint(path, **{**kwargs, "expected_file_sha256": "f" * 64})
            for malformed in (b'{"schema_version":1,"schema_version":1,"payload":{},"sha256":"x"}',
                              b'{"schema_version":1,"payload":{"x":NaN},"sha256":"x"}'):
                path.write_bytes(malformed)
                with self.assertRaises(ValueError):
                    read_frozen_checkpoint(path, expected_bytes=len(malformed),
                                           expected_file_sha256=hashlib.sha256(malformed).hexdigest())

    def test_checkpoint_hash_cannot_replace_frozen_external_identity(self):
        changed = copy.deepcopy(self.record)
        changed["payload"]["model_revision"] = "other-revision"
        self.rebind(changed)
        with self.assertRaisesRegex(ValueError, "frozen parent identity"):
            self.restore(changed)
        for field in ("case_sha256", "initial_context_sha256", "tools_sha256", "assignment_sha256",
                      "environment_source_sha256", "restoration_runner_source_sha256"):
            with self.subTest(field=field):
                expected = copy.deepcopy(self.expected)
                expected[field] = "0" * 64
                with self.assertRaises(ValueError):
                    self.restore(expected=expected)

    def test_declared_model_generation_and_limits_must_match_external_manifest(self):
        for field, replacement in (("model", "other/model"), ("model_revision", "other"),
                                   ("generation", {"temperature": 0}), ("limits", {})):
            with self.subTest(field=field):
                expected = copy.deepcopy(self.expected)
                expected[field] = replacement
                with self.assertRaisesRegex(ValueError, "identity mismatch"):
                    self.restore(expected=expected)

    def test_state_and_transcript_are_independently_replayed(self):
        for mutation in ("artifact", "audit", "tool_result", "tool_id", "pending", "extra_message", "mutable_tool"):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(self.record)
                payload = changed["payload"]
                if mutation == "artifact":
                    payload["state"]["artifact"] = self.case["controls"]["shortcut"]
                elif mutation == "audit":
                    payload["state"]["audit_log"][0]["response"]["artifact"] = {}
                elif mutation == "tool_result":
                    payload["messages"][1]["content"] = '{"ok": true}'
                elif mutation == "tool_id":
                    payload["messages"][1]["tool_call_id"] = "other-id"
                elif mutation == "pending":
                    payload["messages"].pop()
                elif mutation == "extra_message":
                    payload["messages"].append(copy.deepcopy(payload["messages"][0]))
                else:
                    payload["messages"][0]["tool_calls"][0]["function"]["name"] = "submit_artifact"
                expected = self.rebind(changed)
                with self.assertRaises(ValueError):
                    self.restore(changed, expected)

    def test_restored_fixed_sequences_reproduce_transitions_and_grades_in_both_families(self):
        for family in environment.FAMILIES:
            with self.subTest(family=family), tempfile.TemporaryDirectory() as directory:
                case = environment.make_case(family, "development", 1)
                assignment = runner.assignment_ledger([family], "development", 2, 1, 19)
                assignment = next(row for row in assignment if row["case_index"] == 1)
                answers = iter([response(True), response()])
                output = Path(directory) / "actor"
                runner.run_episode(case=case, assignment=assignment, output=output,
                                   model="fake/model", model_revision="exact-revision", generation={},
                                   call=lambda request: next(answers), environment=environment)
                record = json.loads((output / "checkpoint.json").read_text())
                restored = restore_checkpoint(record, expected=expectations(record), environment=environment)
                replay = environment.initial_state(case)
                environment.apply_tool(case, replay, "inspect_artifact", {})
                state = restored["resume_payload"]["state"]
                for name, args in (("submit_artifact", {"artifact": case["controls"]["shortcut"]}),
                                   ("evaluate_visible", {}),
                                   ("submit_artifact", {"artifact": case["controls"]["correct"]}),
                                   ("evaluate_visible", {})):
                    self.assertEqual(environment.apply_tool(case, state, name, args),
                                     environment.apply_tool(case, replay, name, args))
                    self.assertEqual(state, replay)
                    self.assertEqual(environment.grade(case, state), environment.grade(case, replay))
                self.assertTrue(environment.grade(case, state)["shortcut_attempt"])
                self.assertTrue(environment.grade(case, state)["task_success"])
                self.assertEqual(record["payload"]["state"]["artifact"], None)

    def test_case_and_public_context_cannot_be_replaced_with_self_consistent_hashes(self):
        for mutation in ("case", "initial_context", "tools"):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(self.record)
                payload = changed["payload"]
                if mutation == "case":
                    payload["case"]["private"]["hidden_cases"] = []
                elif mutation == "initial_context":
                    payload["initial_context"][1]["content"] = "Changed task"
                else:
                    payload["tools"][0]["function"]["description"] = "Changed tool"
                with self.assertRaises(ValueError):
                    self.restore(changed, self.rebind(changed))

    def test_exhausted_invalid_or_mismatched_budgets_and_seeds_reject(self):
        variants = [
            ("completed_calls", 2), ("completed_calls", True), ("next_generation_seed", 0),
            ("usage", {"prompt_tokens": 50, "completion_tokens": 16000}),
            ("usage", {"prompt_tokens": 50, "completion_tokens": 4000}),
            ("usage", {"prompt_tokens": 50, "completion_tokens": -1}),
            ("limits", {"max_calls": 1, "max_completion_tokens": 16000, "per_call_tokens": 3072}),
            ("seed_derivation_version", "other-version"),
        ]
        for field, value in variants:
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(self.record)
                changed["payload"][field] = value
                with self.assertRaises(ValueError):
                    self.restore(changed, self.rebind(changed))

    def test_unknown_metadata_nonfinite_json_and_duplicate_tool_json_reject(self):
        changed = copy.deepcopy(self.record)
        changed["payload"]["pending_operation"] = None
        with self.assertRaises(ValueError):
            self.restore(changed, self.rebind(changed))
        changed = copy.deepcopy(self.record)
        changed["payload"]["generation"]["temperature"] = float("nan")
        with self.assertRaises(ValueError):
            self.restore(changed)
        changed = copy.deepcopy(self.record)
        changed["payload"]["messages"][1]["content"] = '{"ok": false, "ok": true}'
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.restore(changed, self.rebind(changed))

    def test_unchanged_branch_is_exact_and_edits_do_not_mutate_parent_or_other_branches(self):
        ledger = self.ledger()
        rows = self.prepare(ledger)
        self.assertEqual([row["status"] for row in rows], ["ready"] * 4)
        self.assertEqual(rows[0]["resume_payload"], self.record["payload"])
        self.assertEqual(rows[0]["edited_payload_sha256"], self.record["sha256"])
        self.assertTrue(rows[1]["resume_payload"]["messages"][0]["reasoning_content"].startswith("First I should"))
        for row in rows:
            self.assertEqual(row["original_payload_sha256"], self.record["sha256"])
            self.assertEqual(row["request_seeds"][0], self.record["payload"]["next_generation_seed"])
            self.assertEqual(row["resume_payload"]["state"], self.record["payload"]["state"])
            self.assertEqual(row["resume_payload"]["usage"], self.record["payload"]["usage"])
        rows[1]["resume_payload"]["state"]["artifact"] = {}
        self.assertEqual(rows[0]["resume_payload"]["state"]["artifact"], None)
        self.assertEqual(self.record, self.original)

    def test_context_edits_are_explicit_and_preserve_tool_and_environment_identity(self):
        assignments = self.assignment_rows()
        context = self.record["payload"]["initial_context"][0]["content"]
        assignments[2]["edits"] = [{"path": ["initial_context", 0, "content"], "start": 0,
                                    "end": 8, "before": context[:8], "after": "Carry out"}]
        rows = self.prepare(self.ledger(assignments))
        self.assertEqual(rows[2]["status"], "ready")
        self.assertNotEqual(rows[2]["resume_payload"]["initial_context"], self.record["payload"]["initial_context"])
        self.assertEqual(rows[2]["resume_payload"]["messages"], self.record["payload"]["messages"])
        self.assertEqual(rows[2]["resume_payload"]["state"], self.record["payload"]["state"])

    def test_typed_reasoning_blocks_preserve_public_text_and_metadata(self):
        self.record["payload"]["messages"][0]["content"] = [
            {"type": "text", "text": "Inspecting."},
            {"type": "reasoning", "reasoning": "I should inspect.", "text": "shadowed"},
        ]
        self.expected = self.rebind(self.record)
        assignments = self.assignment_rows()
        assignments[1]["edits"][0]["path"] = ["messages", 0, "content", 1, "reasoning"]
        rows = self.prepare(self.ledger(assignments))
        self.assertEqual(rows[1]["status"], "ready")
        self.assertEqual(rows[1]["resume_payload"]["messages"][0]["content"], [
            {"type": "text", "text": "Inspecting."},
            {"type": "reasoning", "reasoning": "First I should inspect.", "text": "shadowed"},
        ])
        assignments[1]["edits"][0].update({"path": ["messages", 0, "content", 1, "text"],
                                          "before": "shadowed"})
        self.assertEqual(self.prepare(self.ledger(assignments))[1]["status"], "preparation_error")

    def test_explicit_schedule_is_recorded_without_rewriting_parent_seed(self):
        rows = self.prepare(self.ledger(self.assignment_rows(explicit=True)))
        for row in rows:
            self.assertEqual(row["status"], "ready")
            self.assertEqual(row["next_request_seed"], 101)
            self.assertEqual(row["request_seeds"], [101, 102, 103, 104, 105])
            self.assertEqual(row["original_next_generation_seed"], self.record["payload"]["next_generation_seed"])
            self.assertEqual(row["resume_payload"]["next_generation_seed"], self.record["payload"]["next_generation_seed"])
        assignments = self.assignment_rows(explicit=True)
        for row in assignments:
            row["request_seeds"] = [101]
        self.assertTrue(all(row["status"] == "preparation_error" for row in self.prepare(self.ledger(assignments))))

    def test_controls_and_matching_are_required_before_ledger_freeze(self):
        rows = self.assignment_rows()
        for subset in (rows[1:], rows[:2], rows[:1] + rows[2:]):
            with self.assertRaises(ValueError):
                self.ledger(subset)
        self.assertEqual(len(self.ledger(rows[:1])["payload"]["assignments"]), 1)
        rows[1]["parent_checkpoint_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "share a parent"):
            self.ledger(rows)
        rows = self.assignment_rows(explicit=True)
        rows[1]["request_seeds"][0] = 106
        with self.assertRaises(ValueError):
            self.ledger(rows)

    def test_invalid_edits_and_missing_parent_retain_every_assigned_failure(self):
        assignments = self.assignment_rows()
        assignments[1]["edits"][0]["before"] = "WRONGTXT"
        assignments[2]["edits"][0]["path"] = ["messages", 1, "content"]
        rows = self.prepare(self.ledger(assignments))
        self.assertEqual([row["status"] for row in rows], ["ready", "preparation_error", "preparation_error", "ready"])
        self.assertEqual([row["branch_id"] for row in rows], [row["branch_id"] for row in assignments])
        ledger = self.ledger()
        missing = prepare_branches(ledger, expected_ledger_sha256=ledger["sha256"], parents={},
                                   parent_expectations={}, environment=environment)
        self.assertEqual(len(missing), 4)
        self.assertTrue(all(row["status"] == "preparation_error" and row["error"] for row in missing))

    def test_multiple_spans_use_original_offsets_and_overlaps_fail(self):
        assignments = self.assignment_rows()
        assignments[1]["edits"].append({"path": ["messages", 0, "reasoning_content"],
                                         "start": 9, "end": 16, "before": "inspect", "after": "read"})
        row = self.prepare(self.ledger(assignments))[1]
        self.assertEqual(row["status"], "ready")
        self.assertTrue(row["resume_payload"]["messages"][0]["reasoning_content"].startswith("First I should read"))
        assignments[1]["edits"].append({"path": ["messages", 0, "reasoning_content"],
                                         "start": 2, "end": 8, "before": "should", "after": "must"})
        self.assertEqual(self.prepare(self.ledger(assignments))[1]["status"], "preparation_error")

    def test_ledger_tampering_and_unsafe_edits_reject(self):
        ledger = self.ledger()
        ledger["payload"]["assignments"][0]["replicate_id"] = "changed"
        with self.assertRaises(ValueError):
            self.prepare(ledger)
        for path in (["state", 0, "artifact"], ["messages", 0, "tool_calls"],
                     ["messages", 0, "role"], ["messages", True, "content"]):
            assignments = self.assignment_rows()
            assignments[1]["edits"][0]["path"] = path
            with self.assertRaises(ValueError):
                self.ledger(assignments)

    def test_result_accounting_keeps_failures_unknowns_and_all_assigned_branches(self):
        ledger = self.ledger()
        assignments = ledger["payload"]["assignments"]
        outcomes = [
            {"branch_id": assignments[0]["branch_id"], "assignment_sha256": runner.digest(assignments[0]),
             "status": "completed", "observed_outcomes": {"shortcut_attempt": True}},
            {"branch_id": assignments[1]["branch_id"], "assignment_sha256": runner.digest(assignments[1]),
             "status": "error", "error": "transport"},
            {"branch_id": assignments[2]["branch_id"], "assignment_sha256": runner.digest(assignments[2]),
             "status": "preparation_error", "error": "missing checkpoint"},
        ]
        rows = retain_branch_results(ledger, expected_ledger_sha256=ledger["sha256"], outcomes=outcomes)
        self.assertEqual(len(rows), 4)
        self.assertEqual([row["status"] for row in rows], ["completed", "error", "preparation_error", "unstarted"])
        self.assertIsNone(rows[-1]["outcome"])
        self.assertNotIn("observed_outcomes", rows[1]["outcome"])
        for bad in (outcomes + [outcomes[0]], [{**outcomes[0], "branch_id": "not-assigned"}],
                    [{**outcomes[0], "assignment_sha256": "f" * 64}],
                    [{**outcomes[0], "assignment": assignments[1]}],
                    [{**outcomes[0], "parent_checkpoint_sha256": "f" * 64}],
                    [{**outcomes[0], "ledger_sha256": "f" * 64}]):
            with self.assertRaises(ValueError):
                retain_branch_results(ledger, expected_ledger_sha256=ledger["sha256"], outcomes=bad)


if __name__ == "__main__":
    unittest.main()
