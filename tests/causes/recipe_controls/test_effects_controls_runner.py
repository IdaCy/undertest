import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from inspect_ai import eval_set
from inspect_ai.model import ModelOutput, ModelUsage, get_model

from causes.recipe_controls.controls.design import build_control_design
from causes.recipe_controls.tasks.recipe import contract_for
from causes.recipe_controls.tasks.recipe import reference_recipe


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("control_runner_test", ROOT / "src/causes/recipe_controls/effects_controls.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class ControlRunnerTests(unittest.TestCase):
    def test_execution_metadata_and_exports_with_actual_offline_inspect(self):
        recipe = json.dumps(reference_recipe(contract_for("ledger-dev-0")))
        calls = []

        def respond(messages, tools, tool_choice, config):
            calls.append(len(messages))
            if not any(message.role == "tool" for message in messages):
                output = ModelOutput.for_tool_call("mockllm/control-runner", "submit_recipe", {"recipe": recipe})
            else:
                output = ModelOutput.from_content("mockllm/control-runner", "Canned offline report.")
            output.usage = ModelUsage(input_tokens=100, output_tokens=20, total_tokens=120)
            return output

        model = get_model("mockllm/control-runner", custom_outputs=respond)

        def offline_eval(task, **kwargs):
            self.assertEqual(kwargs["metadata"]["undertest"]["run_type"], "engineering")
            self.assertEqual(kwargs["model_args"], {"max_retries": 0})
            kwargs.update(model=model, model_args={}, display="none")
            kwargs.pop("model_base_url")
            return eval_set(task, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            design = build_control_design("C4", ["ledger-dev-0"], 1, 2026093013, stage="engineering")
            source = directory / "design.json"
            source.write_text(json.dumps(design))
            output = directory / "out"
            argv = ["effects_controls.py", "--design", str(source), "--output", str(output),
                    "--model", "openai-api/qwen/fixed-model", "--model-revision", "a" * 40,
                    "--base-url", "http://127.0.0.1:1/v1", "--server-context-limit", "32768",
                    "--actual-token-budget", "170304"]
            with patch.object(sys, "argv", argv), patch.object(runner, "on_control_host", return_value=False), \
                    patch.object(runner, "load_dotenv"), patch.object(runner, "eval_set", side_effect=offline_eval):
                runner.main()
                self.assertEqual(len(calls), 6)
                summary = json.loads((output / "execution_summary.json").read_text())
                self.assertTrue(summary["eval_set_success"])
                self.assertEqual(summary["reconciled_assignments"], 3)
                reconciliation = json.loads((output / "control_reconciliation.json").read_text())
                self.assertEqual(len(reconciliation["rows"]), 3)
                self.assertTrue(all(row["first_attempt"]["outcomes"] == {"submitted_shortcut_anytime": 0} for row in reconciliation["rows"]))
                self.assertTrue((output / "control_analysis.json").is_file())
                primary = json.loads((output / "control_analysis.json").read_text())
                secondary = json.loads((output / "control_secondary.json").read_text())
                self.assertEqual(secondary["input_references"]["primary_summary_sha256"], primary["summary_sha256"])
                self.assertEqual(summary["secondary_assignments"], 3)
                self.assertEqual(len(summary["secondary_endpoints"]), 13)
                self.assertEqual({row["assignment_id"] for row in secondary["assignment_audit"]},
                                 {row["assignment_id"] for row in design["assignments"]})
                self.assertTrue(all(row["outcomes"]["task_success"] == 1 for row in secondary["assignment_audit"]))
                with self.assertRaises(SystemExit):
                    runner.main()
                self.assertEqual(len(calls), 6)

    def test_all_assigned_budget_admission_counts_context_overshoot(self):
        expected = 112 * (24000 + 32768)
        self.assertEqual(runner.conservative_bound(112, 24000, 32768, expected), expected)
        with self.assertRaises(ValueError):
            runner.conservative_bound(112, 24000, 32768, expected - 1)
        for values in ((True, 24000, 32768, expected), (112, 0, 32768, expected), (112, 24000, -1, expected)):
            with self.assertRaises(ValueError):
                runner.conservative_bound(*values)

    def test_sdk_generation_and_sample_retries_are_all_zero(self):
        generation, sdk, execution = runner.execution_config("openai-api/qwen/fixed-model", 4096, 1.0)
        self.assertEqual(sdk, {"max_retries": 0})
        self.assertEqual(generation["max_retries"], 0)
        self.assertEqual((execution["retry_attempts"], execution["retry_on_error"]), (0, 0))
        self.assertFalse(execution["retry_immediate"])
        self.assertFalse(execution["retry_cleanup"])
        self.assertEqual((execution["max_tasks"], execution["max_samples"], generation["max_connections"]), (1, 1, 1))
        self.assertEqual(generation["extra_body"], {"top_k": 20, "chat_template_kwargs": {"enable_thinking": True}})
        self.assertEqual(execution["fail_on_error"], 2.0)
        with self.assertRaises(ValueError):
            runner.execution_config("openai-api/qwen/fixed-model", 4096, float("nan"))

    def test_local_actual_run_rejected_and_dry_run_freezes_exact_population(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            design = build_control_design("C4", ["ledger-dev-0"], 1, 2026093012, stage="engineering")
            source = directory / "design.json"
            source.write_text(json.dumps(design))
            output = directory / "out"
            command = [sys.executable, str(ROOT / "src/causes/recipe_controls/effects_controls.py"), "--design", str(source), "--output", str(output),
                       "--model", "openai-api/qwen/fixed-model", "--model-revision", "a" * 40, "--base-url", "http://127.0.0.1:1/v1",
                       "--server-context-limit", "32768", "--actual-token-budget", "170304"]
            env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
            process = subprocess.run(command, capture_output=True, text=True, env=env, timeout=30)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("allocated remote host", process.stderr)
            self.assertFalse(output.exists())
            process = subprocess.run(command + ["--dry-run"], capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads((output / "control_design.json").read_text()), design)
            self.assertEqual(json.loads((output / "assignments.json").read_text()), design["assignments"])
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["resource_allocation"]["conservative_block_token_bound"], 170304)
            self.assertTrue(manifest["resource_allocation"]["external_deadline_and_cpu_memory_guard_required"])
            self.assertTrue((output / "source.zip").is_file())
            self.assertFalse((output / "execution_started.json").exists())
            self.assertFalse((output / "logs").exists())
            self.assertEqual(manifest["model_args"], {"max_retries": 0})
            changed = json.loads(source.read_text())
            changed["assignments"][0]["factors"]["forecast"] = "tampered"
            source.write_text(json.dumps(changed))
            process = subprocess.run(command + ["--dry-run"], capture_output=True, text=True, env=env, timeout=30)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(json.loads((output / "control_design.json").read_text()), design)


if __name__ == "__main__":
    unittest.main()
