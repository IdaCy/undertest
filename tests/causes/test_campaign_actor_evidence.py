import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_actor_evidence as audit
from causes import campaign_runner as runner


class ActorEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'episode'
        self.root.mkdir()
        self.original = Path('/original/episode')
        self.job = {'allocation': {'run_id': 'episode', 'seed': 19, 'continue_to_end': True},
                    'spec': {'task_id': 'task', 'workspace': '/workspace/project'},
                    'variant': {'id': 'v1', 'request': 'Inspect the workspace.'}}
        self.config = {'model': 'example', 'system': 'Work in {workspace}.', 'sampling': {'temperature': 0.5},
                       'tools': [{'type': 'function', 'function': {'name': 'bash'}}]}
        for name, value in [('allocation', self.job['allocation']), ('spec', self.job['spec']),
                            ('variant', self.job['variant']), ('model', self.config)]:
            self.save(name + '.json', value)
        messages = [{'role': 'system', 'content': 'Work in /workspace/project.'},
                    {'role': 'user', 'content': 'Inspect the workspace.'}]
        self.save('initial_context.json', messages)
        calls = [{'id': 'tool' + str(i), 'type': 'function',
                  'function': {'name': 'bash', 'arguments': json.dumps({'command': 'true'})}} for i in range(3)]
        for turn in range(2):
            name = f'{turn:04d}'
            payload = runner.request(self.config, messages, runner.seed_for(19, ['actor', turn]), self.config['tools'])
            answer = {'role': 'assistant', 'content': 'Inspecting.' if turn == 0 else 'Done.',
                      'reasoning': 'Check the files.' if turn == 0 else 'The check is complete.'}
            if turn == 0:
                answer['tool_calls'] = calls
            rendered = '\n'.join((m.get('content') or '') + '\n' + (m.get('reasoning') or '')
                                 + '\n' + json.dumps(m.get('tool_calls', [])) for m in messages)
            response = {'model': 'example', 'system_fingerprint': 'fixture', 'prompt_text': rendered,
                        'usage': {'prompt_tokens': 50 + turn, 'completion_tokens': 10, 'total_tokens': 60 + turn},
                        'choices': [{'index': 0, 'message': answer, 'finish_reason': 'tool_calls' if turn == 0 else 'stop'}]}
            self.save('actor/' + name + '/request.json', payload)
            attempt = {'request_sha256': runner.sha(payload), 'retry': False,
                       'started_at': f'2026-01-01T00:00:0{turn * 2}+00:00'}
            self.save('actor/' + name + '/attempt.json', attempt)
            raw = runner.encoded(response)
            (self.root / 'actor' / name / 'response.raw').write_bytes(raw)
            receipt = dict(attempt, response_sha256=hashlib.sha256(raw).hexdigest(), status='complete',
                           http_status=200, elapsed_seconds=1 + turn,
                           finished_at=f'2026-01-01T00:00:0{turn * 2 + 1}+00:00',
                           **{key: response[key] for key in ('usage', 'model', 'system_fingerprint')})
            self.save('actor/' + name + '/receipt.json', receipt)
            messages.append(copy.deepcopy(answer))
            if turn == 0:
                for index, call in enumerate(calls):
                    result = {'returncode': 0, 'stdout': 'file\n', 'stderr': '', 'timeout': False}
                    self.save(f'tools/{name}_{index:03d}.json', {'call': call, 'result': result})
                    messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': runner.render_tool(result, 32000)})
                early = copy.deepcopy(messages)
            self.save('contexts/' + name + '.json', messages)
        self.save('early_context.json', early)
        self.save('end_context.json', messages)
        self.save('actor_final_context.json', messages)
        summary = {'run_id': 'episode', 'task_id': 'task', 'variant_id': 'v1', 'calls': 2,
                   'tool_results': 3, 'elapsed_seconds': 5, 'end_status': 'natural_completion',
                   'early_status': 'completed_batch_at_least_three_results',
                   'early_context_sha256': runner.sha(early), 'actor_context_sha256': runner.sha(messages),
                   'sandbox_closure': {'state': 'closed', 'overlay_mounted': False, 'closure_errors': []}}
        for phase in ('early', 'end'):
            relative = Path('checkpoints') / phase / 'workspace.tar'
            path = self.root / relative
            path.parent.mkdir(parents=True)
            raw = ('opaque fixture ' + phase).encode()
            path.write_bytes(raw)
            summary[phase + '_workspace_status'] = 'captured'
            summary[phase + '_workspace_checkpoint'] = {'name': phase, 'complete': True, 'errors': [],
                'archives': [{'path': str(self.original / relative), 'bytes': len(raw),
                              'sha256': hashlib.sha256(raw).hexdigest(), 'role': 'workspace_upper'}]}
        self.save('actor_summary.json', dict(summary, probes=[]))
        self.save('summary.json', dict(summary, probes=[{'separate_probe': True}]))
        self.save('early_capture.json', {key: value for key, value in summary.items() if key.startswith('early_')})

    def save(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(runner.encoded(value) + b'\n')

    def read(self, name):
        return json.loads((self.root / name).read_bytes())

    def check(self):
        return audit.audit_natural_episode(self.root, self.job, self.config, original_directory=self.original)

    def test_reconstructs_all_turns_and_archives(self):
        result = self.check()
        self.assertEqual(result['successful_requests'], 2)
        self.assertEqual(result['tool_results'], 3)
        self.assertEqual(result['request_latency_seconds']['median'], 1.5)
        self.assertEqual(result['request_latency_seconds']['p95_nearest_rank'], 2)
        self.assertEqual(len(result['checkpoint_archives']), 2)
        self.assertEqual(result['missing_or_invalid_token_usage']['prompt_tokens'], [])
        self.assertEqual(result['model_calls'], 0)

    def test_rejects_changed_request_even_with_matching_request_digests(self):
        name = 'actor/0001/request.json'
        request = self.read(name)
        request['messages'][0]['content'] = 'Changed instruction.'
        self.save(name, request)
        for name in ('attempt', 'receipt'):
            value = self.read('actor/0001/' + name + '.json')
            value['request_sha256'] = runner.sha(request)
            self.save('actor/0001/' + name + '.json', value)
        with self.assertRaisesRegex(ValueError, 'actor_request_changed'):
            self.check()

    def test_rejects_raw_response_corruption(self):
        (self.root / 'actor/0000/response.raw').write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'actor_request_receipt_changed'):
            self.check()

    def test_rejects_tool_history_corruption(self):
        tool = self.read('tools/0000_001.json')
        tool['result']['stdout'] = 'changed'
        self.save('tools/0000_001.json', tool)
        with self.assertRaisesRegex(ValueError, 'saved_actor_context_changed'):
            self.check()

    def test_rejects_unreported_actor_turn(self):
        (self.root / 'actor/0002').mkdir()
        with self.assertRaisesRegex(ValueError, 'actor_turn_roster_changed'):
            self.check()

    def test_rejects_archive_corruption_and_escape(self):
        (self.root / 'checkpoints/end/workspace.tar').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'checkpoint_archive_changed'):
            self.check()
        value = self.read('summary.json')
        value['end_workspace_checkpoint']['archives'][0]['path'] = '/outside/archive.tar'
        self.save('summary.json', value)
        self.save('actor_summary.json', value)
        with self.assertRaisesRegex(ValueError, 'checkpoint_archive_outside_run'):
            self.check()


if __name__ == '__main__':
    unittest.main()
