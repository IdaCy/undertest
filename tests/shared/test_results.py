import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from inspect_ai.log import EvalConfig, EvalDataset, EvalError, EvalLog, EvalSample, EvalSampleLimit, EvalSpec
from inspect_ai.model import ChatMessageAssistant, ChatMessageUser, GenerateConfig
from inspect_ai.scorer import Score

from shared.results import summarize


class ResultsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.output = self.root / "export"

    def sample(self, **changes):
        values = dict(
            id="sample-1", epoch=1, input="repair the task", target="",
            messages=[ChatMessageUser(content="repair the task"), ChatMessageAssistant(content="done")],
            metadata={"condition": "ordinary"},
        )
        values.update(changes)
        return EvalSample(**values)

    def log(self, samples=None, *, day=1, task_id="task-id", metadata=None, **spec_changes):
        spec = dict(
            created=f"2026-09-{day:02d}T00:00:00+00:00", task="test/task", task_id=task_id,
            model="mockllm/model", eval_set_id="eval-set", run_id=f"native-run-{day}",
            dataset=EvalDataset(samples=1), config=EvalConfig(epochs=1), metadata=metadata,
        )
        spec.update(spec_changes)
        return EvalLog(status="success", eval=EvalSpec(**spec), samples=samples)

    def runner_metadata(self, **changes):
        values = dict(run_id="run-1", config_id="config-1", model_revision="weights-1",
                      section="signals", run_type="pilot")
        values.update(changes)
        return {"undertest": values, "other": {"nested": [1, None, "text"]}}

    def export(self, logs):
        entries = {}
        originals = {}
        for name, log in logs.items():
            path = self.logs / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"original log must remain unchanged")
            originals[path] = path.read_bytes()
            entries[path.as_uri()] = log
        with patch("shared.results.list_eval_logs", return_value=[
            SimpleNamespace(name=name) for name in entries
        ]), patch("shared.results.read_eval_log", side_effect=lambda name, **kwargs: entries[name]):
            rows = summarize(self.logs, self.output)
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)
        return rows

    def test_named_scores_metadata_and_artifacts_survive_export(self):
        scores = {
            "awareness": Score(value={"explicit": 0, "uncertain": 0.5}, answer="span",
                               explanation="not a task score", metadata={"evidence": [2, 3]}),
            "task_success": Score(value="C", explanation="all cases passed", metadata={"cases": [True]}),
            "proxy": Score(value=0),
        }
        sample = self.sample(scores=scores, store={"artifacts": {"src/solution.py": "print(1)\n"}},
                             metadata={"condition": "ordinary", "factors": {"grading": "narrow"}})
        log = self.log([sample], metadata=self.runner_metadata())
        log.metadata = {"review": {"complete": False}}
        row, = self.export({"first.eval": log})
        artifact_dir = Path(row["artifacts"])
        self.assertEqual((artifact_dir / "src/solution.py").read_text(), "print(1)\n")
        expected = {name: score.model_dump(mode="json") for name, score in scores.items()}
        self.assertEqual(json.loads((artifact_dir / "scores.json").read_text()), expected)
        self.assertEqual(json.loads(row["scores"]), expected)
        self.assertEqual(row["task_success"], 1)
        self.assertEqual(row["score.task_success.value"], "C")
        self.assertEqual(row["score.proxy.value"], 0)
        self.assertEqual(json.loads(row["score.awareness.value"]), scores["awareness"].value)
        self.assertEqual(json.loads(row["sample_metadata"]), sample.metadata)
        self.assertEqual(json.loads(row["eval_metadata"]), log.eval.metadata)
        self.assertEqual(json.loads(row["log_metadata"]), log.metadata)
        self.assertEqual(json.loads((artifact_dir / "metadata.json").read_text()), {
            "sample": sample.metadata, "eval": log.eval.metadata, "log": log.metadata,
        })
        self.assertEqual((row["run_id"], row["config_id"], row["model_revision"]),
                         ("run-1", "config-1", "weights-1"))
        with (self.output / "samples.csv").open(newline="") as handle:
            exported, = csv.DictReader(handle)
        self.assertEqual(json.loads(exported["scores"]), expected)

    def test_unrelated_scorer_never_becomes_task_success(self):
        row, = self.export({"first.eval": self.log([
            self.sample(scores={"awareness": Score(value="C")}),
        ])})
        self.assertEqual(row["task_success"], "")
        self.assertEqual(row["score_detail"], "")
        self.assertEqual(row["score.awareness.value"], "C")

    def test_legacy_submitted_artifact_takes_priority_over_cleanup_copy(self):
        samples = [
            self.sample(store={"submitted_report_py": "graded", "report_py": "cleanup"},
                        scores={"task_success": Score(value="I")}),
            self.sample(id="sample-2", store={"report_py": "saved before error"}),
        ]
        rows = self.export({"legacy.eval": self.log(samples)})
        self.assertEqual((Path(rows[0]["artifacts"]) / "report.py").read_text(), "graded")
        self.assertEqual((Path(rows[1]["artifacts"]) / "report.py").read_text(), "saved before error")
        self.assertEqual(rows[0]["task_success"], 0)

    def test_errors_limits_and_missing_scores_are_not_converted_to_failure_scores(self):
        error = EvalError(message="execution failed", traceback="trace", traceback_ansi="trace")
        samples = [
            self.sample(error=error, store={"artifact_error": "missing file"}, scores=None),
            self.sample(id="limited", limit=EvalSampleLimit(type="token", limit=50), scores={}),
        ]
        empty = self.log(None, day=2)
        empty.status = "error"
        empty.error = error
        rows = self.export({"samples.eval": self.log(samples), "empty.eval": empty})
        failed, limited, absent = rows
        self.assertEqual(failed["sample_status"], "error")
        self.assertEqual(failed["error"], "execution failed")
        self.assertEqual(json.loads(failed["error_details"])["traceback"], "trace")
        self.assertEqual(failed["artifact_error"], "missing file")
        self.assertEqual(failed["task_success"], "")
        self.assertIsNone(json.loads(failed["scores"]))
        self.assertEqual(limited["sample_status"], "limit")
        self.assertEqual(json.loads(limited["scores"]), {})
        self.assertEqual(json.loads(limited["limit"])["type"], "token")
        self.assertEqual(absent["sample_status"], "no_samples")
        self.assertEqual(absent["error"], "execution failed")
        self.assertEqual(json.loads(absent["eval_error_details"])["traceback"], "trace")

    def test_unsafe_and_reserved_artifacts_do_not_discard_valid_peers(self):
        outside = self.root / "outside.txt"
        sample = self.sample(store={"artifacts": {
            "../outside.txt": "escape", str(outside): "absolute", "C:/outside.txt": "windows",
            "a\\outside.txt": "backslash", ".": "directory", "": "empty",
            "messages.json": "fake transcript", "scores.json/nested": "fake score",
            "metadata.json": "fake metadata", "export_errors.json": "hide errors",
            "not-text": {"value": "not text"}, "nested/good.txt": "valid",
        }})
        row, = self.export({"unsafe.eval": self.log([sample])})
        directory = Path(row["artifacts"])
        self.assertEqual((directory / "nested/good.txt").read_text(), "valid")
        self.assertFalse(outside.exists())
        self.assertEqual(json.loads((directory / "messages.json").read_text())[0]["role"], "user")
        self.assertEqual(len(json.loads(row["artifact_export_errors"])), 11)
        self.assertFalse((directory / "not-text").exists())

    def test_escaping_symlinks_are_rejected_without_following_them(self):
        name = "symlinks.eval"
        key = hashlib.sha256(f"{name}:sample-1:1".encode()).hexdigest()[:20]
        directory = self.output / "artifacts" / key
        directory.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        secret = outside / "untouched.txt"
        secret.write_text("original")
        (directory / "linked").symlink_to(outside, target_is_directory=True)
        (directory / "file.txt").symlink_to(secret)
        (directory / "messages.json").symlink_to(secret)
        row, = self.export({name: self.log([self.sample(store={"artifacts": {
            "linked/evil.txt": "escape", "file.txt": "overwrite", "good.txt": "valid",
        }})])})
        self.assertEqual(secret.read_text(), "original")
        self.assertFalse((outside / "evil.txt").exists())
        self.assertEqual((directory / "good.txt").read_text(), "valid")
        self.assertEqual({entry["path"] for entry in json.loads(row["artifact_export_errors"])},
                         {"linked/evil.txt", "file.txt", "messages.json"})

    def test_symlinked_artifact_root_does_not_redirect_exports(self):
        self.output.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (self.output / "artifacts").symlink_to(outside, target_is_directory=True)
        row, = self.export({"first.eval": self.log([self.sample(store={"artifacts": {"good": "text"}})])})
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(json.loads(row["artifact_export_errors"]))
        self.assertTrue((self.output / "samples.csv").exists())

    def test_tagged_retries_group_but_runs_revisions_and_settings_remain_distinct(self):
        logs = {
            "first.eval": self.log([self.sample()], day=1, metadata=self.runner_metadata()),
            "retry.eval": self.log([self.sample()], day=2, metadata=self.runner_metadata()),
            "other-run.eval": self.log([self.sample()], day=3, metadata=self.runner_metadata(run_id="run-2")),
            "revision.eval": self.log([self.sample()], day=4, metadata=self.runner_metadata(model_revision="weights-2")),
            "generation.eval": self.log([self.sample()], day=5, metadata=self.runner_metadata(),
                                        model_generate_config=GenerateConfig(temperature=0.7)),
            "config.eval": self.log([self.sample()], day=6, metadata=self.runner_metadata(config_id="config-2")),
            "other-task.eval": self.log([self.sample()], day=7, metadata=self.runner_metadata(), task_id="other-task"),
        }
        rows = self.export(logs)
        self.assertEqual([row["latest_attempt"] for row in rows], [False, True, True, True, True, True, True])
        self.assertEqual(rows[0]["attempt_group"], rows[1]["attempt_group"])

    def test_legacy_grouping_requires_reliable_identity_and_matching_provenance(self):
        logs = {
            "first.eval": self.log([self.sample()], day=1),
            "retry.eval": self.log([self.sample()], day=2),
            "another-directory/first.eval": self.log([self.sample()], day=3),
            "another-set.eval": self.log([self.sample()], day=4, eval_set_id="different-set"),
            "metadata.eval": self.log([self.sample()], day=5, metadata={"model_revision": "other"}),
            "unknown-1.eval": self.log([self.sample()], day=6, task_id=""),
            "unknown-2.eval": self.log([self.sample()], day=7, task_id=""),
        }
        rows = self.export(logs)
        self.assertEqual([row["latest_attempt"] for row in rows], [False, True, True, True, True, True, True])

    def test_retry_transport_changes_group_but_solver_plan_changes_do_not(self):
        original = self.log([self.sample()], day=1, metadata=self.runner_metadata(),
                            model_generate_config=GenerateConfig(temperature=0.5, max_connections=4))
        retry = self.log([self.sample()], day=2, metadata=self.runner_metadata(),
                         model_generate_config=GenerateConfig(temperature=0.5, max_connections=1, timeout=60))
        original.plan.config = GenerateConfig(temperature=0.5, max_connections=4)
        retry.plan.config = GenerateConfig(temperature=0.5, max_connections=1, timeout=60)
        changed_plan = retry.model_copy(deep=True)
        changed_plan.eval.created = "2026-09-03T00:00:00+00:00"
        changed_plan.plan.config.temperature = 0.9
        rows = self.export({"original.eval": original, "retry.eval": retry, "changed.eval": changed_plan})
        self.assertEqual([row["latest_attempt"] for row in rows], [False, True, True])


if __name__ == "__main__":
    unittest.main()
