import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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

if __name__=='__main__':unittest.main()
