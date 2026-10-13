import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from causes import campaign_actor_evidence as audit
from causes import campaign_runner as runner


class PrefixEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'episode'
        self.job = {'allocation': {'run_id': 'episode', 'seed': 7, 'stage': 'prefix',
                                  'continue_to_end': False, 'instruments': []},
                    'spec': {'task_id': 'task', 'workspace': '/workspace'},
                    'variant': {'id': 'baseline', 'request': 'Inspect the files.'}}
        self.config = {'model': 'example', 'endpoint': 'http://unused.invalid/chat',
                       'sampling': {}, 'system': 'Work in {workspace}.',
                       'tools': [{'type': 'function', 'function': {'name': name}} for name in ('read', 'bash')],
                       'max_actor_turns': 5, 'max_episode_seconds': 100, 'questions': []}

    def read(self, relative):
        return json.loads((self.root / relative).read_bytes())

    def save(self, relative, value):
        (self.root / relative).write_bytes(runner.encoded(value))

    def generate(self, mode='exchange', returncode=0):
        root, clock = self.root, [0]

        class Sandbox:
            def __init__(self, spec, destination, run_id, **kwargs):
                self.root = destination / 'sandbox'
                self.root.mkdir(parents=True)
                (self.root / 'commands').mkdir()
                self.state = {'schema': 'caus.sandbox.v1', 'run_id': run_id, 'container': 'fixture',
                              'operations': []}
                self.counter = 0

            def start(self):
                pass

            def run(self, command, timeout):
                self.counter += 1
                prefix = self.root / 'commands' / f'{self.counter:05d}_tool'
                stdout, stderr = prefix.with_suffix('.stdout'), prefix.with_suffix('.stderr')
                stdout.write_bytes(b'file\n')
                stderr.write_bytes(b'')
                raw = {'argv': ['docker', 'exec', 'fixture', 'timeout', '-s', 'TERM', '-k', '2', str(timeout), *command],
                       'returncode': returncode, 'timeout': False, 'elapsed_seconds': 0.25,
                       'stdout_path': str(stdout), 'stderr_path': str(stderr), 'stdout_bytes': 5, 'stderr_bytes': 0,
                       'stdout': 'file\n', 'stderr': '', 'stdout_truncated': False, 'stderr_truncated': False}
                runner.save(prefix.with_suffix('.json'), raw)
                self.state['operations'].append({key: value for key, value in raw.items() if key not in ('stdout', 'stderr')})
                return dict(raw, timeout=returncode in (124, 137))

            def checkpoint(self, phase):
                path = self.root / 'checkpoints' / phase / 'workspace.tar'
                path.parent.mkdir(parents=True)
                path.write_bytes(b'opaque fixture archive')
                return {'name': phase, 'complete': True, 'errors': [], 'archives': [{
                    'path': str(path), 'role': 'workspace_upper', 'bytes': path.stat().st_size,
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]}

            def close(self):
                closure = {'state': 'closed', 'overlay_mounted': False, 'closure_errors': [], 'retained_container': True}
                self.state.update(closure)
                runner.save(self.root / 'STATE.json', self.state)
                return closure

        def now():
            return (dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=clock[0])).isoformat()

        def exchange(endpoint, payload, destination, timeout):
            destination.mkdir(parents=True)
            runner.save(destination / 'request.json', payload)
            attempt = {'started_at': now(), 'request_sha256': runner.sha(payload), 'retry': False}
            runner.save(destination / 'attempt.json', attempt)
            clock[0] += 2
            turn = int(destination.name)
            answer = {'role': 'assistant', 'content': 'Inspecting.', 'reasoning': 'Check the files.'}
            calls = []
            if mode != 'natural':
                count = 3 if mode == 'one_batch' else 1 if turn == 0 else 2
                for index in range(count):
                    name = 'read' if turn == 0 else 'bash'
                    arguments = {'path': 'file'} if name == 'read' else {'command': 'true'}
                    if mode == 'malformed' and turn == 0:
                        arguments = {'unexpected': 'file'}
                    if mode == 'nul' and turn == 0:
                        arguments = {'path': 'file\0suffix'}
                    calls.append({'id': f'call_{turn}_{index}', 'type': 'function',
                                  'function': {'name': name, 'arguments': json.dumps(arguments)}})
                answer['tool_calls'] = calls
            rendered = '\n'.join((message.get('content') or '') + '\n' + (message.get('reasoning') or '')
                                 + '\n' + json.dumps(message.get('tool_calls', [])) for message in payload['messages'])
            if mode == 'nul':
                rendered += '\nfile\0suffix'
            response = {'model': 'example', 'system_fingerprint': 'fixture', 'prompt_text': rendered,
                        'usage': {'prompt_tokens': 50, 'completion_tokens': 10, 'total_tokens': 60},
                        'choices': [{'message': answer, 'finish_reason': 'tool_calls' if calls else 'stop'}]}
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
            runner.run_episode(self.job['spec'], self.job['variant'], self.config, self.job['allocation'], root)

    def check(self):
        with patch.object(runner, 'exchange', side_effect=AssertionError('network forbidden')), \
             patch.object(runner, 'execute_tool', side_effect=AssertionError('tools forbidden')), \
             patch.object(runner, 'save', side_effect=AssertionError('writes forbidden')):
            return audit.audit_prefix_tool_exchanges(self.root, self.job, self.config, original_directory=self.root)

    def test_native_prefix_retains_real_exchange_and_final_batch(self):
        self.generate()
        result = self.check()
        self.assertEqual(result['successful_requests'], 2)
        self.assertEqual(result['tool_results'], 3)
        self.assertEqual(result['complete_native_tool_exchanges'], 1)
        self.assertEqual(result['tool_coverage']['read']['complete_exchanges'], 1)
        self.assertEqual(result['tool_coverage']['bash']['verified_executions'], 2)
        self.assertEqual(result['tool_coverage']['bash']['complete_exchanges'], 0)
        self.assertEqual(result['captured_stages'], ['early'])
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(result['tool_executions'], 0)

    def test_first_batch_with_three_results_has_no_followup(self):
        self.generate('one_batch')
        result = self.check()
        self.assertEqual(result['successful_requests'], 1)
        self.assertEqual(len(result['native_tool_executions']), 3)
        self.assertEqual(result['complete_native_tool_exchanges'], 0)

    def test_early_natural_completion_never_invents_tools_or_end_checkpoint(self):
        self.generate('natural')
        result = self.check()
        self.assertTrue(result['natural_completion'])
        self.assertEqual(result['complete_native_tool_exchanges'], 0)
        self.assertEqual(len(result['checkpoint_archives']), 1)

    def test_malformed_call_is_not_sandbox_execution(self):
        self.generate('malformed')
        result = self.check()
        self.assertEqual(result['complete_native_tool_exchanges'], 0)
        self.assertEqual(len(result['invalid_or_unsupported_tool_calls']), 1)
        self.assertEqual(result['tool_coverage']['read']['verified_executions'], 0)

    def test_nonzero_real_command_exit_and_native_timeout_are_preserved(self):
        for code in (2, 124):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary) / 'run'
                self.generate(returncode=code)
                result = self.check()
                self.assertEqual(result['complete_native_tool_exchanges'], 1)
                self.assertEqual(result['native_tool_executions'][0]['returncode'], code)
                self.assertEqual(result['native_tool_executions'][0]['timeout'], code == 124)

    def test_impossible_native_execution_with_nul_argument_refuses(self):
        self.generate('nul')
        with self.assertRaisesRegex(ValueError, 'tool_execution_invalid_argv'):
            self.check()

    def test_actual_command_stream_tamper_refuses(self):
        self.generate()
        (self.root / 'sandbox/sandbox/commands/00001_tool.stdout').write_bytes(b'bad!\n')
        with self.assertRaisesRegex(ValueError, 'tool_execution_stream_changed'):
            self.check()

    def test_missing_native_command_record_refuses(self):
        self.generate()
        (self.root / 'sandbox/sandbox/commands/00001_tool.json').unlink()
        with self.assertRaisesRegex((ValueError, FileNotFoundError), 'missing_actor_evidence|No such file'):
            self.check()

    def test_native_command_record_cannot_cover_two_calls(self):
        self.generate()
        earlier = self.read('tools/0001_000.json')
        later = self.read('tools/0001_001.json')
        later['result'] = earlier['result']
        self.save('tools/0001_001.json', later)
        with self.assertRaisesRegex(ValueError, 'tool_execution_reused_for_multiple_calls'):
            self.check()

    def test_subsequent_render_must_include_tool_result(self):
        self.generate()
        response = self.read('actor/0001/response.raw')
        response['prompt_text'] = 'Work in /workspace. Inspect the files.'
        self.save('actor/0001/response.raw', response)
        receipt = self.read('actor/0001/receipt.json')
        receipt['response_sha256'] = hashlib.sha256((self.root / 'actor/0001/response.raw').read_bytes()).hexdigest()
        self.save('actor/0001/receipt.json', receipt)
        with self.assertRaisesRegex(RuntimeError, 'render'):
            self.check()

    def test_command_identity_and_sandbox_operations_remain_bound(self):
        self.generate()
        original = self.read('sandbox/sandbox/STATE.json')
        for change in ({'container': 'other'}, {'operations': []}, {'run_id': 'other'}, {'state': 'running'}):
            self.save('sandbox/sandbox/STATE.json', dict(original, **change))
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'tool_execution|tool_sandbox'):
                self.check()

    def test_changed_request_with_rehashed_receipts_still_refuses(self):
        self.generate()
        request = self.read('actor/0001/request.json')
        request['messages'][-1]['tool_call_id'] = 'wrong'
        self.save('actor/0001/request.json', request)
        for file in ('attempt', 'receipt'):
            value = self.read(f'actor/0001/{file}.json')
            value['request_sha256'] = runner.sha(request)
            self.save(f'actor/0001/{file}.json', value)
        with self.assertRaisesRegex(ValueError, 'actor_request_changed'):
            self.check()

    def test_tool_result_path_escape_refuses(self):
        self.generate()
        saved = self.read('tools/0000_000.json')
        saved['result']['stdout_path'] = '/outside/00001_tool.stdout'
        self.save('tools/0000_000.json', saved)
        with self.assertRaisesRegex(ValueError, 'tool_execution_path_changed'):
            self.check()

    def test_json_distinct_request_types_refuse_with_rehashed_receipts(self):
        self.config['sampling']['max_tokens'] = 32768
        self.generate()
        original = self.read('actor/0000/request.json')
        for key, value in (('n', True), ('stream', 0), ('parallel_tool_calls', 1),
                           ('max_tokens', 32768.0)):
            with self.subTest(key=key):
                request = dict(original, **{key: value})
                self.save('actor/0000/request.json', request)
                for file in ('attempt', 'receipt'):
                    record = self.read(f'actor/0000/{file}.json')
                    record['request_sha256'] = runner.sha(request)
                    self.save(f'actor/0000/{file}.json', record)
                with self.assertRaisesRegex(ValueError, 'actor_request_changed:0000'):
                    self.check()

    def test_prefix_cannot_be_relabelled_full_or_censored(self):
        self.generate()
        with self.assertRaisesRegex(ValueError, 'full_episode_required'):
            audit.audit_closed_episode(self.root, self.job, self.config, original_directory=self.root)
        for file in ('summary.json', 'actor_summary.json'):
            summary = self.read(file)
            summary['end_status'] = 'administratively_censored_turns'
            self.save(file, summary)
        with self.assertRaisesRegex(ValueError, 'nontechnical_closed_prefix_required'):
            self.check()

    def test_extra_end_checkpoint_refuses(self):
        self.generate()
        (self.root / 'sandbox/sandbox/checkpoints/end').mkdir()
        with self.assertRaisesRegex(ValueError, 'prefix_has_unassigned_end_evidence'):
            self.check()


if __name__ == '__main__':
    unittest.main()
