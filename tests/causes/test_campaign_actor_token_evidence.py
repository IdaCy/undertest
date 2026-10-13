import copy
import hashlib
import json
import unittest

from causes import campaign_actor_token_evidence as audit
from causes import campaign_runner as runner


class ActorTokenEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.config = {'model': 'fixture-model', 'sampling': {
            'temperature': 1.0, 'max_tokens': 32768, 'return_prompt_text': True,
            'return_token_ids': True, 'include_reasoning': True, 'reasoning_effort': 'xhigh',
            'chat_template_kwargs': {'enable_thinking': True, 'preserve_thinking': True,
                                     'reasoning_effort': 'xhigh'}},
            'tools': [{'type': 'function', 'function': {'name': 'read', 'parameters': {}}},
                      {'type': 'function', 'function': {'name': 'bash', 'parameters': {}}}]}
        messages = [{'role': 'user', 'content': 'Inspect the saved file.'},
                    {'role': 'assistant', 'reasoning': 'Read first.', 'content': None,
                     'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {
                         'name': 'read', 'arguments': '{"path":"file.txt"}'}}]},
                    {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'saved tool result'}]
        self.request = runner.request(self.config, messages, 123, self.config['tools'])
        self.response = {'model': 'fixture-model', 'system_fingerprint': 'fixture',
                         'prompt_text': 'native prompt with combining e\u0301',
                         'prompt_token_ids': [10, 20, 30],
                         'usage': {'prompt_tokens': 3, 'completion_tokens': 1, 'total_tokens': 4}}
        self.attempt = {'request_sha256': runner.sha(self.request), 'retry': False,
                        'started_at': '2026-01-01T00:00:00+00:00'}
        self.receipt = dict(self.attempt, status='complete', http_status=200,
                            **{key: copy.deepcopy(self.response[key])
                               for key in ('model', 'system_fingerprint', 'usage')})
        self.native = {'request_body_sha256': runner.sha(self.request),
                       'prompt_text': self.response['prompt_text'],
                       'prompt_token_ids': list(self.response['prompt_token_ids']),
                       'request_before': dict(copy.deepcopy(self.request), skip_special_tokens=True),
                       'request_after': dict(copy.deepcopy(self.request), skip_special_tokens=False)}
        self.expected_request = runner.sha(self.request)
        self.expected_response = hashlib.sha256(runner.encoded(self.response)).hexdigest()
        self.receipt['response_sha256'] = self.expected_response

    def check(self, *, request_file=None, response_file=None, maximum_model_len=262144):
        return audit.compare_saved_actor_prompt(
            runner.encoded(self.request) + b'\n' if request_file is None else request_file,
            runner.encoded(self.attempt) + b'\n', runner.encoded(self.receipt) + b'\n',
            runner.encoded(self.response) if response_file is None else response_file,
            config=self.config, native_render=self.native, maximum_model_len=maximum_model_len,
            expected_request_sha256=self.expected_request,
            expected_response_sha256=self.expected_response)

    def rebind_response(self):
        self.expected_response = hashlib.sha256(runner.encoded(self.response)).hexdigest()
        self.receipt['response_sha256'] = self.expected_response
        self.receipt['usage'] = copy.deepcopy(self.response.get('usage'))

    def rebind_request(self):
        self.expected_request = runner.sha(self.request)
        self.attempt['request_sha256'] = self.receipt['request_sha256'] = self.expected_request
        self.native['request_body_sha256'] = self.expected_request

    def test_full_comparison_returns_only_opaque_evidence(self):
        before = copy.deepcopy((self.request, self.response, self.native))
        result = self.check()
        self.assertEqual(result['prompt_tokens'], 3)
        self.assertEqual(result['prompt_usage_status'], 'present_equal')
        self.assertEqual(result['output_allowance'], 32768)
        self.assertEqual(result['adjusted_request_fields'], ['skip_special_tokens'])
        self.assertTrue(result['complete_prompt_text_equal'])
        self.assertTrue(result['complete_prompt_token_ids_equal'])
        self.assertEqual((self.request, self.response, self.native), before)
        self.assertNotIn('native prompt', json.dumps(result))
        self.assertNotIn('saved tool result', json.dumps(result))
        self.assertNotIn('prompt_token_ids', result)

    def test_same_length_token_mutation_refuses(self):
        self.native['prompt_token_ids'][1] = 21
        with self.assertRaisesRegex(ValueError, 'prompt_token_mismatch'):
            self.check()

    def test_unicode_normalization_is_not_silently_accepted(self):
        self.native['prompt_text'] = 'native prompt with combining é'
        with self.assertRaisesRegex(ValueError, 'prompt_text_mismatch'):
            self.check()

    def test_missing_empty_bool_negative_or_noninteger_tokens_refuse(self):
        for tokens in (None, [], [True], [-1], [1.0], ['1']):
            for side in ('native', 'response'):
                with self.subTest(tokens=tokens, side=side):
                    self.setUp()
                    target = self.native if side == 'native' else self.response
                    target['prompt_token_ids'] = tokens
                    if side == 'response':
                        self.rebind_response()
                    with self.assertRaisesRegex(ValueError, 'token_ids_required'):
                        self.check()

    def test_missing_prompt_text_or_native_wire_binding_refuses(self):
        for key, value in (('prompt_text', None), ('request_body_sha256', 'other-request')):
            with self.subTest(key=key):
                self.setUp()
                self.native[key] = value
                with self.assertRaises(ValueError):
                    self.check()

    def test_response_custody_checks_original_bytes(self):
        with self.assertRaisesRegex(ValueError, 'custody_changed'):
            self.check(response_file=runner.encoded(self.response) + b'\n')
        self.expected_response = 'invented-roster-hash'
        with self.assertRaisesRegex(ValueError, 'custody_changed'):
            self.check()

    def test_native_request_file_has_exact_final_newline(self):
        for raw in (runner.encoded(self.request), runner.encoded(self.request) + b'\n\n',
                    json.dumps(self.request).encode() + b'\n'):
            with self.subTest(raw=raw[:30]), self.assertRaisesRegex(ValueError, 'request_file_changed'):
                self.check(request_file=raw)

    def test_changed_actor_settings_refuse_even_with_rebound_hashes(self):
        changes = [('tool_choice', 'none'), ('parallel_tool_calls', False), ('max_tokens', 8192),
                   ('stream', True), ('n', 2), ('seed', True), ('model', 'other-model'),
                   ('tools', list(reversed(self.config['tools'])))]
        for key, value in changes:
            with self.subTest(key=key):
                self.setUp()
                self.request[key] = value
                self.rebind_request()
                with self.assertRaisesRegex(ValueError, 'frozen_actor_request_changed'):
                    self.check()

    def test_failed_or_retried_calls_are_not_successful_qualification(self):
        for change in ({'retry': True}, {'status': 'transport_or_response_failure'},
                       {'http_status': 500}, {'started_at': 'different'}):
            with self.subTest(change=change):
                self.setUp()
                self.receipt.update(change)
                with self.assertRaisesRegex(ValueError, 'successful_nonretry'):
                    self.check()

    def test_json_distinct_request_types_refuse_with_rebound_hashes(self):
        for key, value in (('n', True), ('stream', 0), ('parallel_tool_calls', 1),
                           ('max_tokens', 32768.0)):
            with self.subTest(key=key):
                self.setUp()
                self.request[key] = value
                self.rebind_request()
                with self.assertRaisesRegex(ValueError, 'frozen_actor_request_changed'):
                    self.check()

    def test_usage_and_context_limit_are_checked(self):
        with self.assertRaisesRegex(ValueError, 'context_limit'):
            self.check(maximum_model_len=32770)
        self.response['usage']['prompt_tokens'] = 4
        self.rebind_response()
        with self.assertRaisesRegex(ValueError, 'prompt_usage_mismatch'):
            self.check()

    def test_native_adjustment_evidence_is_required(self):
        self.native['request_after'] = None
        with self.assertRaisesRegex(ValueError, 'adjustment_evidence_required'):
            self.check()

    def test_missing_usage_is_reported_without_inventing_counts(self):
        for usage in (None, {}, {'completion_tokens': 1}):
            with self.subTest(usage=usage):
                self.setUp()
                self.response['usage'] = usage
                self.rebind_response()
                result = self.check()
                self.assertEqual(result['prompt_usage_status'], 'missing')
                self.assertEqual(result['prompt_tokens'], 3)
        self.response.pop('usage')
        self.rebind_response()
        self.assertEqual(self.check()['prompt_usage_status'], 'missing')
        self.response['usage'] = []
        self.rebind_response()
        with self.assertRaisesRegex(ValueError, 'usage_object_required'):
            self.check()

    def test_native_adjustment_records_null_field_addition(self):
        self.native['request_after']['native_new_default'] = None
        result = self.check()
        self.assertEqual(result['adjusted_request_fields'],
                         ['native_new_default', 'skip_special_tokens'])
        self.assertNotEqual(result['request_before_sha256'], result['request_after_sha256'])

    def test_native_adjustment_records_json_distinct_value_types(self):
        self.native['request_after']['n'] = True
        result = self.check()
        self.assertEqual(result['adjusted_request_fields'], ['n', 'skip_special_tokens'])
        self.assertNotEqual(result['request_before_sha256'], result['request_after_sha256'])


if __name__ == '__main__':
    unittest.main()
