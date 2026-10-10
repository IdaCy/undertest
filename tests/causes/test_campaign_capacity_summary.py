import copy
import datetime as dt
import hashlib
import json
import unittest

from causes import campaign_capacity_summary as c


START = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)


def wall(seconds):
    return (START + dt.timedelta(seconds=seconds)).isoformat()


def point(seconds):
    return {'wall': wall(seconds), 'monotonic_seconds': 1000 + seconds}


def journal(events):
    raw = b''.join(json.dumps(e, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()+b'\n' for e in events)
    return {'group':'fixed', 'events':events, 'bytes':len(raw), 'sha256':hashlib.sha256(raw).hexdigest(),
            'incomplete_tail_bytes':0}


def fixture():
    owner={'group':'fixed','controller_id':'current','role':'controller','workers':8,'max_full':2}
    processes=[{'role':'controller','pid':100,'process_starttime_ticks':'10','argv':['python','controller']},
               {'role':'provider','pid':200,'process_starttime_ticks':'20','argv':['python','provider']}]
    declaration={'controls_absent':[], 'owner':owner,'processes':processes,'groups':['fixed'],
      'timing':{'max_lateness_seconds':10,'max_clock_drift_seconds':1,'max_sample_observation_span_seconds':30,
                'max_boundary_receipt_capture_span_seconds':60},
      'eligibility':{'max_wait_seconds':600,'active_full':2,'active_total':8}}
    claims=[{'event':'claimed','controller_id':'current','run_id':str(i),'at':wall(-20),'full':i<2} for i in range(8)]
    claims.insert(0,{'event':'claimed','controller_id':'retired','run_id':'retired','at':wall(-100),'full':True})
    log=journal(claims)
    identities=[dict(role=p['role'],pid=p['pid'],start_ticks=p['process_starttime_ticks'],start_matches=True,argv_matches=True,
                     argv_sha256=hashlib.sha256(json.dumps(p['argv']).encode()).hexdigest()) for p in processes]
    identities[0].update(qualified_cap_matches=True,workers=8,max_full=2)
    samples=[]
    for i in range(61):
        s=i*30
        samples.append({'index':i,'at':wall(s),'monotonic_seconds':1000+s,'scheduled_monotonic_seconds':1000+s,
            'lateness_seconds':0,'finished_at':wall(s+.1),'finished_monotonic_seconds':1000+s+.1,
            'metrics_capture_started':point(s+.01),'metrics_capture_finished':point(s+.02),
            'journal_capture_started':point(s+.03),'journal_capture_finished':point(s+.04),
            'controllers':[{'group':'fixed','journal_sha256':log['sha256']}],
            'current_owner':dict(group='fixed',controller_id='current',active_full=2,active_prefix=6,
                                 active_run_ids=[str(k) for k in range(8)]),
            'process_identities':copy.deepcopy(identities),'pin_errors':[],'storage_guard':{'healthy':True},'controls_present':{},
            'vmstat':{'oom_kill':0,'pswpin':0,'pswpout':0},'memory':{'MemAvailable_KiB':32*1024**2},
            'metrics':['vllm:'+k+' '+str(v) for k,v in {'generation_tokens_total':i*100,'prompt_tokens_total':i*50,
                'request_success_total':i,'num_preemptions_total':0,'num_requests_waiting':0,'kv_cache_usage_perc':.2}.items()]})
    episodes=[{'group':'fixed','run_id':r['run_id'],'full':r['full'],'claim':r,'final':None} for r in claims]
    boundary={'journals':[log],'requests':{'episodes':episodes,'requests':[],'metadata_errors':[]},
              'capture':{'started':point(.2),'finished':point(.3)}}
    end=copy.deepcopy(boundary);end['capture']={'started':point(1800.2),'finished':point(1800.3)}
    window={'start_wall':wall(0),'end_wall':wall(1800),'start_monotonic_seconds':1000,'end_monotonic_seconds':2800,
            'scheduled_duration_seconds':1800,'half_open_receipt_finish_interval':True}
    return samples,boundary,end,window,declaration


def request(run, end, *, tokens=100, success=True, start=-5, name='actor/0'):
    return {'group':'fixed','run_id':run,'full':int(run)<2,'relative_request_directory':name,'phase':'actor_continuation',
            'attempt':{'started_at':wall(start)},'attempt_pin':None,
            'receipt':{'finished_at':wall(end),'elapsed_seconds':end-start,'status':'complete' if success else 'transport_or_response_failure',
                       'usage':{'completion_tokens':tokens,'prompt_tokens':100}},'receipt_pin':None}


def criteria():
    return {'reference_selection':{'duration_seconds':1800},'reference_support':{
        'full_successful_requests_min':20,'full_unique_actors_min':2,'continuation_successful_requests_min':10,
        'continuation_unique_actors_min':2,'prefix_successful_requests_min':40,'prefix_unique_actors_min':10,
        'mean_full_occupied_workers_min':1.8,'overall_failure_fraction_max':.1,'full_failure_fraction_max':.1},
        'guards':{'full_actor_technical_terminals':{'minimum_terminal_dispositions':4,'minimum_technical_failures':3,
                 'technical_fraction_hold_at_or_above':.75}},
        'candidate_windows':{'confirmation_support':{'full_successful_requests_min':40,'full_unique_actors_min':4,
            'continuation_successful_requests_min':20,'continuation_unique_actors_min':2,'prefix_successful_requests_min':40,
            'prefix_unique_actors_min':10}},
        'adoption_gates':{'full_progress_ratio_min':1.5,'continuation_progress_ratio_min':1.25,'service_productivity_ratio_min':.8}}


def accepted_reference():
    metrics={k:{'completed':50,'successful':50,'failed':0,'successful_actors':10,'failed_actors':0,
            'returned_tokens':18000,'tokens_per_second':10,'elapsed_seconds':1800,'elapsed_supported':True,
            'service_productivity':10,'usage_coverage':1,'failure_fraction':0} for k in ['overall','full','continuation','prefix']}
    metrics.update(mean_active_full=2,pending_requests=0)
    return {'decision':'eligible_reference','supported':True,'measurements':metrics}


class CapacitySummaryTest(unittest.TestCase):
    def test_half_open_receipts_and_retired_owner_exclusion(self):
        s,a,b,w,d=fixture()
        b['requests']['requests']=[request('0',0),request('1',1800),request('0',900,name='actor/1')]
        r=c.summarize_window(s,a,b,w,d)
        self.assertEqual(r['metrics']['full']['completed'],2)
        self.assertEqual(r['metrics']['full']['returned_tokens'],200)
        self.assertEqual(r['metrics']['mean_active_full'],2)
        self.assertEqual(r['descriptive']['historical_boundary_episodes_excluded'],1)

    def test_delayed_last_sample_does_not_extend_denominator(self):
        s,a,b,w,d=fixture();b['requests']['requests']=[request('0',1801)]
        r=s[-1]
        for k in ['at','finished_at']:r[k]=wall(c.epoch(r[k])-START.timestamp()+5)
        for k in ['monotonic_seconds','finished_monotonic_seconds']:r[k]+=5
        for k in ['metrics_capture_started','metrics_capture_finished','journal_capture_started','journal_capture_finished']:
            r[k]['wall']=wall(c.epoch(r[k]['wall'])-START.timestamp()+5);r[k]['monotonic_seconds']+=5
        r['lateness_seconds']=5
        b['capture']={'started':point(1805.2),'finished':point(1805.3)}
        result=c.summarize_window(s,a,b,w,d)
        self.assertTrue(result['support']['supported'])
        self.assertEqual(result['metrics']['full']['completed'],0)
        self.assertEqual(result['window']['scheduled_duration_seconds'],1800)
        self.assertEqual(result['descriptive']['counter_intervals']['generation_tokens_total']['actual_interval_seconds'],1805)

    def test_drift_or_lateness_retained_as_unsupported(self):
        for kind in ['drift','lateness','span']:
            with self.subTest(kind=kind):
                s,a,b,w,d=fixture()
                if kind=='drift':s[10]['at']=wall(302)
                elif kind=='lateness':s[10]['lateness_seconds']=11
                else:s[10]['finished_monotonic_seconds']+=31
                self.assertFalse(c.summarize_window(s,a,b,w,d)['support']['supported'])

    def test_regression_or_missing_samples_refused(self):
        for kind in ['regression','missing']:
            s,a,b,w,d=fixture()
            if kind=='regression':s[10]['at']=s[9]['at']
            else:s.pop(10)
            with self.assertRaises(ValueError):c.summarize_window(s,a,b,w,d)

    def test_terminal_without_request_receipt_unsupported(self):
        s,a,b,w,d=fixture();terminal={'event':'unresolved_current_attempt','controller_id':'current','run_id':'0','at':wall(100)}
        b['journals'][0]=journal(b['journals'][0]['events']+[terminal])
        for row in s[4:]:
            row['controllers'][0]['journal_sha256']=b['journals'][0]['sha256'];row['current_owner']['active_full']=1;row['current_owner']['active_run_ids'].remove('0')
        r=request('0',90);r['receipt']=None;b['requests']['requests']=[r]
        result=c.summarize_window(s,a,b,w,d)
        self.assertFalse(result['support']['supported']);self.assertIsNone(result['requests'][0]['occupied_seconds'])
        self.assertEqual(result['actors'][0]['classification'],'unprojected')

    def test_invalid_usage_bool_or_missing_is_not_coverage(self):
        for tokens in [True,None,-1]:
            s,a,b,w,d=fixture();b['requests']['requests']=[request('0',10,tokens=tokens)]
            r=c.summarize_window(s,a,b,w,d)
            self.assertEqual(r['metrics']['full']['usage_coverage'],0)
            self.assertEqual(r['metrics']['full']['returned_tokens'],0)

    def test_request_extra_field_not_serialized(self):
        s,a,b,w,d=fixture();r=request('0',10);r['semantic_sentinel']='do not emit';b['requests']['requests']=[r]
        self.assertNotIn('do not emit',json.dumps(c.summarize_window(s,a,b,w,d)))

    def test_identity_mismatch_cannot_qualify(self):
        s,a,b,w,d=fixture();s[10]['process_identities'][1]['argv_sha256']='changed'
        self.assertFalse(c.summarize_window(s,a,b,w,d)['support']['supported'])

    def test_reference_minima_and_zero_reference(self):
        ref=accepted_reference();summary={'metrics':ref['measurements'],'support':{'issues':[]},'window':{'scheduled_duration_seconds':1800}}
        self.assertEqual(c.evaluate_reference(summary,criteria())['decision'],'eligible_reference')
        summary['metrics']['full']['tokens_per_second']=0
        self.assertIn('full_zero_progress_reference',c.evaluate_reference(summary,criteria())['reasons'])
        summary['metrics']['prefix']['successful_actors']=9
        self.assertIn('prefix_actor_support',c.evaluate_reference(summary,criteria())['reasons'])

    def test_reference_one_failure_allowed_but_fraction_guarded(self):
        ref=accepted_reference();summary={'metrics':ref['measurements'],'support':{'issues':[]},'window':{'scheduled_duration_seconds':1800}}
        summary['metrics']['full']['failure_fraction']=.04
        self.assertTrue(c.evaluate_reference(summary,criteria())['supported'])
        summary['metrics']['full']['failure_fraction']=.11
        self.assertFalse(c.evaluate_reference(summary,criteria())['supported'])

    def test_actor_terminal_budget_and_censor_distinction(self):
        rows=[{'run_id':str(i),'full':True,'claim':{'at':wall(0)},'terminal':{'at':wall(50+i)},
               'classification':'technical' if i<3 else 'censored'} for i in range(4)]
        check=lambda rows:c.actor_terminal_guard(rows,criteria(),START.timestamp(),START.timestamp()+1800,cohort_start=START.timestamp())
        self.assertFalse(check(rows[:3])['hold']);self.assertTrue(check(rows)['hold'])
        rows[-1]['classification']='unknown';self.assertFalse(check(rows)['supported'])
        rows[-1]['classification']='natural';rows[0]['claim']['at']=wall(-1)
        self.assertFalse(check(rows)['hold'])

    def test_candidate_failure_conjunction_and_prefix_cannot_dilute_full(self):
        ref=accepted_reference();m=copy.deepcopy(ref['measurements']);m['mean_active_full']=4
        summary={'metrics':m,'support':{'supported':True,'issues':[]},'window':{'start_wall':wall(0),'end_wall':wall(1800),'scheduled_duration_seconds':1800},'actors':[],'requests':[]}
        m['full'].update(completed=20,failed=2,failure_fraction=.1,failed_actors=2)
        call=lambda:c.evaluate_candidate(summary,ref,criteria(),cohort_start=START.timestamp(),cumulative_actors=[])
        self.assertEqual(call()['decision'],'continue_confirmation')
        m['full'].update(failed=3,failure_fraction=.15)
        self.assertEqual(call()['decision'],'hold')
        m['full'].update(completed=5,failure_fraction=.6,failed_actors=1)
        self.assertEqual(call()['decision'],'continue_confirmation')
        m['full']['failed_actors']=2;self.assertEqual(call()['decision'],'hold')

    def test_unsupported_intervening_block_breaks_service_streak(self):
        ref=accepted_reference();m=copy.deepcopy(ref['measurements']);m['mean_active_full']=4;m['prefix']['service_productivity']=7
        previous={'metrics':copy.deepcopy(m),'support':{'supported':False,'issues':['unsupported']},'window':{'end_wall':wall(0)}}
        summary={'metrics':m,'support':{'supported':True,'issues':[]},'window':{'start_wall':wall(0),'end_wall':wall(1800),'scheduled_duration_seconds':1800},'actors':[],'requests':[]}
        call=lambda:c.evaluate_candidate(summary,ref,criteria(),cohort_start=START.timestamp(),cumulative_actors=[],prior_blocks=[previous])
        self.assertEqual(call()['decision'],'continue_confirmation')
        previous['support']['supported']=True;self.assertEqual(call()['decision'],'hold')

    def test_prelude_first_eligible_reuse_and_health(self):
        s,a,b,w,d=fixture();first=s[0];first['eligible']=True;first['wall_minus_monotonic_delta_seconds']=0.0
        pre=copy.deepcopy(first);pre.pop('wall_minus_monotonic_delta_seconds')
        self.assertEqual(c.prelude_support([pre],first,d),[])
        pre['eligible']=False
        with self.assertRaises(ValueError):c.prelude_support([pre],first,d)

    def test_passive_health_adjacency_and_counter_changes(self):
        s,*_=fixture();s[0]['memory']['MemAvailable_KiB']=1
        self.assertNotIn('ram_threshold',c.passive_health_issues(s))
        s[1]['memory']['MemAvailable_KiB']=1;self.assertIn('ram_threshold',c.passive_health_issues(s))
        s[-1]['vmstat']['oom_kill']=1;self.assertIn('vm_counter_changed:oom_kill',c.passive_health_issues(s))

    def test_delayed_shared_boundary_blocks_and_confirmation(self):
        original,a,b,_,d=fixture()
        def sample(index):
            row=copy.deepcopy(original[0]);value=index*30+(5 if index==60 else 0)
            row.update(index=index,at=wall(value),monotonic_seconds=1000+value,
                       scheduled_monotonic_seconds=1000+index*30,lateness_seconds=value-index*30,
                       finished_at=wall(value+.1),finished_monotonic_seconds=1000+value+.1)
            for kind,offset in [('metrics_capture',.01),('journal_capture',.03)]:
                row[kind+'_started']=point(value+offset);row[kind+'_finished']=point(value+offset+.01)
            row['metrics']=[line.rsplit(' ',1)[0]+' '+str(index*100 if 'generation_tokens_total' in line else
                index*50 if 'prompt_tokens_total' in line else index if 'request_success_total' in line else float(line.rsplit(' ',1)[1]))
                for line in row['metrics']]
            return row
        def summarize(base,count):
            rows=[sample(i) for i in range(base,base+count+1)]
            start=copy.deepcopy(a);end=copy.deepcopy(b)
            start['capture']={'started':point(base*30+(5 if base==60 else 0)+.2),
                              'finished':point(base*30+(5 if base==60 else 0)+.3)}
            at=(base+count)*30+(5 if base+count==60 else 0)
            end['capture']={'started':point(at+.2),'finished':point(at+.3)}
            end['requests']['requests']=[request('0',1800)]
            w={'start_wall':wall(base*30),'end_wall':wall((base+count)*30),
               'start_monotonic_seconds':1000+base*30,'end_monotonic_seconds':1000+(base+count)*30,
               'scheduled_duration_seconds':count*30,'half_open_receipt_finish_interval':True,
               'global_start_wall':wall(0),'global_start_monotonic_seconds':1000}
            return c.summarize_window(rows,start,end,w,d,preceding_sample=sample(base-1) if base else None)
        left,right,confirmation=summarize(0,60),summarize(60,60),summarize(60,240)
        self.assertEqual(left['metrics']['full']['completed'],0)
        self.assertEqual(right['metrics']['full']['completed'],1)
        self.assertEqual(confirmation['metrics']['full']['completed'],1)
        for value in [left,right,confirmation]:
            self.assertTrue(value['support']['supported']);self.assertEqual(value['metrics']['mean_active_full'],2)
        self.assertEqual(confirmation['metrics']['full']['tokens_per_second'],100/7200)

    def test_initial_endpoint_metadata_integrity_holds(self):
        ref=accepted_reference();m=copy.deepcopy(ref['measurements']);m['mean_active_full']=4
        summary={'metrics':m,'support':{'supported':True,'issues':[]},'window':{'start_wall':wall(0),'end_wall':wall(1800),'scheduled_duration_seconds':1800},'actors':[],'requests':[]}
        for field,value in [('usage_coverage',.5),('elapsed_supported',False)]:
            candidate=copy.deepcopy(summary);candidate['metrics']['full'][field]=value
            result=c.evaluate_candidate(candidate,ref,criteria(),cohort_start=START.timestamp(),cumulative_actors=[])
            self.assertEqual(result['decision'],'hold');self.assertIn('full_request_metadata_integrity',result['reasons'])

    def test_pending_at_fixed_endpoint_includes_later_captured_receipt(self):
        s,a,b,w,d=fixture();b['requests']['requests']=[request('0',1805,start=500)]
        summary=c.summarize_window(s,a,b,w,d)
        self.assertEqual(summary['metrics']['pending_requests'],1)
        summary['metrics']['mean_active_full']=4
        result=c.evaluate_candidate(summary,accepted_reference(),criteria(),cohort_start=START.timestamp(),cumulative_actors=[])
        self.assertIn('endpoint_progress_stall',result['reasons'])
        b['requests']['requests'][0]['receipt']['finished_at']=wall(1800)
        self.assertEqual(c.summarize_window(s,a,b,w,d)['metrics']['pending_requests'],1)

    def test_duplicate_or_mutated_boundary_metadata_refused(self):
        for kind in ['attempt','receipt','attempt_pin','duplicate_request','duplicate_episode']:
            with self.subTest(kind=kind):
                s,a,b,w,d=fixture();a['requests']['requests']=[request('0',100)]
                b['requests']['requests']=copy.deepcopy(a['requests']['requests'])
                if kind=='attempt':b['requests']['requests'][0]['attempt']['started_at']=wall(-4)
                elif kind=='receipt':b['requests']['requests'][0]['receipt']['usage']['completion_tokens']=101
                elif kind=='attempt_pin':b['requests']['requests'][0]['attempt_pin']={'changed':True}
                elif kind=='duplicate_request':b['requests']['requests']*=2
                else:b['requests']['episodes'].append(copy.deepcopy(b['requests']['episodes'][1]))
                with self.assertRaises(ValueError):c.summarize_window(s,a,b,w,d)

    def test_none_receipt_and_justified_phase_resolution_allowed(self):
        s,a,b,w,d=fixture();a['requests']['requests']=[request('0',100)]
        a['requests']['requests'][0].update(receipt=None,phase='actor_prefix')
        b['requests']['requests']=[request('0',100)]
        b['requests']['episodes'][1]['early_capture.json']={'fields':{'calls':0},'pin':None}
        self.assertEqual(c.summarize_window(s,a,b,w,d)['metrics']['continuation']['successful'],1)

    def test_cumulative_actor_guard_rejects_bad_class_identity_and_time(self):
        row={'run_id':'0','full':True,'claim':{'at':wall(0),'run_id':'0','controller_id':'owner'},
             'terminal':{'at':wall(10),'run_id':'0','controller_id':'owner'},'classification':'natural'}
        for kind in ['label','duplicate','time','owner','identity']:
            rows=[copy.deepcopy(row)]
            if kind=='label':rows[0]['classification']='unexpected'
            elif kind=='duplicate':rows*=2
            elif kind=='time':rows[0]['terminal']['at']=wall(-1)
            elif kind=='owner':rows[0]['terminal']['controller_id']='foreign'
            else:rows[0]['terminal']['run_id']='different'
            with self.assertRaises(ValueError):c.actor_terminal_guard(rows,criteria(),START.timestamp(),START.timestamp()+1800,cohort_start=START.timestamp())


if __name__=='__main__':unittest.main()
