import importlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml
from inspect_ai.scorer import CORRECT, INCORRECT, Target
from inspect_ai.tool import ToolDef, bash
from inspect_ai.util import Store

report = importlib.import_module("shared.tasks.report_task")


def result(stdout="", stderr="", returncode=0):
    return SimpleNamespace(
        stdout=stdout, stderr=stderr, returncode=returncode, success=returncode == 0,
    )


class CheckerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.submission = "print('saved submission')\n"
        self.actor = SimpleNamespace(
            read_file=AsyncMock(return_value=self.submission),
            write_file=AsyncMock(), exec=AsyncMock(),
        )
        self.checker = SimpleNamespace(write_file=AsyncMock(), exec=AsyncMock())
        self.saved = Store()
        self.sandbox_patch = patch.object(
            report, "sandbox", side_effect={"default": self.actor, "checker": self.checker}.__getitem__,
        )
        self.store_patch = patch.object(report, "store", return_value=self.saved)
        self.sandbox_patch.start()
        self.store_patch.start()
        self.addCleanup(self.sandbox_patch.stop)
        self.addCleanup(self.store_patch.stop)

    def correct_results(self):
        return [result()] + [
            result(json.dumps(report.expected_report(case))) for case in report.CASES
        ]

    async def test_only_detached_snapshot_is_executed_as_worker(self):
        self.checker.exec.side_effect = self.correct_results()
        score = await report.task_success()(None, Target(""))
        self.assertEqual(score.value, CORRECT)
        self.actor.exec.assert_not_awaited()
        self.actor.write_file.assert_not_awaited()
        self.actor.read_file.assert_awaited_once_with("/workspace/report.py")
        self.checker.write_file.assert_awaited_once()
        path, text = self.checker.write_file.await_args.args
        self.assertEqual(text, self.submission)
        self.assertTrue(path.startswith("/tmp/submission-"))
        self.assertTrue(path.endswith("/report.py"))
        calls = self.checker.exec.await_args_list
        self.assertEqual(calls[0].args[0], ["/bin/chmod", "-R", "a-w", path.rsplit("/", 1)[0]])
        self.assertEqual(len(calls), len(report.CASES) + 1)
        for call, case in zip(calls[1:], report.CASES):
            self.assertEqual(call.args[0], ["/usr/local/bin/python3", "-I", path])
            self.assertEqual(call.kwargs["user"], "worker")
            self.assertEqual(json.loads(call.kwargs["input"]), case)
            self.assertEqual(call.kwargs["timeout"], 5)
            self.assertFalse(call.kwargs["timeout_retry"])
        self.assertEqual(self.saved.get("submitted_report_py"), self.submission)
        self.assertEqual(self.saved.get("report_py"), self.submission)
        self.assertEqual(self.saved.get("artifacts"), {"report.py": self.submission})
        self.assertEqual(score.metadata["checker"], "detached-report-v1")

    async def test_missing_checker_fails_closed_and_retains_submission(self):
        # Inspect returns default for any name when only one sandbox exists.
        with patch.object(report, "sandbox", return_value=self.actor):
            with self.assertRaisesRegex(RuntimeError, "separate from the actor"):
                await report.task_success()(None, Target(""))
        self.actor.exec.assert_not_awaited()
        self.actor.write_file.assert_not_awaited()
        self.assertEqual(self.saved.get("artifacts"), {"report.py": self.submission})

    async def test_checker_setup_failure_is_an_error_with_submission_retained(self):
        self.checker.exec.return_value = result(stderr="chmod failed", returncode=1)
        with self.assertRaisesRegex(RuntimeError, "Could not protect"):
            await report.task_success()(None, Target(""))
        self.assertEqual(self.checker.exec.await_count, 1)
        self.assertEqual(self.saved.get("submitted_report_py"), self.submission)
        self.assertEqual(self.saved.get("artifacts"), {"report.py": self.submission})

    async def test_wrong_output_nonzero_exit_and_timeout_fail(self):
        outputs = self.correct_results()
        outputs[1] = result("not json")
        outputs[2] = result(outputs[2].stdout, returncode=1)
        outputs[3] = TimeoutError()
        # Boolean values compare equal to integers in Python but are invalid here.
        outputs[4] = result('{"count": true, "total_cents": false}')
        self.checker.exec.side_effect = outputs
        score = await report.task_success()(None, Target(""))
        self.assertEqual(score.value, INCORRECT)
        self.assertEqual(sum(case["passed"] for case in score.metadata["cases"]), len(report.CASES) - 4)
        self.assertEqual(score.metadata["cases"][2]["error"], "timeout")
        self.assertEqual(self.saved.get("artifacts"), {"report.py": self.submission})

    async def test_missing_submission_fails_without_running_checker(self):
        self.actor.read_file.side_effect = FileNotFoundError("report.py")
        score = await report.task_success()(None, Target(""))
        self.assertEqual(score.value, INCORRECT)
        self.checker.exec.assert_not_awaited()
        self.checker.write_file.assert_not_awaited()

    async def test_cleanup_retains_artifact_after_solver_error(self):
        self.saved.set("artifacts", {"other.txt": "preserve me"})
        await report.retain_submission(None)
        self.assertEqual(self.saved.get("report_py"), self.submission)
        self.assertNotIn("submitted_report_py", self.saved)
        self.assertEqual(self.saved.get("artifacts"), {
            "report.py": self.submission, "other.txt": "preserve me",
        })

    async def test_cleanup_keeps_scored_snapshot_when_actor_changes(self):
        self.saved.set("submitted_report_py", "scored content")
        await report.retain_submission(None)
        self.assertEqual(self.saved.get("report_py"), self.submission)
        self.assertEqual(self.saved.get("artifacts"), {"report.py": "scored content"})

    async def test_cleanup_read_error_keeps_previous_artifact(self):
        self.saved.set("artifacts", {"report.py": "captured before error"})
        self.actor.read_file.side_effect = PermissionError("read denied")
        await report.retain_submission(None)
        self.assertEqual(self.saved.get("artifacts"), {"report.py": "captured before error"})
        self.assertEqual(self.saved.get("artifact_error"), "PermissionError: read denied")

    async def test_model_bash_cannot_select_checker(self):
        actor_tool = bash(timeout=30, sandbox="default")
        with patch.object(report, "bash", return_value=actor_tool) as factory:
            report.report_task()
        factory.assert_called_once_with(timeout=30, sandbox="default")
        self.assertEqual(set(ToolDef(actor_tool).parameters.properties), {"command"})
        self.actor.exec.return_value = result("actor output")
        with patch("inspect_ai.tool._tools._execute.sandbox_env", return_value=self.actor) as lookup:
            self.assertEqual(await actor_tool("echo checker"), "actor output")
        lookup.assert_called_once_with("default")


class CheckerConfigTests(unittest.TestCase):
    def test_checker_has_no_actor_files_or_shared_access(self):
        services = yaml.safe_load((report.FIXTURES / "compose.yaml").read_text())["services"]
        actor, checker = services["default"], services["checker"]
        self.assertEqual(actor["build"]["target"], "actor")
        self.assertEqual(checker["build"]["target"], "checker")
        self.assertTrue(checker["read_only"])
        self.assertEqual(checker["user"], "root")
        self.assertTrue(any(mount.startswith("/tmp:") for mount in checker["tmpfs"]))
        for service in (actor, checker):
            self.assertEqual(service["network_mode"], "none")
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertIn("no-new-privileges:true", service["security_opt"])
            for shared_access in ("volumes", "volumes_from", "pid", "ipc", "privileged"):
                self.assertNotIn(shared_access, service)
        dockerfile = (report.FIXTURES / "Dockerfile").read_text()
        self.assertNotIn("COPY", dockerfile.split("FROM runtime AS actor")[0])


if __name__ == "__main__":
    unittest.main()
