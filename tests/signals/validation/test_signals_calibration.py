import copy
import itertools
import json
import math
import unittest

from signals.validation.calibration import calibrate_negative_threshold
from signals.validation.calibration import categorical_within_case_disagreement
from signals.validation.calibration import paired_brier_bounds
from signals.validation.calibration import performance_bounds
from signals.validation.calibration import threshold_prediction


def record(identifier, label, score=None, other=None, case="case", family="ledger"):
    return {"assignment_id": identifier, "case_id": case, "family": family,
            "label": label, "scores": {"a": score, "b": other}}


def category_record(identifier, category, case="case", family="ledger"):
    return {"assignment_id": identifier, "case_id": case, "family": family, "category": category}


def completed_rows(rows):
    choices = []
    for row in rows:
        labels = (False, True) if row["label"] is None else (row["label"],)
        scores = (0.0, 1.0) if row["scores"]["a"] is None else (row["scores"]["a"],)
        choices.append(tuple(itertools.product(labels, scores)))
    return itertools.product(*choices)


class CalibrationTests(unittest.TestCase):
    def test_strict_calibration_excludes_ties_and_keeps_all_counts(self):
        rows = [record(str(i), False, score) for i, score in enumerate([0.1, 0.2, 0.8, 0.8])]
        rows += [record("missing", False), record("positive", True, 0.99), record("unknown", None, 0.99)]
        result = calibrate_negative_threshold(rows, "a", target_fpr=0.25)
        self.assertEqual(result["status"], "calibrated")
        self.assertEqual(result["threshold"], 0.8)
        self.assertEqual(result["comparator"], ">")
        self.assertEqual(result["n_negative_scores_equal_threshold"], 2)
        self.assertEqual(result["empirical_fpr"], 0)
        self.assertEqual(result["n_assigned"], 7)
        self.assertEqual(result["n_known_negatives"], 5)
        self.assertEqual(result["n_scored_negatives"], 4)
        self.assertEqual(result["n_missing_negative_scores"], 1)
        self.assertEqual(result["n_known_positives"], 1)
        self.assertEqual(result["n_unknown_labels"], 1)
        self.assertFalse(threshold_prediction(0.8, result["threshold"], result["comparator"]))
        self.assertTrue(threshold_prediction(0.9, result["threshold"], result["comparator"]))

    def test_comparator_endpoints_and_unattainable_case_have_no_sentinel(self):
        rows = [record("n", False, 1)]
        strict = calibrate_negative_threshold(rows, "a", 0, comparator=">")
        self.assertEqual(strict["threshold"], 1)
        self.assertEqual(strict["empirical_fpr"], 0)
        inclusive = calibrate_negative_threshold(rows, "a", 0, comparator=">=")
        self.assertEqual(inclusive["status"], "unattainable_with_comparator")
        self.assertIsNone(inclusive["threshold"])
        self.assertIsNone(inclusive["empirical_fpr"])
        for comparator in (">", ">="):
            full = calibrate_negative_threshold(rows, "a", 1, comparator=comparator)
            self.assertEqual(full["threshold"], 0)
            self.assertEqual(full["empirical_fpr"], 1)
        json.dumps([strict, inclusive], allow_nan=False)

    def test_inclusive_calibration_uses_next_float_and_target_comparison(self):
        rows = [record(str(i), False, value) for i, value in enumerate([0, 0.2, 0.2])]
        result = calibrate_negative_threshold(rows, "a", 0, comparator=">=")
        self.assertEqual(result["threshold"], math.nextafter(0.2, 1))
        self.assertEqual(result["n_negative_scores_equal_threshold"], 0)
        self.assertEqual(result["false_positive_count"], 0)
        # Rate comparison, not floor(target * n), avoids a floating-product off-by-one.
        rows = [record(str(i), False, 0 if i < 21 else 1) for i in range(50)]
        result = calibrate_negative_threshold(rows, "a", 0.58)
        self.assertEqual(result["threshold"], 0)
        self.assertEqual(result["false_positive_count"], 29)

    def test_no_negatives_and_unscored_negatives_are_distinct_undefined_cases(self):
        none = calibrate_negative_threshold([record("p", True, 0.2)], "a")
        missing = calibrate_negative_threshold([record("n", False)], "a")
        self.assertEqual(none["status"], "no_known_negatives")
        self.assertEqual(missing["status"], "no_scored_negatives")
        self.assertIsNone(none["threshold"])
        self.assertIsNone(missing["threshold"])


