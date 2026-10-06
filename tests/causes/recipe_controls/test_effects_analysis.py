import copy
import json
import tempfile
import unittest
from pathlib import Path

from causes.recipe_controls.analysis import PRIMARY_OUTCOME
from causes.recipe_controls.analysis import summarize_factorial


CELLS = (("ordinary", "limited"), ("ordinary", "broad"), ("evaluation", "limited"), ("evaluation", "broad"))
GRADING = "grading_limited_minus_broad"
FRAMING = "framing_evaluation_minus_ordinary"
INTERACTION = "grading_by_framing_interaction"


def assignments(instance, cell_values, *, family="ledger"):
    rows = []
    for (framing, grading), values in zip(CELLS, cell_values):
        for replica, value in enumerate(values):
            identity = f"{instance}-{framing}-{grading}-{replica}"
            attempt = {
                "attempt_id": identity + "-first", "started": True,
                "trace_complete": value is not None, "termination": "natural" if value is not None else "unknown",
                "model_cap": False, "sample_limit": False, "errors": [], "samples": [],
                "outcomes": {PRIMARY_OUTCOME: value},
            }
            rows.append({
                "assignment_id": identity, "instance_id": instance, "family": family,
                "framing": framing, "grading": grading, "rollout": replica, "prompt_variant": "base",
                "status": "completed" if value is not None else "incomplete",
                "primary_attempt_id": attempt["attempt_id"], "primary_order_verified": True,
                "primary_outcome": value, "outcomes": {PRIMARY_OUTCOME: value}, "attempts": [attempt],
            })
    return rows


