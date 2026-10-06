import copy
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from shared.runs import config_id
from shared.runs import run_directory
from shared.runs import run_metadata


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("eval_cue", ROOT / "src/shared/report_control/eval_cue.py")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class RunIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "run"
        self.config = {
            "model": "mockllm/model",
            "model_revision": "checkpoint-001",
            "section": "development",
            "run_type": "pilot",
            "generation": {"max_tokens": 512, "temperature": 0.7},
            "source_sha256": {"data/fixture.csv": "abc"},
        }

    def test_resume_preserves_identity_and_manifest(self):
        with run_directory(self.output, self.config) as first:
            original = (self.output / "manifest.json").read_bytes()
        with run_directory(self.output, self.config) as resumed:
            self.assertEqual(first, resumed)
            self.assertEqual(original, (self.output / "manifest.json").read_bytes())
            self.assertEqual(run_metadata(resumed)["undertest"]["model_revision"], "checkpoint-001")

    def test_separate_runs_share_only_config_identity(self):
        with run_directory(self.output, self.config) as first:
            pass
        with run_directory(self.output.with_name("second"), self.config) as second:
            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertEqual(first["config_id"], second["config_id"])

    def test_config_hash_ignores_dictionary_order(self):
        self.assertEqual(config_id({"b": {"y": 1, "x": 2}, "a": 3}),
                         config_id({"a": 3, "b": {"x": 2, "y": 1}}))

    def test_changed_config_rejected_without_mutating_evidence(self):
        with run_directory(self.output, self.config):
            (self.output / "saved.eval").write_bytes(b"immutable raw evidence")
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        for field, value in {
            "model_revision": "checkpoint-002", "section": "effects", "run_type": "confirmation",
            "generation": {"max_tokens": 512, "temperature": 1},
            "source_sha256": {"data/fixture.csv": "changed"},
        }.items():
            with self.subTest(field=field):
                changed = copy.deepcopy(self.config)
                changed[field] = value
                with self.assertRaisesRegex(ValueError, "choose a new"):
                    with run_directory(self.output, changed):
                        self.fail("Changed configuration was accepted")
                self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})

    def test_directory_lock_and_release_on_failure(self):
        with self.assertRaisesRegex(RuntimeError, "simulated failure"):
            with run_directory(self.output, self.config):
                with self.assertRaisesRegex(ValueError, "Another runner"):
                    with run_directory(self.output, self.config):
                        self.fail("Concurrent access was accepted")
                raise RuntimeError("simulated failure")
        with run_directory(self.output, self.config):
            pass

    def test_legacy_or_broken_manifests_are_not_upgraded(self):
        self.output.mkdir()
        for contents in (json.dumps(self.config), "not json", "null", "[]"):
            with self.subTest(contents=contents):
                manifest = self.output / "manifest.json"
                manifest.write_text(contents)
                with self.assertRaisesRegex(ValueError, "incompatible manifest"):
                    with run_directory(self.output, self.config):
                        self.fail("Unverifiable manifest was accepted")
                self.assertEqual(manifest.read_text(), contents)

    def test_orphaned_results_are_not_adopted(self):
        self.output.mkdir()
        (self.output / "logs").mkdir()
        with self.assertRaisesRegex(ValueError, "unregistered results"):
            with run_directory(self.output, self.config):
                self.fail("Unregistered results were accepted")
        self.assertFalse((self.output / "manifest.json").exists())

    def test_runner_passes_metadata_and_guard_precedes_eval(self):
        arguments = ["eval_cue.py", "--model", "mockllm/model", "--output", str(self.output),
                     "--model-revision", "checkpoint-001", "--section", "development"]
        with patch.object(sys, "argv", arguments), patch.object(RUNNER, "load_dotenv"), \
             patch.object(RUNNER, "source_files", return_value=[ROOT / "pyproject.toml"]), \
             patch.object(RUNNER, "eval_set", return_value=(True, [])) as evaluate, \
             patch.object(RUNNER, "summarize"):
            RUNNER.main()
            manifest = json.loads((self.output / "manifest.json").read_text())
            self.assertEqual(evaluate.call_args.kwargs["metadata"], run_metadata(manifest))
            self.assertFalse(evaluate.call_args.kwargs["retry_cleanup"])
            original = {p.name: p.read_bytes() for p in self.output.iterdir()}
            arguments[-1] = "effects"
            with patch("sys.stderr"), self.assertRaises(SystemExit):
                RUNNER.main()
            self.assertEqual(evaluate.call_count, 1)
            self.assertEqual(original, {p.name: p.read_bytes() for p in self.output.iterdir()})

    def test_snapshot_includes_non_code_fixtures(self):
        root = Path(self.temp.name)
        (root / "data").mkdir()
        for name in ("input.csv", "message.txt", "fixture.yml", ".env"):
            (root / "data" / name).write_text("fixture")
        with patch.object(RUNNER, "ROOT", root):
            names = {p.name for p in RUNNER.source_files()}
        self.assertEqual(names, {"input.csv", "message.txt", "fixture.yml"})

    def test_interrupted_snapshot_is_rebuilt_before_evaluation(self):
        arguments = ["eval_cue.py", "--model", "mockllm/model", "--output", str(self.output)]
        write_member = zipfile.ZipFile.writestr

        def interrupt(archive, *args, **kwargs):
            write_member(archive, *args, **kwargs)
            raise RuntimeError("interrupted snapshot")

        with patch.object(sys, "argv", arguments), patch.object(RUNNER, "load_dotenv"), \
             patch.object(RUNNER, "source_files", return_value=[ROOT / "pyproject.toml", ROOT / "uv.lock"]), \
             patch.object(RUNNER, "eval_set", return_value=(True, [])) as evaluate, \
             patch.object(RUNNER, "summarize"):
            with patch.object(zipfile.ZipFile, "writestr", interrupt), \
                 self.assertRaisesRegex(RuntimeError, "interrupted snapshot"):
                RUNNER.main()
            evaluate.assert_not_called()
            self.assertFalse((self.output / "source.zip").exists())
            original_manifest = (self.output / "manifest.json").read_bytes()

            def check_snapshot(*args, **kwargs):
                with zipfile.ZipFile(self.output / "source.zip") as archive:
                    self.assertEqual(set(archive.namelist()), {"pyproject.toml", "uv.lock"})
                self.assertTrue(json.loads((self.output / "inputs.json").read_text()))
                self.assertIn("git_head", json.loads((self.output / "revision.json").read_text()))
                return True, []

            evaluate.side_effect = check_snapshot
            RUNNER.main()
            evaluate.assert_called_once()
            self.assertEqual(original_manifest, (self.output / "manifest.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
