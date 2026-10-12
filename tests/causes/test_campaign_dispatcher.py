import concurrent.futures
import copy
import fcntl
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from causes.campaign_dispatcher import Dispatcher, extension_block, sha


def job(run_id, full=False, task=None):
    return {'spec': {'task_id': task or run_id, 'image': 'sha256:fixed'},
            'variant': {'id': 'canonical', 'request': 'Implement the request.'},
            'allocation': {'run_id': run_id, 'stage': 'main', 'seed': 7,
                           'continue_to_end': full, 'instruments': ['evaluation_presence']}}


def finish(spec, variant, config, allocation, output, **fields):
    output.mkdir(parents=True, exist_ok=False)
    summary = {'run_id': allocation['run_id'], 'end_status': 'natural_completion', **fields}
    for name, value in [('spec', spec), ('variant', variant), ('model', config),
                        ('allocation', allocation), ('summary', summary)]:
        (output / (name + '.json')).write_text(json.dumps(value))
    return summary


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / 'runs'
        self.output.mkdir()
        self.pause = self.output / 'PAUSE'
        self.config = {'model': 'model', 'endpoint': 'http://localhost:1',
                       'dispatcher': {'disk_floor_bytes': 0}}

    def dispatcher(self, jobs, runner, **kwargs):
        return Dispatcher(jobs, self.config, self.output, runner=runner,
                          pause_file=self.pause, poll_seconds=0.002, **kwargs)

    def events(self):
        return [json.loads(line) for line in (self.output / '.dispatcher/journal.jsonl').read_text().splitlines()]

    def run_bounded(self, dispatcher, seconds=4):
        # A test timeout sets operator pause and releases its artificial blocker;
        # it never leaves a runaway background queue after assertion failure.
        errors, results = [], []
        def run():
            try:
                results.append(dispatcher.run())
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(seconds)
        if thread.is_alive():
            self.pause.touch()
            thread.join(seconds)
            self.fail('dispatcher did not reach the fixture operator pause')
        if errors:
            raise errors[0]
        return results[0]

    def test_full_cap_bypasses_waiting_full_jobs_and_attempts_fixed_ids_once(self):
        jobs = [job('full_a', True), job('full_b', True), job('prefix')]
        prefix_started = threading.Event()
        mutex = threading.Lock()
        seen, active, max_active = [], [0, 0], [0, 0]
        def runner(spec, variant, config, allocation, output):
            full = allocation['continue_to_end']
            with mutex:
                seen.append(copy.deepcopy(allocation))
                active[0] += 1
                active[1] += int(full)
                max_active[:] = [max(a, b) for a, b in zip(max_active, active)]
            if allocation['run_id'] == 'full_a':
                self.assertTrue(prefix_started.wait(2), 'prefix was starved behind the second full job')
            elif allocation['run_id'] == 'prefix':
                prefix_started.set()
            result = finish(spec, variant, config, allocation, output)
            with mutex:
                active[0] -= 1
                active[1] -= int(full)
                if len(seen) == 3 and not active[0]:
                    self.pause.touch()
            return result
        self.run_bounded(self.dispatcher(jobs, runner, workers=2, max_full=1))
        self.assertEqual(max_active, [2, 1])
        self.assertEqual([row['run_id'] for row in seen], ['full_a', 'prefix', 'full_b'])
        self.assertEqual(sorted(seen, key=lambda x: x['run_id']), sorted([j['allocation'] for j in jobs], key=lambda x: x['run_id']))
        # A restart recognizes exact completed evidence; pause prevents admission.
        self.run_bounded(self.dispatcher(jobs, lambda *args: self.fail('completed job replayed'), workers=2, max_full=1))
        self.assertEqual(len([e for e in self.events() if e['event'] == 'claimed']), 3)

    def test_partial_folder_and_claim_without_folder_are_never_replayed(self):
        partial, lost, healthy = job('partial'), job('lost'), job('healthy')
        (self.output / 'partial').mkdir()
        marker = self.output / 'partial/opaque-evidence'
        marker.write_bytes(b'unchanged')
        dispatcher = self.dispatcher([partial, lost, healthy], lambda *args: None, workers=1, max_full=1)
        dispatcher.meta.mkdir()
        dispatcher.event('claimed', run_id='lost', job_sha256=sha(lost))
        seen = []
        def runner(spec, variant, config, allocation, output):
            seen.append(allocation['run_id'])
            result = finish(spec, variant, config, allocation, output)
            self.pause.touch()
            return result
        dispatcher.runner = runner
        self.run_bounded(dispatcher)
        self.assertEqual(seen, ['healthy'])
        self.assertEqual(marker.read_bytes(), b'unchanged')
        unresolved = [e for e in self.events() if e['event'] == 'unresolved_prior_attempt']
        self.assertEqual({e['run_id'] for e in unresolved}, {'partial', 'lost'})
        self.assertTrue(all(e['replayed'] is False and e['counted_completed'] is False for e in unresolved))

    def test_runner_exception_preserves_cause_without_replaying_attempt(self):
        assignment = job('initialization_failure')
        jobs = [assignment, job('after_failure')]

        def runner(*args):
            self.pause.touch()
            try:
                raise FileNotFoundError('source seed is absent')
            except FileNotFoundError as error:
                raise ValueError('sandbox initialization failed') from error

        self.run_bounded(self.dispatcher(jobs, runner, workers=1, max_full=1))
        failed = [e for e in self.events() if e['event'] == 'unresolved_current_attempt']
        self.assertEqual(len(failed), 1)
        receipt = failed[0]
        self.assertEqual(receipt['controller_error_type'], 'ValueError')
        self.assertIn('FileNotFoundError: source seed is absent', receipt['controller_traceback'])
        self.assertIn('ValueError: sandbox initialization failed', receipt['controller_traceback'])
        self.assertIn('in runner', receipt['controller_traceback'])
        self.assertTrue(receipt['technical_failure'])
        self.assertFalse(receipt['replayed'])
        self.assertIsNone(receipt['summary_sha256'])
        self.assertFalse((self.output / 'initialization_failure/summary.json').exists())

        self.pause.unlink()

        def healthy_runner(spec, variant, config, allocation, output):
            self.assertEqual(allocation['run_id'], 'after_failure', 'failed attempt replayed')
            result = finish(spec, variant, config, allocation, output)
            self.pause.touch()
            return result

        self.run_bounded(self.dispatcher(jobs, healthy_runner, workers=1, max_full=1))
        claimed = [e['run_id'] for e in self.events() if e['event'] == 'claimed']
        self.assertEqual(claimed, ['initialization_failure', 'after_failure'])
        self.assertEqual([e for e in self.events() if e['event'] == 'unresolved_current_attempt'], failed)

    def test_extensions_keep_forming_balanced_blocks_until_operator_pause(self):
        prototypes = [job('proto_a'), job('proto_b')]
        allocations = []
        def runner(spec, variant, config, allocation, output):
            allocations.append(copy.deepcopy(allocation))
            result = finish(spec, variant, config, allocation, output)
            if len(allocations) == 6:
                self.pause.touch()
            return result
        result = self.run_bounded(self.dispatcher([], runner, workers=1, max_full=1, extension_prototypes=prototypes))
        self.assertEqual(result['next_extension_block'], 3)
        self.assertEqual(len({a['run_id'] for a in allocations}), 6)
        self.assertEqual(len({a['seed'] for a in allocations}), 6)
        for index in range(3):
            rows = [a for a in allocations if a['block_index'] == index]
            self.assertEqual({a['prototype_run_id'] for a in rows}, {'proto_a', 'proto_b'})
            self.assertTrue(all(a['stage'] == 'corpus_extension' and a['instruments'] == ['evaluation_presence'] for a in rows))
        self.run_bounded(self.dispatcher([], lambda *args: self.fail('persisted block replayed'), workers=1, max_full=1, extension_prototypes=prototypes))
        reconciled = {e['run_id'] for e in self.events() if e['event'] == 'existing_completed_summary'}
        self.assertEqual(reconciled, {a['run_id'] for a in allocations})

    def test_extension_uses_free_slot_while_fixed_full_tail_runs(self):
        extension_started = threading.Event()
        seen = []
        def runner(spec, variant, config, allocation, output):
            seen.append(allocation['run_id'])
            if allocation['stage'] == 'main':
                self.assertTrue(extension_started.wait(2), 'fixed full tail left the second worker idle')
            else:
                extension_started.set()
                self.pause.touch()
            return finish(spec, variant, config, allocation, output)
        self.run_bounded(self.dispatcher([job('long_full', True)], runner, workers=2, max_full=1,
                                        extension_prototypes=[job('prefix_prototype')]))
        self.assertEqual(len(seen), 2)
        self.assertTrue(extension_started.is_set())

    def test_disk_floor_waits_without_spending_the_assignment(self):
        self.config['dispatcher']['disk_floor_bytes'] = 100
        free_checks = []
        def free_bytes():
            free_checks.append(1)
            return 0 if len(free_checks) < 3 else 100
        def runner(spec, variant, config, allocation, output):
            self.assertGreaterEqual(len(free_checks), 3)
            result = finish(spec, variant, config, allocation, output)
            self.pause.touch()
            return result
        self.run_bounded(self.dispatcher([job('a')], runner, free_bytes=free_bytes, workers=1, max_full=1))
        kinds = [e['event'] for e in self.events()]
        self.assertEqual(kinds.count('disk_floor_wait'), 1)
        self.assertEqual(kinds.count('disk_floor_recovered'), 1)
        self.assertEqual(kinds.count('claimed'), 1)

    def test_extension_fills_spare_capacity_while_fixed_full_backlog_waits(self):
        extension_started = threading.Event()
        seen = []
        def runner(spec, variant, config, allocation, output):
            seen.append(allocation['run_id'])
            if allocation['run_id'] == 'full_a':
                self.assertTrue(extension_started.wait(2), 'fixed full backlog starved extension prefixes')
            elif allocation['stage'] == 'corpus_extension':
                extension_started.set()
                self.pause.touch()
            return finish(spec, variant, config, allocation, output)
        result = self.run_bounded(self.dispatcher([job('full_a', True), job('full_b', True)], runner,
            workers=2, max_full=1, extension_prototypes=[job('prefix_prototype')]))
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], 'full_a')
        self.assertEqual(result['pending'], 1)

    def test_duplicate_controller_is_rejected(self):
        with (self.output / '.dispatcher.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'another_dispatcher'):
                self.dispatcher([], lambda *args: None).run()

    def test_completed_summary_without_all_identity_files_is_unresolved(self):
        existing = self.output / 'a'
        existing.mkdir()
        (existing / 'summary.json').write_text(json.dumps({'run_id': 'a'}))
        self.pause.touch()
        self.run_bounded(self.dispatcher([job('a')], lambda *args: self.fail('incomplete evidence replayed')))
        self.assertIn('unresolved_completed_evidence', [e['event'] for e in self.events()])
        self.assertNotIn('existing_completed_summary', [e['event'] for e in self.events()])

    def test_changed_execution_identity_cannot_resume_the_same_queue(self):
        self.pause.touch()
        self.run_bounded(self.dispatcher([], lambda *args: None))
        path = self.output / '.dispatcher/queue_identity.json'
        value = json.loads(path.read_text())
        value['execution_code']['dispatcher_sha256'] = 'different-frozen-code'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeError, 'queue_identity_changed'):
            self.dispatcher([], lambda *args: None).run()

    def test_quarantined_route_does_not_consume_fresh_assignments(self):
        self.config['dispatcher']['route_failure_threshold'] = 1
        seen = []
        def runner(spec, variant, config, allocation, output):
            seen.append(allocation['run_id'])
            if spec['task_id'] == 'broken_task':
                return finish(spec, variant, config, allocation, output, end_status='technical_failure', actor_error='environment unavailable')
            self.pause.touch()
            return finish(spec, variant, config, allocation, output)
        result = self.run_bounded(self.dispatcher([job('broken_1', task='broken_task'), job('broken_2', task='broken_task'), job('healthy')],
                                                runner, workers=1, max_full=1))
        self.assertEqual(seen, ['broken_1', 'healthy'])
        self.assertEqual(result['pending'], 1)
        self.assertEqual(len([e for e in self.events() if e['event'] == 'route_quarantined']), 1)
        # Quarantine is durable across a paused restart.
        restarted = self.dispatcher([job('broken_1', task='broken_task'), job('broken_2', task='broken_task'), job('healthy')], runner, workers=1, max_full=1)
        self.run_bounded(restarted)
        self.assertEqual(len(restarted.quarantines), 1)

    def test_probe_render_fault_quarantines_immediately_unparsed_only_counts_toward_threshold(self):
        dispatcher = self.dispatcher([], lambda *args: None)
        dispatcher.meta.mkdir()
        item = job('probe_case')
        summary = finish(item['spec'], item['variant'], self.config, item['allocation'], self.output / 'probe_case',
                         probes=[{'technical_status': 'failure', 'error': 'prior_reasoning_not_preserved_in_rendered_prompt'}])
        future = concurrent.futures.Future()
        future.set_result(summary)
        dispatcher.complete(future, item)
        self.assertIn(dispatcher.routes(item)[0], dispatcher.quarantines)
        other = self.dispatcher([], lambda *args: None)
        other.load_history()
        self.assertEqual(other.failures[dispatcher.routes(item)[0]], 1)
        self.assertIn(dispatcher.routes(item)[0], other.quarantines)
        self.assertFalse(other.route_ready(job('fresh')))
        # An unusable schema counts toward recurring instrument failure, but a
        # single such answer does not immediately quarantine the model.
        other.output = self.output / 'other'
        other.output.mkdir()
        other.meta = other.output / '.dispatcher'
        other.meta.mkdir()
        other.quarantines = {}
        item = job('unparsed')
        summary = finish(item['spec'], item['variant'], self.config, item['allocation'], other.output / 'unparsed',
                         probes=[{'technical_status': 'invalid_answer_schema'}])
        future = concurrent.futures.Future()
        future.set_result(summary)
        other.complete(future, item)
        self.assertFalse(other.quarantines)
        self.assertEqual(other.failures[other.routes(item)[0]], 2)

    def test_task_quarantine_allows_healthy_extension_route(self):
        self.config['dispatcher']['route_failure_threshold'] = 1
        seen = []
        def runner(spec, variant, config, allocation, output):
            seen.append(allocation['run_id'])
            if spec['task_id'] == 'broken':
                return finish(spec, variant, config, allocation, output,
                              end_status='technical_failure', actor_error='environment unavailable')
            self.pause.touch()
            return finish(spec, variant, config, allocation, output)
        result = self.run_bounded(self.dispatcher([job('failed', task='broken'), job('untouched', task='broken')],
            runner, workers=1, max_full=1, extension_prototypes=[job('healthy_prototype')]))
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], 'failed')
        self.assertTrue(seen[1].startswith('ext_b00000000_'))
        self.assertEqual(result['pending'], 1)

    def test_shared_model_quarantine_does_not_create_unlaunchable_blocks(self):
        self.config['dispatcher']['route_failure_threshold'] = 1
        def runner(spec, variant, config, allocation, output):
            return finish(spec, variant, config, allocation, output,
                          end_status='technical_failure', actor_error='model_http_status_503')
        dispatcher = self.dispatcher([job('failed'), job('untouched')], runner, workers=1, max_full=1,
                                     extension_prototypes=[job('prototype')])
        def pause_after_quarantine():
            deadline = time.monotonic() + 2
            while not dispatcher.quarantines and time.monotonic() < deadline:
                time.sleep(0.002)
            time.sleep(0.025)
            self.pause.touch()
        supervisor = threading.Thread(target=pause_after_quarantine)
        supervisor.start()
        self.run_bounded(dispatcher)
        supervisor.join()
        self.assertFalse((dispatcher.meta / 'blocks').exists())
        self.assertEqual(len([e for e in self.events() if e['event'] == 'claimed']), 1)

    def test_route_requires_matching_retained_qualification_receipt(self):
        dispatcher = self.dispatcher([], lambda *args: None)
        dispatcher.meta.mkdir()
        item = job('a')
        route = dispatcher.routes(item)[1]
        dispatcher.quarantines[route] = {'quarantine_id': 'qualification'}
        resolutions = dispatcher.meta / 'route_resolutions'
        resolutions.mkdir()
        path = resolutions / 'qualification.json'
        path.write_text(json.dumps({'status': 'qualified', 'route': route, 'quarantine_id': 'qualification'}))
        self.assertFalse(dispatcher.route_ready(item))
        path.write_text(json.dumps({'status': 'qualified', 'route': route, 'quarantine_id': 'qualification',
                                    'qualification_evidence': 'retained-repair-and-smoke-receipt.json'}))
        self.assertTrue(dispatcher.route_ready(item))
        self.assertFalse(dispatcher.quarantines)

    def test_closed_unmounted_container_retained_for_evidence_is_not_recorder_failure(self):
        dispatcher = self.dispatcher([], lambda *args: None)
        dispatcher.meta.mkdir()
        item = job('retained_evidence')
        summary = finish(item['spec'], item['variant'], self.config, item['allocation'], self.output / 'retained_evidence',
                         sandbox_closure={'state': 'closed', 'closure_errors': [],
                                          'overlay_mounted': False, 'retained_container': True})
        future = concurrent.futures.Future()
        future.set_result(summary)
        dispatcher.complete(future, item)
        self.assertFalse(dispatcher.quarantines)
        finished = next(e for e in self.events() if e['event'] == 'episode_finished')
        self.assertFalse(finished['technical_failure'])

    def test_actual_closure_errors_still_quarantine_recorder(self):
        dispatcher = self.dispatcher([], lambda *args: None)
        dispatcher.meta.mkdir()
        item = job('closure_failure')
        summary = finish(item['spec'], item['variant'], self.config, item['allocation'], self.output / 'closure_failure',
                         sandbox_closure={'state': 'closure_requires_review', 'closure_errors': ['unmount_failed'],
                                          'overlay_mounted': True, 'retained_container': True})
        future = concurrent.futures.Future()
        future.set_result(summary)
        dispatcher.complete(future, item)
        self.assertIn(dispatcher.routes(item)[2], dispatcher.quarantines)

    def test_extension_determinism_preserves_full_selection_and_input(self):
        prototypes = [job('prototype', True)]
        frozen = copy.deepcopy(prototypes)
        first = extension_block(prototypes, self.config, 0)
        self.assertEqual(first, extension_block(prototypes, self.config, 0))
        second = extension_block(prototypes, self.config, 1)
        self.assertNotEqual(first['jobs'][0]['allocation']['seed'], second['jobs'][0]['allocation']['seed'])
        self.assertTrue(first['jobs'][0]['allocation']['continue_to_end'])
        self.assertEqual(prototypes, frozen)

    def test_interleaved_system_prompts_share_full_capacity_and_preserve_resume(self):
        jobs = [job('full_a', True), job('full_b', True), job('full_c', True), job('prefix')]
        prompts = {item['allocation']['run_id']: 'system for ' + item['allocation']['run_id'] for item in jobs}
        original = copy.deepcopy(prompts)
        started_a, started_b, started_prefix = (threading.Event() for _ in range(3))
        mutex = threading.Lock()
        seen, active_full, maximum_full, finished = {}, [0], [0], [0]

        def runner(spec, variant, config, allocation, output):
            run_id, full = allocation['run_id'], allocation['continue_to_end']
            with mutex:
                active_full[0] += int(full)
                maximum_full[0] = max(maximum_full[0], active_full[0])
                seen[run_id] = copy.deepcopy(config)
            if run_id == 'full_a':
                started_a.set()
                self.assertTrue(started_b.wait(2))
                self.assertTrue(started_prefix.wait(2))
            elif run_id == 'full_b':
                started_b.set()
                self.assertTrue(started_a.wait(2))
                self.assertTrue(started_prefix.wait(2))
            elif run_id == 'prefix':
                started_prefix.set()
            result = finish(spec, variant, config, allocation, output)
            with mutex:
                active_full[0] -= int(full)
                finished[0] += 1
                if finished[0] == len(jobs):
                    self.pause.touch()
            return result

        dispatcher = self.dispatcher(jobs, runner, workers=3, max_full=2, system_prompts_by_run_id=prompts)
        prompts['full_a'] = 'caller mutation must not change the frozen queue'
        self.run_bounded(dispatcher)
        self.assertEqual(maximum_full[0], 2)
        self.assertEqual(set(seen), set(original))
        for run_id, config in seen.items():
            self.assertEqual(config, {**self.config, 'system': original[run_id]})
        claims = [row for row in self.events() if row['event'] == 'claimed']
        self.assertEqual(len(claims), 4)
        self.assertTrue(all(row['config_sha256'] == sha(seen[row['run_id']]) for row in claims))
        identity = json.loads((self.output / '.dispatcher/queue_identity.json').read_text())
        self.assertEqual(identity['system_prompts_sha256'], sha(original))
        self.run_bounded(self.dispatcher(jobs, lambda *args: self.fail('completed prompt replayed'),
                                        system_prompts_by_run_id=original))
        self.assertEqual({row['run_id'] for row in self.events() if row['event'] == 'existing_completed_summary'}, set(original))
        with self.assertRaisesRegex(RuntimeError, 'queue_identity_changed'):
            self.run_bounded(self.dispatcher(jobs, lambda *args: self.fail('changed prompt admitted'),
                                            system_prompts_by_run_id=prompts))

    def test_system_prompt_map_requires_exact_finite_coverage(self):
        for mapping in ({}, [], {'a': 1}, {'extra': 'text'}, {'a': 'text', 'extra': 'text'}):
            with self.subTest(mapping=mapping), self.assertRaisesRegex(ValueError, 'system_prompts_require_exact_fixed_queue'):
                self.dispatcher([job('a')], lambda *args: None, system_prompts_by_run_id=mapping)
        for kwargs in ({'extension_prototypes': [job('prototype')]}, {'corpus_continuation': 'control'}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, 'system_prompts_require_exact_fixed_queue'):
                self.dispatcher([job('a')], lambda *args: None, system_prompts_by_run_id={'a': 'text'}, **kwargs)

    def test_completed_system_prompt_mismatch_is_retained_without_replay(self):
        item = job('a')
        finish(item['spec'], item['variant'], self.config, item['allocation'], self.output / 'a')
        self.pause.touch()
        self.run_bounded(self.dispatcher([item], lambda *args: self.fail('mismatched original replayed'),
                                        system_prompts_by_run_id={'a': 'different system'}))
        self.assertEqual(json.loads((self.output / 'a/model.json').read_text()), self.config)
        events = self.events()
        self.assertEqual(len([row for row in events if row['event'] == 'unresolved_completed_evidence']), 1)
        self.assertFalse(any(row['event'] == 'claimed' for row in events))

    def test_interleaved_prefix_extensions_share_capacity_and_preserve_prompts(self):
        jobs = [job('full_a', True), job('full_b', True), job('fixed_prefix')]
        frozen_jobs = copy.deepcopy(jobs)
        prompts = {row['allocation']['run_id']: 'fixed ' + row['allocation']['run_id'] for row in jobs}
        scope = {'namespace': 'extra_prefix', 'system_prompt': 'Explicit baseline prompt'}
        frozen_scope = copy.deepcopy(scope)
        prototype = job('prototype')
        extra_started, full_b_started = threading.Event(), threading.Event()
        mutex = threading.Lock()
        seen, active, maximum = {}, [0, 0], [0, 0]

        def runner(spec, variant, config, allocation, output):
            run_id = allocation['run_id']
            with mutex:
                active[0] += 1
                active[1] += int(allocation['continue_to_end'])
                maximum[:] = [max(a, b) for a, b in zip(maximum, active)]
                seen[run_id] = (copy.deepcopy(allocation), copy.deepcopy(config))
            if run_id == 'full_a':
                self.assertTrue(extra_started.wait(2), 'free prefix slot stayed idle behind a full backlog')
            elif run_id == 'full_b':
                full_b_started.set()
            elif allocation['stage'] == 'corpus_extension':
                extra_started.set()
                self.assertTrue(full_b_started.wait(2), 'extension displaced the waiting fixed full job')
                self.pause.touch()
            result = finish(spec, variant, config, allocation, output)
            with mutex:
                active[0] -= 1
                active[1] -= int(allocation['continue_to_end'])
            return result

        dispatcher = self.dispatcher(jobs, runner, workers=2, max_full=1,
                                    extension_prototypes=[prototype], system_prompts_by_run_id=prompts,
                                    prefix_extension=scope)
        scope['system_prompt'] = 'caller mutation'
        self.run_bounded(dispatcher)
        self.assertEqual(maximum, [2, 1])
        self.assertEqual(len(seen), 4)
        self.assertEqual(jobs, frozen_jobs)
        for row in jobs:
            run_id = row['allocation']['run_id']
            self.assertEqual(seen[run_id], (row['allocation'], {**self.config, 'system': prompts[run_id]}))
        extra = next((allocation, config) for rid, (allocation, config) in seen.items() if rid not in prompts)
        self.assertTrue(extra[0]['run_id'].startswith('extra_prefix_b00000000_'))
        self.assertEqual(extra[0]['extension_namespace'], 'extra_prefix')
        self.assertIs(extra[0]['continue_to_end'], False)
        self.assertIs(extra[0]['fixed_study_inference'], False)
        self.assertEqual(extra[1], {**self.config, 'system': frozen_scope['system_prompt']})
        identity = json.loads((self.output / '.dispatcher/queue_identity.json').read_text())
        self.assertEqual(identity['prefix_extension_sha256'], sha(frozen_scope))
        self.run_bounded(self.dispatcher(jobs, lambda *args: self.fail('completed work replayed'),
                                        workers=2, max_full=1, extension_prototypes=[prototype],
                                        system_prompts_by_run_id=prompts, prefix_extension=frozen_scope))
        self.assertEqual(len([row for row in self.events() if row['event'] == 'claimed']), 4)
        with self.assertRaisesRegex(RuntimeError, 'queue_identity_changed'):
            self.run_bounded(self.dispatcher(jobs, lambda *args: self.fail('changed baseline admitted'),
                                            extension_prototypes=[prototype], system_prompts_by_run_id=prompts,
                                            prefix_extension=scope))

    def test_prefix_extension_requires_explicit_disjoint_prefix_scope(self):
        scope = {'namespace': 'extra', 'system_prompt': 'baseline'}
        common = {'extension_prototypes': [job('prototype')],
                  'system_prompts_by_run_id': {'fixed': 'arm'}, 'prefix_extension': scope}
        invalid = [
            {'prefix_extension': {'namespace': '../escape', 'system_prompt': 'baseline'}},
            {'prefix_extension': {'namespace': 'extra', 'system_prompt': None}},
            {'prefix_extension': {**scope, 'unexpected': True}},
            {'extension_prototypes': []},
            {'extension_prototypes': [job('full_prototype', True)]},
            {'system_prompts_by_run_id': None},
        ]
        intervention = job('arm_prototype')
        intervention['allocation']['system_prompt_intervention'] = {'arm': 'fixed'}
        invalid.append({'extension_prototypes': [intervention]})
        for change in invalid:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'invalid_prefix_extension_scope'):
                self.dispatcher([job('fixed')], lambda *args: None, **(common | change))
        with self.assertRaisesRegex(ValueError, 'invalid_prefix_extension_scope'):
            self.dispatcher([job('extra_b_fixed')], lambda *args: None,
                            **(common | {'system_prompts_by_run_id': {'extra_b_fixed': 'arm'}}))

    def test_prefix_extension_namespace_separates_ids_and_seed_streams(self):
        prototypes = [job('prototype')]
        prototypes[0]['allocation']['pair_id'] = 'original_pair'
        original = copy.deepcopy(prototypes)
        first = extension_block(prototypes, self.config, 0, namespace='first')
        second = extension_block(prototypes, self.config, 0, namespace='second')
        self.assertEqual(first, extension_block(prototypes, self.config, 0, namespace='first'))
        self.assertNotEqual(first['jobs'][0]['allocation']['run_id'], second['jobs'][0]['allocation']['run_id'])
        self.assertNotEqual(first['jobs'][0]['allocation']['seed'], second['jobs'][0]['allocation']['seed'])
        self.assertNotEqual(first['jobs'][0]['allocation']['pair_id'], second['jobs'][0]['allocation']['pair_id'])
        self.assertEqual(prototypes, original)

    def test_system_prompt_cli_verifies_mapping_file_before_dispatch(self):
        import hashlib
        from unittest.mock import patch
        from causes import campaign_dispatcher

        inputs = {'jobs': [job('a')], 'config': self.config, 'prototypes': [], 'prompts': {'a': 'system a'}}
        paths = {}
        for name, value in inputs.items():
            paths[name] = Path(self.temporary.name) / (name + '.json')
            paths[name].write_text(json.dumps(value))
        prompt_hash = hashlib.sha256(paths['prompts'].read_bytes()).hexdigest()
        args = ['dispatcher', '--jobs', str(paths['jobs']), '--config', str(paths['config']),
                '--output', str(self.output), '--extension-prototypes', str(paths['prototypes']),
                '--pause-file', str(self.pause), '--system-prompts', str(paths['prompts']),
                '--system-prompts-sha256', prompt_hash]
        with patch('sys.argv', args), patch.object(campaign_dispatcher, 'Dispatcher') as constructor, patch('builtins.print'):
            constructor.return_value.run.return_value = {}
            campaign_dispatcher.main()
            self.assertEqual(constructor.call_args.kwargs['system_prompts_by_run_id'], inputs['prompts'])
            paths['prompts'].write_text(json.dumps({'a': 'changed'}))
            constructor.reset_mock()
            with self.assertRaises(ValueError):
                campaign_dispatcher.main()
            constructor.assert_not_called()

    def test_prefix_extension_cli_verifies_its_separate_policy(self):
        import hashlib
        from unittest.mock import patch
        from causes import campaign_dispatcher

        values = {'jobs': [job('fixed')], 'config': self.config, 'prototypes': [job('proto')],
                  'prompts': {'fixed': 'arm'},
                  'prefix': {'namespace': 'extra', 'system_prompt': 'baseline'}}
        paths = {name: Path(self.temporary.name) / (name + '.json') for name in values}
        for name, value in values.items():
            paths[name].write_text(json.dumps(value))
        digest = lambda name: hashlib.sha256(paths[name].read_bytes()).hexdigest()
        args = ['dispatcher', '--jobs', str(paths['jobs']), '--config', str(paths['config']),
                '--output', str(self.output), '--extension-prototypes', str(paths['prototypes']),
                '--pause-file', str(self.pause), '--system-prompts', str(paths['prompts']),
                '--system-prompts-sha256', digest('prompts'), '--prefix-extension', str(paths['prefix']),
                '--prefix-extension-sha256', digest('prefix')]
        with patch('sys.argv', args), patch.object(campaign_dispatcher, 'Dispatcher') as constructor, patch('builtins.print'):
            constructor.return_value.run.return_value = {}
            campaign_dispatcher.main()
            self.assertEqual(constructor.call_args.kwargs['prefix_extension'], values['prefix'])
            paths['prefix'].write_text(json.dumps({**values['prefix'], 'system_prompt': 'changed'}))
            constructor.reset_mock()
            with self.assertRaises(ValueError):
                campaign_dispatcher.main()
            constructor.assert_not_called()


if __name__ == '__main__':
    unittest.main()
