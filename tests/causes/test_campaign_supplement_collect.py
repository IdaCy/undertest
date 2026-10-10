import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from causes import campaign_runner as runner
from causes import campaign_supplement as core
from causes import campaign_supplement_collect as collector


class SupplementCollectFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.family, self.model = 'supplement_v1', 'model_a'
        self.run_root = self.root / 'attempts'
        self.run_root.mkdir()
        self.output = self.root / 'export'
        self.question = {'id': 'oversight_strong', 'text': 'Synthetic question.',
                         'awareness_type': 'oversight', 'threshold': 'strongly_favors'}
        self.config = {'model': 'synthetic', 'endpoint': 'http://invalid.example',
                       'sampling': {'temperature': 0.6}, 'tools': [], 'questions': [self.question],
                       'answer_categories': ['yes', 'no'], 'probe_max_tokens': 8192,
                       'request_timeout_seconds': 600}
        self.context = [{'role': 'user', 'content': 'Saved synthetic task.'}]
        self.config_pin = self.save('config.json', self.config)
        self.context_pin = self.save('context.json', self.context)
        self.roster = [{'run_id': 'run' + str(i), 'seed': 7 + i} for i in range(3)]
        self.obligations = [self.obligation(r, stage, self.question) for r in self.roster for stage in ('early', 'end')]
        self.ready = [self.prepare(j) for j in self.obligations[:4]]
        self.history = []
        self.rows = [self.original(self.roster[0], both=True), self.original(self.roster[1], both=False)]

    def save(self, name, value):
        p = self.root / name
        p.write_bytes(core.encoded(value) + b'\n')
        return core.pin(p)

    def obligation(self, allocation, stage, question):
        return {'family': self.family, 'model': self.model, 'run_id': allocation['run_id'], 'stage': stage,
                'question_id': question['id'], 'measurement_id': '/'.join((self.family, self.model, allocation['run_id'], stage, question['id'])),
                'awareness_type': question['awareness_type'], 'threshold': question['threshold'],
                'original_seed': allocation['seed'], 'seed': runner.seed_for(allocation['seed'], ['probe', stage, question['id']]),
                'question_canonical_sha256': runner.sha(question), 'disposition': 'historical_planned_only'}

    def prepare(self, job):
        q = next(q for q in self.config['questions'] if q['id'] == job['question_id'])
        payload = runner.request(self.config, self.context + [{'role': 'user', 'content': q['text']}], job['seed'], [])
        payload.update(tool_choice='none', max_tokens=8192)
        raw = runner.encoded(payload)
        return {**job, 'context': self.context_pin, 'context_canonical_sha256': runner.sha(self.context),
                'request_body_sha256': hashlib.sha256(raw).hexdigest(), 'request_body_bytes': len(raw),
                'request_file_sha256': hashlib.sha256(raw + b'\n').hexdigest(), 'frozen_config': self.config_pin}

    def original(self, allocation, *, both):
        return {'run_id': allocation['run_id'], 'model': self.model, 'allocation': copy.deepcopy(allocation),
                'self': {s: {'evaluation_presence': {'label': 'unchanged'}} for s in (('early', 'end') if both else ('early',))},
                'reader': {}, 'reader_dispositions': {'early': {'status': 'unchanged'}}, 'actor': {'unchanged': True}}

    def contexts(self):
        return {(j['run_id'], j['stage']): {'run_id': j['run_id'], 'stage': j['stage'],
                'original_bindings': {'context': self.context_pin, 'config': self.config_pin}} for j in self.ready}

    def final(self, job, mode='complete'):
        destination = core.measurement_path(self.run_root, job, family=self.family, model=self.model)
        core.mkdir(destination / 'capture')
        core.save(destination / 'INPUT.json', job)
        capture = destination / 'capture'

        def exchange(endpoint, payload, path, timeout):
            self.assertEqual((endpoint, Path(path), timeout), (self.config['endpoint'], capture, 600))
            core.save(capture / 'request.json', payload)
            core.save(capture / 'attempt.json', {'retry': False, 'request_sha256': job['request_body_sha256']})
            if mode == 'transport_failure':
                core.save(capture / 'receipt.json', {'status': 'exception', 'retry': False,
                                                   'request_sha256': job['request_body_sha256'], 'error': 'TimeoutError'})
                raise TimeoutError('synthetic only')
            response = {'prompt_text': '\n'.join(m['content'] for m in payload['messages']),
                        'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(
                            {'answer': 'yes', 'reason': 'Synthetic only.'} if mode == 'complete' else {'unexpected': True})}}]}
            raw = core.encoded(response)
            (capture / 'response.raw').write_bytes(raw)
            core.save(capture / 'receipt.json', {'status': 'complete', 'retry': False, 'http_status': 200,
                'request_sha256': job['request_body_sha256'], 'response_sha256': hashlib.sha256(raw).hexdigest()})
            return response

        try:
            with mock.patch.object(runner, 'exchange', side_effect=exchange):
                result = core.probe_measurement(job, capture,
                    lambda j: core.prepare_request(j, self.contexts(), self.config, runner), self.config, runner)
            status, failure = result['technical_status'], None
        except TimeoutError as error:
            result, status, failure = None, 'failure', repr(error)
        final = {'measurement_id': job['measurement_id'], 'input_sha256': core.digest(job),
                 'technical_status': status, 'admissible': status == 'complete', 'retry': False,
                 'result': result, 'failure': failure, 'integrity_failure': False,
                 'capture_files': [core.pin(p) for p in sorted(capture.iterdir())]}
        core.save(destination / 'FINAL.json', final)
        self.history.extend([{'event': 'claimed', 'measurement_id': job['measurement_id'], 'input_sha256': core.digest(job)},
            {'event': 'route_success' if status == 'complete' else 'route_failure', 'measurement_id': job['measurement_id'],
             'technical_status': status, 'final': core.pin(destination / 'FINAL.json')}])
        return destination

    def binding(self, *, pending=None, unavailable=None):
        ids = [j['measurement_id'] for j in self.obligations]
        ready_ids = [j['measurement_id'] for j in self.ready]
        pending = pending if pending is not None else ids[4:5]
        unavailable = unavailable if unavailable is not None else ids[5:]
        prepared = {'family': self.family, 'model': self.model, 'output_root_proposed': str(self.run_root),
                    'all_original_measurement_ids': ids, 'ready_jobs': self.ready, 'ready_order': ready_ids,
                    'contexts': list(self.contexts().values()), 'pending_measurement_ids': pending,
                    'unavailable_measurement_ids': unavailable, 'frozen_config': self.config_pin,
                    'frozen_runner': core.pin(Path(runner.__file__).resolve())}
        def lines(name, rows):
            p = self.root / name; p.write_bytes(b''.join(core.encoded(r) + b'\n' for r in rows)); return core.pin(p)
        value = {'schema': 'causes.supplement_export_binding.v1', 'family': self.family, 'model': self.model,
                 'supplement_root': str(self.run_root), 'stages': ['early', 'end'],
                 'question_ids': [q['id'] for q in self.config['questions']],
                 'prepared_inputs': self.save('prepared.json', prepared), 'roster': self.save('roster.json', self.roster),
                 'original_obligations': lines('obligations.jsonl', self.obligations),
                 'trajectories': lines('original.jsonl', self.rows), 'journal_cut': lines('journal_copy.jsonl', self.history),
                 'config': self.config_pin, 'runner_source': core.pin(Path(runner.__file__).resolve()),
                 'core_source': core.pin(Path(core.__file__).resolve()),
                 'collector_source': core.pin(Path(collector.__file__).resolve()), 'required_pins': []}
        return self.save('binding.json', value)

    def collect(self, binding):
        original_exchange, original_save = runner.exchange, runner.save
        with mock.patch.object(runner, 'exchange', side_effect=AssertionError('network forbidden')):
            result = collector.collect(binding, self.output, runner=runner)
        self.assertIs(runner.exchange, original_exchange)
        self.assertIs(runner.save, original_save)
        return json.loads(core.pinned(result))

