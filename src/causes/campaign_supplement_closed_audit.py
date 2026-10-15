"""Once-authorized saved custody for an exact completed-actor plan and original bank.

No host or model calls. Native request rendering/parser and final verification
remain unchanged. Provider and guard checks use their retained original clocks.
All output bytes are buffered and sized before the output directory is created.
"""
import argparse,base64,copy,datetime as dt,fcntl,hashlib,json,os,shlex,stat,sys,types
from pathlib import Path

def require(ok,why):
    if not ok:raise ValueError(why)
def encoded(x):return (json.dumps(x,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
def stamp(x):
    v=dt.datetime.fromisoformat(x);require(v.tzinfo is not None,'aware_clock');return v
def pin(path):
    p=Path(path);a=p.stat();b=p.read_bytes();z=p.stat()
    require(p.is_absolute() and p.resolve()==p and not p.is_symlink() and stat.S_ISREG(a.st_mode),'pin_path')
    require((a.st_dev,a.st_ino,a.st_size,a.st_mtime_ns)==(z.st_dev,z.st_ino,z.st_size,z.st_mtime_ns),'pin_changed')
    return dict(path=str(p),bytes=len(b),sha256=hashlib.sha256(b).hexdigest())
def raw(ref):
    require(pin(ref['path'])==ref,'pin_mismatch');return Path(ref['path']).read_bytes()
def checked(ref):return json.loads(raw(ref))
def module(ref,name):
    b=raw(ref);m=types.ModuleType(name);m.__file__=ref['path'];sys.modules[name]=m
    exec(compile(b,ref['path'],'exec'),m.__dict__);require(raw(ref)==b,'loaded_source_changed');return m
def clone(fn,**changes):
    return types.FunctionType(fn.__code__,dict(fn.__globals__,**changes),fn.__name__,fn.__defaults__,fn.__closure__)
def blob(x,limit):
    b=base64.b64decode(x['base64'],validate=True)
    require(len(b)<=limit and len(b)==x['bytes'] and hashlib.sha256(b).hexdigest()==x['sha256'],'saved_blob');return b
def tree(root):
    rows=[]
    for p in sorted(Path(root).rglob('*')):
        info=p.lstat();require(not stat.S_ISLNK(info.st_mode),'tree_link')
        if stat.S_ISREG(info.st_mode):require(info.st_size<=16*1024**2,'file_bound');rows.append(pin(p))
        else:require(stat.S_ISDIR(info.st_mode),'tree_device')
    return rows
def size(root):return sum(p.stat().st_size for p in Path(root).rglob('*')if p.is_file())
def absent(expected):
    p=Path('/proc')/str(expected['pid'])/'stat'
    try:b=p.read_bytes()
    except FileNotFoundError:return dict(expected=expected,absent=True,reason='pid_absent')
    require(len(b)<=16384,'stat_bound');fields=b.decode().rsplit(')',1)[1].split()
    old=expected.get('process_starttime_ticks',expected.get('start_ticks'))
    require(fields[19]!=old or fields[0]in('Z','X','x'),'owned_process_still_live')
    return dict(expected=expected,absent=True,current_start_ticks=fields[19],current_state=fields[0],reason='old_process_exited_or_pid_reused')
def client_overlap(exchanges,healthy):
    changes=[]
    for row in exchanges:
        if row['measurement_id']in healthy:
            require(stamp(row['receipt']['finished_at'])>=stamp(row['receipt']['started_at']),'http_clock_order')
            if row['receipt']['finished_at']==row['receipt']['started_at']:continue
            changes.extend([(stamp(row['receipt']['started_at']),1,row['measurement_id']),
                            (stamp(row['receipt']['finished_at']),-1,row['measurement_id'])])
    active=set();peak=[];at=None
    for when,kind,mid in sorted(changes):
        if kind<0:active.remove(mid)
        else:active.add(mid)
        if len(active)>len(peak):peak=sorted(active);at=when.isoformat()
    return dict(maximum_observed_http_clients=len(peak),at=at,measurement_ids=peak,
                scope='retained successful HTTP intervals',gpu_execution_overlap_established=False)


def durable_chain(recipe,p):
    dur=Path(recipe['durable_root']);out=Path(p['output_root']);refs=[]
    def read(ref):refs.append(ref);return checked(ref)
    def at(path):return read(pin(path))
    require(recipe['complete']['path']==str(dur/'COMPLETE.json')and recipe['driver_result']['path']==str(out/'DRIVER_RESULT.json'),'actual_result_paths')
    complete=read(recipe['complete']);packet=read(recipe['packet']);adoption=read(recipe['adoption']);submission=read(recipe['submission'])
    require(complete['all_actors_succeeded']is True and complete['launch_error']is None and complete['automatic_retry']is False and len(complete['executions'])==1,'genuine_complete')
    require(packet['output_directory']==str(dur)and len(packet['actors'])==1,'exact_durable_packet')
    actor=packet['actors'][0];require(actor['name']=='mi'and actor['source']==p['sources']['driver'],'exact_durable_actor')
    for ref in actor['pins']:raw(ref);refs.append(ref)
    raw(packet['supervisor_source']);refs.append(packet['supervisor_source'])
    require(adoption['root_authorized']is True and adoption['packet']==recipe['packet']and adoption['plan']==recipe['plan']and adoption['attempt_id']==packet['attempt_id']and adoption['maximum_invocations_per_actor']==1 and adoption['automatic_retry']is False,'durable_adoption')
    require(submission['packet']==recipe['packet']and submission['adoption']==recipe['adoption']and submission['plan']==recipe['plan']and submission['driver_authority']==adoption['driver_authority']and submission['complete']is True and submission['returncode']==0 and submission['error']is None and submission['automatic_retry']is False and raw(submission['stderr'])==b'','genuine_submission')
    supervisor=at(dur/'INTENT.json');require(supervisor['packet']==recipe['packet']and supervisor['adoption']==recipe['adoption']and supervisor['attempt_id']==packet['attempt_id']and supervisor['automatic_retry']is False,'supervisor_intent')
    er=complete['executions'][0];require(er['path']==str(dur/'mi/EXECUTION.json'),'execution_path');ex=read(er)
    require(ex['actor']=='mi'and ex['returncode']==0 and ex['source_stable']is True and ex['source_error']is None and ex['automatic_retry']is False and ex['signals_sent']is False,'native_child_success')
    require(ex['intent']['path']==str(dur/'mi/INTENT.json')and ex['stdout']['path']==str(dur/'mi/stdout')and ex['stderr']['path']==str(dur/'mi/stderr')and raw(ex['stderr'])==b'','native_stream_paths')
    intent=read(ex['intent']);require(intent['actor']==actor and intent['packet']==recipe['packet']and intent['adoption']==recipe['adoption']and intent['attempt_id']==packet['attempt_id']and intent['automatic_retry']is False,'actor_intent')
    owner=read(recipe['owner']);invocation=at(out/'INVOCATION.json');process=at(dur/'mi/PROCESS.json')
    argv=actor['argv'];require(argv.count('--plan')==argv.count('--authority')==1 and json.loads(argv[argv.index('--plan')+1])==recipe['plan']and json.loads(argv[argv.index('--authority')+1])==adoption['driver_authority'],'exact_actor_argv')
    own=owner['process'];start=argv.index(p['sources']['driver']['path'])-2
    require(start>=0 and argv[start+1]=='-B'and argv[start:]==own['argv']and owner['plan']==recipe['plan']and owner['authority']==adoption['driver_authority']and invocation['plan']==recipe['plan']and invocation['authority']==owner['authority'],'driver_owner_invocation')
    pi=process['identity'];require(process['intent']==ex['intent']and pi['pid']==own['pid']and pi['start_ticks']==own['process_starttime_ticks']and pi['argv']in(argv,own['argv']),'durable_process_identity')
    stdout=raw(ex['stdout']);require(json.loads(stdout)==dict(result=recipe['driver_result'],complete=True),'driver_stdout_result')
    result=read(recipe['driver_result']);require(result['result']['active']==0 and type(result['result']['consecutive_failures'])is int and result['result']['consecutive_failures']>=0,'native_closed')
    require(stamp(intent['at'])<=stamp(ex['started_at'])<=stamp(invocation['at'])<=stamp(result['at'])<=stamp(ex['at'])<=stamp(complete['at']),'durable_original_clock_order')
    return complete,ex,result,owner,supervisor['supervisor'],refs+[ex['stdout'],ex['stderr'],submission['stderr']]

def native_summary(events,result,p,expected_stop):
    starts=[r for r in events if r['event']=='controller_started'];ends=[r for r in events if r['event']=='controller_finished']
    require(len(starts)==len(ends)==1 and starts[0]is events[0]and ends[0]is events[-1],'single_native_controller')
    start=starts[0];require(start['phase']==p['mode']and start['existing_finals']==0 and start['pending']==p['maximum_new_requests'] and start['inherited_failures']==p['initial_failures']and start['max_clients']==8 and start['max_new_requests']==p['maximum_new_requests'] and start['queue_prefix_count']==p['maximum_new_requests'] and start['capacity_limit']==p['capacity_limit']and start['local_free_space_floor_bytes']==p['local_free_space_floor_bytes'],'native_controller_start')
    require(all(ends[0][k]==v for k,v in result.items())and result['phase']==p['mode']and result['existing_final']==0 and result['active']==0 and result['stop_reason']==expected_stop and result['local_free_space_floor_bytes']==p['local_free_space_floor_bytes'],'native_controller_end')
    counter=p['initial_failures'];terminals=[];claims=[];retained=[]
    for row in events:
        if row['event']=='claimed':claims.append(row['measurement_id'])
        if row['event']in('route_success','route_failure'):
            counter=0 if row['event']=='route_success'else counter+1
            require(row['consecutive_failures']==counter,'native_route_counter');terminals.append(row['measurement_id'])
        if row['event']in('route_quarantined','admission_held','unresolved_prior_attempt','unresolved_current_attempt'):retained.append(row)
    require(len(claims)==len(set(claims))and len(terminals)==len(set(terminals))and set(terminals)<=set(claims),'native_terminal_partition')
    require(result['consecutive_failures']==counter and result['completed_ids']==terminals and result['new_final']==len(terminals)and result['remaining_unclaimed']==p['maximum_new_requests']-len(claims),'native_final_counters_and_order')
    quarantined=[r for r in retained if r['event']!='admission_held']
    if quarantined or counter>=3:require(result['stop_reason']is not None,'fault_without_stop')
    return dict(consecutive_failures=counter,events=retained,quarantined=bool(quarantined),unresolved_claimed_ids=[mid for mid in claims if mid not in set(terminals)],technical_failures=sum(r['event']=='route_failure'for r in events))

def lock_identity(stream,desc):
    opened=os.fstat(stream.fileno());named=Path(desc['path']).stat()
    require(stat.S_ISREG(opened.st_mode)and stat.S_ISREG(named.st_mode)and (opened.st_dev,opened.st_ino)==(named.st_dev,named.st_ino)==(desc['device'],desc['inode']),'held_lock_identity')

class SavedAudit:
    def __init__(self,plan,runtime,mods,journal,records):
        self.plan=plan;self.runtime=runtime;self.mods=mods;self.core=mods['core'];self.direct=mods['direct']
        self.contract=checked(plan['contract']);self.journal=journal;self.records=records;self.pins={};self.prefixes={}
        self.remote_cache={};self.guard_cache={};self.provider_cache={}
        self.record_roster={r['path']:r for r in records};self.used_capture_novelty={}
        self.provider_plan=checked(plan['provider_intent'])['request']['plan']
        self.native=module(self.provider_plan['native_source'],'_saved_direct_provider_native')
        self.retired=checked(plan['completed_actor_custody']['actual'])['retired_actors']
        self.transport=mods.get('transport') if 'control_transport' in plan else None
        self.control_ready_refs=[];self.control_intent_times=[]
        if 'control_transport' in plan:
            require(self.transport is not None and pin(self.transport.__file__)==plan['sources']['control_transport'],'exact_saved_transport_module')
            self.transport.validate_plan(plan,read=self.read)
    def record(self,value,read=False):
        ref=value if type(value)is dict else pin(Path(value))
        if ref['path']==str(Path(self.plan['output_root'])/'.control/journal.jsonl') and ref['bytes']<len(self.journal):
            b=self.journal[:ref['bytes']];require(b.endswith(b'\n')and hashlib.sha256(b).hexdigest()==ref['sha256'],'historical_journal_prefix')
            self.prefixes[(ref['bytes'],ref['sha256'])]=ref
        else:
            b=raw(ref);old=self.pins.get(ref['path']);require(old is None or old==ref,'conflicting_pin');self.pins[ref['path']]=ref
        return b if read else None
    def read(self,ref):return self.record(ref,True)
    def value(self,ref):return json.loads(self.read(ref))
    def registered(self,ref,kind):
        path=Path(ref['path'])
        require(path.parent==Path(self.plan['output_root'])/'admission_records'and path.name.split('_',1)[1]==kind and self.record_roster.get(ref['path'])==ref,'unregistered_admission_record')
    def candidate_accounting(self,candidates):
        numbers={};novelty={}
        for ref in candidates:
            self.registered(ref,'candidate');v=self.value(ref);self.registered(v['provider'],'capture');self.registered(v['guard'],'guard')
            provider=self.value(v['provider']);number=v['capture_number']
            require(provider['number']==number and (number not in numbers or numbers[number]==v['provider']),'capture_number_reference')
            numbers[number]=v['provider'];body=provider['value']
            key=self.core.digest([body['identity'],body['process'],body['observed_from'],body['observed_until']])
            require(key not in novelty or novelty[key]==number,'renumbered_capture_novelty');novelty[key]=number
        return dict(used_capture_numbers=sorted(numbers),distinct_original_captures=len(novelty))
    def admission(self,**callbacks):
        defaults=dict(capture=lambda:None,guard=lambda phase:None,retain=lambda kind,value:None,
            source_check=lambda job:None,prepare_request=lambda job:None,read=self.read)
        defaults.update(callbacks);return self.direct.Admission(self.runtime,self.contract,**defaults)
    def kernel(self,value):
        k=value['kernel'];saved=[k['anchor'],k['window']];seen=[]
        def command(argv):
            i=len(seen);require(i<2 and argv==saved[i]['argv'],'kernel_exact_argv');r=saved[i]
            require(r['returncode']==0 and blob(r['stderr'],65536)==b'','kernel_transport')
            data=blob(r['stdout'],16*1024**2);seen.append(True);return r,data
        fn=clone(self.native.kernel,successful_command=command,now=lambda:k['until'])
        require(fn(self.provider_plan)==k and len(seen)==2,'native_kernel_reconstruction')
        require(k['error_records']==[] and k['since']==self.provider_plan['kernel_since'],'kernel_clean')
        resources=value['resources'];require(resources['available_bytes']>=self.provider_plan['remote_floor_bytes'],'provider_storage_floor')
        for key,limit in [('meminfo',65536),('provider_status',65536),('provider_cgroup',16384)]:blob(resources[key],limit)
    def remote_intent(self,intent):
        p=self.plan;require(type(intent['full'])is bool,'remote_full_type')
        refs=[p['sources'][key]for key in ['remote','provider_status','collector','exact_bytes']]+[p['provider_intent'],self.provider_plan['native_source']]
        transport_argv=None
        if 'control_transport' in p:
            more,transport_argv=self.transport.saved_call(p,intent['control_transport'],read=self.read);refs+=more
            self.control_ready_refs.append(intent['control_transport']['ready']);self.control_intent_times.append(intent['at'])
        else:require('control_transport' not in intent,'undeclared_transport')
        require(intent['source']==p['sources']['remote']and intent['sources']==refs and intent['maximum_invocations']==1 and intent['automatic_retry']is False,'remote_exact_source_roster')
        for ref in refs:self.record(ref)
        envelope=dict(provider_plan=self.provider_plan,runtime_identity=p['provider_identity'],helper_source=p['sources']['provider_status'],helper_base64=base64.b64encode(self.read(p['sources']['provider_status'])).decode(),native_base64=base64.b64encode(self.read(self.provider_plan['native_source'])).decode(),retired_actors=self.retired,remote_pause_paths=p['remote_pause_paths'],full=intent['full'])
        payload=encoded(envelope)[:-1]
        argv=p['ssh_argv']+[p['provider_interpreter']+' -c '+shlex.quote(self.read(p['sources']['remote']).decode())]
        if transport_argv is not None:
            require(transport_argv[-1]==argv[-1],'transport_remote_command_changed');argv=transport_argv
        require(intent['argv']==argv and intent['payload_bytes']==len(payload)and intent['payload_sha256']==hashlib.sha256(payload).hexdigest(),'remote_exact_argv_payload')
    def remote_accounting(self,files,number,control_closed=None):
        root=Path(self.plan['output_root'])/'remote_records';groups={}
        for ref in files:
            path=Path(ref['path'])
            if root in path.parents:
                require(path.parent.parent==root,'remote_unexpected_nested_path');groups.setdefault(path.parent.name,[]).append(ref)
        require(type(number)is int and 0<=number<=2*self.plan['maximum_admission_attempts']+self.plan['maximum_captures']and sorted(groups)==['call_%06d'%i for i in range(1,number+1)],'remote_call_count_sequence')
        rows=[];full=0
        for name,refs in sorted(groups.items()):
            refs.sort(key=lambda r:r['path']);names=[Path(r['path']).name for r in refs]
            require(names[:4]==['000001_intent','000002_stdout','000003_stderr','000004_execution']and len(names)in(4,5)and(len(names)==4 or names[4]=='000005_exact_gzip'),'remote_file_sequence')
            intent=self.value(refs[0]);self.remote_intent(intent);ex=self.value(refs[3]);full+=int(intent['full'])
            require(ex['intent']==refs[0]and ex['output']==refs[1]and ex['stderr']==refs[2]and ex['automatic_retry']is False and ex['model_calls']==0,'remote_saved_execution_roster')
            if len(refs)==5:require(intent['full'],'gzip_in_status_call')
            rows.append(dict(directory=name,full=intent['full'],intent=refs[0],execution=refs[3],complete=ex['transport']['complete'],returncode=ex['transport']['returncode'],source_error=ex['source_error'],all_files=refs))
        require(full<=self.plan['maximum_captures'],'remote_full_budget')
        result=dict(calls=number,full_calls=full,rows=rows,failed_and_unclaimed_calls_retained=True)
        if 'control_transport' in self.plan:
            custody=self.transport.saved_closed(self.plan,control_closed,self.control_ready_refs,read=self.read)
            require(all(stamp(custody['ready_at'])<=stamp(t)<=stamp(custody['stopped_at']) for t in self.control_intent_times),'transport_call_outside_owned_lifetime')
            result['control_transport']=custody
        else:require(control_closed is None,'undeclared_transport_closure')
        return result
    def remote(self,evidence):
        executions=[r for r in evidence if Path(r['path']).name.endswith('_execution')]
        require(len(executions)==1,'remote_execution_count');er=executions[0]
        for ref in evidence:self.record(ref)
        if er['path']in self.remote_cache:return self.remote_cache[er['path']]
        ex=self.value(er);intent=self.value(ex['intent']);t=ex['transport']
        require(t['complete']is True and t['returncode']==0 and self.read(ex['stderr'])==b'' and ex['source_error']is None and ex['automatic_retry']is False,'remote_transport')
        require(intent['source']==self.plan['sources']['remote']and intent['maximum_invocations']==1 and intent['automatic_retry']is False,'remote_source_intent')
        self.remote_intent(intent)
        require(ex['model_calls']==0,'remote_model_calls')
        wire=self.value(ex['output']);full=intent['full']
        if full:
            require(wire['schema']=='causes.completed_actor_exact_gzip_wire.v1','wire_schema')
            packed=base64.b64decode(wire['base64'],validate=True)
            require(wire['stored']==dict(bytes=len(packed),sha256=hashlib.sha256(packed).hexdigest()),'wire_stored')
            gz=[r for r in evidence if Path(r['path']).name.endswith('_exact_gzip')];require(len(gz)==1 and self.read(gz[0])==packed,'exact_stored_gzip')
            value=json.loads(self.mods['exact'].read_gzip(dict(schema='shared.exact_gzip_bytes.v1',stored=gz[0],raw=wire['raw']),limit=32*1024**2))
        else:value=wire
        require(value['schema']=='causes.completed_actor_mi_remote_observation.v1'and value['full']is full and value['healthy']is True and value['errors']==[],'remote_value')
        clocks=[stamp(intent['at']),stamp(value['observed_from']),stamp(value['status']['observed_from']),stamp(value['status']['observed_until']),stamp(value['observed_until']),stamp(ex['at'])]
        require(clocks==sorted(clocks),'remote_original_clocks')
        self.mods['status'].validate_status(value['status'],self.provider_plan,self.plan['provider_identity'])
        require(value['controls']=={p:False for p in self.plan['remote_pause_paths']},'remote_controls')
        require(value['actors_before']==value['actors_after'] and len(value['actors_before'])==len(self.retired),'retired_actor_roster')
        for got,expected in zip(value['actors_before'],self.retired):
            require(got['process']['expected']==expected['process']and got['process']['absent']is True,'actor_absence')
            require(got['lock']==dict(lock=expected['executor_lock'],acquired_nonblocking=True,released=True)and got['journal']==expected['journal']and got['retirement_pause']==expected['retirement_pause'],'actor_retirement_evidence')
        if full:self.kernel(value)
        summary={k:value[k]for k in ('full','observed_from','observed_until','status')}
        summary['kernel_error_records']=[]if full else None;self.remote_cache[er['path']]=summary;return summary
    def local(self,value):
        for side in ['before','after']:
            row=value[side];require(row['owner']==self.runtime['owner_identity'],'local_owner')
            require(row['pauses']=={p:False for p in self.plan['pause_paths']},'local_pause')
            require(row['forward']['process']==self.plan['forward_process']and row['free_bytes']>=self.plan['local_free_space_floor_bytes'],'forward_or_local_space')
            listener=row['forward']['listener'];require(listener['local']=='0100007F:%04X'%self.plan['forward_port']and listener['state']=='0A'and type(listener['inode'])is str and listener['inode'].isdigit()and int(listener['inode'])>0,'forward_listener')
            s=row['storage'];n=row['native'];r=self.plan['retention']
            require(s['claimed']==n['claimed']and s['maximum_claims']==self.plan['maximum_new_requests'] and 0<=n['claimed']<self.plan['maximum_new_requests'] and 0<=n['active']<8 and n['quarantined']is False and 0<=n['consecutive_failures']<3,'local_native_state')
            require(s['reserve_bytes']==r['reserve_bytes']and s['maximum_retained_bytes']==r['role_maximum_retained_bytes']and s['maximum_file_bytes']==r['maximum_file_bytes']and s['maximum_written_files']==r['maximum_written_files']and s['retained_bytes']+s['reserve_bytes']<=s['maximum_retained_bytes'],'local_retention')
            if n['journal']is not None:
                prefix=self.read(n['journal']);events=[json.loads(x)for x in prefix.splitlines()]
                claims=[x['measurement_id']for x in events if x['event']=='claimed'];term=[x for x in events if x['event']in('route_success','route_failure')]
                require(n['claimed']==len(claims)and n['active']==len(claims)-len(term)and n['consecutive_failures']==(term[-1]['consecutive_failures']if term else self.plan['initial_failures']),'native_prefix_state')
            require(len(row['locks'])==len(self.runtime['exclusive_locks']),'local_lock_roster')
            for proof,desc in zip(row['locks'],self.runtime['exclusive_locks']):
                d=proof['descriptor'];require(proof['accepted']is True and all(d[k]==desc[k]for k in ('path','device','inode')),'flock_descriptor')
                a=self.mods['flock'].parse_fdinfo(proof['fdinfo_before'],row['owner'],d);b=self.mods['flock'].parse_fdinfo(proof['fdinfo_after'],row['owner'],d)
                require(a==b and proof['locks']==[a]and proof['process_before']==proof['process_after']and proof['named_before']==proof['named_after']and proof['fd_before']==proof['fd_after'],'flock_saved_proof')
        require(value['before']['native']==value['after']['native'],'guard_state_transition')
    def guard(self,ref):
        if ref['path']in self.guard_cache:return self.guard_cache[ref['path']]
        value=self.value(ref);v=value['value'];require('clock_error'not in value,'guard_clock_error')
        times=iter([value['callback_from'],value['callback_until']])
        a=self.admission(guard=lambda phase:copy.deepcopy(v),clock=lambda:next(times),retain=lambda kind,x:ref if kind=='guard'and x==value else None)
        state,got,epoch=a._guard(value['phase']);require(got==ref,'guard_ref')
        local=[r for r in v['evidence']if Path(r['path']).name.endswith('_local_guard')];require(len(local)==1,'local_guard_count')
        l=self.value(local[0]);require(l['phase']==value['phase']and l['observed_from']==v['observed_from']and l['observed_until']==v['observed_until'],'local_guard_join');self.local(l)
        require(v['consecutive_failures']==l['after']['native']['consecutive_failures']and v['quarantined']==l['after']['native']['quarantined']and v['running_requests']<=self.plan['max_clients'],'private_guard_counter_pressure')
        remote=self.remote(l['remote']);require(remote['full']is False,'guard_status_mode')
        require(all(v[k]==remote['status'][k]for k in ('identity','running_requests','waiting_requests','preemptions')),'guard_status_projection')
        clocks=[stamp(v['observed_from']),stamp(remote['observed_from']),stamp(remote['observed_until']),stamp(v['observed_until'])];require(clocks==sorted(clocks),'guard_remote_clocks')
        self.guard_cache[ref['path']]=(v,epoch);return v,epoch
    def provider(self,ref):
        if ref['path']in self.provider_cache:return self.provider_cache[ref['path']]
        row=self.value(ref);v=row['value'];require('clock_error'not in row,'capture_clock_error')
        a=self.admission();a._pressure(v);require(v['running_requests']<=self.plan['max_clients'],'private_capture_pressure')
        require(v['schema']==self.direct.PROVIDER_SCHEMA and v['healthy']is True and v['kernel_error_records']==[],'provider_normalized')
        require(stamp(row['callback_from'])<=stamp(v['observed_from'])<=stamp(v['observed_until'])<=stamp(row['callback_until']),'provider_callback_clock')
        a._age(v,row['callback_until']);remote=self.remote(v['evidence']);require(remote['full']is True,'full_provider_capture')
        require(v['observed_from']==remote['observed_from']and v['observed_until']==remote['observed_until'],'original_capture_times')
        require(all(v[k]==remote['status'][k]for k in ('identity','running_requests','waiting_requests','preemptions')),'capture_status_projection')
        self.provider_cache[ref['path']]=v;return v
    def claim(self,job,claim,reference,expired):
        v=self.value(reference);require(v['schema']==self.direct.ADMISSION_SCHEMA,'admission_schema')
        for key,kind in [('candidate','candidate'),('provider','capture'),('guard','guard'),('claim_ready','ready')]:self.registered(v[key],kind)
        candidate=self.value(v['candidate']);require(candidate=={k:x for k,x in v.items()if k not in('candidate','claim_ready')},'native_candidate_copy')
        require(v['admission_attempt']not in expired,'expired_candidate_claimed')
        provider=self.provider(v['provider']);guard,epoch=self.guard(v['guard'])
        require(self.value(v['provider'])['number']==v['capture_number'] and self.value(v['guard'])['phase']=='admission','capture_number_or_guard_phase')
        require(epoch==v['epoch']and v['identity']==self.plan['provider_identity']and v['observed_from']==provider['observed_from']and v['observed_until']==provider['observed_until'],'candidate_provider_epoch')
        require(1<=v['capture_use']<=self.plan['maximum_capture_uses'] and 1<=v['capture_number']<=self.plan['maximum_captures'] and 1<=v['admission_attempt']<=self.plan['maximum_admission_attempts'],'candidate_budgets')
        ready=self.value(v['claim_ready']);require(ready['candidate']==v['candidate']and ready['measurement_id']==job['measurement_id']and ready['input_sha256']==self.core.digest(job),'ready_job_identity')
        self.registered(ready['guard'],'guard')
        state,ready_epoch=self.guard(ready['guard']);require(self.value(ready['guard'])['phase']=='claim_ready'and ready_epoch==epoch and stamp(ready['at'])>=stamp(state['observed_until'])and stamp(claim['at'])>=stamp(ready['at']),'ready_epoch_clock')
        a=self.admission();a.cached=provider;a._verify_candidate(v);a._age(v,ready['at']);a._age(v,claim['at'],ready=False)
        return dict(measurement_id=job['measurement_id'],native_observation=reference,candidate=v['candidate'],ready=v['claim_ready'],provider=v['provider'],admission_attempt=v['admission_attempt'],capture_number=v['capture_number'],capture_use=v['capture_use'],claimed_at=claim['at'],original_age_seconds=(stamp(claim['at'])-stamp(v['observed_from'])).total_seconds(),qualified=True)

def buffered_evidence(name,value,destination,outputs,codec,sizes,*,raw_limit=32*1024**2,plain_limit=16*1024**2):
    require(type(raw_limit)is int and 0<raw_limit<=512*1024**2 and type(plain_limit)is int and 0<plain_limit<=128*1024**2,'explicit_evidence_limits')
    data=value if type(value)is bytes else encoded(value);original=len(data);encoding='identity'
    require(original<=raw_limit,'raw_evidence_bound')
    def retained(path,body):
        require(path not in outputs,'duplicate_buffered_output');outputs[path]=body
        return dict(path=str(path),bytes=len(body),sha256=hashlib.sha256(body).hexdigest())
    if name in ('VERIFIED_FINALS.v1.json','CANDIDATE_DISPOSITIONS.v1.json','SOURCE_PINS.v1.json','ADMISSION_AUDIT.v1.json','CAPACITY_AND_THROUGHPUT.v1.json'):
        record=clone(codec.write_gzip,_write=lambda path,body:retained(Path(path),body))(destination/(name+'.gz'),data,limit=raw_limit)
        def read(ref,**kwargs):
            body=outputs[Path(ref['path'])];require(ref==dict(path=ref['path'],bytes=len(body),sha256=hashlib.sha256(body).hexdigest()),'buffered_gzip_pin');return body
        require(clone(codec.read_gzip,read_pinned=read)(record,limit=raw_limit)==data,'buffered_exact_gzip_roundtrip')
        data=encoded(record);encoding='shared.exact_gzip_bytes.v1'
        sizes.append(dict(name=name,raw_bytes=original,gzip_bytes=record['stored']['bytes'],wrapper_bytes=len(data),encoding=encoding))
    else:
        require(len(data)<=plain_limit,'plain_metadata_file_bound')
        sizes.append(dict(name=name,raw_bytes=original,stored_bytes=len(data),encoding=encoding))
    return retained(destination/name,data)

def derived_final_verifier(recipe,plan,scope,mods,collector,record):
    """Bind the accepted derived verifier to its frozen scientific module graph.

    Queue migration remains separately checked by the runtime bridge. Only the
    execution endpoint may differ; neither frozen jobs nor protocol hashes are
    relabeled. This setup performs no request, observation or output write.
    """
    require('derived_bank' in plan and 'derived_bank' in scope,'explicit_derived_final_scope')
    peer=checked(recipe['derived_collector_review'])
    require(peer.get('accepted_derived_probe_collect_source')is True and peer.get('findings')==[]
        and peer['source']==recipe['derived_collector']and peer['collector']==recipe['native_collector']
        and peer['input_adapter']==mods['scientific_sources']['derived_probe_inputs']
        and peer['modules']=={'causes.campaign_supplement_collect':recipe['native_collector'],
                             'causes.derived_probe_collect':recipe['derived_collector']},'derived_collector_source_peer')
    integrity_refs=[recipe['derived_collector_review'],recipe['derived_collector'],recipe['native_collector'],
                    plan['execution_protocol'],*mods['scientific_sources'].values()]
    for ref in integrity_refs:record(ref)
    package=sys.modules['causes'];package.campaign_supplement_collect=collector
    package.derived_probe_inputs=mods['adapter'];sys.modules['causes.derived_probe_inputs']=mods['adapter']
    derived=module(recipe['derived_collector'],'causes.derived_probe_collect')
    require(derived.collector is collector and derived.derived is mods['adapter']and
            collector.core is mods['core'],'derived_collector_module_graph')
    resolved=scope['derived_bank']
    def source_check(job):
        for ref in integrity_refs:record(ref)
    probe=mods['bridge'].FrozenProbeAdapter(mods['adapter'],mods['core'],resolved['protocol'],
        resolved['row_by_job'],scientific_source=mods['scientific_sources']['campaign_supplement'],
        operational_source=plan['sources']['native_queue'],source_check=source_check,
        operational_review=mods['constructor'].get('operational_queue_review'),
        execution_protocol=checked(plan['execution_protocol']),read=raw)
    require(probe.config['endpoint']==plan['endpoint'],'derived_closed_endpoint')
    def verify(job,terminal):
        require(job['measurement_id']in resolved['row_by_job'],'derived_closed_unknown_job')
        source_check(job)
        try:
            return derived.verify_final(job,terminal,root=Path(plan['output_root']),
                row=resolved['row_by_job'][job['measurement_id']],protocol=probe.protocol,record=record)
        finally:source_check(job)
    return verify

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--recipe',required=True);parser.add_argument('--authority',required=True);args=parser.parse_args()
    recipe_ref=json.loads(args.recipe);authority_ref=json.loads(args.authority);recipe=checked(recipe_ref);authority=checked(authority_ref)
    require(authority['root_authorized']is True and authority['source']==pin(__file__)and authority['recipe']==recipe_ref and authority['maximum_invocations']==1 and authority['automatic_retry']is False,'once_authority')
    require(stamp(authority['not_before'])<=dt.datetime.now(dt.timezone.utc)<=stamp(authority['expires_at']),'authority_time')
    allocation=checked(recipe['output_allocation'])
    require(allocation['schema']=='causes.saved_supplement_closure_output_reservation.v1'and allocation['root_authorized']is True and allocation['purpose']=='saved_closure_metadata'and authority['output_allocation']==recipe['output_allocation'],'saved_output_allocation')
    require(type(recipe['maximum_output_bytes'])is int and 0<recipe['maximum_output_bytes']<=allocation['maximum_output_bytes']<=512*1024**2 and authority['maximum_output_bytes']==recipe['maximum_output_bytes'],'allocated_output_bound')
    require(type(recipe['maximum_evidence_raw_bytes'])is int and 0<recipe['maximum_evidence_raw_bytes']<=allocation['maximum_evidence_raw_bytes']<=512*1024**2 and type(recipe['maximum_plain_file_bytes'])is int and 0<recipe['maximum_plain_file_bytes']<=allocation['maximum_plain_file_bytes']<=128*1024**2,'allocated_file_bounds')
    require(type(allocation['maximum_complete_bytes'])is int and 0<allocation['maximum_complete_bytes']<=512*1024**2 and type(allocation['future_margin_bytes'])is int and allocation['future_margin_bytes']>=65536,'allocated_root_bound')
    p=checked(recipe['plan']);out=Path(p['output_root']);dur=Path(recipe['durable_root']);destination=Path(recipe['output_directory']);require(not destination.exists(),'fresh_audit_output')
    allocation_root=Path(allocation['output_root']);require(allocation_root.is_absolute()and allocation_root.resolve()==allocation_root and destination.is_absolute()and destination.resolve()==destination and destination.parent==allocation_root,'allocated_output_path')
    complete,ex,result_wrapper,owner,supervisor,durable_refs=durable_chain(recipe,p);result=result_wrapper['result']
    absence=[absent(owner['process']),absent(supervisor)]
    locks=[]
    try:
        for desc in p['exclusive_locks']:
            path=Path(desc['path']);info=path.stat();require((info.st_dev,info.st_ino)==(desc['device'],desc['inode']),'closed_lock_identity')
            f=path.open('rb');locks.append(f);lock_identity(f,desc);fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);lock_identity(f,desc)
        driver=module(p['sources']['driver'],'_direct_closed_driver');mods=driver.load_native(p);core=mods['core']
        runtime=checked(recipe['runtime_plan']);require(runtime['self_reference']==recipe['plan']and runtime['owner_identity']==owner['process'],'runtime_plan_binding')
        driver.verify_roster(runtime['execution_pins'],list(driver.references(p['sources'])))
        previous=checked(p['predecessor_custody']['actual'])
        scope_module=module(recipe['closed_scope_source'],'causes.campaign_supplement_closed_scope');require(scope_module.core is core,'closed_scope_native_core')
        scope=scope_module.resolve(p,previous,original_transfer_jobs=getattr(mods['direct'],'original_transfer_jobs',None),derived_bank_jobs=getattr(mods.get('bank'),'resolve',None),read=raw)
        fulljobs=scope['full_jobs'];jobs=scope['jobs']
        mods['direct'].validate_contract(runtime,checked(p['contract']),jobs,initial_failures=p['initial_failures'],max_clients=8,read=raw)
        journal=raw(recipe['journal']);events=[json.loads(x)for x in journal.splitlines()];require(journal.endswith(b'\n'),'closed_journal_lines')
        ending=native_summary(events,result,p,recipe['expected_stop_reason'])
        invocation=checked(pin(out/'INVOCATION.json'));driver_authority=checked(invocation['authority'])
        require(invocation['plan']==recipe['plan']and driver_authority['plan']==recipe['plan']and driver_authority['source']==p['sources']['driver']and driver_authority['root_authorized']is True and driver_authority['maximum_invocations']==1 and driver_authority['automatic_retry']is False,'actual_driver_authority')
        require(stamp(driver_authority['not_before'])<=stamp(invocation['at'])<=stamp(driver_authority['expires_at']),'original_invocation_window')
        original_start=stamp(events[0]['at'])
        class SavedDatetime(dt.datetime):
            @classmethod
            def now(cls,tz=None):return original_start
        saved_capacity=clone(core.validate_capacity_limit,dt=types.SimpleNamespace(datetime=SavedDatetime,timezone=dt.timezone))
        saved_capacity(p['capacity_limit'],8,out,jobs,family=p['family'],model=p['model'])
        pending,complete_ids,ambiguous=core.reconcile(out,jobs,events,family=p['family'],model=p['model'])
        collector=module(recipe['native_collector'],'causes.campaign_supplement_collect');require(collector.core is core,'unchanged_native_core')
        claims,terminals=collector.journal_index(events,{j['measurement_id']for j in jobs})
        require(list(claims)==[j['measurement_id']for j in jobs[:len(claims)]]and set(terminals)==set(complete_ids)and set(terminals)<=set(claims),'exact_prefix_terminal_partition')
        require(result['new_final']==len(terminals)and result['remaining_unclaimed']==p['maximum_new_requests']-len(claims)and result['completed_ids']==list(terminals),'native_result_counts')
        require(result['stop_reason']==recipe['expected_stop_reason'],'exact_closed_reason')
        files=tree(out);records=[r for r in files if Path(r['path']).parent==out/'admission_records'];records.sort(key=lambda r:r['path'])
        require(len(records)==result_wrapper['retained_records']and len(records)<=p['maximum_admission_attempts']*12+16,'record_count')
        for i,r in enumerate(records,1):require(Path(r['path']).name.startswith('%06d_'%i),'record_sequence')
        audit=SavedAudit(p,runtime,mods,journal,records)
        for ref in files+durable_refs+scope['required_pins']+[recipe_ref,authority_ref,pin(__file__),recipe['plan'],recipe['owner'],recipe['runtime_plan'],recipe['closed_scope_source'],recipe['output_allocation']]:audit.record(ref)
        remote_accounting=audit.remote_accounting(files,result_wrapper['remote_calls'],result_wrapper.get('control_transport'))
        kinds={}
        for ref in records:kinds.setdefault(Path(ref['path']).name.split('_',1)[1],[]).append(ref)
        reservations=[audit.value(r)for r in kinds.get('reservation',[])];captures=[audit.value(r)for r in kinds.get('capture_intent',[])]
        require([x['number']for x in reservations]==list(range(1,result_wrapper['attempts']+1))and result_wrapper['attempts']<=p['maximum_admission_attempts'],'attempt_accounting')
        require([x['number']for x in captures]==list(range(1,result_wrapper['captures']+1))and result_wrapper['captures']<=p['maximum_captures'],'capture_accounting')
        expired={audit.value(r)['admission_attempt']for r in kinds.get('expiry',[])}
        for r in kinds.get('expiry',[]):require(audit.value(r)['claimed']is False,'expiry_claim')
        capture_novelty=audit.candidate_accounting(kinds.get('candidate',[]))
        all_candidates=[audit.value(r)for r in kinds.get('candidate',[])];ordinals=[r['admission_attempt']for r in all_candidates]
        require(ordinals==sorted(set(ordinals))and set(ordinals)<=set(range(1,result_wrapper['attempts']+1)),'candidate_attempt_partition')
        uses={}
        for candidate in all_candidates:
            use=uses.setdefault(candidate['capture_number'],[]);use.append(candidate['capture_use'])
        require(all(v==list(range(1,len(v)+1))and len(v)<=p['maximum_capture_uses'] for v in uses.values()),'capture_use_partition')
        verified=[];admissions=[];findings=[];exchanges=[]
        if ambiguous or ending['unresolved_claimed_ids']:findings.append(dict(stage='unresolved_native_attempt',ambiguous=ambiguous,measurement_ids=ending['unresolved_claimed_ids']))
        if 'derived_bank' in p:
            verify_derived=derived_final_verifier(recipe,p,scope,mods,collector,audit.record)
        else:
            contexts={(j['run_id'],j['stage']):dict(original_bindings=j['original_refs'])for j in jobs}
            prepare=lambda job,ct,cf,ru:mods['adapter'].prepare_request(job,ct,cf,ru,endpoint=p['endpoint'],modules=mods['contexts'])
        for job in jobs[:len(claims)]:
            mid=job['measurement_id'];require(claims[mid]['input_sha256']==core.digest(job),'claim_input')
            try:
                require(mid in terminals,'claimed_attempt_unresolved')
                if 'derived_bank' in p:row=verify_derived(job,terminals[mid])
                else:row=collector.verify_final(job,terminals[mid],root=out,family=p['family'],model=p['model'],contexts=contexts,config=checked(job['frozen_config']),runner=mods['contexts']['runner'],record=audit.record,prepare_request=prepare)
                row['measurement_id']=mid
            except Exception as error:row=dict(measurement_id=mid,qualification_error=repr(error));findings.append(dict(stage='native_final',measurement_id=mid,error=repr(error)))
            verified.append(row)
            dest=core.measurement_path(out,job,family=p['family'],model=p['model'])
            try:admissions.append(audit.claim(job,claims[mid],pin(dest/'ADMISSION_OBSERVATION.json'),expired))
            except Exception as error:admissions.append(dict(measurement_id=mid,qualified=False,error=repr(error)));findings.append(dict(stage='native_admission',measurement_id=mid,error=repr(error)))
            if mid not in terminals:continue
            final=audit.value(terminals[mid]['final']);refs={Path(r['path']).name:r for r in final['capture_files']}
            if 'attempt.json'in refs and'receipt.json'in refs:exchanges.append(dict(measurement_id=mid,attempt=audit.value(refs['attempt.json']),receipt=audit.value(refs['receipt.json']),attempt_ref=refs['attempt.json'],receipt_ref=refs['receipt.json'],request_sha256=job['request_body_sha256']))
        qualified={r['measurement_id']for r in verified if'qualification_error'not in r}&{r['measurement_id']for r in admissions if r['qualified']}
        admitted_numbers=[r['admission_attempt']for r in admissions if r['qualified']]
        require(admitted_numbers==sorted(set(admitted_numbers)),'claimed_attempt_order')
        new_ids=list(claims);good=[mid for mid in new_ids if mid in qualified];bad=[mid for mid in new_ids if mid not in qualified]
        overlap=module(recipe['closure_helper'],'_direct_saved_exchange_helper').exchange_overlap(exchanges)
        peak=client_overlap(exchanges,set(overlap['healthy_ids']));useful=len(qualified&set(overlap['healthy_ids']))
        elapsed=(stamp(events[-1]['at'])-stamp(events[0]['at'])).total_seconds()
        outputs={};output_sizes=[]
        def buffered(name,value):return buffered_evidence(name,value,destination,outputs,mods['exact'],output_sizes,raw_limit=recipe['maximum_evidence_raw_bytes'],plain_limit=recipe['maximum_plain_file_bytes'])
        partition=scope_module.partition(scope,new_ids,list(terminals));require(partition['unresolved_claimed_ids']==ending['unresolved_claimed_ids'],'closed_partition_unresolved')
        full_jobs_ref=previous['full_jobs']if scope['extension']is None else buffered('FULL_JOBS.v1.json',core.encoded(fulljobs))
        bank_extension=scope_module.bind_extension(scope,full_jobs_ref)
        journal_ref=buffered('JOURNAL.frozen.jsonl',journal);verified_ref=buffered('VERIFIED_FINALS.v1.json',verified)
        admission_ref=buffered('ADMISSION_AUDIT.v1.json',admissions)
        dispositions=buffered('CANDIDATE_DISPOSITIONS.v1.json',dict(records_by_kind=kinds,claimed_candidates=[r.get('candidate')for r in admissions if r['qualified']],expired_attempts=sorted(expired),attempts=result_wrapper['attempts'],captures=result_wrapper['captures'],all_attempts_retained=True,unclaimed_are_not_qualified=True,historical_journal_prefixes=list(audit.prefixes.values()),remote_accounting=remote_accounting,native_ending=ending,capture_novelty=capture_novelty))
        sources=buffered('SOURCE_PINS.v1.json',list(audit.pins.values()))
        assessment=buffered('CAPACITY_AND_THROUGHPUT.v1.json',dict(allowed_clients=8,operational_request_cap=p['maximum_new_requests'],capacity_mode=checked(p['capacity_limit'])['mode'],observed_http=overlap,observed_http_peak=peak,controller_elapsed_seconds=elapsed,qualified_new=len(good),useful_request_count=useful,useful_requests_per_hour=None if elapsed<=0 else useful*3600/elapsed,successful_parsed_count=sum(row['event']=='route_success'and row['measurement_id']in qualified for row in events),successful_parsed_per_hour=None if elapsed<=0 else sum(row['event']=='route_success'and row['measurement_id']in qualified for row in events)*3600/elapsed,healthy_http_count=useful,exchanges=exchanges,capacity_demonstrated=False,capacity_requires_separate_independent_assessment=True,new_draws=0))
        actual=dict(schema='causes.finite_supplement_tranche_closed.v1',at=dt.datetime.now(dt.timezone.utc).isoformat(),role='mi',mode='completed_actor_direct_probe',source=pin(__file__),recipe=recipe_ref,plan=recipe['plan'],output=str(out),retention_root=p['retention_root'],prior_closed=p['predecessor_custody']['actual'],prior_closed_review=p['predecessor_custody']['review'],completed_actor_custody=p['completed_actor_custody'],active=0,ambiguous=ambiguous,clean_native_ending=not(ambiguous or ending['quarantined']or ending['unresolved_claimed_ids']),quarantined=ending['quarantined'],native_ending=ending,consecutive_failures=result['consecutive_failures'],stop_reason=result['stop_reason'],complete_ids=partition['complete_ids'],unstarted_ids=partition['unstarted_ids'],terminal=partition['terminal'],occupied=partition['occupied'],unresolved_claimed_ids=ending['unresolved_claimed_ids'],full_jobs=full_jobs_ref,original_bank_extension=bank_extension,journal=journal_ref,original_journal=recipe['journal'],predecessor_journal=p['predecessor_journal'],executor_lock=dict(p['exclusive_locks'][0],acquired_nonblocking=True,released=True),process_absence=previous['process_absence']+absence,all_invocation_pids_absent=previous['all_invocation_pids_absent']+[r['expected']['pid']for r in absence],native_invocations=previous['native_invocations']+[dict(plan=recipe['plan'],source=p['sources']['driver'],result=recipe['driver_result'],pid=owner['process']['pid'],process_absent=True,family=p['family'],model=p['model'],jobs_sha256=p['jobs_sha256'],original_reservation_transfer=p.get('original_reservation_transfer'))],native_result=recipe['driver_result'],durable_complete=recipe['complete'],durable_execution=complete['executions'][0],verified_finals=verified_ref,admission_observations=admission_ref,source_pins=sources,observation_dispositions=dispositions,prior_scientifically_qualified=previous['scientifically_qualified_total'],scientifically_qualified_total=previous['scientifically_qualified_total']+len(good),prior_invalid_ids=previous['cumulative_invalid_ids'],new_scientifically_qualified_ids=good,new_invalid_ids=bad,cumulative_invalid_ids=previous['cumulative_invalid_ids']+bad,current_qualification_findings=findings,inherited_qualification_findings=previous['qualification_findings'],qualification_findings=previous['qualification_findings']+findings,current_tranche_qualified=not findings,full_tranche_qualified=not(previous['qualification_findings']+findings),all_prospective_admission_count=result_wrapper['attempts'],all_prospective_observation_count=result_wrapper['captures'],expired_unclaimed_candidates=len(expired),original_denominator_preserved=True,all_original_attempts_retained=True,capacity_assessment=assessment,capacity_demonstrated=False,scientific_inference=False,further_runtime_authorized=False,host_calls=0,model_calls=0,old_native_final_and_admission_replays=0)
        if 'derived_bank' in p:
            actual['original_bank_extension']=None
            actual['derived_bank_extension']=bank_extension if bank_extension is not None else scope['carried_derived_extension']
            actual['native_invocations'][-1]['derived_bank']=p['derived_bank']
            actual['derived_scientific_protocol']=scope['derived_bank']['protocol']
            actual['derived_execution_protocol']=p['execution_protocol']
        if 'control_transport' in p:actual['control_transport']=remote_accounting['control_transport']
        actual_ref=buffered('ACTUAL.v1.json',actual)
        total=sum(map(len,outputs.values()));require(total<=recipe['maximum_output_bytes']and recipe['maximum_output_bytes']<=allocation['maximum_output_bytes'],'buffered_audit_output_bound')
        require(size(allocation_root)+total+allocation['future_margin_bytes']<=allocation['maximum_complete_bytes'],'whole_saved_output_root_bound')
        require(all(absent(r['expected'])['absent']for r in absence),'owner_returned')
        for stream,desc in zip(locks,p['exclusive_locks']):lock_identity(stream,desc);fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        driver.verify_roster(runtime['execution_pins'],list(driver.references(p['sources'])))
        require(tree(out)==files,'closed_runtime_tree_changed')
        for ref in audit.pins.values():raw(ref)
        require(raw(recipe['journal'])==journal,'closed_journal_changed')
        destination.mkdir()
        for path,data in outputs.items():
            with path.open('xb')as f:f.write(data)
        print(json.dumps(dict(actual=actual_ref,output_bytes=total,new_qualified=len(good),new_invalid=len(bad),terminal=actual['terminal'],unstarted=len(actual['unstarted_ids']),output_files=len(outputs),output_sizes=output_sizes)),flush=True)
    finally:
        for f in reversed(locks):f.close()

if __name__=='__main__':main()
