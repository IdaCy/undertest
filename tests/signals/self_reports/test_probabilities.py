import unittest

from signals.self_reports.probabilities import MAX_FINAL_CHARS
from signals.self_reports.probabilities import parse_source_probability


class SourceProbabilityTests(unittest.TestCase):
    def test_valid_boundaries_and_whitespace(self):
        for text, expected in [
            ('{"p_eval_source":0}', 0.0),
            (' \n{"p_eval_source":1}\t', 1.0),
            ('{"p_eval_source":0.375}', 0.375),
            ('{"p_eval_source":1e-3}', 0.001),
        ]:
            with self.subTest(text=text):
                result = parse_source_probability(text, finish_reason="stop")
                self.assertEqual(result.status, "valid")
                self.assertEqual(result.probability, expected)

    def test_invalid_payloads_are_missing_not_zero(self):
        for text in [
            '{"p_eval_source":true}', '{"p_eval_source":"0.5"}',
            '{"p_eval_source":null}', '{"p_eval_source":NaN}',
            '{"p_eval_source":Infinity}', '{"p_eval_source":1e9999}',
            '{"p_eval_source":-0.01}', '{"p_eval_source":1.01}',
            '{"p_eval_source":1.0000000000000001}', '{"p_eval_source":-1e-9999}',
            '{"p_eval_source":0.2,"p_eval_source":0.8}',
            '{"p_eval_source":0.2,"reason":"guess"}',
            '0.5', '[{"p_eval_source":0.5}]', '{}',
            '```json\n{"p_eval_source":0.5}\n```',
            '{"p_eval_source":0.5} trailing',
            '{"p_eval_source":0.5}{"p_eval_source":0.5}',
            '<think>reason</think>{"p_eval_source":0.5}',
            '[\n' * 2000, ' ' * (MAX_FINAL_CHARS + 1), None,
        ]:
            with self.subTest(text=str(text)[:50]):
                result = parse_source_probability(text, finish_reason="stop")
                self.assertEqual(result.status, "invalid")
                self.assertIsNone(result.probability)

    def test_completion_and_transport_take_precedence(self):
        for reason in ["length", "max_tokens", "max_length"]:
            result = parse_source_probability('{"p_eval_source":1}', finish_reason=reason)
            self.assertEqual(result.status, "truncated")
            self.assertIsNone(result.probability)
        for reason in [None, False, "tool_calls", "content_filter"]:
            self.assertEqual(
                parse_source_probability('{"p_eval_source":1}', finish_reason=reason).status,
                "unscorable",
            )
        self.assertEqual(
            parse_source_probability('{"p_eval_source":1}', finish_reason="stop", transport_status="timeout").status,
            "request_error",
        )


if __name__ == "__main__":
    unittest.main()
