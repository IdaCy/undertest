import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from concurrent.futures import Future
from contextlib import ExitStack

from causes import campaign_capacity_prefix as p
from causes import campaign_capacity_sample as sampler
from causes import campaign_handoff as h
from test_campaign_capacity_sample import make_plan
from test_campaign_capacity_trial import sample, SERIES
from test_campaign_dispatcher import Dispatcher, job, finish


START = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def at(seconds):
    return (START + dt.timedelta(seconds=seconds)).isoformat()


def criteria():
    return {'schema': 'causes.finite_prefix_capacity_criteria.v1', 'workers': 8,
        'max_full': 0, 'assigned_count': 64, 'maximum_trial_seconds': 7200,
        'minimum_actor_overlap_seconds': 30, 'maximum_clock_difference_seconds': 1,
        'maximum_new_technical_terminals': 0, 'minimum_completed_per_hour': 200,
        'no_replay_or_replacement': True, 'automatic_adoption': False, 'automatic_resume': False}


def event(kind, rid, seconds, **fields):
    return {'event': kind, 'run_id': rid, 'at': at(seconds), 'controller_id': 'owner',
            'full': False, **fields}


class PrefixCapacityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.serial = 0
        self.owner = {'group': 'prefix', 'controller_id': 'owner', 'workers': 8, 'max_full': 0}

    def save(self, value):
        self.serial += 1
        return h.save_new(self.root, f'{self.serial}.json', value)

    def pair(self, value, flag, field):
        ref = self.save(value)
        return {'evidence': ref, 'review': self.save({flag: True, field: ref})}

    def assignment(self):
        jobs = [{'allocation': {'run_id': f'r{i}', 'continue_to_end': False,
                                'instruments': ['presence', 'strong', 'lean'], 'seed': i},
                 'spec': {'task': i % 7}} for i in range(70)]
        rows = [event('claimed', 'r0', 0),
                event('episode_finished', 'r0', 1, technical_failure=True)]
        path = self.root / 'prior.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        closed = {'journal': h.pin(path), 'owner': self.owner, 'remote_owner_retired': True,
            'local_transport_retired': True, 'all_claims_accounted': True, 'route_history_preserved': True}
        close_ref = self.save(closed)
        predecessor = {'evidence': close_ref, 'review': self.save({
            'accepted_finite_prefix_predecessor_closure': True, 'actual': close_ref,
            'journal': closed['journal']})}
        a = {'schema': 'causes.finite_prefix_capacity_assignment.v1',
            'selection': 'first_never_claimed_in_original_order', 'no_replay_or_replacement': True,
            'original_jobs': self.save(jobs), 'jobs': self.save(jobs[1:65]),
            'ordered_run_ids': [f'r{i}' for i in range(1, 65)], 'prior_claimed_run_ids': ['r0'],
            'instruments': ['presence', 'strong', 'lean'], 'predecessors': [predecessor]}
        return a

    def accepted_assignment(self, value):
        return self.pair(value, 'accepted_finite_prefix_capacity_assignment', 'assignment')

    def fixture(self):
        ids = [f'r{i}' for i in range(64)]
        logs = [{'group': 'prefix', 'incomplete_tail_bytes': 0, 'events': []}]
        projection = {'episodes': [{'run_id': rid} for rid in ids], 'requests': [], 'metadata_errors': []}
        for i, rid in enumerate(ids):
            begin = (i // 8) * 90
            logs[0]['events'].extend([event('claimed', rid, begin),
                event('episode_finished', rid, begin + 85, technical_failure=False)])
            for relative, start, end in [('actor/0000', begin, begin + 60),
                    ('probes/early/presence', begin + 60, begin + 65),
                    ('probes/early/strong', begin + 65, begin + 70),
                    ('probes/early/lean', begin + 70, begin + 80)]:
                projection['requests'].append({'run_id': rid, 'relative_request_directory': relative,
                    'receipt': {'status': 'complete', 'retry': False, 'started_at': at(start),
                                'finished_at': at(end), 'elapsed_seconds': end - start}})
        logs[0]['events'].sort(key=lambda row: row['at'])
        samples = []
        for index in range(25):
            row = sample(index)
            row['process_identities'][0]['role'] = 'controller'
            row['current_owner'] = {'active_full': 0, 'active_prefix': 0 if index == 24 else 8}
            samples.append(row)
        declaration = {'metric_series': SERIES, 'owner': self.owner, 'processes': [{'role': 'controller'}]}
        assignment = {'ordered_run_ids': ids, 'instruments': ['presence', 'strong', 'lean']}
        return ({'requests': projection, 'journals': logs}, samples,
                p.journal_state(logs, self.owner, ids), criteria(), {'completed_per_hour': 200},
                assignment, declaration)

    def test_explicit_prefix_profile_does_not_relax_default(self):
        plan = make_plan(self.root)
        plan['owner']['max_full'] = plan['eligibility']['active_full'] = 0
        with self.assertRaisesRegex(ValueError, 'valid_capacity'):
            sampler.validate_plan(plan)
        plan['profile'] = 'finite_prefix'
        sampler.validate_plan(plan)
        for field, value in [('workers', 9), ('max_full', 1), ('workers', True)]:
            bad = copy.deepcopy(plan)
            bad['owner'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                sampler.validate_plan(bad)
        plan['profile'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'unknown_capacity_profile'):
            sampler.validate_plan(plan)

    def test_prospective_bounds_and_positive_rate(self):
        p.validate_criteria(criteria())
        for key, value in [('assigned_count', 65), ('workers', 4), ('max_full', 1),
                           ('automatic_adoption', True), ('minimum_completed_per_hour', float('nan')),
                           ('minimum_completed_per_hour', 0), ('minimum_completed_per_hour', True)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                p.validate_criteria(dict(criteria(), **{key: value}))

    def test_literal_first_never_claimed_preserves_old_failure(self):
        a = self.assignment()
        accepted, jobs = p.checked_assignment(self.accepted_assignment(a))
        self.assertEqual(accepted, a)
        self.assertEqual([job['allocation']['run_id'] for job in jobs], a['ordered_run_ids'])
        self.assertNotIn('r0', a['ordered_run_ids'])

    def test_exact_policy_reference_and_finite_extensions(self):
        assignment = self.assignment()
        config = self.save({'model': 'frozen-model'})
        provider = {'role': 'provider', 'pid': 10, 'process_starttime_ticks': '20', 'argv': ['provider']}
        timing = h.pin(Path(p.evidence.__file__).resolve())
        plan = {'assignment': self.accepted_assignment(assignment), 'provider': provider,
                'timing_source': timing, 'capacity_guard': {'required_sources': [timing]},
                'sampling': {'groups': ['prefix'], 'owner': self.owner, 'processes': [provider]}}
        policy = {'workers': 8, 'max_full': 0, 'launch': {'jobs': assignment['jobs'], 'config': config,
                  'options': {'workers': 8, 'max_full': 0}}}
        reference = {'profile': 'finite_prefix_reference', 'workers': 4, 'max_full': 2, 'observed_full_claims': 0,
                     'config': config, 'provider': provider, 'completed_per_hour': 200}
        p.validate_trial(plan, policy, criteria(), reference)
        for change in ('config', 'provider', 'workers', 'reference_full', 'reference_bound',
                       'reference_boolean', 'observed_boolean', 'extensions', 'group', 'source'):
            q, pol, ref = copy.deepcopy(plan), copy.deepcopy(policy), copy.deepcopy(reference)
            if change == 'config': ref['config'] = self.save({'model': 'other'})
            if change == 'provider': ref['provider']['pid'] += 1
            if change == 'workers': pol['workers'] = 9
            if change == 'reference_full': ref['observed_full_claims'] = 1
            if change == 'reference_bound': ref['max_full'] = 5
            if change == 'reference_boolean': ref['max_full'] = False
            if change == 'observed_boolean': ref['observed_full_claims'] = False
            if change == 'extensions': pol['launch']['options']['extension_prototypes'] = [{}]
            if change == 'group': q['sampling']['groups'].append('unrelated')
            if change == 'source': q['capacity_guard']['required_sources'] = []
            with self.subTest(change=change), self.assertRaises(ValueError):
                p.validate_trial(q, pol, criteria(), ref)

    def test_provider_identity_without_role_joins_native_sampling_plan(self):
        assignment = self.assignment()
        config = self.save({'model': 'frozen-model'})
        provider = {'pid': 10, 'process_starttime_ticks': '20', 'argv': ['provider']}
        sampling = make_plan(self.root)
        sampling['profile'] = 'finite_prefix'
        sampling['groups'] = [sampling['owner']['group']]
        sampling['owner']['max_full'] = sampling['eligibility']['active_full'] = 0
        sampling['processes'].append(dict(provider, role='provider'))
        timing = h.pin(Path(p.evidence.__file__).resolve())
        plan = {'assignment': self.accepted_assignment(assignment), 'provider': provider,
                'timing_source': timing, 'capacity_guard': {'required_sources': [timing]},
                'sampling': sampling}
        policy = {'workers': 8, 'max_full': 0, 'launch': {'jobs': assignment['jobs'], 'config': config,
                  'options': {'workers': 8, 'max_full': 0}}}
        reference = {'profile': 'finite_prefix_reference', 'workers': 4, 'max_full': 2,
                     'observed_full_claims': 0, 'config': config, 'provider': provider,
                     'completed_per_hour': 200}
        before = copy.deepcopy((plan, reference))
        sampler.validate_plan(sampling)
        p.validate_trial(plan, policy, criteria(), reference)
        self.assertEqual((plan, reference), before)
        for change in ('missing', 'duplicate', 'wrong_role', 'pid', 'process_starttime_ticks',
                       'argv', 'reference', 'conflicting_reference_role'):
            q, ref = copy.deepcopy(plan), copy.deepcopy(reference)
            rows = q['sampling']['processes']
            if change == 'missing': rows.pop()
            if change == 'duplicate': rows.append(copy.deepcopy(rows[-1]))
            if change == 'wrong_role': rows[-1]['role'] = 'unrelated'
            if change == 'pid': rows[-1]['pid'] += 1
            if change == 'process_starttime_ticks': rows[-1]['process_starttime_ticks'] = '21'
            if change == 'argv': rows[-1]['argv'].append('--changed')
            if change == 'reference': ref['provider']['pid'] += 1
            if change == 'conflicting_reference_role':
                q['provider']['role'] = ref['provider']['role'] = 'unrelated'
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'reference_changed'):
                p.validate_trial(q, policy, criteria(), ref)

    def test_replayed_or_modified_job_is_refused(self):
        a = self.assignment()
        for change in ('replay', 'seed', 'order', 'full', 'instrument'):
            value = copy.deepcopy(a)
            jobs = h.checked(a['jobs'])
            if change == 'replay': jobs[0] = h.checked(a['original_jobs'])[0]
            if change == 'seed': jobs[0]['allocation']['seed'] += 1000
            if change == 'order': jobs[:2] = list(reversed(jobs[:2]))
            if change == 'full': jobs[0]['allocation']['continue_to_end'] = True
            if change == 'instrument': jobs[0]['allocation']['instruments'].pop()
            value['jobs'] = self.save(jobs)
            with self.subTest(change=change), self.assertRaises(ValueError):
                p.checked_assignment(self.accepted_assignment(value))

    def test_unclosed_unaccounted_or_duplicate_history_refused(self):
        a = self.assignment()
        for key in ('remote_owner_retired', 'local_transport_retired', 'all_claims_accounted',
                    'route_history_preserved'):
            value = copy.deepcopy(a)
            closed = h.checked(value['predecessors'][0]['evidence'])
            closed[key] = False
            ref = self.save(closed)
            value['predecessors'] = [{'evidence': ref, 'review': self.save({
                'accepted_finite_prefix_predecessor_closure': True, 'actual': ref,
                'journal': closed['journal']})}]
            with self.subTest(key=key), self.assertRaises(ValueError):
                p.checked_assignment(self.accepted_assignment(value))
        a['predecessors'] *= 2
        with self.assertRaisesRegex(ValueError, 'prior_active_or_replayed'):
            p.checked_assignment(self.accepted_assignment(a))

    def test_torn_or_active_prior_history_refused(self):
        a = self.assignment()
        path = self.root / 'active.jsonl'
        for raw in (json.dumps(event('claimed', 'r0', 0)), json.dumps(event('claimed', 'r0', 0)) + '\n'):
            path.write_text(raw)
            closed = h.checked(a['predecessors'][0]['evidence'])
            closed['journal'] = h.pin(path)
            ref = self.save(closed)
            a['predecessors'] = [{'evidence': ref, 'review': self.save({
                'accepted_finite_prefix_predecessor_closure': True, 'actual': ref,
                'journal': closed['journal']})}]
            with self.assertRaises(ValueError):
                p.checked_assignment(self.accepted_assignment(a))

    def test_journal_refuses_unaccounted_full_or_duplicate_claims(self):
        for rows in ([event('claimed', 'r0', 0, full=True)],
                     [event('claimed', 'other', 0)],
                     [event('claimed', 'r0', 0)] * 2,
                     [event('episode_finished', 'r0', 0)],
                     [event('claimed', 'r0', 0, controller_id='other')]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                p.journal_state([{'group': 'prefix', 'incomplete_tail_bytes': 0, 'events': rows}],
                                self.owner, ['r0'])

    def test_complete_trial_uses_whole_duration_and_eight_way_receipts(self):
        result = p.evaluate(*self.fixture())
        self.assertEqual(result['decision'], 'eligible_candidate')
        self.assertEqual(result['elapsed_seconds'], 715)
        self.assertEqual(result['actor_request_exposure']['cross_job_actor_overlap_seconds'], 480)
        self.assertFalse(result['automatic_adoption'])
        self.assertFalse(result['automatic_resume'])

    def test_missing_self_receipt_and_retry_cannot_qualify(self):
        args = self.fixture()
        args[0]['requests']['requests'].pop()
        with self.assertRaisesRegex(ValueError, 'self_receipt_roster_incomplete'):
            p.evaluate(*args)
        args = self.fixture()
        args[0]['requests']['requests'][0]['receipt']['retry'] = True
        self.assertEqual(p.evaluate(*args)['decision'], 'hold')

    def test_faults_headroom_clock_and_throughput_hold(self):
        for change in ('technical', 'queue', 'ram', 'preemption', 'clock', 'rate', 'missing_identity'):
            args = self.fixture()
            if change == 'technical': args[2]['technical'] = ['r0']
            if change == 'queue':
                for row in args[1][:3]: row['metrics'][0] = 'queue 1'
            if change == 'ram':
                for row in args[1][:2]: row['memory']['MemAvailable_KiB'] = 1
            if change == 'preemption': args[1][1]['metrics'][2] = 'preemptions 1'
            if change == 'clock':
                args[0]['requests']['requests'][0]['receipt']['elapsed_seconds'] = 1
            if change == 'rate': args[3]['minimum_completed_per_hour'] = 1000
            if change == 'missing_identity': args[1][0]['process_identities'] = []
            with self.subTest(change=change):
                self.assertEqual(p.evaluate(*args)['decision'], 'hold')

    def test_insufficient_actual_concurrency_is_not_capacity(self):
        args = self.fixture()
        for row in args[0]['requests']['requests']:
            if row['relative_request_directory'].startswith('actor/'):
                begin = (int(row['run_id'][1:]) // 8) * 90
                row['receipt'].update(started_at=at(begin), finished_at=at(begin + 1), elapsed_seconds=1)
        self.assertIn('finite_prefix_eight_way_actor_exposure_insufficient', p.evaluate(*args)['reasons'])

    def test_incomplete_trial_retains_pending_without_qualification(self):
        args = self.fixture()
        del args[2]['terminals']['r63']
        args[2]['active'] = ['r63']
        with self.assertRaisesRegex(ValueError, 'finite_prefix_incomplete'):
            p.evaluate(*args)

    def test_finite_supervisor_always_holds_after_success(self):
        self.run_supervisor(False)

    def test_native64_exhaustion_waits_with_owned_lock_until_trial_pause(self):
        output = self.root / 'native-output'
        pause = self.root / 'TRIAL_PAUSE'
        jobs = [job(f'native{i}') for i in range(64)]
        dispatcher = Dispatcher(jobs, {'model': 'fixture', 'endpoint': 'http://localhost:1',
            'dispatcher': {'disk_floor_bytes': 0}}, output, workers=8, max_full=0,
            extension_prototypes=[], pause_file=pause, runner=finish, poll_seconds=.002)
        results, errors = [], []
        def run():
            try: results.append(dispatcher.run())
            except BaseException as error: errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            rows = []
            while time.monotonic() < deadline:
                path = output / '.dispatcher/journal.jsonl'
                raw = path.read_bytes() if path.exists() else b''
                rows = [json.loads(line) for line in raw[:raw.rfind(b'\n') + 1].splitlines()]
                if sum(row['event'] == 'episode_finished' for row in rows) == 64:
                    break
                time.sleep(.005)
            self.assertEqual(sum(row['event'] == 'claimed' for row in rows), 64)
            self.assertEqual(sum(row['event'] == 'episode_finished' for row in rows), 64)
            self.assertTrue(thread.is_alive())
            self.assertFalse(h.lock_free(output / '.dispatcher.lock'))
            self.assertFalse(results)
            self.assertFalse(errors)
            self.assertFalse(any(row['event'] == 'extension_block_created' for row in rows))
            records = self.root / 'guard-stop'
            records.mkdir()
            p.guard.hold_admission({'pause_file': str(pause), 'trial_id': 'exact64',
                'output_root': str(output)}, str(pause), records, ['finite_prefix_trial_complete'])
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, [{'status': 'paused', 'pending': 0, 'next_extension_block': 0}])
            self.assertTrue(h.lock_free(output / '.dispatcher.lock'))
            rows = [json.loads(line) for line in (output / '.dispatcher/journal.jsonl').read_bytes().splitlines()]
            self.assertEqual(sum(row['event'] == 'claimed' for row in rows), 64)
        finally:
            if thread.is_alive():
                pause.touch(exist_ok=True)
                thread.join(3)

    def test_finite_supervisor_holds_on_first_technical_without_replacement(self):
        self.run_supervisor(True)

    def run_supervisor(self, technical):
        fixture = self.fixture()
        boundary, samples, _, limits, reference, assignment, declaration = fixture
        if technical:
            terminal = next(row for row in boundary['journals'][0]['events']
                            if row['event'] == 'episode_finished')
            terminal['technical_failure'] = True
        binding = {'pause_file': str(self.root / 'PAUSE'), 'trial_id': 'fixed64',
                   'output_root': str(self.root / 'owner-output')}
        plan = {'sampling': declaration, 'capacity_guard': binding,
                'assignment': {'fixture': 'accepted immutable assignment'},
                'output': str(self.root / 'trial'), 'owner_process': {'fixture': 'exact owner'},
                'metric_series': SERIES, 'release_wait_seconds': 600}
        ref = self.save(plan)
        clock = [1000.0]
        published = []

        class Writer:
            def __init__(self, *args): pass
            def publish(self, state, *args): published.append(state)
            def close(self): pass

        class Pool:
            def __init__(self, **kwargs): pass
            def submit(self, method, *args):
                future = Future()
                future.set_result(boundary)
                return future
            def shutdown(self, **kwargs): pass

        def collect(_declaration, index, scheduled, **kwargs):
            row = copy.deepcopy(samples[index])
            logs = copy.deepcopy(boundary['journals'])
            logs[0]['events'] = [event for event in logs[0]['events'] if event['at'] <= row['at']]
            return row, logs

        with ExitStack() as stack:
            stack.enter_context(patch.object(p, 'checked_assignment', return_value=(assignment, [])))
            stack.enter_context(patch.object(p.guard, 'LeaseWriter', Writer))
            stack.enter_context(patch.object(p.guard, 'self_process', return_value={'pid': 1}))
            stack.enter_context(patch.object(p, 'ThreadPoolExecutor', Pool))
            stack.enter_context(patch.object(p.sample_api, 'sample', side_effect=collect))
            stack.enter_context(patch.object(p.time, 'monotonic', side_effect=lambda: clock[0]))
            stack.enter_context(patch.object(p.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
            p.run_trial(ref, plan, limits, reference)
        out = Path(plan['output'])
        self.assertTrue(Path(binding['pause_file']).is_file())
        stopped = json.loads((out / 'STOPPED.json').read_bytes())
        self.assertTrue(stopped['admission_held'])
        self.assertTrue(stopped['remote_and_local_retirement_required'])
        self.assertFalse(stopped['automatic_retry'])
        self.assertEqual(published[-1], 'held')
        if technical:
            self.assertFalse((out / 'QUALIFICATION.json').exists())
            self.assertEqual(stopped['counts']['technical'], 1)
            self.assertGreater(stopped['counts']['unclaimed'], 0)
        else:
            result = json.loads((out / 'QUALIFICATION.json').read_bytes())
            self.assertEqual(result['evaluation']['decision'], 'eligible_candidate')
            self.assertEqual(stopped['counts']['terminals'], 64)
            self.assertFalse(result['automatic_adoption'])


if __name__ == '__main__':
    unittest.main()
