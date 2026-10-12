import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from causes import campaign_runner as runner
from causes.campaign_concurrency_evidence import (actor_request_exposure, aggregate_generation_windows,
                                                audit_self_probe_traffic, audit_prefix_self_probe_traffic)


def at(seconds):
    return (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()


def episode(rid, intervals):
    return {'run_id': rid, 'episode_elapsed_seconds': 9999, 'requests': [
        {'turn': i, 'started_at': at(a), 'finished_at': at(b), 'elapsed_seconds': b-a}
        for i, (a, b) in enumerate(intervals)]}


class ExposureTests(unittest.TestCase):
    def test_three_distinct_runs_and_default_pairwise_behavior(self):
        rows = [episode('a', [(0, 20), (30, 40)]), episode('b', [(10, 35)]), episode('c', [(15, 25)])]
        result = actor_request_exposure(rows, maximum_clock_difference_seconds=.1, minimum_concurrent_runs=3)
        self.assertEqual(result['cross_job_actor_overlap_seconds'], 5)
        self.assertEqual(result['cross_job_actor_overlap_intervals'], [[
            dt.datetime.fromisoformat(at(15)).timestamp(), dt.datetime.fromisoformat(at(20)).timestamp()]])
        self.assertEqual(actor_request_exposure(rows, maximum_clock_difference_seconds=.1),
                         actor_request_exposure(rows, maximum_clock_difference_seconds=.1, minimum_concurrent_runs=2))

    def test_multiple_requests_from_two_runs_do_not_count_as_three(self):
        rows = [episode('a', [(0, 10), (10, 20), (20, 30)]), episode('b', [(0, 30)])]
        self.assertEqual(actor_request_exposure(rows, maximum_clock_difference_seconds=.1,
            minimum_concurrent_runs=3)['cross_job_actor_overlap_seconds'], 0)

    def test_touching_zero_length_intervals_and_changed_membership(self):
        rows = [episode('a', [(0, 10)]), episode('b', [(0, 20)]),
                episode('c', [(10, 20)]), episode('d', [(10, 10)])]
        value = actor_request_exposure(rows, maximum_clock_difference_seconds=.1, minimum_concurrent_runs=3)
        self.assertEqual(value['cross_job_actor_overlap_seconds'], 0)
        rows[3] = episode('d', [(0, 20)])
        value = actor_request_exposure(rows, maximum_clock_difference_seconds=.1, minimum_concurrent_runs=3)
        self.assertEqual(value['cross_job_actor_overlap_seconds'], 20)
        self.assertEqual(len(value['cross_job_actor_overlap_intervals']), 1)

    def test_invalid_overlap_thresholds_refuse(self):
        for value in (0, 1, True, 3.0):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'invalid_overlap_concurrency'):
                actor_request_exposure([episode('a', [(0, 10)]), episode('b', [(0, 10)])],
                    maximum_clock_difference_seconds=.1, minimum_concurrent_runs=value)

    def test_actor_spans_exclude_summary_and_overlap_is_union(self):
        value = actor_request_exposure([episode('a', [(0, 20), (30, 40)]),
                                        episode('b', [(10, 35)]), episode('c', [(15, 25)])],
                                       maximum_clock_difference_seconds=.1)
        self.assertEqual(value['sum_actor_span_seconds'], 75)
        self.assertEqual(value['cross_job_actor_overlap_seconds'], 20)
        self.assertEqual(value['successful_requests'], 4)

    def test_clock_jump_is_retained_and_inconclusive(self):
        rows = [episode('a', [(0, 20)]), episode('b', [(5, 25)])]
        rows[0]['requests'][0]['elapsed_seconds'] = 1
        value = actor_request_exposure(rows, maximum_clock_difference_seconds=.1)
        self.assertFalse(value['timing_consistent'])
        self.assertEqual(value['episodes'][0]['requests'][0]['clock_difference_seconds'], 19)

    def test_duplicate_reordered_and_overlapping_requests_refuse(self):
        good = [episode('a', [(0, 20), (30, 40)]), episode('b', [(5, 25)])]
        changes = [('run_id', 'a'), ('turn', 5), ('started_at', at(10))]
        for field, value in changes:
            rows = copy.deepcopy(good)
            target = rows[1] if field == 'run_id' else rows[0]['requests'][1]
            target[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                actor_request_exposure(rows, maximum_clock_difference_seconds=.1)


class WindowTests(unittest.TestCase):
    def rows(self):
        return [{'at': at(s), 'healthy': True, 'active': 2, 'running': 2, 'waiting': 0,
                 'claims': 2, 'terminals': 0, 'generation_tokens': t}
                for s, t in [(0, 0), (20, 20), (60, 140), (90, 200)]]

    def project(self, rows):
        return aggregate_generation_windows(rows, concurrency=2, minimum_seconds=20, maximum_seconds=45)

    def test_pool_tokens_and_time_instead_of_mean_rates(self):
        result = self.project(self.rows())
        self.assertEqual(result['eligible_bins'], 3)
        self.assertAlmostEqual(result['tokens_per_second'], 200/90)

    def test_all_endpoint_and_transition_exclusions_preserved(self):
        for field, value in [('healthy', False), ('running', 1), ('waiting', 1), ('claims', 3), ('terminals', 1)]:
            rows = self.rows()
            rows[1][field] = value
            with self.subTest(field=field):
                result = self.project(rows)
                self.assertEqual(result['eligible_bins'], 1)
                self.assertEqual(len(result['windows']), 3)

    def test_counter_reset_is_explicit_not_negative_throughput(self):
        rows = self.rows()
        rows[2]['generation_tokens'] = 10
        result = self.project(rows)
        self.assertEqual(result['counter_decreases'], 1)
        self.assertFalse(result['windows'][1]['eligible'])

    def test_bad_order_nonfinite_and_boolean_counts_refuse(self):
        for field, value in [('at', at(0)), ('generation_tokens', float('nan')), ('active', True)]:
            rows = self.rows()
            rows[1][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.project(rows)


class SelfTrafficTests(unittest.TestCase):
    def fixture(self, root, mode, *, prefix=False):
        job = {'allocation': {'run_id': 'control', 'seed': 19, 'instruments': ['question']}}
        if prefix:
            job['allocation']['continue_to_end'] = False
        config = {'endpoint': 'http://unused.invalid/v1/chat/completions', 'model': 'fixture', 'tools': [],
                  'sampling': {}, 'answer_categories': ['yes', 'no', 'unknown'], 'request_timeout_seconds': 600,
                  'questions': [{'id': 'question', 'text': 'A question.', 'awareness_type': 'example', 'threshold': 'presence'}]}

        def save(path, value):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(runner.encoded(value))

        save(root / 'allocation.json', job['allocation'])
        save(root / 'model.json', config)
        rows = []
        for stage in (('early',) if prefix else ('early', 'end')):
            context = [{'role': 'user', 'content': 'Original ' + stage}]
            save(root / (stage + '_context.json'), context)
            if mode == 'failure':
                rows.append({'stage': stage, 'instrument': 'question', 'technical_status': 'failure', 'error': 'TimeoutError'})
                continue

            def exchange(endpoint, payload, destination, timeout):
                save(destination / 'request.json', payload)
                attempt = {'started_at': at(0), 'request_sha256': runner.sha(payload), 'retry': False}
                save(destination / 'attempt.json', attempt)
                response = {'model': 'fixture', 'system_fingerprint': 'fixture', 'usage': {'completion_tokens': 10},
                            'prompt_text': '\n'.join(m['content'] for m in payload['messages']),
                            'choices': [{'finish_reason': 'length' if mode == 'truncated' else 'stop',
                                         'message': {'role': 'assistant', 'content': 'bad' if mode == 'invalid' else '{"answer":"no","reason":"example"}'}}]}
                raw = runner.encoded(response)
                (destination / 'response.raw').write_bytes(raw)
                save(destination / 'receipt.json', {**attempt, 'finished_at': at(1), 'elapsed_seconds': 1,
                     'status': 'complete', 'http_status': 200, 'response_sha256': hashlib.sha256(raw).hexdigest(),
                     **{k: response[k] for k in ('model', 'system_fingerprint', 'usage')}})
                return response

            native = types.FunctionType(runner.probe.__code__, {**runner.probe.__globals__, 'exchange': exchange, 'save': save})
            value = native(config, context, config['questions'][0], runner.seed_for(19, ['probe', stage, 'question']), root / 'probes' / stage / 'question')
            rows.append({'stage': stage, **value})
        summary = {'probes': rows}
        if prefix:
            summary.update(run_id='control', finished_at=at(2), early_status='completed_batch_at_least_three_results',
                           early_context_sha256=runner.sha(context), end_status='not_selected',
                           sandbox_closure={'state': 'closed', 'overlay_mounted': False, 'closure_errors': []})
        save(root / 'summary.json', summary)
        return job, config

    def test_complete_and_unusable_measurements_are_not_transport_faults(self):
        for mode in ('complete', 'invalid', 'truncated'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                job, config = self.fixture(root, mode)
                with patch.object(runner, 'exchange', side_effect=AssertionError('network forbidden')):
                    result = audit_self_probe_traffic(root, job, config)
                self.assertEqual(result['capacity_faults'], 0)
                self.assertEqual(result['completed_http_requests'], 2)
                self.assertEqual(result['measurement_only_technical'], 0 if mode == 'complete' else 2)

    def test_failed_native_call_remains_capacity_concern(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, config = self.fixture(root, 'failure')
            self.assertEqual(audit_self_probe_traffic(root, job, config)['capacity_faults'], 2)

    def test_changed_seed_and_rendering_are_refused(self):
        for mutation in ('seed', 'render'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                job, config = self.fixture(root, 'complete')
                target = root / 'probes' / 'early' / 'question'
                if mutation == 'seed':
                    request = json.loads((target / 'request.json').read_bytes())
                    request['seed'] += 1
                    (target / 'request.json').write_bytes(runner.encoded(request))
                    for name in ('attempt', 'receipt'):
                        value = json.loads((target / (name + '.json')).read_bytes())
                        value['request_sha256'] = runner.sha(request)
                        (target / (name + '.json')).write_bytes(runner.encoded(value))
                else:
                    response = json.loads((target / 'response.raw').read_bytes())
                    response['prompt_text'] = 'Changed context'
                    raw = runner.encoded(response)
                    (target / 'response.raw').write_bytes(raw)
                    receipt = json.loads((target / 'receipt.json').read_bytes())
                    receipt['response_sha256'] = hashlib.sha256(raw).hexdigest()
                    (target / 'receipt.json').write_bytes(runner.encoded(receipt))
                with self.assertRaises((ValueError, RuntimeError)):
                    audit_self_probe_traffic(root, job, config)


class PrefixSelfTrafficTests(unittest.TestCase):
    def fixture(self, root, mode='complete'):
        return SelfTrafficTests().fixture(root, mode, prefix=True)

    def test_original_prefix_without_end_capture_is_verified_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, config = self.fixture(root)
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with patch.object(runner, 'exchange', side_effect=AssertionError('network forbidden')), \
                    patch.object(runner, 'save', side_effect=AssertionError('write forbidden')):
                result = audit_prefix_self_probe_traffic(root, job, config)
            self.assertEqual(result['completed_http_requests'], 1)
            self.assertEqual(result['captured_stages'], ['early'])
            self.assertEqual(result['unassigned_stages'], ['end'])
            self.assertEqual(result['unattempted_probes'], [])
            self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_invalid_truncated_and_failed_calls_keep_distinct_statuses(self):
        for mode, expected in [('invalid', 'invalid_answer_schema'), ('truncated', 'truncated'), ('failure', 'failure')]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                job, config = self.fixture(root, mode)
                result = audit_prefix_self_probe_traffic(root, job, config)
                self.assertEqual(result['probes'][0]['technical_status'], expected)
                self.assertEqual(result['capacity_faults'], int(mode == 'failure'))

    def test_absent_context_has_explicit_unattempted_slots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job = {'allocation': {'run_id': 'missing', 'continue_to_end': False, 'instruments': ['question']}}
            config = {'questions': [{'id': 'question'}]}
            summary = {'run_id': 'missing', 'finished_at': at(2), 'early_status': 'missing',
                       'end_status': 'technical_failure', 'actor_error': 'retained failure', 'probes': []}
            for name, value in [('allocation', job['allocation']), ('model', config), ('summary', summary)]:
                (root / (name + '.json')).write_bytes(runner.encoded(value))
            result = audit_prefix_self_probe_traffic(root, job, config)
            self.assertEqual(result['completed_http_requests'], 0)
            self.assertEqual(result['unattempted_probes'], [
                {'stage': 'early', 'instrument': 'question', 'reason': 'context_not_captured'}])
            self.assertEqual(result['actor_disposition']['actor_error'], 'retained failure')
            (root / 'early_context.json').write_text('[]')
            with self.assertRaisesRegex(ValueError, 'missing_early_has_evidence'):
                audit_prefix_self_probe_traffic(root, job, config)

    def test_full_assignment_unfinished_summary_and_extra_evidence_refuse(self):
        for mutation in ('full', 'unfinished', 'end_probes', 'foreign_stage', 'foreign_instrument', 'wrong_run'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                job, config = self.fixture(root)
                summary = json.loads((root / 'summary.json').read_bytes())
                if mutation == 'full':
                    job['allocation']['continue_to_end'] = True
                    (root / 'allocation.json').write_bytes(runner.encoded(job['allocation']))
                elif mutation == 'unfinished':
                    del summary['finished_at']
                elif mutation == 'end_probes':
                    (root / 'probes/end').mkdir()
                elif mutation == 'foreign_stage':
                    (root / 'probes/unassigned_stage').mkdir()
                elif mutation == 'foreign_instrument':
                    (root / 'probes/early/unassigned').mkdir()
                else:
                    summary['run_id'] = 'other'
                (root / 'summary.json').write_bytes(runner.encoded(summary))
                with self.assertRaises((ValueError, KeyError)):
                    audit_prefix_self_probe_traffic(root, job, config)

    def test_context_and_rehashed_seed_tampering_refuse(self):
        for mutation in ('context', 'seed'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                job, config = self.fixture(root)
                if mutation == 'context':
                    (root / 'early_context.json').write_text('[]')
                else:
                    folder = root / 'probes/early/question'
                    request = json.loads((folder / 'request.json').read_bytes())
                    request['seed'] += 1
                    (folder / 'request.json').write_bytes(runner.encoded(request))
                    for name in ('attempt', 'receipt'):
                        value = json.loads((folder / (name + '.json')).read_bytes())
                        value['request_sha256'] = runner.sha(request)
                        (folder / (name + '.json')).write_bytes(runner.encoded(value))
                with self.assertRaises(ValueError):
                    audit_prefix_self_probe_traffic(root, job, config)

    def test_actor_failure_after_capture_is_retained_without_discarding_probe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            job, config = self.fixture(root)
            summary = json.loads((root / 'summary.json').read_bytes())
            summary.update(actor_error='checkpoint failed', end_status='technical_failure')
            (root / 'summary.json').write_bytes(runner.encoded(summary))
            result = audit_prefix_self_probe_traffic(root, job, config)
            self.assertEqual(result['completed_http_requests'], 1)
            self.assertEqual(result['actor_disposition']['actor_error'], 'checkpoint failed')


if __name__ == '__main__':
    unittest.main()