class FactorialSummaryTests(unittest.TestCase):
    def test_equal_family_instance_and_factor_weights_ignore_rollout_imbalance(self):
        rows = assignments("a", [[1, 1], [0, 0], [1, 1], [0, 0]])
        rows += assignments("b", [[0] * 8, [1] * 8, [0] * 8, [1] * 8])
        rows += assignments("c", [[1, 1], [0, 0], [0, 0], [0, 0]], family="validation")
        result = summarize_factorial(rows, panel={"ledger": ["a", "b"], "validation": ["c"]}, run_type="smoke")
        self.assertEqual(result["pooled"]["counts"]["assigned"], 48)
        self.assertEqual(result["family_weight"], 0.5)
        self.assertEqual(result["families"]["ledger"]["instance_weight"], 0.5)
        self.assertEqual(result["families"]["ledger"]["contrasts"][GRADING]["estimate"], 0)
        self.assertEqual(result["families"]["validation"]["contrasts"][GRADING]["estimate"], 0.5)
        pooled = result["pooled"]["contrasts"]
        self.assertEqual([pooled[name]["estimate"] for name in (GRADING, FRAMING, INTERACTION)], [0.25, -0.25, -0.5])
        self.assertEqual(pooled[GRADING]["missingness_bounds"], {"lower": 0.25, "upper": 0.25})
        self.assertEqual(result["claim_status"], "descriptive_only_not_confirmatory")
        self.assertEqual(result["run_type"], "smoke")
        self.assertEqual(result["bootstrap"]["reason"], "not_requested")

    def test_missingness_bounds_use_all_assignments_and_suppress_population_point_estimates(self):
        rows = assignments("a", [[1, None], [0, 0], [0, 1], [None, 0]])
        result = summarize_factorial(rows, panel={"ledger": ["a"]})
        counts = result["pooled"]["counts"]
        self.assertEqual((counts["assigned"], counts["known_positive"], counts["known_negative"], counts["missing"]), (8, 2, 4, 2))
        cells = result["families"]["ledger"]["instances"]["a"]["cells"]
        self.assertEqual(cells[0]["missingness_bounds"], {"lower": 0.5, "upper": 1.0})
        self.assertIsNone(cells[0]["estimate"])
        self.assertEqual(cells[0]["observed_only_descriptive_rate"], 1.0)
        contrasts = result["pooled"]["contrasts"]
        self.assertEqual(contrasts[GRADING]["missingness_bounds"], {"lower": 0.25, "upper": 0.75})
        self.assertEqual(contrasts[FRAMING]["missingness_bounds"], {"lower": -0.25, "upper": 0.25})
        self.assertEqual(contrasts[INTERACTION]["missingness_bounds"], {"lower": -1.0, "upper": 0.0})
        self.assertTrue(all(value["estimate"] is None for value in contrasts.values()))
        self.assertEqual(contrasts[GRADING]["observed_only_descriptive"], 0.75)

    def test_all_unknown_remains_unknown_with_full_possible_contrast_ranges(self):
        rows = assignments("a", [[None], [None], [None], [None]])
        for row in rows:
            row.update(status="never_started", primary_attempt_id=None, primary_order_verified=None, attempts=[])
        result = summarize_factorial(rows, panel={"ledger": ["a"]})
        self.assertEqual(result["pooled"]["counts"]["missing"], 4)
        self.assertEqual(result["pooled"]["counts"]["primary_attempts"]["count"], 0)
        self.assertEqual(result["pooled"]["counts"]["primary_attempts"]["model_cap"]["unknown"], 4)
        for name, width in ((GRADING, 1), (FRAMING, 1), (INTERACTION, 2)):
            contrast = result["pooled"]["contrasts"][name]
            self.assertIsNone(contrast["estimate"])
            self.assertIsNone(contrast["observed_only_descriptive"])
            self.assertEqual(contrast["missingness_bounds"], {"lower": -width, "upper": width})

    def test_retries_do_not_replace_first_outcomes_or_repair_unknowns(self):
        rows = assignments("a", [[None, 1], [0, 0], [0, 0], [0, 0]])
        for row, retry_value in zip(rows[:2], (1, 0)):
            retry = copy.deepcopy(row["attempts"][0])
            retry.update(attempt_id=row["assignment_id"] + "-retry", outcomes={PRIMARY_OUTCOME: retry_value}, model_cap=True)
            row["attempts"].append(retry)
        result = summarize_factorial(rows, panel={"ledger": ["a"]})
        counts = result["pooled"]["counts"]
        self.assertEqual((counts["known_positive"], counts["missing"]), (1, 1))
        self.assertEqual(counts["assignments_with_additional_attempts"], 2)
        self.assertEqual(counts["primary_attempts"]["count"], 8)
        self.assertEqual(counts["all_attempts"]["count"], 10)
        self.assertEqual(counts["primary_attempts"]["model_cap"]["yes"], 0)
        self.assertEqual(counts["all_attempts"]["model_cap"]["yes"], 2)
        self.assertIsNone(result["pooled"]["contrasts"][GRADING]["estimate"])

    def test_ambiguous_order_preserves_unknown_even_when_attempts_have_values(self):
        rows = assignments("a", [[None, 0], [0, 0], [0, 0], [0, 0]])
        row = rows[0]
        row["attempts"][0]["outcomes"][PRIMARY_OUTCOME] = 1
        retry = copy.deepcopy(row["attempts"][0])
        retry.update(attempt_id="ambiguous-retry", outcomes={PRIMARY_OUTCOME: 0})
        row.update(primary_attempt_id=None, primary_order_verified=False, attempts=[row["attempts"][0], retry])
        result = summarize_factorial(rows, panel={"ledger": ["a"]})
        self.assertEqual(result["pooled"]["counts"]["known_positive"], 0)
        self.assertEqual(result["pooled"]["counts"]["missing"], 1)

    def test_errors_and_caps_are_reported_without_erasing_known_positive_actions(self):
        rows = assignments("a", [[1, 0], [0, 0], [0, 0], [0, 0]])
        rows[0]["status"] = "incomplete"
        rows[0]["attempts"][0].update(
            termination="interrupted", trace_complete=False, errors=[{"error": "lost connection"}],
            samples=[{"sample_status": "error", "error": "lost connection"}],
        )
        rows[1]["attempts"][0].update(model_cap=True, sample_limit=True, termination="prespecified_limit")
        result = summarize_factorial(rows, panel={"ledger": ["a"]})
        counts = result["pooled"]["counts"]
        self.assertEqual((counts["known_positive"], counts["missing"]), (1, 0))
        self.assertEqual(counts["assignment_status_counts"], {"completed": 7, "incomplete": 1})
        self.assertEqual(counts["primary_attempts"]["errors"], {"yes": 1, "no": 7, "unknown": 0})
        self.assertEqual(counts["primary_attempts"]["model_cap"]["yes"], 1)
        self.assertEqual(counts["primary_attempts"]["sample_limit"]["yes"], 1)
        self.assertEqual(counts["primary_attempts"]["termination_counts"], {"interrupted": 1, "natural": 6, "prespecified_limit": 1})

    def test_design_gaps_duplicates_and_inconsistent_provenance_are_rejected(self):
        original = assignments("a", [[0], [0], [0], [0]])
        invalid = []
        invalid.append((original[:-1], {"ledger": ["a"]}))
        invalid.append((original, {"ledger": ["a", "missing-instance"]}))
        invalid.append((original + [original[0]], {"ledger": ["a"]}))
        invalid.append((original, {"wrong-family": ["a"]}))
        invalid.append((original, {"ledger": ["another-instance"]}))
        for field, value in (("grading", "unlisted"), ("primary_attempt_id", "missing"), ("primary_order_verified", False)):
            changed = copy.deepcopy(original)
            changed[0][field] = value
            invalid.append((changed, {"ledger": ["a"]}))
        changed = copy.deepcopy(original)
        changed[0]["outcomes"][PRIMARY_OUTCOME] = 1
        invalid.append((changed, {"ledger": ["a"]}))
        changed = copy.deepcopy(original)
        changed[0]["outcomes"][PRIMARY_OUTCOME] = 0.5
        invalid.append((changed, {"ledger": ["a"]}))
        changed = copy.deepcopy(original)
        changed[0]["prompt_variant"] = "paraphrase"
        invalid.append((changed, {"ledger": ["a"]}))
        for index, (rows, panel) in enumerate(invalid):
            with self.subTest(case=index), self.assertRaises(ValueError):
                summarize_factorial(rows, panel=panel)

    def test_selecting_a_later_retry_as_primary_is_rejected(self):
        rows = assignments("a", [[0], [0], [0], [0]])
        row = rows[0]
        retry = copy.deepcopy(row["attempts"][0])
        retry["attempt_id"] = "later-retry"
        row["attempts"].append(retry)
        row["primary_attempt_id"] = "later-retry"
        with self.assertRaisesRegex(ValueError, "first reconciled"):
            summarize_factorial(rows, panel={"ledger": ["a"]})

    def test_input_is_unchanged_and_result_is_strict_json(self):
        rows = assignments("a", [[0, 1], [0, 1], [0, 1], [None, 1]])
        before = copy.deepcopy(rows)
        panel = {"ledger": ["a"]}
        result = summarize_factorial(rows, panel=panel)
        self.assertEqual(rows, before)
        self.assertEqual(panel, {"ledger": ["a"]})
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)

    def test_reconciler_records_without_family_metadata_preserve_unstarted_denominator(self):
        from causes.recipe_controls.results import reconcile

        scheduled = [
            {"assignment_id": f"assignment-{index}", "instance_id": "ledger-dev-0",
             "framing": framing, "grading": grading, "rollout": 0, "prompt_variant": "base"}
            for index, (framing, grading) in enumerate(CELLS)
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "assignments.json").write_text(json.dumps(scheduled))
            reconciled = reconcile(output)
            result = summarize_factorial(reconciled["assignments"], panel={"ledger": ["ledger-dev-0"]})
        counts = result["pooled"]["counts"]
        self.assertEqual((counts["assigned"], counts["missing"]), (4, 4))
        self.assertEqual(counts["assignment_status_counts"], {"never_started": 4})
        self.assertIsNone(result["pooled"]["contrasts"][GRADING]["estimate"])


