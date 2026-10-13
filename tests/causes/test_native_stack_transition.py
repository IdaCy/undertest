"""Explicit archived host custody and current actor/self exclusion boundaries."""
import ast
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from causes import campaign_admission as a, campaign_corpus_succession as s
from causes import campaign_handoff as h, campaign_monitor as m, campaign_release as r
from causes.corpus_continuation import CorpusContinuation
from tests.causes.test_native_release_operator import ReleaseFixture


class TransitionFixture(ReleaseFixture):
    def pair(self, name, value, flag, key):
        evidence = self.save(name + '.json', value)
        return {'evidence': evidence, 'review': self.save(name + '-review.json', {flag: True, key: evidence})}

    def transition(self):
        policy, reg = self.lineage(prefix=True)
        base = h.checked(policy['native_successions']['base_policy'])
        base['controller_host'] = 'archived-host'
        for row in base['history']['donor_scopes']:
            row['host'] = 'archived-host'
        manifest = h.checked(base['host_transfer']['manifest'])
        manifest['target_host'] = 'archived-host'
        base['host_transfer']['manifest'] = self.save('original-transfer.json', manifest)
        base['host_transfer']['independent_review'] = self.save('original-transfer-review.json', {
            'accepted_JP_phase02_host_transfer': True, 'transfer': base['host_transfer']['manifest']})
        policy['host_transfer'] = copy.deepcopy(base['host_transfer'])
        reg['host_transfer'] = copy.deepcopy(base['host_transfer'])
        base_ref = self.save('original-policy.json', base)
        policy['native_successions']['base_policy'] = base_ref
        base_review = h.checked(policy['native_successions']['base_review'])
        original_registration = h.checked(policy['native_successions']['base_registration'])
        original_registration.update(controller_host=base['controller_host'], provider=base['provider'],
                                     controller_config=base['phases'][-1]['controller_config'])
        policy['native_successions']['base_registration'] = self.save('original-registration.json', original_registration)
        base_review['registration'] = policy['native_successions']['base_registration']
        base_review['policy'] = base_ref
        policy['native_successions']['base_review'] = self.save('original-policy-review.json', base_review)
        for row in reg['donor_histories']:
            row['host'] = 'archived-host'
        journal = Path(reg['donor_histories'][0]['journal']['path'])
        journal.write_bytes(journal.read_bytes() + b'{"event":"route_failure","route":"old-route","consecutive_failures":1}\n{"event":"route_success","routes":["old-route"]}\n')
        reg['donor_histories'][0]['journal'] = h.pin(journal)
        archive_donors = copy.deepcopy(reg['donor_histories'])
        closure_row = policy['native_successions']['links'][0]['predecessor']
        closed = h.checked(closure_row['closure']['evidence'])
        refs = [row['journal'] for row in archive_donors]
        refs += [closure_row['launch'], *closure_row['closure'].values()]
        refs += [closed[key] for key in ('never_claimed', 'owned_pause', 'pause_ownership')]
        files = []
        for index, ref in enumerate(refs):
            path = self.root / 'archive' / str(index)
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(h.checked_bytes(ref))
            files.append({'original': ref, 'copy': h.pin(path)})
        archive = self.pair('archive-custody', {
            'schema': 'causes.native_archived_host_custody.v1', 'base_policy': base_ref,
            'base_registration': policy['native_successions']['base_registration'],
            'source_host': 'archived-host', 'phases': base['phases'],
            'source_owners_closed': True, 'future_source_launch_excluded': True,
            'donor_histories': archive_donors, 'files': files, 'last_predecessor': closure_row},
            'accepted_native_archived_host_custody', 'custody')
        stack = self.pair('actor-stack', {
            'schema': 'causes.native_corpus_actor_stack.v1', 'version': 'fixture-v1',
            'workload_kind': 'actor', 'config': base['phases'][-1]['controller_config'],
            'bindings': {key: policy[key] for key in s.STACK_BINDING_KEYS}},
            'accepted_native_corpus_actor_stack', 'stack')
        owners, donors = [], []
        for index, kind in enumerate(('actor', 'self')):
            output = self.root / ('current-' + kind)
            output.mkdir()
            lock = output / '.lock'; lock.touch()
            row = dict(id=kind, kind=kind, host='fixture', output_root=str(output),
                       process=dict(pid=2147483600 + index, process_starttime_ticks='1', argv=['fixture']),
                       lock_paths=[str(lock)])
            raw = dict(schema='causes.native_owner_closure_projection.v1', **row,
                       owner_absent=True, all_locks_free=True, active_claims=0,
                       original_evidence=[self.save(kind+'-original.json', {'original': kind})],
                       original_reviews=[self.save(kind+'-original-review.json', {'reviewed': kind})],
                       attempts_preserved=True, dispositions_preserved=True)
            if kind == 'self':
                raw.update(whole_reservation_complete=True, assigned=19, terminal=19)
            else:
                path = output / '.dispatcher/journal.jsonl'; path.parent.mkdir()
                path.write_bytes(b'{"event":"controller_paused"}\n')
                raw['journal'] = h.pin(path)
                donors.append(dict(host='fixture', output_root=str(output), journal=h.pin(path),
                                   mode='sealed', recorder_relevant=True))
            row['closure'] = self.pair(kind+'-closure', raw, 'accepted_native_owner_closure_projection', 'closure')
            owners.append(row)
        exclusion = self.pair('exclusion', {
            'schema': 'causes.native_current_owner_exclusion.v1', 'host': 'fixture',
            'actor_stack': stack['evidence'], 'roster_complete': True,
            'future_owner_launch_excluded': True, 'owners': owners, 'donor_histories': donors},
            'accepted_native_current_owner_exclusion', 'exclusion')
        failed = self.pair('failed-qualification', {'qualification_accepted': False}, 'accepted_failed_qualification_custody', 'actual')
        failed_review = h.checked(failed['review']); failed_review['qualification_accepted'] = False
        failed['review'] = self.save('failed-qualification-review-final.json', failed_review)
        failed_root = self.root / 'failed-qualification-owner'
        failed_journal = failed_root / '.dispatcher/journal.jsonl'
        failed_journal.parent.mkdir(parents=True)
        events = []
        for index in range(156):
            rid = 'failed-attempt-' + str(index)
            events += [dict(event='claimed', run_id=rid, job_sha256='job-' + str(index)),
                       dict(event='episode_finished', run_id=rid, job_sha256='job-' + str(index),
                            technical_failure=index < 6, replayed=False)]
            events.append(dict(event='route_failure', route='model:failed', consecutive_failures=index + 1)
                          if index < 6 else dict(event='route_success', routes=['task:successful']))
        events += [dict(event='route_quarantined', route=route, quarantine_id='quarantine-' + route)
                   for route in ('model:failed', 'task:failed')]
        events += [dict(event='pause_seen', admission_stopped=True, active=0, pending=358),
                   dict(event='controller_paused', pending=358)]
        failed_journal.write_bytes(b''.join(h.encoded(event) + b'\n' for event in events))
        journal_ref = h.pin(failed_journal)
        failed_donor = dict(host='archived-host', output_root=str(failed_root), journal=journal_ref,
                            mode='sealed', recorder_relevant=True)
        failed_copy = self.root / 'archive/failed-journal'; failed_copy.write_bytes(failed_journal.read_bytes())
        archive_donors.append(failed_donor)
        files.append(dict(original=journal_ref, copy=h.pin(failed_copy)))
        archive = self.mutate(archive, lambda v:v.update(donor_histories=archive_donors, files=files))
        failed_history = self.pair('failed-history', dict(schema='causes.native_failed_qualification_history.v1',
            id='failed-qualification', host='archived-host', original_custody=failed,
            qualification_accepted=False, owner_absent=True, all_locks_free=True,
            attempts_preserved=True, dispositions_preserved=True, output_root=str(failed_root),
            journal=journal_ref, journal_summary=m.replay_history(failed_journal.read_bytes()),
            issued=156, terminal=156, technical_failures=6, never_claimed=358),
            'accepted_native_failed_qualification_history', 'history')
        old_history = dict(id='failed-qualification', host='archived-host', kind='failed_qualification',
                           evidence=failed, history=failed_history)
        history = self.pair('intervening-history', dict(schema='causes.native_intervening_owner_history.v1',
            base_policy=base_ref, source_host='archived-host', target_host='fixture', complete=True,
            entries=[old_history] + [dict(id=row['id'], host='fixture', kind=row['kind'], evidence=row['closure']) for row in owners]),
            'accepted_native_intervening_owner_history', 'history')
        archive = self.mutate(archive, lambda v:v.update(history_ledger=history['evidence'], additional_history=[old_history]))
        exclusion = self.mutate(exclusion, lambda v:v.update(history_ledger=history['evidence']))
        value = dict(schema='causes.native_corpus_stack_transition.v1', no_fixed_study_pooling=True,
                     base_policy=base_ref, base_registration=policy['native_successions']['base_registration'],
                     source_host='archived-host', target_host='fixture', archived_custody=archive,
                     actor_stack=stack, current_exclusion=exclusion, history_ledger=history,
                     old_config=base['phases'][-1]['controller_config'],
                     current_config=base['phases'][-1]['controller_config'],
                     configuration_change={'path': '/dispatcher/disk_floor_bytes', 'before': 0, 'after': 0})
        pair = self.pair('transition', value, 'accepted_native_corpus_stack_transition', 'transition')
        policy['stack_transition'] = reg['stack_transition'] = pair
        reg['prior_local_owners'] = owners
        reg['donor_histories'] += [failed_donor] + donors
        policy['phases'][-1]['donor_histories'] = copy.deepcopy(reg['donor_histories'])
        policy['history']['donor_scopes'] = [{key: row[key] for key in ('host', 'output_root', 'mode', 'recorder_relevant')}
                                           for row in reg['donor_histories']]
        return policy, reg

    def mutate(self, pair, change):
        value = h.checked(pair['evidence']); change(value)
        replacement = self.save(Path(pair['evidence']['path']).name + '-next', value)
        peer = h.checked(pair['review'])
        for key, item in list(peer.items()):
            if item == pair['evidence']: peer[key] = replacement
        return dict(evidence=replacement, review=self.save(Path(pair['review']['path']).name + '-next', peer))

    def eight_phase_archive(self, policy, reg):
        value = h.checked(policy['stack_transition']['evidence'])
        archive = h.checked(value['archived_custody']['evidence'])
        old = copy.deepcopy(h.checked(value['base_policy']))
        old['schema'] = s.POLICY_SCHEMA
        old['native_successions'] = copy.deepcopy(policy['native_successions'])
        old['native_successions']['links'] = []
        prototype = copy.deepcopy(policy['phases'][-1])
        original_allocation = h.checked(policy['native_successions']['links'][0]['allocation']['evidence'])
        previous = archive['last_predecessor']
        native_donors = copy.deepcopy(archive['donor_histories'][:4])
        files = copy.deepcopy(archive['files'])

        def retain(ref):
            if any(item['original'] == ref for item in files):
                return
            destination = self.root / 'archive' / ('v2-' + str(len(files)))
            destination.write_bytes(h.checked_bytes(ref))
            files.append(dict(original=ref, copy=h.pin(destination)))

        for index in range(4):
            name = 'archived-native-' + str(index)
            allocation = copy.deepcopy(original_allocation)
            control = h.checked(allocation['allocation_control'])
            control.update(family=name, task_host='archived-host', provider_host='archived-host')
            control_ref = self.save(name + '-control.json', control)
            allocator = CorpusContinuation.from_control(control, h.checked(control['prototypes']), h.checked(control['config']))
            jobs = allocator.block(0)['jobs']
            allocation.update(family=name, controller_host='archived-host', allocation_control=control_ref,
                              output_root=str(self.root / name), jobs=self.save(name + '-jobs.json', jobs),
                              selection=dict(block_index=0, run_ids=[job['allocation']['run_id'] for job in jobs]))
            allocation_pair = self.pair(name + '-allocation', allocation, 'accepted_native_corpus_allocation', 'allocation')
            phase = copy.deepcopy(prototype)
            phase.update(phase=name, runtime_cohort=name, output_root=allocation['output_root'],
                         controller_jobs=allocation['jobs'], donor_histories=copy.deepcopy(native_donors))
            phase['process']['pid'] += index + 10
            phase['process']['argv'][phase['process']['argv'].index('--output') + 1] = phase['output_root']
            phase['process']['argv'][phase['process']['argv'].index('--jobs') + 1] = allocation['jobs']['path']
            old['native_successions']['links'].append(dict(phase=name, predecessor=previous, allocation=allocation_pair,
                root_adoption=self.save(name + '-adoption.json', dict(root_accepted=True,
                    allocation=allocation_pair['evidence'], allocation_review=allocation_pair['review']))))
            old['phases'].append(phase)
            old['sync_roots'].append(str(Path(phase['output_root']).relative_to(self.root)))
            journal = Path(phase['output_root']) / '.dispatcher/journal.jsonl'
            journal.parent.mkdir(parents=True)
            journal.write_bytes(b'{"event":"controller_paused","pending":0}\n')
            pause = self.save(name + '-pause.json', dict(closed=True))
            launch = self.save(name + '-launch.json', dict(process=phase['process']))
            closed = dict(schema='causes.closed_issued_corpus_history.v1', output_root=phase['output_root'],
                          process=phase['process'], launch=launch, claims=0, finals=0, active_claims=0,
                          ambiguous_ids=[], owner_absent=True, output_lock_free=True, pending_count=0,
                          never_claimed=self.save(name + '-pending.json', []), journal=h.pin(journal),
                          owned_pause=pause, owned_pause_stat=h.stat_identity(pause['path']))
            closed['pause_ownership'] = self.save(name + '-pause-owner.json', dict(process=phase['process'],
                marker=pause, marker_stat=closed['owned_pause_stat']))
            closure = self.pair(name + '-closure', closed, 'accepted_closed_issued_corpus_history', 'closure')
            previous = dict(phase=name, output_root=phase['output_root'], process=phase['process'],
                            launch=launch, pause_path=pause['path'], closure=closure)
            for ref in [launch, *closure.values(), *(closed[key] for key in ('journal', 'never_claimed', 'owned_pause', 'pause_ownership'))]:
                retain(ref)
            native_donors.append(dict(host='archived-host', output_root=phase['output_root'], journal=h.pin(journal),
                                     mode='sealed', recorder_relevant=True))
        old['history']['donor_scopes'] = [{key:row[key] for key in ('host', 'output_root', 'mode', 'recorder_relevant')}
                                         for row in native_donors[:-1]]
        old_reg = dict(phase=old['phases'][-1]['phase'], controller_host='archived-host', provider=old['provider'],
                       controller_config=old['phases'][-1]['controller_config'], donor_histories=native_donors[:-1],
                       controller=dict(workers=old['phases'][-1]['workers'], process=old['phases'][-1]['process']))
        old_ref, old_reg_ref = self.save('eight-phase-policy.json', old), self.save('eight-phase-registration.json', old_reg)
        self.assert_lineage(old, old_reg)
        history = self.mutate(value['history_ledger'], lambda v:v.update(base_policy=old_ref))
        archive_donors = native_donors + archive['donor_histories'][4:]
        archived = self.mutate(value['archived_custody'], lambda v:v.update(base_policy=old_ref, base_registration=old_reg_ref,
            phases=old['phases'], history_ledger=history['evidence'], files=files, last_predecessor=previous,
            donor_histories=archive_donors))
        current = self.mutate(value['current_exclusion'], lambda v:v.update(history_ledger=history['evidence']))
        pair = self.mutate(policy['stack_transition'], lambda v:v.update(base_policy=old_ref, base_registration=old_reg_ref,
            archived_custody=archived, history_ledger=history, current_exclusion=current))
        policy['stack_transition'] = reg['stack_transition'] = pair
        policy['native_successions'].update(base_policy=old_ref, base_registration=old_reg_ref, base_phase_count=8,
            base_review=self.save('eight-phase-base-review.json', dict(accepted_native_succession_base=True,
                policy=old_ref, registration=old_reg_ref)))
        policy['native_successions']['links'][0]['predecessor'] = previous
        reg['donor_histories'] = archive_donors + h.checked(current['evidence'])['donor_histories']
        tail = copy.deepcopy(policy['phases'][-1]); tail['donor_histories'] = copy.deepcopy(reg['donor_histories'])
        policy['phases'] = old['phases'] + [tail]
        policy['sync_roots'] = old['sync_roots'] + [str(Path(tail['output_root']).relative_to(self.root))]
        policy['history']['donor_scopes'] = [{key:row[key] for key in ('host', 'output_root', 'mode', 'recorder_relevant')}
                                            for row in reg['donor_histories']]
        return policy, reg

    @staticmethod
    def assert_lineage(policy, registration):
        s.native_lineage(policy, registration)

    def external_self_transition(self, policy, reg):
        value = h.checked(policy['stack_transition']['evidence'])
        exclusion = h.checked(value['current_exclusion']['evidence'])
        row = copy.deepcopy(next(row for row in exclusion['owners'] if row['kind']=='self'))
        row['host'] = 'execution-host'
        jobs = self.save('external-self-jobs.json', [{'run_id':str(i)} for i in range(19)])
        row['self_plan'] = self.save('external-self-plan.json', dict(host='fixture',
            output_root=row['output_root'], jobs=jobs))
        closed = h.checked(row['closure']['evidence'])
        closed.update(host=row['host'], provider_host='fixture', provider=policy['provider'],
                      self_plan=row['self_plan'], reservation_jobs=jobs)
        row['closure'] = self.pair('external-self-closure', closed,
                                   'accepted_native_owner_closure_projection', 'closure')
        row['execution_custody'] = self.pair('external-self-custody', dict(
            schema='causes.native_external_self_execution_custody.v1', execution_host=row['host'],
            provider_host='fixture', provider=policy['provider'], closure=row['closure'],
            self_plan=row['self_plan'], reservation_jobs=jobs, process=row['process'],
            output_root=row['output_root'], lock_paths=row['lock_paths'], closed_on_execution_host=True,
            future_self_launch_excluded=True, original_execution_evidence=closed['original_evidence'],
            original_execution_reviews=closed['original_reviews']),
            'accepted_native_external_self_execution_custody', 'custody')
        history = self.mutate(value['history_ledger'], lambda ledger:ledger.update(entries=[
            {'id':row['id'], 'host':row['host'], 'kind':'self', 'evidence':row['closure']}
            if item['id']==row['id'] else item for item in ledger['entries']]))
        current = self.mutate(value['current_exclusion'], lambda current:current.update(
            owners=[dict(item, host='fixture') for item in current['owners'] if item['kind']=='actor'],
            external_self_owners=[row], history_ledger=history['evidence']))
        archive = self.mutate(value['archived_custody'], lambda archive:archive.update(history_ledger=history['evidence']))
        pair = self.mutate(policy['stack_transition'], lambda transition:transition.update(
            history_ledger=history, current_exclusion=current, archived_custody=archive))
        policy['stack_transition'] = reg['stack_transition'] = pair
        reg['prior_local_owners'] = h.checked(current['evidence'])['owners']
        return row


