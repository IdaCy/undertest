import copy
import csv
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from emergence.checkpoints.analysis import CHOICE_FIELDS
from emergence.checkpoints.analysis import CURRENT_MEASUREMENT_SOURCES
from emergence.checkpoints.analysis import CURRENT_RUNNER_SOURCE
from emergence.checkpoints.analysis import DIAGNOSTIC_FIELDS
from emergence.checkpoints.analysis import MEASUREMENT_SOURCES
from emergence.checkpoints.analysis import RUNNER_SOURCE
from emergence.checkpoints.analysis import VALIDITY_FIELDS
from emergence.checkpoints.analysis import binary_summary
from emergence.checkpoints.analysis import grading_contrast
from emergence.checkpoints.analysis import load_cohort
from emergence.checkpoints.analysis import summarize_cohort
from emergence.checkpoints.analysis import write_analysis


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def put_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def panel_fixture():
    samples = []
    instances = ["a", "b", "c", "d"]
    conditions = ["ordinary_limited", "ordinary_broad", "evaluation_limited", "evaluation_broad"]
    for index, instance in enumerate(instances):
        for kind, condition, replicate in (
            *[("main", condition, replicate) for condition in conditions for replicate in range(2)],
            ("competence", "control_competence", 0),
            ("score_comprehension", "control_score_limited", 0),
            ("format", "control_format", 0),
        ):
            identifier = f"{instance}-{condition}-{replicate}"
            prompt = f"Frozen input for {identifier}"
            metadata = {
                "sample_kind": kind, "condition": condition, "instance_id": instance,
                "family": "one" if index < 2 else "two", "replicate": replicate,
                "framing": condition.split("_")[0] if kind == "main" else None,
                "grading": condition.split("_")[1] if kind == "main" else None,
                "split": "calibration", "assay_version": "test-v1", "cue_variant": "primary",
                "seed": index, "input_sha256": digest(prompt.encode()),
            }
            samples.append({"id": identifier, "input": prompt, "metadata": metadata})
    return {
        "schema_version": 1, "assay_version": "test-v1", "panel_kind": "calibration",
        "build_config": {"repetitions": 2, "seed": 7, "conditions": conditions, "include_controls": True, "instance_ids": instances, "cue_variant": "primary"},
        "samples": samples,
    }


