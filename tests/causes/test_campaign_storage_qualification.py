import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_handoff as h
from causes import campaign_storage_policy as policy
from causes import campaign_monitor as monitor
from causes.campaign_dispatcher import sha


class SealedQualificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.serial = 0
        self.root = self.base / 'old'
        (self.root / '.dispatcher').mkdir(parents=True)
        self.journal = self.root / '.dispatcher/journal.jsonl'
        self.config = self.save({'model': 'fixture', 'endpoint': 'http://127.0.0.1:123/v1/chat/completions'})
        self.provider = {'model': 'fixture', 'port': 123}
        self.route = 'model:' + sha(['fixture', 'http://127.0.0.1:123/v1/chat/completions'])
        self.rows = [{'event': 'route_failure', 'route': self.route, 'consecutive_failures': 1}]
        self.scope = {'output_root': str(self.root), 'baseline': self.write_journal(), 'mode': 'sealed',
                      'recorder_relevant': True, 'model_aliases': [self.route], 'task_routes': ['task:current']}
        self.runner = h.pin(Path(policy.runner.__file__))
        self.source = self.save({'fixture': 'launcher'})
        self.plan = {'config': self.config, 'provider_binding': self.save(self.provider),
                     'runner': self.runner, 'stage': 'early', 'question': {'id': 'evaluation_presence'},
                     'qualification_id': 'new-control', 'seed': 123, 'one_attempt_only': True,
                     'automatic_retry': False, 'at': '2025-12-31T23:59:59+00:00'}
        self.plan_ref = self.save(self.plan)
        self.probe_dir = self.base / 'qualification/probe'
        self.probe_dir.mkdir(parents=True)
        request = {'model': 'fixture', 'seed': 123}
        response_ref = h.save_new(self.probe_dir, 'response.raw', {'fixture': 'response'})
        receipt = {'status': 'complete', 'http_status': 200, 'retry': False,
                   'request_sha256': policy.runner.sha(request), 'response_sha256': response_ref['sha256'],
                   'started_at': '2026-01-01T00:00:01+00:00', 'finished_at': '2026-01-01T00:00:02+00:00'}
        attempt = {k: receipt[k] for k in ('retry', 'request_sha256', 'started_at')}
        artifacts = [response_ref]
        for name, value in [('request.json', request), ('attempt.json', attempt), ('receipt.json', receipt),
                            ('result.json', {'technical_status': 'complete'})]:
            artifacts.append(h.save_new(self.probe_dir, name, value))
        self.result = {'schema': 'causes.storage_route_qualification_result.v1', 'accepted': True,
                       'technical_status': 'complete', 'finish_reason': 'stop', 'context_unchanged': True,
                       'rendered_context_preserved': True, 'model_calls': 1, 'task_actor_calls': 0,
                       'automatic_retry': False, 'semantic_answer_selection': False, 'study_observation': False,
                       'stage': 'early', 'instrument': 'evaluation_presence', 'qualification_id': 'new-control',
                       'seed': 123, 'plan': self.plan_ref, 'config': self.config, 'source': self.source,
                       'runner': self.runner, 'artifacts': artifacts,
                       'started_at': '2026-01-01T00:00:00+00:00', 'finished_at': '2026-01-01T00:00:03+00:00'}
        self.result_ref = h.save_new(self.base / 'qualification', 'RESULT.json', self.result)
        self.allowed = {'schema': 'causes.storage_sealed_model_qualification.v1',
                        'output_root': str(self.root), 'baseline': self.scope['baseline'], 'route': self.route,
                        'consecutive_failures': 1, 'config': self.config, 'provider': self.plan['provider_binding'],
                        'plan': self.plan_ref, 'source': self.source, 'runner': self.runner,
                        'qualification': self.pair(self.result_ref, 'accepted_storage_route_qualification', 'qualification')}
        self.bind_scope()

    def save(self, value):
        self.serial += 1
        return h.save_new(self.base, str(self.serial) + '.json', value)

    def pair(self, reference, flag, key):
        return {'evidence': reference, 'review': self.save({flag: True, key: reference})}

    def write_journal(self):
        for row in self.rows:
            row.setdefault('at', '2025-12-31T23:59:58+00:00')
        self.journal.write_bytes(b''.join(h.encoded(row) + b'\n' for row in self.rows))
        return h.pin(self.journal)

    def bind_scope(self):
        self.scope['sealed_model_qualification'] = self.pair(self.save(self.allowed),
            'accepted_storage_route_scope', 'scope')

    def read(self):
        return policy.read_history(self.scope, config_reference=self.config, provider=self.provider)

    def replace_result(self, **changes):
        self.result.update(changes)
        path = Path(self.result_ref['path'])
        path.write_bytes(h.encoded(self.result) + b'\n')
        self.result_ref = h.pin(path)
        self.allowed['qualification'] = self.pair(self.result_ref,
            'accepted_storage_route_qualification', 'qualification')
        self.bind_scope()

    def test_exact_qualification_preserves_replay_and_global_monitor_blocker(self):
        raw = self.journal.read_bytes()
        result = self.read()
        self.assertEqual(result['replay'], monitor.replay_history(raw))
        self.assertEqual(result['replay']['nonzero_failures'], {self.route: 1})
        self.assertEqual(monitor.history_blockers(result['replay'], [self.route], [], True), [self.route])
        self.assertEqual(monitor.history_blockers(result['replay'], ['model:unrelated'], [], True), [])
        self.assertEqual(self.journal.read_bytes(), raw)
        self.assertEqual(result['sealed_model_qualification'], self.scope['sealed_model_qualification'])

    def test_missing_exception_stays_blocked(self):
        del self.scope['sealed_model_qualification']
        with self.assertRaisesRegex(ValueError, 'requires_requalification'):
            self.read()

    def test_no_current_binding_and_live_prefix_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'current_binding'):
            policy.read_history(self.scope)
        self.scope['mode'] = 'live_prefix'
        with self.assertRaisesRegex(ValueError, 'history_scope'):
            self.read()

    def test_any_journal_append_or_changed_prefix_is_rejected(self):
        self.rows.append({'event': 'controller_started'})
        self.write_journal()
        with self.assertRaisesRegex(ValueError, 'sealed_storage_history_changed'):
            self.read()
        self.rows[0]['consecutive_failures'] = 2
        self.write_journal()
        with self.assertRaisesRegex(ValueError, 'prefix_changed'):
            self.read()

    def test_other_pending_failures_quarantine_and_count_changes_are_rejected(self):
        variants = [
            [{'event': 'route_failure', 'route': self.route, 'consecutive_failures': 2}],
            [{'event': 'route_failure', 'route': self.route, 'consecutive_failures': True}],
            self.rows + [{'event': 'route_failure', 'route': 'task:current', 'consecutive_failures': 1}],
            self.rows + [{'event': 'route_failure', 'route': 'model:other', 'consecutive_failures': 1}],
            self.rows + [{'event': 'route_quarantined', 'route': self.route, 'quarantine_id': 'q'}],
            self.rows + [{'event': 'route_quarantined', 'route': 'recorder:old', 'quarantine_id': 'q'}],
        ]
        for rows in variants:
            with self.subTest(rows=rows):
                self.rows = rows
                self.scope['baseline'] = self.allowed['baseline'] = self.write_journal()
                self.bind_scope()
                with self.assertRaisesRegex(ValueError, 'history_scope|invalid_route_failure_count'):
                    self.read()

    def test_exact_scope_root_route_and_count_cannot_change(self):
        for key, value in [('output_root', str(self.base)), ('route', 'model:other'),
                           ('consecutive_failures', 2), ('consecutive_failures', True)]:
            original = self.allowed[key]
            self.allowed[key] = value
            self.bind_scope()
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'history_scope'):
                self.read()
            self.allowed[key] = original

    def test_current_config_and_provider_drift_are_rejected(self):
        original = self.config
        self.config = self.save({'model': 'changed', 'endpoint': 'changed'})
        with self.assertRaises(ValueError):
            self.read()
        self.config = original
        self.provider = {'model': 'fixture', 'port': 456}
        with self.assertRaisesRegex(ValueError, 'identity_changed'):
            self.read()

    def test_unaccepted_scope_or_actual_qualification_are_rejected(self):
        pair = self.scope['sealed_model_qualification']
        pair['review'] = self.save({'accepted_storage_route_scope': False, 'scope': pair['evidence']})
        with self.assertRaisesRegex(ValueError, 'unaccepted_storage_input'):
            self.read()
        pair = self.allowed['qualification']
        pair['review'] = self.save({'accepted_storage_route_qualification': False, 'qualification': pair['evidence']})
        self.bind_scope()
        with self.assertRaisesRegex(ValueError, 'unaccepted_storage_input'):
            self.read()

    def test_unsuccessful_or_retried_results_are_rejected_even_with_review(self):
        for key, value in [('accepted', False), ('technical_status', 'invalid_answer_schema'),
                           ('finish_reason', 'length'), ('model_calls', 2), ('model_calls', True),
                           ('task_actor_calls', 1), ('context_unchanged', False),
                           ('rendered_context_preserved', False), ('automatic_retry', True),
                           ('semantic_answer_selection', True), ('study_observation', True),
                           ('seed', 456), ('qualification_id', 'different')]:
            original = self.result[key]
            self.replace_result(**{key: value})
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'unsuccessful'):
                self.read()
            self.replace_result(**{key: original})

    def test_source_and_result_byte_drift_are_rejected(self):
        path = Path(self.source['path'])
        raw = path.read_bytes()
        path.write_bytes(raw + b' ')
        with self.assertRaises(ValueError):
            self.read()
        path.write_bytes(raw)
        Path(self.result_ref['path']).write_bytes(b'{}')
        with self.assertRaises(ValueError):
            self.read()

    def test_stale_or_timezone_ambiguous_qualification_is_rejected(self):
        for value in ['2025-12-31T23:59:57+00:00', '2025-12-31T23:59:58+00:00',
                      '2026-01-01T00:01:00+00:00', '2025-12-31T23:59:59']:
            self.plan['at'] = value
            self.plan_ref = self.save(self.plan)
            self.allowed['plan'] = self.plan_ref
            self.replace_result(plan=self.plan_ref)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'not_after_sealed_history'):
                self.read()

    def test_changed_runner_requires_new_qualification(self):
        other = self.save({'fixture': 'different runner'})
        self.plan['runner'] = other
        self.plan_ref = self.save(self.plan)
        self.allowed['plan'] = self.plan_ref
        self.allowed['runner'] = other
        self.replace_result(plan=self.plan_ref, runner=other)
        with self.assertRaisesRegex(ValueError, 'runner_changed'):
            self.read()

    def test_failed_receipt_or_missing_artifact_is_rejected(self):
        ref = next(r for r in self.result['artifacts'] if r['path'].endswith('/receipt.json'))
        path = Path(ref['path'])
        receipt = json.loads(path.read_bytes())
        receipt['http_status'] = 503
        path.write_bytes(h.encoded(receipt) + b'\n')
        artifacts = [h.pin(path) if r is ref else r for r in self.result['artifacts']]
        self.replace_result(artifacts=artifacts)
        with self.assertRaisesRegex(ValueError, 'receipt_changed'):
            self.read()
        self.replace_result(artifacts=artifacts[:-1])
        with self.assertRaisesRegex(ValueError, 'artifacts_changed'):
            self.read()


if __name__ == '__main__':
    unittest.main()
