import datetime as dt

import hashlib

import io

import json

from pathlib import Path

import contextlib

import tempfile

import unittest

from unittest.mock import patch

from causes import campaign_capacity_sample as m

def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}

def make_plan(tmp_path):
    fields = {'workers': 8, 'max_full': 2, 'config_sha256': 'config'}
    policy = {'workers': 8, 'max_full': 2, 'launch': {'options': {'workers': 8, 'max_full': 2}}}
    return {
        'schema': 'causes.passive_capacity_sample.v1', 'sampler_sha256': m._SOURCE_SHA,
        'duration_seconds': 1800, 'interval_seconds': 30,
        'campaign_root': str(tmp_path), 'output': str(tmp_path / 'out'),
        'model_key': 'actor', 'groups': ['old', 'current'], 'host': 'fixture',
        'metrics_endpoint': 'http://127.0.0.1:8000/metrics', 'storage_paths': [str(tmp_path)],
        'owner': {'group': 'current', 'controller_id': 'current-owner', 'workers': 8, 'max_full': 2,
                  'role': 'controller', 'identity': write(tmp_path / 'identity.json', fields),
                  'identity_fields': fields, 'policy': write(tmp_path / 'policy.json', policy)},
        'processes': [{'role': 'controller', 'pid': 123, 'process_starttime_ticks': '456',
                       'argv': ['python', '-m', 'policy', 'run']}],
        'required_pins': [], 'guard': {'source': write(tmp_path / 'guard.py', '')},
        'qualification': {'pin': write(tmp_path / 'qualified.json', {'accepted': True}),
                          'accepted_field': 'accepted'},
        'controls_absent': [str(tmp_path / 'PAUSE')],
        'eligibility': {'max_wait_seconds': 600, 'active_full': 2, 'active_total': 8},
        'timing': {'max_lateness_seconds': 5, 'max_clock_drift_seconds': 1},
        'execution_authorized': False,
    }

def event(kind, rid, owner='current-owner', full=False, at='2026-01-01T00:00:00+00:00'):
    return {'event': kind, 'run_id': rid, 'controller_id': owner, 'full': full, 'at': at}

class Clock:
    def __init__(self):
        self.seconds = 100.0

    def sleep(self, seconds):
        self.seconds += seconds

    def now(self):
        return (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
                + dt.timedelta(seconds=self.seconds)).isoformat()

def simulated(patcher, plan, eligible_after=0, late_index=None, invalid=False):
    clock = Clock()
    patcher(m.time, 'monotonic', lambda: clock.seconds)
    patcher(m.time, 'sleep', clock.sleep)
    patcher(m, 'now', clock.now)
    patcher(m, 'requests', lambda *args: {'requests': [], 'episodes': [], 'metadata_errors': []})
    calls = []

    def sample(_plan, index, scheduled):
        if len(calls) == late_index:
            clock.seconds += 7
        row = {'index': index, 'at': clock.now(), 'monotonic_seconds': clock.seconds,
               'scheduled_monotonic_seconds': scheduled, 'lateness_seconds': clock.seconds - scheduled,
               'controllers': [], 'pin_errors': ['error'] if invalid else [],
               'process_identities': [{'start_matches': True, 'argv_matches': True, 'qualified_cap_matches': True}],
               'current_owner': {'active_full': 2 if len(calls) >= eligible_after else 1, 'active_prefix': 6}}
        calls.append(row)
        return row, []

    return sample, calls


class CapacitySampleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.patcher = lambda obj, name, value: self.stack.enter_context(patch.object(obj, name, value))
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def test_boundary_projection_does_not_open_semantic_payloads(self):
        tmp_path = self.root
        patcher = self.patcher
        base = tmp_path
        run = base / 'runs/current/actor/r1'
        write(run / 'early_capture.json', {'calls': 1, 'private_text': 'do not project'})
        write(run / 'actor_summary.json', {'calls': 2, 'private_text': 'do not project'})
        request = {'started_at': '2026-01-01T00:00:00+00:00', 'finished_at': '2026-01-01T00:00:01+00:00',
                   'elapsed_seconds': 1, 'status': 'complete', 'usage': {'completion_tokens': 9, 'secret': 'omit'},
                   'response': 'do not project'}
        write(run / 'actor/0/attempt.json', request)
        write(run / 'actor/0/receipt.json', request)
        write(run / 'actor/1/attempt.json', request)
        write(run / 'probes/early/presence/attempt.json', request)
        write(run / 'probes/early/presence/receipt.json', {**request, 'status': 'error', 'error': 'TimeoutError: private'})
        forbidden = [run / 'actor/0/response.json', run / 'initial_context.json', run / 'RESULT.json']
        for path in forbidden:
            path.write_text('not JSON')
        original = Path.read_bytes

        def read(path):
            assert path not in forbidden
            return original(path)

        patcher(Path, 'read_bytes', read)
        logs = [{'group': 'current', 'events': [event('claimed', 'r1', full=True)]}]
        result = m.requests(base, 'actor', logs, m.epoch('2026-01-01T00:00:00+00:00'))
        assert [r['phase'] for r in result['requests']] == ['actor_prefix', 'actor_continuation', 'probe_early']
        assert result['requests'][1]['receipt'] is None
        assert result['requests'][2]['receipt']['error_class'] == 'TimeoutError'
        assert result['requests'][0]['receipt']['usage'] == {'completion_tokens': 9}
        assert 'do not project' not in json.dumps(result)
        assert result['large_request_response_context_or_actor_text_read'] is False

    def test_journal_complete_prefix_and_owner_terminals(self):
        tmp_path = self.root
        plan = make_plan(self.root)
        path = tmp_path / 'runs/current/actor/.dispatcher/journal.jsonl'
        path.parent.mkdir(parents=True)
        events = [event('claimed', 'old', owner='retired', full=True), event('claimed', 'a', full=True),
                  event('claimed', 'b'), event('unresolved_current_attempt', 'b')]
        raw = b''.join(json.dumps(v).encode() + b'\n' for v in events)
        path.write_bytes(raw + b'{"partial":')
        log = m.journal(tmp_path, 'actor', 'current')
        assert log['bytes'] == len(raw) and log['incomplete_tail_bytes'] == 11
        assert log['events'] == events
        current = m.current_owner_occupancy([log], plan['owner'])
        assert current['active_full'] == 1 and current['active_prefix'] == 0
        assert current['active_run_ids'] == ['a']
        log['events'].append(event('claimed', 'b'))
        with self.assertRaisesRegex(ValueError, 'post_terminal'):
            m.current_owner_occupancy([log], plan['owner'])

    def test_exact_policy_mode_process_and_pinned_capacity(self):
        tmp_path = self.root
        plan = make_plan(self.root)
        proc = tmp_path / 'proc'
        proc.mkdir()
        (proc / 'vmstat').write_text('oom_kill 0\npswpin 0\npswpout 0\npgfault 9\n')
        (proc / 'meminfo').write_text('MemTotal: 100 kB\nMemAvailable: 90 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n')
        pid = proc / '123'
        pid.mkdir()
        (pid / 'stat').write_text('123 (python worker) ' + ' '.join(['S'] + ['0'] * 18 + ['456']))
        (pid / 'cmdline').write_bytes(b'python\0-m\0policy\0run\0')
        result = m.passive_identity_and_memory(plan, proc)
        identity = result['process_identities'][0]
        assert identity['start_matches'] and identity['argv_matches'] and identity['qualified_cap_matches']
        assert identity['workers'] == 8 and identity['max_full'] == 2
        assert identity['capacity_source'] == 'pinned_policy_and_controller_identity'
        (pid / 'cmdline').write_bytes(b'python\0-m\0other\0run\0')
        assert m.passive_identity_and_memory(plan, proc)['process_identities'][0]['argv_matches'] is False
        Path(plan['owner']['identity']['path']).write_text('{"workers": 4}')
        result = m.passive_identity_and_memory(plan, proc)
        assert result['pin_errors'] and result['process_identities'][0]['identity_error'] == 'ValueError'

    def test_plan_refuses_scope_drift(self):
        for change in [
            {'duration_seconds': 900}, {'interval_seconds': True},
            {'metrics_endpoint': 'http://127.0.0.1:8000/v1/chat/completions'},
            {'metrics_endpoint': 'http://example.com/metrics'}, {'groups': ['../other']},
            {'groups': ['same', 'same']},
        ]:
            with self.subTest(change=change):
                plan = make_plan(self.root)
                m.validate_plan(plan)
                plan.update(change)
                with self.assertRaises(ValueError):
                    m.validate_plan(plan)

    def test_single_bounded_prelude_and_fixed_window(self):
        patcher = self.patcher
        tmp_path = self.root
        plan = make_plan(self.root)
        sample, calls = simulated(patcher, plan, eligible_after=2, late_index=62)
        out = tmp_path / 'out'
        out.mkdir()
        m.collect(plan, out, sample)
        assert len(list(out.glob('eligibility_*.json'))) == 3
        assert len(list(out.glob('sample_*.json'))) == 61
        first = json.loads((out / 'sample_00.json').read_bytes())
        assert first['at'] == calls[2]['at']
        complete = json.loads((out / 'COMPLETE.json').read_bytes())
        window = complete['window']
        assert m.epoch(window['end_wall']) - m.epoch(window['start_wall']) == 1800
        assert window['actual_last_sample_monotonic_seconds'] - window['end_monotonic_seconds'] == 7
        assert complete['timing_error_sample_indices'] == [60]
        assert (out / 'boundary_00.json').exists() and (out / 'boundary_60.json').exists()
        assert len(calls) == 63

    def test_no_exposure_does_not_select_or_repeat_window(self):
        patcher = self.patcher
        tmp_path = self.root
        plan = make_plan(self.root)
        sample, calls = simulated(patcher, plan, eligible_after=100)
        out = tmp_path / 'out'
        out.mkdir()
        m.collect(plan, out, sample)
        assert len(calls) == 21
        assert (out / 'INELIGIBLE.json').exists()
        assert not list(out.glob('sample_*.json')) and not (out / 'COMPLETE.json').exists()

    def test_identity_fault_never_waits_for_a_better_identity(self):
        patcher = self.patcher
        tmp_path = self.root
        plan = make_plan(self.root)
        sample, calls = simulated(patcher, plan, invalid=True)
        out = tmp_path / 'out'
        out.mkdir()
        m.collect(plan, out, sample)
        assert len(calls) == 1 and (out / 'PRELUDE_INVALID.json').exists()
        assert not list(out.glob('sample_*.json'))

    def test_inactive_plan_has_no_output_side_effects(self):
        plan = make_plan(self.root)
        tmp_path = self.root
        ref = write(tmp_path / 'PLAN.json', plan)
        with self.assertRaisesRegex(ValueError, 'inactive_sampling_plan'):
            m.run(ref['path'], ref['sha256'])
        assert not Path(plan['output']).exists()

    def test_sample_preserves_raw_counters_and_adds_distinct_current_occupancy(self):
        plan = make_plan(self.root)
        for group, events in [
            ('old', [event('claimed', 'old', owner='retired', full=True)]),
            ('current', [event('claimed', 'a', full=True), event('claimed', 'b'),
                         event('unresolved_current_attempt', 'b')]),
        ]:
            path = self.root / 'runs' / group / 'actor/.dispatcher/journal.jsonl'
            path.parent.mkdir(parents=True)
            path.write_bytes(b''.join(json.dumps(v).encode() + b'\n' for v in events))
        self.patcher(m, 'passive_identity_and_memory', lambda plan: {'pin_errors': [], 'process_identities': []})
        original_glob = Path.glob
        self.patcher(Path, 'glob', lambda path, pattern: [] if str(path) == '/sys/class/drm' else original_glob(path, pattern))
        self.patcher(m.urllib.request, 'urlopen', lambda *args, **kwargs: io.BytesIO(
            b'# comment\nqueue 0\nlatency_bucket{le="1"} 3\ntokens_total 12\n'))
        Path(plan['controls_absent'][0]).touch()
        row, _ = m.sample(plan, 0, m.time.monotonic(), lambda plan: {'healthy': False, 'alarms': {'guard_not_ready': True}})
        assert row['controllers'][0]['active_full'] == 1
        assert row['controllers'][1]['active_prefix'] == 1
        assert row['current_owner']['active_prefix'] == 0
        assert row['current_owner']['active_full'] == 1
        assert row['metrics'] == ['queue 0', 'tokens_total 12']
        assert row['metrics_capture_finished']['monotonic_seconds'] >= row['metrics_capture_started']['monotonic_seconds']
        assert m.epoch(row['metrics_capture_finished']['wall']) >= m.epoch(row['metrics_capture_started']['wall'])
        assert row['storage_guard']['healthy'] is False
        assert row['controls_present'][plan['controls_absent'][0]] is True
        assert row['journal_capture_finished']['monotonic_seconds'] >= row['journal_capture_started']['monotonic_seconds']

    def test_failed_metrics_request_preserves_actual_interval(self):
        plan = make_plan(self.root)
        for group in plan['groups']:
            path = self.root / 'runs' / group / 'actor/.dispatcher/journal.jsonl'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'')
        self.patcher(m, 'passive_identity_and_memory', lambda plan: {'pin_errors': [], 'process_identities': []})
        original_glob = Path.glob
        self.patcher(Path, 'glob', lambda path, pattern: [] if str(path) == '/sys/class/drm' else original_glob(path, pattern))

        def unavailable(*args, **kwargs):
            raise TimeoutError('fixture_metrics_timeout')

        self.patcher(m.urllib.request, 'urlopen', unavailable)
        row, _ = m.sample(plan, 0, m.time.monotonic(), lambda plan: {'healthy': True})
        assert 'metrics_error' in row and 'metrics' not in row
        assert row['metrics_capture_finished']['monotonic_seconds'] >= row['metrics_capture_started']['monotonic_seconds']

    def test_failed_invocation_retains_receipt_and_partial_output(self):
        plan = make_plan(self.root)
        plan['execution_authorized'] = True
        ref = write(self.root / 'PLAN.json', plan)

        def fail(plan, out):
            m.save(out / 'partial.json', {'retained': True})
            raise RuntimeError('fixture_failure')

        self.patcher(m, 'collect', fail)
        with self.assertRaisesRegex(RuntimeError, 'fixture_failure'):
            m.run(ref['path'], ref['sha256'])
        out = Path(plan['output'])
        assert (out / 'RECEIPT.json').exists() and (out / 'partial.json').exists()
        assert json.loads((out / 'FAILED.json').read_bytes())['error_class'] == 'RuntimeError'
        with self.assertRaises(FileExistsError):
            m.run(ref['path'], ref['sha256'])


if __name__ == '__main__':
    unittest.main()
