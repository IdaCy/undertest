import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from causes import campaign_supplement as core
from causes import campaign_supplement_closed_scope as scope
from causes import campaign_supplement_derived_bank as bank
from causes import campaign_supplement_closed_audit as audit
from causes import campaign_derived_probe_runtime as bridge
from test_campaign_supplement_derived_bank import Fixture


class ClosedFixture(Fixture):
    def closed_peer(self, value):
        actual = self.save(value)
        return dict(actual=actual, review=self.save(dict(
            accepted_finite_supplement_closed_custody=True, actual=actual, findings=[],
            original_denominator_preserved=True, all_original_attempts_retained=True,
            qualification_findings=value['qualification_findings'])))

    def closure(self, *args, **kwargs):
        value, _ = super().closure(*args, **kwargs)
        value['terminal'] = len(value['complete_ids'])
        return value, self.closed_peer(value)


class DerivedClosedScopeTests(unittest.TestCase):
    def setup_scope(self, limit=None):
        self.f = ClosedFixture()
        plan, previous, jobs, adoption = self.f.build()
        plan.update(maximum_admission_attempts=84, maximum_captures=21, maximum_capture_uses=8)
        if limit is not None:
            plan.update(maximum_new_requests=limit, jobs_sha256=core.digest(jobs[:limit]))
        return plan, previous, jobs, adoption

    def resolve(self, plan, previous):
        return scope.resolve(plan, previous, derived_bank_jobs=bank.resolve, read=self.f.read)

    def test_initial_transition_preserves_whole_bank_and_missing_shared_controls(self):
        plan, previous, jobs, _ = self.setup_scope(3)
        found = self.resolve(plan, previous)
        self.assertEqual(found['jobs'], jobs[:3])
        self.assertEqual(found['full_jobs'], self.f.owner_jobs + jobs)
        self.assertEqual(found['derived_bank']['unique_missing_slots'], 3)
        self.assertEqual([r['shared_jobs'] for r in found['extension']['source_counts']], [0, 3, 3])
        ref = self.f.save(found['full_jobs'])
        extension = scope.bind_extension(found, ref)
        self.assertEqual(extension['schema'], scope.DERIVED_EXTENSION_SCHEMA)
        self.assertEqual(extension['adoption'], plan['derived_bank'])
        self.assertEqual(extension['appended_count'], 21)
        self.assertEqual(extension['cumulative_count'], 22)
        self.assertEqual(extension['full_jobs'], ref)
        self.assertTrue(extension['prior_prefix_unchanged'])

    def test_partial_closure_keeps_unresolved_occupied_and_exact_rest(self):
        plan, previous, jobs, _ = self.setup_scope(3)
        found = self.resolve(plan, previous)
        mids = [j['measurement_id'] for j in jobs]
        cut = scope.partition(found, mids[:2], mids[:1])
        self.assertEqual(cut['occupied'], 3)
        self.assertEqual(cut['terminal'], 2)
        self.assertEqual(cut['unresolved_claimed_ids'], mids[1:2])
        self.assertEqual(cut['unstarted_ids'], mids[2:])

    def resumed(self):
        plan, previous, jobs, _ = self.setup_scope()
        found = self.resolve(plan, previous)
        full = self.f.save(found['full_jobs'])
        extension = scope.bind_extension(found, full)
        part = scope.partition(found, [j['measurement_id'] for j in jobs[:4]],
                               [j['measurement_id'] for j in jobs[:4]])
        current = dict(previous, **{k:part[k] for k in ('complete_ids','unstarted_ids','terminal')},
                       full_jobs=full, derived_bank_extension=extension, consecutive_failures=2)
        current_descriptor = self.f.closed_peer(current)
        resumed = dict(plan, predecessor_custody=current_descriptor,
            occupied_ids=current['complete_ids'], initial_failures=2,
            continuation_scope='derived_bank_remaining', output_root='/synthetic-derived-bank/resume',
            maximum_new_requests=len(jobs)-4, jobs_sha256=core.digest(jobs[4:]))
        return resumed, current, jobs, extension

    def test_resume_reuses_exact_full_jobs_and_typed_extension_without_reset(self):
        plan, previous, jobs, extension = self.resumed()
        found = self.resolve(plan, previous)
        self.assertEqual(found['jobs'], jobs[4:])
        self.assertIsNone(scope.bind_extension(found, previous['full_jobs']))
        self.assertEqual(found['carried_derived_extension'], extension)
        self.assertEqual(plan['initial_failures'], 2)

    def test_resume_rejects_relabeled_extension_even_with_matching_peer(self):
        for key, value in [('unique_missing_slots',0), ('appended_count',20),
                           ('schema','causes.original_supplement_owner_bank_extension.v1')]:
            plan, previous, _, _ = self.resumed()
            previous['derived_bank_extension'][key] = value
            plan['predecessor_custody'] = self.f.closed_peer(previous)
            with self.subTest(key=key), self.assertRaisesRegex(core.IntegrityError,'extension_changed'):
                self.resolve(plan, previous)

    def test_derived_family_without_explicit_bank_cannot_fall_into_original_path(self):
        plan, previous, _, _ = self.setup_scope()
        del plan['derived_bank']
        with self.assertRaisesRegex(core.IntegrityError, 'explicit_derived_bank_required'):
            self.resolve(plan, previous)

    def test_explicit_resolver_ambiguous_transfer_and_failure_reset_refuse(self):
        plan, previous, _, _ = self.setup_scope()
        with self.assertRaisesRegex(core.IntegrityError, 'derived_resolver_required'):
            scope.resolve(plan, previous, read=self.f.read)
        for extra in ({'original_reservation_transfer':{}}, {'initial_failures':0}):
            with self.subTest(extra=extra), self.assertRaises(core.IntegrityError):
                self.resolve(dict(plan, **extra), previous)

    def test_resume_old_full_bank_or_claim_replay_refuses(self):
        plan, previous, jobs, _ = self.resumed()
        for changed in (dict(plan, occupied_ids=[]), dict(plan, jobs_sha256=core.digest(jobs))):
            with self.assertRaises(core.IntegrityError): self.resolve(changed, previous)


