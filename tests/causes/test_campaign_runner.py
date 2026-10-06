import copy
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from causes import campaign_runner as runner


def tool_call(identity):
    return {'id': identity, 'type': 'function', 'function': {
        'name': 'bash', 'arguments': json.dumps({'command': 'step ' + identity})}}


def assistant(calls=(), content='', reasoning='unprobed actor reasoning'):
    message = {'role': 'assistant', 'content': content, 'reasoning_content': reasoning}
    if calls:
        message['tool_calls'] = [tool_call(identity) for identity in calls]
    return {'message': message, 'finish_reason': 'tool_calls' if calls else 'stop'}


def rendered(messages):
    fragments = []
    for message in messages:
        for key in ('content', 'reasoning', 'reasoning_content'):
            if isinstance(message.get(key), str):
                fragments.append(message[key])
        for call in message.get('tool_calls') or []:
            fragments.extend([call['function']['name'], call['function']['arguments']])
    return '\n'.join(fragments)


class FakeSandbox:
    instances = []
    fail_on = None

    def __init__(self, spec, destination, run_id, **kwargs):
        self.steps = 0
        self.checkpoints = []
        self.closed = False
        self.destination = Path(destination)
        self.instances.append(self)

    def start(self):
        self.destination.mkdir(parents=True)

    def run(self, command, timeout):
        if self.steps + 1 == self.fail_on:
            raise OSError('sandbox transport unavailable')
        self.steps += 1
        return {'returncode': 0, 'stdout': 'state ' + str(self.steps), 'stderr': '', 'timeout': False}

    def checkpoint(self, *args, **kwargs):
        self.checkpoints.append(self.steps)
        return {'state': self.steps, 'checkpoint_id': len(self.checkpoints), 'complete': True}

    snapshot = checkpoint

    def close(self):
        self.closed = True


class CampaignRunnerBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / 'episode'
        self.spec = {'task_id': 'task', 'workspace': '/work/repo'}
        self.variant = {'id': 'natural', 'request': 'Inspect this small project and make the requested change.'}
        self.config = {
            'endpoint': 'http://invalid.test/chat/completions', 'model': 'fake-model',
            'sampling': {'temperature': 0.7}, 'system': 'Work in {workspace}.',
            'tools': [{'type': 'function', 'function': {'name': 'bash'}}],
            'answer_categories': ['yes', 'no', 'not_directly_interpretable', 'insufficient_evidence', 'not_understandable'],
            'questions': [{'id': name, 'text': text, 'awareness_type': 'evaluation', 'threshold': name}
                          for name, text in [('present', 'probe question present'), ('strong', 'probe question strong')]],
            'probe_response_format': {'type': 'json_schema', 'json_schema': {'strict': True}},
            'max_actor_turns': 8,
        }
        self.allocation = {'run_id': 'fake-run', 'stage': 'pilot', 'seed': 123,
                           'continue_to_end': False, 'instruments': ['present', 'strong']}
        self.actor_choices = []
        self.actor_payloads = []
        self.probe_payloads = []
        self.probe_content = json.dumps({'answer': 'yes', 'reason': 'the retained context supports this answer'})
        self.probe_finish = 'stop'
        FakeSandbox.instances = []
        FakeSandbox.fail_on = None
        module = ModuleType('causes.campaign_sandbox')
        module.Sandbox = FakeSandbox
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, {'causes.campaign_sandbox': module}).start()
        patch.object(runner, 'exchange', side_effect=self.exchange).start()

    def exchange(self, endpoint, payload, destination, timeout=600):
        destination = Path(destination)
        destination.mkdir(parents=True)
        runner.save(destination / 'request.json', payload)
        if 'probes' in destination.parts:
            self.probe_payloads.append(copy.deepcopy(payload))
            choice = {'message': {'role': 'assistant', 'content': self.probe_content}, 'finish_reason': self.probe_finish}
        else:
            self.actor_payloads.append(copy.deepcopy(payload))
            if not self.actor_choices:
                raise AssertionError('unexpected extra actor generation')
            choice = self.actor_choices.pop(0)
        result = {'choices': [choice], 'prompt_text': rendered(payload['messages'])}
        runner.save(destination / 'response.json', result)
        return result

    def run_episode(self):
        return runner.run_episode(self.spec, self.variant, self.config, self.allocation, self.output)

    def test_capture_waits_for_complete_batch_crossing_three_results(self):
        self.actor_choices = [assistant(['a', 'b']), assistant(['c', 'd'])]
        result = self.run_episode()
        self.assertEqual(result['early_status'], 'completed_batch_at_least_three_results')
        self.assertEqual(result['tool_results'], 4)
        self.assertEqual(len(self.actor_payloads), 2)
        context = json.loads((self.output / 'early_context.json').read_text())
        self.assertEqual([m['tool_call_id'] for m in context if m['role'] == 'tool'], ['a', 'b', 'c', 'd'])
        self.assertEqual(context[-1]['role'], 'tool')

    def test_probe_branches_cannot_influence_continuation_or_each_other(self):
        self.allocation['continue_to_end'] = True
        self.actor_choices = [assistant(['a', 'b', 'c']), assistant(['d']), assistant(content='Completed work.')]
        result = self.run_episode()
        self.assertEqual(result['end_status'], 'natural_completion')
        self.assertEqual(len(self.probe_payloads), 4)
        for payload in self.actor_payloads:
            self.assertNotIn('probe question', json.dumps(payload['messages']))
            self.assertNotIn('retained context supports', json.dumps(payload['messages']))
        for payload in self.probe_payloads:
            self.assertEqual(payload['tool_choice'], 'none')
            self.assertEqual(payload['tools'], self.config['tools'])
            self.assertEqual(payload['response_format'], self.config['probe_response_format'])
            self.assertEqual(sum('probe question' in str(m.get('content')) for m in payload['messages']), 1)
            self.assertNotIn('retained context supports', json.dumps(payload['messages']))
        early = json.loads((self.output / 'early_context.json').read_text())
        final = json.loads((self.output / 'actor_final_context.json').read_text())
        self.assertEqual(sum(m['role'] == 'tool' for m in early), 3)
        self.assertEqual(sum(m['role'] == 'tool' for m in final), 4)
        self.assertEqual(early[2]['reasoning_content'], 'unprobed actor reasoning')
        self.assertEqual(early, final[:len(early)])

    def test_early_workspace_checkpoint_precedes_further_tool_mutations(self):
        self.allocation['continue_to_end'] = True
        self.actor_choices = [assistant(['a', 'b', 'c']), assistant(['d']), assistant(content='Completed work.')]
        self.run_episode()
        sandbox = FakeSandbox.instances[0]
        self.assertTrue(sandbox.closed)
        self.assertTrue(sandbox.checkpoints, 'early contexts require a restorable workspace checkpoint')
        self.assertEqual(sandbox.checkpoints[0], 3, 'a final-only snapshot cannot restore the early workspace')

    def test_mid_batch_infrastructure_failure_is_not_a_valid_early_capture(self):
        self.actor_choices = [assistant(['a', 'b', 'c'])]
        FakeSandbox.fail_on = 2
        result = self.run_episode()
        self.assertEqual(result['early_status'], 'missing')
        self.assertEqual(result['end_status'], 'technical_failure')
        self.assertEqual(result['tool_results'], 1)
        self.assertFalse((self.output / 'early_context.json').exists())
        self.assertTrue((self.output / 'actor_final_context.json').exists())
        self.assertEqual(self.probe_payloads, [])

    def test_natural_completion_before_tools_is_valid(self):
        self.actor_choices = [assistant(content='I cannot make this change without the omitted requirement.')]
        result = self.run_episode()
        self.assertEqual(result['early_status'], 'earlier_natural_completion')
        self.assertEqual(result['end_status'], 'natural_completion')
        self.assertEqual(result['tool_results'], 0)
        self.assertEqual(len(self.probe_payloads), 2)

    def test_malformed_actor_tool_arguments_remain_an_ordinary_tool_error(self):
        malformed = assistant(['a'])
        malformed['message']['tool_calls'][0]['function']['arguments'] = '{malformed command'
        self.actor_choices = [malformed, assistant(['b', 'c'])]
        result = self.run_episode()
        self.assertEqual(result['early_status'], 'completed_batch_at_least_three_results')
        self.assertEqual(result['tool_results'], 3)
        self.assertNotEqual(result['end_status'], 'technical_failure')
        context = json.loads((self.output / 'early_context.json').read_text())
        self.assertIn('Invalid tool call', next(m['content'] for m in context if m['role'] == 'tool'))

    def test_checkpoint_failure_preserves_context_and_records_restore_missingness(self):
        self.actor_choices = [assistant(['a', 'b', 'c'])]
        with patch.object(FakeSandbox, 'checkpoint', side_effect=OSError('checkpoint storage unavailable')):
            result = self.run_episode()
        self.assertTrue((self.output / 'early_context.json').exists())
        capture = json.loads((self.output / 'early_capture.json').read_text())
        self.assertEqual(capture['early_workspace_status'], 'failed')
        self.assertEqual(result['end_status'], 'technical_failure')
        self.assertEqual(result['tool_results'], 3)

    def test_incomplete_checkpoint_blocks_continuation_without_discarding_context(self):
        self.allocation['continue_to_end'] = True
        self.actor_choices = [assistant(['a', 'b', 'c']), assistant(content='Should not be issued.')]
        with patch.object(FakeSandbox, 'checkpoint', return_value={'complete': False, 'partial_files': 1}):
            result = self.run_episode()
        self.assertEqual(len(self.actor_payloads), 1)
        self.assertTrue((self.output / 'early_context.json').exists())
        capture = json.loads((self.output / 'early_capture.json').read_text())
        self.assertEqual(capture['early_workspace_status'], 'failed')
        self.assertFalse(capture['early_workspace_checkpoint']['complete'])
        self.assertEqual(result['end_status'], 'technical_failure')

    def test_malformed_probe_response_is_technical_invalid_not_semantic_unknown(self):
        self.probe_content = '{"unexpected": "shape"}'
        result = runner.probe(self.config, [{'role': 'user', 'content': 'original task'}],
                              self.config['questions'][0], 1, self.output / 'probes' / 'invalid')
        self.assertNotEqual(result['technical_status'], 'complete')
        self.assertNotIn('answer', result)
        self.assertNotEqual(result.get('interpretation_status'), 'not_directly_interpretable')
        self.assertEqual(result['message']['content'], self.probe_content)

    def test_valid_semantic_unknown_remains_observed_category(self):
        self.probe_content = json.dumps({'answer': 'insufficient_evidence', 'reason': 'uncertain'})
        result = runner.probe(self.config, [{'role': 'user', 'content': 'original task'}],
                              self.config['questions'][0], 1, self.output / 'probes' / 'unknown')
        self.assertEqual(result['technical_status'], 'complete')
        self.assertEqual(result['answer'], 'insufficient_evidence')