class FactorialBootstrapTests(unittest.TestCase):
    def test_fixed_panel_bootstrap_is_reproducible_and_assignment_order_independent(self):
        rows = assignments("a", [[0, 1]] * 4) + assignments("b", [[0, 0, 1, 1]] * 4)
        options = dict(panel={"ledger": ["a", "b"]}, bootstrap_target="fixed_panel", bootstrap_draws=300, bootstrap_seed=81)
        first = summarize_factorial(rows, **options)["bootstrap"]
        second = summarize_factorial(list(reversed(rows)), **options)["bootstrap"]
        self.assertEqual(first, second)
        self.assertTrue(first["available"])
        self.assertEqual(first["scope"], "fixed_finite_panel_episode_variation")
        self.assertFalse(first["sufficiency_claim"])
        grading = first["pooled"][GRADING]
        self.assertTrue(grading["available"])
        self.assertLess(grading["lower"], 0)
        self.assertGreater(grading["upper"], 0)

    def test_generator_bootstrap_keeps_all_four_conditions_paired(self):
        rows = assignments("a", [[0, 0]] * 4) + assignments("b", [[1, 1]] * 4)
        result = summarize_factorial(rows, panel={"ledger": ["a", "b"]}, bootstrap_target="instance_generator", bootstrap_draws=200, bootstrap_seed=17)
        bootstrap = result["bootstrap"]
        self.assertFalse(bootstrap["available"])
        self.assertEqual(bootstrap["reason"], "all_contrasts_degenerate")
        self.assertEqual(bootstrap["resampling_unit"], "paired_four_cell_instance_blocks_within_family")
        for value in bootstrap["pooled"].values():
            self.assertEqual(value["reason"], "degenerate_empirical_distribution")
            self.assertIsNone(value["lower"])
            self.assertIsNone(value["upper"])

    def test_generator_bootstrap_varies_instances_without_weighting_their_replica_counts(self):
        rows = assignments("a", [[1, 1], [0, 0], [1, 1], [0, 0]])
        rows += assignments("b", [[0] * 10, [1] * 10, [0] * 10, [1] * 10])
        result = summarize_factorial(rows, panel={"ledger": ["a", "b"]}, bootstrap_target="instance_generator", bootstrap_draws=300, bootstrap_seed=2)
        self.assertEqual(result["pooled"]["contrasts"][GRADING]["estimate"], 0)
        interval = result["bootstrap"]["pooled"][GRADING]
        self.assertEqual(interval, {"available": True, "reason": None, "lower": -1.0, "upper": 1.0})
        self.assertEqual(result["bootstrap"]["scope"], "selected_families_narrow_instance_generator")

    def test_unknown_outcomes_or_inadequate_cells_make_bootstrap_unavailable(self):
        checks = (
            ([[0, None]] * 4, "fixed_panel", "unknown_outcomes_present"),
            ([[0]] * 4, "fixed_panel", "fewer_than_two_assigned_episodes_in_a_cell"),
            ([[0, 1]] * 4, "instance_generator", "fewer_than_two_instances_in_a_family"),
        )
        for values, target, reason in checks:
            with self.subTest(target=target, reason=reason):
                result = summarize_factorial(assignments("a", values), panel={"ledger": ["a"]}, bootstrap_target=target)
                self.assertFalse(result["bootstrap"]["available"])
                self.assertEqual(result["bootstrap"]["reason"], reason)

    def test_bootstrap_target_and_configuration_are_explicit_and_validated(self):
        rows = assignments("a", [[0, 1]] * 4)
        for options in (
            {"bootstrap_target": "tasks"}, {"bootstrap_target": "fixed_panel", "bootstrap_draws": 1},
            {"bootstrap_target": "fixed_panel", "bootstrap_seed": True},
            {"bootstrap_target": "fixed_panel", "confidence": float("nan")},
            {"bootstrap_target": "fixed_panel", "confidence": 1.0},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                summarize_factorial(rows, panel={"ledger": ["a"]}, **options)


if __name__ == "__main__":
    unittest.main()
