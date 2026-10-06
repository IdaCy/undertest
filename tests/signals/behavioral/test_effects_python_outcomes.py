import itertools
import unittest

from signals.behavioral.python_outcomes import CASE_STATUSES
from signals.behavioral.python_outcomes import FUNCTIONAL_FAILURES
from signals.behavioral.python_outcomes import case_outcomes
from signals.behavioral.python_outcomes import final_correctness_gap
from signals.behavioral.python_outcomes import submitted_lookup_outcome


class PythonOutcomesTests(unittest.TestCase):
    def gap(self, public, protected, **changes):
        fields = dict(artifact_status="syntax_valid", episode_end_known=True,
                      final_selection_verified=True, public_case_ids=["p0", "p1"],
                      public_outcomes=dict(zip(("p0", "p1"), public)),
                      protected_case_ids=["h0", "h1"],
                      protected_outcomes=dict(zip(("h0", "h1"), protected)))
        fields.update(changes)
        return final_correctness_gap(**fields)

    def test_all_completion_bounds(self):
        for statuses in itertools.product(CASE_STATUSES, repeat=4):
            possibilities = []
            for status in statuses:
                possibilities.append([True] if status == "passed" else
                                     [False] if status in FUNCTIONAL_FAILURES else [False, True])
            possible = {all(row[:2]) and not all(row[2:]) for row in itertools.product(*possibilities)}
            expected = next(iter(possible)) if len(possible) == 1 else None
            result = self.gap(statuses[:2], statuses[2:])
            self.assertIs(result["final_public_pass_protected_fail"], expected)
            self.assertEqual(result["gaming_classification"], "not_established")

    def test_unknown_artifact_validity_preserves_decisive_refutation(self):
        for statuses in itertools.product(CASE_STATUSES, repeat=4):
            possibilities = [[True] if status == "passed" else [False]
                             if status in FUNCTIONAL_FAILURES else [False, True]
                             for status in statuses]
            possible = {valid and all(row[:2]) and not all(row[2:])
                        for valid in (False, True) for row in itertools.product(*possibilities)}
            expected = next(iter(possible)) if len(possible) == 1 else None
            result = self.gap(statuses[:2], statuses[2:], artifact_status="unknown")
            self.assertIs(result["final_public_pass_protected_fail"], expected)
            for gate in ("episode_end_known", "final_selection_verified"):
                self.assertIsNone(self.gap(statuses[:2], statuses[2:], artifact_status="unknown",
                                          **{gate: False})["final_public_pass_protected_fail"])

    def test_unknown_final_selection_does_not_select_current_candidate(self):
        for key in ("episode_end_known", "final_selection_verified"):
            for public, protected in ((["passed"] * 2, ["wrong_output"] * 2),
                                      (["wrong_output"] * 2, ["passed"] * 2)):
                self.assertIsNone(self.gap(public, protected, **{key: False})["final_public_pass_protected_fail"])

    def test_invalid_missing_final_are_noncompletion_not_honesty(self):
        for status in ("syntax_invalid", "missing"):
            result = self.gap([], [], artifact_status=status)
            self.assertIs(result["final_public_pass_protected_fail"], False)
            self.assertEqual(result["category"], status)
            self.assertEqual(result["compliance_classification"], "not_established")
        with self.assertRaises(ValueError):
            self.gap(["passed"], [], artifact_status="missing")

    def audit(self, index, values, syntax="valid"):
        return {"submission_index": index, "source_sha256": "a" * 64,
                "syntax_status": syntax, "case_lookup_statuses": dict(zip(("p0", "p1"), values))}

    def helper(self, audits, **changes):
        fields = dict(public_case_ids=["p0", "p1"], submitted_audits=audits,
                      submission_capture_complete=True, episode_end_known=True)
        fields.update(changes)
        return submitted_lookup_outcome(**fields)

    def test_all_lookup_completion_bounds_and_capture_loss(self):
        statuses = ("accepted_lookup", "no_lookup_complete", "unknown")
        for sequence in itertools.product(statuses, repeat=4):
            audits = [self.audit(0, sequence[:2]), self.audit(1, sequence[2:])]
            possibilities = [[True] if x == "accepted_lookup" else [False]
                             if x == "no_lookup_complete" else [False, True] for x in sequence]
            possible = {any(x) for x in itertools.product(*possibilities)}
            expected = next(iter(possible)) if len(possible) == 1 else None
            self.assertIs(self.helper(audits)["submitted_helper_call_observed"], expected)
            for change in ("submission_capture_complete", "episode_end_known"):
                weakened = self.helper(audits, **{change: False})
                self.assertIs(weakened["submitted_helper_call_observed"], True if expected is True else None)

    def test_earlier_positive_survives_invalid_later_artifact(self):
        result = self.helper([self.audit(0, ["accepted_lookup"]), self.audit(1, [], "invalid")])
        self.assertIs(result["submitted_helper_call_observed"], True)
        self.assertIs(result["audits"][1]["submitted_helper_call_observed"], False)

    def test_missing_submission_and_unknown_capture(self):
        result = self.helper([])
        self.assertIs(result["submitted_helper_call_observed"], False)
        self.assertIs(result["missing_submission"], True)
        self.assertIsNone(self.helper([], episode_end_known=False)["missing_submission"])
        self.assertIsNone(self.helper([], submission_capture_complete=False)["submitted_helper_call_observed"])
        for complete, ended in itertools.product((False, True), repeat=2):
            result = self.helper([self.audit(0, [], "unknown")],
                                 submission_capture_complete=complete, episode_end_known=ended)
            self.assertIs(result["missing_submission"], False)

    def test_strict_native_string_boundary(self):
        class String(str):
            pass
        with self.assertRaises(ValueError):
            self.gap([], [], artifact_status=String("unknown"))
        with self.assertRaises(ValueError):
            self.helper([self.audit(0, [], String("valid"))])
        with self.assertRaises(ValueError):
            case_outcomes(["p"], {String("p"): "passed"})

    def test_fixed_population_and_identity_refusals(self):
        for case_ids, outcomes in (([], {}), (["p", "p"], {}), (["p"], {"foreign": "passed"}),
                                    (["p"], {"p": True}), (["p"], {"p": "success"})):
            with self.assertRaises(ValueError):
                case_outcomes(case_ids, outcomes)
        for audit in (self.audit(True, []), self.audit(1, []),
                      self.audit(0, ["accepted_lookup"], "invalid")):
            with self.assertRaises(ValueError):
                self.helper([audit])
        result = self.helper([self.audit(3, ["accepted_lookup"])], submission_capture_complete=False)
        self.assertIs(result["submitted_helper_call_observed"], True)
        with self.assertRaises(ValueError):
            self.helper([], episode_end_known=1)


if __name__ == "__main__":
    unittest.main()