def outcomes_fixture():
    outcomes = {field: None for field in (*VALIDITY_FIELDS, *CHOICE_FIELDS, *DIAGNOSTIC_FIELDS)}
    outcomes.update({
        "segmented_format_valid": True, "raw_text_format_valid": False,
        "behavior_observed": True, "choice_depends_on_segmentation": True,
        "conservative_behavior_observed": False, "shortcut_selection": True,
        "task_success": False, "ordinary_bug_selection": False, "weak_proxy_success": True,
        "diagnostic_success": True, "raw_text_selected_id": None, "selected_id": "A",
    })
    return outcomes


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.panel = panel_fixture()
        self.panel_path = self.root / "full-panel.json"
        put_json(self.panel_path, self.panel)
        self.original = self.root / "original"
        # First main assignment completes, next is cancelled; 42 never start.
        self.make_run(self.original, self.panel, ["completed", "error"])
        self.reconciliation = self.make_reconciliation()

    def tearDown(self):
        self.temp.cleanup()

    def make_run(self, directory, panel, statuses, *, retry=2, current_layout=False):
        names = (*CURRENT_MEASUREMENT_SOURCES, CURRENT_RUNNER_SOURCE) if current_layout else (*MEASUREMENT_SOURCES, RUNNER_SOURCE)
        sources = {name: f"frozen source for {name}".encode() for name in names}
        directory.mkdir()
        with zipfile.ZipFile(directory / "source.zip", "w") as archive:
            for name, raw in sources.items():
                archive.writestr(name, raw)
        put_json(directory / "panel.json", panel)
        put_json(directory / "inputs.json", panel["samples"])
        put_json(directory / "tokenized_inputs.json", {
            sample["id"]: {
                "request": {"model": "served-model", "add_special_tokens": False, "prompt": sample["input"]},
                "response": {"tokens": list(sample["input"].encode())},
            } for sample in panel["samples"]
        })
        ids = [sample["id"] for sample in panel["samples"]]
        manifest = {
            "run_type": "calibration", "preview_only": False, "epochs": 1,
            "run_id": directory.name, "config_id": directory.name + "-config",
            "model": "offline-model", "model_revision": "revision", "mode": "raw",
            "checkpoint": {"model_revision": "revision", "local_evidence_directory": "/old/location"},
            "assay_version": "test-v1", "generation": {"max_tokens": 100, "max_retries": retry},
            "system_message": None, "max_connections": 1,
            "inspect_ai_version": "1", "openai_version": "2",
            "server_receipt": {"served_model": "served-model", "precision": "bfloat16", "server_version": "1", "launch_argv": ["server"]},
            "source_sha256": {name: digest(raw) for name, raw in sources.items()},
            "panel_sha256": digest((directory / "panel.json").read_bytes()), "sample_ids": ids,
            "git": {"commit": "old" if retry else "new", "worktree_dirty": False},
            "retry_policy": {"provider_max_retries": retry},
        }
        put_json(directory / "manifest.json", manifest)
        rows = []
        for index, status in enumerate(statuses):
            sample = panel["samples"][index]
            key = f"sample-{index}"
            metadata = {**sample["metadata"], "elicitation_mode": "raw", "reasoning_prefix_in_prompt": False, "generation_seed": sample["metadata"]["seed"]}
            outcomes = outcomes_fixture()
            artifact = directory / "artifacts" / key
            put_json(artifact / "metadata.json", {"sample": metadata})
            put_json(artifact / "export_errors.json", [])
            put_json(artifact / "scores.json", {"development_outcomes": {"value": outcomes}} if status == "completed" else {})
            put_json(artifact / "development/outcomes.json", outcomes)
            put_json(artifact / "development/evidence.json", {"stop_reason": "length", "inline_segmentation_status": "complete"})
            put_json(artifact / "development/model_output.json", {"usage": {"input_tokens": 10, "output_tokens": 20}} if status == "completed" else {"usage": None})
            rows.append({
                "sample_id": sample["id"], "sample_key": key, "epoch": "1", "latest_attempt": "True",
                "run_id": manifest["run_id"], "config_id": manifest["config_id"], "model": manifest["model"], "model_revision": manifest["model_revision"],
                "condition": metadata["condition"], "sample_metadata": json.dumps(metadata),
                "sample_status": status, "error": "CancelledError" if status == "error" else "", "error_retries": "[]", "limit": "{}" if status == "limit" else "",
                "artifact_error": "", "artifact_export_errors": "[]", "artifacts": "/remote/not-mounted/artifacts/" + key,
                "input_tokens": 10 if status == "completed" else 0, "output_tokens": 20 if status == "completed" else 0,
                "score.development_outcomes.value": json.dumps(outcomes) if status == "completed" else "",
            })
        self.write_rows(directory, rows)
        put_json(directory / "execution_audit.json", {"complete": len(statuses) == len(ids) and all(s == "completed" for s in statuses), "expected_count": len(ids), "latest_count": len(statuses), "missing_ids": ids[len(statuses):], "unexpected_ids": [], "duplicate_ids": [], "failed_samples": []})
        (directory / "logs").mkdir()
        (directory / "logs/frozen.eval").write_bytes(b"frozen interrupted log")

    def write_rows(self, directory, rows):
        with (directory / "samples.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["sample_id", "sample_status"])
            writer.writeheader()
            writer.writerows(rows)

    def rows(self, directory=None):
        with ((directory or self.original) / "samples.csv").open() as stream:
            return list(csv.DictReader(stream))

    def mutate_json(self, path, mutation):
        value = json.loads(path.read_text())
        mutation(value)
        put_json(path, value)

    def make_reconciliation(self):
        ids = [s["id"] for s in self.panel["samples"]]
        path = self.root / "reconciliation.json"
        put_json(path, {
            "original_panel_sha256": digest(self.panel_path.read_bytes()), "full_assigned_count": 44,
            "original_log": "/remote/old/path/frozen.eval", "original_log_sha256": digest(b"frozen interrupted log"),
            "completed_ids": ids[:1], "interrupted_retained_unknown_ids": ids[1:2], "unstarted_ids": ids[2:],
            "attempts": [{"sample_id": identifier, "event_count": 1, "framework_retries": None} for identifier in ids[:2]],
        })
        return path

    def continuation(self, samples=None):
        panel = {**self.panel, "samples": samples if samples is not None else self.panel["samples"][2:]}
        directory = self.root / "continuation"
        self.make_run(directory, panel, ["completed"] * len(panel["samples"]), retry=0)
        return directory

    def load(self, runs=None, reconcile=True):
        return load_cohort(self.panel_path, runs or [self.original], reconciliation_path=self.reconciliation if reconcile else None)

    def test_unknown_binary_bounds_and_no_coercion(self):
        self.assertEqual(binary_summary([True, False, None]), {"assigned": 3, "true": 1, "false": 1, "unknown": 1, "observed_rate": 0.5, "all_assigned_bounds": [1/3, 2/3]})
        self.assertEqual(binary_summary([None, None])["all_assigned_bounds"], [0, 1])
        self.assertIsNone(binary_summary([None])["observed_rate"])
        self.assertIsNone(binary_summary([])["all_assigned_bounds"])
        for invalid in (1, 0, "False", float("nan")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                binary_summary([invalid])

    def test_equal_families_then_instances_not_pooled_samples(self):
        records = []
        # Family A has two instances, +1 and 0; family B has one, -1.
        # Equal-family mean = ((1 + 0)/2 - 1)/2 = -0.25, not 0.
        for family, instance, limited, broad, repeats in [("A", "a", True, False, 1), ("A", "b", False, False, 3), ("B", "c", False, True, 2)]:
            for framing in ("ordinary", "evaluation"):
                for grading, value in (("limited", limited), ("broad", broad)):
                    records.extend({"metadata": {"sample_kind": "main", "family": family, "instance_id": instance, "framing": framing, "grading": grading}, "outcomes": {"shortcut_selection": value}} for _ in range(repeats))
        records.append({"metadata": {"sample_kind": "competence"}, "outcomes": {"shortcut_selection": True}})
        result = grading_contrast(records, "shortcut_selection")
        self.assertEqual(result["all_assigned_bounds"], [-0.25, -0.25])
        self.assertEqual(result["fully_observed_contrast"], -0.25)
        records[0]["outcomes"]["shortcut_selection"] = None
        result = grading_contrast(records, "shortcut_selection")
        self.assertEqual(result["all_assigned_bounds"], [-0.375, -0.25])
        self.assertIsNone(result["observed_cell_contrast"])
        self.assertIsNone(result["fully_observed_contrast"])

    def test_error_saved_scores_are_unknown_and_raw_endpoints_stay_separate(self):
        cohort = self.load()
        self.assertEqual(len(cohort["episodes"]), 44)
        complete, cancelled, absent = cohort["episodes"][:3]
        self.assertEqual(cancelled["sample_status"], "error")
        self.assertEqual(cancelled["outcomes"], {})
        self.assertTrue(cancelled["saved_outcomes"]["shortcut_selection"])
        self.assertIsNone(cancelled["output_tokens"])
        self.assertEqual(absent["sample_status"], "unstarted")
        summary = summarize_cohort(cohort)
        self.assertEqual(summary["returned_output_tokens"], 20)
        group = next(g for g in summary["groups"] if g["condition"] == "ordinary_limited")
        self.assertEqual(group["assigned"], 8)
        self.assertEqual(group["outcomes"]["shortcut_selection"]["true"], 1)
        self.assertEqual(group["outcomes"]["shortcut_selection"]["unknown"], 7)
        self.assertEqual(group["outcomes"]["raw_text_format_valid"]["false"], 1)
        self.assertEqual(group["outcomes"]["segmented_format_valid"]["true"], 1)
        self.assertEqual(group["outcomes"]["conservative_shortcut_selection"]["unknown"], 8)
        self.assertEqual(complete["evidence"]["stop_reason"], "length")
        self.assertEqual(self.load(reconcile=False)["episodes"][2]["sample_status"], "not_recorded")

    def test_continuation_retains_original_denominator_and_cancelled_unknown(self):
        continuation = self.continuation()
        cohort = self.load([self.original, continuation])
        summary = summarize_cohort(cohort)
        self.assertEqual(summary["assigned_count"], 44)
        self.assertEqual(summary["measurement_available_count"], 43)
        self.assertEqual(summary["execution_status"], {"completed": 43, "error": 1})
        self.assertEqual(cohort["runs"][0]["generation_max_retries"], 2)
        self.assertEqual(cohort["runs"][1]["generation_max_retries"], 0)
        with self.assertRaisesRegex(ValueError, "requires interruption"):
            self.load([self.original, continuation], reconcile=False)

    def test_current_layout_source_archive_loads_without_rewriting_historical_pins(self):
        current = self.root / "current-layout"
        self.make_run(current, self.panel, ["completed", "error"], current_layout=True)
        cohort = self.load([current])
        self.assertEqual(len(cohort["episodes"]), 44)
        sources = cohort["measurement_signature"]["measurement_sources"]
        self.assertTrue(set(CURRENT_MEASUREMENT_SOURCES) <= set(sources))
        self.assertTrue(set(MEASUREMENT_SOURCES).isdisjoint(sources))

    def test_cross_layout_continuation_is_not_assumed_measurement_identical(self):
        current = self.root / "current-continuation"
        panel = {**self.panel, "samples": self.panel["samples"][2:]}
        self.make_run(current, panel, ["completed"] * len(panel["samples"]), retry=0, current_layout=True)
        with self.assertRaisesRegex(ValueError, "differs across continuation"):
            self.load([self.original, current])

    def test_large_export_evidence_field_and_csv_limit_restoration(self):
        path = self.original / "samples.csv"
        with path.open() as stream:
            rows = list(csv.DictReader(stream))
        fieldnames = [*rows[0], "exported_evidence"]
        rows[0]["exported_evidence"] = 'long evidence, with "quotes"\n' * 8000
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        previous_limit = csv.field_size_limit()
        self.assertGreater(len(rows[0]["exported_evidence"]), previous_limit)
        cohort = self.load()
        self.assertEqual(len(cohort["episodes"]), 44)
        self.assertTrue(cohort["episodes"][0]["measurement_available"])
        self.assertEqual(csv.field_size_limit(), previous_limit)

    def test_incomplete_factorial_and_control_panel_rejected(self):
        for sample_index in (0, 8):
            with self.subTest(sample_index=sample_index):
                broken = copy.deepcopy(self.panel)
                broken["samples"].pop(sample_index)
                put_json(self.panel_path, broken)
                with self.assertRaisesRegex(ValueError, "Incomplete"):
                    self.load()
        put_json(self.panel_path, self.panel)
        with self.assertRaisesRegex(ValueError, "Missing assigned factorial"):
            grading_contrast([{"metadata": self.panel["samples"][0]["metadata"], "outcomes": {}}], "shortcut_selection")

    def test_duplicate_attempts_inputs_keys_and_run_identity_rejected(self):
        original_rows = self.rows()
        for mutation in (lambda rows: rows.append(rows[0]), lambda rows: rows[0].update(latest_attempt="False"), lambda rows: rows[0].update(error_retries='[{"message":"retry"}]'), lambda rows: rows[1].update(sample_key=rows[0]["sample_key"])):
            rows = copy.deepcopy(original_rows)
            mutation(rows)
            self.write_rows(self.original, rows)
            with self.assertRaises(ValueError):
                self.load()
        self.write_rows(self.original, original_rows)
        with self.assertRaisesRegex(ValueError, "unique run"):
            self.load([self.original, self.original])
        self.mutate_json(self.original / "inputs.json", lambda value: value.append(value[0]))
        with self.assertRaisesRegex(ValueError, "Duplicate inputs"):
            self.load()

    def test_continuation_cannot_replace_cancelled_or_completed(self):
        continuation = self.continuation(self.panel["samples"][1:])
        with self.assertRaisesRegex(ValueError, "started, unknown"):
            self.load([self.original, continuation])

    def test_later_runs_cannot_repeat_continued_inputs_even_without_a_row(self):
        first = self.root / "continuation-one"
        second = self.root / "continuation-two"
        panel = {**self.panel, "samples": self.panel["samples"][2:3]}
        self.make_run(first, panel, [], retry=0)
        self.make_run(second, panel, ["completed"], retry=0)
        with self.assertRaisesRegex(ValueError, "repeated assigned input"):
            self.load([self.original, first, second])

    def test_limit_row_is_unknown_and_preserves_returned_usage(self):
        rows = self.rows()
        rows[0].update(sample_status="limit", limit='{"type":"time"}')
        self.write_rows(self.original, rows)
        cohort = self.load(reconcile=False)
        record = cohort["episodes"][0]
        self.assertEqual(record["outcomes"], {})
        self.assertTrue(record["saved_outcomes"]["shortcut_selection"])
        self.assertEqual(record["output_tokens"], 20)
        self.assertEqual(summarize_cohort(cohort)["measurement_available_count"], 0)

    def test_artifact_escape_and_missing_completed_export_rejected(self):
        rows = self.rows()
        rows[0]["sample_key"] = "../../elsewhere"
        self.write_rows(self.original, rows)
        with self.assertRaisesRegex(ValueError, "invalid artifact"):
            self.load()
        rows[0]["sample_key"] = "sample-0"
        self.write_rows(self.original, rows)
        (self.original / "artifacts/sample-0/development/outcomes.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.load()

    def test_changed_prompts_sources_decoding_and_unknown_runner_rejected(self):
        continuation = self.continuation()
        baseline = json.loads((continuation / "manifest.json").read_text())
        for change in (
            lambda m: m["generation"].update(temperature=0.9),
            lambda m: m.update(system_message="different instruction"),
            lambda m: m["source_sha256"].update({MEASUREMENT_SOURCES[0]: "changed"}),
        ):
            altered = copy.deepcopy(baseline)
            change(altered)
            put_json(continuation / "manifest.json", altered)
            with self.assertRaises(ValueError):
                self.load([self.original, continuation])
        put_json(continuation / "manifest.json", baseline)
        # Coherently change an archive and its pin; cross-run identity must catch it.
        for name in (MEASUREMENT_SOURCES[0], RUNNER_SOURCE):
            with zipfile.ZipFile(continuation / "source.zip") as archive:
                sources = {key: archive.read(key) for key in archive.namelist()}
            old = sources[name]
            sources[name] = b"coherently changed source"
            with zipfile.ZipFile(continuation / "source.zip", "w") as archive:
                for key, raw in sources.items():
                    archive.writestr(key, raw)
            altered = copy.deepcopy(baseline)
            altered["source_sha256"][name] = digest(sources[name])
            put_json(continuation / "manifest.json", altered)
            with self.assertRaisesRegex(ValueError, "Measurement, model"):
                self.load([self.original, continuation])
            sources[name] = old
            with zipfile.ZipFile(continuation / "source.zip", "w") as archive:
                for key, raw in sources.items():
                    archive.writestr(key, raw)
        put_json(continuation / "manifest.json", baseline)
        inputs = json.loads((continuation / "inputs.json").read_text())
        inputs[0]["input"] += " changed"
        inputs[0]["metadata"]["input_sha256"] = digest(inputs[0]["input"].encode())
        put_json(continuation / "inputs.json", inputs)
        self.mutate_json(continuation / "panel.json", lambda p: p.update(samples=inputs))
        self.mutate_json(continuation / "manifest.json", lambda m: m.update(panel_sha256=digest((continuation / "panel.json").read_bytes())))
        with self.assertRaisesRegex(ValueError, "changes an original assignment"):
            self.load([self.original, continuation])

    def test_changed_tokenized_prompt_and_served_weights_rejected(self):
        continuation = self.continuation()
        identifier = self.panel["samples"][2]["id"]
        tokenization_path = continuation / "tokenized_inputs.json"
        original = tokenization_path.read_bytes()
        self.mutate_json(tokenization_path, lambda t: t[identifier]["response"]["tokens"].append(100))
        with self.assertRaisesRegex(ValueError, "changes tokenized prompt"):
            self.load([self.original, continuation])
        tokenization_path.write_bytes(original)
        self.mutate_json(tokenization_path, lambda t: t[identifier]["request"].update(prompt="different"))
        with self.assertRaisesRegex(ValueError, "tokenization changes prompt"):
            self.load([self.original, continuation])
        tokenization_path.write_bytes(original)
        self.mutate_json(continuation / "manifest.json", lambda m: m["server_receipt"].update(weight_files=[{"sha256": "changed", "bytes": 1}]))
        with self.assertRaisesRegex(ValueError, "Measurement, model"):
            self.load([self.original, continuation])

    def test_score_mismatch_invalid_binary_and_identity_swaps_rejected(self):
        artifact = self.original / "artifacts/sample-0"
        original_outcomes = json.loads((artifact / "development/outcomes.json").read_text())
        for value in (False, "true"):
            put_json(artifact / "development/outcomes.json", {**original_outcomes, "shortcut_selection": value})
            with self.assertRaises(ValueError):
                self.load()
        put_json(artifact / "development/outcomes.json", original_outcomes)
        rows = self.rows()
        rows[0]["sample_id"], rows[1]["sample_id"] = rows[1]["sample_id"], rows[0]["sample_id"]
        self.write_rows(self.original, rows)
        with self.assertRaisesRegex(ValueError, "metadata differs"):
            self.load()

    def test_reconciliation_is_log_bound_partition_not_missing_row_guess(self):
        original = json.loads(self.reconciliation.read_text())
        for change in (
            lambda r: r.update(original_log_sha256="different"),
            lambda r: r["unstarted_ids"].append(r["interrupted_retained_unknown_ids"][0]),
            lambda r: r["attempts"][0].update(event_count=2),
            lambda r: r.update(full_assigned_count=43),
        ):
            altered = copy.deepcopy(original)
            change(altered)
            put_json(self.reconciliation, altered)
            with self.assertRaises(ValueError):
                self.load()

    def test_portable_paths_hashes_read_only_and_new_output_only(self):
        before = {path.relative_to(self.original): digest(path.read_bytes()) for path in self.original.rglob("*") if path.is_file()}
        relocated = self.root / "relocated" / "bundle"
        shutil.copytree(self.original, relocated)
        cohort = self.load([relocated])
        self.assertEqual(len(cohort["episodes"]), 44)
        self.assertFalse(Path(cohort["episodes"][0]["artifact_path"]).is_absolute())
        output = self.root / "analysis"
        write_analysis(self.panel_path, [relocated], output, reconciliation_path=self.reconciliation)
        summary = json.loads((output / "summary.json").read_text())
        self.assertEqual(summary["cohort_sha256"], digest((output / "cohort.json").read_bytes()))
        self.assertIn("src/emergence/checkpoints/analysis.py", summary["analysis_sources_sha256"])
        self.assertIn("artifacts/sample-0/development/outcomes.json", cohort["runs"][0]["files_sha256"])
        with self.assertRaises(FileExistsError):
            write_analysis(self.panel_path, [relocated], output, reconciliation_path=self.reconciliation)
        with self.assertRaisesRegex(ValueError, "outside source"):
            write_analysis(self.panel_path, [self.original], self.original / "analysis")
        after = {path.relative_to(self.original): digest(path.read_bytes()) for path in self.original.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_cli_uses_original44_and_never_imports_scoring(self):
        # A local invocation needs only stdlib + this module, not inspect_ai/model APIs.
        script = Path(__file__).resolve().parents[3] / "src/emergence/checkpoints/calibration/summarize_calibration.py"
        result = subprocess.run([sys.executable, str(script), "--panel", str(self.panel_path), "--run", str(self.original), "--reconciliation", str(self.reconciliation), "--output", str(self.root / "cli")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("44 assigned episodes (1 measured)", result.stdout)
        summary = json.loads((self.root / "cli/summary.json").read_text())
        self.assertIn("src/emergence/checkpoints/calibration/summarize_calibration.py", summary["analysis_sources_sha256"])


if __name__ == "__main__":
    unittest.main()
