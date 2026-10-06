import copy
import hashlib
import json
import unittest

from causes.recipe_controls.controls.analysis import INPUT_SCHEMA_VERSION
from causes.recipe_controls.controls.analysis import summarize_control_design
from causes.recipe_controls.controls.design import build_control_design


def rows_for(design, value):
    endpoint = design["manifest"]["contrast_specification"]["endpoint"]["name"]
    result = []
    for assignment in design["assignments"]:
        result.append({"schema_version": INPUT_SCHEMA_VERSION, "assignment_id": assignment["assignment_id"],
                       "assignment_metadata": copy.deepcopy(assignment),
                       "first_attempt": {
                           "attempt_id": assignment["assignment_id"] + "-first", "first_started_attempt_verified": True,
                           "metadata_integrity": True, "metadata_complete": True, "metadata_conflicts": [], "metadata_absent": [],
                           "outcome_verified": True, "episode_end_observed": True, "trace_complete": True,
                           "outcomes": {endpoint: value(assignment) if callable(value) else value},
                           "final_category": None, "error": False, "model_cap": False, "sample_limit": False, "termination": "natural",
                       }})
    return result


class EffectsControlAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c2 = build_control_design("C2", ["ledgerbanks-dev-0"], 1, 8, stage="development")
        cls.c3 = build_control_design("C3", ["ledger-dev-0"], 2, 8, stage="development")
        cls.unequal_panel = build_control_design("C3", ["ledger-dev-0", "ledger-dev-5", "validator-dev-0"], 2, 8, stage="development")
        cls.c4 = build_control_design("C4", ["validator-dev-0"], 1, 8, stage="development")
        cls.c5 = build_control_design("C5", ["ledger-dev-0"], 2, 8, stage="development")

    def test_equal_family_then_instance_weighting_differs_from_rollout_pooling(self):
        def value(row):
            if row["cell_id"] == "neutral":
                return 0
            return int((row["cell_id"] == "artifact") == (row["instance_id"] != "ledger-dev-5"))

        result = summarize_control_design(self.unequal_panel, rows_for(self.unequal_panel, value))
        self.assertEqual(result["pooled"]["counts"]["assigned"], 18)
        self.assertEqual(result["family_weight"], 0.5)
        self.assertEqual(result["families"]["ledger"]["instance_weight"], 0.5)
        self.assertEqual(result["families"]["validator"]["instance_weight"], 1)
        name = "artifact_minus_history"
        self.assertEqual(result["families"]["ledger"]["contrasts"][name]["estimate"], 0)
        self.assertEqual(result["families"]["validator"]["contrasts"][name]["estimate"], 1)
        self.assertEqual(result["pooled"]["contrasts"][name]["estimate"], 0.5)
        self.assertEqual(result["pooled"]["contrasts"][name]["missingness_bounds"], {"lower": 0.5, "upper": 0.5})
        self.assertEqual(result["pooled"]["cells"]["artifact"]["counts"]["known_value_counts"], {"0": 2, "1": 4})
        self.assertEqual(result["pooled"]["cells"]["artifact"]["estimate"], 0.75)

    def test_binary_unknown_bounds_keep_denominators_and_observed_only_separate(self):
        values = {("artifact", 0): 1, ("artifact", 1): None, ("history", 0): 0,
                  ("history", 1): None, ("neutral", 0): 0, ("neutral", 1): 0}
        result = summarize_control_design(self.c3, rows_for(self.c3, lambda row: values[(row["cell_id"], row["rollout"])]))
        cells = result["pooled"]["cells"]
        self.assertEqual(cells["artifact"]["counts"]["assigned"], 2)
        self.assertEqual(cells["artifact"]["missingness_bounds"], {"lower": 0.5, "upper": 1})
        self.assertEqual(cells["history"]["missingness_bounds"], {"lower": 0, "upper": 0.5})
        primary = result["pooled"]["contrasts"]["artifact_minus_history"]
        self.assertIsNone(primary["estimate"])
        self.assertEqual(primary["missingness_bounds"], {"lower": 0, "upper": 1})
        self.assertEqual(primary["observed_only_descriptive"], 1)
        self.assertEqual(primary["required_unknown_assignments"], 2)

    def test_zero_coefficient_unknown_cells_do_not_remove_known_contrast(self):
        result = summarize_control_design(self.c3, rows_for(self.c3, lambda row: {"artifact": 1, "history": 0, "neutral": None}[row["cell_id"]]))
        self.assertEqual(result["pooled"]["counts"]["unknown"], 2)
        primary = result["pooled"]["contrasts"]["artifact_minus_history"]
        self.assertEqual(primary["estimate"], 1)
        self.assertEqual(primary["missingness_bounds"], {"lower": 1, "upper": 1})
        self.assertEqual(primary["required_unknown_assignments"], 0)
        self.assertIsNone(result["pooled"]["contrasts"]["artifact_minus_neutral"]["estimate"])

    def test_all_zero_all_one_and_no_rows_do_not_imply_sampling_precision(self):
        for study in (self.c3, self.c4, self.c5):
            for value in (0, 1):
                result = summarize_control_design(study, rows_for(study, value))
                self.assertEqual(result["pooled"]["counts"]["unknown"], 0)
                for contrast in result["pooled"]["contrasts"].values():
                    self.assertAlmostEqual(contrast["estimate"], 0)
                    self.assertAlmostEqual(contrast["missingness_bounds"]["lower"], 0)
                    self.assertAlmostEqual(contrast["missingness_bounds"]["upper"], 0)
                self.assertEqual(result["claim_status"], "descriptive_only_not_confirmatory")
                self.assertNotIn("bootstrap", result)
        result = summarize_control_design(self.c5, [])
        self.assertEqual(result["pooled"]["counts"]["assigned"], 12)
        self.assertEqual(result["pooled"]["counts"]["unknown"], 12)
        interaction = result["pooled"]["contrasts"]["simulated_minus_operational_by_reuse"]
        self.assertEqual(interaction["missingness_bounds"], {"lower": -2, "upper": 2})
        self.assertIsNone(interaction["estimate"])
        self.assertIsNone(interaction["observed_only_descriptive"])

    def test_c2_signed_extremes_and_coefficient_signs(self):
        for multiplier in (1, -1):
            rows = rows_for(self.c2, lambda row: multiplier * (1 if row["factors"]["final_bank"] == "A" else -1))
            result = summarize_control_design(self.c2, rows)
            contrast = result["pooled"]["contrasts"]["directional_adaptation"]
            self.assertEqual(contrast["estimate"], multiplier)
            self.assertEqual(contrast["missingness_bounds"], {"lower": multiplier, "upper": multiplier})
        all_unknown = summarize_control_design(self.c2, [])
        self.assertEqual(all_unknown["pooled"]["contrasts"]["directional_adaptation"]["missingness_bounds"], {"lower": -1, "upper": 1})
        partial = rows_for(self.c2, lambda row: 1 if row["cell_id"] == "A-A" else None)
        result = summarize_control_design(self.c2, partial)
        contrast = result["pooled"]["contrasts"]["directional_adaptation"]
        self.assertEqual(contrast["missingness_bounds"], {"lower": -0.5, "upper": 1})
        self.assertIsNone(contrast["estimate"])

    def test_c2_category_derivation_preserves_invalid_missing_and_unavailable_end(self):
        categories = {"A-A": "invalid", "A-B": "missing", "B-A": "bank_A", "B-B": "bank_B"}
        rows = rows_for(self.c2, None)
        for row in rows:
            row["first_attempt"]["final_category"] = categories[row["assignment_metadata"]["cell_id"]]
        result = summarize_control_design(self.c2, rows)
        self.assertEqual(result["pooled"]["counts"]["trusted_final_category_counts"],
                         {"bank_A": 1, "bank_B": 1, "valid_no_bank": 0, "invalid": 1, "missing": 1})
        self.assertEqual(result["pooled"]["counts"]["known_value_counts"], {"-1": 1, "0": 2, "1": 1})
        self.assertEqual(result["pooled"]["contrasts"]["directional_adaptation"]["estimate"], 0.5)
        for row in rows:
            row["first_attempt"]["episode_end_observed"] = False
        unavailable = summarize_control_design(self.c2, rows)
        self.assertEqual(unavailable["pooled"]["counts"]["unknown"], 4)
        self.assertEqual(sum(unavailable["pooled"]["counts"]["trusted_final_category_counts"].values()), 0)
        self.assertEqual({row["reported_final_category"] for row in unavailable["assignment_audit"]}, set(categories.values()))

    def test_c5_difference_in_differences_matches_hand_calculation(self):
        rows = rows_for(self.c5, lambda row: int(row["cell_id"] == "simulated-reuse" or
                                               (row["cell_id"] == "operational-reuse" and row["rollout"] == 0)))
        result = summarize_control_design(self.c5, rows)
        contrasts = result["pooled"]["contrasts"]
        self.assertEqual(contrasts["simulated_minus_operational_by_reuse"]["estimate"], 0.5)
        self.assertEqual(contrasts["simulated_minus_operational"]["estimate"], 0.25)
        self.assertAlmostEqual(contrasts["reuse_minus_archive"]["estimate"], 0.5)

    def test_provenance_and_exact_metadata_fail_closed_including_bool_integer_alias(self):
        originals = rows_for(self.c3, 1)
        mutations = [
            lambda row: row["assignment_metadata"].__setitem__("rendered_input_sha256", "0" * 64),
            lambda row: row.__setitem__("assignment_metadata", None),
            lambda row: row["assignment_metadata"].__setitem__("rollout", bool(row["assignment_metadata"]["rollout"])),
            lambda row: row["first_attempt"].__setitem__("first_started_attempt_verified", False),
            lambda row: row["first_attempt"].__setitem__("metadata_integrity", False),
            lambda row: row["first_attempt"].__setitem__("metadata_complete", False),
            lambda row: row["first_attempt"].__setitem__("metadata_conflicts", ["mismatch"]),
            lambda row: row["first_attempt"].pop("metadata_absent"),
            lambda row: row["first_attempt"].__setitem__("outcome_verified", None),
        ]
        for mutate in mutations:
            rows = copy.deepcopy(originals)
            mutate(rows[0])
            result = summarize_control_design(self.c3, rows)
            self.assertEqual(result["pooled"]["counts"]["unknown"], 1)
            self.assertEqual(result["pooled"]["counts"]["known_value_counts"]["0"], 0)
            self.assertTrue(next(record for record in result["assignment_audit"] if record["assignment_id"] == rows[0]["assignment_id"])["unknown_reasons"])

    def test_interrupted_positive_survives_but_negative_needs_complete_action_history(self):
        rows = rows_for(self.c3, 1)
        first = rows[0]["first_attempt"]
        first.update(episode_end_observed=False, trace_complete=False, error=True, model_cap=True, termination="interrupted")
        result = summarize_control_design(self.c3, rows)
        self.assertEqual(result["pooled"]["counts"]["unknown"], 0)
        self.assertEqual(result["pooled"]["counts"]["reported_diagnostics"]["error"]["yes"], 1)
        self.assertEqual(result["pooled"]["counts"]["reported_diagnostics"]["model_cap"]["yes"], 1)
        first["outcomes"]["submitted_shortcut_anytime"] = 0
        result = summarize_control_design(self.c3, rows)
        self.assertEqual(result["pooled"]["counts"]["unknown"], 1)
        self.assertEqual(result["pooled"]["counts"]["known_value_counts"]["0"], 0)
        first["first_started_attempt_verified"] = False
        first["outcomes"]["submitted_shortcut_anytime"] = 1
        self.assertEqual(summarize_control_design(self.c3, rows)["pooled"]["counts"]["unknown"], 1)

    def test_missing_rows_keep_population_but_duplicate_extra_and_crosslinked_rows_reject(self):
        rows = rows_for(self.c3, 0)
        missing = summarize_control_design(self.c3, rows[:-1])
        self.assertEqual(missing["pooled"]["counts"]["assigned"], 6)
        self.assertEqual(missing["pooled"]["counts"]["unknown"], 1)
        self.assertEqual(len(missing["assignment_audit"]), 6)
        extra = copy.deepcopy(rows)
        extra[0]["assignment_id"] = "foreign-assignment"
        crosslinked = copy.deepcopy(rows)
        crosslinked[1]["first_attempt"]["attempt_id"] = crosslinked[0]["first_attempt"]["attempt_id"]
        for bad in (rows + [rows[0]], extra, crosslinked):
            with self.assertRaises(ValueError):
                summarize_control_design(self.c3, bad)
        unequal = copy.deepcopy(self.c3)
        unequal["assignments"].pop()
        with self.assertRaises(ValueError):
            summarize_control_design(unequal, rows[:-1])

    def test_invalid_domains_categories_flags_and_retry_fields_are_rejected(self):
        for target, values in ((self.c3, (True, False, -1, 2, 0.0, "1")), (self.c2, (True, 2, -2, 1.0, "A"))):
            for value in values:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    summarize_control_design(target, rows_for(target, value))
        bad_category = rows_for(self.c2, -1)
        bad_category[0]["first_attempt"]["final_category"] = "bank_A"
        with self.assertRaisesRegex(ValueError, "contradicts"):
            summarize_control_design(self.c2, bad_category)
        bad_category[0]["first_attempt"]["final_category"] = "honest"
        with self.assertRaises(ValueError):
            summarize_control_design(self.c2, bad_category)
        for field, value in (("metadata_integrity", 1), ("later_attempts", [])):
            bad = rows_for(self.c3, 1)
            bad[0]["first_attempt"][field] = value
            with self.assertRaises(ValueError):
                summarize_control_design(self.c3, bad)

    def test_input_and_output_references_are_deterministic_strict_json_and_not_aliased(self):
        design = copy.deepcopy(self.c3)
        rows = rows_for(design, 0)
        references = [{"path": "/private/reconciled.json", "sha256": "1" * 64}]
        before = copy.deepcopy((design, rows, references))
        result = summarize_control_design(design, rows, source_references=references)
        self.assertEqual((design, rows, references), before)
        self.assertEqual(result, json.loads(json.dumps(result, allow_nan=False)))
        self.assertEqual(result, summarize_control_design(design, rows, source_references=references))
        digest = result.pop("summary_sha256")
        text = json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        self.assertEqual(digest, hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(result["input_references"]["source_reference_status"], "caller_supplied_not_reread")
        result["contrast_specification"]["endpoint"]["domain"].append(7)
        result["input_references"]["source_references"][0]["path"] = "changed"
        self.assertEqual((design, rows, references), before)
        with self.assertRaises(ValueError):
            summarize_control_design(design, rows, source_references=[{"path": "file", "sha256": "bad"}])


if __name__ == "__main__":
    unittest.main()
