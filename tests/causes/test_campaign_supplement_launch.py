import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from causes import campaign_runner as runner
from causes import campaign_supplement as core
from causes import campaign_supplement_launch as launch


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = {'endpoint': 'http://127.0.0.1:18081/v1/chat/completions', 'model': 'synthetic',
                       'sampling': {'temperature': 0.6}, 'tools': [], 'probe_max_tokens': 8192,
                       'request_timeout_seconds': 600, 'answer_categories': ['yes', 'no'],
                       'questions': [{'id': 'oversight_strong', 'text': 'Synthetic question.',
                                      'awareness_type': 'oversight_awareness', 'threshold': 'strongly_favors'}]}
        self.config_ref = self.save('config.json', self.config)
        self.context = [{'role': 'user', 'content': 'Synthetic task.'}]
        self.context_ref = self.save('context.json', self.context)
        self.contexts = {}
        self.output = self.root / 'output'
        self.baseline = self.root / 'baseline.jsonl'
        self.baseline.write_text('{"event":"start"}\n')
        self.scope = {'id': 'same-route-root', 'identity': {'host': 'host-a', 'root': '/root-a'},
                      'baseline': core.pin(self.baseline), 'mode': 'live_prefix',
                      'model_aliases': ['model:a'], 'accepted_state': self.replay(self.baseline.read_bytes())}
        self.disk = mock.patch.object(core.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=100 * 1024**3))
        self.disk.start(); self.addCleanup(self.disk.stop)

    def save(self, name, value):
        p = self.root / name; core.save(p, value); return core.pin(p)

    @staticmethod
    def replay(raw):
        result = {'lifetime_route_failures': 0, 'nonzero_failures': {}, 'quarantines': {},
                  'unresolved_events': [], 'unconsumed_tail_bytes': 0}
        for row in map(json.loads, raw.splitlines()):
            if row['event'] == 'failure':
                result['lifetime_route_failures'] += 1; result['nonzero_failures']['model:a'] = 1
            if row['event'] == 'success':
                result['nonzero_failures'] = {}
        return result

    def job(self, name):
        q = self.config['questions'][0]; seed = runner.seed_for(7, ['probe', 'early', q['id']])
        payload = runner.request(self.config, self.context + [{'role': 'user', 'content': q['text']}], seed, [])
        payload.update(tool_choice='none', max_tokens=8192); raw = runner.encoded(payload)
        self.contexts[(name, 'early')] = {'run_id': name, 'stage': 'early', 'original_bindings': {'context': self.context_ref}}
        return {'family': 'family', 'model': 'model_a', 'measurement_id': f'family/model_a/{name}/early/{q["id"]}',
                'run_id': name, 'stage': 'early', 'question_id': q['id'], 'original_seed': 7, 'seed': seed,
                'context': self.context_ref, 'frozen_config': self.config_ref, 'context_canonical_sha256': runner.sha(self.context),
                'question_canonical_sha256': runner.sha(q), 'request_body_sha256': core.hashlib.sha256(raw).hexdigest(),
                'request_file_sha256': core.hashlib.sha256(raw+b'\n').hexdigest(), 'request_body_bytes': len(raw)}

    def observation(self, policy):
        return {'schema': 'causes.supplement_admission_observation.v1', 'observed_from': core.now(), 'observed_until': core.now(),
                'identity': policy['runtime_identity'], 'checks': {k: True for k in launch.CHECKS},
                'controls': {'/qualified/current/PAUSE': False},
                'resources': {'local_free_bytes': 100*1024**3, 'remote_root_free_bytes': 25*1024**3,
                              'remote_scratch_free_bytes': 100*1024**3},
                'evidence': [self.config_ref], 'donors': [{'id': self.scope['id'], 'identity': self.scope['identity'],
                                                         'capture': core.pin(self.baseline)}]}

    def policy(self):
        jobs = [self.job('run0'), self.job('run1'), self.job('run2')]
        prepared = {'family': 'family', 'model': 'model_a', 'output_root_proposed': str(self.output),
                    'control_journal_proposed': str(self.output/'.control/journal.jsonl'), 'ready_jobs': jobs,
                    'ready_order': [j['measurement_id'] for j in jobs], 'contexts': list(self.contexts.values()),
                    'frozen_config': self.config_ref, 'public_source': core.pin(Path(core.__file__).resolve()),
                    'frozen_runner': core.pin(Path(runner.__file__).resolve())}
        p = {'phase': 'pilot', 'max_clients': 1, 'initial_failures': 0, 'prepared': self.save('prepared.json', prepared),
             'output_root': str(self.output), 'pilot_measurement_ids': prepared['ready_order'][:2],
             'local_pause_paths': [str(self.output/'PAUSE')], 'original_endpoint': self.config['endpoint'],
             'transport_endpoint': 'http://127.0.0.1:49181/v1/chat/completions',
             'launcher_source': core.pin(Path(launch.__file__).resolve()), 'core_source': prepared['public_source'],
             'runner_source': prepared['frozen_runner'], 'required_pins': [self.config_ref],
             'host_adapter_source': core.pin(Path(__file__).resolve()),
             'history_replay_source': core.pin(Path(__file__).resolve()),
             'runtime_identity': {'phase': 'running', 'model': 'synthetic', 'provider': 123},
             'remote_pause_paths': ['/qualified/current/PAUSE'], 'donors': [self.scope]}
        ref = self.save('policy.json', p)
        adoption = self.save('adoption.json', {'at': core.now(), 'authorized_supplement_invocation': True,
                    'policy': ref, 'phase': 'pilot', 'max_clients': 1, 'pilot_measurement_ids': p['pilot_measurement_ids']})
        return p, ref, adoption

    def exchange(self, endpoint, payload, destination, timeout):
        self.assertEqual(endpoint, 'http://127.0.0.1:49181/v1/chat/completions')
        self.assertEqual(timeout, 600); self.assertEqual(payload['max_tokens'], 8192)
        core.mkdir(destination); core.save(destination/'request.json', payload)
        core.save(destination/'attempt.json', {'retry': False}); core.save(destination/'receipt.json', {'status': 'complete'})
        response = {'prompt_text': '\n'.join(m['content'] for m in payload['messages']),
                    'choices': [{'finish_reason': 'stop', 'message': {'content': '{"answer":"no","reason":"Synthetic."}'}}]}
        (destination/'response.raw').write_bytes(runner.encoded(response)); return response

    def test_real_runner_endpoint_translation_preserves_original_request_and_pilot_order(self):
        p, ref, adoption = self.policy(); before = core.pinned(self.config_ref)
        with mock.patch.object(runner, 'exchange', side_effect=self.exchange) as exchange:
            result = launch.launch(ref, adoption, runner=runner, observation=lambda: self.observation(p),
                                   replay=self.replay, source_check=lambda: None)
            self.assertEqual(result['completed_ids'], p['pilot_measurement_ids'])
            self.assertEqual(exchange.call_count, 2)
            with self.assertRaisesRegex(core.IntegrityError, 'pilot_already_attempted'):
                launch.launch(ref, adoption, runner=runner, observation=lambda: self.observation(p),
                              replay=self.replay, source_check=lambda: None)
            self.assertEqual(exchange.call_count, 2)
        self.assertEqual(core.pinned(self.config_ref), before)
        self.assertFalse((self.output/'model_a/run2').exists())

    def test_selected_local_floor_requires_matching_adoption_before_observation(self):
        p, _, adoption = self.policy()
        p['local_free_space_floor_bytes'] = 64 * 1024**3
        ref = self.save('lower_floor.policy.json', p)
        adopted = {**core.load(adoption['path']), 'policy': ref}
        observation = mock.Mock(side_effect=AssertionError('must not observe'))
        for index, value in enumerate((None, True, float(64 * 1024**3), 0, 63 * 1024**3)):
            changed = dict(adopted)
            if value is not None:
                changed['local_free_space_floor_bytes'] = value
            adopted_ref = self.save('wrong_floor' + str(index) + '.json', changed)
            with self.subTest(value=value), self.assertRaises(core.IntegrityError):
                launch.launch(ref, adopted_ref, runner=runner, observation=observation,
                              replay=self.replay, source_check=lambda: None)
        observation.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_selected_local_floor_reaches_both_admission_checks_and_records_intent(self):
        p, _, adoption = self.policy()
        p['local_free_space_floor_bytes'] = 64 * 1024**3
        ref = self.save('lower_floor.policy.json', p)
        adopted = {**core.load(adoption['path']), 'policy': ref, 'local_free_space_floor_bytes': 64 * 1024**3}
        adopted_ref = self.save('lower_floor.adoption.json', adopted)

        def observation():
            value = self.observation(p)
            value['resources']['local_free_bytes'] = 70 * 1024**3
            return value

        with mock.patch.object(core.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=70 * 1024**3)), \
             mock.patch.object(runner, 'exchange', side_effect=self.exchange) as exchange:
            result = launch.launch(ref, adopted_ref, runner=runner, observation=observation,
                                   replay=self.replay, source_check=lambda: None)
        self.assertEqual(result['completed_ids'], p['pilot_measurement_ids'])
        self.assertEqual(exchange.call_count, 2)
        self.assertEqual(result['local_free_space_floor_bytes'], 64 * 1024**3)
        intent = core.load(next((self.output / '.control').glob('invocation_*/INTENT.json')))
        self.assertEqual(intent['local_free_space_floor_bytes'], 64 * 1024**3)

    def test_local_floor_does_not_lower_remote_storage_requirements(self):
        p, _, _ = self.policy()
        p['local_free_space_floor_bytes'] = 64 * 1024**3
        good = self.observation(p)
        good['resources']['local_free_bytes'] = 64 * 1024**3
        self.assertEqual(launch.qualify_observation(good, p, self.replay), good)
        for key, floor in [('remote_root_free_bytes', 20 * 1024**3),
                           ('remote_scratch_free_bytes', 80 * 1024**3),
                           ('local_free_bytes', 64 * 1024**3)]:
            changed = copy.deepcopy(good)
            changed['resources'][key] = floor - 1
            with self.subTest(key=key), self.assertRaisesRegex(core.AdmissionHold, key):
                launch.qualify_observation(changed, p, self.replay)

    def test_transport_rejects_other_setting_or_original_numeric_identity_change(self):
        job = self.job('run0'); transport = launch.transport_config(self.config, 'http://localhost:49181')
        transport['probe_max_tokens'] = 8191
        with self.assertRaisesRegex(core.IntegrityError, 'transport_changed_request_settings'):
            launch.request_source_check(job, self.contexts, self.config, transport, runner)
        original = copy.deepcopy(self.config); original['probe_max_tokens'] = 8192.0
        with self.assertRaisesRegex(core.IntegrityError, 'frozen_original_config_changed'):
            launch.request_source_check(job, self.contexts, original, transport, runner)

    def test_history_increase_and_success_cannot_erase_baseline_change(self):
        changed = self.root/'current.jsonl'
        for tail in ('{"event":"failure"}\n', '{"event":"failure"}\n{"event":"success"}\n'):
            changed.write_bytes(self.baseline.read_bytes()+tail.encode())
            with self.assertRaisesRegex(core.AdmissionHold, 'shared_route_history_changed'):
                launch.shared_histories([self.scope], [{'id': self.scope['id'], 'identity': self.scope['identity'],
                                                       'capture': core.pin(changed)}], self.replay)
        changed.write_text('{"event":"success"}\n')
        with self.assertRaisesRegex(core.IntegrityError, 'prefix_changed'):
            launch.shared_histories([self.scope], [{'id': self.scope['id'], 'identity': self.scope['identity'],
                                                   'capture': core.pin(changed)}], self.replay)

    def test_typed_donor_identity_drift_refuses(self):
        scope = copy.deepcopy(self.scope)
        scope['identity']['controller_pid'] = 7
        changed = copy.deepcopy(scope['identity']); changed['controller_pid'] = 7.0
        self.assertEqual(changed, scope['identity'])
        self.assertNotEqual(core.encoded(changed), core.encoded(scope['identity']))
        with self.assertRaisesRegex(core.IntegrityError, 'donor_route_identity_changed'):
            launch.shared_histories([scope], [{'id': scope['id'], 'identity': changed,
                                              'capture': core.pin(self.baseline)}], self.replay)

    def test_reviewed_continuation_reuses_two_pilot_finals_and_calls_only_third(self):
        p, ref, adoption = self.policy()
        def invoke(policy_ref, adoption_ref):
            return launch.launch(policy_ref, adoption_ref, runner=runner,
                                 observation=lambda: self.observation(p), replay=self.replay, source_check=lambda: None)
        def continuation(policy, name):
            policy_ref = self.save(name + '.policy.json', policy)
            adopted = self.save(name + '.adoption.json', {'at': core.now(), 'authorized_supplement_invocation': True,
                'policy': policy_ref, 'phase': 'continue', 'max_clients': 1,
                'pilot_measurement_ids': p['pilot_measurement_ids']})
            return policy_ref, adopted
        with mock.patch.object(runner, 'exchange', side_effect=self.exchange) as exchange:
            pilot = invoke(ref, adoption)
            self.assertEqual(pilot['completed_ids'], p['pilot_measurement_ids'])
            prepared = core.load(p['prepared']['path'])
            finals = [core.pin(core.measurement_path(self.output, job, family='family', model='model_a')/'FINAL.json')
                      for job in prepared['ready_jobs'][:2]]
            pilot_review = {'accepted_for_continuation': True, 'prepared': p['prepared'],
                            'pilot_measurement_ids': p['pilot_measurement_ids'], 'finals': finals}
            missing = {**p, 'phase': 'continue'}
            with self.assertRaises((core.IntegrityError, KeyError)):
                invoke(*continuation(missing, 'missing'))
            wrong = self.save('wrong_review.json', {**pilot_review, 'pilot_measurement_ids': list(reversed(p['pilot_measurement_ids']))})
            with self.assertRaisesRegex(core.IntegrityError, 'pilot_acceptance_required'):
                invoke(*continuation({**missing, 'pilot_acceptance': wrong}, 'wrong'))
            self.assertEqual(exchange.call_count, 2)
            accepted = self.save('pilot_review.json', pilot_review)
            result = invoke(*continuation({**missing, 'pilot_acceptance': accepted}, 'accepted'))
            self.assertEqual(result['existing_final'], 2)
            self.assertEqual(result['completed_ids'], prepared['ready_order'][2:])
            self.assertEqual(exchange.call_count, 3)
            for reference in finals:
                core.pinned(reference)

    def test_age_pause_identity_and_floor_refusals_precede_claim(self):
        p, _, _ = self.policy(); good = self.observation(p); at = dt.datetime.now(dt.timezone.utc)
        old = copy.deepcopy(good); old['observed_from'] = (at-dt.timedelta(seconds=241)).isoformat()
        with self.assertRaises(core.AdmissionHold): launch.qualify_observation(old, p, self.replay, at=at.isoformat())
        for change in ('pause', 'identity', 'floor', 'check'):
            value = copy.deepcopy(good)
            if change == 'pause': value['controls']['/qualified/current/PAUSE'] = True
            if change == 'identity': value['identity']['provider'] = 124
            if change == 'floor': value['resources']['remote_root_free_bytes'] = 19*1024**3
            if change == 'check': value['checks'].pop('model_template_revision')
            with self.assertRaises((core.AdmissionHold, core.IntegrityError)):
                launch.qualify_observation(value, p, self.replay)

    def test_source_failure_prevents_output_creation(self):
        p, ref, adoption = self.policy()
        with self.assertRaisesRegex(core.IntegrityError, 'synthetic_source_drift'):
            launch.launch(ref, adoption, runner=runner, observation=lambda: self.observation(p), replay=self.replay,
                          source_check=lambda: (_ for _ in ()).throw(core.IntegrityError('synthetic_source_drift')))
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
