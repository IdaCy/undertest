import copy
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter, defaultdict
from pathlib import Path
from unittest.mock import patch

import emergence.checkpoints.fresh_registry as fresh_registry
from emergence.checkpoints.choice import ASSAY_VERSION as LEGACY_ASSAY_VERSION
from emergence.checkpoints.choice import CHOICE_IDS
from emergence.checkpoints.choice import CONDITIONS
from emergence.checkpoints.choice import KINDS
from emergence.checkpoints.choice import build_panel as build_legacy_panel
from emergence.checkpoints.choice import score_choice
from emergence.checkpoints.evidence import extract_evidence
from emergence.checkpoints.fresh_choice import ANCHOR_CUE
from emergence.checkpoints.fresh_choice import build_panel
from emergence.checkpoints.fresh_choice import register_panel
from emergence.checkpoints.fresh_registry import REGISTRY_SHA256
from emergence.checkpoints.fresh_registry import SOURCE_SHA256
from emergence.checkpoints.fresh_registry import load_registry
from emergence.checkpoints.fresh_registry import validate_registry
from emergence.checkpoints.provenance import load_panel
from emergence.checkpoints.response import score_response


ROOT = Path(__file__).resolve().parents[3]


def catalogue_directory():
    override = os.environ.get("TRAINDEV_FRESH_V2_DIRECTORY")
    if override:
        return Path(override)
    raise unittest.SkipTest("Pinned catalogue fixtures require TRAINDEV_FRESH_V2_DIRECTORY")


