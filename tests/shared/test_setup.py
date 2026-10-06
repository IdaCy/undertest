import json
import subprocess
import sys
import unittest

from shared.tasks.report_task import CASES
from shared.tasks.report_task import FIXTURES
from shared.tasks.report_task import ROOT
from shared.tasks.report_task import expected_report
from shared.tasks.report_task import report_task


class SetupTests(unittest.TestCase):
    def test_conditions_change_only_the_framing_line(self):
        ordinary = report_task("ordinary").dataset[0]
        evaluation = report_task("evaluation").dataset[0]
        self.assertEqual(ordinary.id, evaluation.id)
        left = ordinary.input.splitlines()
        right = evaluation.input.splitlines()
        self.assertEqual(len(left), len(right))
        self.assertEqual([i for i, (a, b) in enumerate(zip(left, right)) if a != b], [0])

    def test_correct_and_broken_programs_are_distinguishable(self):
        results = {}
        for path in (FIXTURES / "report.py", ROOT / "tests" / "shared" / "reference_report.py"):
            passes = 0
            for records in CASES:
                result = subprocess.run(
                    [sys.executable, "-I", str(path)],
                    input=json.dumps(records), capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    passes += json.loads(result.stdout) == expected_report(records)
            results[path.name] = passes
        self.assertEqual(results["reference_report.py"], len(CASES))
        self.assertLess(results["report.py"], len(CASES))

    def test_unknown_condition_fails_before_running(self):
        with self.assertRaises(ValueError):
            report_task("typo")


if __name__ == "__main__":
    unittest.main()
