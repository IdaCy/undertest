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


if __name__ == '__main__':
    unittest.main()
