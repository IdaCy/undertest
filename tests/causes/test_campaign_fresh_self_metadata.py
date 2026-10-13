import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_fresh_self_metadata as planner


class FreshSelfMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.serial=0
        self.source=self.put({'source':'unchanged'});self.source_peer=self.put({'source':self.source})
        self.known=self.put({'known':'unchanged'})
        self.phase=self.put({'phase':'frozen'})
        self.phase_peer=self.put(dict(accepted_saved_corpus_self_phase_cut=True,actual=self.phase))
        self.member=self.put(dict(kind='supplement_phase',actual=self.phase,review=self.phase_peer,
                                 terminal_run_ids=['supplement'],terminal_measurement_ids=['q1','q2']))
        extra={( 'supplement','self_'+q)for q in planner.SELF[1:]}
        member_doc=planner.load(self.member)
        part=dict(membership=self.member,actual=self.phase,review=self.phase_peer,count=1)
        rows=[dict(measurement_id=mid,run_id='supplement',source_kind='historical_extra_self',
                   custody=dict(membership=self.member,actual=self.phase,review=self.phase_peer)) for mid in ['q1','q2']]
        self.supp_manifest=self.put(dict(parts=[part],rows=rows,new_slot_keys=[list(k)for k in sorted(extra)]))
        self.supp_result=self.put(dict(source=self.source,manifest=self.supp_manifest,new_physical_measurements=2))
        self.supp_peer=self.put(dict(accepted_historical_extra_self_measurement_readout=True,actual=self.supp_result,
                                    source=self.source,manifest=self.supp_manifest,counts=dict(new_physical_measurements=2)))
        accounting=dict(accepted_supplement_result=self.supp_result,accepted_supplement_review=self.supp_peer,
                        supplement_membership=self.member,phase=self.phase,phase_review=self.phase_peer,
                        exact_supplement_run_ids=['supplement'],exact_supplement_slots=[list(k)for k in sorted(extra)],
                        historical_unavailable_run_ids=['unavailable'])
        self.occupied=planner.slots('old')|planner.slots('supplement')|planner.slots('unavailable')
        self.parent=dict(binding_proofs={'mi':self.put(dict(ordered_intersection=['old']))},
                         historical_supplement_accounting=accounting,builder=self.source,builder_review=self.source_peer,
                         importer=self.source,importer_review=self.source_peer,known=self.known)
        self.pairs=[self.pair('mi',['old'],['old']),self.pair('ny',['n'],['n']),
                    self.pair('texas',['t'],['t']),self.pair('moved_ny',['m'],['m']),
                    self.pair('retained_texas',['r'],['r'])]
        sa=planner.load(self.pairs[0]['self']['actual']);ra=planner.load(self.pairs[0]['reader']['actual'])
        reg=planner.load(planner.load(sa['plan'])['registration']);producer=planner.load(reg['producer_plan'])
        self.parent['binding_proofs']['mi']=self.put(dict(ordered_intersection=['old'],
            producer_plan=reg['producer_plan'],producer_plan_review=reg['producer_plan_review'],
            jobs=producer['jobs'],config=producer['config'],runner_source=self.source,
            self_index=sa['index']['saved'],reader_index=ra['index']['saved']))
        self.materialize=dict(source=self.source,source_review=self.source_peer,known=self.known,
                              base_result=None,base_review=None,base_source=None,root_materialization_authorized=False,
                              output=str(self.root/'unused-old-manifest'),parts=[dict(source_kind='fresh',runner_source=self.source,
                              actual=None,review=None,reader_actual=None,reader_review=None)])
        self.readout=dict(source=self.source,source_review=self.source_peer,known=self.known,
                          base_result=None,base_review=None,base_source=None,manifest=None,root_readout_authorized=False,
                          output=str(self.root/'unused-old-readout'))
        self.refresh()

    def put(self,value):
        self.serial+=1;p=self.root/f'{self.serial}.json';p.write_bytes(planner.encoded(value))
        return dict(path=str(p),bytes=p.stat().st_size,sha256=planner.hashlib.sha256(p.read_bytes()).hexdigest())

    def index(self,runs):
        self.serial+=1;p=self.root/f'{self.serial}.jsonl'
        p.write_bytes(b''.join(json.dumps(dict(run='/native/'+r,manifest='/forbidden/'+r+'/MANIFEST.json',files=1)).encode()+b'\n'for r in runs))
        return dict(path=str(p),bytes=p.stat().st_size,sha256=planner.hashlib.sha256(p.read_bytes()).hexdigest())

    def pair(self,name,self_ids,reader_ids):
        producer=self.put(dict(source=self.source,jobs=self.put([]),config=self.put({})))
        producer_peer=self.put(dict(accepted_individual_variant_texas_runtime_plan=True,plan=producer,source=self.source))
        selected=list(dict.fromkeys(self_ids+reader_ids))
        role=name if name in ('mi','ny','texas') else 'texas'
        host='ny' if name == 'moved_ny' else ('mi' if name == 'retained_texas' else role)
        registration=self.put(dict(producer_plan=producer,producer_plan_review=producer_peer,selected_run_ids=selected,
                                   logical_role=role,execution_host=host))
        plan=self.put(dict(source=self.source,registration=registration));pair=dict(id=name,
                 logical_role=role,execution_host=host,
                 manifest_output=str(self.root/(name+'-manifest')),readout_output=str(self.root/(name+'-readout')))
        for profile,ids in [('self',self_ids),('reader',reader_ids)]:
            actual=self.put(dict(plan=plan,index=dict(saved=self.index(ids)),accepted=len(ids),at='2026-10-12T22:00:00+00:00'))
            flag='accepted_individual_variant_self_probe_feed_startup_actual'if profile=='self'else'accepted_individual_variant_reader_feed_index_cut'
            pair[profile]=dict(actual=actual,review=self.put({flag:True,'actual':actual,'findings':[]}))
        return pair

    def refresh(self):
        manifest=self.put(dict(occupied_slot_keys=[list(k)for k in sorted(self.occupied)]))
        result=self.put(dict(manifest=manifest,physical_measurements=len(self.occupied)))
        base_peer=self.put(dict(accepted_incremental_measurement_readout=True,actual=result))
        parent=self.put(self.parent);parent_peer=self.put(dict(accepted_fresh_self_incremental_metadata=True,preparation=parent))
        self.descriptor=dict(schema='causes.fresh_self_metadata_descriptor.v1',output=str(self.root/'metadata'),
                             base_result=result,base_review=base_peer,previous_preparation=parent,previous_review=parent_peer,
                             materialize_template=self.put(self.materialize),readout_template=self.put(self.readout),pairs=self.pairs)
        self.descriptor_ref=self.put(self.descriptor)

    def extra_supplement(self, run='second-supplement'):
        phase=self.put({'phase':run})
        phase_peer=self.put(dict(accepted_saved_corpus_self_phase_cut=True,actual=phase))
        ids=[run+'/strongly',run+'/leans']
        member=self.put(dict(kind='supplement_phase',actual=phase,review=phase_peer,
                             terminal_run_ids=[run],terminal_measurement_ids=ids))
        extra=planner.slots(run)-{(run,'self_'+planner.SELF[0])}
        rows=[dict(measurement_id=mid,run_id=run,source_kind='historical_extra_self',
                   custody=dict(membership=member,actual=phase,review=phase_peer))for mid in ids]
        manifest=self.put(dict(parts=[dict(membership=member,actual=phase,review=phase_peer,count=1)],
                               rows=rows,new_slot_keys=[list(k)for k in sorted(extra)]))
        result=self.put(dict(source=self.source,manifest=manifest,new_physical_measurements=2))
        review=self.put(dict(accepted_historical_extra_self_measurement_readout=True,actual=result,
                             source=self.source,manifest=manifest,counts=dict(new_physical_measurements=2)))
        return dict(accepted_supplement_result=result,accepted_supplement_review=review,
                    supplement_membership=member,phase=phase,phase_review=phase_peer,
                    exact_supplement_run_ids=[run],exact_supplement_slots=[list(k)for k in sorted(extra)],
                    historical_unavailable_run_ids=['unavailable'])

    def two_supplement_descriptor(self):
        second=self.extra_supplement()
        self.occupied.update(planner.slots('second-supplement'));self.refresh()
        entries=[copy.deepcopy(self.parent['historical_supplement_accounting']),second]
        self.descriptor['historical_supplement_accounting']=entries
        return entries

    def test_two_accepted_supplements_cover_the_complete_union(self):
        entries=self.two_supplement_descriptor()
        with patch.object(Path,'mkdir',side_effect=AssertionError('read-only plan wrote')):
            artifacts=planner.plan(self.put(self.descriptor))
        result=artifacts['PREPARATION.v1.json']
        self.assertEqual(result['historical_supplement_accounting'],entries)
        self.assertEqual(result['old_fresh_groups'],1)
        self.assertEqual(result['new_measurement_slots'],12)
        self.assertEqual(result['prospective_total_measurements'],24)

    def test_legacy_single_proof_keeps_its_dictionary_and_counts(self):
        result=planner.plan(self.descriptor_ref)['PREPARATION.v1.json']
        self.assertEqual(result['historical_supplement_accounting'],
                         self.parent['historical_supplement_accounting'])
        self.assertIsInstance(result['historical_supplement_accounting'],dict)
        self.assertEqual(result['prospective_total_measurements'],21)

    def test_inherited_proof_list_needs_no_descriptor_override(self):
        entries=self.two_supplement_descriptor()
        self.parent['historical_supplement_accounting']=entries;self.refresh()
        result=planner.plan(self.descriptor_ref)['PREPARATION.v1.json']
        self.assertEqual(result['historical_supplement_accounting'],entries)

    def test_proof_prefix_cannot_be_removed_reordered_or_changed(self):
        entries=self.two_supplement_descriptor()
        self.parent['historical_supplement_accounting']=entries;self.refresh()
        changed=copy.deepcopy(entries);changed[0]['exact_supplement_run_ids']=['replacement']
        for invalid in [entries[1:],entries[::-1],changed]:
            with self.subTest(invalid=invalid):
                self.descriptor['historical_supplement_accounting']=invalid
                with self.assertRaisesRegex(ValueError,'historical_accounting_prefix'):
                    planner.plan(self.put(self.descriptor))

    def test_empty_or_malformed_proof_list_refuses(self):
        for invalid in [[],[None],None]:
            with self.subTest(invalid=invalid):
                self.descriptor['historical_supplement_accounting']=invalid
                with self.assertRaisesRegex(ValueError,'historical_accounting_schema'):
                    planner.plan(self.put(self.descriptor))

    def test_duplicate_supplement_groups_refuse(self):
        old=self.parent['historical_supplement_accounting']
        self.descriptor['historical_supplement_accounting']=[old,copy.deepcopy(old)]
        with self.assertRaisesRegex(ValueError,'historical_supplement_overlap'):
            planner.plan(self.put(self.descriptor))

    def test_supplements_must_keep_the_same_unavailable_set(self):
        entries=self.two_supplement_descriptor()
        entries[1]['historical_unavailable_run_ids']=['another-unavailable']
        with self.assertRaisesRegex(ValueError,'historical_unavailable_changed'):
            planner.plan(self.put(self.descriptor))

    def test_each_supplement_requires_its_actual_acceptance(self):
        entries=self.two_supplement_descriptor()
        peer=planner.load(entries[1]['accepted_supplement_review'])
        peer['accepted_historical_extra_self_measurement_readout']=False
        entries[1]['accepted_supplement_review']=self.put(peer)
        with self.assertRaisesRegex(ValueError,'accepted_metadata_join'):
            planner.plan(self.put(self.descriptor))

    def test_accepted_supplement_must_already_occupy_base_slots(self):
        second=self.extra_supplement()
        self.descriptor['historical_supplement_accounting']=[
            self.parent['historical_supplement_accounting'],second]
        with self.assertRaisesRegex(ValueError,'supplement_occupied_slots'):
            planner.plan(self.put(self.descriptor))

    def test_five_producers_exact_historical_accounting_and_zero_skip(self):
        with patch.object(Path,'mkdir',side_effect=AssertionError('read-only plan wrote')):
            artifacts=planner.plan(self.descriptor_ref)
        p=artifacts['PREPARATION.v1.json']
        self.assertEqual(p['active_import_order'],['ny','texas','moved_ny','retained_texas'])
        self.assertEqual(p['old_fresh_groups'],1);self.assertEqual(p['new_measurement_slots'],12)
        self.assertEqual(p['prospective_total_measurements'],21)
        self.assertEqual(artifacts['ZERO_DELTA_SKIPS.v1.json']['producers'],['mi'])
        self.assertEqual(artifacts['moved_ny/self.ACTUAL.v1.json']['observed_at'],'2026-10-12T22:00:00+00:00')
        for name in p['active_import_order']:
            q=artifacts[name+'/REQUEST.materialize.pending.v1.json']
            self.assertEqual(len(q['parts']),1);self.assertIsNone(q['base_result'])
            self.assertFalse(q['root_materialization_authorized'])
        self.assertEqual(p['requests']['ny']['previous_readout_required'],self.descriptor['base_result']['path'])

    def test_dated_self_status_keeps_membership_without_startup_claim(self):
        pair = self.pairs[3]
        pair['self']['review'] = self.put(dict(
            accepted_individual_variant_self_probe_feed_index_cut=True,
            actual=pair['self']['actual'], findings=[], new_startup_claimed=False))
        self.refresh()
        result = planner.plan(self.descriptor_ref)
        view = result['moved_ny/self.ACTUAL.v1.json']
        self.assertFalse(view['new_startup_claimed'])
        self.assertEqual(view['accepted'], 1)
        self.assertEqual(result['PREPARATION.v1.json']['new_measurement_slots'], 12)

    def test_dated_self_status_rejects_wrong_peer_identity(self):
        pair = self.pairs[3]
        pair['self']['review'] = self.put(dict(
            accepted_individual_variant_self_probe_feed_index_cut=True,
            actual=self.pairs[4]['self']['actual'], findings=[]))
        self.refresh()
        with self.assertRaisesRegex(ValueError, 'feed_peer_join'):
            planner.plan(self.descriptor_ref)

    def test_exclusive_metadata_write_preserves_no_native_outputs(self):
        result=planner.write(self.descriptor_ref);self.assertTrue(Path(result['path']).is_file())
        self.assertFalse(any(Path(p['manifest_output']).exists()for p in self.pairs))
        with self.assertRaisesRegex(ValueError,'metadata_output_exists'):
            planner.write(self.descriptor_ref)

    def test_mixed_occupied_group_refuses(self):
        self.occupied.add(('n','self_'+planner.SELF[0]));self.refresh()
        with self.assertRaisesRegex(ValueError,'partly_occupied_group'):
            planner.plan(self.descriptor_ref)

    def test_unknown_full_historical_group_refuses(self):
        self.occupied.update(planner.slots('unknown'));self.refresh()
        with self.assertRaisesRegex(ValueError,'complete_prior_group_union'):
            planner.plan(self.descriptor_ref)

    def test_duplicate_group_across_producers_refuses(self):
        self.pairs[-1]=self.pair('retained_texas',['m'],['m']);self.refresh()
        with self.assertRaisesRegex(ValueError,'duplicate_producer_group'):
            planner.plan(self.descriptor_ref)

    def test_mismatched_producer_pair_refuses(self):
        self.pairs[-1]['reader']=self.pair('retained_texas',['different'],['different'])['reader'];self.refresh()
        with self.assertRaisesRegex(ValueError,'producer_pair_identity'):
            planner.plan(self.descriptor_ref)

    def test_reader_only_and_deferred_self_are_not_admitted(self):
        self.pairs[-1]=self.pair('retained_texas',['r','deferred'],['r','reader-only']);self.refresh()
        a=planner.plan(self.descriptor_ref);p=a['retained_texas/INDEX_BINDING.v1.json']
        self.assertEqual(p['ordered_intersection'],['r'])
        self.assertEqual(p['deferred_missing_reader_run_ids'],['deferred'])
        self.assertEqual(p['reader_only_run_ids'],['reader-only'])

    def test_parent_groups_cannot_disappear(self):
        self.pairs[0]=self.pair('mi',['changed'],['changed']);self.refresh()
        with self.assertRaisesRegex(ValueError,'previous_producer_identity'):
            planner.plan(self.descriptor_ref)

    def test_replaced_previous_producer_with_same_ids_refuses(self):
        self.pairs[0]=self.pair('mi',['old'],['old']);self.refresh()
        with self.assertRaisesRegex(ValueError,'previous_producer_identity'):planner.plan(self.descriptor_ref)

    def test_changed_previous_index_receipt_refuses(self):
        actual=planner.load(self.pairs[0]['self']['actual']);rows=planner.saved_bytes(actual['index']['saved'])
        self.serial+=1;p=self.root/f'{self.serial}.jsonl';p.write_bytes(rows.replace(b'"files": 1',b'"files": 2'))
        actual['index']['saved']=dict(path=str(p),bytes=p.stat().st_size,sha256=planner.hashlib.sha256(p.read_bytes()).hexdigest())
        ref=self.put(actual);self.pairs[0]['self']=dict(actual=ref,review=self.put(dict(
            accepted_individual_variant_self_probe_feed_startup_actual=True,actual=ref)))
        self.refresh()
        with self.assertRaisesRegex(ValueError,'previous_index_prefix'):planner.plan(self.descriptor_ref)

    def test_duplicate_index_is_rejected(self):
        a=planner.load(self.pairs[-1]['self']['actual']);a['index']['saved']=self.index(['r','r']);a['accepted']=2
        actual=self.put(a);self.pairs[-1]['self']=dict(actual=actual,review=self.put(dict(
            accepted_individual_variant_self_probe_feed_startup_actual=True,actual=actual)))
        self.refresh()
        with self.assertRaisesRegex(ValueError,'index_membership'):
            planner.plan(self.descriptor_ref)

    def test_changed_pin_refuses_without_native_or_payload_reads(self):
        Path(self.pairs[-1]['reader']['actual']['path']).write_text('{}')
        with self.assertRaisesRegex(ValueError,'pin_changed'):
            planner.plan(self.descriptor_ref)

    def test_payload_name_cannot_be_declared_as_metadata(self):
        p=self.root/'context.json';p.write_text('{}');ref=dict(path=str(p),bytes=2,sha256=planner.hashlib.sha256(b'{}').hexdigest())
        with self.assertRaisesRegex(ValueError,'per_run_read_forbidden'):
            planner.load(ref)

    def test_existing_native_output_refuses_before_any_metadata(self):
        Path(self.pairs[-1]['manifest_output']).mkdir()
        with self.assertRaisesRegex(ValueError,'native_output_collision'):
            planner.write(self.descriptor_ref)
        self.assertFalse((self.root/'metadata').exists())

    def test_execution_host_does_not_relabel_logical_role(self):
        a=planner.plan(self.descriptor_ref)
        p=a['moved_ny/INDEX_BINDING.v1.json']
        self.assertEqual((p['logical_role'],p['execution_host']),('texas','ny'))
        self.pairs[-2]['logical_role']='ny';self.refresh()
        with self.assertRaisesRegex(ValueError,'registered_role_identity'):
            planner.plan(self.descriptor_ref)

    def test_aggregate_cut_preserves_accepted_prefix_and_role(self):
        pair=self.pairs[0];feeds={}
        for profile in ('self','reader'):
            startup=planner.load(pair[profile]['actual'])
            feeds['mi_'+profile]=dict(errors=[],trailing_partial_bytes=0,prior_prefix_retained=True,
                startup_prefix_retained=True,startup=pair[profile]['actual'],startup_review=pair[profile]['review'],
                plan=startup['plan'],index=startup['index']['saved'],index_rows=startup['accepted'],
                observed_at='2026-10-12T23:00:00+00:00')
            if profile=='reader':
                feeds['mi_reader']['startup_review']=self.put(dict(accepted_individual_variant_reader_feed_startup=True,
                                                               actual=pair['reader']['actual']))
        actual=self.put(dict(errors=[],feeds=feeds));review=self.put(dict(accepted_local_copy_progress_cut=True,actual=actual))
        for profile in ('self','reader'):pair[profile]=dict(actual=actual,review=review,entry='mi_'+profile)
        self.refresh();a=planner.plan(self.descriptor_ref)
        self.assertEqual(a['mi/self.ACTUAL.v1.json']['observed_at'],'2026-10-12T23:00:00+00:00')
        self.pairs[0]['execution_host']='ny';self.refresh()
        with self.assertRaisesRegex(ValueError,'aggregate_role_identity'):planner.plan(self.descriptor_ref)

    def test_duplicate_supplement_manifest_row_is_not_a_count_exemption(self):
        m=planner.load(self.supp_manifest);m['rows'].append(m['rows'][0]);manifest=self.put(m)
        r=planner.load(self.supp_result);r['manifest']=manifest;result=self.put(r)
        p=planner.load(self.supp_peer);p.update(actual=result,manifest=manifest);peer=self.put(p)
        self.parent['historical_supplement_accounting'].update(accepted_supplement_result=result,accepted_supplement_review=peer)
        self.refresh()
        with self.assertRaisesRegex(ValueError,'supplement_row_membership'):planner.plan(self.descriptor_ref)


if __name__=='__main__':unittest.main()
