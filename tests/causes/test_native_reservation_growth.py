"""Synthetic complete census/block proofs for opt-in foreign growth."""
import copy
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_admission as a
from causes import campaign_census as c
from causes import campaign_corpus_succession as s
from causes import campaign_handoff as h
from causes.corpus_continuation import CorpusContinuation
from tests.causes.test_corpus_continuation import fixture
from tests.causes import test_native_reservation_transition as transition


class NativeReservationGrowthTests(unittest.TestCase):
    setUp = transition.NativeReservationTransitionTests.setUp
    save = transition.NativeReservationTransitionTests.save
    operator = transition.NativeReservationTransitionTests.operator

    def block(self, index, *, root=None, rows=None):
        target = (root or self.foreign)/'.dispatcher/blocks'/('%06d.json' % index)
        target.parent.mkdir(parents=True, exist_ok=True)
        if rows is None:
            rows = [dict(run_id='foreign_%d_%d' % (index, k), seed=10001+index*10+k,
                         block_seed=10003+index*10, task_seed=10004+index*10+k,
                         model_seed=10006+index*10) for k in range(2)]
        target.write_bytes(h.encoded(dict(jobs=[dict(allocation=row) for row in rows])))
        return target

    def captures(self, config):
        config_ref = self.capture_config_ref
        result = {}
        for host in config['hosts']:
            value = c.collect_host(host); value['collector_source'] = h.pin(c.__file__)
            value.update(configuration=config_ref,composition_source=h.pin(c.__file__))
            result[host['host']] = dict(value=value, pin=self.save(value, 'capture'))
        return result

    def compose(self,config,frozen,ref):
        self.capture_config_ref=ref;captures=self.captures(config)
        value=c.compose_reservation(config,captures,frozen)
        value.update(configuration=ref,composition_source=h.pin(c.__file__),
                     capture_map=self.save({host:item['pin'] for host,item in captures.items()},'capture-map'))
        return value

    def build(self, *, expanded=False, additional=False):
        data = fixture()
        if expanded:
            for task, count in [('one', 2), ('two', 3)]:
                prototype = next(job for job in data['prototypes'] if job['spec']['task_id'] == task)
                original_cell = next(cell for cell in data['catalog']['physical_cells'] if cell['task_id'] == task)
                for index in range(count, 257):
                    job = copy.deepcopy(prototype); variant = job['variant']
                    variant.update(id='V'+str(index), request='synthetic '+task+str(index), definition_sha256=s.digest([task,index]))
                    variant['stimulus_sha256'] = s.digest({key:variant.get(key) for key in ('request','history','workspace','files','env')})
                    job['allocation']['run_id'] = 'bank_'+task+'_'+str(index)
                    original = copy.deepcopy(job)
                    original['allocation'].update(run_id='original_'+task+'_'+str(index), source_bank83_run_id=job['allocation']['run_id'])
                    cell = copy.deepcopy(original_cell)
                    cell.update(stimulus_sha256=variant['stimulus_sha256'], earliest_accepted_canonical_variant_id=variant['id'])
                    cell['source_aliases'][0].update(variant_id=variant['id'], definition_sha256=variant['definition_sha256'])
                    data['prototypes'].append(job); data['original_jobs'].append(original); data['catalog']['physical_cells'].append(cell)
        refs = {key:self.save(data[key],key) for key in ('catalog','prototypes','original_jobs','config')}
        self.base = self.root/'host'; self.foreign = self.base/'runs/foreign/model'
        self.block(0); self.block(1)
        if additional:self.block(0,root=self.base/'runs/unapproved/model')
        other = self.root/'other'; (other/'runs/sealed/model').mkdir(parents=True)
        limits = dict(max_file_bytes=10000000,max_total_bytes=20000000,max_files=100,max_directory_entries=2000,max_allocations=10000)
        self.baseline_config = dict(schema=c.CONFIG_SCHEMA,collector_sha256=h.pin(c.__file__)['sha256'],
            max_capture_bytes=20000000,max_frozen_bytes=20000000,
            hosts=[dict(host='fixture',base=str(self.base),roots=['runs/foreign/model'],top_jobs=[],control_directories=[],limits=limits),
                   dict(host='other',base=str(other),roots=['runs/sealed/model'],top_jobs=[],control_directories=[],limits=limits)],
            frozen_inputs=[dict(pin=refs[key],selector=[],kind='jobs',expected_count=len(data[key])) for key in ('prototypes','original_jobs')])
        if additional:self.baseline_config['hosts'][0]['roots'].append('runs/unapproved/model')
        self.frozen = [dict(pin=refs[key],value=data[key]) for key in ('prototypes','original_jobs')]
        baseline_ref=self.save(self.baseline_config,'baseline-config')
        self.old = self.compose(self.baseline_config,self.frozen,baseline_ref)
        control = dict(schema='causes.corpus_continuation_control.v2',**refs,reservation=self.save(self.old,'baseline'),
            repetition_start=1,full_selection={'method':'one_cell_per_task_rotating_v1'},
            **{key:data[key] for key in ('family','model_key','task_host','provider_host')})
        control_ref = self.save(control,'control')
        allocator = CorpusContinuation.from_control(control,data['prototypes'],data['config'])
        self.jobs = allocator.block(0)['jobs']; jobs_ref = self.save(self.jobs,'jobs')
        self.allocation = dict(schema='causes.native_corpus_allocation.v1',origin='native_successor',no_fixed_study_pooling=True,
            target_mode='finite_qualification',workload='mixed',workers=4,max_full=1,family=data['family'],model_key=data['model_key'],
            controller_host='fixture',jobs=jobs_ref,config=refs['config'],extensions=self.save([]),allocation_control=control_ref,control=None,
            selection=dict(block_index=0,run_ids=[job['allocation']['run_id'] for job in self.jobs]),output_root=str(self.base/'runs/new/model'))
        self.growth = dict(baseline_census_config=baseline_ref,
                           foreign_roots=[dict(host='fixture',root='runs/foreign/model')])
        self.block(2)
        return self.refresh()

    def refresh(self, *, waiting=False, mutate=None):
        config = copy.deepcopy(self.baseline_config)
        spec = dict(pin=self.allocation['jobs'],selector=[],kind='jobs',expected_count=len(self.jobs))
        if waiting:
            Path(self.allocation['output_root']).mkdir(parents=True,exist_ok=True)
            config['hosts'][0]['roots'].append('runs/new/model')
            spec['allowed_locations'] = [['fixture','runs/new/model']]
        config['frozen_inputs'].append(spec)
        if mutate: mutate(config)
        self.config_ref = self.save(config,'config'); self.current_config = config
        self.current = self.compose(config,self.frozen+[dict(pin=self.allocation['jobs'],value=self.jobs)],self.config_ref)
        return self.current

    def check(self, *, waiting=False):
        return a.native_reservation_foreign_blocks(self.old,self.current,self.allocation,self.growth,self.config_ref,waiting=waiting)

    def test_full_unclaimed_append_preserves_all_514_jobs_generation_and_all_inputs(self):
        self.build(expanded=True)
        before = copy.deepcopy((self.old,self.current,self.allocation,self.growth,self.jobs))
        self.assertEqual((len(self.jobs),sum(job['allocation']['continue_to_end'] for job in self.jobs)),(514,2))
        self.assertTrue(self.check())
        self.assertEqual(len(self.current['run_ids'])-len(self.old['run_ids']),516)
        self.assertEqual((self.old,self.current,self.allocation,self.growth,self.jobs),before)
        self.assertFalse(Path(self.allocation['output_root']).exists())

    def test_existing_modes_still_refuse_foreign_growth(self):
        self.build()
        with self.assertRaisesRegex(ValueError,'native_reservation_blocks_changed'):
            a.native_reservation_augmentation(self.old,self.current,self.allocation)
        with self.assertRaisesRegex(ValueError,'reservation_changed_requires_new_review'):
            a.reservation_unchanged(self.old,self.current)

    def test_both_start_and_release_evidence_use_the_new_proof(self):
        self.build()
        for waiting in (False,True):
            if waiting:self.refresh(waiting=True)
            operator,foundation=self.operator(self.old,self.current,self.allocation,self.config_ref)
            operator.policy.update(reservation_transition='exact_own_and_foreign_blocks_v1',reservation_growth=self.growth,census_hosts=['fixture','other'])
            peer=h.checked(operator.inputs['fresh_reservation']['review'])
            peer.update(accepted_dual_host_capture_custody=True,accepted_native_foreign_block_extension=True,
                        generation_reservation=operator.inputs['precreation_reservation']['evidence'],reservation_growth=self.growth,
                        capture_pins=self.current['capture_pins'])
            operator.inputs['fresh_reservation']['review']=self.save(peer)
            if waiting:
                operator.policy_ref=self.save({'synthetic':'policy'})
                process={'pid':1};barrier=self.save({'synthetic':'barrier'})
                launch=self.save(dict(owned_barrier=barrier,process=process));operator.inputs['launch']=launch
                registration=self.save(dict(controller=dict(process=process,launch_receipt=launch),donor_histories=[]))
                pointer=self.save(dict(registration=registration));monitor=self.save({})
                ready=dict(at=h.now(),policy=operator.policy_ref,launch=launch,owned_barrier=barrier,required_sync_roots=[],
                    evidence_pins=[],registration=registration,authorization_pointer=pointer,monitor_policy=monitor,
                    **{key:True for key in ('zero_initial_claims','zero_persisted_blocks','legacy_owners_healthy','mandatory_companion_valid','snapshot_ownership_valid')})
                ready_ref=self.save(ready);operator.inputs['startup']=dict(evidence=ready_ref,review=self.save(dict(accepted_native_startup_consumers=True,readiness=ready_ref)))
                operator.policy['required_sync_roots']=[]
            with self.subTest(waiting=waiting),patch.object(a,'validate_registration',return_value=True):
                self.assertEqual(operator.evidence(operator.release_action if waiting else operator.start_action),foundation)

    def test_current_reservation_missing_extra_changed_rows_or_seed_union_refuse(self):
        self.build();original=copy.deepcopy(self.current)
        own=next(row for row in original['allocation_identities'] if row['run_id']==self.jobs[0]['allocation']['run_id'])
        for kind in ('missing','extra','changed','duplicate','seed','boolseed'):
            self.current=copy.deepcopy(original)
            if kind=='missing':self.current['allocation_identities'].remove(own)
            elif kind=='extra':self.current['allocation_identities'].append(dict(own,run_id='unproven'))
            elif kind=='changed':self.current['allocation_identities'][0]['seed']=2147483646
            elif kind=='duplicate':self.current['allocation_identities'].append(own)
            elif kind=='seed':self.current['allocation_seeds'].append(2147483646)
            else:self.current['allocation_seeds'][0]=True
            with self.subTest(kind=kind),self.assertRaises(ValueError):self.check()

    def test_old_block_pin_or_projection_mutation_and_removal_refuse(self):
        self.build();file=self.foreign/'.dispatcher/blocks/000000.json';original=file.read_bytes()
        for kind in ('bytes','projection','remove'):
            if kind=='bytes':file.write_bytes(original+b'\n')
            elif kind=='projection':self.block(0,rows=[dict(run_id='rewritten',seed=42,block_seed=43,task_seed=44,model_seed=45)])
            else:file.unlink()
            self.refresh()
            with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,'foreign_prior_block_changed'):self.check()
            file.write_bytes(original)

    def test_block_gap_partial_duplicate_identity_and_duplicate_index_refuse(self):
        self.build();file=self.foreign/'.dispatcher/blocks/000002.json';original=file.read_bytes()
        rows=c.strict_json(original)['jobs']
        for kind in ('gap','partial','duplicate','index'):
            file.write_bytes(original)
            if kind=='gap':file.rename(file.with_name('000003.json'))
            elif kind=='partial':self.block(2,rows=[rows[0]['allocation']])
            elif kind=='duplicate':self.block(2,rows=[rows[0]['allocation']]*2)
            else:file.with_name('2.json').write_bytes(original)
            self.refresh()
            with self.subTest(kind=kind),self.assertRaises(ValueError):self.check()
            for extra in ('000003.json','2.json'):
                path=file.with_name(extra)
                if path.exists():path.unlink()
            file.write_bytes(original)

    def test_own_id_and_each_numeric_seed_collision_refuse(self):
        self.build();row=self.jobs[0]['allocation'];file=self.foreign/'.dispatcher/blocks/000002.json';original=file.read_bytes()
        foreign=[job['allocation'] for job in c.strict_json(original)['jobs']]
        for key in ('run_id','seed','block_seed','task_seed','model_seed'):
            changed=copy.deepcopy(foreign);changed[0][key]=row[key]
            self.block(2,rows=changed)
            try:self.refresh()
            except ValueError as error:
                self.assertEqual(key,'run_id');self.assertIn('allocation_identity_conflict',str(error));continue
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'native_own_allocation_collision'):self.check()
        file.write_bytes(original)

    def test_new_block_cannot_reuse_even_identical_old_id(self):
        self.build();old_row=next(row for row in self.old['allocation_identities'] if row['run_id'].startswith('foreign_'))
        self.block(2,rows=[old_row,dict(old_row,run_id='other_new')]);self.refresh()
        with self.assertRaisesRegex(ValueError,'foreign_added_identity_collision'):self.check()

    def test_duplicate_old_actual_reference_is_allowed_but_unattributed_new_actual_is_not(self):
        self.build();old=next(row for row in self.old['allocation_identities'] if row['run_id'].startswith('foreign_'))
        path=self.foreign/old['run_id'];path.mkdir();(path/'allocation.json').write_bytes(h.encoded(old))
        self.refresh();self.assertTrue(self.check())
        extra=dict(old,run_id='unattributed_actual');path=self.foreign/extra['run_id'];path.mkdir()
        (path/'allocation.json').write_bytes(h.encoded(extra));self.refresh()
        with self.assertRaisesRegex(ValueError,'foreign_unattributed_capture_identity'):self.check()

    def test_unapproved_root_even_same_reserved_rows_refuses(self):
        self.build(additional=True);self.growth['foreign_roots']=[dict(host='other',root='runs/foreign/model')]
        with self.assertRaisesRegex(ValueError,'foreign_growth_root_changed'):self.check()
        self.growth['foreign_roots']=[dict(host='fixture',root='runs/foreign/model')]
        newroot=self.base/'runs/unapproved/model';self.block(1,root=newroot)
        self.refresh()
        with self.assertRaisesRegex(ValueError,'foreign_unreviewed_growth_root'):self.check()

    def test_whole_config_frozen_declarations_and_host_order_are_exact(self):
        self.build();original=copy.deepcopy(self.current_config)
        for kind in ('limit','root','frozen','host','own-location'):
            changed=copy.deepcopy(original)
            if kind=='limit':changed['hosts'][0]['limits']['max_files']+=1
            elif kind=='root':changed['hosts'][0]['roots'].append('runs/extra/model')
            elif kind=='frozen':changed['frozen_inputs'][0]['selector']=['changed']
            elif kind=='host':changed['hosts'].reverse()
            else:changed['frozen_inputs'][-1]['allowed_locations']=[['fixture','runs/foreign/model']]
            self.config_ref=self.save(changed)
            self.current['configuration']=self.config_ref
            with self.subTest(kind=kind),self.assertRaisesRegex(ValueError,'foreign_growth_census_config_changed'):self.check()

    def test_stale_future_naive_or_mutated_cut_and_capture_pin_refuse(self):
        self.build();original=copy.deepcopy(self.current)
        for timestamp in [(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=181)).isoformat(),
                          (dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=60)).isoformat(),'2026-01-01T00:00:00']:
            self.current=copy.deepcopy(original)
            capture=h.checked(self.current['capture_pins'][0]);capture['started_at']=timestamp
            self.current['capture_pins'][0]=self.save(capture)
            self.current['capture_map']=self.save(dict(zip(['fixture','other'],self.current['capture_pins'])))
            self.current['host_cuts'][0]['started_at']=timestamp
            with self.subTest(timestamp=timestamp),self.assertRaises(ValueError):self.check()
        self.current=copy.deepcopy(original)
        self.current['capture_pins'][0]['sha256']='0'*64
        with self.assertRaises(ValueError):self.check()

    def test_block_projection_and_full_inventory_cannot_be_detached_from_reservation(self):
        self.build();original=copy.deepcopy(self.current)
        for key in ('persisted_full_blocks','host_cuts','capture_pins','frozen_input_pins'):
            self.current=copy.deepcopy(original);self.current[key]=self.current[key][:-1]
            with self.subTest(key=key),self.assertRaises(ValueError):self.check()

    def test_generation_rebase_and_changed_jobs_refuse(self):
        self.build();original=copy.deepcopy(self.allocation)
        control=h.checked(original['allocation_control']);control['reservation']=self.save(self.current)
        self.allocation['allocation_control']=self.save(control)
        with self.assertRaisesRegex(ValueError,'native_generation_reservation_changed'):self.check()
        self.allocation=original;changed=copy.deepcopy(self.jobs);changed.reverse();self.allocation['jobs']=self.save(changed)
        with self.assertRaisesRegex(ValueError,'native_finite_selection_changed'):self.check()

    def test_reservation_config_join_and_collector_proof_are_mandatory(self):
        self.build();original=copy.deepcopy(self.current)
        self.current['configuration']=self.growth['baseline_census_config']
        with self.assertRaisesRegex(ValueError,'foreign_reservation_configuration_changed'):self.check()
        self.current=original
        growth=copy.deepcopy(self.growth);self.growth['baseline_census_config']=self.config_ref
        with self.assertRaisesRegex(ValueError,'foreign_reservation_configuration_changed'):self.check()
        self.growth=growth
        operator,_=self.operator(self.old,self.current,self.allocation,self.config_ref)
        operator.policy.update(reservation_transition='exact_own_and_foreign_blocks_v1',reservation_growth=self.growth,census_hosts=['fixture','other'])
        with self.assertRaisesRegex(ValueError,'foreign_capture_proof_not_accepted'):operator.evidence(operator.start_action)

    def test_each_independent_capture_proof_binding_is_mandatory(self):
        self.build();operator,_=self.operator(self.old,self.current,self.allocation,self.config_ref)
        operator.policy.update(reservation_transition='exact_own_and_foreign_blocks_v1',reservation_growth=self.growth,census_hosts=['fixture','other'])
        original=h.checked(operator.inputs['fresh_reservation']['review'])
        original.update(accepted_dual_host_capture_custody=True,accepted_native_foreign_block_extension=True,
            generation_reservation=operator.inputs['precreation_reservation']['evidence'],reservation_growth=self.growth,
            capture_pins=self.current['capture_pins'])
        variants=dict(accepted_dual_host_capture_custody=False,accepted_native_foreign_block_extension=False,
            generation_reservation=self.save(self.old,'equal-byte-other-pin'),
            reservation_growth=dict(self.growth,foreign_roots=[]),capture_pins=list(reversed(self.current['capture_pins'])))
        for key,value in variants.items():
            operator.inputs['fresh_reservation']['review']=self.save(dict(original,**{key:value}))
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'foreign_capture_proof_not_accepted'):
                operator.evidence(operator.start_action)


if __name__ == '__main__':unittest.main()
