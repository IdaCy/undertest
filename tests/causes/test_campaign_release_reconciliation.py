import ast
import copy
import datetime as dt
import inspect
import json
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from causes import campaign_admission as a
from causes import campaign_handoff as h
from causes import campaign_release_reconciliation as r
from causes import corpus_allocation, corpus_continuation
from tests.causes.test_corpus_continuation import fixture
from tests.causes import test_campaign_admission as native_tests


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def save(self, name, value):
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(h.encoded(value) + b'\n')
        return h.pin(path)

    def prepared(self):
        f = fixture()
        old = copy.deepcopy(f['reservation'])
        old.update(run_ids=sorted(old['run_ids']),
            allocation_identities=sorted([r.identity(j['allocation']) for j in f['prototypes'] + f['original_jobs']], key=lambda v:v['run_id']),
            frozen_input_pins=[{'path':'/fixture/catalog','bytes':1,'sha256':'f'*64}],
            persisted_full_blocks=[{'host':'one','file':{'path':'/fixture/runs/foreign/.dispatcher/blocks/000000000000.json',
                'bytes':12,'sha256':'a'*64},'allocation_count':10}],
            host_cuts=[{'host':host,'started_at':h.now(),'finished_at':h.now()} for host in ('one','two')],capture_pins=[])
        oldref = self.save('original.json', old)
        control = dict(schema='causes.corpus_continuation_control.v1',
            catalog=self.save('catalog.json',f['catalog']),prototypes=self.save('prototypes.json',f['prototypes']),
            original_jobs=self.save('jobs.json',f['original_jobs']),config=self.save('config.json',f['config']),
            reservation=oldref,**{k:f[k] for k in ('family','model_key','task_host','provider_host')},repetition_start=1)
        controlref = self.save('control.json', control)
        binding = corpus_continuation.CorpusContinuation.from_control(control,f['prototypes'],f['config']).binding
        policyref = self.save('policy.json',dict(continuation={'control':controlref,'binding':binding},
            source_sha256=h.pin(a.__file__)['sha256'],pause_paths=[]))
        foreign = {'run_id':'foreign_new','seed':10011,'block_seed':10012,'task_seed':10013,'model_seed':10014}
        block = {'host':'one','file':{'path':'/fixture/runs/foreign/.dispatcher/blocks/000000000001.json',
            'bytes':100,'sha256':'b'*64},'allocation_count':1}
        item = {**block['file'],'kind':'persisted_extension_block','root':'runs/foreign','allocations':[foreign]}
        observed = copy.deepcopy(old)
        observed['allocation_identities'] = sorted(old['allocation_identities']+[foreign], key=lambda row:row['run_id'])
        observed['run_ids'] = sorted(old['run_ids']+[foreign['run_id']])
        observed['allocation_seeds'] = sorted(old['allocation_seeds']+[10011,10012,10013,10014])
        observed['persisted_full_blocks'].append(block)
        observed['configuration'] = self.save('census_config.json',{'hosts':['one','two']})
        observed['composition_source'] = h.pin(a.__file__)
        for host in ('one','two'):
            observed['capture_pins'].append(self.save('capture_'+host+'.json',dict(
                schema='causes.allocation_metadata_census.v1',host=host,reads_actor_or_probe_outcomes=False,
                files=[item] if host=='one' else [])))
        observedref = self.save('observed.json', observed)
        config = dict(schema=r.CONFIG_SCHEMA,adapter_source=h.pin(r.__file__),native_source=h.pin(a.__file__),
            allocator_sources={'causes.corpus_allocation':h.pin(corpus_allocation.__file__),
                               'causes.corpus_continuation':h.pin(corpus_continuation.__file__)},
            policy=policyref,control=controlref,launch={'path':'/fixture/launch.json','bytes':1,'sha256':'c'*64},
            original_reservation=oldref,observed_reservation=observedref,foreign_blocks=[block],
            expected_delta=r.reservation_delta(old,observed,[block])[2],expected_preview_counts={'IDs':5,'positive_seeds':9},
            guarantee=r.GUARANTEE,limitation=r.LIMIT)
        return old, observed, config, self.save('configuration.json', config)

    def accepted_operator(self):
        old, observed, config, configref = self.prepared()
        rec = r.compose(configref)
        recref = self.save('reconciliation.json', rec)
        sourcereview = self.save('source_review.json',dict(accepted_continuous_release_reconciliation_source=True,
            source=config['adapter_source'],configuration=configref))
        recreview = self.save('reconciliation_review.json',dict(accepted_continuous_release_reconciliation=True,
            reconciliation=recref,first_block_preview_replayed=True,foreign_census_entries_reconstructed=True))
        inputs = self.save('inputs.json',dict(launch=config['launch'],policy=config['policy'],
            precreation_reservation={'evidence':config['original_reservation']}))
        now=dt.datetime.now(dt.timezone.utc)
        authority = dict(root_authorized=True,no_user_revocation=True,action=a.RELEASE,
            issued_at=(now-dt.timedelta(seconds=1)).isoformat(),expires_at=(now+dt.timedelta(seconds=120)).isoformat(),
            policy=config['policy'],inputs=inputs,phase_requests_authorized=True,
            release_reconciliation=dict(adapter_source=config['adapter_source'],configuration=configref,source_review=sourcereview,
                reconciliation=recref,reconciliation_review=recreview))
        op = r.ReconciledRelease(config['policy'],inputs,self.save('authority.json',authority),configref,sourcereview,recref,recreview)
        return op, old, observed, config, rec, authority

    def test_pure_composition_preserves_inputs_and_previews_original_first_block(self):
        old, observed, config, ref = self.prepared()
        before = {str(p):p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        result = r.compose(ref)
        self.assertEqual(result, r.compose(ref))
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.base.rglob('*') if p.is_file()})
        self.assertEqual(result['delta']['added_IDs'],1)
        self.assertEqual(result['delta']['added_positive_seeds'],4)
        self.assertEqual(result['preview']['counts'],{'IDs':5,'positive_seeds':9})
        control=h.checked(config['control'])
        block=corpus_continuation.CorpusContinuation.from_control(control,h.checked(control['prototypes']),h.checked(control['config'])).block(0)
        self.assertEqual(result['preview']['block_sha256'],corpus_allocation.digest(block))
        self.assertEqual(result['preview']['binding']['reservation_sha256'],corpus_allocation.digest(old))
        self.assertTrue(r.fresh_matches_reconciliation(old,observed,observed,config,result))

    def test_old_typed_rows_and_exact_foreign_block_scope_cannot_change(self):
        old, observed, config, _ = self.prepared()
        changes = [lambda v:v['allocation_identities'][1].update(seed=1.0),
            lambda v:v['allocation_identities'][1].update(seed=5),
            lambda v:v['allocation_identities'][1].update(run_id='replacement'),
            lambda v:v['allocation_identities'].pop(),
            lambda v:v['persisted_full_blocks'].pop(),
            lambda v:v['persisted_full_blocks'][0]['file'].update(sha256='d'*64),
            lambda v:v['persisted_full_blocks'].append(dict(host='one',file=dict(path='/fixture/block8',bytes=1,sha256='e'*64),allocation_count=1)),
            lambda v:v['frozen_input_pins'][0].update(bytes=1.0)]
        for n, change in enumerate(changes):
            value=copy.deepcopy(observed);change(value)
            with self.subTest(change=n),self.assertRaises(ValueError):r.reservation_delta(old,value,config['foreign_blocks'])

    def test_current_new_union_staleness_or_census_scope_still_refuses(self):
        old, observed, config, ref=self.prepared();rec=r.compose(ref)
        stale=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=181)).isoformat()
        for change in (lambda v:v['host_cuts'][0].update(started_at=stale),
                       lambda v:v['host_cuts'][1].update(finished_at=stale),
                       lambda v:v.update(configuration={'changed':True}),
                       lambda v:v.update(composition_source={'changed':True}),
                       lambda v:(v['allocation_identities'].append(dict(run_id='block8',seed=10101)),
                                 v['run_ids'].append('block8'),v['allocation_seeds'].append(10101))):
            value=copy.deepcopy(observed);change(value)
            with self.assertRaises(ValueError):r.fresh_matches_reconciliation(old,value,observed,config,rec)

    def test_actual_foreign_census_entries_must_explain_each_new_row_and_location(self):
        old, observed, config, _=self.prepared()
        added,seeds,_=r.reservation_delta(old,observed,config['foreign_blocks'])
        for number, change in enumerate((lambda v:v['files'][0].update(allocations=[]),
            lambda v:v['files'][0].update(sha256='d'*64),
            lambda v:v['files'][0]['allocations'][0].update(seed=10011.0),
            lambda v:v['files'].append(dict(path='/fixture/else/allocation.json',kind='actual_allocation',root='runs/else',
                allocations=copy.deepcopy(v['files'][0]['allocations']))))):
            current=copy.deepcopy(observed);capture=h.checked(current['capture_pins'][0]);change(capture)
            current['capture_pins'][0]=self.save('bad_capture'+str(number)+'.json',capture)
            with self.subTest(change=number),self.assertRaises(ValueError):r.foreign_rows_from_captures(current,config['foreign_blocks'],added,seeds)

    def test_preview_refuses_ID_and_each_allocation_seed_domain_collision(self):
        _,_,config,_=self.prepared();policy=h.checked(config['policy']);control=h.checked(config['control'])
        allocator=corpus_continuation.CorpusContinuation.from_control(control,h.checked(control['prototypes']),h.checked(control['config']))
        block=allocator.block(0);first=block['jobs'][0]['allocation']
        with self.assertRaisesRegex(ValueError,'ID_collision'):
            r.preview_first_block(config['control'],policy['continuation']['binding'],{first['run_id']:{}},[])
        for field in ('seed','block_seed','task_seed','model_seed'):
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'seed_collision'):
                r.preview_first_block(config['control'],policy['continuation']['binding'],{},[first[field]])

    def test_native_methods_and_evidence_body_are_inherited_except_declared_seam(self):
        for name in ('static','live','release'):
            self.assertIs(getattr(r.ReconciledRelease,name),getattr(a.ContinuousAdmission,name))
        base=ast.parse(textwrap.dedent(inspect.getsource(a.ContinuousAdmission.evidence))).body[0]
        candidate=ast.parse(textwrap.dedent(inspect.getsource(r.ReconciledRelease.evidence))).body[0]
        candidate.body=candidate.body[2:]
        for index,node in enumerate(candidate.body):
            if isinstance(node,ast.Expr) and isinstance(node.value,ast.Call) and isinstance(node.value.func,ast.Attribute) and node.value.func.attr=='reconciled_reservation':
                candidate.body[index]=ast.parse('reservation_unchanged(old,fresh)').body[0]
        self.assertEqual(ast.dump(base,include_attributes=False),ast.dump(candidate,include_attributes=False))

    def test_authority_rechecks_adapter_bindings_each_time_and_keeps_native_checks(self):
        op,_,_,_,_,authority=self.accepted_operator()
        with patch.object(op,'live',return_value={}) as live:
            auth=op.authority(a.RELEASE)
            auth.check();auth.check()
            self.assertEqual(live.call_count,2)
            # Change independently pinned approval after construction/checks.
            Path(op.source_review_ref['path']).write_text('{}')
            with self.assertRaisesRegex(ValueError,'pin_changed'):auth.check()
            self.assertEqual(live.call_count,2)
        op,_,_,_,_,authority=self.accepted_operator()
        authority['release_reconciliation']['adapter_source']={'wrong':True}
        op.authority_ref=self.save('wrong_adapter_authority.json',authority)
        with patch.object(op,'live',side_effect=AssertionError('must refuse before native live')):
            with self.assertRaisesRegex(ValueError,'authority_reconciliation_changed'):op.authority(a.RELEASE).check()
        authority['release_reconciliation']['adapter_source']=h.checked(op.configuration_ref)['adapter_source']
        authority['inputs']={'wrong':True}
        op.authority_ref=self.save('wrong_native_authority.json',authority)
        with patch.object(op,'live',side_effect=AssertionError('must refuse before native live')):
            with self.assertRaisesRegex(ValueError,'authority_scope_changed'):op.authority(a.RELEASE).check()

    def test_source_pin_reconciliation_review_and_START_are_mandatory(self):
        op,_,_,config,_,_=self.accepted_operator()
        for method in (lambda:op.start(),lambda:op.start(execute=True),lambda:op.authority(a.START),lambda:op.evidence(a.START)):
            with self.assertRaisesRegex(ValueError,'release_only'):method()
        config['adapter_source']=config['native_source']
        with self.assertRaisesRegex(ValueError,'loaded_reconciliation_source_changed'):r.source_bindings(config)
        Path(op.reconciliation_review_ref['path']).write_text('{}')
        with self.assertRaisesRegex(ValueError,'pin_changed'):op.adapter_bindings()

    def test_new_evidence_seam_keeps_original_startup_and_donor_validation(self):
        # Reuse only the existing synthetic evidence fixture; no native/live call.
        fixture_case=native_tests.AdmissionTests();fixture_case.setUp();self.addCleanup(fixture_case.doCleanups)
        native,complete,_=fixture_case.evidence_fixture(a.RELEASE)
        op=r.ReconciledRelease.__new__(r.ReconciledRelease)
        op.policy=native.policy;op.inputs=native.inputs
        with patch.object(op,'adapter_bindings'),patch.object(op,'reconciled_reservation') as check,patch.object(r,'validate_registration') as validate:
            self.assertEqual(op.evidence(a.RELEASE),complete)
            check.assert_called_once();validate.assert_called_once()
        # The unchanged base check still refuses false zero-claim readiness.
        native,_,_=fixture_case.evidence_fixture(a.RELEASE,ready_change=lambda v:v.update(zero_initial_claims=False))
        op.inputs=native.inputs
        with patch.object(op,'adapter_bindings'),patch.object(op,'reconciled_reservation'),self.assertRaisesRegex(ValueError,'startup_gate_missing'):
            op.evidence(a.RELEASE)


if __name__ == '__main__':
    unittest.main()
