import copy
import hashlib
import json
import unittest

from causes.campaign_qualification import qualify_finite_phase_journal


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class FinitePhaseQualificationTests(unittest.TestCase):
    def setUp(self):
        self.ids = ["first", "second"]
        self.jobs = [{"allocation": {"run_id": run_id}} for run_id in self.ids]
        self.config = {"model": "fixture"}
        self.summaries = {run_id: digest(run_id) for run_id in self.ids}
        self.rows = [
            {"event": "claimed", "run_id": job["allocation"]["run_id"],
             "job_sha256": digest(job), "config_sha256": digest(self.config)}
            for job in self.jobs
        ]
        self.rows.extend(self.finish("first"))
        self.rows.append({"event": "waiting_for_operator_pause_no_eligible_extensions", "at": "exhaustion"})
        self.rows.extend(self.finish("second"))

    def finish(self, run_id):
        return [
            {"event": "episode_finished", "run_id": run_id, "technical_failure": False,
             "replayed": False, "summary_sha256": self.summaries[run_id]},
            {"event": "route_success", "run_id": run_id},
        ]

    def qualify(self, rows=None, **overrides):
        raw = b"".join(json.dumps(row).encode() + b"\n" for row in (self.rows if rows is None else rows))
        args = dict(raw=raw, ids=self.ids, jobs=self.jobs, config=self.config, summaries=self.summaries)
        args.update(overrides)
        return qualify_finite_phase_journal(**args)

    def test_exhaustion_before_final_drain_is_successful(self):
        result = self.qualify()
        self.assertEqual(result["route_successes"], 2)
        self.assertTrue(result["exhaustion"]["preceded_last_final"])
        self.assertEqual(result["exhaustion"]["unfinished_at_first_event"], 1)

    def test_exhaustion_after_final_drain_is_also_successful(self):
        rows = copy.deepcopy(self.rows)
        rows.append(rows.pop(4))
        self.assertFalse(self.qualify(rows)["exhaustion"]["preceded_last_final"])

    def test_exhaustion_alone_cannot_qualify_unfinished_work(self):
        with self.assertRaises(ValueError):
            self.qualify(self.rows[:-2])

    def test_missing_duplicate_or_early_success_is_rejected(self):
        for rows in (self.rows[:-1], self.rows + [self.rows[-1]], [self.rows[-1]] + self.rows[:-1]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.qualify(rows)

    def test_missingness_replay_and_summary_drift_remain_rejected(self):
        for key, value in (("technical_failure", True), ("replayed", True), ("summary_sha256", "changed")):
            rows = copy.deepcopy(self.rows)
            rows[2][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.qualify(rows)

    def test_fault_or_unresolved_history_requires_separate_review(self):
        for event in ("route_failure", "route_quarantined", "route_requalified", "unresolved_prior_attempt", "unresolved_current_attempt", "extension_block_created", "controller_error"):
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.qualify(self.rows + [{"event": event}])

    def test_frozen_identity_and_claim_binding_cannot_drift(self):
        for override in ({"ids": ["first", "first"]}, {"jobs": self.jobs[::-1]}, {"config": {}}, {"summaries": {}}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.qualify(**override)

    def test_early_missing_exhaustion_or_truncated_journal_rejected(self):
        for rows in ([self.rows[4]] + self.rows[:4] + self.rows[5:], self.rows[:4] + self.rows[5:]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.qualify(rows)
        with self.assertRaises(ValueError):
            self.qualify(raw=b'{"event":"claimed"}')


if __name__ == "__main__":
    unittest.main()
