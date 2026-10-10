import copy
import unittest

from causes import campaign_capacity_summary as c
from test_campaign_capacity_summary import fixture, request, accepted_reference, criteria, journal, wall, START


def prelude():
    _,_,b,_,d=fixture();d['owner']['max_full']=4
    return b,d,accepted_reference(),criteria(),START.timestamp()-20


def finish(boundary, run, at, classification):
    row=next(r for r in boundary['requests']['episodes'] if r['run_id']==run)
    terminal={'event':'episode_finished','run_id':run,'controller_id':'current','at':wall(at)}
    row['final']=terminal;row['terminal_metadata']={'classification':classification}
    events=boundary['journals'][0]['events']+[terminal];boundary['journals']=[journal(events)]


class CapacityPreludeTest(unittest.TestCase):
    def test_zero_interval_has_no_exposure_or_progress_gate(self):
        b,d,r,k,lo=prelude()
        result=c.evaluate_prelude(b,d,r,k,cohort_start=lo,end_wall=wall(-20))
        self.assertTrue(result['supported']);self.assertEqual(result['decision'],'continue_confirmation')
        self.assertEqual(result['measurements']['overall']['completed'],0)
        self.assertFalse(result['progress_duration_and_occupancy_qualification_performed'])

    def test_same_sparse_and_dense_full_failure_predicate(self):
        for dense in (False,True):
            b,d,r,k,lo=prelude()
            b['requests']['requests']=[request(str(i%2),i+1,start=0,success=i>=3,name='actor/'+str(i))
                                      for i in range(20 if dense else 3)]
            result=c.evaluate_prelude(b,d,r,k,cohort_start=lo,end_wall=wall(30))
            self.assertEqual(result['decision'],'hold')
            self.assertIn('full_request_failure_guard',result['reasons'])

    def test_overall_sparse_failures_and_half_open_endpoint(self):
        b,d,r,k,lo=prelude()
        b['requests']['requests']=[request(run,i+1,start=0,success=False,name='actor/'+str(i))
                                  for i,run in enumerate(['2','3','4','2'])]
        result=c.evaluate_prelude(b,d,r,k,cohort_start=lo,end_wall=wall(4))
        self.assertEqual(result['decision'],'continue_confirmation')
        self.assertEqual(result['measurements']['overall']['completed'],3)
        result=c.evaluate_prelude(b,d,r,k,cohort_start=lo,end_wall=wall(5))
        self.assertIn('overall_request_failure_guard',result['reasons'])

    def test_success_metadata_and_duplicate_or_temporal_faults(self):
        for kind in ('usage','elapsed','duplicate','time'):
            b,d,r,k,lo=prelude();row=request('0',1,start=0);b['requests']['requests']=[row]
            if kind=='usage':row['receipt']['usage']=None
            elif kind=='elapsed':row['receipt']['elapsed_seconds']=0
            elif kind=='duplicate':b['requests']['requests'].append(copy.deepcopy(row))
            else:row['attempt']['started_at']=wall(-21)
            if kind in ('duplicate','time'):
                with self.assertRaises(ValueError):c.evaluate_prelude(b,d,r,k,cohort_start=lo,end_wall=wall(5))
            else:
                result=c.evaluate_prelude(b,d,r,k,cohort_start=lo,end_wall=wall(5))
                self.assertEqual(result['decision'],'hold');self.assertFalse(result['supported'])

    def test_actor_terminal_budget_and_unknown_terminal(self):
        b,d,r,k,lo=prelude()
        for row in b['requests']['episodes']:
            if row['run_id'] in ('2','3'):row['full']=True;row['claim']['full']=True
        b['journals']=[journal(b['journals'][0]['events'])]
        for i in range(4):finish(b,str(i),i+1,'technical' if i<3 else 'natural')
        result=c.evaluate_prelude(b,d,r,k,cohort_start=lo,end_wall=wall(5))
        self.assertEqual(result['decision'],'hold');self.assertEqual(result['actor_guard']['technical'],3)
        b,d,r,k,lo=prelude();finish(b,'0',1,'unknown')
        result=c.evaluate_prelude(b,d,r,k,cohort_start=lo,end_wall=wall(5))
        self.assertEqual(result['decision'],'hold');self.assertFalse(result['supported'])

    def test_normal_prefix_terminal_keeps_request_integrity(self):
        b,d,r,k,lo=prelude();finish(b,'2',2,'unknown')
        episode=next(row for row in b['requests']['episodes'] if row['run_id']=='2')
        episode['terminal_metadata']['technical_fields']={
            'end_status':'not_selected','explicit_clean_closure':True}
        row=request('2',1,start=0);b['requests']['requests']=[row]
        result=c.evaluate_prelude(b,d,r,k,cohort_start=lo,end_wall=wall(5))
        self.assertTrue(result['supported']);self.assertEqual(result['decision'],'continue_confirmation')
        self.assertEqual(result['measurements']['overall']['successful'],1)
        self.assertEqual(result['measurements']['full']['successful'],0)
        row['receipt']=None
        result=c.evaluate_prelude(b,d,r,k,cohort_start=lo,end_wall=wall(5))
        self.assertFalse(result['supported']);self.assertEqual(result['decision'],'hold')
        self.assertIn('prelude_terminal_request_missing_receipt:2',result['reasons'])

    def test_terminal_missing_receipt_and_bad_cohort(self):
        b,d,r,k,lo=prelude();finish(b,'0',2,'natural')
        row=request('0',1,start=0);row['receipt']=None;b['requests']['requests']=[row]
        result=c.evaluate_prelude(b,d,r,k,cohort_start=lo,end_wall=wall(5))
        self.assertIn('prelude_terminal_request_missing_receipt:0',result['reasons'])
        with self.assertRaisesRegex(ValueError,'first_claim_changed'):
            c.evaluate_prelude(b,d,r,k,cohort_start=lo+1,end_wall=wall(5))

    def test_control_fault_and_missing_episode_remain_holds(self):
        b,d,r,k,lo=prelude()
        b['journals']=[journal(b['journals'][0]['events']+[{'event':'disk_floor_wait','controller_id':'current','at':wall(0)}])]
        result=c.evaluate_prelude(b,d,r,k,cohort_start=lo,end_wall=wall(5))
        self.assertEqual(result['decision'],'hold')
        b,d,r,k,lo=prelude();b['requests']['episodes']=[x for x in b['requests']['episodes'] if x['run_id']!='0']
        with self.assertRaisesRegex(ValueError,'current_episode_missing'):
            c.evaluate_prelude(b,d,r,k,cohort_start=lo,end_wall=wall(5))
