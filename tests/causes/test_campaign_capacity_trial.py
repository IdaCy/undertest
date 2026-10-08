import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from causes import campaign_capacity_trial as t
from causes import campaign_handoff as h
from causes import campaign_storage as storage
from causes.campaign_sandbox import Sandbox
from test_campaign_storage import storage_fixture


SERIES = {key: [key] for key in ('queue', 'kv', 'preemptions', 'prompt_tokens',
                               'completion_tokens', 'successful_requests')}


def sample(index=0, **metrics):
    wall = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=index * 30)
    mono = 1000 + index * 30
    pair = {'wall': wall.isoformat(), 'monotonic_seconds': mono}
    values = dict(queue=0, kv=.2, preemptions=0, prompt_tokens=100 + index,
                  completion_tokens=100 + index, successful_requests=100 + index)
    values.update(metrics)
    return {'index': index, 'at': wall.isoformat(), 'monotonic_seconds': mono,
            'finished_at': wall.isoformat(), 'finished_monotonic_seconds': mono,
            'lateness_seconds': 0, 'pin_errors': [], 'process_identities': [
                {'start_matches': True, 'argv_matches': True, 'qualified_cap_matches': True}],
            'storage_guard': {'healthy': True, 'owned_startup_barrier_present': False}, 'controls_present': {},
            'current_owner': {'active_full': 4, 'active_prefix': 4},
            'metrics': [key + ' ' + str(value) for key, value in values.items()],
            'memory': {'MemAvailable_KiB': 32 * 1024**2},
            'vmstat': {'oom_kill': 0, 'pswpin': 0, 'pswpout': 0},
            **{key: dict(pair) for key in ('metrics_capture_started', 'metrics_capture_finished',
                    'journal_capture_started', 'journal_capture_finished')}}


