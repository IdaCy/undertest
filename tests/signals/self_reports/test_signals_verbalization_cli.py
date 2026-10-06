import contextlib
import copy
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import signals.self_reports.run as cli
from shared.runs import run_directory
from shared.runs import write_json
import signals.validation.controls.environment as environment
import signals.self_reports.verbalization as v
from signals.validation.controls.runner import digest
from tests.signals.self_reports.test_signals_verbalization import RUBRIC
from tests.signals.self_reports.test_signals_verbalization import annotation
from tests.signals.self_reports.test_signals_verbalization import make_plan
from tests.signals.self_reports.test_signals_verbalization import response
from tests.signals.self_reports.test_signals_verbalization import trajectory


class VerbalizationCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        names = ["src/signals/self_reports/verbalization.py", "src/signals/reasoning/observations.py",
                 "src/signals/validation/controls/runner.py", "src/shared/runs.py", "src/signals/reasoning/run_monitor.py",
                 "src/signals/self_reports/run.py", "tests/signals/self_reports/test_signals_verbalization.py",
                 "tests/signals/self_reports/test_signals_verbalization_cli.py"]
        self.sources = {name: cli.file_hash(cli.ROOT / name) for name in names}
        source_patch = patch.object(cli, "source_hashes", return_value=self.sources)
        source_patch.start()
        self.addCleanup(source_patch.stop)
        network_patch = patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden in scripted tests"))
        network_patch.start()
        self.addCleanup(network_patch.stop)
        self.calls = []
        self.actor_dir, self.output = self.root / "actors", self.root / "coding"
        case = environment.make_case("ledger", "development", 0)
        assignments = [{"assignment_id": f"actor-{n}", "family": "ledger", "split": "development", "case_index": 0,
                        "case_id": case["case_id"], "repeat": n, "generation_seed": n} for n in range(2)]
        actor_config = {"section": "signals", "run_type": "development", "model": "scripted/actor", "model_revision": "actor-revision"}
        with run_directory(self.actor_dir, actor_config):
            write_json(self.actor_dir / "assignments.json", assignments)
            write_json(self.actor_dir / "cases.json", {"case": case})
            destination = self.actor_dir / "episodes" / "actor-0"
            destination.mkdir(parents=True)
            source = {**trajectory(), "assignment": assignments[0], "case_sha256": digest(case),
                      "model": "scripted/actor", "model_revision": "actor-revision"}
            write_json(destination / "trajectory.json", source)
        control = trajectory("synthetic readable control")
        control = {key: control[key] for key in ("initial_context", "tools", "messages", "retrospective_cutoff")}
        self.controls = self.root / "controls.json"
        write_json(self.controls, {"schema_version": 1, "controls": {"PROTECTED_CONTROL_NAME": control}})
        actor = cli.load_actor_run(self.actor_dir)
        inputs, controls_hash = cli.load_inputs(actor, self.controls)
        self.plan = make_plan(inputs, self.sources)
        self.plan.update(actor_snapshot=actor["snapshot"], controls_sha256=controls_hash)
        self.plan_path, self.rubric = self.root / "plan.json", self.root / "rubric.txt"
        self.rubric.write_text(RUBRIC)
        self.allocation = self.root / "allocation.json"
        write_json(self.allocation, {"scripted_external_allocation": 72})
        self.plan["allocation_sha256"] = cli.file_hash(self.allocation)
        self.freeze_plan()
        self.control_ids = [task["annotation_id"] for task in self.plan["assignments"] if task["input_kind"] == "control"]
        self.actor_id = next(task["annotation_id"] for task in self.plan["assignments"] if task["input_id"] == "actor-0")

    def freeze_plan(self):
        write_json(self.plan_path, self.plan)
        self.plan_hash = cli.file_hash(self.plan_path)
        self.args = ["--input", str(self.actor_dir), "--controls", str(self.controls), "--plan", str(self.plan_path),
                     "--plan-sha256", self.plan_hash, "--rubric", str(self.rubric), "--output", str(self.output)]

    def invoke(self, *args, injected_endpoint_factory=None, use_http=False):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            cli.main(self.args + list(args), injected_endpoint_factory=None if use_http else
                     (injected_endpoint_factory or self.fake_endpoint))
        return json.loads(stdout.getvalue())

    def fake_endpoint(self, base_url):
        self.assertEqual(base_url, "http://fixture.invalid/v1")
        def call(request):
            self.calls.append(copy.deepcopy(request))
            payload = json.loads(request["messages"][1]["content"])
            return response(annotation(payload["annotation_id"]))
        return call

    def evidence_entry(self, name, value):
        path = self.root / name
        write_json(path, value)
        return {"path": path.name, "sha256": cli.file_hash(path)}

    def receipt(self, identifier, *, context_changes=None):
        task = next(task for task in self.plan["assignments"] if task["annotation_id"] == identifier)
        plan = self.plan
        context = {"request_sha256": task["request_sha256"], "model": plan["model"], "model_revision": plan["model_revision"],
                   "service_identity_sha256": plan["service_identity_sha256"], "tokenizer_sha256": "1" * 64,
                   "chat_template_sha256": "2" * 64, "prompt_tokens": 100, "max_model_len": 4096,
                   "max_completion_tokens": 2048, "truncated": False}
        context.update(context_changes or {})
        evidence = {"allocation": {"path": self.allocation.name, "sha256": cli.file_hash(self.allocation)},
                    "readiness_review": self.evidence_entry("review.json", {"scripted_review_artifact": True}),
                    "context_preflight": self.evidence_entry(f"context-{identifier}.json", context),
                    "control_disposition": None,
                    "resource_checkpoint": self.evidence_entry("checkpoint.json", {"scripted_external_resource_check": True})}
        if task["input_kind"] == "actor":
            control_records = {}
            for control_id in self.control_ids:
                result = self.output / "annotations" / control_id / "annotation.json"
                if result.exists():
                    control_records[control_id] = json.loads(result.read_text())
            evidence["control_disposition"] = self.evidence_entry("disposition.json", {
                "plan_sha256": self.plan_hash, "control_results_sha256": digest(control_records),
                "decision": "continue", "review_sha256": "3" * 64})
        receipt = {"schema_version": 1, "plan_sha256": self.plan_hash, "annotation_id": identifier,
                   "request_sha256": task["request_sha256"], "rubric_sha256": plan["rubric_sha256"],
                   "prompt_sha256": plan["prompt_sha256"], "source_sha256": plan["source_sha256"],
                   "actor_snapshot_sha256": digest(plan["actor_snapshot"]), "controls_sha256": plan["controls_sha256"],
                   "model": plan["model"], "model_revision": plan["model_revision"],
                   "service_identity_sha256": plan["service_identity_sha256"], "generation_sha256": digest(plan["generation"]),
                   "allocation_sha256": plan["allocation_sha256"], "base_url": "http://fixture.invalid/v1", "evidence": evidence}
        path = self.root / f"receipt-{identifier}.json"
        write_json(path, receipt)
        return ["--execute-task", identifier, "--execution-receipt", str(path), "--execution-receipt-sha256", cli.file_hash(path)]

    def test_default_export_retains_every_assignment_and_sends_nothing(self):
        with patch.object(cli, "ChatEndpoint", side_effect=AssertionError("Default export must not create endpoint")):
            result = self.invoke()
        self.assertEqual(result["n_assignments"], 4)
        self.assertEqual(result["statuses"], {"pending": 3, "unavailable": 1})
        self.assertEqual(len(json.loads((self.output / "annotation_index.json").read_text())), 4)
        preview = json.loads((self.output / "previews" / f"{self.control_ids[0]}.json").read_text())
        self.assertIn("PROTECTED_CONTROL_NAME", json.dumps(preview["task"]))
        self.assertNotIn("PROTECTED_CONTROL_NAME", json.dumps(preview["request"]))
        self.assertNotIn("coder_id", json.dumps(preview["request"]))
        self.assertNotIn("actor-", json.dumps(preview["request"]))

    def test_explicit_one_call_resumes_without_retry_and_controls_precede_transcripts(self):
        self.invoke()
        with patch.object(cli, "ChatEndpoint", side_effect=self.fake_endpoint):
            args = self.receipt(self.control_ids[0])
            result = self.invoke(*args)
            self.assertEqual(result["statuses"], {"completed": 1, "pending": 2, "unavailable": 1})
            self.invoke(*args)
            self.assertEqual(len(self.calls), 1)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.invoke(*self.receipt(self.actor_id))
            self.assertEqual(len(self.calls), 1)
            self.invoke(*self.receipt(self.control_ids[1]))
            result = self.invoke(*self.receipt(self.actor_id))
        self.assertEqual(result["statuses"], {"completed": 3, "unavailable": 1})
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len({request["seed"] for request in self.calls}), 3)
        self.assertTrue((self.output / "execution_evidence" / self.actor_id / "control_disposition").exists())

    def test_truncation_overflow_wrong_render_identity_and_mutated_receipts_reject_before_call(self):
        self.invoke()
        for changes in ({"truncated": True}, {"prompt_tokens": 3000}, {"request_sha256": "0" * 64}, {"prompt_tokens": True}):
            args = self.receipt(self.control_ids[0], context_changes=changes)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.invoke(*args)
        args = self.receipt(self.control_ids[0])
        (self.root / f"receipt-{self.control_ids[0]}.json").write_text("{}")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke(*args)
        self.assertFalse((self.output / "annotations" / self.control_ids[0] / "attempt.json").exists())

    def test_interrupt_after_registration_is_retained_without_automatic_replacement(self):
        self.invoke()
        def interrupted(_):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.invoke(*self.receipt(self.control_ids[0]), injected_endpoint_factory=lambda _: interrupted)
        result = self.invoke()
        self.assertEqual(result["statuses"], {"interrupted": 1, "pending": 2, "unavailable": 1})
        with patch.object(cli, "ChatEndpoint", side_effect=AssertionError("Interrupted task must not retry")):
            self.invoke(*self.receipt(self.control_ids[0]))

    def test_omitted_assignment_actor_mutation_and_source_mutation_reject(self):
        original = copy.deepcopy(self.plan)
        self.plan["assignments"] = [task for task in self.plan["assignments"] if task["input_id"] != "actor-1"]
        self.plan["seed_table_sha256"] = digest([{key: task[key] for key in ("annotation_id", "coder_id", "seed")} for task in self.plan["assignments"]])
        self.freeze_plan()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke()

        self.plan = original
        self.freeze_plan()
        with patch.object(cli, "source_hashes", return_value={}), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke()
        path = self.actor_dir / "episodes" / "actor-0" / "trajectory.json"
        changed = json.loads(path.read_text())
        changed["messages"][0]["reasoning_content"] = "changed saved reasoning"
        write_json(path, changed)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke()

    def test_confirmation_ledger_is_rejected_before_trajectory_loading(self):
        path = self.actor_dir / "assignments.json"
        assignments = json.loads(path.read_text())
        assignments[0]["split"] = "confirmation"
        write_json(path, assignments)
        with patch.object(cli, "load_actor_run", side_effect=AssertionError("No confirmation contents may be opened")):
            with self.assertRaises(ValueError):
                cli.load_development_actor(self.actor_dir)

    def assert_resume_rejects_without_call_or_index_rewrite(self, *args):
        index = (self.output / "annotation_index.json").read_bytes()
        count = len(self.calls)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.invoke(*args)
        self.assertEqual(len(self.calls), count)
        self.assertEqual((self.output / "annotation_index.json").read_bytes(), index)

    def test_completed_slot_deletion_or_emptying_never_repeats_frozen_request(self):
        self.invoke()
        identifier = self.control_ids[0]
        args = self.receipt(identifier)
        self.invoke(*args)
        journal = (self.output / "consumption.json").read_bytes()
        slot = self.output / "annotations" / identifier
        shutil.rmtree(slot)
        self.assert_resume_rejects_without_call_or_index_rewrite(*args)
        self.assertFalse(slot.exists())
        slot.mkdir()
        self.assert_resume_rejects_without_call_or_index_rewrite(*args)
        self.assertEqual(list(slot.iterdir()), [])
        self.assertEqual((self.output / "consumption.json").read_bytes(), journal)
        self.assertEqual(len(self.calls), 1)

    def test_interrupted_slot_deletion_preserves_consumption_before_index_update(self):
        self.invoke()
        identifier = self.control_ids[0]
        args = self.receipt(identifier)
        before = (self.output / "annotation_index.json").read_bytes()
        def interrupted(request):
            self.calls.append(request)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.invoke(*args, injected_endpoint_factory=lambda _: interrupted)
        self.assertEqual((self.output / "annotation_index.json").read_bytes(), before)
        journal = json.loads((self.output / "consumption.json").read_text())
        self.assertIsNotNone(journal["consumed"][identifier])
        self.assertEqual(set(journal["identities"]), {task["annotation_id"] for task in self.plan["assignments"]})
        shutil.rmtree(self.output / "annotations" / identifier)
        self.assert_resume_rejects_without_call_or_index_rewrite(*args)
        (self.output / "annotations" / identifier).mkdir()
        self.assert_resume_rejects_without_call_or_index_rewrite(*args)
        self.assertEqual(len(self.calls), 1)

    def test_crash_after_durable_consumption_before_slot_write_does_not_retry(self):
        self.invoke()
        identifier = self.control_ids[0]
        args = self.receipt(identifier)
        original_write = v._durable_bytes
        def crash(path, body):
            if path.name == "attempt.json":
                raise KeyboardInterrupt()
            return original_write(path, body)
        with patch.object(v, "_durable_bytes", side_effect=crash), self.assertRaises(KeyboardInterrupt):
            self.invoke(*args)
        self.assertEqual(len(self.calls), 0)
        journal = json.loads((self.output / "consumption.json").read_text())
        self.assertIsNotNone(journal["consumed"][identifier])
        self.assert_resume_rejects_without_call_or_index_rewrite(*args)

    def test_missing_run_journal_is_not_rebuilt_from_completed_slot_or_index(self):
        self.invoke()
        identifier = self.control_ids[0]
        args = self.receipt(identifier)
        self.invoke(*args)
        (self.output / "consumption.json").unlink()
        self.assert_resume_rejects_without_call_or_index_rewrite(*args)
        self.assertFalse((self.output / "consumption.json").exists())

    def test_every_execution_artifact_and_attempt_receipt_is_required_on_resume(self):
        self.invoke()
        identifier = self.control_ids[0]
        args = self.receipt(identifier)
        self.invoke(*args)
        evidence = self.output / "execution_evidence" / identifier
        for path in sorted(evidence.iterdir()):
            original = path.read_bytes()
            path.unlink()
            self.assert_resume_rejects_without_call_or_index_rewrite()
            path.write_bytes(original + b"changed")
            self.assert_resume_rejects_without_call_or_index_rewrite()
            path.write_bytes(original)
        attempt_path = self.output / "annotations" / identifier / "attempt.json"
        original = attempt_path.read_bytes()
        attempt = json.loads(original)
        attempt["execution_receipt"] = {"bound_receipt": "CHANGED"}
        write_json(attempt_path, attempt)
        self.assert_resume_rejects_without_call_or_index_rewrite()
        attempt_path.write_bytes(original)
        shutil.rmtree(evidence)
        self.assert_resume_rejects_without_call_or_index_rewrite()
        self.assertEqual(len(self.calls), 1)

    def test_archived_receipt_is_self_contained_and_missing_index_recovers_without_call(self):
        self.invoke()
        identifier = self.control_ids[0]
        args = self.receipt(identifier)
        self.invoke(*args)
        receipt_path = self.root / f"receipt-{identifier}.json"
        receipt = json.loads(receipt_path.read_text())
        for entry in receipt["evidence"].values():
            if entry is not None:
                (self.root / entry["path"]).unlink()
        receipt_path.unlink()
        (self.output / "annotation_index.json").unlink()
        result = self.invoke(*args)
        self.assertEqual(result["statuses"], {"completed": 1, "pending": 2, "unavailable": 1})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(json.loads((self.output / "annotation_index.json").read_text())), 4)

    def test_both_raw_http_files_required_for_completed_and_malformed_calls(self):
        self.invoke()
        for identifier, raw, status in ((self.control_ids[0], json.dumps(response(annotation(self.control_ids[0]))).encode(), "completed"),
                                         (self.control_ids[1], b' {broken HTTP JSON', "error")):
            args = self.receipt(identifier)
            fake = io.BytesIO(raw)
            fake.status = 200
            with patch("urllib.request.urlopen", return_value=fake) as http:
                result = self.invoke(*args, use_http=True)
            self.assertEqual(http.call_count, 1)
            self.assertGreaterEqual(result["statuses"][status], 1)
            slot = self.output / "annotations" / identifier
            saved = {name: (slot / name).read_bytes() for name in ("response.body", "response.http.json")}
            attempt = json.loads((slot / "attempt.json").read_text())
            self.assertEqual(attempt["call_kind"], "chat_endpoint")
            for name in saved:
                (slot / name).unlink()
            self.assert_resume_rejects_without_call_or_index_rewrite(*args)
            for name, body in saved.items():
                (slot / name).write_bytes(body)
        self.assertEqual(self.invoke()["statuses"], {"completed": 1, "error": 1, "pending": 1, "unavailable": 1})

    def test_interrupted_http_call_requires_its_retained_transport_pair(self):
        self.invoke()
        identifier = self.control_ids[0]
        args = self.receipt(identifier)
        with patch("urllib.request.urlopen", side_effect=KeyboardInterrupt()) as http, self.assertRaises(KeyboardInterrupt):
            self.invoke(*args, use_http=True)
        self.assertEqual(http.call_count, 1)
        result = self.invoke()
        self.assertEqual(result["statuses"], {"interrupted": 1, "pending": 2, "unavailable": 1})
        slot = self.output / "annotations" / identifier
        (slot / "response.body").unlink()
        (slot / "response.http.json").unlink()
        self.assert_resume_rejects_without_call_or_index_rewrite(*args)


if __name__ == "__main__":
    unittest.main()
