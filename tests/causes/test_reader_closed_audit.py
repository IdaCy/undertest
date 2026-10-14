import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

SOURCE=Path(__file__).resolve().parents[2]/'src/causes/campaign_reader_closed_audit.py'
spec=importlib.util.spec_from_file_location('closed_audit_test_target',SOURCE)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class ClosedAuditBoundaries(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  p=self.root/'saved.json';p.write_text('{}\n');self.ref=m.pin(p)
  self.d=dict(schema='causes.saved_reader_closed_audit_descriptor.v1',root=str(self.root),actual=self.ref,plan=self.ref,prior_progress=None,direct_binding=self.ref,capture_execution=self.ref,capture_source=self.ref,reader_source=self.ref,prepared=self.ref,startup_actual_path=str(self.root/'startup.json'),output=str(self.root/'peer.json'))
 def test_exact_descriptor_accepts(self):m.validate_descriptor(self.d)
 def test_extra_field_refuses(self):
  self.d['authority']=True
  with self.assertRaisesRegex(AssertionError,'descriptor_fields'):m.validate_descriptor(self.d)
 def test_pin_change_refuses(self):
  Path(self.ref['path']).write_text('changed')
  with self.assertRaises(AssertionError):m.validate_descriptor(self.d)
 def test_output_cannot_overwrite_input(self):
  self.d['output']=self.ref['path']
  with self.assertRaisesRegex(AssertionError,'output_overwrites_input'):m.validate_descriptor(self.d)
 def test_ambiguous_custody_modes_refuse(self):
  self.d['prior_progress']=self.ref
  with self.assertRaisesRegex(AssertionError,'one_prior_custody_mode'):m.validate_descriptor(self.d)
 def history(self):
  def r(x):return {'path':x,'bytes':1,'sha256':'x'}
  p0=r('p0');a0=r('a0');p1=r('p1');a1=r('a1');v0=r('v0');v1=r('v1');anchor=r('anchor')
  rows={};previous=anchor
  for actual,peer,plan,key in [(a0,v0,p0,'historical'),(a1,v1,p1,'fresh')]:
   a=dict(plan=plan,reader_source=r('source'),root='root',journal=r('journal'),runtime_result=r('result'),route_predecessor=previous,all_claims_accounted=True,claimed_keys=[key],claimed=1,finished_or_unresolved=1,active=0,unclaimed=0,ready=1,quarantine=None)
   rows[actual['path']]=a;rows[peer['path']]=dict(accepted_finite_reader_route_closed_cut=True,actual=actual,findings=[],**{k:a[k]for k in ('plan','reader_source','root','journal','runtime_result')});rows[plan['path']]=dict(schema='causes.saved_corpus_reader_plan.v1',expected_unclaimed_contexts=3) if key=='historical' else dict(schema='causes.fresh_candidate_reader_plan.v1',expected_unclaimed_contexts=1);previous=actual
  plan=dict(prior_arrival6_final_cut=anchor,intervening_reader_cuts=[dict(actual=a0,review=v0),dict(actual=a1,review=v1)],route_predecessor=a1)
  return rows,plan
 def test_native_ordered_history_retains_all_claims(self):
  rows,plan=self.history()
  with patch.object(m,'ck',lambda r:rows[r['path']]):
   history,seen=m.previous_history(plan)
  self.assertEqual(len(history),2);self.assertEqual(seen,{'historical','fresh'})
 def test_historical_partial_cut_retains_intentionally_unclaimed(self):
  rows,plan=self.history();rows['a0'].update(ready=3,unclaimed=2)
  with patch.object(m,'ck',lambda r:rows[r['path']]):
   history,seen=m.previous_history(plan)
  self.assertEqual(history[0]['unclaimed'],2);self.assertEqual(seen,{'historical','fresh'})
 def test_repeated_attempt_refuses(self):
  rows,plan=self.history();rows['a1']['claimed_keys']=['historical']
  with patch.object(m,'ck',lambda r:rows[r['path']]),self.assertRaises(AssertionError):m.previous_history(plan)
 def test_broken_predecessor_refuses(self):
  rows,plan=self.history();rows['a1']['route_predecessor']=rows['a0']['route_predecessor']
  with patch.object(m,'ck',lambda r:rows[r['path']]),self.assertRaises(AssertionError):m.previous_history(plan)
 def test_incomplete_natural_selection_refuses(self):
  rows,plan=self.history();rows['p1']['expected_unclaimed_contexts']=2
  with patch.object(m,'ck',lambda r:rows[r['path']]),self.assertRaises(AssertionError):m.previous_history(plan)

class SavedCohortBoundaries(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
 def put(self,name,value):
  p=self.root/name;p.write_bytes(value if isinstance(value,bytes) else json.dumps(value).encode());return m.pin(p)
 def case(self):
  cohort=self.put('cohort.json',dict(selected_run_ids=['a','b','unavailable'],assigned_contexts=3))
  peer=self.put('peer.json',dict(accepted_original_candidate_measurement_cohort=True,cohort=cohort,findings=[]))
  prep=dict(cohort=cohort,cohort_review=peer,counts=dict(selected=3,context_ready=2,reader_ready=2,reader_requests=6,unavailable=1,self_requests=4))
  return dict(cohort=cohort),prep,[dict(run_id=x) for x in ('a','b','unavailable')]
 def test_saved_cohort_accepts_unavailable_and_separate_self_metadata(self):
  plan,prep,assigned=self.case()
  m.validate_cohort_counts('saved_corpus',None,plan,prep,assigned)
 def test_substituted_or_duplicate_assigned_run_refuses(self):
  plan,prep,assigned=self.case()
  for run in ('a','foreign'):
   with self.subTest(run=run),self.assertRaisesRegex(AssertionError,'saved_cohort_assigned_runs'):
    m.validate_cohort_counts('saved_corpus',None,plan,prep,assigned[:2]+[dict(run_id=run)])
 def test_unaccepted_or_changed_cohort_refuses(self):
  plan,prep,assigned=self.case();peer=m.ck(prep['cohort_review']);peer['cohort']=self.put('other.json',{})
  prep['cohort_review']=self.put('bad_peer.json',peer)
  with self.assertRaisesRegex(AssertionError,'saved_cohort_acceptance'):m.validate_cohort_counts('saved_corpus',None,plan,prep,assigned)
 def test_saved_integer_counts_and_three_instruments_are_required(self):
  plan,prep,assigned=self.case()
  for key,value in [('unavailable',True),('reader_requests',5),('context_ready',1),('selected',4)]:
   changed=copy.deepcopy(prep);changed['counts'][key]=value
   with self.subTest(key=key),self.assertRaises(AssertionError):m.validate_cohort_counts('saved_corpus',None,plan,changed,assigned)
 def test_fresh_global_count_and_pending_accounting_remain(self):
  runtime=SimpleNamespace(KNOWN={'fresh_known':'known'},checked=lambda ref:dict(global_count=5))
  prep=dict(counts=dict(selected=5,copied=3,reader_ready=2,unavailable=1,pending=2))
  m.validate_cohort_counts('fresh_candidate',runtime,{},prep,[{}, {}, {}])
  for key,value in [('selected',6),('pending',1),('unavailable',0)]:
   changed=copy.deepcopy(prep);changed['counts'][key]=value
   with self.subTest(key=key),self.assertRaises(AssertionError):m.validate_cohort_counts('fresh_candidate',runtime,{},changed,[{}, {}, {}])

class ProcessJournalBoundaries(unittest.TestCase):
 setUp=SavedCohortBoundaries.setUp
 put=SavedCohortBoundaries.put
 def progress_case(self):
  ref=self.put('identity.json',{})
  process={'identity':dict(pid=301,start_ticks='77',argv=['python','reader'])}
  supervisor={'supervisor':dict(pid=300,start_ticks='76',argv=['python','supervisor'])}
  events=[dict(event='claimed',key='a'),dict(event='finished',key='a'),dict(event='claimed',key='b')]
  raw=b''.join(json.dumps(row).encode()+b'\n' for row in events)
  journal=self.put('prefix.jsonl',raw)
  seen=dict(reader=dict(process['identity'],state='S'),supervisor=dict(supervisor['supervisor'],state='S'))
  observation=dict(plan=ref,packet=ref,original_process=ref,supervisor_intent=ref,journal_original_path=str(self.root/'original.jsonl'),journal_snapshot=journal,process_before=seen,process_after=copy.deepcopy(seen),counts=dict(claimed_contexts=2,terminal_contexts=1,active_contexts=1,unclaimed_contexts=0,event_counts={'claimed':2,'finished':1}),observed_from='2026-01-01T00:01:00+00:00',observed_to='2026-01-01T00:01:00+00:00',complete_exists=False,findings=[])
  actual=dict(plan=ref,packet=ref,actor_process=ref,supervisor_intent=ref,journal_original=dict(path=observation['journal_original_path']),observed_at='2026-01-01T00:03:00+00:00')
  closed=raw+json.dumps(dict(event='finished',key='b')).encode()+b'\n'
  settings=dict(output=str(self.root/'output.json'))
  return observation,actual,process,supervisor,closed,settings
 def seal(self,observation,settings,observations=None):
  startup=self.put('startup.json',observation);settings['startup_actual_path']=startup['path']
  review=self.put('startup_peer.json',dict(accepted_reader_process_journal_startup=True,actual=startup,findings=[]))
  return self.put('custody.json',dict(schema='causes.reader_process_journal_progress_custody.v1',startup=startup,startup_review=review,startup_review_flag='accepted_reader_process_journal_startup',observations=observations or []))
 def run_case(self,case,observations=None):
  observation,actual,process,supervisor,closed,settings=case
  return m.validate_process_journal_progress(self.seal(observation,settings,observations),settings,actual,process,supervisor,closed,2)
 def test_exact_snapshot_does_not_claim_historical_lock(self):
  result=self.run_case(self.progress_case())
  self.assertTrue(result['journal_prefixes_verified']);self.assertFalse(result['historical_watch_lock_identity_claimed'])
  self.assertEqual(result['observations'][0]['terminal_contexts'],1)
 def test_all_dated_prefixes_and_counts_are_checked(self):
  case=self.progress_case();later=copy.deepcopy(case[0]);later['observed_from']=later['observed_to']='2026-01-01T00:02:00+00:00';later['previous']=self.put('startup.json',case[0])
  later['journal_snapshot']=self.put('later.jsonl',case[4]);later['counts'].update(terminal_contexts=2,active_contexts=0,event_counts={'claimed':2,'finished':2})
  result=self.run_case(case,[self.put('later.json',later)])
  self.assertEqual([x['terminal_contexts'] for x in result['observations']],[1,2])
  later['counts']['terminal_contexts']=1
  with self.assertRaisesRegex(AssertionError,'progress_counts'):self.run_case(case,[self.put('bad_later.json',later)])
 def test_same_pid_with_changed_start_or_argv_refuses(self):
  for field,value in [('start_ticks','78'),('argv',['python','other'])]:
   case=self.progress_case();case[0]['process_after']['reader'][field]=value
   with self.subTest(field=field),self.assertRaisesRegex(AssertionError,'progress_process_identity'):self.run_case(case)
 def test_changed_packet_or_original_process_refuses(self):
  for key in ('packet','original_process','supervisor_intent'):
   case=self.progress_case();case[0][key]=self.put('foreign.json',{'foreign':True})
   with self.subTest(key=key),self.assertRaises(AssertionError):self.run_case(case)
 def test_rehashed_unrelated_or_partial_prefix_refuses(self):
  for raw in (b'{"event":"claimed","key":"foreign"}\n',b'{"event":"claimed"}'):
   case=self.progress_case();case[0]['journal_snapshot']=self.put('bad_prefix.jsonl',raw)
   with self.subTest(raw=raw),self.assertRaisesRegex(AssertionError,'progress_journal_prefix'):self.run_case(case)
 def test_duplicate_observation_and_backwards_time_refuse(self):
  case=self.progress_case();reference=self.seal(case[0],case[5]);custody=m.ck(reference);custody['observations']=[custody['startup']]
  with self.assertRaisesRegex(AssertionError,'duplicate_progress_observation'):
   m.validate_process_journal_progress(self.put('duplicate.json',custody),case[5],case[1],case[2],case[3],case[4],2)
  later=copy.deepcopy(case[0]);later['observed_from']=later['observed_to']='2026-01-01T00:00:00+00:00';later['previous']=custody['startup']
  with self.assertRaisesRegex(AssertionError,'progress_time_order'):self.run_case(case,[self.put('backwards.json',later)])
 def test_unaccepted_startup_cannot_replace_native_custody(self):
  case=self.progress_case();reference=self.seal(case[0],case[5]);custody=m.ck(reference)
  custody['startup_review']=self.put('bad_review.json',dict(actual=custody['startup'],findings=[]))
  with self.assertRaisesRegex(AssertionError,'startup_acceptance'):
   m.validate_process_journal_progress(self.put('unaccepted.json',custody),case[5],case[1],case[2],case[3],case[4],2)

class DescriptorV2Boundaries(unittest.TestCase):
 def setUp(self):
  ClosedAuditBoundaries.setUp(self);self.d.update(schema='causes.saved_reader_closed_audit_descriptor.v2',cohort_kind='saved_corpus',process_journal_progress=self.ref,direct_binding=None)
 def test_saved_explicit_descriptor_accepts(self):m.validate_descriptor(self.d)
 def test_unknown_cohort_kind_refuses(self):
  self.d['cohort_kind']='automatic'
  with self.assertRaisesRegex(AssertionError,'cohort_kind'):m.validate_descriptor(self.d)
 def test_ambiguous_custody_modes_refuse(self):
  self.d['direct_binding']=self.ref
  with self.assertRaisesRegex(AssertionError,'one_prior_custody_mode'):m.validate_descriptor(self.d)
 def test_cannot_overwrite_process_journal_custody(self):
  self.d['output']=self.ref['path']
  with self.assertRaisesRegex(AssertionError,'output_overwrites_input'):m.validate_descriptor(self.d)

class DerivedCohortBoundaries(unittest.TestCase):
 setUp=SavedCohortBoundaries.setUp
 put=SavedCohortBoundaries.put
 def case(self):
  rows=[dict(run_id='same_actor',view_kind='neutral',stage=stage,measurement_key='derived_reader_'+stage,disposition=disposition) for stage,disposition in [('early','prepared'),('end','unavailable')]]
  assignments=self.put('assigned.json',[{key:row[key] for key in ('run_id','view_kind','stage')} for row in rows])
  ready=self.put('ready.json',rows[:1]);missing=self.put('missing.json',rows[1:])
  plan=dict(schema='causes.derived_reader_plan.v1',cohort=assignments,additive_assigned=assignments,additive_ready=ready)
  prep=dict(schema='causes.frozen_derived_reader_preparation.v1',assignments=assignments,ready=ready,unavailable=missing,counts=dict(assigned=2,ready=1,unavailable=1,requests=3),missing_slots=3)
  return plan,prep,rows
 def test_derived_keeps_missing_arm_and_distinct_stages_of_one_actor(self):
  plan,prep,rows=self.case();m.validate_cohort_counts('derived',None,plan,prep,rows)
 def test_derived_dropped_or_reordered_assignment_refuses(self):
  plan,prep,rows=self.case()
  for changed in [rows[:1],list(reversed(rows)),[rows[0],rows[0]]]:
   with self.subTest(changed=changed),self.assertRaises(AssertionError):m.validate_cohort_counts('derived',None,plan,prep,changed)
 def test_derived_changed_or_boolean_counts_refuse(self):
  plan,prep,rows=self.case()
  for key,value in [('assigned',3),('ready',True),('requests',2),('unavailable',0)]:
   changed=copy.deepcopy(prep);changed['counts'][key]=value
   with self.subTest(key=key),self.assertRaises(AssertionError):m.validate_cohort_counts('derived',None,plan,changed,rows)
 def test_derived_missing_slots_and_cohort_join_refuse(self):
  plan,prep,rows=self.case()
  for changed in [dict(prep,missing_slots=0),dict(prep,missing_slots=True),dict(prep,assignments=self.put('foreign.json',[]))]:
   with self.subTest(changed=changed),self.assertRaises(AssertionError):m.validate_cohort_counts('derived',None,plan,changed,rows)
 def test_derived_row_substitution_refuses(self):
  plan,prep,rows=self.case();changed=copy.deepcopy(rows);changed[0]['disposition']='unavailable'
  with self.assertRaisesRegex(AssertionError,'derived_assigned_rows'):m.validate_cohort_counts('derived',None,plan,prep,changed)
 def test_original_measurement_key_cannot_impersonate_derived(self):
  plan,prep,rows=self.case();rows[0]['measurement_key']='original_reader_early';ready=self.put('changed_ready.json',rows[:1]);plan['additive_ready']=prep['ready']=ready
  with self.assertRaisesRegex(AssertionError,'derived_measurement_identity'):m.validate_cohort_counts('derived',None,plan,prep,rows)
 def test_derived_runtime_uses_exact_plan_pin(self):
  plan,_,_=self.case();ref=self.put('plan.json',plan);seen=[];bound=object();runtime=SimpleNamespace(bind_for_plan=lambda value:(seen.append(value),bound)[1])
  self.assertIs(m.runtime_for_plan(runtime,plan,ref),bound);self.assertEqual(seen,[ref])
  with self.assertRaisesRegex(AssertionError,'derived_runtime_plan_pin'):m.runtime_for_plan(runtime,plan)
  with self.assertRaisesRegex(AssertionError,'derived_runtime_plan_pin'):m.runtime_for_plan(runtime,dict(plan,workers=9),ref)
 def test_old_runtime_interfaces_remain(self):
  seen=[];bound=object();runtime=SimpleNamespace(bind_for_plan=lambda value:(seen.append(value),bound)[1]);ordinary={'schema':'old'}
  self.assertIs(m.runtime_for_plan(runtime,ordinary),runtime)
  extension=dict(ordinary,cohort_extension={'path':'binding'})
  self.assertIs(m.runtime_for_plan(runtime,extension),bound);self.assertEqual(seen,[extension])
 def test_derived_projection_only_adds_native_interface_fields(self):
  _,prep,_=self.case();prep['protocol']=dict(checker_config={'path':'config'},prompts={'presence':'prompt'},reader_source={'sha256':'native'})
  original=copy.deepcopy(prep);loaded=dict(prep,checker_config=prep['protocol']['checker_config'],prompts=prep['protocol']['prompts'],cohort=prep['assignments'])
  m.validate_loaded_preparation('derived',loaded,prep);self.assertEqual(prep,original)
  self.assertEqual(m.native_reader_source('derived',prep),{'sha256':'native'})
  for changed in [dict(loaded,unreviewed=True),dict(loaded,missing_slots=0),dict(loaded,checker_config={})]:
   with self.assertRaisesRegex(AssertionError,'loaded_preparation_changed'):m.validate_loaded_preparation('derived',changed,prep)
 def test_old_preparation_must_still_be_identical(self):
  prep={'reader_source':{'sha256':'native'}};m.validate_loaded_preparation('saved_corpus',prep,prep)
  self.assertEqual(m.native_reader_source('saved_corpus',prep),{'sha256':'native'})
  with self.assertRaisesRegex(AssertionError,'loaded_preparation_changed'):m.validate_loaded_preparation('saved_corpus',dict(prep,extra=True),prep)
 def test_explicit_derived_descriptor_accepts(self):
  ClosedAuditBoundaries.setUp(self);self.d.update(schema='causes.saved_reader_closed_audit_descriptor.v2',cohort_kind='derived',process_journal_progress=self.ref,direct_binding=None);m.validate_descriptor(self.d)
 def test_derived_predecessor_must_have_completed_exact_selection(self):
  rows,plan=ClosedAuditBoundaries.history(self);rows['p1']['schema']='causes.derived_reader_plan.v1';rows['p1']['expected_unclaimed_contexts']=2
  with patch.object(m,'ck',lambda ref:rows[ref['path']]),self.assertRaises(AssertionError):m.previous_history(plan)

if __name__=='__main__':unittest.main()
