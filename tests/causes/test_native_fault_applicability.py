import ast
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_admission as a, campaign_corpus_succession as s
from causes import campaign_handoff as h, campaign_monitor as m


class ArchivedFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.serial = 0
        self.config = {'model': 'fixture-model', 'endpoint': 'http://127.0.0.1:18083/v1/chat/completions'}
        self.config_ref = self.save(self.config)
        self.route = 'model:' + s.digest([self.config['model'], self.config['endpoint']])
        self.old_provider = {'pid': 123, 'process_starttime_ticks': '1', 'argv': ['old-service']}
        self.new_provider = {'pid': 456, 'process_starttime_ticks': '2', 'argv': ['current-service']}
        events = []
        for index in range(156):
            events += [dict(event='claimed', run_id=f'old-{index}', job_sha256=f'job-{index}',
                            config_sha256=s.digest(self.config)),
                       dict(event='episode_finished', run_id=f'old-{index}', technical_failure=index < 6)]
            if index < 6:
                events.append(dict(event='route_failure', route=self.route, consecutive_failures=index % 3 + 1))
            if index in (2, 5):
                events.append(dict(event='route_quarantined', route=self.route, quarantine_id=f'q-{index}'))
        events.append(dict(event='controller_paused', pending=358))
        self.journal = self.raw(b''.join(h.encoded(row) + b'\n' for row in events), 'failed/.dispatcher/journal.jsonl')
        self.copy = self.raw(h.checked_bytes(self.journal), 'archive/journal.jsonl')
        self.summary = m.replay_history(h.checked_bytes(self.journal))
        self.donor = dict(host='old-host', output_root=str(self.root/'failed'), journal=self.journal,
                          mode='sealed', recorder_relevant=True)
        self.failed = dict(id='failed-qualification', host='old-host', output_root=self.donor['output_root'],
                           journal=self.journal, journal_summary=self.summary,
                           original_custody=self.pair({'qualification_accepted': False}, 'accepted_failed_custody', 'actual'))
        self.owner_policy = self.save(dict(schema=s.ADMISSION_SCHEMA, controller_host='old-host',
            output_root=self.donor['output_root'], resident_config=self.config_ref, provider=self.old_provider))
        self.attribution = dict(schema='causes.native_archived_actor_provider_binding.v1', donor=self.donor,
            original_custody=self.failed['original_custody'], failed_history_id=self.failed['id'],
            owner_policy=self.owner_policy, config=self.config_ref, provider=self.old_provider)
        self.sources = [self.raw(b'frozen native source', 'native.py')]
        jobs = [dict(allocation=dict(run_id=f'new-{i}', continue_to_end=False)) for i in range(8)]
        artifact = self.save({'saved_fixture_only': True})
        self.qualification = dict(schema='causes.native_actor_protocol_qualification.v1', host='new-host',
            provider=self.new_provider, config=self.config_ref, native_actor_sources=self.sources,
            workload_kind='prefix_actor_tool', workers=4, max_full=0, assigned=8, terminal=8,
            technical_failures=0, replayed=False, owner_absent=True, all_locks_free=True,
            whole_self_closed_before_bootstrap=True, jobs=self.save(jobs), journal=artifact,
            owner_closure=artifact, guard_evidence=artifact, saved_prefix_tool_evidence=artifact,
            saved_prefix_tool_review=artifact, current_token_custody=artifact,
            complete_native_tool_exchanges=1, observed_tool_coverage={
                'read': dict(observed_calls=1, verified_executions=1, complete_exchanges=1),
                'bash': dict(observed_calls=0, verified_executions=0, complete_exchanges=0)})
        value = dict(source_host='old-host', target_host='new-host', base_policy=artifact,
                     archived_custody={'evidence': artifact}, history_ledger={'evidence': artifact},
                     actor_stack={'evidence': artifact}, current_exclusion={'evidence': artifact})
        self.transition = dict(value=value, stack=dict(bindings={'provider': self.new_provider},
            config=self.config_ref, native_actor_sources=self.sources), archive=dict(
                donor_histories=[self.donor], files=[dict(original=self.journal, copy=self.copy)]))
        self.decision = dict(schema='causes.native_archived_model_fault_applicability.v1', **value,
            current_actor_qualification=None, rows=[dict(donor=self.donor, model_routes=[self.route],
                provider_binding=None, retained_journal_summary=self.summary)])
        self.policy = dict(workers=4, max_full=0, resident_config=self.config_ref,
            history=dict(model_aliases=[self.route], task_routes=['task:blocked'], donor_scopes=[{
                key:self.donor[key] for key in ('host','output_root','mode','recorder_relevant')}]))

    def save(self, value):
        self.serial += 1
        return self.raw(h.encoded(value), f'item-{self.serial}.json')

    def raw(self, raw, name):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return h.pin(path)

    def pair(self, value, flag, key):
        evidence = self.save(value)
        return dict(evidence=evidence, review=self.save({flag:True, key:evidence}))

    def validate(self):
        self.decision['current_actor_qualification'] = self.pair(self.qualification,
            'accepted_native_actor_protocol_qualification', 'qualification')
        self.decision['rows'][0]['provider_binding'] = self.pair(self.attribution,
            'accepted_native_archived_actor_provider_binding', 'binding')
        self.transition['value']['fault_applicability'] = self.pair(self.decision,
            'accepted_native_archived_model_fault_applicability', 'applicability')
        return s.native_fault_applicability(self.transition, [self.failed], self.policy)

    def test_exact_archived_route_classification_preserves_every_raw_counter(self):
        projection = self.validate()
        before = copy.deepcopy(self.summary)
        result = m.history_relevance(self.summary, [self.route], [], True, self.donor, projection)
        self.assertEqual(result['blocked_routes'], [])
        self.assertEqual(result['original_blocked_routes'], [self.route])
        self.assertEqual(result['nonapplicable_archived_model_routes'], [self.route])
        self.assertEqual(self.summary, before)
        self.assertEqual(self.summary['lifetime_route_failures'], 6)
        self.assertEqual(self.summary['event_counts']['route_quarantined'], 2)
        self.assertEqual(self.summary['nonzero_failures'], {self.route:3})
        self.assertEqual(self.summary['quarantines'], {self.route:'q-5'})
        self.assertEqual(self.qualification['observed_tool_coverage']['bash']['complete_exchanges'], 0)

    def test_same_alias_on_another_donor_or_current_controller_still_blocks(self):
        projection = self.validate()
        for field, changed in [('host','new-host'), ('output_root','/another/donor'),
                               ('journal',dict(self.journal, sha256='different'))]:
            with self.subTest(field=field):
                donor = {**self.donor, field:changed}
                result = m.history_relevance(self.summary, [self.route], [], True, donor, projection)
                self.assertEqual(result['blocked_routes'], [self.route])
                self.assertIsNone(result['applicability'])
        self.assertEqual(m.history_blockers(self.summary, [self.route], [], True), [self.route])

    def test_task_recorder_unknown_and_unresolved_blockers_are_not_exempted(self):
        projection = self.validate()
        replay = copy.deepcopy(self.summary)
        replay['nonzero_failures'].update({'task:blocked':1, 'recorder:disk':1})
        result = m.history_relevance(replay, [self.route], ['task:blocked'], True, self.donor, projection)
        self.assertEqual(result['blocked_routes'], ['recorder:disk','task:blocked'])
        replay['nonzero_failures']['unknown:route'] = 1
        with self.assertRaisesRegex(ValueError, 'unrecognized_history_route'):
            m.history_relevance(replay, [self.route], ['task:blocked'], True, self.donor, projection)
        raw = h.checked_bytes(self.journal) + b'{"event":"unresolved_task"}\n'
        capture = self.raw(raw, 'unresolved-capture')
        current = dict(self.donor, mode='live_prefix', at=dt.datetime.now(dt.timezone.utc).isoformat(),
                       journal=dict(capture,path=self.journal['path']), capture=capture)
        baseline = dict(self.donor, mode='live_prefix')
        history = copy.deepcopy(self.policy['history']); history['donor_scopes'][0]['mode']='live_prefix'
        with self.assertRaisesRegex(ValueError, 'requires_reconciliation'):
            a.donor_cuts_clear(history, [baseline], [current], [])

    def test_native_saved_gate_uses_exact_projection_and_default_remains_blocked(self):
        projection = self.validate()
        current = dict(self.donor, at=dt.datetime.now(dt.timezone.utc).isoformat(), capture=self.copy)
        with self.assertRaisesRegex(ValueError, 'requires_reconciliation'):
            a.donor_cuts_clear(self.policy['history'], [self.donor], [current])
        self.assertTrue(a.donor_cuts_clear(self.policy['history'], [self.donor], [current], projection))
        self.assertEqual(m.replay_history(h.checked_bytes(self.copy)), self.summary)

    def test_provider_restart_host_config_and_native_source_changes_refuse(self):
        original = copy.deepcopy(self.qualification)
        changes = [dict(provider={**self.new_provider,'process_starttime_ticks':'restarted'}),
                   dict(host='different-host'), dict(config=self.save({'different': True})),
                   dict(native_actor_sources=[self.raw(b'different source','other.py')])]
        for change in changes:
            with self.subTest(change=change):
                self.qualification = {**original, **change}
                with self.assertRaisesRegex(ValueError, 'current_actor_qualification_changed'):
                    self.validate()

    def test_self_evidence_failed_or_unclosed_actor_cannot_qualify(self):
        original = copy.deepcopy(self.qualification)
        for change in [dict(workload_kind='self'), dict(technical_failures=1), dict(replayed=True),
                       dict(owner_absent=False), dict(all_locks_free=False),
                       dict(whole_self_closed_before_bootstrap=False), dict(terminal=7)]:
            with self.subTest(change=change):
                self.qualification = {**original, **change}
                with self.assertRaisesRegex(ValueError, 'current_actor_qualification_changed'):
                    self.validate()

    def test_no_exchange_or_promoted_unobserved_tool_refuses(self):
        original = copy.deepcopy(self.qualification)
        self.qualification['complete_native_tool_exchanges'] = 0
        self.qualification['observed_tool_coverage']['read']['complete_exchanges'] = 0
        with self.assertRaisesRegex(ValueError, 'tool_exchange_not_qualified'):
            self.validate()
        self.qualification = original
        self.qualification['observed_tool_coverage']['bash']['complete_exchanges'] = 1
        self.qualification['complete_native_tool_exchanges'] = 2
        with self.assertRaisesRegex(ValueError, 'tool_exchange_not_qualified'):
            self.validate()

    def test_qualification_cannot_authorize_full_or_higher_concurrency(self):
        for workers, max_full in ((5,0),(4,1)):
            with self.subTest(workers=workers,max_full=max_full):
                self.policy.update(workers=workers,max_full=max_full)
                with self.assertRaisesRegex(ValueError, 'scope_exceeded'):
                    self.validate()

    def test_original_provider_attribution_and_claim_config_must_match(self):
        original = copy.deepcopy(self.attribution)
        for change in [dict(provider=self.new_provider), dict(config=self.save({'wrong':True})),
                       dict(failed_history_id='another-history')]:
            with self.subTest(change=change):
                self.attribution = {**original,**change}
                with self.assertRaisesRegex(ValueError, 'attribution_changed|provider_policy_changed'):
                    self.validate()
        self.attribution = original
        raw = h.checked_bytes(self.copy).replace(s.digest(self.config).encode(), b'0'*64)
        new_ref = self.raw(raw,'changed-journal')
        self.transition['archive']['files'][0]['copy'] = new_ref
        with self.assertRaisesRegex(ValueError, 'claim_provider_config_changed'):
            self.validate()

    def test_unsupported_donor_and_changed_replay_refuse(self):
        self.decision['rows'][0]['retained_journal_summary'] = {**self.summary,'lifetime_route_failures':0}
        with self.assertRaisesRegex(ValueError, 'replay_changed'):
            self.validate()
        self.decision['rows'][0]['retained_journal_summary'] = self.summary
        self.transition['archive']['donor_histories'] = []
        with self.assertRaisesRegex(ValueError, 'not_failed_archived_donor'):
            self.validate()

    def test_remote_and_local_relevance_functions_are_identical(self):
        local = ast.parse(Path(m.__file__).read_text())
        remote = ast.parse(m.REMOTE)
        for name in ('replay_history','history_blockers','history_relevance'):
            functions = [next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name==name)
                         for tree in (local,remote)]
            self.assertEqual(ast.dump(functions[0],include_attributes=False),
                             ast.dump(functions[1],include_attributes=False))
        self.assertIn("'journal':summary",m.REMOTE)
        self.assertIn("item['route_applicability'] = relevance",m.REMOTE)


if __name__ == '__main__':
    unittest.main()
