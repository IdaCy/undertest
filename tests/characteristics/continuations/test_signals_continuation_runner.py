import copy
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.runs import run_directory
from shared.runs import write_json
import characteristics.continuations.continuations as continuations
import signals.validation.controls.environment as environment
import signals.validation.controls.runner as runner


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("characteristics.continuations.run", ROOT / "src/characteristics/continuations/run.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def answer(name=None, artifact=None, *, tokens=5, finish=None):
    message = {"role": "assistant", "content": None if name else "Done.",
               "reasoning": "I should inspect before deciding what to submit."}
    if name:
        message["tool_calls"] = [{"id": "call-a", "type": "function",
                                  "function": {"name": name, "arguments": json.dumps({"artifact": artifact} if artifact is not None else {})}}]
    return {"choices": [{"message": message, "finish_reason": finish or ("tool_calls" if name else "stop")}],
            "usage": {"prompt_tokens": 40, "completion_tokens": tokens}}


def expected_identity(record):
    payload = record["payload"]
    result = {"checkpoint_sha256": record["sha256"]}
    result.update({f"{key}_sha256": runner.digest(payload[key]) for key in ("case", "assignment", "initial_context", "tools")})
    result.update({key: copy.deepcopy(payload[key]) for key in
                   ("model", "model_revision", "generation", "limits", "seed_derivation_version")})
    result["environment_source_sha256"] = hashlib.sha256(Path(environment.__file__).read_bytes()).hexdigest()
    result["restoration_runner_source_sha256"] = hashlib.sha256(Path(runner.__file__).read_bytes()).hexdigest()
    return result


class ContinuationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = environment.make_case("ledger", "development", 0)
        self.assignment = runner.assignment_ledger(["ledger"], "development", 1, 1, 102)[0]
        self.assignment["case_id"] = self.case["case_id"]
        self.parent_dir = self.root / "parent"
        self.sources = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in
                        (ROOT / "src/signals/validation/controls/runner.py", ROOT / "src/signals/validation/controls/environment.py",
                         ROOT / "src/characteristics/continuations/continuations.py", ROOT / "src/characteristics/continuations/run.py")}
        config = {"section": "signals", "run_type": "smoke", "model": "fake/model", "model_revision": "exact-revision",
                  "generation": {"temperature": 1, "chat_template_kwargs": {"enable_thinking": True}},
                  "seed_derivation_version": runner.SEED_DERIVATION_VERSION, "source_sha256": self.sources,
                  "max_calls": 4, "max_completion_tokens": 25, "per_call_tokens": 10}
        self.parent_output = self.parent_dir / "episodes" / self.assignment["assignment_id"]
        with run_directory(self.parent_dir, config):
            cli.archive_sources(self.parent_dir, self.sources)
            write_json(self.parent_dir / "assignments.json", [self.assignment])
            script = iter([answer("inspect_artifact", tokens=10), answer()])
            runner.run_episode(case=self.case, assignment=self.assignment, output=self.parent_output,
                               model=config["model"], model_revision=config["model_revision"], generation=config["generation"],
                               call=lambda request: next(script), environment=environment, max_calls=4,
                               max_completion_tokens=25, per_call_tokens=10)
        self.checkpoint = json.loads((self.parent_output / "checkpoint.json").read_text())
        self.expected = expected_identity(self.checkpoint)

    def ledger(self, explicit=False):
        rows = []
        for condition, replacement in (("unchanged", None), ("sham", "First I should"),
                                       ("criterion_text", "The task says to"), ("reasoning_text", "I will")):
            rows.append({"branch_id": condition, "parent_checkpoint_sha256": self.checkpoint["sha256"],
                         "comparison_id": "comparison", "replicate_id": "r0", "condition": condition,
                         "seed_mode": "explicit" if explicit else "original",
                         "request_seeds": [101, 102, 103] if explicit else None,
                         "edits": [] if replacement is None else [{"path": ["messages", 0, "reasoning"],
                            "start": 0, "end": 8, "before": "I should", "after": replacement}]})
        return continuations.freeze_branch_ledger(rows, protocol_sha256="a" * 64, selection_sha256="b" * 64)

    def branches(self, ledger=None):
        ledger = ledger or self.ledger()
        parent_hash = self.checkpoint["sha256"]
        return continuations.prepare_branches(ledger, expected_ledger_sha256=ledger["sha256"],
                                              parents={parent_hash: self.checkpoint},
                                              parent_expectations={parent_hash: self.expected}, environment=environment)

    def run_branch(self, answers, *, branch=None, name="continuation"):
        script, requests = iter(answers), []

        def call(request):
            requests.append(copy.deepcopy(request))
            response = next(script)
            if isinstance(response, BaseException):
                raise response
            return response

        branch = branch or self.branches()[0]
        record = runner.run_continuation(branch=branch, output=self.root / name, call=call, environment=environment)
        return record, requests

    def descriptor(self, path):
        body = path.read_bytes()
        return {"path": str(path), "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}

    def artifact(self, name, value, *, raw=False):
        path = self.root / name
        if raw:
            path.write_text(value)
        else:
            write_json(path, value)
        return self.descriptor(path)

    def render_entry(self, name, payload, request):
        normalized = cli.normalized_conversation(request)
        # Deliberately synthetic renderer/tokenizer for no-network gate fixtures.
        rendered = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
        token_ids = list(rendered.encode())
        return {"payload_sha256": runner.digest(payload), "request_sha256": runner.digest(request),
                "canonical_reasoning_paths": cli.canonical_reasoning_paths(payload),
                "normalized_messages": self.artifact(name + "-normalized.json", normalized),
                "rendered_text": self.artifact(name + "-render.txt", rendered, raw=True),
                "token_ids": self.artifact(name + "-tokens.json", token_ids),
                "token_ids_sha256": runner.digest(token_ids)}

    def make_plan(self, *, ledger=None, mutate_receipt=None):
        ledger = ledger or self.ledger()
        branches = self.branches(ledger)
        parent_hash = self.checkpoint["sha256"]
        serving = {"base_url": "http://localhost/v1", "model": "fake/model", "model_revision": "exact-revision",
                   "context_tokens": 65536, "renderer_profile": cli.RENDERER_PROFILE,
                   "tokenizer_files_sha256": {"tokenizer.json": "c" * 64}, "chat_template_sha256": "d" * 64,
                   "normalizer_source_sha256": {"chat_utils.py": "e" * 64},
                   "serving_stack_sha256": "f" * 64, "acceptance_sha256": "0" * 64}
        receipt = {"schema_version": cli.RENDER_SCHEMA, "ledger_sha256": ledger["sha256"],
                   "serving_identity_sha256": runner.digest(serving), "source_sha256": self.sources,
                   "renderer_profile": cli.RENDERER_PROFILE,
                   "parents": {parent_hash: self.render_entry("parent", self.checkpoint["payload"], cli._parent_request(self.checkpoint["payload"]))},
                   "branches": {branch["branch_id"]: self.render_entry(branch["branch_id"], branch["resume_payload"],
                                    runner.continuation_request(branch, environment=environment)) for branch in branches}}
        if mutate_receipt:
            mutate_receipt(receipt)
        receipt_descriptor = self.artifact("render-receipt.json", receipt)
        review = {"schema_version": cli.REVIEW_SCHEMA, "receipt_file_sha256": receipt_descriptor["sha256"],
                  "ledger_sha256": ledger["sha256"], "serving_identity_sha256": runner.digest(serving),
                  "decision": "accepted", "reviewer_ids": ["synthetic-unit-fixture"], "review_report_sha256": "1" * 64}
        plan = {"schema_version": cli.PLAN_SCHEMA, "protocol_sha256": "a" * 64, "selection_sha256": "b" * 64,
                "ledger": ledger, "source_sha256": self.sources, "serving_identity": serving,
                "parents": {parent_hash: {"checkpoint": self.descriptor(self.parent_output / "checkpoint.json"),
                    "expected": self.expected, "manifest": self.descriptor(self.parent_dir / "manifest.json"),
                    "assignments": self.descriptor(self.parent_dir / "assignments.json"),
                    "source_archive": self.descriptor(self.parent_dir / "source.zip")}},
                "render_receipt": receipt_descriptor, "render_review": self.artifact("render-review.json", review)}
        descriptor = self.artifact("plan.json", plan)
        return Path(descriptor["path"]), descriptor["sha256"]

    def load_plan(self, path_hash):
        return cli.load_plan(*path_hash, self.sources)

    def test_resume_uses_inherited_state_usage_tools_cutoff_and_exact_next_request(self):
        branch = self.branches()[3]
        record, requests = self.run_branch([answer("submit_artifact", self.case["controls"]["correct"], tokens=10), answer(tokens=5)], branch=branch)
        self.assertEqual(requests[0], runner.continuation_request(branch, environment=environment))
        self.assertEqual([request["max_tokens"] for request in requests], [10, 5])
        self.assertTrue(all(request["tool_choice"] == "auto" and len(request["tools"]) == 3 for request in requests))
        self.assertTrue(requests[0]["messages"][2]["reasoning"].startswith("I will"))
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["usage"], {"prompt_tokens": 120, "completion_tokens": 25})
        self.assertEqual(record["continuation"]["inherited_usage"], {"prompt_tokens": 40, "completion_tokens": 10})
        self.assertEqual(record["continuation"]["new_usage"], {"prompt_tokens": 80, "completion_tokens": 15})
        self.assertEqual(record["completed_calls"], 3)
        self.assertEqual(record["continuation"]["new_completed_calls"], 2)
        self.assertEqual(record["prospective_cutoff"], 2)
        self.assertEqual(record["state"]["audit_log"][0], self.checkpoint["payload"]["state"]["audit_log"][0])
        self.assertEqual(record["state"]["tool_calls"], 2)
        self.assertEqual(record["continuation"]["new_tool_calls"], 1)
        self.assertFalse((self.root / "continuation/request-000.json").exists())
        self.assertFalse((self.root / "continuation/checkpoint.json").exists())
        self.assertTrue(json.loads((self.root / "continuation/labels.json").read_text())["observed_outcomes"]["task_success"])
        self.assertIsNone(self.checkpoint["payload"]["state"]["artifact"])

    def test_explicit_branch_seed_schedule_retains_original_seed_history(self):
        branch = self.branches(self.ledger(explicit=True))[0]
        record, requests = self.run_branch([answer("submit_artifact", self.case["controls"]["correct"]), answer()], branch=branch)
        self.assertEqual([request["seed"] for request in requests], [101, 102])
        self.assertEqual(record["continuation"]["branch_request_seeds"], [101, 102, 103])
        self.assertEqual(record["continuation"]["original_request_seeds"], [runner.request_seed(self.assignment, i) for i in (1, 2, 3)])
        self.assertEqual([row["generation_seed"] for row in record["calls"]], [101, 102])

    def test_inherited_token_and_call_limits_stop_without_extra_calls(self):
        record, requests = self.run_branch([answer("inspect_artifact", tokens=10), answer("inspect_artifact", tokens=5)])
        self.assertEqual(record["status"], "token_limit")
        self.assertEqual(len(requests), 2)
        self.assertEqual(record["usage"]["completion_tokens"], 25)
        record, requests = self.run_branch([answer("inspect_artifact", tokens=1)] * 3, name="call-limit")
        self.assertEqual(record["status"], "call_limit")
        self.assertEqual(len(requests), 3)
        self.assertEqual(record["state"]["tool_calls"], 4)

    def test_truncation_or_invalid_usage_never_executes_the_returned_submission(self):
        truncated = answer("submit_artifact", self.case["controls"]["shortcut"], finish="length")
        record, _ = self.run_branch([truncated])
        self.assertEqual(record["status"], "generation_limit")
        self.assertEqual(record["state"], self.checkpoint["payload"]["state"])
        invalid = answer("submit_artifact", self.case["controls"]["shortcut"])
        invalid["usage"].pop("completion_tokens")
        record, _ = self.run_branch([invalid], name="invalid-usage")
        self.assertEqual(record["status"], "error")
        self.assertFalse(record["continuation"]["new_usage_complete"])
        self.assertEqual(record["state"], self.checkpoint["payload"]["state"])
        record, _ = self.run_branch([answer("submit_artifact", self.case["controls"]["shortcut"], tokens=11)], name="excess-usage")
        self.assertEqual(record["status"], "error")
        self.assertEqual(record["state"], self.checkpoint["payload"]["state"])

    def test_transport_failure_retains_raw_partial_body_and_positive_history(self):
        success = io.BytesIO(json.dumps(answer("submit_artifact", self.case["controls"]["shortcut"])).encode())
        success.status = 200

        class Partial(io.BytesIO):
            status = 200

            def read1(self, size=-1):
                if self.tell():
                    raise TimeoutError("fixture timeout")
                return super().read(size)

        endpoint = runner.ChatEndpoint("http://localhost/v1")
        output = self.root / "partial"
        with patch("urllib.request.urlopen", side_effect=[success, Partial(b"partial evidence")]):
            record = runner.run_continuation(branch=self.branches()[0], output=output, call=endpoint, environment=environment)
        self.assertEqual(record["status"], "error")
        self.assertFalse(record["continuation"]["new_usage_complete"])
        self.assertEqual(record["continuation"]["new_usage"]["completion_tokens"], 5)
        self.assertEqual((output / "response-002.body").read_bytes(), b"partial evidence")
        labels = json.loads((output / "labels.json").read_text())
        self.assertTrue(labels["observed_outcomes"]["shortcut_attempt"])
        self.assertFalse(labels["complete_episode"])

    def test_changed_preparation_rejects_before_creating_attempt_or_calling(self):
        for mutation in ("seed", "payload", "budget"):
            branch = self.branches()[0]
            if mutation == "seed":
                branch["request_seeds"][0] = 0
            elif mutation == "payload":
                branch["resume_payload"]["messages"][0]["reasoning"] = "changed"
            else:
                branch["remaining_completion_tokens"] += 1
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                runner.run_continuation(branch=branch, output=self.root / mutation,
                                        call=lambda request: self.fail("must not call"), environment=environment)
            self.assertFalse((self.root / mutation).exists())

    def test_render_gate_binds_exact_normalization_and_rejects_noop_or_changed_receipts(self):
        loaded = self.load_plan(self.make_plan())
        self.assertEqual(cli.apply_render_gate(loaded)["ready"], 4)
        self.assertTrue(all("render_gate" in branch for branch in loaded["prepared"]))

        def noop(receipt):
            baseline = receipt["parents"][self.checkpoint["sha256"]]
            edited = receipt["branches"]["reasoning_text"]
            for key in ("rendered_text", "token_ids", "token_ids_sha256"):
                edited[key] = copy.deepcopy(baseline[key])

        loaded = self.load_plan(self.make_plan(mutate_receipt=noop))
        self.assertEqual(cli.apply_render_gate(loaded)["ready"], 3)
        self.assertIn("no-op", loaded["prepared"][3]["error"]["message"])
        loaded = self.load_plan(self.make_plan())
        (self.root / "reasoning_text-normalized.json").write_text("[]")
        self.assertEqual(cli.apply_render_gate(loaded)["ready"], 3)
        self.assertEqual(loaded["prepared"][3]["status"], "preparation_error")

    def test_alias_conflicts_and_shadowed_fields_fail_before_normalization(self):
        payload = copy.deepcopy(self.checkpoint["payload"])
        payload["messages"][0]["reasoning_content"] = "contradictory alias"
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            cli.canonical_reasoning_paths(payload)
        request = cli._parent_request(payload)
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            cli.normalized_conversation(request)
        payload["messages"][0]["reasoning_content"] = payload["messages"][0]["reasoning"]
        self.assertEqual(cli.canonical_reasoning_paths(payload), [["messages", 0, "reasoning"]])

    def test_parent_artifact_failure_retains_all_assigned_preparation_errors(self):
        path_hash = self.make_plan()
        with (self.parent_output / "checkpoint.json").open("a") as stream:
            stream.write(" ")
        loaded = self.load_plan(path_hash)
        self.assertEqual(len(loaded["prepared"]), 4)
        self.assertTrue(all(branch["status"] == "preparation_error" for branch in loaded["prepared"]))

    def test_full_cli_runs_once_and_retains_frozen_archives_and_branch_denominators(self):
        path, sha = self.make_plan()
        output = self.root / "continuation-cli"
        arguments = ["signals_continue.py", "--plan", str(path), "--plan-sha256", sha, "--output", str(output)]
        with patch.object(cli, "source_hashes", return_value=self.sources), patch.object(sys, "argv", arguments):
            cli.main()
        before = json.loads((output / "branch_index.json").read_text())
        self.assertEqual([row["status"] for row in before], ["unstarted"] * 4)
        requests = []

        def call(request):
            requests.append(copy.deepcopy(request))
            return answer()

        with patch.object(cli, "source_hashes", return_value=self.sources), patch.object(sys, "argv", arguments + ["--execute"]), patch.object(runner.ChatEndpoint, "__call__", side_effect=call):
            cli.main()
            cli.main()
        self.assertEqual(len(requests), 4)
        index = json.loads((output / "branch_index.json").read_text())
        self.assertEqual(len(index), 4)
        self.assertTrue(all(row["status"] == "completed" for row in index))
        self.assertTrue((output / "source.zip").is_file())
        self.assertTrue((output / "input_artifacts.json").is_file())
        self.assertTrue(all((output / "branches" / row["branch_id"] / "request-001.json").is_file() for row in index))
        self.assertFalse(any("private" in request or "observed_outcomes" in request for request in requests))

    def test_interrupted_assignment_is_retained_and_remaining_unstarted_branches_continue(self):
        loaded = self.load_plan(self.make_plan())
        cli.apply_render_gate(loaded)
        output = self.root / "interrupted-run"
        output.mkdir()
        with self.assertRaises(KeyboardInterrupt):
            cli.run_branches(loaded, output, execute=True, call=lambda request: (_ for _ in ()).throw(KeyboardInterrupt()))
        index = json.loads((output / "branch_index.json").read_text())
        self.assertEqual([row["status"] for row in index], ["unstarted"] * 4)
        requests = []
        index = cli.run_branches(loaded, output, execute=True, call=lambda request: requests.append(request) or answer())
        self.assertEqual([row["status"] for row in index], ["interrupted", "completed", "completed", "completed"])
        self.assertEqual(len(requests), 3)
        self.assertTrue((output / "branches/unchanged/interrupted.json").is_file())

    def test_empty_crash_directory_is_interrupted_without_a_silent_retry(self):
        loaded = self.load_plan(self.make_plan())
        cli.apply_render_gate(loaded)
        output = self.root / "empty-crash"
        (output / "branches/unchanged").mkdir(parents=True)
        requests = []
        index = cli.run_branches(loaded, output, execute=True, call=lambda request: requests.append(request) or answer())
        self.assertEqual(len(requests), 3)
        self.assertEqual(index[0]["status"], "interrupted")
        self.assertTrue(index[0]["outcome"]["missing_preparation_at_recovery"])
        cli.run_branches(loaded, output, execute=True, call=lambda request: self.fail("must not retry"))

    def test_terminal_artifact_or_summary_tampering_is_detected_before_any_new_call(self):
        loaded = self.load_plan(self.make_plan())
        cli.apply_render_gate(loaded)
        output = self.root / "tampering"
        output.mkdir()
        cli.run_branches(loaded, output, execute=True, call=lambda request: answer())
        target = output / "branches/unchanged"
        response_path = target / "response-001.json"
        original_response = response_path.read_bytes()
        response_path.write_bytes(original_response + b" ")
        with self.assertRaisesRegex(ValueError, "evidence files changed"):
            cli.run_branches(loaded, output, execute=True, call=lambda request: self.fail("must not call"))
        response_path.write_bytes(original_response)
        result_path = target / "result.json"
        result = json.loads(result_path.read_text())
        result["observed_outcomes"]["shortcut_attempt"] = True
        write_json(result_path, result)
        with self.assertRaisesRegex(ValueError, "differs from its evidence"):
            cli.run_branches(loaded, output, execute=True, call=lambda request: self.fail("must not call"))

    def test_unfinished_render_review_and_changed_frozen_sources_block_execution(self):
        path, _ = self.make_plan()
        plan = json.loads(path.read_text())
        review_path = Path(plan["render_review"]["path"])
        review = json.loads(review_path.read_text())
        review["decision"] = "changes_required"
        write_json(review_path, review)
        plan["render_review"] = self.descriptor(review_path)
        write_json(path, plan)
        loaded = self.load_plan((path, hashlib.sha256(path.read_bytes()).hexdigest()))
        self.assertEqual(cli.apply_render_gate(loaded)["status"], "error")
        self.assertTrue(all(branch["status"] == "preparation_error" for branch in loaded["prepared"]))
        changed_sources = {**self.sources, "src/signals/validation/controls/runner.py": "f" * 64}
        with self.assertRaisesRegex(ValueError, "source hashes differ"):
            cli.load_plan(path, hashlib.sha256(path.read_bytes()).hexdigest(), changed_sources)

    def test_missing_render_review_blocks_all_model_calls_and_retains_assignments(self):
        path, _ = self.make_plan()
        plan = json.loads(path.read_text())
        plan["render_review"] = None
        write_json(path, plan)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        output = self.root / "no-review"
        arguments = ["signals_continue.py", "--plan", str(path), "--plan-sha256", sha, "--output", str(output), "--execute"]
        with patch.object(cli, "source_hashes", return_value=self.sources), patch.object(sys, "argv", arguments), patch.object(runner.ChatEndpoint, "__call__", side_effect=AssertionError("must not call")), self.assertRaises(SystemExit) as raised:
            cli.main()
        self.assertEqual(raised.exception.code, 2)
        index = json.loads((output / "branch_index.json").read_text())
        self.assertEqual(len(index), 4)
        self.assertTrue(all(row["status"] == "preparation_error" for row in index))


if __name__ == "__main__":
    unittest.main()