class SupplementCollectTests(SupplementCollectFixture):
    def test_complete_missing_pending_unavailable_and_absent_original_join(self):
        for job, mode in zip(self.ready, ('complete', 'invalid', 'transport_failure')):
            self.final(job, mode)
        before = copy.deepcopy((self.obligations, self.ready, self.rows))
        summary = self.collect(self.binding())
        self.assertEqual(summary['dispositions'], {'final_verified': 3, 'unclaimed_at_journal_cut': 1,
            'pending_original_context_at_cut': 1, 'assigned_stage_unavailable': 1})
        self.assertFalse(summary['all_ready_final'])
        self.assertFalse(summary['all_obligations_final_or_unavailable'])
        rows = [json.loads(line) for line in (self.output / 'MEASUREMENTS.jsonl').read_text().splitlines()]
        self.assertEqual([r['admissible_label'] for r in rows], ['yes', None, None, None, None, None])
        joined = json.loads((self.output / 'trajectories/run1.json').read_text())
        self.assertEqual(joined['original_data_availability']['self_stages_present'], ['early'])
        self.assertNotIn('end', joined['original_trajectory']['self'])
        absent = json.loads((self.output / 'trajectories/run2.json').read_text())
        self.assertIsNone(absent['original_trajectory'])
        self.assertIsNone(absent['original_reader_unchanged'])
        self.assertEqual(len(absent['supplement_measurements']), 2)
        self.assertEqual(before, (self.obligations, self.ready, self.rows))

    def test_full_1440_original_identity_and_order_without_available_rows(self):
        self.roster = [{'run_id': f'run{i:02}', 'seed': i + 7} for i in range(60)]
        self.config['questions'] = [{**self.question, 'id': f'question{i:02}'} for i in range(12)]
        self.config_pin = self.save('config.json', self.config)
        self.obligations = [self.obligation(r, s, q) for r in reversed(self.roster)
                            for s in ('end', 'early') for q in reversed(self.config['questions'])]
        self.ready, self.rows = [], []
        ids = [j['measurement_id'] for j in self.obligations]
        summary = self.collect(self.binding(pending=ids[:1008], unavailable=ids[1008:]))
        rows = [json.loads(line) for line in (self.output / 'MEASUREMENTS.jsonl').read_text().splitlines()]
        self.assertEqual([r['original_obligation'] for r in rows], self.obligations)
        self.assertEqual([r['measurement_id'] for r in rows], ids)
        self.assertEqual(summary['supplement_assigned'], 1440)
        self.assertEqual(summary['original_stage_obligations'], 120)
        self.assertEqual(summary['original_rows_available_at_cut'], 0)
        self.assertFalse(summary['all_obligations_final_or_unavailable'])
        index = json.loads((self.output / 'INDEX.json').read_text())
        self.assertEqual([r['run_id'] for r in index], [r['run_id'] for r in self.roster])

    def test_other_model_same_id_is_filtered_and_does_not_fill_absence(self):
        other = self.original(self.roster[0], both=True)
        other['model'] = 'model_b'
        other['allocation']['seed'] = 999
        only_other = self.original(self.roster[2], both=True)
        only_other['model'] = 'model_b'
        self.rows = [other, *self.rows, only_other]
        summary = self.collect(self.binding())
        self.assertEqual(summary['original_rows_available_at_cut'], 2)
        present = json.loads((self.output / 'trajectories/run0.json').read_text())
        self.assertEqual(present['original_trajectory']['model'], self.model)
        self.assertEqual(present['allocation']['seed'], 7)
        absent = json.loads((self.output / 'trajectories/run2.json').read_text())
        self.assertIsNone(absent['original_trajectory'])
        self.assertFalse(absent['original_data_availability']['trajectory_in_frozen_cut'])
        self.assertEqual(len(absent['supplement_measurements']), 2)

    def test_saved_numeric_identity_drift_refuses_and_partial_output_is_not_reused(self):
        path = self.final(self.ready[0])
        changed = {**self.ready[0], 'original_seed': float(self.ready[0]['original_seed'])}
        self.assertEqual(changed, self.ready[0])
        (path / 'INPUT.json').write_bytes(core.encoded(changed) + b'\n')
        binding = self.binding()
        with self.assertRaisesRegex(core.IntegrityError, 'saved_input_identity'):
            self.collect(binding)
        self.assertTrue((self.output / 'ERROR.json').is_file())
        self.assertFalse((self.output / 'SUMMARY.json').exists())
        with self.assertRaisesRegex(core.IntegrityError, 'fresh_separate_output'):
            self.collect(binding)

    def test_unready_claim_and_original_order_drift_refuse_before_output(self):
        self.history = [{'event': 'claimed', 'measurement_id': self.obligations[-1]['measurement_id'], 'input_sha256': 'unused'}]
        with self.assertRaisesRegex(core.IntegrityError, 'unready_obligation_attempted'):
            self.collect(self.binding())
        self.assertFalse(self.output.exists())
        self.history = []
        binding = self.binding(); b = json.loads(core.pinned(binding)); prepared = json.loads(core.pinned(b['prepared_inputs']))
        prepared['all_original_measurement_ids'].reverse(); b['prepared_inputs'] = self.save('prepared.json', prepared)
        with self.assertRaisesRegex(core.IntegrityError, 'original_order_changed'):
            self.collect(self.save('binding.json', b))
        self.assertFalse(self.output.exists())

    def test_later_final_not_in_pinned_journal_stays_unclaimed(self):
        binding = self.binding()
        self.final(self.ready[0])  # Later terminal deliberately absent from the retained cut.
        summary = self.collect(binding)
        self.assertEqual(summary['terminals_at_consumed_cut'], 0)
        self.assertEqual(summary['dispositions']['unclaimed_at_journal_cut'], 4)

    def test_missing_success_capture_and_original_instrument_replacement_refuse(self):
        path = self.final(self.ready[0]); final = core.load(path / 'FINAL.json')
        final['capture_files'] = [p for p in final['capture_files'] if not p['path'].endswith('/receipt.json')]
        (path / 'FINAL.json').write_bytes(core.encoded(final) + b'\n')
        self.history[-1]['final'] = core.pin(path / 'FINAL.json')
        with self.assertRaisesRegex(core.IntegrityError, 'parsed_result_missing_capture'):
            self.collect(self.binding())
        self.output = self.root / 'second_export'; self.history = []
        self.rows[0]['self']['early'][self.question['id']] = {'label': 'original'}
        with self.assertRaisesRegex(core.IntegrityError, 'supplement_cannot_replace'):
            self.collect(self.binding())

    def test_input_pin_drift_after_real_verification_preserves_partial_failure(self):
        self.final(self.ready[0]); binding = self.binding()
        b = json.loads(core.pinned(binding)); source = self.save('qualified_source.json', {'version': 1})
        b['required_pins'] = [source]; binding = self.save('binding.json', b)
        real_verify = collector.verify_final
        def mutate(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            Path(source['path']).write_text('{}\n')
            return result
        with mock.patch.object(collector, 'verify_final', side_effect=mutate):
            with self.assertRaisesRegex(core.IntegrityError, 'file_pin_mismatch'):
                self.collect(binding)
        self.assertTrue((self.output / 'ERROR.json').is_file())
        self.assertTrue((self.output / 'MEASUREMENTS.jsonl').is_file())
        self.assertFalse((self.output / 'SUMMARY.json').exists())

    def test_output_confinement_and_active_journal_refusal(self):
        binding = self.binding()
        for output in (self.run_root, self.run_root / 'export', self.root):
            with self.subTest(output=output), self.assertRaisesRegex(core.IntegrityError, 'fresh_separate_output'):
                collector.collect(binding, output, runner=runner)
        b = json.loads(core.pinned(binding)); control = core.mkdir(self.run_root / '.control')
        (control / 'journal.jsonl').write_bytes(b''); b['journal_cut'] = core.pin(control / 'journal.jsonl')
        with self.assertRaisesRegex(core.IntegrityError, 'immutable_journal_copy'):
            self.collect(self.save('binding.json', b))


class SupplementCarryForwardTests(SupplementCollectFixture):
    def setUp(self):
        super().setUp()
        self.base = self.root
        self.rows = [self.original(row, both=True) for row in self.roster]
        for row in self.rows:
            row['context_sources'] = {stage: {'path': self.context_pin['path'],
                'bytes_sha256': self.context_pin['sha256'], 'context_sha256': runner.sha(self.context)}
                for stage in ('early', 'end')}

    def freeze_binding(self, batch, *, pending, unavailable):
        self.root = self.base / ('inputs' + str(batch))
        self.root.mkdir()
        try:
            return self.binding(pending=pending, unavailable=unavailable)
        finally:
            self.root = self.base

    def accepted(self, binding, summary):
        b = json.loads(core.pinned(binding))
        directory = Path(summary['measurements']['path']).parent
        summary_ref = core.pin(directory / 'SUMMARY.json')
        review = self.save(directory.name + '_review.json', {'accepted_supplement_export': True,
            'binding': binding, 'summary': summary_ref})
        retirement = self.save(directory.name + '_retirement.json', {
            'accepted_supplement_batch_retirement': True, 'prepared_inputs': b['prepared_inputs'],
            'supplement_root': b['supplement_root'], 'journal_cut': b['journal_cut'],
            'all_ready_terminal': True, 'journal_sealed': True, 'client_process_absent': True,
            'executor_lock_acquired_nonblocking': True, 'ambiguous_attempts': 0,
            'evidence': [b['journal_cut']]})
        return {'binding': binding, 'summary': summary_ref, 'independent_review': review,
                'retirement_review': retirement}

    def prior(self, *, finish=True):
        self.ready = [self.prepare(j) for j in self.obligations[:2]]
        self.final(self.ready[0])
        if finish:
            self.final(self.ready[1], 'transport_failure')
        ids = [j['measurement_id'] for j in self.obligations]
        binding = self.freeze_binding(0, pending=ids[2:5], unavailable=ids[5:])
        self.output = self.base / 'export0'
        return self.accepted(binding, self.collect(binding))

    def wave(self, prior, batch=1, ready_indices=(2, 3), *, finish=True):
        previous = json.loads(core.pinned(prior['binding']))
        previous_rows = [json.loads(line) for line in core.pinned(
            json.loads(core.pinned(prior['summary']))['measurements']).splitlines()]
        carried = [r['measurement_id'] for r in previous_rows if r['disposition'] == 'final_verified']
        self.run_root = self.base / ('attempts' + str(batch)); self.run_root.mkdir()
        self.output = self.base / ('export' + str(batch)); self.history = []
        self.ready = [self.prepare(self.obligations[i]) for i in ready_indices]
        ready_ids = [j['measurement_id'] for j in self.ready]
        if finish:
            for job in self.ready:
                self.final(job)
        ids = [j['measurement_id'] for j in self.obligations]
        pending = [mid for mid in ids[:-1] if mid not in carried + ready_ids]
        ref = self.freeze_binding(batch, pending=pending, unavailable=ids[-1:])
        b = json.loads(core.pinned(ref)); p = json.loads(core.pinned(b['prepared_inputs']))
        p['carried_measurement_ids'] = carried
        b['prepared_inputs'] = self.save('prepared_wave' + str(batch) + '.json', p)
        b.update(schema='causes.supplement_export_binding.v2', prior_export=prior)
        for key in ('roster', 'original_obligations', 'config', 'runner_source'):
            b[key] = previous[key]
        return self.save('binding_wave' + str(batch) + '.json', b)

    def change_prepared(self, ref, change):
        b = json.loads(core.pinned(ref)); p = json.loads(core.pinned(b['prepared_inputs']))
        change(p); b['prepared_inputs'] = self.save('changed_prepared.json', p)
        return self.save('changed_binding.json', b)

    def accepted_partial(self, binding, summary):
        prior = self.accepted(binding, summary)
        previous = json.loads(core.pinned(binding))
        prepared = json.loads(core.pinned(previous['prepared_inputs']))
        claimed = {row['measurement_id'] for row in
                   map(json.loads, core.pinned(previous['journal_cut']).splitlines()) if row['event'] == 'claimed'}
        retirement = json.loads(core.pinned(prior['retirement_review']))
        retirement.update(accepted_supplement_batch_retirement=False, all_ready_terminal=False,
            accepted_supplement_partial_batch_handoff=True, all_claimed_terminal=True,
            unclaimed_destinations_absent=True,
            unclaimed_measurement_ids=[mid for mid in prepared['ready_order'] if mid not in claimed])
        prior['retirement_review'] = self.save('partial_retirement.json', retirement)
        prior['handoff_mode'] = 'unclaimed_only'
        return prior

    def partial_prior(self, *, unresolved=False):
        self.final(self.ready[0])
        self.final(self.ready[1], 'transport_failure')
        if unresolved:
            self.history.append({'event': 'claimed', 'measurement_id': self.ready[2]['measurement_id'],
                                 'input_sha256': core.digest(self.ready[2])})
        ids = [j['measurement_id'] for j in self.obligations]
        binding = self.freeze_binding(0, pending=ids[4:5], unavailable=ids[5:])
        self.output = self.base / 'export0'
        return self.accepted_partial(binding, self.collect(binding))

    def partial_wave(self, prior, batch=1, ready_indices=(2, 3)):
        binding = self.wave(prior, batch, ready_indices)
        value = json.loads(core.pinned(binding))
        value['trajectories'] = json.loads(core.pinned(prior['binding']))['trajectories']
        return self.save('partial_binding' + str(batch) + '.json', value)

    def test_partial_handoff_preserves_failed_finals_and_exact_unclaimed_jobs_without_replay(self):
        prior = self.partial_prior()
        before = core.pinned(json.loads(core.pinned(prior['summary']))['measurements']).splitlines()
        binding = self.partial_wave(prior)
        real_pinned = core.pinned

        def no_old_capture(ref):
            self.assertFalse(Path(ref['path']).is_relative_to(self.base / 'attempts'))
            return real_pinned(ref)

        with mock.patch.object(core, 'pinned', side_effect=no_old_capture), \
                mock.patch.object(collector, 'verify_final', wraps=collector.verify_final) as verifier:
            summary = self.collect(binding)
        after = core.pinned(summary['measurements']).splitlines()
        self.assertEqual(after[:2], before[:2])
        self.assertEqual(after[4:], before[4:])
        self.assertEqual(verifier.call_count, 2)
        self.assertEqual(summary['technical_final_statuses'], {'complete': 3, 'failure': 1})
        self.assertTrue(summary['all_ready_final'])
        provenance = json.loads(core.pinned(summary['carry_forward']))
        self.assertEqual(provenance['unclaimed_measurement_ids'], [j['measurement_id'] for j in self.ready])
        self.assertEqual(provenance['handoff_mode'], 'unclaimed_only')
        self.assertFalse(provenance['runtime_authorized'])

    def test_partial_handoff_keeps_an_earlier_cumulative_failure_and_provenance_chain(self):
        prior = self.prior()
        first = self.wave(prior, finish=False)
        self.final(self.ready[0])
        value = json.loads(core.pinned(first))
        journal = self.base / 'partial_wave_journal.jsonl'
        journal.write_bytes(b''.join(core.encoded(row) + b'\n' for row in self.history))
        value['journal_cut'] = core.pin(journal)
        first = self.save('partial_first_binding.json', value)
        first_summary = self.collect(first)
        first_rows = core.pinned(first_summary['measurements']).splitlines()
        second = self.partial_wave(self.accepted_partial(first, first_summary), batch=2, ready_indices=(3,))
        summary = self.collect(second)
        self.assertEqual(summary['carried_final_count'], 3)
        self.assertEqual(core.pinned(summary['measurements']).splitlines()[:3], first_rows[:3])
        provenance = json.loads(core.pinned(summary['carry_forward']))
        self.assertEqual(provenance['predecessor_carry_forward'], first_summary['carry_forward'])
        self.assertEqual(summary['technical_final_statuses'], {'complete': 3, 'failure': 1})

    def test_partial_handoff_is_explicit_and_requires_its_own_sealed_retirement(self):
        prior = self.partial_prior(); binding = self.partial_wave(prior)
        base = json.loads(core.pinned(binding))
        for change in ('no_mode', 'invalid_mode', 'accepted_supplement_partial_batch_handoff',
                       'all_claimed_terminal', 'journal_sealed', 'client_process_absent',
                       'executor_lock_acquired_nonblocking', 'unclaimed_destinations_absent',
                       'ambiguous_attempts', 'unclaimed_measurement_ids'):
            with self.subTest(change=change):
                value = copy.deepcopy(base)
                if change == 'no_mode':
                    del value['prior_export']['handoff_mode']
                elif change == 'invalid_mode':
                    value['prior_export']['handoff_mode'] = True
                else:
                    retirement = json.loads(core.pinned(prior['retirement_review']))
                    retirement[change] = ([] if change == 'unclaimed_measurement_ids' else
                                          1 if change == 'ambiguous_attempts' else False)
                    value['prior_export']['retirement_review'] = self.save('bad_partial_retirement.json', retirement)
                with self.assertRaises(core.IntegrityError):
                    self.collect(self.save('bad_partial_binding.json', value))
                self.assertFalse(self.output.exists())

    def test_partial_handoff_rejects_unresolved_claim_even_with_acceptance_assertion(self):
        prior = self.partial_prior(unresolved=True)
        binding = self.partial_wave(prior, ready_indices=(3,))
        with self.assertRaisesRegex(core.IntegrityError, 'partial_predecessor_unresolved_claim'):
            self.collect(binding)
        self.assertFalse(self.output.exists())

    def test_partial_handoff_cannot_drop_reorder_change_or_reclassify_unclaimed_jobs(self):
        prior = self.partial_prior(); binding = self.partial_wave(prior)
        mutations = [lambda p: p['ready_jobs'].reverse(),
                     lambda p: p['ready_jobs'][0].update(seed=999),
                     lambda p: p['ready_jobs'][0].update(request_body_sha256='changed'),
                     lambda p: p['contexts'][0].update(extra='changed'),
                     lambda p: p['pending_measurement_ids'].append(p['ready_jobs'].pop()['measurement_id'])]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(core.IntegrityError):
                self.collect(self.change_prepared(binding, mutation))
            self.assertFalse(self.output.exists())

    def test_partial_handoff_checks_claim_identity_and_journal_counts(self):
        prior = self.partial_prior(); binding = self.partial_wave(prior)
        for mutation in ('claim', 'summary'):
            with self.subTest(mutation=mutation):
                value = json.loads(core.pinned(binding)); previous = json.loads(core.pinned(prior['binding']))
                if mutation == 'claim':
                    history = [json.loads(line) for line in core.pinned(previous['journal_cut']).splitlines()]
                    history[0]['input_sha256'] = 'changed'
                    journal = self.base / 'bad_claim_journal.jsonl'
                    journal.write_bytes(b''.join(core.encoded(row) + b'\n' for row in history))
                    previous['journal_cut'] = core.pin(journal)
                    value['prior_export']['binding'] = self.save('bad_claim_binding.json', previous)
                    retirement = json.loads(core.pinned(prior['retirement_review']))
                    retirement['journal_cut'] = previous['journal_cut']
                    value['prior_export']['retirement_review'] = self.save('bad_claim_retirement.json', retirement)
                else:
                    summary = json.loads(core.pinned(prior['summary'])); summary['claims_at_consumed_cut'] += 1
                    value['prior_export']['summary'] = self.save('bad_count_summary.json', summary)
                review = json.loads(core.pinned(prior['independent_review']))
                review.update(binding=value['prior_export']['binding'], summary=value['prior_export']['summary'])
                value['prior_export']['independent_review'] = self.save('bad_partial_review.json', review)
                with self.assertRaisesRegex(core.IntegrityError, 'partial_predecessor_(claim_identity|journal_counts)'):
                    self.collect(self.save('bad_partial_binding.json', value))
                self.assertFalse(self.output.exists())

    def test_carries_complete_and_failure_rows_exactly_without_old_capture_reads(self):
        prior = self.prior(); old = json.loads(core.pinned(prior['summary']))
        prior_lines = core.pinned(old['measurements']).splitlines()
        binding = self.wave(prior)
        real_pinned, real_verify = core.pinned, collector.verify_final
        seen = []
        def pin_without_old_capture(ref):
            self.assertFalse(Path(ref['path']).is_relative_to(self.base / 'attempts'))
            return real_pinned(ref)
        def verify(*args, **kwargs):
            seen.append(args[0]['measurement_id'])
            return real_verify(*args, **kwargs)
        with mock.patch.object(core, 'pinned', side_effect=pin_without_old_capture), \
                mock.patch.object(collector, 'verify_final', side_effect=verify):
            summary = self.collect(binding)
        lines = core.pinned(summary['measurements']).splitlines()
        self.assertEqual(lines[:2], prior_lines[:2])
        self.assertEqual(lines[-1], prior_lines[-1])
        self.assertEqual(seen, [j['measurement_id'] for j in self.ready])
        self.assertEqual(summary['carried_final_count'], 2)
        self.assertEqual(summary['technical_final_statuses'], {'complete': 3, 'failure': 1})
        self.assertTrue(summary['all_ready_final'])
        self.assertFalse(summary['all_obligations_final_or_unavailable'])

    def test_chain_keeps_all_old_rows_and_completeness_without_reparsing(self):
        prior = self.prior(); first = self.wave(prior)
        summary1 = self.collect(first); next_prior = self.accepted(first, summary1)
        first_lines = core.pinned(summary1['measurements']).splitlines()
        second = self.wave(next_prior, batch=2, ready_indices=(4,))
        with mock.patch.object(collector, 'verify_final', wraps=collector.verify_final) as verifier:
            summary2 = self.collect(second)
        self.assertEqual(verifier.call_count, 1)
        self.assertEqual(core.pinned(summary2['measurements']).splitlines()[:4], first_lines[:4])
        self.assertEqual(summary2['carried_final_count'], 4)
        self.assertTrue(summary2['all_obligations_final_or_unavailable'])
        provenance = json.loads(core.pinned(summary2['carry_forward']))
        self.assertEqual(provenance['predecessor_carry_forward'], summary1['carry_forward'])
        self.assertFalse(provenance['runtime_authorized'])

    def test_predecessor_carry_forward_link_cannot_be_dropped(self):
        prior = self.prior(); first = self.wave(prior)
        summary = self.collect(first); next_prior = self.accepted(first, summary)
        del summary['carry_forward']
        next_prior['summary'] = self.save('missing_chain_summary.json', summary)
        review = json.loads(core.pinned(next_prior['independent_review']))
        review['summary'] = next_prior['summary']
        next_prior['independent_review'] = self.save('missing_chain_review.json', review)
        second = self.wave(next_prior, batch=2, ready_indices=(4,))
        with self.assertRaisesRegex(core.IntegrityError, 'prior_carry_forward_provenance_missing'):
            self.collect(second)
        self.assertFalse(self.output.exists())

    def test_missing_or_changed_carried_context_source_refuses(self):
        prior = self.prior(); binding = self.wave(prior)
        b = json.loads(core.pinned(binding)); original_rows = copy.deepcopy(self.rows)
        for mutation in ('missing', 'path', 'bytes_sha256', 'context_sha256'):
            with self.subTest(mutation=mutation):
                rows = copy.deepcopy(original_rows)
                if mutation == 'missing':
                    del rows[0]['context_sources']['early']
                else:
                    rows[0]['context_sources']['early'][mutation] = 'changed'
                path = self.base / 'changed_rows.jsonl'
                path.write_bytes(b''.join(core.encoded(r) + b'\n' for r in rows))
                b['trajectories'] = core.pin(path)
                with self.assertRaisesRegex(core.IntegrityError, 'context_source_changed'):
                    self.collect(self.save('changed_context_binding.json', b))
                self.assertFalse(self.output.exists())

    def test_new_ready_context_must_match_current_original_cut(self):
        prior = self.prior(); binding = self.wave(prior)
        b = json.loads(core.pinned(binding)); self.rows[1]['context_sources'].pop('early')
        path = self.base / 'changed_rows.jsonl'
        path.write_bytes(b''.join(core.encoded(r) + b'\n' for r in self.rows))
        b['trajectories'] = core.pin(path)
        with self.assertRaisesRegex(core.IntegrityError, 'context_source_changed'):
            self.collect(self.save('changed_context_binding.json', b))

    def test_overlap_duplicate_and_dropped_carried_ids_refuse(self):
        prior = self.prior(); binding = self.wave(prior)
        mutations = [lambda p: p['carried_measurement_ids'].append(p['ready_order'][0]),
                     lambda p: p['carried_measurement_ids'].append(p['carried_measurement_ids'][0]),
                     lambda p: p['pending_measurement_ids'].append(p['carried_measurement_ids'].pop())]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(core.IntegrityError):
                self.collect(self.change_prepared(binding, mutation))
            self.assertFalse(self.output.exists())

    def test_old_unavailable_cannot_be_promoted_or_reclassified(self):
        prior = self.prior(); binding = self.wave(prior)
        def change(p):
            p['pending_measurement_ids'].extend(p['unavailable_measurement_ids'])
            p['unavailable_measurement_ids'] = []
        with self.assertRaisesRegex(core.IntegrityError, 'unavailable_cannot_be_promoted'):
            self.collect(self.change_prepared(binding, change))

    def test_old_unavailable_cannot_become_a_new_ready_job(self):
        prior = self.prior(); binding = self.wave(prior, ready_indices=(4, 5))
        binding = self.change_prepared(binding, lambda p: p.update(unavailable_measurement_ids=[]))
        with self.assertRaisesRegex(core.IntegrityError, 'unavailable_cannot_be_promoted'):
            self.collect(binding)
        self.assertFalse(self.output.exists())

    def test_prior_unclaimed_or_unresolved_attempt_cannot_cross_batch_boundary(self):
        prior = self.prior(finish=False)
        summary = json.loads(core.pinned(prior['summary']))
        summary['all_ready_final'] = True  # Even a false summary assertion cannot hide the row.
        prior['summary'] = self.save('incorrect_summary.json', summary)
        review = json.loads(core.pinned(prior['independent_review'])); review['summary'] = prior['summary']
        prior['independent_review'] = self.save('incorrect_review.json', review)
        binding = self.wave(prior)
        # Cover the otherwise unclaimed old ID as pending: the predecessor row
        # is still explicitly rejected, never treated as a newly ready context.
        with self.assertRaisesRegex(core.IntegrityError, 'prior_outstanding_or_unresolved_attempt'):
            self.collect(binding)
        self.assertFalse(self.output.exists())

    def test_prior_claim_without_terminal_refuses_even_if_review_asserts_completion(self):
        prior = self.prior(finish=False)
        previous = json.loads(core.pinned(prior['binding']))
        pending_job = self.ready[1]
        journal = core.pinned(previous['journal_cut']) + core.encoded({'event': 'claimed',
            'measurement_id': pending_job['measurement_id'], 'input_sha256': core.digest(pending_job)}) + b'\n'
        path = self.base / 'claimed_prior_journal.jsonl'; path.write_bytes(journal)
        previous['journal_cut'] = core.pin(path)
        prior_binding = self.save('claimed_prior_binding.json', previous)
        self.output = self.base / 'claimed_export'
        summary = self.collect(prior_binding)
        self.assertEqual(summary['dispositions']['attempted_pending_or_unresolved_at_cut'], 1)
        prior = self.accepted(prior_binding, summary)
        summary['all_ready_final'] = True
        prior['summary'] = self.save('incorrect_summary.json', summary)
        review = json.loads(core.pinned(prior['independent_review'])); review['summary'] = prior['summary']
        prior['independent_review'] = self.save('incorrect_review.json', review)
        binding = self.wave(prior)
        with self.assertRaisesRegex(core.IntegrityError, 'prior_outstanding_or_unresolved_attempt'):
            self.collect(binding)
        self.assertFalse(self.output.exists())

    def test_prior_review_retirement_and_original_scope_are_mandatory(self):
        prior = self.prior(); binding = self.wave(prior); base = json.loads(core.pinned(binding))
        mutations = ('review', 'drain', 'journal', 'config', 'questions', 'root')
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                b = copy.deepcopy(base)
                if mutation == 'review':
                    r = json.loads(core.pinned(prior['independent_review'])); r['accepted_supplement_export'] = False
                    b['prior_export']['independent_review'] = self.save('bad_review.json', r)
                elif mutation in ('drain', 'journal'):
                    r = json.loads(core.pinned(prior['retirement_review']))
                    if mutation == 'drain': r['journal_sealed'] = False
                    else: r['journal_cut'] = b['journal_cut']
                    b['prior_export']['retirement_review'] = self.save('bad_retirement.json', r)
                elif mutation == 'config':
                    b['config'] = self.save('different_config.json', self.config)
                elif mutation == 'questions':
                    b['question_ids'] = list(reversed(b['question_ids'])) + ['another']
                else:
                    b['supplement_root'] = json.loads(core.pinned(prior['binding']))['supplement_root']
                with self.assertRaises(core.IntegrityError):
                    self.collect(self.save('bad_binding.json', b))
                self.assertFalse(self.output.exists())

    def test_current_batch_cannot_claim_carried_measurement(self):
        prior = self.prior(); binding = self.wave(prior)
        b = json.loads(core.pinned(binding)); mid = self.obligations[0]['measurement_id']
        raw = core.encoded({'event': 'claimed', 'measurement_id': mid, 'input_sha256': 'old'}) + b'\n'
        path = self.base / 'illegal_journal.jsonl'; path.write_bytes(raw); b['journal_cut'] = core.pin(path)
        with self.assertRaisesRegex(core.IntegrityError, 'unready_obligation_attempted'):
            self.collect(self.save('illegal_binding.json', b))


if __name__ == '__main__':
    unittest.main()
