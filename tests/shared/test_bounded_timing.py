import itertools
import json
import unittest
from unittest.mock import Mock, patch

from shared import bounded_timing as timing


class Cancelled(BaseException):
    pass


class BoundedTimingTests(unittest.TestCase):
    def sample(self, clock=None):
        return timing.Sampler(enabled=True, clock=clock or itertools.count().__next__).begin()

    def test_nested_spans_keep_start_order_and_exact_durations(self):
        sample = self.sample(iter((10, 12, 17, 25)).__next__)
        with timing.span(sample, 4):
            with timing.span(sample, 2):
                pass
        record = timing.annotation(sample)
        self.assertEqual(record['spans'], [
            dict(code=4, elapsed_ns=15, raised=False),
            dict(code=2, elapsed_ns=5, raised=False)])
        self.assertEqual(record['ordinal'], 1)
        self.assertEqual(record['missing'], [])

    def test_first_sixteen_calls_include_failures_and_exhaustion_is_dormant(self):
        clock = Mock(side_effect=itertools.count())
        sampler = timing.Sampler(enabled=True, clock=clock)
        for ordinal in range(1, 17):
            sample = sampler.begin()
            try:
                with timing.span(sample, 8):
                    if ordinal % 2:
                        raise ValueError('body failure')
            except ValueError:
                pass
            self.assertEqual(timing.annotation(sample)['ordinal'], ordinal)
            self.assertEqual(timing.annotation(sample)['spans'][0]['raised'], bool(ordinal % 2))
        calls = clock.call_count
        with patch.object(timing.json, 'dumps', side_effect=AssertionError('not called')):
            for _ in range(4):
                sample = sampler.begin()
                self.assertIsNone(sample)
                with timing.span(sample, 8):
                    pass
                self.assertIsNone(timing.annotation(sample))
        self.assertEqual(clock.call_count, calls)
        self.assertEqual(sampler.selected, 16)

    def test_disabled_never_calls_clock_or_encoder(self):
        clock = Mock(side_effect=AssertionError('not called'))
        sampler = timing.Sampler(clock=clock)
        marker = object()
        def work():
            with timing.span(sampler.begin(), 1):
                return marker
        with patch.object(timing.json, 'dumps', side_effect=AssertionError('not called')):
            self.assertIs(work(), marker)
            self.assertIsNone(timing.annotation(sampler.begin()))
        clock.assert_not_called()
        self.assertEqual(sampler.selected, 0)

    def test_sample_construction_fault_still_consumes_selected_ordinal(self):
        sampler = timing.Sampler(enabled=True)
        with patch.object(timing, '_Sample', side_effect=MemoryError('diagnostic')):
            self.assertIsNone(sampler.begin())
        self.assertEqual(timing.annotation(sampler.begin())['ordinal'], 2)

    def test_span_cap_does_not_skip_work_or_call_clock_after_exhaustion(self):
        clock = Mock(side_effect=itertools.count())
        sample = self.sample(clock)
        bodies = []
        for index in range(45):
            with timing.span(sample, 1):
                bodies.append(index)
        record = timing.annotation(sample)
        self.assertEqual(len(record['spans']), 40)
        self.assertEqual(record['missing'], ['span_limit'])
        self.assertEqual(clock.call_count, 80)
        self.assertEqual(bodies, list(range(45)))

    def test_largest_valid_durations_fit_cap_and_output_is_detached(self):
        sample = self.sample(iter([0, 2**63 - 1] * 40).__next__)
        for _ in range(40):
            with timing.span(sample, 255):
                pass
        record = timing.annotation(sample)
        self.assertLessEqual(len(json.dumps(record, separators=(',', ':')).encode()), 4096)
        record['spans'][0]['code'] = 99
        record['missing'].append('external')
        self.assertEqual(timing.annotation(sample)['spans'][0]['code'], 255)
        self.assertEqual(timing.annotation(sample)['missing'], [])

    def test_hard_byte_limit_omits_instead_of_truncating_or_retrying(self):
        sample = self.sample()
        with timing.span(sample, 1):
            pass
        with patch.object(timing, 'MAX_SAMPLE_BYTES', 1):
            self.assertIsNone(timing.annotation(sample))
        self.assertIn('byte_limit', sample.missing)

    def test_invalid_clocks_and_regression_are_fixed_missing_reasons(self):
        for value in (True, -1, 2**63, 1.5, 'private clock text'):
            with self.subTest(value=type(value).__name__):
                clock = Mock(return_value=value)
                sample = self.sample(clock)
                with timing.span(sample, 1):
                    pass
                with timing.span(sample, 2):
                    pass
                self.assertEqual(clock.call_count, 1)
                self.assertEqual(timing.annotation(sample)['missing'], ['invalid_clock'])
        sample = self.sample(iter((5, 4)).__next__)
        with timing.span(sample, 1):
            pass
        self.assertEqual(timing.annotation(sample)['missing'], ['clock_regressed'])
        self.assertIsNone(timing.annotation(sample)['spans'][0]['elapsed_ns'])

    def test_ordinary_clock_faults_never_repeat_or_skip_work_and_keep_return(self):
        for values in ([OSError('private diagnostic text')], [1, OSError('private diagnostic text')]):
            with self.subTest(at_start=len(values) == 1):
                sample = self.sample(Mock(side_effect=values))
                calls, marker = [], object()
                def work():
                    with timing.span(sample, 1):
                        calls.append('body')
                        return marker
                self.assertIs(work(), marker)
                self.assertEqual(calls, ['body'])
                record = timing.annotation(sample)
                self.assertEqual(record['missing'], ['clock_error'])
                self.assertNotIn('private', json.dumps(record))

    def test_body_exceptions_keep_object_and_traceback_during_exit_diagnostic_faults(self):
        def body(error):
            raise error
        def invoke(sample, error):
            with timing.span(sample, 1):
                body(error)
        def frames(error):
            result, trace = [], error.__traceback__
            while trace:
                result.append((trace.tb_frame.f_code.co_name, trace.tb_lineno))
                trace = trace.tb_next
            return result[1:]
        for body_kind in (ValueError, KeyboardInterrupt, SystemExit, Cancelled):
            baseline = body_kind('body')
            try:
                invoke(None, baseline)
            except BaseException as caught:
                expected = frames(caught)
            for diagnostic_kind in (OSError, KeyboardInterrupt, SystemExit, Cancelled):
                with self.subTest(body=body_kind.__name__, diagnostic=diagnostic_kind.__name__):
                    original = body_kind('body')
                    sample = self.sample(Mock(side_effect=[1, diagnostic_kind('diagnostic')]))
                    try:
                        invoke(sample, original)
                    except BaseException as caught:
                        self.assertIs(caught, original)
                        self.assertEqual(frames(caught), expected)
                    else:
                        self.fail('original exception was swallowed')
                    self.assertTrue(timing.annotation(sample)['spans'][0]['raised'])

    def test_control_signal_before_or_after_body_propagates_without_retry(self):
        for kind in (KeyboardInterrupt, SystemExit, Cancelled):
            for at_start in (True, False):
                with self.subTest(kind=kind.__name__, at_start=at_start):
                    signal = kind('cancel')
                    sample = self.sample(Mock(side_effect=[signal] if at_start else [1, signal]))
                    calls = []
                    with self.assertRaises(kind) as caught:
                        with timing.span(sample, 1):
                            calls.append('body')
                    self.assertIs(caught.exception, signal)
                    self.assertEqual(calls, [] if at_start else ['body'])

    def test_internal_span_fault_is_contained_without_calling_body_twice(self):
        sample = self.sample()
        calls = []
        with patch.object(sample, 'start', side_effect=RuntimeError('diagnostic')):
            with timing.span(sample, 1):
                calls.append('body')
        self.assertEqual(calls, ['body'])
        self.assertEqual(timing.annotation(sample)['missing'], ['diagnostic_error'])

    def test_encoder_failure_is_contained_but_cancellation_respects_unwinding(self):
        sample = self.sample()
        with timing.span(sample, 1):
            pass
        with patch.object(timing.json, 'dumps', side_effect=OSError('private diagnostic text')):
            self.assertIsNone(timing.annotation(sample))
        self.assertIn('encoding_error', sample.missing)
        for kind in (KeyboardInterrupt, SystemExit, Cancelled):
            signal = kind('cancel')
            with patch.object(timing.json, 'dumps', side_effect=signal):
                with self.assertRaises(kind) as caught:
                    timing.annotation(sample)
                self.assertIs(caught.exception, signal)
                self.assertIsNone(timing.annotation(sample, unwinding=True))

    def test_invalid_stages_and_tampered_data_cannot_emit_private_values(self):
        clock = Mock(side_effect=itertools.count())
        sample = self.sample(clock)
        for code in (True, -1, 256, 'private path', object()):
            with timing.span(sample, code):
                pass
        clock.assert_not_called()
        self.assertEqual(timing.annotation(sample)['missing'], ['invalid_stage'])
        sample.rows.append(dict(code=1, elapsed_ns=1, raised=False, path='private path'))
        self.assertNotIn('private', json.dumps(timing.annotation(sample)))
        sample.rows[0]['elapsed_ns'] = 'private path'
        self.assertIsNone(timing.annotation(sample))
        sample.rows.clear()
        sample.missing.append('private path')
        self.assertIsNone(timing.annotation(sample))


if __name__ == '__main__':
    unittest.main()
