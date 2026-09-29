import unittest

from report import summarize


class ReportTests(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(
            summarize([{"amount_cents": 125}, {"amount_cents": -25}]),
            {"count": 2, "total_cents": 100},
        )

    def test_missing_amount(self):
        self.assertEqual(summarize([{}]), {"count": 1, "total_cents": 0})


if __name__ == "__main__":
    unittest.main()
