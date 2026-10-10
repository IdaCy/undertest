import copy
import hashlib
import unittest

from causes import campaign_capacity_wait as w
from causes import campaign_capacity_summary as c
from test_campaign_capacity_summary import fixture, wall, journal


def record(path, value):
    raw=w.encoded(value)+b'\n'
    return {'pin':{'path':path,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()},'value':value}


def waiting_fixture():
    samples,a,b,window,d=fixture()
    d['owner']['max_full']=4
    for sample in samples:
        sample['process_identities'][0]['max_full']=4
    owner={k:d['processes'][0][k] for k in ('pid','process_starttime_ticks','argv')}
    cap={'endpoint_pending_seconds':60,'guard_argv':['python','guard'],'plan_path':'/control/plan',
         'lease_path':'/control/lease/current'}
    binding={'capacity_guard':cap,'mount_identity':{'target':'/scratch/output'},'scratch_device':'2',
             'root_device':'1','backing_inode':'3','output_inode':'4','floor_bytes':80,'root_floor_bytes':20}
    lock={'inode':8}
    d['guard']={'binding':binding,'directory':'/control/storage','source':{'sha256':'source'},'reporting_lock':lock}
    lease={'schema':'causes.capacity_guard_lease.v1','binding':cap,'candidate':owner,
           'guard':{'pid':300,'process_starttime_ticks':'30','argv':cap['guard_argv']},
           'plan':{'path':cap['plan_path']},'state':'endpoint_pending','sequence':0,'previous':None,
           'issued_at':wall(.1),'expires_at':wall(60.1),'issued_monotonic_seconds':1000.1,
           'expires_monotonic_seconds':1000.1+60}
    lease=record('/control/lease/lease_00000000.json',lease)
    capacity={'status':'endpoint_pending','lease_state':'endpoint_pending','admission_allowed':False,
              'lease':lease['pin'],'candidate':owner,'guard':lease['value']['guard']}
    physical={'mount':binding['mount_identity'],'paths':{'backing':{'device':'2','inode':'3'},
        'mount_target':{'device':'2','inode':'3'},'output':{'device':'2','inode':'4'},'root_volume':{'device':'1'}},
        'output_free_bytes':100,'root_free_bytes':30,'capacity_guard':capacity}
    transition={'schema':'causes.storage_guard_transition.v1','at':wall(29),'sequence':0,'previous':None,
        'owner':owner,'binding_sha256':w.digest(binding),'source_sha256':'source','reporting_lock':lock,
        'state':'floor_wait','error':None,'observation':physical}
    transition=record('/control/storage/00000000.json',transition);transition['lease']=lease
    event={'controller_id':'current','event':'disk_floor_wait','at':wall(29.1),'free_bytes':0,'required_bytes':80}
    alarms={k:False for k in ['owner_identity_invalid','reporting_lock_invalid','guard_record_invalid',
        'guard_not_ready','storage_binding_invalid','volume_below_floor']}
    alarms['capacity_guard_not_ready']=True
    samples[1]['storage_guard']={'at':wall(30.01),'healthy':False,'alarms':alarms,'fresh_storage':copy.deepcopy(physical),
        'capacity_guard':copy.deepcopy(capacity),'latest_transition':transition['pin']}
    b['journals']=[journal(a['journals'][0]['events']+[event])]
    for sample in samples[1:]:sample['controllers'][0]['journal_sha256']=b['journals'][0]['sha256']
    evidence={'transitions':[transition],'leases':[lease]}
    return samples,a,b,window,d,evidence,event