class TelemetryTests(unittest.TestCase):
    def test_three_adjacent_queued_samples_and_reset(self):
        monitor = t.TelemetryGuard(SERIES)
        self.assertFalse(monitor.evaluate(sample(0, queue=1), [])['hold'])
        self.assertFalse(monitor.evaluate(sample(1, queue=1), [])['hold'])
        self.assertTrue(monitor.evaluate(sample(2, queue=1), [])['hold'])
        monitor = t.TelemetryGuard(SERIES)
        for index, queue in enumerate((1, 1, 0, 1, 1)):
            self.assertFalse(monitor.evaluate(sample(index, queue=queue), [])['hold'])

    def test_ram_kv_and_warning_boundaries(self):
        for name in ('ram', 'kv'):
            monitor = t.TelemetryGuard(SERIES)
            for index in range(2):
                row = sample(index, kv=.95 if name == 'kv' else .2)
                if name == 'ram':
                    row['memory']['MemAvailable_KiB'] = 16 * 1024**2 - 1
                self.assertEqual(monitor.evaluate(row, [])['hold'], index == 1)
        monitor = t.TelemetryGuard(SERIES)
        for index in range(3):
            result = monitor.evaluate(sample(index, kv=.85), [])
            self.assertFalse(result['hold'])
        self.assertEqual(result['warnings'], ['candidate_kv_warning'])

    def test_resets_new_swap_and_missing_data_hold(self):
        for change in ('reset', 'swap', 'missing', 'torn', 'source'):
            monitor = t.TelemetryGuard(SERIES)
            monitor.evaluate(sample(), [])
            row, logs = sample(1), []
            if change == 'reset': row['metrics'][-1] = 'successful_requests 1'
            if change == 'swap': row['vmstat']['pswpin'] = 1
            if change == 'missing': row['metrics'].pop()
            if change == 'torn': logs = [{'incomplete_tail_bytes': 1}]
            if change == 'source': row['pin_errors'] = ['changed']
            self.assertTrue(monitor.evaluate(row, logs)['hold'], change)

    def test_timing_limits_and_no_skipped_samples(self):
        for change in ('late', 'span', 'clock', 'gap'):
            monitor = t.TelemetryGuard(SERIES)
            monitor.evaluate(sample(), [])
            row = sample(2 if change == 'gap' else 1)
            if change == 'late': row['lateness_seconds'] = 10.01
            if change == 'span': row['finished_monotonic_seconds'] += 30.01
            if change == 'clock': row['metrics_capture_finished']['monotonic_seconds'] += 1.01
            self.assertTrue(monitor.evaluate(row, [])['hold'], change)

    def test_physical_floor_and_capacity_wait_are_distinct(self):
        binding, observation = storage_fixture()
        binding['capacity_guard'] = {'fixture': True, 'output_root': binding['output']}
        owner = {'pid': 123}
        with patch.object(storage, 'validate_capacity_guard', return_value={
                'status': 'endpoint_pending', 'admission_allowed': False}):
            instance = storage.StorageGuard(binding, lambda _: observation, capacity_owner=owner)
            self.assertEqual(instance.check(), 0)
            self.assertEqual(instance.last_observation['root_free_bytes'], 140)
            self.assertEqual(instance.last_observation['capacity_guard']['status'], 'endpoint_pending')
        with patch.object(storage, 'validate_capacity_guard', return_value={
                'status': 'startup_held', 'admission_allowed': False, 'construction_allowed': True}):
            instance = storage.StorageGuard(binding, lambda _: observation)
            self.assertEqual(instance.check(), 140)
            self.assertEqual(instance(), 0)

    def test_only_own_well_bound_capacity_wait_is_ignored(self):
        process = {'pid': 123}
        value = {'healthy': False, 'alarms': {'capacity_guard_not_ready': True, 'volume_below_floor': False},
                 'capacity_guard': {'status': 'endpoint_pending', 'guard': process}}
        declaration = {'guard': {'binding': {'capacity_guard': {'owned_startup_barrier': {}}}}}
        owned = {'marker': {'path': '/nonexistent_capacity_fixture_barrier'}}
        with patch.object(t.sample_api, 'guard_observation', return_value=value), patch.object(t.guard, 'self_process', return_value=process), patch.object(t.h, 'checked', return_value=owned):
            result = t.own_storage_observation(declaration)
            self.assertTrue(result['telemetry_healthy'])
            self.assertFalse(result['healthy'])
            self.assertFalse(result['admission_healthy'])
        value['healthy'] = False
        value['alarms']['volume_below_floor'] = True
        with patch.object(t.sample_api, 'guard_observation', return_value=value), patch.object(t.guard, 'self_process', return_value=process), patch.object(t.h, 'checked', return_value=owned):
            self.assertFalse(t.own_storage_observation(declaration)['telemetry_healthy'])

    def test_exact_held_barrier_then_absence_and_reappearance(self):
        monitor = t.TelemetryGuard(SERIES)
        row = sample()
        row['storage_guard']['owned_startup_barrier_present'] = True
        self.assertFalse(monitor.evaluate(row, [])['hold'])
        self.assertFalse(monitor.evaluate(sample(1), [])['hold'])
        row = sample(2)
        row['storage_guard']['owned_startup_barrier_present'] = True
        self.assertIn('ValueError:candidate_startup_barrier_reappeared', monitor.evaluate(row, [])['reasons'])

    def test_barrier_present_after_first_claim_holds_even_before_absence_sample(self):
        monitor = t.TelemetryGuard(SERIES, owner={'group': 'fixed', 'controller_id': 'candidate'})
        row = sample()
        row['storage_guard']['owned_startup_barrier_present'] = True
        logs = [{'group': 'fixed', 'incomplete_tail_bytes': 0,
                 'events': [{'event': 'claimed', 'controller_id': 'candidate'}]}]
        self.assertTrue(monitor.evaluate(row, logs)['hold'])


class BoundaryAndSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_actor_probe_failure_does_not_reclassify_natural(self):
        value = {'run_id': 'run', 'end_status': 'natural_completion',
                 'sandbox_closure': {'state': 'closed', 'closure_errors': [],
                                     'overlay_mounted': False, 'retained_container': True},
                 'probes': [{'technical_status': 'failure'}]}
        (self.root / 'actor_summary.json').write_text(json.dumps(value))
        terminal = {'event': 'episode_finished'}
        result = t.terminal_metadata(self.root, {'run_id': 'run'}, terminal)
        self.assertEqual(result['classification'], 'natural')
        self.assertNotIn('probes', result['technical_fields'])
        value['actor_error'] = 'TimeoutError()'
        value['end_status'] = 'administratively_censored_runtime'
        (self.root / 'actor_summary.json').write_text(json.dumps(value))
        result = t.terminal_metadata(self.root, {'run_id': 'run'}, terminal)
        self.assertEqual(result['classification'], 'technical')
        self.assertTrue(result['runtime_censored'])

    def test_preserved_stopped_sandbox_can_be_natural_or_censored(self):
        seed = self.root / 'seed'
        seed.mkdir()
        sandbox = Sandbox({'workspace': '/work/repo', 'seed_path': str(seed)},
                          self.root / 'output', 'run')
        sandbox.created = sandbox.started = sandbox.mounted = True
        operations = []

        def execute(argv, timeout=90, tag='operation', **kwargs):
            operations.append(tag)
            if tag == 'closure_state':
                return {'returncode': 0, 'stdout': json.dumps({'Running': True, 'Paused': False})}
            return {'returncode': 0, 'timeout': False, 'stdout_path': 'retained-command-output'}

        sandbox._execute = execute
        closure = sandbox.close()
        self.assertTrue(closure['retained_container'])
        self.assertEqual(operations[-3:], ['stop', 'wait', 'unmount'])
        for status, expected in (('natural_completion', 'natural'),
                                 ('administratively_censored_runtime', 'censored')):
            value = {'run_id': 'run', 'end_status': status, 'sandbox_closure': closure}
            (self.root / 'actor_summary.json').write_text(json.dumps(value))
            result = t.terminal_metadata(self.root, {'run_id': 'run'}, {'event': 'episode_finished'})
            self.assertEqual(result['classification'], expected)
            self.assertTrue(result['technical_fields']['explicit_clean_closure'])
            self.assertTrue(result['technical_fields']['retained_container'])

    def test_preservation_does_not_mask_closure_or_runner_failures(self):
        clean = {'state': 'closed', 'closure_errors': [],
                 'overlay_mounted': False, 'retained_container': True}
        for change in ({'state': 'closure_requires_review'},
                       {'closure_errors': [{'stage': 'stop', 'returncode': 1}]},
                       {'overlay_mounted': True}):
            value = {'run_id': 'run', 'end_status': 'natural_completion',
                     'sandbox_closure': dict(clean, **change)}
            (self.root / 'actor_summary.json').write_text(json.dumps(value))
            result = t.terminal_metadata(self.root, {'run_id': 'run'}, {'event': 'episode_finished'})
            self.assertEqual(result['classification'], 'technical')
        value['sandbox_closure'] = clean
        (self.root / 'actor_summary.json').write_text(json.dumps(value))
        result = t.terminal_metadata(self.root, {'run_id': 'run'}, {'event': 'unresolved_current_attempt'})
        self.assertEqual(result['classification'], 'technical')

    def test_missing_or_ambiguous_closure_cannot_be_natural_or_censored(self):
        clean = {'state': 'closed', 'closure_errors': [], 'overlay_mounted': False, 'retained_container': False}
        for status in ('natural_completion', 'administratively_censored_runtime'):
            for closure in (None, {}, dict(clean, state='unknown'), dict(clean, closure_errors=None),
                            dict(clean, overlay_mounted=0), dict(clean, retained_container='false')):
                value = {'run_id': 'run', 'end_status': status, 'sandbox_closure': closure}
                (self.root / 'actor_summary.json').write_text(json.dumps(value))
                result = t.terminal_metadata(self.root, {'run_id': 'run'}, {'event': 'episode_finished'})
                self.assertEqual(result['classification'], 'unknown', (status, closure))
                self.assertFalse(result['technical_fields']['explicit_clean_closure'])

    def test_capture_failure_retains_actual_interval(self):
        out = self.root / 'boundary'
        with patch.object(t.sample_api, 'requests', side_effect=ValueError('fixture')):
            with self.assertRaises(ValueError):
                t.capture_candidate_boundary(out, [], 0, {'campaign_root': str(self.root), 'model_key': 'm'})
        capture = json.loads((out / 'capture.json').read_text())
        self.assertEqual(capture['failure']['error_class'], 'ValueError')
        self.assertGreaterEqual(capture['finished']['monotonic_seconds'], capture['started']['monotonic_seconds'])

    def test_pending_deadline_never_slides_and_final_inconclusive_holds(self):
        writer, telemetry = Mock(), Mock()
        telemetry.evaluate.return_value = {'hold': False, 'reasons': []}
        binding = {'pause_file': str(self.root / 'PAUSE'), 'trial_id': 't', 'output_root': str(self.root)}
        supervisor = t.CandidateSupervisor(writer, telemetry, binding, self.root)
        first = h.save_new(self.root, 'SAMPLE.json', {'finished_at': h.now(), 'finished_monotonic_seconds': time.monotonic()})
        supervisor.sample_completed({}, [], first)
        initial = supervisor.begin_endpoint(60)
        later = h.save_new(self.root, 'LATER.json', {'finished_at': h.now(), 'finished_monotonic_seconds': time.monotonic()})
        supervisor.sample_completed({}, [], later)
        self.assertEqual(supervisor.pending['deadline'], initial['deadline'])
        self.assertEqual(writer.publish.call_count, 2)
        supervisor.finish_endpoint(60, {'supported': True, 'decision': 'inconclusive'}, final=True)
        self.assertTrue(supervisor.held)
        self.assertTrue(Path(binding['pause_file']).exists())
        writer.close.assert_called_once()

    def test_subwindow_never_reanchors_on_late_endpoint(self):
        start = sample()
        window = t.fixed_window(start, 1800, 1800)
        self.assertEqual(window['start_monotonic_seconds'], 2800)
        self.assertEqual(window['end_monotonic_seconds'], 4600)
        self.assertEqual(window['global_start_wall'], start['at'])

    def test_exact_operational_adoption_controls_and_criteria_are_required(self):
        binding = {'criteria': {'evidence': {'fixture': 'criteria'}},
                   'criteria_adoption': {'fixture': 'criteria adoption'},
                   'output_root': str(self.root / 'runs'), 'pause_file': str(self.root / 'PAUSE')}
        owned = {'marker': {'path': str(self.root / 'BARRIER')}}
        for case in ('valid', 'policy', 'criteria', 'output', 'adoption'):
            directory = self.root / case
            directory.mkdir()
            value = {'schema': 'causes.capacity_operational_binding_addendum.v1',
                     'criteria': binding['criteria']['evidence'], 'criteria_adoption': binding['criteria_adoption'],
                     'candidate': {'output_namespace': binding['output_root'],
                         'sole_hold_path': binding['pause_file'], 'startup_barrier': owned['marker']['path'],
                         'workers': 8, 'max_full': 4}}
            if case == 'criteria': value['criteria'] = {'fixture': 'other'}
            if case == 'output': value['candidate']['output_namespace'] = str(self.root / 'other')
            evidence = h.save_new(directory, 'EVIDENCE.json', value)
            review = h.save_new(directory, 'REVIEW.json', {
                'accepted_capacity_operational_binding_addendum': True, 'addendum': evidence,
                'criteria': binding['criteria']['evidence'], 'criteria_adoption': binding['criteria_adoption']})
            adoption = h.save_new(directory, 'ADOPTION.json', {
                'root_adopted_capacity_operational_binding_addendum': True, 'addendum': evidence,
                'independent_review': review if case != 'adoption' else evidence,
                'criteria': binding['criteria']['evidence'], 'criteria_adoption': binding['criteria_adoption']})
            refs = {'evidence': evidence, 'review': review, 'adoption': adoption}
            policy = {'capacity_trial': {'mode': 'candidate', 'operational_bindings':
                      refs if case != 'policy' else {}}}
            if case == 'valid':
                t.validate_operational_bindings({'operational_bindings': refs}, policy, binding, owned)
            else:
                with self.assertRaises(ValueError, msg=case):
                    t.validate_operational_bindings({'operational_bindings': refs}, policy, binding, owned)