class DerivedAuditDispatchTests(unittest.TestCase):
    """Synthetic pinned collector tests cover dispatch, never real model data."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.sequence = 0
        self.job = dict(measurement_id='derived_probe_fixture/model/id/early/evaluation_presence')
        self.row = dict(jobs=[self.job], view={'synthetic': True})
        self.config = dict(endpoint='http://fixture.invalid', sampling={'temperature':0.6})
        self.protocol = dict(config=self.save(self.config), sources={'campaign_supplement':audit.pin(core.__file__)})
        self.execution = copy.deepcopy(self.protocol)
        self.execution['config'] = self.save(dict(self.config, endpoint='http://execution.invalid'))
        self.adapter_file = self.root/'adapter.py'; self.adapter_file.write_text('# synthetic adapter\n')
        self.adapter = types.SimpleNamespace(supplement=core,
            _protocol=lambda p:(json.loads(audit.raw(p['config'])), 'same-frozen-identity'))
        self.collector = types.SimpleNamespace(core=core)
        source = self.root/'collector.py'
        source.write_text('from causes import campaign_supplement_collect as collector\n'
            'from causes import derived_probe_inputs as derived\n'
            'def verify_final(job, terminal, *, root, row, protocol, record):\n'
            '    if job not in row["jobs"]: raise ValueError("changed_row_job")\n'
            '    return dict(label=terminal["label"], synthetic_only=True, '
            'measurement_id=job["measurement_id"], protocol=protocol, row=row)\n')
        native = self.root/'native_collector.py';native.write_text('# synthetic native collector\n')
        self.recipe = dict(derived_collector=audit.pin(source), native_collector=audit.pin(native))
        self.scientific = dict(campaign_supplement=audit.pin(core.__file__),
                               derived_probe_inputs=audit.pin(self.adapter_file))
        self.peer = dict(accepted_derived_probe_collect_source=True, findings=[],
            source=self.recipe['derived_collector'], collector=self.recipe['native_collector'],
            input_adapter=self.scientific['derived_probe_inputs'], modules={
                'causes.campaign_supplement_collect':self.recipe['native_collector'],
                'causes.derived_probe_collect':self.recipe['derived_collector']})
        self.recipe['derived_collector_review'] = self.save(self.peer)
        self.plan = dict(derived_bank={'synthetic':True}, execution_protocol=self.save(self.execution),
            sources={'native_queue':audit.pin(core.__file__)}, endpoint='http://execution.invalid',
            output_root=str(self.root/'output'))
        self.scope = dict(derived_bank=dict(protocol=self.protocol,
            row_by_job={self.job['measurement_id']:self.row}))
        self.mods = dict(core=core,adapter=self.adapter,bridge=bridge,
                         scientific_sources=self.scientific,constructor={})
        self.recorded = []
        import causes
        self.package = causes
        previous = {key:getattr(causes,key,None) for key in ('campaign_supplement_collect','derived_probe_inputs')}
        previous_modules = {key:sys.modules.get(key) for key in ('causes.derived_probe_inputs','causes.derived_probe_collect')}
        def restore():
            for key,value in previous.items():
                if value is None:
                    if hasattr(causes,key): delattr(causes,key)
                else: setattr(causes,key,value)
            for key,value in previous_modules.items():
                if value is None: sys.modules.pop(key,None)
                else: sys.modules[key]=value
        self.addCleanup(restore)

    def save(self, value):
        self.sequence += 1; p = self.root/f'{self.sequence}.json';p.write_bytes(core.encoded(value))
        return audit.pin(p)

    def record(self, ref, read=False):
        b = audit.raw(ref);self.recorded.append(ref)
        return b if read else ref

    def verifier(self):
        return audit.derived_final_verifier(self.recipe,self.plan,self.scope,self.mods,self.collector,self.record)

    def test_exact_typed_dispatch_preserves_missing_and_frozen_rows(self):
        old = copy.deepcopy(self.row)
        verify = self.verifier()
        with patch.object(core, 'probe_measurement', side_effect=AssertionError('no request')):
            result = verify(self.job, {'label':None})
        self.assertIsNone(result['label'])
        self.assertEqual(result['row'],old)
        self.assertEqual(result['protocol'],self.execution)
        self.assertEqual(self.row,old)
        self.assertIn(self.recipe['derived_collector_review'],self.recorded)

    def test_wrong_or_missing_collector_peer_refuses(self):
        for change in ({'accepted_derived_probe_collect_source':False}, {'findings':['unresolved']},
                       {'input_adapter':self.recipe['native_collector']}):
            self.recipe['derived_collector_review'] = self.save(dict(self.peer, **change))
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'source_peer'):
                self.verifier()

    def test_endpoint_only_execution_configuration_is_allowed(self):
        self.assertEqual(self.verifier()(self.job,{'label':'yes'})['protocol'],self.execution)
        changed = copy.deepcopy(self.execution)
        changed['config'] = self.save(dict(self.config, sampling={'temperature':1}))
        self.plan['execution_protocol'] = self.save(changed)
        with self.assertRaisesRegex(core.IntegrityError,'semantic_protocol_changed'): self.verifier()

    def test_wrong_endpoint_and_missing_explicit_scope_refuse(self):
        self.plan['endpoint'] = 'http://other.invalid'
        with self.assertRaisesRegex(ValueError,'derived_closed_endpoint'):self.verifier()
        self.plan.pop('derived_bank')
        with self.assertRaisesRegex(ValueError,'explicit_derived_final_scope'):self.verifier()

    def test_collector_mutation_after_binding_refuses_before_verification(self):
        verify=self.verifier()
        Path(self.recipe['derived_collector']['path']).write_text('# changed\n')
        with self.assertRaisesRegex(ValueError,'pin_mismatch'):verify(self.job,{'label':'yes'})

    def test_scientific_mutation_during_verification_is_detected_on_exit(self):
        verify=self.verifier()
        loaded=sys.modules['causes.derived_probe_collect']
        original=loaded.verify_final
        def changing(*args,**kwargs):
            result=original(*args,**kwargs);self.adapter_file.write_text('# changed\n');return result
        loaded.verify_final=changing
        with self.assertRaisesRegex(ValueError,'pin_mismatch'):verify(self.job,{'label':'yes'})

    def test_unknown_or_altered_job_is_not_sent_to_original_verifier(self):
        verify=self.verifier()
        with self.assertRaisesRegex(ValueError,'unknown_job'):
            verify({'measurement_id':'other'}, {'label':'yes'})
        with self.assertRaisesRegex(ValueError,'changed_row_job'):
            verify(dict(self.job,seed=999), {'label':'yes'})


if __name__ == '__main__': unittest.main()
