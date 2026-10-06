import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import signals.validation.controls.run as actors
import signals.reasoning.run_monitor as monitors
import signals.validation.controls.environment as environment
import signals.reasoning.monitor as monitor
import signals.validation.controls.runner as runner


class InterfaceIntegrationTests(unittest.TestCase):
    def test_v2_confirmation_rejects_between_phase_source_drift(self):
        sources = monitors.source_hashes()
        freeze = {"monitor_model": "fake/model", "monitor_model_revision": "r1",
                  "monitor_cutoffs": ["retrospective", "prospective"],
                  "monitor_sha256": monitor.prompt_hash(monitor.MONITOR_VERSION_V2),
                  "monitor_identity": monitor.monitor_identity(monitor.MONITOR_VERSION_V2),
                  "source_sha256": sources}
        actor = {"assignments": [{"split": "confirmation"}], "manifest": {
            "monitor_version": monitor.MONITOR_VERSION_V2, "freeze": freeze}}
        def check(current):
            monitors.validate_confirmation_monitor(actor, "fake/model", "r1",
                ["retrospective", "prospective"], current, monitor.MONITOR_VERSION_V2)
        check(dict(sources))
        names = ("src/signals/reasoning/monitor.py", "src/signals/reasoning/observations.py",
                 "src/signals/validation/controls/environment.py", "src/signals/validation/controls/runner.py",
                 "src/signals/validation/controls/finite_execution.py", "src/signals/reasoning/run_monitor.py",
                 "src/signals/validation/controls/analyze.py", "src/signals/validation/controls/analysis.py",
                 "src/signals/validation/calibration.py")
        for name in names:
            with self.subTest(source=name):
                changed = {**sources, name: "0" * 64}
                with self.assertRaisesRegex(ValueError, "source differs"):
                    check(changed)
                missing = dict(sources)
                del missing[name]
                with self.assertRaisesRegex(ValueError, "source differs"):
                    check(missing)
                freeze["source_sha256"] = missing
                with self.assertRaisesRegex(ValueError, "lacks an exact"):
                    check(missing)
                freeze["source_sha256"] = sources

    def test_explicit_v2_offline_previews_match_actual_actor_and_all_monitor_views(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch("urllib.request.urlopen", side_effect=AssertionError("No network permitted")), \
                contextlib.redirect_stdout(io.StringIO()):
            root = Path(temporary)
            actor_dir = root / "actors"
            actors.main(["--output", str(actor_dir), "--families", "ledger", "--instances", "1", "--repeats", "1",
                "--model", "fake/model", "--model-revision", "fixture-revision", "--max-calls", "8",
                "--per-call-tokens", "512", "--max-completion-tokens", "1400",
                "--public-interface-version", environment.PUBLIC_INTERFACE_V2, "--monitor-version", monitor.MONITOR_VERSION_V2])
            manifest = monitors.read_json(actor_dir / "manifest.json")
            assignment = monitors.read_json(actor_dir / "assignments.json")[0]
            case = next(iter(monitors.read_json(actor_dir / "cases.json").values()))
            preview = monitors.read_json(actor_dir / "preview_requests" / f"{assignment['assignment_id']}.json")
            self.assertEqual(manifest["public_interface_version"], environment.PUBLIC_INTERFACE_V2)
            self.assertEqual(manifest["monitor_version"], monitor.MONITOR_VERSION_V2)
            self.assertEqual(preview["artifact_schema_sha256"], runner.digest(case["public"]["artifact_schema"]))
            public = json.loads(preview["initial_context"][1]["content"])
            self.assertEqual(public["model_response_budget"]["max_calls"], 8)
            calls = []

            def scripted(request):
                calls.append(request)
                message = {"role": "assistant", "content": "Done."}
                finish = "stop"
                if len(calls) == 1:
                    message["tool_calls"] = [{"id": "inspect", "type": "function",
                                              "function": {"name": "inspect_artifact", "arguments": "{}"}}]
                    finish = "tool_calls"
                return {"choices": [{"message": message, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 10}}

            runner.run_episode(case=case, assignment=assignment,
                output=actor_dir / "episodes" / assignment["assignment_id"], model=manifest["model"],
                model_revision=manifest["model_revision"], generation=manifest["generation"], call=scripted,
                environment=environment, max_calls=8, per_call_tokens=512, max_completion_tokens=1400)
            self.assertEqual(preview["request"], calls[0])
            monitor_dir = root / "monitors"
            monitors.main(["--input", str(actor_dir), "--output", str(monitor_dir),
                           "--model", "fake/monitor", "--monitor-version", monitor.MONITOR_VERSION_V2])
            monitor_manifest = monitors.read_json(monitor_dir / "manifest.json")
            for key, value in monitor.monitor_identity(monitor.MONITOR_VERSION_V2).items():
                self.assertEqual(monitor_manifest[key], value)
            tasks = monitors.read_json(monitor_dir / "monitor_assignments.json")
            self.assertEqual(len(tasks), 8)
            self.assertFalse(list((monitor_dir / "attempts").iterdir()))
            for task in tasks:
                self.assertEqual(task["monitor_identity"], monitor.monitor_identity(monitor.MONITOR_VERSION_V2))
                request = monitors.read_json(monitor_dir / "preview_requests" / f"{task['monitor_assignment_id']}.json")["request"]
                self.assertEqual(request["response_format"], monitor.response_format(monitor.MONITOR_VERSION_V2))
                record = json.loads(request["messages"][1]["content"])["record"]
                self.assertEqual(record["initial_context"], preview["initial_context"])
                self.assertEqual(record["tools"], preview["tools"])
                self.assertNotIn("execution_certificate_sha256", record)
            (actor_dir / "episodes" / assignment["assignment_id"] / "checkpoint.json").unlink()
            actor = monitors.load_actor_run(actor_dir)
            retained, new_previews = monitors.build_plan(actor, ["retrospective", "prospective"], "fake/monitor",
                                                        monitor.MONITOR_VERSION_V2)
            self.assertEqual(len(retained), 8)
            self.assertEqual(len(new_previews), 4)
            self.assertTrue(all(task["availability"] == "unavailable" and task["reason"] == "missing_checkpoint"
                                for task in retained if task["cutoff_name"] == "prospective"))
            request_file = actor_dir / "episodes" / assignment["assignment_id"] / "request-000.json"
            bad = monitors.read_json(request_file)
            bad["max_tokens"] += 1
            request_file.write_text(json.dumps(bad))
            with self.assertRaisesRegex(ValueError, "request"):
                monitors.build_plan(actor, ["retrospective"], "fake/monitor", monitor.MONITOR_VERSION_V2)

    def test_default_actor_selector_keeps_legacy_case_and_rendering(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch("urllib.request.urlopen", side_effect=AssertionError("No network permitted")), \
                contextlib.redirect_stdout(io.StringIO()):
            output = Path(temporary) / "legacy"
            actors.main(["--output", str(output), "--families", "validator", "--instances", "1", "--repeats", "1"])
            case = next(iter(monitors.read_json(output / "cases.json").values()))
            self.assertEqual(case, environment.make_case("validator", "development", 0))
            self.assertFalse((output / "preview_requests").exists())
            self.assertEqual(monitors.read_json(output / "manifest.json")["monitor_version"], monitor.MONITOR_VERSION_V1)


if __name__ == "__main__":
    unittest.main()