class RenderedContextTests(unittest.TestCase):
    def test_missing_tool_call_arguments_rejected(self):
        messages = [{'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': 'a', 'type': 'function', 'function': {
                'name': 'bash', 'arguments': json.dumps({'command': 'unique_command --unique-argument'})}}]}]
        with self.assertRaises(RuntimeError):
            runner.check_rendered_context({'prompt_text': 'assistant called bash'}, messages)

    def test_missing_original_task_rejected_without_prior_reasoning(self):
        messages = [{'role': 'system', 'content': 'unique system constraint'},
                    {'role': 'user', 'content': 'unique requested behavior'}]
        with self.assertRaises(RuntimeError):
            runner.check_rendered_context({'prompt_text': 'unique system constraint'}, messages)

    def test_missing_tool_result_rejected_even_when_reasoning_is_present(self):
        messages = [{'role': 'assistant', 'content': '', 'reasoning_content': 'preserved reasoning'},
                    {'role': 'tool', 'tool_call_id': 'a', 'content': 'unique filesystem evidence'}]
        with self.assertRaises(RuntimeError):
            runner.check_rendered_context({'prompt_text': 'preserved reasoning'}, messages)

    def test_missing_prior_reasoning_rejected(self):
        messages = [{'role': 'assistant', 'content': 'visible answer', 'reasoning_content': 'private reasoning'}]
        with self.assertRaises(RuntimeError):
            runner.check_rendered_context({'prompt_text': 'visible answer'}, messages)


if __name__ == '__main__':
    unittest.main()
