import copy
import math
import unittest

from signals.validation.controls.analysis import case_cluster_bootstrap
from signals.validation.controls.analysis import channel_metrics
from signals.validation.controls.analysis import paired_channel_scores
from signals.validation.controls.analysis import primary_label
from signals.validation.controls.analysis import wilson_interval
from signals.validation.controls.analysis import within_case_disagreement


def record(identifier, case, label, a=None, b=None, family="ledger"):
    return {"assignment_id": identifier, "case_id": case, "family": family,
            "label": label, "scores": {"a": a, "b": b}}


class AnalysisTests(unittest.TestCase):
    def test_comparator_ties_preserve_legacy_default_and_undefined_threshold(self):
        rows = [record("n", "N", False, 0.5), record("p", "P", True, 0.8)]
        self.assertEqual(channel_metrics(rows, "a", 0.5)["fpr"]["value"], 1)
        self.assertEqual(channel_metrics(rows, "a", 0.5, comparator=">")["fpr"]["value"], 0)
        undefined = channel_metrics(rows, "a", None, comparator=">")
        self.assertEqual(undefined["classification_status"], "undefined_threshold")
        self.assertEqual(undefined["n_available_scores"], 2)
        self.assertIsNone(undefined["fpr"]["value"])
        self.assertIsNone(undefined["precision"]["denominator"])
        self.assertAlmostEqual(undefined["brier"]["value"], 0.145)

    def test_ratio_bootstrap_retains_repeats_and_zero_denominator_clusters(self):
        for metric in ("fpr", "recall", "precision"):
            if metric == "fpr":
                data = [(False, 0.1), (False, 0.1), (False, 0.9), (True, 0.9)]
            elif metric == "recall":
                data = [(True, 0.1), (True, 0.1), (True, 0.9), (False, 0.9)]
            else:
                data = [(False, 0.9), (False, 0.9), (True, 0.9), (True, 0.1)]
            rows = [record(str(i), case, label, score) for i, ((label, score), case) in enumerate(zip(data, ["A", "A", "B", "C"]))]
            rows.append(record("missing", "D", None, 0.5))
            result = case_cluster_bootstrap(rows, metric, channel="a", draws=500, seed=12)
            self.assertAlmostEqual(result["estimate"], 1 / 3)
            self.assertEqual(result["n_clusters"], 4)
            self.assertEqual(result["n_denominator_clusters"], 2)
            self.assertEqual(result["denominator"], 3)
            self.assertGreater(result["n_failed_draws"], 0)
            self.assertEqual(result["n_successful_draws"] + result["n_failed_draws"], 500)
            possible = {None} | {b / (2 * a + b) for a in range(5) for b in range(5 - a) if a + b > 0}
            self.assertTrue(set(result["replicate_estimates"]) <= possible)
            self.assertTrue(result["interval"]["conditional_on_nonempty_resamples"])

    def test_ratio_bootstrap_unsupported_and_zero_event_limits(self):
        rows = [record("n1", "A", False, 0.1), record("n2", "B", False, 0.2)]
        no_positive = case_cluster_bootstrap(rows, "recall", channel="a")
        self.assertEqual(no_positive["status"], "no_denominator")
        self.assertIsNone(no_positive["interval"])
        zero = case_cluster_bootstrap(rows, "fpr", channel="a", draws=50)
        self.assertEqual(zero["status"], "degenerate_resampling_distribution")
        self.assertTrue(zero["zero_observed_events"])
        self.assertIsNone(zero["interval"])
        self.assertEqual(zero["empirical_percentiles"]["upper"], 0)
        self.assertIn("zero population risk", " ".join(zero["caveats"]))
        one = case_cluster_bootstrap(rows[:1], "fpr", channel="a")
        self.assertEqual(one["status"], "insufficient_evaluable_clusters")
        null = case_cluster_bootstrap(rows, "fpr", channel="a", threshold=None)
        self.assertEqual(null["status"], "undefined_threshold")
        self.assertIsNone(null["estimate"])

    def test_primary_label_preserves_incomplete_positives_and_unknown_negatives(self):
        for complete in (True, False, None):
            self.assertIs(primary_label({"observed_outcomes": {"shortcut_attempt": True}, "complete_episode": complete}), True)
            expected = False if complete else None
            self.assertIs(primary_label({"observed_outcomes": {"shortcut_attempt": False}, "complete_episode": complete}), expected)
        for labels in (None, {}, {"shortcut_attempt": True}, {"observed_outcomes": None},
                       {"observed_outcomes": {}, "complete_episode": True},
                       {"observed_outcomes": {"shortcut_attempt": None}, "complete_episode": True}):
            self.assertIsNone(primary_label(labels))
        for labels in ({"observed_outcomes": {"shortcut_attempt": 1}},
                       {"observed_outcomes": {"shortcut_attempt": False}, "complete_episode": "true"}):
            with self.assertRaises(ValueError):
                primary_label(labels)

    def test_metrics_use_known_scored_denominators_and_inclusive_threshold(self):
        rows = [
            record("1", "c1", True, 0.8), record("2", "c1", True, 0.2),
            record("3", "c2", False, 0.5), record("4", "c2", False, 0.1),
            record("5", "c3", None, 0.9), record("6", "c3", False, None),
        ]
        result = channel_metrics(rows, "a")
        self.assertEqual(result["confusion"], {"true_positive": 1, "false_positive": 1, "true_negative": 1, "false_negative": 1})
        self.assertAlmostEqual(result["brier"]["value"], 0.235)
        self.assertEqual(result["brier"]["denominator"], 4)
        for metric in ("fpr", "recall", "precision"):
            self.assertEqual(result[metric]["value"], 0.5)
            self.assertEqual(result[metric]["denominator"], 2)
            self.assertEqual(result[metric]["interval"]["assumption"], "iid-only")
        self.assertEqual((result["n_assigned"], result["n_known_labels"], result["n_unknown_labels"]), (6, 5, 1))
        self.assertEqual((result["n_missing_scores"], result["n_known_label_missing_score"], result["n_unknown_label_with_score"]), (1, 1, 1))

    def test_zero_denominators_are_unknown_not_perfect_performance(self):
        empty = channel_metrics([], "a")
        for metric in ("fpr", "recall", "precision", "brier"):
            self.assertIsNone(empty[metric]["value"])
        negative = channel_metrics([record("n", "c", False, 0.1)], "a")
        self.assertEqual(negative["fpr"]["value"], 0)
        self.assertIsNone(negative["recall"]["value"])
        self.assertIsNone(negative["precision"]["value"])
        unknown = channel_metrics([record("u", "c", None, 0.99)], "a")
        self.assertEqual(unknown["n_evaluable"], 0)
        self.assertIsNone(unknown["brier"]["value"])

    def test_wilson_values_and_iid_label(self):
        interval = wilson_interval(5, 10)
        self.assertAlmostEqual(interval["lower"], 0.2365930905, places=9)
        self.assertAlmostEqual(interval["upper"], 0.7634069095, places=9)
        self.assertEqual(interval["assumption"], "iid-only")
        zero = wilson_interval(0, 10)
        self.assertAlmostEqual(zero["lower"], 0)
        self.assertAlmostEqual(zero["upper"], 0.2775327999, places=9)
        full = wilson_interval(10, 10)
        self.assertAlmostEqual(full["lower"], 1 - zero["upper"])
        self.assertAlmostEqual(full["upper"], 1)
        self.assertEqual(wilson_interval(0, 0)["status"], "no_denominator")
        self.assertIsNone(wilson_interval(0, 0)["lower"])

    def test_paired_scores_never_compare_different_episode_sets(self):
        rows = [
            record("1", "c1", True, 0.8, 0.6),
            record("2", "c1", False, 0.2, 0.4),
            record("3", "c2", True, 1.0, None),
            record("4", "c2", None, 0.9, 0.3),
            record("5", "c2", False, None, 0.1),
        ]
        result = paired_channel_scores(rows, "a", "b")
        self.assertEqual(result["n_common_scores"], 3)
        self.assertEqual(result["n_missing_either_score"], 2)
        self.assertEqual(result["n_common_known_labels"], 2)
        self.assertEqual(result["n_common_unknown_labels"], 1)
        self.assertAlmostEqual(result["score_difference"]["value"], 0.2)
        self.assertAlmostEqual(result["brier_difference"]["value"], -0.12)
        reverse = paired_channel_scores(rows, "b", "a")
        self.assertAlmostEqual(reverse["score_difference"]["value"], -0.2)
        self.assertAlmostEqual(reverse["brier_difference"]["value"], 0.12)

    def test_missing_label_pair_bounds_require_consistent_completions(self):
        cases = {
            "mixed": [True, False, None],
            "all_unknown": [None, None, None],
            "one_sided": [True, True, None],
            "known": [True, True, False, False],
            "singleton": [None],
        }
        rows = [record(f"{case}-{i}", case, label) for case, labels in cases.items() for i, label in enumerate(labels)]
        result = within_case_disagreement(rows)
        by_case = {case["case_id"]: case for case in result["cases"]}
        # For three binary outcomes there can be at most two disagreements,
        # and a known positive plus negative forces exactly two after completion.
        self.assertEqual((by_case["mixed"]["minimum_disagreements"], by_case["mixed"]["maximum_disagreements"]), (2, 2))
        self.assertEqual(by_case["mixed"]["known_disagreement_rate"], 1)
        self.assertAlmostEqual(by_case["mixed"]["disagreement_lower"], 2 / 3)
        self.assertEqual((by_case["all_unknown"]["minimum_disagreements"], by_case["all_unknown"]["maximum_disagreements"]), (0, 2))
        self.assertIsNone(by_case["all_unknown"]["known_disagreement_rate"])
        self.assertEqual((by_case["one_sided"]["minimum_disagreements"], by_case["one_sided"]["maximum_disagreements"]), (0, 2))
        self.assertEqual((by_case["known"]["minimum_disagreements"], by_case["known"]["maximum_disagreements"]), (4, 4))
        self.assertIsNone(by_case["singleton"]["disagreement_upper"])
        self.assertEqual(result["pooled"]["all_pairs"], 15)
        self.assertEqual(result["pooled"]["known_pairs"], 8)
        self.assertEqual(result["pooled"]["known_disagreements"], 5)
        self.assertAlmostEqual(result["pooled"]["disagreement_lower"], 6 / 15)
        self.assertAlmostEqual(result["pooled"]["disagreement_upper"], 10 / 15)
        self.assertEqual(result["n_cases_with_pairs"], 4)

    def test_pair_bounds_handle_majority_constraints_and_empty_input(self):
        rows = [record(str(i), "c", label) for i, label in enumerate([True, True, True, True, None])]
        case = within_case_disagreement(rows)["cases"][0]
        self.assertEqual(case["maximum_disagreements"], 4)
        self.assertAlmostEqual(case["disagreement_upper"], 0.4)
        empty = within_case_disagreement([])
        self.assertEqual(empty["n_cases"], 0)
        self.assertIsNone(empty["pooled"]["known_disagreement_rate"])
        self.assertIsNone(empty["pooled"]["disagreement_upper"])

    def test_cluster_bootstrap_preserves_unequal_repeat_counts(self):
        rows = [record("a1", "A", False, 0), record("a2", "A", False, 0), record("b1", "B", True, 0)]
        before = copy.deepcopy(rows)
        result = case_cluster_bootstrap(rows, channel="a", draws=100, seed=17)
        self.assertAlmostEqual(result["estimate"], 1 / 3)
        self.assertEqual(result["n_clusters"], 2)
        self.assertEqual(result["n_evaluable_clusters"], 2)
        self.assertEqual(result["n_failed_draws"], 0)
        # AA, AB/BA and BB retain 4, 3 and 2 episodes respectively, giving
        # these three possible means; averaging case means would introduce 1/2.
        self.assertEqual(set(result["replicate_estimates"]), {0, 1 / 3, 1})
        self.assertEqual(result["interval"]["lower"], 0)
        self.assertEqual(result["interval"]["upper"], 1)
        self.assertEqual(result, case_cluster_bootstrap(list(reversed(rows)), channel="a", draws=100, seed=17))
        self.assertEqual(rows, before)

    def test_bootstrap_missing_cases_failures_and_insufficient_clusters(self):
        rows = [record("a", "A", True, 0), record("b", "B", False, 0), record("c", "C", None, 0)]
        result = case_cluster_bootstrap(rows, channel="a", draws=200, seed=7)
        self.assertEqual(result["n_clusters"], 3)
        self.assertEqual(result["n_evaluable_clusters"], 2)
        self.assertGreater(result["n_failed_draws"], 0)
        self.assertEqual(result["n_failed_draws"], result["replicate_estimates"].count(None))
        self.assertEqual(result["n_successful_draws"] + result["n_failed_draws"], 200)
        self.assertEqual(result["status"], "empty_resamples_excluded")
        self.assertTrue(result["interval"]["conditional_on_nonempty_resamples"])
        one = case_cluster_bootstrap(rows[1:], channel="a", draws=100)
        self.assertEqual(one["status"], "insufficient_evaluable_clusters")
        self.assertIsNone(one["interval"])
        self.assertEqual(one["replicate_estimates"], [])
        none = case_cluster_bootstrap(rows[2:], channel="a")
        self.assertEqual(none["status"], "no_evaluable_episodes")
        self.assertIsNone(none["estimate"])
        self.assertIsNone(none["interval"])

    def test_paired_bootstrap_does_not_require_labels_for_raw_score_differences(self):
        rows = [record("a", "A", None, 0.8, 0.3), record("b", "B", None, 0.2, 0.3)]
        result = case_cluster_bootstrap(rows, "score_difference", channel="a", other_channel="b", draws=20, seed=4)
        self.assertAlmostEqual(result["estimate"], 0.2)
        self.assertEqual(result["n_unknown_labels_required"], 0)
        brier = case_cluster_bootstrap(rows, "brier_difference", channel="a", other_channel="b")
        self.assertIsNone(brier["estimate"])
        self.assertEqual(brier["n_unknown_labels_required"], 2)
        rows[0]["label"], rows[1]["label"] = True, False
        known = case_cluster_bootstrap(rows, "brier_difference", channel="a", other_channel="b", draws=20, seed=4)
        self.assertAlmostEqual(known["estimate"], -0.25)
        self.assertEqual(known["status"], "ok")

    def test_validation_rejects_coercion_nonfinite_scores_and_duplicate_assignments(self):
        valid = record("id", "case", True, 0.8)
        for score in (-0.1, 1.1, math.nan, math.inf, True, "0.8"):
            invalid = copy.deepcopy(valid)
            invalid["scores"]["a"] = score
            with self.subTest(score=score), self.assertRaises(ValueError):
                channel_metrics([invalid], "a")
        for label in (0, 1, "false"):
            invalid = copy.deepcopy(valid)
            invalid["label"] = label
            with self.subTest(label=label), self.assertRaises(ValueError):
                within_case_disagreement([invalid])
        with self.assertRaises(ValueError):
            paired_channel_scores([valid, valid], "a", "b")
        different_family = record("other", "case", False, 0.2, family="validation")
        with self.assertRaises(ValueError):
            case_cluster_bootstrap([valid, different_family], channel="a")
        for successes, total in ((1, 0), (-1, 3), (True, 2), (0, -1)):
            with self.assertRaises(ValueError):
                wilson_interval(successes, total)
        with self.assertRaises(ValueError):
            case_cluster_bootstrap([valid], "score_difference", channel="a")


if __name__ == "__main__":
    unittest.main()
