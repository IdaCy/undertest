import unittest
from dataclasses import replace
from fractions import Fraction

from emergence.checkpoints.fresh_reconciliation import Assignment
from emergence.checkpoints.fresh_reconciliation import Observation
from emergence.checkpoints.fresh_reconciliation import identification_bounds
from emergence.checkpoints.fresh_reconciliation import reconcile


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.rows = (
            Assignment("sft::a", "sft", "a", "a" * 64, 1, "main", Fraction(-1, 2)),
            Assignment("released::a", "released", "a", "a" * 64, 1, "main", Fraction(1, 2)),
            Assignment("sft::control", "sft", "control", "b" * 64, 2, "format", None),
        )

    def observation(self, index=0, **changes):
        row = self.rows[index]
        base = Observation(row.physical_id, row.checkpoint_id, row.sample_id,
                           row.prompt_sha256, row.seed, "attempt-" + str(index),
                           "accepted", "returned", True, False, None)
        return replace(base, **changes)

    def test_uncertain_boolean_is_unknown_without_losing_the_attempt(self):
        result = reconcile(self.rows, [self.observation(custody="uncertain")])
        self.assertEqual([r.assignment for r in result], list(self.rows))
        self.assertEqual(result[0].attempt_id, "attempt-0")
        self.assertEqual(result[0].status, "uncertain_custody")
        self.assertIsNone(result[0].segmented)
        self.assertEqual(result[1].status, "no_record")
        self.assertEqual(identification_bounds(self.rows, [self.observation(custody="uncertain")], "segmented"), {
            "known_main": 0, "unknown_main": 2, "lower": Fraction(-1, 2),
            "upper": Fraction(1, 2), "width": Fraction(1),
        })

    def test_truncation_cancel_failure_and_unknown_cannot_supply_outcomes(self):
        for status in ("length_truncated", "cancelled", "failed", "unknown"):
            with self.subTest(status=status):
                result = reconcile(self.rows, [self.observation(termination=status)])
                self.assertIsNone(result[0].segmented)
                self.assertIsNone(result[0].raw)
                self.assertEqual(result[0].status, status)

    def test_endpoints_disagree_and_missing_checkpoint_stays_in_bounds(self):
        records = [self.observation(), self.observation(2)]
        self.assertEqual(identification_bounds(self.rows, records, "segmented")["lower"], Fraction(-1, 2))
        self.assertEqual(identification_bounds(self.rows, records, "segmented")["upper"], 0)
        self.assertEqual(identification_bounds(self.rows, records, "raw")["lower"], 0)
        self.assertEqual(identification_bounds(self.rows, records, "raw")["upper"], Fraction(1, 2))
        self.assertEqual(identification_bounds(self.rows, records, "conservative")["unknown_main"], 2)

    def test_observation_order_never_changes_roster_order(self):
        result = reconcile(self.rows, [self.observation(2), self.observation(1), self.observation(0)])
        self.assertEqual(tuple(r.assignment for r in result), self.rows)

    def test_duplicate_conflicting_outside_and_reused_attempts_refuse(self):
        first = self.observation()
        cases = ([first, first], [first, replace(first, segmented=False)],
                 [replace(first, physical_id="outside")],
                 [first, self.observation(1, attempt_id=first.attempt_id)])
        for records in cases:
            with self.subTest(records=records), self.assertRaises(ValueError):
                reconcile(self.rows, records)

    def test_identity_mismatch_refuses_even_if_custody_is_uncertain(self):
        for changed in ({"checkpoint_id": "other"}, {"sample_id": "other"},
                        {"seed": 3}, {"seed": True}, {"prompt_sha256": "c" * 64}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                reconcile(self.rows, [self.observation(custody="uncertain", **changed)])

    def test_non_boolean_and_unknown_dispositions_refuse(self):
        for changed in ({"segmented": 1}, {"raw": "false"}, {"conservative": []},
                        {"custody": "probably"}, {"termination": "done"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                reconcile(self.rows, [self.observation(**changed)])

    def test_invalid_roster_and_duplicate_bound_observations_refuse(self):
        with self.assertRaises(ValueError):
            reconcile([], [])
        with self.assertRaises(ValueError):
            identification_bounds(self.rows[2:], [], "segmented")
        with self.assertRaises(ValueError):
            reconcile(self.rows + self.rows[:1], [])
        with self.assertRaises(ValueError):
            reconcile([replace(self.rows[2], coefficient=Fraction(1))], [])
        with self.assertRaises(ValueError):
            identification_bounds(self.rows, [self.observation(), self.observation()], "segmented")


if __name__ == "__main__":
    unittest.main()