class NativeStackTransitionTests(unittest.TestCase):
    def setUp(self):
        self.f = TransitionFixture(self)
        self.policy, self.reg = self.f.transition()

    def test_all_eight_archived_phases_validate_before_new_stack_suffix(self):
        policy, reg = self.f.eight_phase_archive(self.policy, self.reg)
        transition = s.native_stack_transition(policy)
        self.assertEqual(len(transition['base']['phases']), 8)
        self.assertEqual(len(transition['base']['native_successions']['links']), 4)
        self.assertEqual(m.validate_registration(reg, policy)['mode'], 'finite')
        self.assertEqual(r.validate_release_registration(reg, policy,
            allocation_ref=policy['native_successions']['links'][-1]['allocation']['evidence'],
            allocation_cache=s.NativeAllocationCache())['mode'], 'finite')
        admission = dict(schema=s.ADMISSION_SCHEMA, stack_transition=policy['stack_transition'],
                         predecessor_monitor=dict(evidence=policy['native_successions']['base_policy']),
                         predecessors=transition['base']['phases'][:-1] + [transition['archive']['last_predecessor']],
                         **transition['stack']['bindings'])
        self.assertEqual(s.native_local_owners(admission), reg['prior_local_owners'])
        bad = copy.deepcopy(policy); bad['phases'][6]['process']['pid'] += 1
        with self.assertRaises(ValueError): m.validate_registration(reg, bad)

    def test_external_self_custody_never_becomes_a_local_process_probe(self):
        self.policy, self.reg = self.f.eight_phase_archive(self.policy, self.reg)
        external = self.f.external_self_transition(self.policy, self.reg)
        transition = s.native_stack_transition(self.policy)
        self.assertEqual(transition['external_self']['owners'], [external])
        self.assertEqual(m.validate_registration(self.reg, self.policy)['mode'], 'finite')
        self.assertEqual(r.validate_release_registration(self.reg, self.policy,
            allocation_ref=self.policy['native_successions']['links'][-1]['allocation']['evidence'],
            allocation_cache=s.NativeAllocationCache())['mode'], 'finite')
        with patch.object(h,'retirement_state',return_value={'absent':True}) as state, \
                patch.object(h,'lock_free',return_value=True) as locks:
            s.recheck_native_owners(self.policy)
        self.assertEqual([call.args[0] for call in state.call_args_list],
                         [row['process'] for row in self.reg['prior_local_owners']])
        self.assertTrue(set(call.args[0] for call in locks.call_args_list).isdisjoint(map(Path,external['lock_paths'])))
        scope = s.native_remote_transition(self.policy,self.reg)
        self.assertIn(external['closure']['evidence'],scope['external_self_custody_pins'])

    def test_external_self_requires_current_action_exclusion_and_exact_provider(self):
        row = self.f.external_self_transition(self.policy,self.reg)
        transition = s.native_stack_transition(self.policy)
        foundation = dict(stack_transition=self.policy['stack_transition'],
            current_owners=transition['exclusion']['owners'], current_owners_absent_and_unlocked=True,
            archived_source_custody=transition['value']['archived_custody'],
            external_self_owners=[row], external_execution_exclusions_current=True)
        s.native_transition_foundation(self.policy,foundation)
        foundation['external_execution_exclusions_current'] = False
        with self.assertRaisesRegex(ValueError,'fresh_external_self_exclusion'):
            s.native_transition_foundation(self.policy,foundation)
        closed = h.checked(row['closure']['evidence'])
        changed = copy.deepcopy(row)
        changed['closure'] = self.f.pair('external-changed-provider', dict(closed,provider={'pid':999}),
            'accepted_native_owner_closure_projection','closure')
        with self.assertRaisesRegex(ValueError,'external_self_closure_changed'):
            s.external_self_custody({'external_self_owners':[changed], 'owners':transition['exclusion']['owners']},'fixture',self.policy['provider'],
                h.checked,h.checked_bytes,lambda pair,flag,key:s.accepted(pair,flag,key))

    def assert_local_host_refused_without_external_owners(self, missing):
        value = h.checked(self.policy['stack_transition']['evidence'])
        def change(exclusion):
            if missing: exclusion['owners'][0].pop('host')
            else: exclusion['owners'][0]['host']='foreign-host'
        exclusion = self.f.mutate(value['current_exclusion'],change)
        pair = self.f.mutate(self.policy['stack_transition'],lambda v:v.update(current_exclusion=exclusion))
        self.policy['stack_transition'] = self.reg['stack_transition'] = pair
        self.reg['prior_local_owners'] = h.checked(exclusion['evidence'])['owners']
        checks = [lambda:s.native_stack_transition(self.policy),
                  lambda:m.validate_registration(self.reg,self.policy),
                  lambda:r.validate_release_registration(self.reg,self.policy,
                      allocation_ref=self.policy['native_successions']['links'][-1]['allocation']['evidence'],
                      allocation_cache=s.NativeAllocationCache())]
        for check in checks:
            with self.assertRaisesRegex(ValueError,'local_partition_changed'):
                check()

    def test_missing_local_owner_host_refused_without_external_owners(self):
        self.assert_local_host_refused_without_external_owners(missing=True)

    def test_foreign_local_owner_host_refused_without_external_owners(self):
        self.assert_local_host_refused_without_external_owners(missing=False)

    def test_admission_monitor_and_release_resolve_one_contract(self):
        transition = s.native_stack_transition(self.policy)
        self.assertEqual(transition['value']['source_host'], 'archived-host')
        self.assertEqual(m.validate_registration(self.reg, self.policy)['mode'], 'finite')
        self.assertEqual(r.validate_release_registration(self.reg, self.policy,
            allocation_ref=self.policy['native_successions']['links'][-1]['allocation']['evidence'],
            allocation_cache=s.NativeAllocationCache())['mode'], 'finite')
        admission = dict(schema=s.ADMISSION_SCHEMA, stack_transition=self.policy['stack_transition'],
                         predecessor_monitor=dict(evidence=self.policy['native_successions']['base_policy']),
                         predecessors=transition['base']['phases'][:-1] + [transition['archive']['last_predecessor']],
                         **transition['stack']['bindings'])
        self.assertEqual(s.native_stack_transition(admission), transition)
        owner = object.__new__(a.NativeAdmission); owner.policy = admission
        self.assertEqual(owner.local_owner_projection(), self.reg['prior_local_owners'])

    def test_archived_paths_may_be_absent_but_copy_bytes_cannot_change(self):
        transition = s.native_stack_transition(self.policy)
        for row in transition['archive']['files']:
            Path(row['original']['path']).unlink(missing_ok=True)
        m.validate_registration(self.reg, self.policy)
        copy_path = Path(transition['archive']['files'][0]['copy']['path'])
        copy_path.write_bytes(copy_path.read_bytes() + b'changed')
        with self.assertRaises(ValueError): m.validate_registration(self.reg, self.policy)

    def test_current_lock_and_owner_checks_never_touch_archived_pids(self):
        transition = s.native_stack_transition(self.policy)
        with patch.object(h, 'retirement_state', return_value={'absent': True}) as state:
            s.recheck_native_owners(self.policy)
        self.assertEqual([call.args[0] for call in state.call_args_list],
                         [row['process'] for row in transition['exclusion']['owners']])
        lock = Path(transition['exclusion']['owners'][1]['lock_paths'][0])
        with lock.open('rb') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, 'old_owner_live'): s.recheck_native_owners(self.policy)

    def test_source_target_or_registration_mismatch_refuses(self):
        for key, value in [('controller_host', 'other-host'), ('model', 'different-model')]:
            bad = copy.deepcopy(self.policy); bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): s.native_stack_transition(bad)
        bad = copy.deepcopy(self.reg); bad.pop('stack_transition')
        with self.assertRaises(ValueError): m.validate_registration(bad, self.policy)
        bad = copy.deepcopy(self.reg); bad['prior_local_owners'].pop()
        with self.assertRaises(ValueError): m.validate_registration(bad, self.policy)

    def test_full_self_reservation_gate_cannot_be_replaced_by_prefix_acceptance(self):
        value = h.checked(self.policy['stack_transition']['evidence'])
        exclusion = h.checked(value['current_exclusion']['evidence'])
        row = exclusion['owners'][-1]
        row['closure'] = self.f.mutate(row['closure'], lambda v:v.update(terminal=18))
        ledger = h.checked(value['history_ledger']['evidence'])
        ledger['entries'][-1]['evidence'] = row['closure']
        history = self.f.mutate(value['history_ledger'], lambda v:v.update(entries=ledger['entries']))
        archive = self.f.mutate(value['archived_custody'], lambda v:v.update(history_ledger=history['evidence']))
        current = self.f.mutate(value['current_exclusion'], lambda v:v.update(owners=exclusion['owners'], history_ledger=history['evidence']))
        self.policy['stack_transition'] = self.f.mutate(self.policy['stack_transition'],
            lambda v:v.update(current_exclusion=current, archived_custody=archive, history_ledger=history))
        with self.assertRaisesRegex(ValueError, 'self_reservation_incomplete'): s.native_stack_transition(self.policy)

    def test_failed_qualification_cannot_be_reclassified(self):
        value = h.checked(self.policy['stack_transition']['evidence'])
        ledger = h.checked(value['history_ledger']['evidence'])
        old = ledger['entries'][0]
        peer = h.checked(old['evidence']['review']); peer['qualification_accepted'] = True
        old['evidence']['review'] = self.f.save('falsely-qualified-peer.json', peer)
        history = self.f.mutate(value['history_ledger'], lambda v:v.update(entries=ledger['entries']))
        archive = self.f.mutate(value['archived_custody'], lambda v:v.update(history_ledger=history['evidence'], additional_history=[old]))
        current = self.f.mutate(value['current_exclusion'], lambda v:v.update(history_ledger=history['evidence']))
        self.policy['stack_transition'] = self.f.mutate(self.policy['stack_transition'],
            lambda v:v.update(current_exclusion=current, archived_custody=archive, history_ledger=history))
        with self.assertRaisesRegex(ValueError, 'failed_qualification_reclassified'): s.native_stack_transition(self.policy)

    def test_unsupported_source_host_actor_cannot_silently_lose_its_journal(self):
        value = h.checked(self.policy['stack_transition']['evidence'])
        ledger = h.checked(value['history_ledger']['evidence'])
        actor = copy.deepcopy(ledger['entries'][1]); actor.update(id='another-old-actor', host='archived-host')
        actor['evidence'] = self.f.mutate(actor['evidence'], lambda v:v.update(id=actor['id'], host=actor['host']))
        ledger['entries'].insert(1, actor)
        history = self.f.mutate(value['history_ledger'], lambda v:v.update(entries=ledger['entries']))
        archive = self.f.mutate(value['archived_custody'], lambda v:v.update(
            history_ledger=history['evidence'], additional_history=ledger['entries'][:2]))
        current = self.f.mutate(value['current_exclusion'], lambda v:v.update(history_ledger=history['evidence']))
        self.policy['stack_transition'] = self.f.mutate(self.policy['stack_transition'],
            lambda v:v.update(current_exclusion=current, archived_custody=archive, history_ledger=history))
        with self.assertRaisesRegex(ValueError, 'unsupported_archived_owner_kind'):
            s.native_stack_transition(self.policy)

    def test_failed_qualification_counts_and_full_replay_cannot_be_cleared(self):
        for key, replacement in [('technical_failures', 0), ('issued', 155), ('never_claimed', 0),
                                 ('journal_summary', {})]:
            with self.subTest(key=key):
                fixture = TransitionFixture(self)
                policy, _ = fixture.transition()
                value = h.checked(policy['stack_transition']['evidence'])
                ledger = h.checked(value['history_ledger']['evidence'])
                row = ledger['entries'][0]
                row['history'] = fixture.mutate(row['history'], lambda v:v.update({key:replacement}))
                history = fixture.mutate(value['history_ledger'], lambda v:v.update(entries=ledger['entries']))
                archive = fixture.mutate(value['archived_custody'], lambda v:v.update(
                    history_ledger=history['evidence'], additional_history=[row]))
                current = fixture.mutate(value['current_exclusion'], lambda v:v.update(history_ledger=history['evidence']))
                policy['stack_transition'] = fixture.mutate(policy['stack_transition'],
                    lambda v:v.update(current_exclusion=current, archived_custody=archive, history_ledger=history))
                with self.assertRaisesRegex(ValueError, 'failed_qualification_(attempt_accounting|replay)_changed'):
                    s.native_stack_transition(policy)

    def test_exact_disk_floor_increase_allowed_but_other_config_change_refuses(self):
        value = h.checked(self.policy['stack_transition']['evidence'])
        config = h.checked(value['current_config']); config['dispatcher']['disk_floor_bytes'] = 112 * 2**30
        new_ref = self.f.save('current-config.json', config)
        stack = self.f.mutate(value['actor_stack'], lambda v:v.update(config=new_ref))
        exclusion = self.f.mutate(value['current_exclusion'], lambda v:v.update(actor_stack=stack['evidence']))
        pair = self.f.mutate(self.policy['stack_transition'], lambda v:v.update(current_config=new_ref,
            actor_stack=stack, current_exclusion=exclusion,
            configuration_change={'path':'/dispatcher/disk_floor_bytes','before':0,'after':112*2**30}))
        self.policy['stack_transition'] = pair
        s.native_stack_transition(self.policy)
        config['endpoint'] = 'http://wrong:5'
        bad_ref = self.f.save('wrong-config.json', config)
        value = h.checked(pair['evidence'])
        stack = self.f.mutate(value['actor_stack'], lambda v:v.update(config=bad_ref))
        self.policy['stack_transition'] = self.f.mutate(pair, lambda v:v.update(current_config=bad_ref, actor_stack=stack))
        with self.assertRaisesRegex(ValueError, 'undeclared_configuration_change'): s.native_stack_transition(self.policy)

    def test_archive_and_current_actor_histories_cannot_be_dropped(self):
        for host in ('archived-host', 'fixture'):
            bad = copy.deepcopy(self.reg)
            bad['donor_histories'] = [row for row in bad['donor_histories'] if row['host'] != host]
            with self.subTest(host=host), self.assertRaises(ValueError): m.validate_registration(bad, self.policy)
        value = h.checked(self.policy['stack_transition']['evidence'])
        value['archived_custody'] = self.f.mutate(value['archived_custody'], lambda v:v['phases'].pop())
        self.policy['stack_transition'] = self.f.mutate(self.policy['stack_transition'], lambda v:v.update(archived_custody=value['archived_custody']))
        with self.assertRaisesRegex(ValueError, 'archived_host_custody_changed'): s.native_stack_transition(self.policy)

    def test_fresh_foundation_requires_current_owner_scope(self):
        transition = s.native_stack_transition(self.policy)
        good = dict(stack_transition=self.policy['stack_transition'],
                    current_owners=transition['exclusion']['owners'], current_owners_absent_and_unlocked=True,
                    archived_source_custody=transition['value']['archived_custody'])
        s.native_transition_foundation(self.policy, good)
        for key in good:
            bad = copy.deepcopy(good); bad[key] = None
            with self.subTest(key=key), self.assertRaises(ValueError): s.native_transition_foundation(self.policy, bad)

    def test_actual_remote_owner_checks_skip_only_archived_host_custody(self):
        scope = s.native_remote_transition(self.policy, self.reg)
        for row in scope['files']:
            Path(row['original']['path']).unlink(missing_ok=True)
        tree = ast.parse(m.REMOTE)
        branch = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                      and ast.unparse(node.test) == "side == 'jp'"
                      and any(isinstance(child, ast.For) and ast.unparse(child.iter) == "x['prior_local_owners']" for child in node.body))
        index = next(i for i, node in enumerate(branch.body) if isinstance(node, ast.For)
                     and ast.unparse(node.iter) == "x['prior_local_owners']")
        code = compile(ast.Module(body=branch.body[index:index + 2], type_ignores=[]), '<real remote owners>', 'exec')
        env = dict(policy=self.policy, x=self.reg, transition=scope, Path=Path,
                   fcntl=fcntl, json=json, pincheck=h.checked_bytes)
        exec(code, env)
        lock = Path(self.reg['prior_local_owners'][-1]['lock_paths'][0])
        with lock.open('rb') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError): exec(code, env)

    def test_actual_remote_donor_replay_retains_archived_failure_history(self):
        scope = s.native_remote_transition(self.policy, self.reg)
        for row in scope['files']:
            Path(row['original']['path']).unlink(missing_ok=True)
        tree = ast.parse(m.REMOTE)
        helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                   and node.name in ('replay_history', 'history_blockers', 'history_relevance')]
        loop = next(node for node in tree.body if isinstance(node, ast.For)
                    and isinstance(node.target, ast.Name) and node.target.id == 'donor')
        env = dict(policy=self.policy, x=self.reg, req={'native_transition': scope},
                   side='jp', current_host='fixture', histories=[], Path=Path,
                   hashlib=hashlib, json=json)
        exec(compile(ast.Module(body=helpers + [loop], type_ignores=[]), '<real remote donors>', 'exec'), env)
        self.assertEqual(len(env['histories']), len(self.reg['donor_histories']))
        self.assertEqual(env['histories'][0]['journal']['lifetime_route_failures'], 1)
        self.assertEqual(env['histories'][0]['journal']['nonzero_failures'], {})
        self.assertEqual(env['histories'][0]['root'], scope['archived_donors'][0]['output_root'])
        failed = env['histories'][len(scope['archived_phases'])]['journal']
        self.assertEqual(failed['event_counts']['claimed'], 156)
        self.assertEqual(failed['event_counts']['episode_finished'], 156)
        self.assertEqual(failed['lifetime_route_failures'], 6)
        self.assertEqual(failed['nonzero_failures'], {'model:failed': 6})
        self.assertEqual(len(failed['quarantines']), 2)

    def test_later_native_admission_keeps_archived_prefix_and_current_native_owner(self):
        transition = s.native_stack_transition(self.policy)
        admission = dict(schema=s.ADMISSION_SCHEMA, stack_transition=self.policy['stack_transition'],
                         predecessor_monitor=dict(evidence=self.f.save('current-monitor.json', self.policy)),
                         predecessors=transition['base']['phases'][:-1] + [transition['archive']['last_predecessor'], self.policy['phases'][-1]],
                         **transition['stack']['bindings'])
        resolved = s.native_stack_transition(admission)
        self.assertEqual(s.native_local_owners(admission, resolved),
                         transition['exclusion']['owners'] + [self.policy['phases'][-1]])

    def test_retirement_preserves_archived_closure_and_checks_current_owners(self):
        transition = s.native_stack_transition(self.policy)
        policy = dict(schema=s.ADMISSION_SCHEMA, stack_transition=self.policy['stack_transition'],
                      predecessor_monitor=dict(evidence=self.policy['native_successions']['base_policy']),
                      predecessors=transition['base']['phases'][:-1] + [transition['archive']['last_predecessor']],
                      **transition['stack']['bindings'])
        operator = object.__new__(a.NativeAdmission); operator.policy = policy
        class Authority:
            def check(self): pass
        attempt = self.f.root / 'retirement'; attempt.mkdir()
        with patch.object(h, 'retirement_state', return_value={'absent': True}) as state:
            result = operator.retire_predecessor(attempt, Authority())
        self.assertEqual(len(state.call_args_list), len(transition['exclusion']['owners']))
        actual = h.checked(result)
        self.assertEqual(actual['closure'], transition['archive']['last_predecessor']['closure'])
        self.assertTrue(actual['no_signal_or_restart'])


if __name__ == '__main__':
    unittest.main()