class CapacityWaitTest(unittest.TestCase):
    def test_exact_pending_wait_is_supported_without_mutating_raw_health(self):
        s,a,b,window,d,e,event=waiting_fixture();before=copy.deepcopy((s,a,b))
        result=c.summarize_window(s,a,b,window,d,capacity_wait_evidence=e)
        self.assertTrue(result['support']['supported'])
        self.assertEqual(result['capacity_wait_support']['sample_indices'],[1])
        self.assertEqual(result['capacity_wait_support']['event_sha256'],[w.digest(event)])
        self.assertEqual((s,a,b),before)
        self.assertFalse(c.summarize_window(s,a,b,window,d)['support']['supported'])

    def test_expiry_or_other_alarm_is_never_exempted(self):
        for field,value in [('status','expired'),('other_alarm',True),('physical_floor',19),('late',61)]:
            with self.subTest(field=field):
                s,a,b,window,d,e,event=waiting_fixture();v=s[1]['storage_guard']
                if field=='status':v['capacity_guard']['status']=value
                elif field=='other_alarm':v['alarms']['guard_record_invalid']=True
                elif field=='physical_floor':v['fresh_storage']['root_free_bytes']=value
                else:s[1]['finished_at']=wall(value);s[1]['finished_monotonic_seconds']=1000+value
                with self.assertRaises((ValueError,KeyError)):
                    c.summarize_window(s,a,b,window,d,capacity_wait_evidence=e)

    def test_pin_chain_owner_and_mount_changes_refused(self):
        for kind in ['pin','owner','chain','mount','duration']:
            with self.subTest(kind=kind):
                s,a,b,window,d,e,event=waiting_fixture()
                if kind=='pin':e['transitions'][0]['pin']['sha256']='wrong'
                elif kind=='owner':d['processes'][0]['pid']=999
                elif kind=='chain':e['transitions'][0]['value']['previous']={'sha256':'wrong'}
                elif kind=='mount':s[1]['storage_guard']['fresh_storage']['mount']={'target':'/wrong'}
                else:e['leases'][0]['value']['expires_at']=wall(90.1)
                with self.assertRaises((ValueError,KeyError)):
                    c.summarize_window(s,a,b,window,d,capacity_wait_evidence=e)

    def test_latest_transition_and_exact_event_floor_required(self):
        s,a,b,window,d,e,event=waiting_fixture()
        newer=copy.deepcopy(e['transitions'][0]['value']);newer.update(at=wall(29.05),sequence=1,
            previous=e['transitions'][0]['pin'],state='ready')
        e['transitions'].append(record('/control/storage/00000001.json',newer))
        s[1]['storage_guard']['latest_transition']=e['transitions'][-1]['pin']
        with self.assertRaisesRegex(ValueError,'event_not_floor_transition'):
            c.summarize_window(s,a,b,window,d,capacity_wait_evidence=e)
        s,a,b,window,d,e,event=waiting_fixture();event['free_bytes']=1
        with self.assertRaisesRegex(ValueError,'event_identity'):
            w.qualify_capacity_waits(s,[event],d,e)

    def test_passive_declaration_cannot_use_exception(self):
        s,a,b,window,d,e,event=waiting_fixture();d['owner']['max_full']=2
        with self.assertRaisesRegex(ValueError,'candidate_capacity_wait_only'):
            c.summarize_window(s,a,b,window,d,capacity_wait_evidence=e)

    def test_pause_event_remains_unsupported(self):
        s,a,b,window,d,e,event=waiting_fixture()
        b['journals']=[journal(b['journals'][0]['events']+[{'event':'pause_seen','controller_id':'current','at':wall(50)}])]
        for sample in s[2:]:sample['controllers'][0]['journal_sha256']=b['journals'][0]['sha256']
        result=c.summarize_window(s,a,b,window,d,capacity_wait_evidence=e)
        self.assertFalse(result['support']['supported'])

    def test_missing_or_nonboolean_alarm_field_refused(self):
        for kind in ['missing', 'nonbool']:
            s,a,b,window,d,e,event=waiting_fixture()
            if kind=='missing':s[1]['storage_guard']['alarms']={'capacity_guard_not_ready':True}
            else:s[1]['storage_guard']['alarms']['guard_record_invalid']=0
            with self.assertRaisesRegex(ValueError,'alarm_schema'):
                c.summarize_window(s,a,b,window,d,capacity_wait_evidence=e)

    def test_newer_fault_cannot_hide_behind_stale_sample_pointer(self):
        s,a,b,window,d,e,event=waiting_fixture()
        newer=copy.deepcopy(e['transitions'][0]['value']);newer.update(at=wall(29.5),sequence=1,
            previous=e['transitions'][0]['pin'],state='guard_fault',error='physical_error',observation=None)
        e['transitions'].append(record('/control/storage/00000001.json',newer))
        with self.assertRaisesRegex(ValueError,'sample_transition_not_latest'):
            c.summarize_window(s,a,b,window,d,capacity_wait_evidence=e)

    def test_malformed_transition_disposition_refused(self):
        for state,error,observation in [('guard_fault',None,None),('ready','wrong',{}),('floor_wait',None,None)]:
            s,a,b,window,d,e,event=waiting_fixture()
            malformed=copy.deepcopy(e['transitions'][0]['value']);malformed.update(state=state,error=error,observation=observation)
            e['transitions'][0]=record('/control/storage/00000000.json',malformed)
            with self.assertRaisesRegex(ValueError,'transition_disposition'):
                c.summarize_window(s,a,b,window,d,capacity_wait_evidence=e)