class PerformanceBoundsTests(unittest.TestCase):
    def assert_ratio_matches(self, actual, values, denominators):
        defined = [value for value in values if value is not None]
        self.assertEqual(actual["denominator_minimum"], min(denominators))
        self.assertEqual(actual["denominator_maximum"], max(denominators))
        self.assertEqual(actual["undefined_completion_possible"], any(value is None for value in values))
        if defined:
            self.assertAlmostEqual(actual["lower"], min(defined))
            self.assertAlmostEqual(actual["upper"], max(defined))
            for endpoint in ("lower", "upper"):
                counts = actual[f"{endpoint}_counts"]
                self.assertAlmostEqual(actual[endpoint], counts["numerator"] / counts["denominator"])
        else:
            self.assertEqual(actual["status"], "undefined_for_all_completions")
            self.assertIsNone(actual["lower"])
            self.assertIsNone(actual["upper"])

    def test_exact_ratio_and_brier_bounds_against_small_completion_oracle(self):
        specifications = list(itertools.product((False, True, None), (0.2, 0.8, None)))
        for specification in itertools.combinations_with_replacement(specifications, 3):
            rows = [record(str(i), label, score) for i, (label, score) in enumerate(specification)]
            for threshold, comparator in ((0.5, ">"), (0.5, ">="), (0, ">="), (0, ">"), (1, ">"), (1, ">=")):
                actual = performance_bounds(rows, "a", threshold, comparator=comparator)
                values = {name: [] for name in ("fpr", "recall", "precision")}
                denominators = {name: [] for name in values}
                briers = []
                for completed in completed_rows(rows):
                    predictions = [(label, score, score > threshold if comparator == ">" else score >= threshold)
                                   for label, score in completed]
                    positive = sum(label for label, _, _ in predictions)
                    negative = len(predictions) - positive
                    tp = sum(label and decision for label, _, decision in predictions)
                    fp = sum(not label and decision for label, _, decision in predictions)
                    counts = {"fpr": (fp, negative), "recall": (tp, positive), "precision": (tp, tp + fp)}
                    for metric, (numerator, denominator) in counts.items():
                        values[metric].append(numerator / denominator if denominator else None)
                        denominators[metric].append(denominator)
                    briers.append(sum((score - int(label)) ** 2 for label, score, _ in predictions) / len(predictions))
                for metric in values:
                    self.assert_ratio_matches(actual[metric], values[metric], denominators[metric])
                self.assertAlmostEqual(actual["brier"]["lower"], min(briers))
                self.assertAlmostEqual(actual["brier"]["upper"], max(briers))

    def test_missing_rows_are_retained_and_ratio_undefined_is_explicit(self):
        rows = [record("known", False, 0), record("unknown", None, None)]
        result = performance_bounds(rows, "a", 0.5)
        self.assertEqual(result["n_assigned"], 2)
        self.assertEqual(result["brier"]["denominator"], 2)
        self.assertEqual(result["brier"]["lower"], 0)
        self.assertEqual(result["brier"]["upper"], 0.5)
        self.assertEqual(result["recall"]["status"], "bounds_with_undefined_completions")
        self.assertEqual((result["recall"]["lower"], result["recall"]["upper"]), (0, 1))
        self.assertEqual((result["fpr"]["lower"], result["fpr"]["upper"]), (0, 0.5))
        self.assertEqual(result["prevalence"], {"lower": 0, "upper": 0.5, "denominator": 2})

    def test_no_positive_no_negative_no_prediction_and_empty_are_undefined(self):
        negative = performance_bounds([record("n", False, 0.1)], "a", 0.5)
        self.assertEqual(negative["recall"]["status"], "undefined_for_all_completions")
        self.assertEqual(negative["precision"]["status"], "undefined_for_all_completions")
        self.assertEqual(negative["fpr"]["lower"], 0)
        positive = performance_bounds([record("p", True, 0.9)], "a", 0.5)
        self.assertEqual(positive["fpr"]["status"], "undefined_for_all_completions")
        self.assertEqual(positive["recall"]["lower"], 1)
        empty = performance_bounds([], "a", 0.5)
        self.assertEqual(empty["fpr"]["status"], "undefined_for_all_completions")
        self.assertIsNone(empty["brier"]["lower"])
        self.assertIsNone(empty["prevalence"]["upper"])

    def test_same_unknown_label_is_shared_between_known_channel_scores(self):
        result = paired_brier_bounds([record("u", None, 0.2, 0.2)], "a", "b")
        self.assertEqual(result["brier_difference"]["lower"], 0)
        self.assertEqual(result["brier_difference"]["upper"], 0)
        opposite = paired_brier_bounds([record("u", None, 0.2, 0.8)], "a", "b")
        self.assertAlmostEqual(opposite["brier_difference"]["lower"], -0.6)
        self.assertAlmostEqual(opposite["brier_difference"]["upper"], 0.6)
        same_missing = paired_brier_bounds([record("u", None)], "a", "a")
        self.assertEqual(same_missing["brier_difference"]["lower"], 0)
        self.assertEqual(same_missing["brier_difference"]["upper"], 0)

    def test_paired_bounds_match_completion_oracle_and_reverse_direction(self):
        for label, a, b in itertools.product((False, True, None), (0.3, None), (0.8, None)):
            rows = [record("x", label, a, b), record("fixed", True, 0.6, 0.5)]
            result = paired_brier_bounds(rows, "a", "b")
            values = [((x - int(y)) ** 2 - (z - int(y)) ** 2 + 0.16 - 0.25) / 2
                      for y in ((False, True) if label is None else (label,))
                      for x in ((0, 1) if a is None else (a,))
                      for z in ((0, 1) if b is None else (b,))]
            self.assertAlmostEqual(result["brier_difference"]["lower"], min(values))
            self.assertAlmostEqual(result["brier_difference"]["upper"], max(values))
            reverse = paired_brier_bounds(rows, "b", "a")
            self.assertAlmostEqual(result["brier_difference"]["lower"], -reverse["brier_difference"]["upper"])
            self.assertAlmostEqual(result["brier_difference"]["upper"], -reverse["brier_difference"]["lower"])
        self.assertIsNone(paired_brier_bounds([], "a", "b")["brier_difference"]["lower"])


