import tempfile
import unittest
from pathlib import Path

from emergence.checkpoints.completeness import audit_samples


class ExecutionCompletenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        (self.path / "development").mkdir()
        for name in ("evidence.json", "outcomes.json", "model_output.json"):
            (self.path / "development" / name).write_text("{}")
        self.row = {
            "sample_id": "one", "latest_attempt": True, "sample_status": "completed",
            "score.development_outcomes.value": '{"task_success":null}',
            "artifact_export_errors": "[]", "artifacts": str(self.path),
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_unknown_behavior_can_be_complete_execution_but_errors_cannot(self):
        self.assertTrue(audit_samples([self.row], ["one"])["complete"])
        for change in ({"error": "scorer failed"}, {"sample_status": "error"}, {"sample_status": "limit"}, {"score.development_outcomes.value": ""}, {"artifact_export_errors": '[{"error":"failed"}]'}):
            with self.subTest(change=change):
                audit = audit_samples([{**self.row, **change}], ["one"])
                self.assertFalse(audit["complete"])
                self.assertEqual(len(audit["failed_samples"]), 1)

    def test_assignment_denominator_and_retry_identity_are_checked(self):
        for rows, expected in (([], ["one"]), ([self.row], ["one", "two"]), ([self.row, self.row], ["one"]), ([self.row], ["different"])):
            self.assertFalse(audit_samples(rows, expected)["complete"])
        retry = {**self.row, "latest_attempt": False, "sample_status": "error"}
        self.assertTrue(audit_samples([retry, self.row], ["one"])["complete"])

    def test_lost_export_is_detected(self):
        (self.path / "development/evidence.json").unlink()
        self.assertFalse(audit_samples([self.row], ["one"])["complete"])


if __name__ == "__main__":
    unittest.main()
