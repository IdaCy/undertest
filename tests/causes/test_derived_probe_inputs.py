import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_runner as runner
from causes import campaign_supplement as supplement
from causes import derived_probe_inputs as derived
from causes import derived_reader_inputs as saved


class DerivedProbeInputsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = dict(model='original-model', endpoint='http://historical.invalid/v1/chat/completions',
            sampling=dict(temperature=.6, top_p=.95), tools=[dict(type='function',
                function=dict(name='read', parameters=dict(type='object')))],
            probe_max_tokens=8192, request_timeout_seconds=600,
            probe_response_format=dict(type='json_object'), answer_categories=['yes', 'no'],
            system='Original system instruction', questions=[dict(id='evaluation_' + threshold,
                awareness_type='evaluation', threshold=threshold, text='Original question: ' + threshold)
                for threshold in derived.THRESHOLDS])
        self.protocol = dict(schema=derived.PROTOCOL_SCHEMA, family='derived_probe_neutral_v1',
            model='original_model', config=self.save('execution-config.json', self.config),
            sources={name: saved.pin(module.__file__) for name, module in derived.MODULES.items()})
        self.allocation = dict(run_id='original-run-1', seed=1234, repetition=0, canonical_variant='B00L00')
        self.origin = dict(allocation=self.save('allocation.json', self.allocation),
                           config=self.save('original-config.json', self.config))
        initial = [dict(role='system', content='Adopted neutral summary, identical across arms.'),
                   dict(role='user', content='Implement this feature.')]
        actor = dict(role='assistant', content='I suspect this is an evaluation.',
            reasoning='Preserve this actor-authored thought.', tool_calls=[dict(id='read-1', type='function',
                function=dict(name='read', arguments='{"path":"/workspace/evaluation.txt"}'))])
        self.view = dict(messages=initial + [actor, dict(role='tool', tool_call_id='read-1',
            content='Saved tool text remains visible.')], initial_context=initial,
            prefix_context=None, phase='prefix')
        self.reference = dict(file=self.save('paired-view.json', dict(arms=dict(baseline=self.view))),
                              selector=['arms', 'baseline'])
        self.provenance = [self.save('private-provenance.json',
            dict(condition='SECRET-CONDITION', original_path='/private/origin', treatment='SECRET-TREATMENT'))]

    def save(self, name, value):
        path = self.root / name
        path.write_bytes(runner.encoded(value) + b'\n')
        return saved.pin(path)

    def row(self, **changes):
        args = dict(run_id=self.allocation['run_id'], view_kind='neutral_summary_v1', stage='early',
                    reference=self.reference, provenance=self.provenance, origin=self.origin,
                    protocol=self.protocol)
        args.update(changes)
        return derived.build_row(**args)

    def source_check(self, row, config=None):
        return lambda job: derived.prepare_probe(job, row, self.protocol, config or self.config)

    def fake_exchange(self, endpoint, payload, destination, timeout):
        self.assertEqual(endpoint, self.config['endpoint'])
        self.assertEqual(timeout, 600)
        destination = Path(destination)
        destination.mkdir(parents=True)
        runner.save(destination / 'request.json', payload)
        response = dict(prompt_text=runner.encoded(payload['messages']).decode()
            + ' /workspace/evaluation.txt', choices=[dict(finish_reason='stop',
                message=dict(role='assistant', content='{"answer":"yes","reason":"fixture"}'))])
        for name, value in [('attempt.json', dict(retry=False)), ('receipt.json', dict(status='complete')),
                            ('response.raw', response)]:
            runner.save(destination / name, value)
        return response

    def test_all_three_requests_match_unchanged_native_probe_and_preserve_actor_tool_text(self):
        row = self.row()
        before = copy.deepcopy((self.view, self.config, row))
        self.assertEqual([job['question_id'] for job in row['jobs']], list(derived.QUESTION_IDS))
        for index, job in enumerate(row['jobs']):
            context, question = self.source_check(row)(job)
            self.assertEqual(context, self.view['messages'])
            self.assertEqual(job['seed'], runner.seed_for(1234, ['probe', 'early', question['id']]))
            destination = self.root / ('native-' + str(index))
            with patch.object(runner, 'exchange', self.fake_exchange):
                result = supplement.probe_measurement(job, destination, self.source_check(row), self.config, runner)
            self.assertEqual(result['technical_status'], 'complete')
            body = (destination / 'request.json').read_bytes()
            self.assertEqual(hashlib.sha256(body).hexdigest(), job['request_file_sha256'])
            self.assertEqual(hashlib.sha256(body[:-1]).hexdigest(), job['request_body_sha256'])
            self.assertEqual(len(body) - 1, job['request_body_bytes'])
            self.assertEqual(json.loads(body)['messages'][:-1], self.view['messages'])
            for private in ('SECRET-CONDITION', 'SECRET-TREATMENT', '/private/origin', 'original-run-1'):
                self.assertNotIn(private, body.decode())
            self.assertIn('actor-authored', body.decode())
            self.assertIn('/workspace/evaluation.txt', body.decode())
        self.assertEqual((self.view, self.config, row), before)
        self.assertFalse(row['runtime_authorized'])

    def test_native_queue_accepts_distinct_jobs_without_altering_original_guard(self):
        row = self.row()
        output = self.root / 'native-queue'
        check = self.source_check(row)
        with patch.object(runner, 'exchange', self.fake_exchange), patch.object(supplement.shutil, 'disk_usage') as disk:
            disk.return_value.free = 100 * 1024**3
            result = supplement.run_queue(output, row['jobs'],
                lambda job, destination: supplement.probe_measurement(job, destination, check, self.config, runner),
                lambda: dict(synthetic_offline=True), check, [], family=self.protocol['family'],
                model=self.protocol['model'], max_clients=2)
        self.assertEqual((result['new_final'], result['remaining_unclaimed'], result['active']), (3, 0, 0))
        self.assertEqual(result['consecutive_failures'], 0)
        history = supplement.read_history(output / '.control/journal.jsonl')
        self.assertEqual(sum(row['event'] == 'claimed' for row in history), 3)
        self.assertEqual(sum(row['event'] == 'route_success' for row in history), 3)
        context_ref = self.save('whole-context.json', self.view['messages'])
        for job in row['jobs']:
            original_job = dict(job, context=context_ref)
            with self.assertRaisesRegex(supplement.IntegrityError, 'already_covered_question'):
                supplement.prepare_request(original_job, {(job['run_id'], 'early'): dict(original_bindings={})},
                                           self.config, runner)

    def test_changed_frozen_job_or_row_is_native_integrity_hold_before_claim(self):
        row = self.row()
        for key, value in [('seed', 99), ('original_seed', 99), ('question_id', 'evaluation_presence_changed'),
                           ('request_body_sha256', '0' * 64), ('measurement_id', 'old/identity')]:
            job = dict(row['jobs'][0], **{key: value})
            with self.subTest(key=key), self.assertRaisesRegex(supplement.IntegrityError, 'job_changed'):
                self.source_check(row)(job)
        output = self.root / 'held-queue'
        changed = copy.deepcopy(row)
        changed['jobs'][0]['request_file_sha256'] = '0' * 64
        worker = unittest.mock.Mock(side_effect=AssertionError('must never run'))
        with patch.object(supplement.shutil, 'disk_usage') as disk:
            disk.return_value.free = 100 * 1024**3
            result = supplement.run_queue(output, changed['jobs'], worker, lambda: {}, self.source_check(changed), [],
                family=self.protocol['family'], model=self.protocol['model'])
        worker.assert_not_called()
        self.assertEqual(result['new_final'], 0)
        self.assertIn('derived_probe_row_changed', result['stop_reason'])

    def test_endpoint_only_rebinding_preserves_identity_but_config_is_still_exact_at_runtime(self):
        first = self.row()
        routed = dict(self.config, endpoint='http://authorized-later.invalid/v1/chat/completions')
        protocol = dict(self.protocol, config=self.save('routed.json', routed))
        second = self.row(protocol=protocol)
        self.assertEqual(first['measurement_key'], second['measurement_key'])
        self.assertEqual(first['jobs'], second['jobs'])
        with self.assertRaisesRegex(supplement.IntegrityError, 'runtime_config_changed'):
            derived.prepare_probe(second['jobs'][0], second, protocol, self.config)
        self.assertEqual(derived.prepare_probe(second['jobs'][0], second, protocol, routed)[0], self.view['messages'])

    def test_any_other_original_config_change_is_rejected(self):
        for key, value in [('model', 'other-model'), ('sampling', dict(temperature=.7)), ('tools', []),
                           ('probe_max_tokens', 4096), ('request_timeout_seconds', 601),
                           ('probe_response_format', None), ('answer_categories', ['yes']),
                           ('system', 'Changed original system')]:
            config = dict(self.config, **{key: value})
            protocol = dict(self.protocol, config=self.save(key + '.json', config))
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'original_config_changed'):
                self.row(protocol=protocol)
        for change in ('text', 'duplicate', 'missing', 'threshold'):
            config = copy.deepcopy(self.config)
            if change == 'text': config['questions'][0]['text'] = 'A different instrument.'
            if change == 'duplicate': config['questions'].append(copy.deepcopy(config['questions'][0]))
            if change == 'missing': config['questions'].pop()
            if change == 'threshold': config['questions'][0]['threshold'] = 'strongly_favors'
            protocol = dict(self.protocol, config=self.save(change + '.json', config))
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.row(protocol=protocol)

    def test_original_allocation_must_match_and_bool_seed_is_rejected(self):
        for key, value in [('run_id', 'other-run'), ('seed', True), ('seed', -1),
                           ('repetition', True), ('canonical_variant', '')]:
            origin = dict(self.origin, allocation=self.save('changed-allocation.json', dict(self.allocation, **{key: value})))
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, 'original_allocation_changed'):
                self.row(origin=origin)
        changed_seed = dict(self.origin, allocation=self.save('new-seed.json', dict(self.allocation, seed=5678)))
        self.assertNotEqual(self.row()['measurement_key'], self.row(origin=changed_seed)['measurement_key'])

    def test_identity_depends_on_view_protocol_assignment_but_not_file_location(self):
        first = self.row()
        copied = self.row(reference=dict(file=self.save('copy.json', self.view), selector=[]))
        self.assertEqual(first['measurement_key'], copied['measurement_key'])
        changed = copy.deepcopy(self.view)
        changed['messages'][2]['content'] = 'Different recorded actor text.'
        self.assertNotEqual(first['measurement_key'], self.row(reference=dict(file=self.save('changed-view.json', changed), selector=[]))['measurement_key'])
        self.assertNotEqual(first['measurement_key'], self.row(view_kind='other_accepted_view')['measurement_key'])
        other_origin = dict(self.origin, allocation=self.save('other-run.json', dict(self.allocation, run_id='other-run')))
        self.assertNotEqual(first['measurement_key'], self.row(run_id='other-run', origin=other_origin)['measurement_key'])
        for job in first['jobs']:
            self.assertNotIn('/original-run-1/', job['measurement_id'])
            self.assertTrue(job['run_id'].startswith('derived_probe_'))

    def test_pinned_source_view_origin_and_provenance_changes_are_rejected(self):
        row = self.row()
        for ref in (self.reference['file'], self.origin['allocation'], self.origin['config'], self.provenance[0]):
            path = Path(ref['path'])
            original = path.read_bytes()
            path.write_bytes(original + b' ')
            with self.subTest(path=path), self.assertRaisesRegex(supplement.IntegrityError, 'input_changed'):
                self.source_check(row)(row['jobs'][0])
            path.write_bytes(original)
        protocol = copy.deepcopy(self.protocol)
        protocol['sources']['campaign_runner'] = self.save('different-module.py', dict(not_source=True))
        with self.assertRaisesRegex(ValueError, 'native_source_changed'):
            self.row(protocol=protocol)

    def test_full_and_early_boundaries_remain_native(self):
        full = copy.deepcopy(self.view)
        full.update(phase='full', prefix_context=copy.deepcopy(full['messages']))
        full['messages'].append(dict(role='assistant', content='Final actor text.'))
        reference = dict(file=self.save('full-view.json', full), selector=[])
        row = self.row(reference=reference, stage='end')
        context, _ = self.source_check(row)(row['jobs'][0])
        self.assertEqual(context, full['messages'])
        with self.assertRaisesRegex(ValueError, 'stage_changed'):
            self.row(reference=reference)
        for prefix in (None, [], [dict(role='system', content='Wrong prefix')]):
            malformed = dict(full, prefix_context=prefix)
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                self.row(reference=dict(file=self.save('malformed-full.json', malformed), selector=[]), stage='end')

    def test_private_metadata_is_rejected_inside_view_but_not_scrubbed_from_actor_text(self):
        for where in ('top', 'message', 'tool'):
            view = copy.deepcopy(self.view)
            if where == 'top': view['condition'] = 'SECRET'
            if where == 'message': view['messages'][2]['condition'] = 'SECRET'
            if where == 'tool': view['messages'][2]['tool_calls'][0]['function']['condition'] = 'SECRET'
            with self.subTest(where=where), self.assertRaises(ValueError):
                self.row(reference=dict(file=self.save('metadata-' + where + '.json', view), selector=[]))

    def test_missing_arms_order_and_duplicates_remain_explicit(self):
        ready = self.row()
        missing = derived.unavailable_row('unavailable-run', 'neutral_summary_v1', 'early',
                                         'context_not_captured', self.provenance, self.protocol)
        self.assertEqual(missing['jobs'], [])
        self.assertIsNone(missing['view'])
        self.assertFalse(missing['runtime_authorized'])
        with self.assertRaisesRegex(supplement.IntegrityError, 'prepared_row_required'):
            self.source_check(missing)(ready['jobs'][0])
        rows = [ready, missing]
        assignments = [{k: row[k] for k in ('run_id', 'view_kind', 'stage')} for row in rows]
        self.assertEqual(derived.validate_membership(assignments, rows, self.protocol),
                         dict(assigned=2, ready=1, unavailable=1, requests=3, missing_slots=3))
        for altered in ([ready], list(reversed(rows)), [ready, ready]):
            with self.subTest(altered=len(altered)), self.assertRaises(ValueError):
                derived.validate_membership(assignments, altered, self.protocol)
        with self.assertRaisesRegex(ValueError, 'duplicate_assignment'):
            derived.validate_membership([assignments[0]] * 2, [ready] * 2, self.protocol)

    def test_source_check_uses_native_integrity_exception_for_missing_file(self):
        row = self.row()
        Path(self.origin['allocation']['path']).unlink()
        with self.assertRaises(supplement.IntegrityError):
            self.source_check(row)(row['jobs'][0])


if __name__ == '__main__':
    unittest.main()
