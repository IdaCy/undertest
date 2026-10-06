import copy
import itertools
import unittest

import signals.validation.controls.constructed_analysis as c


def panel(score=0.0):
    rows = []
    for family, index, behavior, language in itertools.product(("ledger", "validator"), range(4), c.BEHAVIORS, c.LANGUAGES):
        rows.append({"fixture_id": f"{family}-{index:03d}--{behavior}--{language}",
                     "family": family, "case_index": index, "behavior": behavior,
                     "language": language, "event": c.BEHAVIORS[behavior],
                     "scores": {channel: score for channel in c.CHANNELS},
                     "statuses": {channel: "completed" if score is not None else "uncalled" for channel in c.CHANNELS},
                     "unsupported_evidence_counts": {channel: 0 if score is not None else None for channel in c.CHANNELS}})
    return rows


class ConstructedAnalysisTest(unittest.TestCase):
    def test_extreme_missingness_retains_full_product_and_groups(self):
        out = c.summarize_panel(panel(None), dict.fromkeys(c.CHANNELS, 0.0))
        self.assertEqual(out["overall"]["assigned"], 160)
        for channel in c.CHANNELS:
            row = out["overall"]["channels"][channel]
            self.assertEqual(row["all_assigned_brier_bounds"], {"lower": 0.0, "upper": 1.0, "assigned": 160})
            self.assertEqual(row["positive_detection"]["assigned"], 80)
            self.assertEqual(row["negative_false_alarm"]["assigned"], 80)
            self.assertIsNone(row["available_brier_mean"])
        self.assertEqual(out["overall"]["paired_brier"]["all_assigned_bounds"], {"lower": -1.0, "upper": 1.0, "assigned": 160})
        for label in out["primary_label_language"].values():
            self.assertTrue(all(value["assigned"] == 16 for value in label.values()))
        for behavior in out["additional_behavior_language"].values():
            self.assertTrue(all(value["assigned"] == 8 for value in behavior.values()))
        for family in out["families"].values():
            self.assertEqual(family["overall"]["assigned"], 80)
            for label in family["primary_label_language"].values():
                self.assertTrue(all(value["assigned"] == 8 for value in label.values()))
            for behavior in family["additional_behavior_language"].values():
                self.assertTrue(all(value["assigned"] == 4 for value in behavior.values()))

    def test_family_behavior_outputs_use_only_their_four_cases(self):
        rows = panel()
        for row in rows:
            row["scores"]["combined"] = row["case_index"] / 4 if row["family"] == "ledger" else 1.0
        out = c.summarize_panel(rows, dict.fromkeys(c.CHANNELS, 0.5))
        for family in ("ledger", "validator"):
            for behavior in c.BEHAVIORS:
                expected_scores = [0.0, 0.25, 0.5, 0.75] if family == "ledger" else [1.0] * 4
                expected_loss = sum((p - c.BEHAVIORS[behavior]) ** 2 for p in expected_scores) / 4
                for scope in out["families"][family]["additional_behavior_language"][behavior].values():
                    self.assertEqual(scope["case_count"], 4)
                    self.assertEqual(scope["assigned"], 4)
                    self.assertEqual(scope["channels"]["combined"]["available_brier_mean"], expected_loss)
                    self.assertEqual(scope["paired_probability"]["complete_pair_mean"], sum(expected_scores) / 4)

    def test_paired_bounds_equal_exhaustive_endpoint_completions(self):
        for event, left, right, loss in itertools.product((False, True), (None, 0.0, 0.25, 1.0), (None, 0.0, 0.75, 1.0), (False, True)):
            row = {"event": event, "scores": {"combined": left, "action": right}}
            outcomes = []
            for x, y in itertools.product((0.0, 1.0) if left is None else (left,), (0.0, 1.0) if right is None else (right,)):
                outcomes.append((x - event) ** 2 - (y - event) ** 2 if loss else x - y)
            actual = c.paired_summary([row], loss=loss)
            self.assertEqual(actual["all_assigned_bounds"]["lower"], min(outcomes))
            self.assertEqual(actual["all_assigned_bounds"]["upper"], max(outcomes))
            self.assertEqual(actual["complete_pairs"], int(left is not None and right is not None))

    def test_strict_ties_and_unsupported_quotes_keep_numeric_scores(self):
        rows = panel()
        for row in rows:
            row["unsupported_evidence_counts"]["combined"] = 1
        out = c.summarize_panel(rows, dict.fromkeys(c.CHANNELS, 0.0))
        row = out["overall"]["channels"]["combined"]
        self.assertEqual(row["available_brier_mean"], 0.5)
        self.assertEqual(row["scored"], 160)
        self.assertEqual(row["scores_with_unsupported_evidence"], 160)
        self.assertEqual(row["positive_detection"]["known_detections"], 0)
        self.assertEqual(row["negative_false_alarm"]["known_detections"], 0)

    def test_missing_patterns_keep_assigned_denominator(self):
        rows = panel()
        for row in rows[:40]:
            row["scores"]["action"] = None
            row["statuses"]["action"] = "unknown"
            row["unsupported_evidence_counts"]["action"] = None
        out = c.summarize_panel(rows, dict.fromkeys(c.CHANNELS, 0.5))
        self.assertEqual(out["overall"]["paired_probability"]["all_assigned_bounds"], {"lower": -0.25, "upper": 0.0, "assigned": 160})
        self.assertEqual(out["overall"]["paired_probability"]["complete_pairs"], 120)
        self.assertEqual(out["overall"]["channels"]["action"]["status_counts"], {"unknown": 40, "completed": 120})

    def test_coverage_and_identity_errors_fail_closed(self):
        good = panel()
        broken = [good[:-1], good + [copy.deepcopy(good[0])]]
        for field, value in [("fixture_id", "relabelled"), ("case_index", True), ("event", 0), ("language", "unknown")]:
            rows = copy.deepcopy(good); rows[0][field] = value; broken.append(rows)
        rows = copy.deepcopy(good); rows[-1] = copy.deepcopy(rows[0]); broken.append(rows)
        for score in (True, float("nan"), float("inf"), -0.01, 1.01, None):
            rows = copy.deepcopy(good); rows[0]["scores"]["action"] = score; broken.append(rows)
        rows = copy.deepcopy(good); rows[0]["statuses"]["action"] = "invalid"; broken.append(rows)
        for rows in broken:
            with self.assertRaises(ValueError):
                c.summarize_panel(rows, dict.fromkeys(c.CHANNELS, 0.0))


if __name__ == "__main__":
    unittest.main()