class SchedulerTests(unittest.TestCase):
    def test_exposure_after_fixed_prelude_cannot_select_a_new_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binding = {'trial_id': 'fixture', 'output_root': str(root / 'runs'),
                       'pause_file': str(root / 'ROLE_PAUSE')}
            declaration = {'owner': {'group': 'new', 'role': 'owner', 'controller_id': 'controller'},
                           'processes': [{'role': 'owner'}],
                           'eligibility': {'active_full': 4, 'active_total': 8}}
            plan = {'sampling': declaration, 'capacity_guard': binding, 'output': str(root / 'capture'),
                    'metric_series': SERIES, 'owner_process': {'pid': 999999}, 'release_wait_seconds': 600}
            clock = SimpleNamespace(value=1000.)
            def observe(declaration, index, scheduled, **kwargs):
                row = sample(index)
                row['process_identities'][0]['role'] = 'owner'
                row['current_owner'] = {'active_full': 3, 'active_prefix': 5}
                log = {'group': 'new', 'incomplete_tail_bytes': 0, 'events': [
                    {'event': 'claimed', 'controller_id': 'controller', 'run_id': 'full',
                     'at': sample()['at'], 'full': True}]}
                return row, [log]
            with patch.object(t, 'load_trial', return_value=(plan, Mock(), {}, {})), \
                 patch.object(t.guard, 'LeaseWriter', return_value=Mock()), \
                 patch.object(t.sample_api, 'sample', side_effect=observe), \
                 patch.object(t.time, 'monotonic', side_effect=lambda: clock.value), \
                 patch.object(t.time, 'sleep', side_effect=lambda seconds: setattr(clock, 'value', clock.value + seconds)):
                t.run_trial({'fixture': True})
            self.assertFalse((root / 'capture/WINDOW.json').exists())
            self.assertTrue((root / 'capture/sample_00020.json').exists())
            self.assertFalse((root / 'capture/sample_00021.json').exists())
            self.assertTrue((root / 'ROLE_PAUSE').exists())

    def test_fixed_five_blocks_then_guarded_continuation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binding = {'trial_id': 'fixture', 'output_root': str(root / 'runs'),
                       'pause_file': str(root / 'ROLE_PAUSE')}
            declaration = {'owner': {'group': 'new', 'role': 'owner', 'controller_id': 'controller'},
                           'processes': [{'role': 'owner'}],
                           'eligibility': {'active_full': 4, 'active_total': 8}}
            plan = {'sampling': declaration, 'capacity_guard': binding, 'output': str(root / 'capture'),
                    'metric_series': SERIES, 'owner_process': {'pid': 999999}, 'release_wait_seconds': 600}
            clock = SimpleNamespace(value=1000.)
            captured = []
            def observe(declaration, index, scheduled, **kwargs):
                if index == 302:
                    raise RuntimeError('fixture stop after provisional continuation')
                row = sample(index)
                row['scheduled_monotonic_seconds'] = scheduled
                row['process_identities'][0]['role'] = 'owner'
                row['current_owner'] = {'active_full': 4, 'active_prefix': 4}
                row['controllers'] = []
                log = {'group': 'new', 'incomplete_tail_bytes': 0, 'events': [
                    {'event': 'claimed', 'controller_id': 'controller', 'run_id': 'full',
                     'at': sample()['at'], 'full': True}]}
                return row, [log]
            def summarize(samples, start, end, window, declaration, **kwargs):
                captured.append((window, [s['index'] for s in samples], kwargs))
                return {'actors': [], 'window': window}
            def evaluate(summary, reference, criteria, **kwargs):
                return {'supported': True, 'decision': 'eligible_candidate' if kwargs.get('confirmation')
                        else 'continue_confirmation', 'reasons': []}
            prelude = Mock(return_value={'supported': True, 'decision': 'continue_confirmation', 'reasons': []})
            evaluator = SimpleNamespace(summarize_window=summarize, evaluate_candidate=evaluate,
                                        evaluate_prelude=prelude)
            class Future:
                def done(self): return True
                def result(self): return {'fixture': True, 'capacity_wait_evidence': {}}
            executor = Mock()
            executor.submit.side_effect = lambda *a, **kw: Future()
            with patch.object(t, 'load_trial', return_value=(plan, evaluator, {}, {})), \
                 patch.object(t.guard, 'LeaseWriter', return_value=Mock()), \
                 patch.object(t, 'ThreadPoolExecutor', return_value=executor), \
                 patch.object(t.sample_api, 'sample', side_effect=observe), \
                 patch.object(t.time, 'monotonic', side_effect=lambda: clock.value), \
                 patch.object(t.time, 'sleep', side_effect=lambda seconds: setattr(clock, 'value', clock.value + seconds)):
                with self.assertRaisesRegex(RuntimeError, 'fixture stop'):
                    t.run_trial({'fixture': True})
            self.assertEqual(len(captured), 6)
            self.assertEqual([v[0]['start_monotonic_seconds'] for v in captured], [1000, 2800, 4600, 6400, 8200, 2800])
            self.assertEqual(captured[-1][0]['end_monotonic_seconds'], 10000)
            self.assertEqual(captured[-1][1], list(range(60, 301)))
            self.assertEqual(captured[-1][2]['preceding_sample']['index'], 59)
            prelude.assert_called_once()
            self.assertEqual(prelude.call_args.kwargs['end_wall'], sample()['at'])
            self.assertEqual(prelude.call_args.kwargs['cohort_start'], dt.datetime.fromisoformat(sample()['at']).timestamp())
            self.assertTrue((root / 'capture/PRELUDE_EVALUATION.json').exists())
            qualification = json.loads((root / 'capture/QUALIFICATION.json').read_text())
            self.assertFalse(qualification['automatic_adoption'])
            self.assertTrue((root / 'capture/measurement_sample_00301.json').exists())
            self.assertTrue((root / 'capture/FAILED.json').exists())
            self.assertTrue((root / 'ROLE_PAUSE').exists())


if __name__ == '__main__':
    unittest.main()
