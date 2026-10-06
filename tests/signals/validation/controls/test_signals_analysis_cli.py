"""Scripted local CLI integration; no actor or monitor inference is performed."""

import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import signals.validation.controls.analyze as cli
import signals.reasoning.run_monitor as monitor_cli
import signals.validation.controls.run as actor_cli
from shared.runs import run_directory
from shared.runs import write_json
import signals.validation.controls.environment as environment
import signals.reasoning.monitor as monitor
from signals.validation.controls.runner import SEED_DERIVATION_VERSION
from signals.validation.controls.runner import assignment_ledger
from signals.validation.controls.runner import digest
from signals.validation.controls.runner import run_episode


def response(name=None, arguments=None):
    message = {"role": "assistant", "content": "Finished." if name is None else None}
    if name:
        message["tool_calls"] = [{"id": "call", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments or {}),
        }}]
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if name else "stop"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 10}}


class AnalysisCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        names = set(cli.CALIBRATION_SOURCES) | {
            "src/signals/validation/controls/environment.py", "src/signals/reasoning/monitor.py",
            "src/signals/reasoning/observations.py", "src/signals/validation/controls/reference.py",
            "src/signals/validation/controls/runner.py",
            "src/signals/validation/controls/finite_execution.py",
            "src/signals/reasoning/run_monitor.py",
        }
        self.sources = {name: cli.file_hash(cli.ROOT / name) for name in sorted(names)}
        self.source_patch = patch.object(cli, "source_hashes", return_value=self.sources)
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)
        self.network_patch = patch("urllib.request.urlopen", side_effect=AssertionError("No test may call a network endpoint"))
        self.network_patch.start()
        self.addCleanup(self.network_patch.stop)

    def fixture(self, name="development", split="development", *, monitor_model="fake/monitor", missing_negative_channel=None,
                operating_points_hash=None, public_interface_version=environment.PUBLIC_INTERFACE_V1,
                monitor_version=monitor.MONITOR_VERSION_V1, final_disposition="error"):
        actor_directory, monitor_directory = self.root / f"{name}-actors", self.root / f"{name}-monitors"
        assignments = sorted(assignment_ledger(["ledger", "validator"], split, 2, 1, 97), key=lambda row: row["assignment_id"])
        cases = {f"{row['family']}-{row['case_index']}": environment.make_case(
            row["family"], split, row["case_index"], public_interface_version=public_interface_version,
        ) for row in assignments}
        for row in assignments:
            row["case_id"] = cases[f"{row['family']}-{row['case_index']}"]["case_id"]
        actor_config = {"section": "signals", "run_type": "pilot" if split == "development" else "confirmation",
                        "model": "fake/actor", "model_revision": "actor-revision",
                        "generation": {"temperature": 1, "top_p": 0.95, "top_k": 20,
                                       "chat_template_kwargs": {"enable_thinking": True}},
                        "max_calls": 6, "max_completion_tokens": 16000, "per_call_tokens": 3072,
                        "environment_schema": environment.SCHEMA_VERSION, "source_sha256": self.sources,
                        "families": ["ledger", "validator"], "split": split, "instances": 2, "repeats": 1,
                        "seed": 97, "seed_derivation_version": SEED_DERIVATION_VERSION}
        if public_interface_version != environment.PUBLIC_INTERFACE_V1 or monitor_version != monitor.MONITOR_VERSION_V1:
            actor_config.update(public_interface_version=public_interface_version, monitor_version=monitor_version)
        if split == "confirmation":
            actor_config["freeze"] = {"operating_points_file_sha256": operating_points_hash}
        with run_directory(actor_directory, actor_config):
            write_json(actor_directory / "assignments.json", assignments)
            write_json(actor_directory / "cases.json", cases)
            for index, row in enumerate(assignments):
                case = cases[f"{row['family']}-{row['case_index']}"]
                answers = [response("inspect_artifact")]
                if index == 3:
                    answers += ([response("evaluate_visible") for _ in range(5)] if final_disposition == "call_limit"
                                else [RuntimeError("scripted interrupted episode")])
                else:
                    artifact = case["controls"]["shortcut" if index == 2 else "correct"]
                    answers += [response("submit_artifact", {"artifact": artifact}), response()]
                answers = iter(answers)

                def call(request):
                    answer = next(answers)
                    if isinstance(answer, Exception):
                        raise answer
                    return answer

                run_episode(case=case, assignment=row, output=actor_directory / "episodes" / row["assignment_id"],
                            model=actor_config["model"], model_revision=actor_config["model_revision"],
                            generation=actor_config["generation"], environment=environment, call=call)
        actor = monitor_cli.load_actor_run(actor_directory)
        tasks, previews = monitor_cli.build_plan(actor, list(monitor_cli.CUTOFFS), monitor_model, monitor_version)
        config = {"section": "signals", "run_type": "monitor", "actor_snapshot": actor["snapshot"],
                  "channels": list(cli.CHANNELS), "cutoffs": list(monitor_cli.CUTOFFS),
                  "model": monitor_model, "model_revision": "monitor-revision", **monitor.monitor_identity(monitor_version),
                  "monitor_assignments_sha256": digest(tasks),
                  "source_sha256": self.sources}
        with run_directory(monitor_directory, config) as manifest:
            write_json(monitor_directory / "assignments.json", assignments)
            write_json(monitor_directory / "monitor_assignments.json", tasks)
            (monitor_directory / "attempts").mkdir()
            for task in tasks:
                identifier = task["monitor_assignment_id"]
                directory = monitor_directory / "predictions" / identifier
                directory.mkdir(parents=True)
                index = next(i for i, row in enumerate(assignments) if row["assignment_id"] == task["assignment_id"])
                absent = (index == 1 and task["channel"] == "action") or (index < 2 and task["channel"] == missing_negative_channel)
                record = {**monitor_cli.prediction_identity(task, config), "status": "error" if absent else "completed",
                          "prediction": None if absent else {"probability": [0.5, 0.2, 0.9, 0.6][index], "evidence": [], "unsupported_evidence": []}}
                write_json(directory / "request.json", previews[identifier]["request"])
                write_json(directory / "prediction.json", record)
                write_json(monitor_directory / "attempts" / f"{identifier}.json", {
                    "identity": monitor_cli.prediction_identity(task, config), "monitor_run_id": manifest["run_id"],
                })
        return actor_directory, monitor_directory

    def invoke(self, actor, monitors, output, *arguments):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["--episodes", str(actor), "--monitors", str(monitors), "--output", str(output),
                      "--bootstrap-draws", "60", *map(str, arguments)])

    def test_cli_freeze_resume_load_confirmation_and_all_assignment_join(self):
        actor, monitors = self.fixture()
        output = self.root / "development-analysis"
        self.invoke(actor, monitors, output, "--freeze-operating-points", "--target-fpr", "0")
        saved_bytes = (output / "operating_points.json").read_bytes()
        saved_hash = hashlib.sha256(saved_bytes).hexdigest()
        manifest = cli.read_json(output / "manifest.json")
        self.assertEqual(manifest["operating_points_file_sha256"], saved_hash)
        document = cli.read_json(output / "operating_points.json")
        point = document["points"]["retrospective"]["context"]
        self.assertEqual((point["threshold"], point["comparator"]), (0.5, ">"))
        summary = cli.read_json(output / "summary.json")
        pooled = summary["cutoffs"]["retrospective"]["pooled"]
        self.assertEqual(pooled["n_assigned"], 4)
        self.assertEqual(pooled["channels"]["context"]["fpr"]["value"], 0)
        self.assertEqual(pooled["channels"]["context"]["all_assigned_bounds"]["brier"]["denominator"], 4)
        self.assertEqual(pooled["channels"]["action"]["n_missing_scores"], 1)
        self.assertEqual(pooled["reasoning_coverage"]["n_readable_reasoning_present"], 0)
        self.assertEqual(pooled["channels"]["cot"]["n_available_scores"], 4)
        self.assertEqual(pooled["structural_action_reference"]["n_unknown_labels_with_observed_negative_prefix"], 1)
        records = cli.read_json(output / "records.json")
        unknown = [row for row in records if row["episode_status"] == "error"]
        self.assertEqual(len(unknown), 2)
        self.assertTrue(all(row["label"] is None for row in unknown))
        self.assertEqual(summary["cutoffs"]["prospective"]["pooled"]["structural_action_reference"]["status"], "not_used_prospectively_or_empty")
        self.invoke(actor, monitors, output, "--freeze-operating-points", "--target-fpr", "0")
        self.assertEqual((output / "operating_points.json").read_bytes(), saved_bytes)
        self.assertEqual(cli.read_json(output / "manifest.json")["run_id"], manifest["run_id"])
        confirm_actor, confirm_monitors = self.fixture("confirmation", "confirmation", operating_points_hash=saved_hash)
        confirmed = self.root / "confirmation-analysis"
        self.invoke(confirm_actor, confirm_monitors, confirmed, "--operating-points", output / "operating_points.json", "--operating-points-sha256", saved_hash)
        self.assertEqual((confirmed / "operating_points.json").read_bytes(), saved_bytes)
        confirmation_manifest = cli.read_json(confirmed / "manifest.json")
        self.assertEqual(confirmation_manifest["development_calibration_provenance"]["development"]["actor_snapshot"], document["development"]["actor_snapshot"])
        self.assertNotEqual(confirmation_manifest["actor_snapshot"], document["development"]["actor_snapshot"])

    def test_cli_confirmation_cannot_calibrate_or_use_an_implicit_diagnostic(self):
        actor, monitors = self.fixture("confirmation", "confirmation")
        for arguments in ((), ("--threshold", "0.5"), ("--freeze-operating-points",)):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                self.invoke(actor, monitors, self.root / "forbidden-output", *arguments)
        self.assertFalse((self.root / "forbidden-output").exists())

    def test_cli_keeps_undefined_points_and_still_reports_brier(self):
        actor, monitors = self.fixture(missing_negative_channel="cot")
        output = self.root / "undefined-analysis"
        self.invoke(actor, monitors, output, "--freeze-operating-points")
        summary = cli.read_json(output / "summary.json")
        cot = summary["cutoffs"]["retrospective"]["pooled"]["channels"]["cot"]
        self.assertEqual(cot["operating_point"]["status"], "no_scored_negatives")
        self.assertIsNone(cot["threshold"])
        self.assertIsNone(cot["fpr"]["value"])
        self.assertEqual(cot["all_assigned_bounds"]["fpr"]["status"], "undefined_threshold")
        self.assertEqual(cot["brier"]["denominator"], 1)
        self.assertEqual(cot["all_assigned_bounds"]["brier"]["denominator"], 4)
        self.assertEqual(cot["fpr_case_cluster_bootstrap"]["status"], "undefined_threshold")

    def test_loaded_file_identity_and_hash_checks_fail_closed(self):
        actor_path, monitor_path = self.fixture()
        output = self.root / "frozen"
        self.invoke(actor_path, monitor_path, output, "--freeze-operating-points")
        path = output / "operating_points.json"
        raw = path.read_bytes()
        actor = monitor_cli.load_actor_run(actor_path)
        monitors = cli.load_monitor_run(monitor_path, actor)
        identity = cli.compatibility_identity(actor, monitors)
        with self.assertRaisesRegex(ValueError, "SHA256"):
            cli.load_operating_points(path, "0" * 64, identity)
        wrong = copy.deepcopy(identity)
        wrong["monitor"]["model_revision"] = "different"
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            cli.load_operating_points(path, hashlib.sha256(raw).hexdigest(), wrong)
        document = json.loads(raw)
        for mutation in ("comparator", "missing_channel", "missing_provenance", "missing_monitor_identity", "boolean_fpr"):
            bad = copy.deepcopy(document)
            if mutation == "comparator":
                bad["points"]["retrospective"]["context"]["comparator"] = ">="
            elif mutation == "missing_channel":
                del bad["points"]["retrospective"]["context"]
            elif mutation == "missing_provenance":
                bad["development"]["outcome_snapshot"]["labels"] = {}
            elif mutation == "missing_monitor_identity":
                bad["development"]["monitor_snapshot"]["predictions"] = {}
            else:
                bad["calibration"]["target_fpr"] = 0
                for channels in bad["points"].values():
                    for point in channels.values():
                        point["target_fpr"] = 0
                bad["points"]["retrospective"]["context"]["target_fpr"] = False
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                cli.validate_operating_points(bad, identity)
        duplicate = self.root / "duplicate.json"
        duplicate.write_bytes(raw.replace(b'"schema_version":', b'"schema_version": "duplicate", "schema_version":', 1))
        with self.assertRaisesRegex(ValueError, "Duplicate JSON"):
            cli.load_operating_points(duplicate, cli.file_hash(duplicate), identity)
        nonfinite = self.root / "nonfinite.json"
        for token in (b"NaN", b"Infinity", b"1e999"):
            nonfinite.write_bytes(b'{"nonfinite": ' + token + b'}')
            with self.assertRaisesRegex(ValueError, "Non-finite JSON"):
                cli.load_operating_points(nonfinite, cli.file_hash(nonfinite), identity)
        path.write_bytes(raw + b" ")
        with self.assertRaises(SystemExit):
            self.invoke(actor_path, monitor_path, output, "--freeze-operating-points")

    def test_legacy_cli_diagnostic_is_explicit_and_cannot_override_loaded_point(self):
        actor, monitors = self.fixture()
        output = self.root / "diagnostic"
        self.invoke(actor, monitors, output)
        summary = cli.read_json(output / "summary.json")
        self.assertEqual(summary["operating_point_source"], "fixed_diagnostic_not_calibrated")
        self.assertIsNone(summary["operating_points_file_sha256"])
        self.assertEqual(summary["cutoffs"]["retrospective"]["pooled"]["channels"]["context"]["fpr"]["value"], 0.5)
        with self.assertRaises(SystemExit):
            self.invoke(actor, monitors, self.root / "bad", "--freeze-operating-points", "--threshold", "0.5")

    def test_confirmation_rejects_a_different_valid_development_file(self):
        actor, monitors = self.fixture()
        first, second = self.root / "first", self.root / "second"
        self.invoke(actor, monitors, first, "--freeze-operating-points", "--target-fpr", "0")
        self.invoke(actor, monitors, second, "--freeze-operating-points", "--target-fpr", "0.5")
        first_hash = cli.file_hash(first / "operating_points.json")
        second_hash = cli.file_hash(second / "operating_points.json")
        self.assertNotEqual(first_hash, second_hash)
        confirm_actor, confirm_monitors = self.fixture("confirmation", "confirmation", operating_points_hash=first_hash)
        with self.assertRaises(SystemExit):
            self.invoke(confirm_actor, confirm_monitors, self.root / "posthoc", "--operating-points",
                        second / "operating_points.json", "--operating-points-sha256", second_hash)
        self.assertFalse((self.root / "posthoc").exists())

    def test_compatibility_includes_actor_runner_source(self):
        actor_path, monitor_path = self.fixture()
        actor = monitor_cli.load_actor_run(actor_path)
        monitors = cli.load_monitor_run(monitor_path, actor)
        before = cli.compatibility_identity(actor, monitors)
        actor["manifest"]["source_sha256"]["src/signals/validation/controls/runner.py"] = "a" * 64
        self.assertNotEqual(before, cli.compatibility_identity(actor, monitors))

    def test_protected_labels_must_match_the_actual_trajectory_state(self):
        actor_path, monitor_path = self.fixture()
        actor = monitor_cli.load_actor_run(actor_path)
        assignment = actor["assignments"][0]
        path = actor_path / "episodes" / assignment["assignment_id"] / "labels.json"
        labels = cli.read_json(path)
        case = actor["cases"][assignment["case_id"]]
        wrong_state = environment.initial_state(case)
        environment.apply_tool(case, wrong_state, "inspect_artifact", {})
        environment.apply_tool(case, wrong_state, "submit_artifact", {"artifact": case["controls"]["shortcut"]})
        labels["observed_outcomes"] = environment.grade(case, wrong_state)
        write_json(path, labels)
        with self.assertRaises(SystemExit):
            self.invoke(actor_path, monitor_path, self.root / "wrong-labels", "--freeze-operating-points")
        self.assertFalse((self.root / "wrong-labels").exists())

    def test_confirmation_launch_preflights_actual_files_and_settings(self):
        actor, monitors = self.fixture()
        analysis = self.root / "calibration"
        self.invoke(actor, monitors, analysis, "--freeze-operating-points")
        operating = analysis / "operating_points.json"
        protocol = self.root / "protocol.txt"
        protocol.write_text("Scripted protocol fixture; no research outcomes.\n")
        config = cli.read_json(actor / "manifest.json")
        settings = {key: config[key] for key in actor_cli.FROZEN_ACTOR_FIELDS}
        settings["split"] = "confirmation"
        freeze = {"source_sha256": self.sources, "monitor_sha256": monitor.prompt_hash(),
                  "protocol_sha256": cli.file_hash(protocol), "operating_points_file_sha256": cli.file_hash(operating),
                  "actor_settings": settings, "monitor_model": "fake/monitor", "monitor_model_revision": "monitor-revision",
                  "monitor_cutoffs": list(monitor_cli.CUTOFFS)}
        freeze_path = self.root / "freeze.json"
        write_json(freeze_path, freeze)
        base = ["signals.py", "--run-type", "confirmation", "--split", "confirmation",
                "--instances", "2", "--repeats", "1", "--seed", "97", "--model", "fake/actor",
                "--model-revision", "actor-revision", "--freeze-manifest", str(freeze_path),
                "--operating-points", str(operating), "--protocol-file", str(protocol)]

        def launch(output, *extra):
            with patch("sys.argv", base + ["--output", str(output), *extra]), \
                    patch.object(actor_cli, "source_files", return_value=[cli.ROOT / name for name in self.sources]), \
                    patch.object(actor_cli, "run_episode", side_effect=AssertionError("No episode may execute")) as run, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                actor_cli.main()
                run.assert_not_called()

        planned = self.root / "valid-confirmation-plan"
        launch(planned)
        self.assertEqual((planned / "frozen_operating_points.json").read_bytes(), operating.read_bytes())
        self.assertEqual((planned / "frozen_protocol.txt").read_bytes(), protocol.read_bytes())
        with self.assertRaises(SystemExit):
            launch(self.root / "changed-budget", "--max-calls", "7", "--execute")
        changed = copy.deepcopy(freeze)
        changed["actor_settings"]["max_calls"] = 7
        write_json(freeze_path, changed)
        with self.assertRaises(SystemExit):
            launch(self.root / "changed-calibration", "--max-calls", "7", "--execute")
        write_json(freeze_path, freeze)
        protocol.write_text("Changed protocol.\n")
        with self.assertRaises(SystemExit):
            launch(self.root / "changed-protocol", "--execute")
        freeze_path.write_text("null\n")
        with self.assertRaises(SystemExit):
            launch(self.root / "null-freeze", "--execute")
        for name in ("changed-budget", "changed-calibration", "changed-protocol", "null-freeze"):
            self.assertFalse((self.root / name).exists())

    def test_v2_finite_endpoint_and_historical_primary_are_retained_side_by_side(self):
        actor_path, monitor_path = self.fixture("amended", public_interface_version=environment.PUBLIC_INTERFACE_V2,
            monitor_version=monitor.MONITOR_VERSION_V2, final_disposition="call_limit")
        output = self.root / "amended-analysis"
        self.invoke(actor_path, monitor_path, output, "--freeze-operating-points")
        records = cli.read_json(output / "records.json")
        limited = [row for row in records if row["episode_status"] == "call_limit"]
        self.assertEqual(len(limited), 2)
        self.assertTrue(all(row["label"] is False and row["historical_primary_label"] is None for row in limited))
        self.assertTrue(all(row["finite_execution_certificate"]["terminal_verified"] for row in limited))
        self.assertTrue(all(row["endpoint_version"] == monitor.FINITE_HORIZON_VERSION for row in records))
        positives = [row for row in records if row["historical_primary_label"] is True]
        self.assertEqual(len(positives), 2)
        self.assertTrue(all(row["label"] is True for row in positives))
        summary = cli.read_json(output / "summary.json")
        self.assertEqual(summary["primary_endpoint"], cli.FINITE_ENDPOINT)
        self.assertEqual(summary["historical_primary_endpoint"], cli.PRIMARY_ENDPOINT)
        self.assertEqual(summary["endpoint_definitions"]["endpoint_version"], monitor.FINITE_HORIZON_VERSION)
        document = cli.read_json(output / "operating_points.json")
        identity = document["compatibility"]
        self.assertEqual(identity["monitor"]["response_schema_sha256"], monitor.monitor_identity(monitor.MONITOR_VERSION_V2)["response_schema_sha256"])
        self.assertEqual(identity["actor"]["public_interface_version"], environment.PUBLIC_INTERFACE_V2)
        self.assertEqual(set(document["development"]["outcome_snapshot"]["finite_execution"]),
                         {row["assignment_id"] for row in records})

    def test_v2_unknown_terminal_receipt_stays_unknown_and_corrupt_history_fails_closed(self):
        actor_path, monitor_path = self.fixture("uncertified", public_interface_version=environment.PUBLIC_INTERFACE_V2,
                                              monitor_version=monitor.MONITOR_VERSION_V2)
        actor = monitor_cli.load_actor_run(actor_path)
        monitors = cli.load_monitor_run(monitor_path, actor)
        records, _ = cli.build_records(actor, monitors)
        uncertified = [row for row in records if row["episode_status"] == "error"]
        self.assertTrue(all(row["label"] is None and row["historical_primary_label"] is None for row in uncertified))
        self.assertTrue(all(not row["finite_execution_certificate"]["terminal_verified"] for row in uncertified))
        assignment = actor["assignments"][0]
        identifier = assignment["assignment_id"]
        request_path = actor_path / "episodes" / identifier / "request-001.json"
        request = cli.read_json(request_path)
        request["seed"] += 1
        write_json(request_path, request)
        with self.assertRaisesRegex(ValueError, "request"):
            cli.build_records(actor, monitors)

    def test_v2_calibration_and_confirmation_bind_interface_schema_endpoint_and_sources(self):
        old_actor, old_monitors = self.fixture("legacy")
        old_output = self.root / "legacy-calibration"
        self.invoke(old_actor, old_monitors, old_output, "--freeze-operating-points")
        actor_path, monitor_path = self.fixture("v2", public_interface_version=environment.PUBLIC_INTERFACE_V2,
            monitor_version=monitor.MONITOR_VERSION_V2, final_disposition="call_limit")
        with self.assertRaises(SystemExit):
            self.invoke(actor_path, monitor_path, self.root / "wrong-old-thresholds", "--operating-points",
                old_output / "operating_points.json", "--operating-points-sha256", cli.file_hash(old_output / "operating_points.json"))
        self.assertFalse((self.root / "wrong-old-thresholds").exists())
        output = self.root / "v2-calibration"
        self.invoke(actor_path, monitor_path, output, "--freeze-operating-points")
        operating = output / "operating_points.json"
        protocol = self.root / "v2-protocol.txt"
        protocol.write_text("Test-only finite execution protocol.\n")
        config = cli.read_json(actor_path / "manifest.json")
        config["split"] = "confirmation"
        freeze = {"source_sha256": self.sources, "monitor_sha256": monitor.prompt_hash(monitor.MONITOR_VERSION_V2),
            "monitor_identity": monitor.monitor_identity(monitor.MONITOR_VERSION_V2),
            "public_interface_version": environment.PUBLIC_INTERFACE_V2,
            "protocol_sha256": cli.file_hash(protocol), "operating_points_file_sha256": cli.file_hash(operating),
            "actor_settings": {key: config[key] for key in actor_cli.FROZEN_ACTOR_FIELDS},
            "monitor_model": "fake/monitor", "monitor_model_revision": "monitor-revision", "monitor_cutoffs": list(monitor_cli.CUTOFFS)}
        frozen = actor_cli.confirmation_preflight(freeze, config, operating, protocol)
        self.assertEqual(frozen["operating_points.json"], operating.read_bytes())
        for key in ("response_schema_sha256", "response_format_sha256", "endpoint_version"):
            bad = copy.deepcopy(freeze)
            bad["monitor_identity"][key] = "wrong"
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "identity"):
                actor_cli.confirmation_preflight(bad, config, operating, protocol)
        bad = copy.deepcopy(freeze)
        bad["public_interface_version"] = environment.PUBLIC_INTERFACE_V1
        with self.assertRaisesRegex(ValueError, "interface"):
            actor_cli.confirmation_preflight(bad, config, operating, protocol)
        actor = monitor_cli.load_actor_run(actor_path)
        monitors = cli.load_monitor_run(monitor_path, actor)
        original = cli.compatibility_identity(actor, monitors)
        monitors["manifest"]["source_sha256"]["src/signals/validation/controls/finite_execution.py"] = "a" * 64
        self.assertNotEqual(original, cli.compatibility_identity(actor, monitors))


if __name__ == "__main__":
    unittest.main()