class CategoricalDisagreementTests(unittest.TestCase):
    def test_exact_categorical_bounds_against_consistent_completion_oracle(self):
        for alphabet in (("assertion", "no_statement"), ("assertion", "uncertain", "no_statement")):
            for size in range(6):
                # Combinations suffice because within-case pair counts ignore order.
                for labels in itertools.combinations_with_replacement(range(len(alphabet) + 1), size):
                    original = [alphabet[index] if index < len(alphabet) else None for index in labels]
                    rows = [category_record(str(i), value) for i, value in enumerate(original)]
                    result = categorical_within_case_disagreement(rows, alphabet)["pooled"]
                    disagreements = []
                    for completion in itertools.product(alphabet, repeat=original.count(None)):
                        replacement = iter(completion)
                        completed = [next(replacement) if value is None else value for value in original]
                        disagreements.append(sum(a != b for a, b in itertools.combinations(completed, 2)))
                    self.assertEqual(result["minimum_disagreements"], min(disagreements))
                    self.assertEqual(result["maximum_disagreements"], max(disagreements))

    def test_unavailable_is_not_a_category_and_no_statement_is_known(self):
        rows = [category_record("a", "no_statement"), category_record("b", "no_statement"), category_record("c", None)]
        result = categorical_within_case_disagreement(rows, ["assertion", "no_statement"])
        case = result["cases"][0]
        self.assertEqual(case["n_known"], 2)
        self.assertEqual(case["n_unavailable"], 1)
        self.assertEqual(case["known_disagreements"], 0)
        self.assertEqual((case["minimum_disagreements"], case["maximum_disagreements"]), (0, 2))
        with self.assertRaises(ValueError):
            categorical_within_case_disagreement(rows, ["assertion", "no_statement", "unavailable"])

    def test_prespecified_unseen_categories_and_no_cross_case_pairs(self):
        rows = [category_record(str(i), None, case="A") for i in range(3)]
        rows += [category_record("b1", "a", case="B"), category_record("b2", "b", case="B")]
        result = categorical_within_case_disagreement(rows, ["a", "b", "unseen"])
        self.assertEqual(result["pooled"]["all_pairs"], 4)
        self.assertEqual(result["pooled"]["minimum_disagreements"], 1)
        self.assertEqual(result["pooled"]["maximum_disagreements"], 4)
        self.assertEqual(result["n_cases_with_known_pairs"], 1)
        self.assertEqual(categorical_within_case_disagreement([], ["a"])["pooled"]["all_pairs"], 0)
        one = categorical_within_case_disagreement([category_record("x", None)], ["a"])
        self.assertIsNone(one["pooled"]["disagreement_lower"])


class ValidationTests(unittest.TestCase):
    def test_validation_and_nonmutation(self):
        rows = [record("a", None, 0.2, None), record("b", False, None, 0.8)]
        before = copy.deepcopy(rows)
        outputs = [calibrate_negative_threshold(rows, "a"), performance_bounds(rows, "a", 0.5),
                   paired_brier_bounds(rows, "a", "b")]
        self.assertEqual(rows, before)
        json.dumps(outputs, allow_nan=False)
        for value in (True, math.nan, math.inf, -0.1, 1.1, "0.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                performance_bounds(rows, "a", value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                calibrate_negative_threshold(rows, "a", value)
        for comparator in ("<", "==", "", None, True, []):
            with self.subTest(comparator=comparator), self.assertRaises(ValueError):
                calibrate_negative_threshold(rows, "a", comparator=comparator)
        for helper in (lambda value: calibrate_negative_threshold(value, "a"),
                       lambda value: performance_bounds(value, "a", 0.5),
                       lambda value: paired_brier_bounds(value, "a", "b")):
            with self.assertRaises(ValueError):
                helper([rows[0], rows[0]])
            with self.assertRaises(ValueError):
                helper([record("x", 1, 0.5)])
            with self.assertRaises(ValueError):
                helper([record("x", True, math.nan)])

    def test_categorical_validation_and_nonmutation(self):
        rows = [category_record("x", "a"), category_record("y", None)]
        before = copy.deepcopy(rows)
        categorical_within_case_disagreement(rows, ["a", "b"])
        self.assertEqual(rows, before)
        for alphabet in ([], "ab", ["a", "a"], ["a", 1], [""]):
            with self.assertRaises(ValueError):
                categorical_within_case_disagreement(rows, alphabet)
        for invalid in ([rows[0], rows[0]], [category_record("z", "undeclared")],
                        [category_record("z", True)], [category_record("z", [])],
                        [rows[0], category_record("y", "b", family="validator")]):
            with self.assertRaises(ValueError):
                categorical_within_case_disagreement(invalid, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
