import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from causes import campaign_actor_evidence as actor
from causes import campaign_concurrency_evidence as traffic
from causes import campaign_runner as runner


class ClosedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'run'
        self.job = {'allocation': {'run_id': 'run', 'seed': 7, 'stage': 'full',
                                  'continue_to_end': True, 'instruments': ['q']},
                    'spec': {'task_id': 'task', 'workspace': '/workspace'},
                    'variant': {'id': 'baseline', 'request': 'Inspect the files.'}}
        self.config = {'model': 'example', 'endpoint': 'http://unused.invalid/chat',
                       'sampling': {}, 'system': 'Work in {workspace}.',
                       'tools': [{'type': 'function', 'function': {'name': 'bash'}}],
                       'max_actor_turns': 2, 'max_episode_seconds': 100,
                       'questions': [{'id': 'q', 'text': 'Question?', 'awareness_type': 'example', 'threshold': 'presence'}],
                       'answer_categories': ['yes', 'no']}

    def read(self, name):
        return json.loads((self.root / name).read_bytes())

    def replace(self, name, value):
        (self.root / name).write_bytes(runner.encoded(value))

    def summaries(self, **changes):
        for name in ('summary.json', 'actor_summary.json'):
            value = self.read(name)
            value.update(changes)
            self.replace(name, value)

    def generate(self, mode='natural', *, early=True, probe_mode='complete'):
        clock = [0]
        root = self.root
        if mode in ('runtime', 'before_first'):
            self.config['max_episode_seconds'] = 1
        if mode == 'missing_early':
            self.config['max_actor_turns'] = 1
            early = False

        class Sandbox:
            def __init__(self, spec, destination, run_id, **kwargs):
                self.destination = destination

            def start(self):
                self.destination.mkdir(parents=True)
                if mode == 'before_first':
                    clock[0] = 2

            def run(self, command, timeout):
                return {'returncode': 0, 'stdout': 'file', 'stderr': '', 'timeout': False}

            def checkpoint(self, phase):
                path = root / 'checkpoints' / phase / 'workspace.tar'
                path.parent.mkdir(parents=True)
                raw = b'opaque fixture archive'
                path.write_bytes(raw)
                return {'name': phase, 'complete': True, 'errors': [], 'archives': [{
                    'path': str(path), 'role': 'workspace_upper', 'bytes': len(raw),
                    'sha256': hashlib.sha256(raw).hexdigest()}]}

            def close(self):
                return {'state': 'closed', 'overlay_mounted': False, 'closure_errors': [], 'retained_container': True}

        def now():
            return (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=clock[0])).isoformat()

        def exchange(endpoint, payload, destination, timeout):
            destination.mkdir(parents=True)
            runner.save(destination / 'request.json', payload)
            attempt = {'started_at': now(), 'request_sha256': runner.sha(payload), 'retry': False}
            runner.save(destination / 'attempt.json', attempt)
            clock[0] += 2
            is_probe = 'probes' in destination.parts
            if is_probe and probe_mode == 'failure':
                runner.save(destination / 'receipt.json', {**attempt, 'finished_at': now(), 'elapsed_seconds': 2,
                    'status': 'transport_or_response_failure', 'error': "TimeoutError('fixture')"})
                raise TimeoutError('fixture')
            if is_probe:
                message = {'role': 'assistant', 'content': 'bad' if probe_mode == 'invalid' else '{"answer":"no","reason":"fixture"}'}
                finish = 'length' if probe_mode == 'truncated' else 'stop'
            else:
                final = mode == 'natural' and destination.name == '0001'
                message = {'role': 'assistant', 'content': 'Done.' if final else 'Inspecting.', 'reasoning': 'Check files.'}
                finish = 'stop' if final else 'tool_calls'
                if not final:
                    message['tool_calls'] = [{'id': 'call' + str(i), 'type': 'function',
                        'function': {'name': 'bash', 'arguments': '{"command":"true"}'}}
                        for i in range(3 if early else 1)]
            rendered = '\n'.join((m.get('content') or '') + '\n' + (m.get('reasoning') or '')
                                  + '\n' + json.dumps(m.get('tool_calls', [])) for m in payload['messages'])
            response = {'model': 'example', 'system_fingerprint': 'fixture',
                        'usage': {'prompt_tokens': 50, 'completion_tokens': 10, 'total_tokens': 60},
                        'prompt_text': rendered, 'choices': [{'message': message, 'finish_reason': finish}]}
            raw = runner.encoded(response)
            (destination / 'response.raw').write_bytes(raw)
            runner.save(destination / 'receipt.json', {**attempt, 'finished_at': now(), 'elapsed_seconds': 2,
                'status': 'complete', 'http_status': 200, 'response_sha256': hashlib.sha256(raw).hexdigest(),
                **{key: response[key] for key in ('model', 'system_fingerprint', 'usage')}})
            return response

        module = types.ModuleType('causes.campaign_sandbox')
        module.Sandbox = Sandbox
        with patch.dict('sys.modules', {'causes.campaign_sandbox': module}), \
             patch.object(runner, 'exchange', side_effect=exchange), \
             patch.object(runner.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(runner, 'now', side_effect=now):
            return runner.run_episode(self.job['spec'], self.job['variant'], self.config,
                                      self.job['allocation'], self.root)

    def audit(self):
        with patch.object(runner, 'exchange', side_effect=AssertionError('network forbidden')), \
             patch.object(runner, 'execute_tool', side_effect=AssertionError('tools forbidden')), \
             patch.object(runner, 'save', side_effect=AssertionError('writes forbidden')):
            return (actor.audit_closed_episode(self.root, self.job, self.config, original_directory=self.root),
                    traffic.audit_captured_self_probe_traffic(self.root, self.job, self.config))

    def test_native_natural_outputs_equal_existing_apis(self):
        self.generate()
        value, probes = self.audit()
        old = actor.audit_natural_episode(self.root, self.job, self.config, original_directory=self.root)
        self.assertEqual({key: value[key] for key in old}, old)
        old_probes = traffic.audit_self_probe_traffic(self.root, self.job, self.config)
        self.assertEqual({key: probes[key] for key in old_probes}, old_probes)
        self.assertEqual(value['captured_stages'], ['early', 'end'])
        self.assertEqual(probes['unattempted_probes'], [])

    def test_native_turn_censor_preserves_early_and_absent_end(self):
        self.generate('turns')
        value, probes = self.audit()
        self.assertEqual(value['end_status'], 'administratively_censored_turns')
        self.assertFalse(value['natural_completion'])
        self.assertEqual(value['captured_stages'], ['early'])
        self.assertEqual(value['uncaptured_stages'], ['end'])
        self.assertEqual(probes['completed_http_requests'], 1)
        self.assertEqual(probes['unattempted_probes'], [{'stage': 'end', 'instrument': 'q', 'reason': 'context_not_captured'}])
        with self.assertRaisesRegex(ValueError, 'natural_closed_actor_required'):
            actor.audit_natural_episode(self.root, self.job, self.config, original_directory=self.root)
        with self.assertRaisesRegex(ValueError, 'self_probe_stage_roster_changed'):
            traffic.audit_self_probe_traffic(self.root, self.job, self.config)

    def test_native_runtime_censor_preserves_elapsed_requests(self):
        self.generate('runtime')
        value, probes = self.audit()
        self.assertEqual(value['end_status'], 'administratively_censored_runtime')
        self.assertEqual(value['successful_requests'], 1)
        self.assertEqual(value['requests'][0]['elapsed_seconds'], 2)
        self.assertEqual(probes['uncaptured_stages'], ['end'])

    def test_native_censor_before_early_has_no_invented_probe(self):
        self.generate('missing_early')
        value, probes = self.audit()
        self.assertEqual(value['successful_requests'], 1)
        self.assertEqual(value['captured_stages'], [])
        self.assertEqual(value['checkpoint_archives'], [])
        self.assertEqual(probes['capacity_faults'], 0)
        self.assertEqual(probes['completed_http_requests'], 0)
        self.assertEqual([p['stage'] for p in probes['unattempted_probes']], ['early', 'end'])

    def test_native_runtime_censor_before_first_request(self):
        self.generate('before_first')
        value, probes = self.audit()
        self.assertEqual(value['successful_requests'], 0)
        self.assertEqual(value['request_latency_seconds'], {'count': 0})
        self.assertEqual(probes['captured_stages'], [])

    def test_semantic_invalid_and_truncated_probe_remain_measurement_statuses(self):
        for mode in ('invalid', 'truncated'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                self.root = Path(temp) / 'run'
                self.generate('turns', probe_mode=mode)
                _, probes = self.audit()
                self.assertEqual(probes['capacity_faults'], 0)
                self.assertEqual(probes['measurement_only_technical'], 1)
                self.assertEqual(len(probes['unattempted_probes']), 1)

    def test_failed_captured_probe_is_separate_from_uncaptured_end(self):
        self.generate('turns', probe_mode='failure')
        _, probes = self.audit()
        self.assertEqual(probes['capacity_faults'], 1)
        self.assertEqual(probes['completed_http_requests'], 0)
        self.assertEqual(probes['probes'][0]['technical_status'], 'failure')
        self.assertEqual(probes['unattempted_probes'], [{'stage': 'end', 'instrument': 'q', 'reason': 'context_not_captured'}])

    def test_unassigned_probe_directory_refuses(self):
        self.generate('turns')
        (self.root / 'probes' / 'early' / 'unassigned').mkdir()
        with self.assertRaisesRegex(ValueError, 'unexpected_self_probe_evidence'):
            self.audit()

    def test_technical_generation_and_closure_errors_are_not_censoring(self):
        self.generate('turns')
        original = self.read('summary.json')
        for change in ({'actor_error': 'TimeoutError'}, {'end_status': 'technical_failure'},
                       {'end_status': 'generation_length'}, {'sandbox_close_error': 'error'},
                       {'sandbox_closure': dict(original['sandbox_closure'], overlay_mounted=True)},
                       {'sandbox_closure': dict(original['sandbox_closure'], closure_errors=['error'])}):
            self.replace('summary.json', original)
            self.replace('actor_summary.json', dict(original, probes=[]))
            self.summaries(**change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'nontechnical_closed_actor_required'):
                self.audit()

    def test_premature_turn_censor_and_inconsistent_runtime_censor_refuse(self):
        self.generate('runtime')
        self.summaries(end_status='administratively_censored_turns')
        with self.assertRaisesRegex(ValueError, 'turn_censor_before_limit'):
            self.audit()
        self.summaries(end_status='administratively_censored_runtime', elapsed_seconds=0)
        with self.assertRaisesRegex(ValueError, 'runtime_censor_inconsistent'):
            self.audit()

    def test_unexpected_uncaptured_context_or_probe_is_not_silently_ignored(self):
        self.generate('turns')
        path = self.root / 'end_context.json'
        path.write_text('[]')
        with self.assertRaisesRegex(ValueError, 'uncaptured_stage_has_evidence:end'):
            self.audit()
        path.unlink()
        (self.root / 'probes' / 'end').mkdir()
        with self.assertRaisesRegex(ValueError, 'uncaptured_stage_has_evidence:end'):
            self.audit()

    def test_captured_context_hash_probe_roster_and_archive_remain_checked(self):
        self.generate('turns')
        context = self.read('early_context.json')
        self.replace('early_context.json', [])
        with self.assertRaisesRegex(ValueError, 'stage_context_changed:early'):
            self.audit()
        self.replace('early_context.json', context)
        summary = self.read('summary.json')
        self.replace('summary.json', dict(summary, probes=[]))
        with self.assertRaisesRegex(ValueError, 'self_probe_stage_roster_changed'):
            self.audit()
        self.replace('summary.json', summary)
        (self.root / 'checkpoints' / 'early' / 'workspace.tar').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'checkpoint_archive_changed'):
            self.audit()

    def test_natural_stop_relabelled_censored_is_refused(self):
        self.generate()
        self.summaries(end_status='administratively_censored_turns')
        with self.assertRaises(ValueError):
            self.audit()


if __name__ == '__main__':
    unittest.main()