class LegacyExactRegressionTests(unittest.TestCase):
    def test_preintegration_panels_and_score_outputs_match_exactly(self):
        cases = [
            ({}, "18b127d2375adf46376f245a650161d4b02803b778789e70edb49b8603bf2521", "ac1d7bb260d72c13e8aec6f0cdc3caa63aea463c4c81d3d6b37dfb7a80ea61e9"),
            ({"repetitions": 1, "seed": 17, "include_controls": False}, "d5404868a3040d80327afd7609e6541d6f4b0e3841f66a83972205b0fdaa4a4e", "bb56478f24d53238767dff3f8fc7e4798a34296502c1a6a74842ec5ed97fd49c"),
            ({"repetitions": 3, "seed": 0, "cue_variant": "paraphrase", "conditions": list(CONDITIONS)[::-1], "instance_ids": ["boolean_permission_001", "numeric_squares_001"]}, "382e5771e6c67d5afe4af30d36048fd4b7f54a29b92282489572c934d4eeb2e7", "e0b22790711ad5d232609c92254d298da497eef4ee24e141c67e602a661a63da"),
        ]
        for config, panel_digest, scores_digest in cases:
            panel = build_legacy_panel(**config)
            raw = json.dumps(panel, ensure_ascii=False, indent=2) + "\n"
            self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(), panel_digest)
            outputs = [
                [sample["id"], text, score_choice(sample["metadata"], text)]
                for sample in panel["samples"]
                for text in ["", "ACTION: missing", "ACTION: K7\nACTION: M2"] + [f"Working.\nACTION: {identifier}" for identifier in sample["metadata"]["choices"]]
            ]
            self.assertEqual(hashlib.sha256(json.dumps(outputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), scores_digest)


class FreshChoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = catalogue_directory()
        cls.registry = load_registry(cls.directory)
        cls.panel = build_panel(cls.registry, repetitions=2, seed=20261001, rule_split="confirmation_core", cue_variants=[ANCHOR_CUE, "heldout_rewording_a", "heldout_rewording_b"])

    def setUp(self):
        register_panel(self.panel)

    def test_full_immutable_exhaustive_evidence_and_count_limits(self):
        report = validate_registry(self.registry)
        self.assertEqual(report["offered_candidate_evaluations"], 8256)
        self.assertEqual(report["outside_menu_candidate_evaluations"], 2064)
        self.assertEqual(report["split_rule_counts"], {"confirmation_core": 16, "rule_transfer": 8})
        self.assertEqual(report["split_dependence_groups"], {"confirmation_core": 8, "rule_transfer": 5})
        self.assertEqual(report["count_limits"]["named_functions"], 24)
        self.assertEqual(report["count_limits"]["nominal_function_shortfall"], 16)
        self.assertEqual({row["rule_id"] for row in report["compact_honest_outside_menu"]}, {"distinct_count", "at_least_three", "exactly_one", "exactly_two"})
        compiled = json.loads((self.directory / "compiled_rules_v2.json").read_text())
        for raw, rule in zip(compiled["rules"], self.registry["rules"]):
            for key in ("id", "family", "rule_id", "cluster_id", "split", "requirement", "candidates", "reference_outputs", "visible_cases"):
                self.assertEqual(rule[key], raw[key])
            self.assertNotIn("design_rationale", rule)
            for original, alternative in zip(raw["out_of_menu_alternatives"], rule["out_of_menu_alternatives"]):
                self.assertEqual(alternative, {key: value for key, value in original.items() if key != "purpose"})
        self.assertEqual(len({rule["cluster_id"] for rule in self.registry["rules"]}), 13)

    def test_registry_rejects_changes_before_any_source_execution(self):
        for field, value in (("source", "def solve(x):\n    return 999\n"), ("domain_pass_bits", "0" * 156), ("robust_score", True)):
            registry = copy.deepcopy(self.registry)
            registry["rules"][0]["candidates"][0][field] = value
            with self.subTest(field=field), patch.object(fresh_registry, "_owned_function", side_effect=AssertionError("source executed")), self.assertRaisesRegex(ValueError, "modified fresh registry"):
                validate_registry(registry)

    def test_file_provenance_must_match_all_four_pinned_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name in SOURCE_SHA256:
                (directory / name).write_bytes((self.directory / name).read_bytes())
            for name in SOURCE_SHA256:
                original = (directory / name).read_bytes()
                (directory / name).write_bytes(original + b"\n")
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Frozen input hash"):
                    load_registry(directory)
                (directory / name).write_bytes(original)

    def test_deterministic_factorial_pairing_unique_ids_and_balanced_labels(self):
        self.assertEqual(self.panel, build_panel(self.registry, **self.panel["build_config"]))
        samples = self.panel["samples"]
        self.assertEqual(len(samples), 512)
        self.assertEqual(len({sample["id"] for sample in samples}), len(samples))
        paired = defaultdict(list)
        seeds = defaultdict(set)
        for sample in samples:
            metadata = sample["metadata"]
            self.assertEqual(metadata["input_sha256"], hashlib.sha256(sample["input"].encode()).hexdigest())
            if metadata["sample_kind"] == "main":
                paired[(metadata["instance_id"], metadata["replicate"])].append(sample)
                seeds[metadata["instance_id"]].add(metadata["seed"])
        for group in paired.values():
            self.assertEqual(len(group), 12)
            self.assertEqual(len({row["metadata"]["seed"] for row in group}), 1)
            self.assertEqual(len({json.dumps(row["metadata"]["choices"], sort_keys=True) for row in group}), 1)
            self.assertEqual(len({row["input"].split("Choose one supplied implementation", 1)[1] for row in group}), 1)
        self.assertTrue(all(len(values) == 2 for values in seeds.values()))
        self.assertTrue(self.panel["assignment"]["all_main_labels_balanced"])
        for row in self.panel["assignment"]["label_balance"]:
            self.assertEqual(len(set(row["counts"].values())), 1)

    def test_reserved_rule_and_cue_population_identifiers_remain_distinct(self):
        transfer = build_panel(self.registry, repetitions=1, seed=20261001, rule_split="rule_transfer", cue_variants=[ANCHOR_CUE, "heldout_rewording_a"])
        self.assertEqual(transfer["assignment"]["selected_rules"], 8)
        self.assertEqual(transfer["assignment"]["selected_dependence_groups"], 5)
        self.assertEqual({sample["metadata"]["split"] for sample in transfer["samples"] if sample["metadata"]["sample_kind"] == "main"}, {"rule_transfer", "joint_transfer"})
        self.assertEqual({sample["metadata"]["split"] for sample in self.panel["samples"] if sample["metadata"]["sample_kind"] == "main"}, {"confirmation_core", "cue_transfer"})
        self.assertFalse(set(transfer["build_config"]["rule_ids"]) & set(self.panel["build_config"]["rule_ids"]))
        clusters = defaultdict(set)
        for rule in self.registry["rules"]:
            clusters[rule["cluster_id"]].add(rule["split"])
        self.assertTrue(all(len(splits) == 1 for splits in clusters.values()))

    def test_rule_subset_keeps_existing_mappings_and_generation_seeds(self):
        ids = [rule["id"] for rule in self.registry["rules"] if rule["split"] == "confirmation_core" and rule["family"] == "numeric_transform"][:4]
        subset = build_panel(self.registry, repetitions=2, seed=20261001, rule_split="confirmation_core", cue_variants=[ANCHOR_CUE], rule_ids=ids)
        existing = {sample["id"]: sample for sample in self.panel["samples"]}
        self.assertTrue(subset["assignment"]["all_main_labels_balanced"])
        self.assertTrue(all(sample == existing[sample["id"]] for sample in subset["samples"]))

    def test_no_implicit_allocation_and_invalid_configuration(self):
        base = dict(repetitions=1, seed=0, rule_split="confirmation_core", cue_variants=[ANCHOR_CUE])
        for changes in ({"repetitions": True}, {"repetitions": 0}, {"seed": -1}, {"seed": True}, {"rule_split": "training"}, {"cue_variants": []}, {"cue_variants": [ANCHOR_CUE, ANCHOR_CUE]}, {"cue_variants": ["paraphrase"]}, {"rule_ids": []}, {"rule_ids": [self.registry["rules"][0]["id"]]}, {"include_controls": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_panel(self.registry, **{**base, **changes})
        with self.assertRaises(TypeError):
            build_panel(self.registry)
        self.assertEqual(self.panel["allocation_status"], "unallocated")

    def test_host_only_reference_and_outside_menu_evidence_not_rendered(self):
        for sample in self.panel["samples"]:
            metadata = sample["metadata"]
            prompt = sample["input"]
            for key in ("registry_sha256", "reference_sha256", "domain_pass_bits", "weak_proxy_score", "out_of_menu_alternatives", "cluster_id"):
                self.assertNotIn(key, prompt)
            for alternative in metadata["out_of_menu_alternatives"]:
                self.assertNotIn(alternative["source"], prompt)
            if metadata["sample_kind"] == "main":
                for candidate in metadata["choices"].values():
                    self.assertEqual(prompt.count(candidate["source"]), 1)

    def test_all_rule_candidates_scored_through_runner_dispatch(self):
        transfer = build_panel(self.registry, repetitions=1, seed=101, rule_split="rule_transfer", cue_variants=[ANCHOR_CUE])
        register_panel(transfer)
        for panel in (self.panel, transfer):
            for sample in panel["samples"]:
                metadata = sample["metadata"]
                for identifier, candidate in metadata["choices"].items():
                    score = score_choice(metadata, f"Working.\nACTION: {identifier}")
                    self.assertTrue(score["behavior_observed"])
                    if metadata["sample_kind"] == "format":
                        self.assertIsNone(score["task_success"])
                        self.assertIsNone(score["shortcut_selection"])
                    else:
                        self.assertEqual(score["task_success"], candidate["kind"].startswith("correct_"))
                        self.assertEqual(score["proxy_exploitation"], candidate["kind"] == "shortcut")
                        self.assertEqual(score["weak_proxy_success"], candidate["kind"] == "shortcut")
                        self.assertEqual(score["ordinary_bug_selection"], candidate["kind"] == "ordinary_bug")
                    if metadata["sample_kind"] != "main":
                        self.assertEqual(score["diagnostic_success"], identifier in metadata["expected_choice_ids"])

    def test_controls_cover_neutral_competence_both_scoring_memos_and_format(self):
        by_rule = defaultdict(Counter)
        for sample in self.panel["samples"]:
            metadata = sample["metadata"]
            if metadata["sample_kind"] != "main":
                by_rule[metadata["instance_id"]][metadata["condition"]] += 1
            if metadata["sample_kind"] == "competence":
                self.assertIsNone(metadata["grading"])
                self.assertIsNone(metadata["framing"])
                self.assertNotIn("Dashboard description:", sample["input"])
        self.assertTrue(all(counts == {"control_competence": 1, "control_format": 1, "control_score_limited": 3, "control_score_broad": 3} for counts in by_rule.values()))

    def test_registration_required_and_not_mutated_by_callers(self):
        metadata = next(sample["metadata"] for sample in self.panel["samples"] if sample["metadata"]["sample_kind"] == "main")
        with patch.dict(fresh_registry._REGISTERED, {}, clear=True), self.assertRaisesRegex(ValueError, "explicitly registered"):
            score_choice(metadata, "ACTION: K7")
        panel = copy.deepcopy(self.panel)
        register_panel(panel)
        panel["registry"]["rules"][0]["candidates"][0]["robust_score"] = 99
        self.assertTrue(score_choice(metadata, "ACTION: K7")["format_valid"])

    def test_panel_preflight_rejects_prompt_metadata_registry_and_missing_cells(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "panel.json"
            path.write_text(json.dumps(self.panel))
            loaded, digest = load_panel(path)
            self.assertEqual(loaded, self.panel)
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
            mutations = [
                lambda panel: panel["samples"][0].update(input=panel["samples"][0]["input"] + "changed"),
                lambda panel: panel["samples"][0]["metadata"].update(seed=0),
                lambda panel: panel["samples"][0]["metadata"].update(registry_sha256="0" * 64),
                lambda panel: panel["samples"].pop(),
                lambda panel: panel["registry"]["rules"][0].update(split="rule_transfer"),
                lambda panel: panel.update(registry_sha256="0" * 64),
                lambda panel: panel.update(assay_version="finite-program-choice-v1-provisional"),
            ]
            for index, mutate in enumerate(mutations):
                panel = copy.deepcopy(self.panel)
                mutate(panel)
                path.write_text(json.dumps(panel))
                with self.subTest(index=index), self.assertRaises(ValueError):
                    load_panel(path)

    def test_scoring_rejects_evidence_and_assignment_tampering_even_for_missing_output(self):
        original = next(sample["metadata"] for sample in self.panel["samples"] if sample["metadata"]["sample_kind"] == "main")
        for key, value in (("seed", 0), ("cluster_id", "renamed"), ("grading", "broad" if original["grading"] == "limited" else "limited"), ("input_sha256", "0" * 64)):
            metadata = {**original, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                score_choice(metadata, "")
        metadata = copy.deepcopy(original)
        metadata["choices"]["K7"]["source"] += "\n"
        with self.assertRaises(ValueError):
            score_choice(metadata, "ACTION: K7")

    def test_fresh_assay_alias_rejects_empty_output_and_forged_format_target_before_parsing(self):
        main = next(row["metadata"] for row in self.panel["samples"] if row["metadata"]["sample_kind"] == "main")
        control = next(row["metadata"] for row in self.panel["samples"] if row["metadata"]["sample_kind"] == "format" and row["metadata"]["expected_choice_ids"] == ["R8"])
        for metadata, text in (
            ({**main, "assay_version": LEGACY_ASSAY_VERSION}, ""),
            ({**control, "assay_version": LEGACY_ASSAY_VERSION, "expected_choice_ids": ["K7"]}, "ACTION: K7"),
        ):
            with self.subTest(kind=metadata["sample_kind"]), patch("emergence.checkpoints.choice._parse_choice", side_effect=AssertionError("parser reached")), self.assertRaisesRegex(ValueError, "namespace"):
                score_choice(metadata, text)

    def test_individual_namespace_markers_and_removed_labels_cannot_select_legacy(self):
        original = next(row["metadata"] for row in self.panel["samples"] if row["metadata"]["sample_kind"] == "format")
        legacy = next(row["metadata"] for row in build_legacy_panel()["samples"] if row["metadata"]["sample_kind"] == "format")
        markers = ("registry", "registry_id", "registry_sha256", "cluster_id", "base_seed", "rule_split", "cue_split", "shortcut_value", "out_of_menu_alternatives", "has_compact_honest_outside_menu")
        for marker in markers:
            metadata = {**legacy, marker: original.get(marker)}
            with self.subTest(marker=marker), self.assertRaisesRegex(ValueError, "namespace"):
                score_choice(metadata, "")
        stripped = {key: value for key, value in original.items() if key not in markers}
        stripped.update(assay_version=LEGACY_ASSAY_VERSION, expected_choice_ids=["K7"])
        for text in ("", "ACTION: K7"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "legacy scoring namespace"):
                score_choice(stripped, text)
        for changes in ({"assay_version": "unknown"}, {"registry_id": None}, {"registry_sha256": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                score_choice({**original, **changes}, "")

    def test_panel_assay_consistency_and_incomplete_fresh_identity_fail_closed(self):
        legacy_keys = set().union(*(row["metadata"] for row in build_legacy_panel()["samples"]))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "panel.json"
            for mutation in ("reviewer", "sample_registry_only", "strip_all_labels", "unknown_assay", "legacy_with_registry_id"):
                panel = copy.deepcopy(self.panel)
                if mutation == "legacy_with_registry_id":
                    panel = build_legacy_panel()
                    panel["registry_id"] = "fresh-rule-registry-v2"
                elif mutation == "unknown_assay":
                    panel["assay_version"] = "unregistered-assay"
                else:
                    panel["assay_version"] = LEGACY_ASSAY_VERSION
                    panel.pop("registry")
                    panel.pop("registry_sha256")
                    for sample in panel["samples"]:
                        sample["metadata"].pop("registry_sha256")
                        if mutation == "sample_registry_only":
                            sample["metadata"]["assay_version"] = LEGACY_ASSAY_VERSION
                        if mutation == "strip_all_labels":
                            sample["metadata"] = {key: value for key, value in sample["metadata"].items() if key in legacy_keys}
                            sample["metadata"]["assay_version"] = LEGACY_ASSAY_VERSION
                path.write_text(json.dumps(panel))
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    load_panel(path)

    def test_mixed_assay_runner_rejects_before_any_external_operation_or_output_directory(self):
        spec = importlib.util.spec_from_file_location("fresh_namespace_runner", ROOT / "src/emergence/checkpoints/calibration/run_panel.py")
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
        panel = copy.deepcopy(self.panel)
        panel["assay_version"] = LEGACY_ASSAY_VERSION
        panel.pop("registry")
        panel.pop("registry_sha256")
        for sample in panel["samples"]:
            sample["metadata"].pop("registry_sha256")
        checkpoint = {"checkpoint_id": "offline", "model_id": "offline/fixture", "model_revision": "a" * 40, "tokenizer_id": "offline/fixture", "tokenizer_revision": "b" * 40, "stage": "offline", "lineage": "offline"}
        receipt = {**checkpoint, "served_model": "offline-" + "a" * 12, "hostname": "offline", "precision": "bfloat16", "server_version": "fixture", "launch_argv": ["fixture"], "verified_utc": "2026-09-30T00:00:00Z"}
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for name, value in (("panel.json", panel), ("lineage.json", {"checkpoints": [checkpoint]}), ("receipt.json", receipt)):
                (directory / name).write_text(json.dumps(value))
            output = directory / "must_not_exist"
            argv = ["run_panel.py", "--panel", str(directory / "panel.json"), "--lineage", str(directory / "lineage.json"), "--checkpoint", "offline", "--server-receipt", str(directory / "receipt.json"), "--output", str(output)]
            with (
                patch.object(sys, "argv", argv),
                patch.dict(fresh_registry._REGISTERED, {}, clear=True),
                patch.object(runner, "check_live_server", side_effect=AssertionError("live server reached")) as live,
                patch.object(runner, "tokenize_inputs", side_effect=AssertionError("tokenization reached")) as tokenize,
                patch.object(runner, "eval_set", side_effect=AssertionError("generation reached")) as generate,
                patch("socket.socket.connect", side_effect=AssertionError("network reached")) as network,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as stopped,
            ):
                runner.main()
            self.assertEqual(stopped.exception.code, 2)
            for boundary in (live, tokenize, generate, network):
                boundary.assert_not_called()
            self.assertFalse(output.exists())

    def test_direct_registration_requires_payload_and_pinned_digest(self):
        for field in ("registry", "registry_sha256"):
            panel = {key: value for key, value in self.panel.items() if key != field}
            with self.subTest(field=field), patch("emergence.checkpoints.fresh_choice.build_panel", side_effect=AssertionError("construction reached")), self.assertRaisesRegex(ValueError, "pinned registry payload and digest"):
                register_panel(panel)

    def test_genuine_legacy_panels_and_controls_still_load_after_fresh_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.json"
            for options in ({}, {"seed": 17, "repetitions": 1, "include_controls": False}, {"cue_variant": "paraphrase", "instance_ids": ["numeric_squares_001"]}):
                panel = build_legacy_panel(**options)
                path.write_text(json.dumps(panel))
                self.assertEqual(load_panel(path)[0], panel)
                for sample in panel["samples"]:
                    self.assertFalse(score_choice(sample["metadata"], "")["behavior_observed"])
                    if sample["metadata"]["sample_kind"] == "format":
                        target = sample["metadata"]["expected_choice_ids"][0]
                        self.assertTrue(score_choice(sample["metadata"], f"ACTION: {target}")["diagnostic_success"])

    def test_legacy_engineering_panel_kinds_preserve_exact_inputs(self):
        original = build_legacy_panel()
        diagnostics = [row for row in original["samples"] if row["metadata"]["sample_kind"] != "main"]
        format_sample = next(row for row in diagnostics if row["metadata"]["sample_kind"] == "format")
        cases = (
            ("diagnostic", diagnostics),
            ("diagnostic", diagnostics[:1]),
            ("diagnostic", diagnostics[1:]),
            ("smoke", [format_sample]),
            ("calibration", original["samples"][5:]),
            (None, original["samples"]),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "panel.json"
            for kind, samples in cases:
                panel = {**original, "panel_kind": kind, "samples": samples}
                raw = json.dumps(panel, ensure_ascii=False, indent=2) + "\n"
                path.write_text(raw)
                with self.subTest(kind=kind, samples=len(samples)):
                    loaded, digest = load_panel(path)
                    self.assertEqual(loaded, panel)
                    self.assertEqual(digest, hashlib.sha256(raw.encode()).hexdigest())
                    self.assertEqual(path.read_text(), raw)

    def test_engineering_panel_kinds_cannot_alias_fresh_namespaces(self):
        legacy = build_legacy_panel()
        legacy_keys = set().union(*(row["metadata"] for row in legacy["samples"]))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "panel.json"
            for kind in ("diagnostic", "smoke"):
                for mutation in ("top_registry", "fresh_builder", "sample_registry", "fresh_assay", "stripped_fresh_fixture"):
                    panel = copy.deepcopy(legacy)
                    panel["panel_kind"] = kind
                    if mutation == "top_registry":
                        panel["registry_id"] = "fresh-rule-registry-v2"
                    elif mutation == "fresh_builder":
                        panel["build_config"]["rule_split"] = "confirmation_core"
                    elif mutation == "sample_registry":
                        panel["samples"][0]["metadata"]["registry_id"] = "fresh-rule-registry-v2"
                    else:
                        sample = copy.deepcopy(self.panel["samples"][0])
                        if mutation == "stripped_fresh_fixture":
                            sample["metadata"] = {key: value for key, value in sample["metadata"].items() if key in legacy_keys}
                            sample["metadata"]["assay_version"] = LEGACY_ASSAY_VERSION
                        panel["samples"] = [sample]
                    path.write_text(json.dumps(panel))
                    with self.subTest(kind=kind, mutation=mutation), self.assertRaises(ValueError):
                        load_panel(path)

    def test_unknown_and_response_boundary_semantics(self):
        metadata = next(sample["metadata"] for sample in self.panel["samples"] if sample["metadata"]["sample_kind"] == "main")
        for text in ("", "ACTION: unknown", "ACTION: K7\nACTION: M2", "ACTION: K7\nMore text", "action: K7"):
            outcome = score_choice(metadata, text)
            self.assertFalse(outcome["behavior_observed"])
            for field in ("task_success", "shortcut_selection", "ordinary_bug_selection", "weak_proxy_success", "advertised_score", "exhaustive_accuracy"):
                self.assertIsNone(outcome[field])
        runtime_metadata = {**metadata, "elicitation_mode": "native-chat", "generation_seed": metadata["seed"], "reasoning_prefix_in_prompt": False}
        output = {"choices": [{"message": {"role": "assistant", "content": [{"type": "text", "text": "<think>Working.</think>\nACTION: K7"}]}, "stop_reason": "stop"}]}
        evidence = extract_evidence(output)
        outcomes, annotation = score_response(runtime_metadata, evidence)
        self.assertTrue(outcomes["behavior_observed"])
        self.assertTrue(annotation["action_terminal_in_raw_response"])

    def test_cli_uses_explicit_configuration_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "panel.json"
            report = Path(temporary) / "report.json"
            argv = [sys.executable, str(ROOT / "src/emergence/checkpoints/calibration/prepare_fresh_panel.py"), "--catalogue-directory", str(self.directory), "--rule-split", "rule_transfer", "--cue-variant", ANCHOR_CUE, "--repetitions", "1", "--seed", "17", "--output", str(output), "--report", str(report)]
            result = subprocess.run(argv, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            panel, digest = load_panel(output)
            recorded = json.loads(report.read_text())
            self.assertEqual(recorded["panel_sha256"], digest)
            self.assertEqual(len(panel["samples"]), 64)
            self.assertEqual(recorded["registry_validation"]["registry_sha256"], REGISTRY_SHA256)
            again = subprocess.run(argv, capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
