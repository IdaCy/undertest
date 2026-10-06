"""Read-only captures and metadata assembly for an accepted admission policy.

No function grants admission, changes a control, retries a transport, synchronizes
episodes, or creates an allocation block. Captures and composed evidence need
independent acceptance before an admission operator can consume them.
"""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import stat
import subprocess
import sys

from . import campaign_admission as admission
from . import campaign_census as census
from . import campaign_handoff as h
from . import campaign_monitor as monitor
from . import campaign_snapshot as snapshot


def require(value,message):
    if not value:raise ValueError(message)


def same(a,b):return h.encoded(a)==h.encoded(b)


def settings(reference):
    cfg=h.checked(reference)
    require(cfg['schema']=='causes.admission_evidence_configuration.v1','evidence_configuration_schema')
    for module in (__file__,admission.__file__,census.__file__,h.__file__,monitor.__file__,snapshot.__file__):
        require(h.pin(module)['sha256']==cfg['source_hashes'][Path(module).name],'evidence_module_changed')
    template=h.checked(cfg['policy_template']);review=h.checked(cfg['admission_review'])
    require(review.get('accepted_continuous_admission_policy_template') is True
            and review['policy_template']==cfg['policy_template'],'unaccepted_evidence_policy')
    registration_review=h.checked(cfg['foundation_registration_review'])
    require(registration_review.get('accepted_companion_registration') is True
            and registration_review['registration']==cfg['foundation_registration'],'unaccepted_foundation_registration')
    fixed=template['fixed']
    require(cfg['native_host']==fixed['controller_host'] and cfg['transfer_host']==fixed['census_hosts'][0]
            and cfg['native_host']==fixed['census_hosts'][1],'evidence_host_scope_changed')
    require(cfg['donor_scopes']==fixed['history']['donor_scopes'],'evidence_donor_scope_changed')
    require(cfg['max_file_bytes']>0 and cfg['max_total_bytes']>=cfg['max_file_bytes']
            and type(cfg['max_files']) is int and cfg['max_files']>0,'evidence_bounds_required')
    require(type(cfg['transport_timeout_seconds']) is int and 0<cfg['transport_timeout_seconds']<=120,
            'bounded_evidence_transport_required')
    return cfg,fixed


def accepted_source(configuration,review_reference):
    cfg,fixed=settings(configuration);review=h.checked(review_reference)
    require(review.get('accepted_continuous_admission_evidence_source') is True
            and review['configuration']==configuration and review['source']['sha256']==h.pin(__file__)['sha256'],
            'unaccepted_evidence_assembler')
    return cfg,fixed


class FileCapture:
    def __init__(self,cfg,host):self.cfg,self.host,self.rows,self.total=cfg,host,{},0

    def take(self,path,expected=None,journal=False):
        path=h.canonical_path(path);require(path.is_relative_to(self.cfg['capture_base']),'capture_outside_base')
        if str(path) in self.rows:
            row=self.rows[str(path)]
            require(expected is None or row['remote']==expected,'conflicting_capture_pin')
            return row
        require(len(self.rows)<self.cfg['max_files'],'capture_file_limit')
        before=path.stat();require(stat.S_ISREG(before.st_mode) and before.st_size<=self.cfg['max_file_bytes'],'capture_file_bound')
        started=h.now()
        with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as stream:
            opened=os.fstat(stream.fileno());raw=stream.read(self.cfg['max_file_bytes']+1);closed=os.fstat(stream.fileno())
        after=path.stat();ended=h.now();fields=('st_dev','st_ino','st_mode','st_size','st_mtime_ns','st_ctime_ns')
        require(all(getattr(before,k)==getattr(opened,k)==getattr(closed,k)==getattr(after,k) for k in fields),
                'capture_changed_during_read')
        require(len(raw)==before.st_size and len(raw)<=self.cfg['max_file_bytes'],'capture_truncated')
        prefix=raw[:raw.rfind(b'\n')+1] if journal else raw
        require(not journal or (prefix and len(prefix)==len(raw)),'journal_torn_or_empty_requires_later_cut')
        remote=h.pin(path,prefix);require(expected is None or remote==expected,'capture_expected_pin_changed')
        self.total+=len(prefix);require(self.total<=self.cfg['max_total_bytes'],'capture_total_bound')
        row=dict(host=self.host,remote=remote,base64=base64.b64encode(prefix).decode(),
                 read_started_at=started,read_finished_at=ended,bytes_read=len(raw),unconsumed_tail_bytes=len(raw)-len(prefix))
        self.rows[str(path)]=row;return row


def capture_json(capture,reference):
    row=capture.take(reference['path'],reference)
    return json.loads(base64.b64decode(row['base64'],validate=True))


def retain_capture(value,output,cfg):
    """Retain a unique original-path map; never fabricate canonical remote files."""
    rows=value['files'];require(len(rows)<=cfg['max_files'],'capture_file_limit')
    seen=set();total=0;result=[]
    for index,row in enumerate(rows):
        remote=row['remote'];path=Path(remote['path'])
        require(path.is_absolute() and str(path)==remote['path'] and '..' not in path.parts
                and path.is_relative_to(cfg['capture_base']) and remote['path'] not in seen,'duplicate_or_unconfined_capture')
        seen.add(remote['path']);raw=base64.b64decode(row['base64'],validate=True)
        require(type(remote['bytes']) is int and len(raw)==remote['bytes']<=cfg['max_file_bytes']
                and hashlib.sha256(raw).hexdigest()==remote['sha256'],'captured_bytes_changed')
        total+=len(raw);require(total<=cfg['max_total_bytes'],'capture_total_bound')
        folder=output/'custody'/str(index);folder.mkdir(parents=True,exist_ok=False)
        local=save_bytes(folder/path.name,raw)
        result.append({k:v for k,v in row.items() if k!='base64'}|{'local':local})
    return h.save_new(output,'RECEIPT_MAP.json',{'files':result})


def save_bytes(path,raw):
    path=h.canonical_path(path)
    with path.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    h.sync_directory(path.parent);return h.pin(path,raw)


def mapped(reference_map,remote):
    matches=[r for r in h.checked(reference_map)['files'] if r['remote']==remote]
    require(len(matches)==1,'unique_original_receipt_required');row=matches[0];raw=h.checked_bytes(row['local'])
    require(len(raw)==remote['bytes'] and hashlib.sha256(raw).hexdigest()==remote['sha256'],'mapped_receipt_changed')
    return row,raw


def collect_foundation(cfg,fixed,host,stage,waiting=None):
    require(stage in ('before_start','waiting_release'),'foundation_stage')
    capture=FileCapture(cfg,host);started=h.now();observation={};donors=[]
    for scope in cfg['donor_scopes']:
        if scope['host']!=host:continue
        row=capture.take(Path(scope['output_root'])/'.dispatcher/journal.jsonl',journal=True)
        donors.append({**scope,'journal':row['remote'],'at':row['read_finished_at']})
    for path in fixed['pause_paths']:require(not os.path.lexists(h.canonical_path(path)),'foundation_pause_present')
    reg=h.checked(cfg['foundation_registration'])
    require(reg['provider']==fixed['provider'] and reg['host_transfer']==fixed['host_transfer'],'foundation_registration_changed')
    if host==cfg['transfer_host']:
        transfer=fixed['host_transfer'];reservation=capture_json(capture,transfer['mi_reservation'])
        require(reservation['target_host']==cfg['native_host']
                and reservation['target_output_roots']==transfer['target_output_roots']
                and reservation['jobs']==transfer['original_phase02_jobs'],'original_transfer_changed')
        require(not os.path.lexists(transfer['mi_unused_output_root']),'unused_MI_output_present')
        reverse=reg['reverse_listener_process']
        observation.update(reverse_listener_process=monitor.process(reverse),
            listener=monitor.listener_owned(reverse['pid'],cfg['relay_port']),
            model=monitor.models(cfg['relay_port'],fixed),original_transfer_present=True,unused_MI_output_absent=True)
    elif host==cfg['native_host']:
        provider=fixed['provider'];process=provider['process'];monitor.process(process)
        for ref in provider['source_pins']+[provider['ready_receipt'],provider['serving_plan']]:h.checked_bytes(ref)
        require((Path('/proc')/str(process['pid'])/'cgroup').read_text()==process['cgroup'],'provider_cgroup_changed')
        observation.update(provider=process,listener=monitor.listener_owned(process['pid'],fixed['provider_port']),
            model=monitor.models(fixed['provider_port'],fixed),resources=admission.resources(fixed['base']))
        owners=[]
        for index,old in enumerate(fixed['predecessors']):
            launch=capture_json(capture,old['launch']);require(launch['process']==old['process'],'predecessor_launch_changed')
            process=old['process'];root=Path(old['output_root'])
            if stage=='before_start' and index==len(fixed['predecessors'])-1:
                monitor.process(process);admission.exact_children_absent(process)
                require(h.lock_owned_by(root/'.dispatcher.lock',process['pid']),'finite_idle_lock_changed')
                state={'exact_live_idle_owner':True,'children':[]}
            else:
                state=h.retirement_state(process)
                require((state['absent'] or state.get('state') in ('Z','X','x')) and h.lock_free(root/'.dispatcher.lock'),
                        'finite_owner_not_retired')
            owners.append({'launch':old['launch'],'process':process,'state':state})
        command=['docker','ps','-q','--filter','label='+cfg['container_label']]
        result=subprocess.run(command,capture_output=True,timeout=10)
        require(result.returncode==0 and not result.stdout and not result.stderr,'native_container_still_active')
        mounts=Path('/proc/self/mountinfo').read_text()
        require(not any(old['output_root']+'/' in mounts for old in fixed['predecessors']),'native_mount_still_active')
        observation.update(owners=owners,container_check={'argv':command,'returncode':0,'stdout':'','stderr':''},
                           no_owned_active_work=True)
        if stage=='waiting_release':
            require(waiting is not None,'waiting_binding_required')
            observation['waiting']=admission.observe_waiting(waiting['policy'],waiting['launch'])
    else:raise ValueError('unregistered_foundation_host')
    return dict(action='foundation',host=host,hostname=socket.gethostname(),stage=stage,started_at=started,
                at=h.now(),observation=observation,donor_cuts=donors,files=list(capture.rows.values()),read_only=True)


def collect_waiting(cfg,policy_reference,launch_reference):
    policy=h.checked(policy_reference);launch=h.checked(launch_reference)
    require(policy['policy_template']==cfg['policy_template'],'waiting_policy_template_changed')
    observation=admission.observe_waiting(policy_reference,launch_reference)
    capture=FileCapture(cfg,cfg['native_host']);capture_json(capture,launch_reference)
    for key in ('process_receipt','intent','owned_barrier','retirement','queue_identity','admission_control','controller_identity'):
        capture_json(capture,launch[key])
    owned=capture_json(capture,launch['owned_barrier'])
    for key in ('marker','archived_marker'):capture.take(owned[key]['path'],owned[key])
    for key in ('creation_intent','creation_authority'):capture_json(capture,owned[key])
    retirement=capture_json(capture,launch['retirement']);capture_json(capture,retirement['intent'])
    capture.take(observation['journal']['path'],observation['journal'],journal=True)
    return dict(action='waiting',host=cfg['native_host'],at=observation['at'],launch=launch_reference,
                observation=observation,files=list(capture.rows.values()),read_only=True)


def collect(request):
    cfg,fixed=settings(request['configuration']);action=request['action']
    if action=='foundation':return collect_foundation(cfg,fixed,request['host'],request['stage'],request.get('waiting'))
    if action=='waiting':
        require(request['host']==cfg['native_host'],'waiting_host_changed')
        return collect_waiting(cfg,request['policy'],request['launch'])
    if action=='census':
        require(request['census_config'] in list(fixed['census_configs'].values()),'census_configuration_changed')
        config=h.checked(request['census_config'])
        require(config['collector_sha256']==h.pin(census.__file__)['sha256'],'census_source_changed')
        hosts=[v for v in config['hosts'] if v['host']==request['host']]
        require(len(hosts)==1,'exact_census_host_required')
        value=census.collect_host(hosts[0]);value['collector_source']=h.pin(census.__file__)
        return value
    raise ValueError('unknown_read_only_evidence_action')


def capture(request,review_reference,output):
    cfg,_=accepted_source(request['configuration'],review_reference)
    require(request['host'] in (cfg['native_host'],cfg['transfer_host']),'capture_host_changed')
    out=h.new_record_directory(output);request_ref=h.save_new(out,'REQUEST.json',request)
    command=['env','PYTHONPATH='+cfg['remote_pythonpath'],cfg['remote_python'],'-B','-m',
             'causes.campaign_admission_evidence','collect']
    argv=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10',
          request['host'],shlex.join(command)]
    intent=h.save_new(out,'INTENT.json',dict(at=h.now(),request=request_ref,source=h.pin(__file__),
        review=review_reference,argv=argv,read_only=True,no_automatic_retry=True))
    timed=False
    try:
        result=subprocess.run(argv,input=h.checked_bytes(request_ref),capture_output=True,timeout=cfg['transport_timeout_seconds'])
        stdout,stderr,code=result.stdout,result.stderr,result.returncode
    except subprocess.TimeoutExpired as error:
        stdout,stderr,code,timed=error.stdout or b'',error.stderr or b'',124,True
    outpin=save_bytes(out/'stdout',stdout);errpin=save_bytes(out/'stderr',stderr)
    execution=h.save_new(out,'EXECUTION.json',dict(at=h.now(),intent=intent,returncode=code,timed_out=timed,
        remote_completion_unknown=timed,stdout=outpin,stderr=errpin))
    require(code==0 and not stderr,'capture_failed_no_retry')
    value=census.strict_json(stdout)
    if request['action']=='census':
        require(value['host']==request['host'],'census_transport_host_changed')
        return h.save_new(out,'CAPTURE.json',value)
    require(value['host']==request['host'] and value['action']==request['action'] and value['read_only'] is True,'capture_scope_changed')
    receipt_map=retain_capture(value,out,cfg)
    report={k:v for k,v in value.items() if k!='files'}
    report.update(configuration=request['configuration'],execution=execution,receipt_map=receipt_map)
    if request['action']=='waiting':
        for key in ('process','owned_barrier','zero_initial_claims','zero_persisted_blocks'):report[key]=value['observation'][key]
    return h.save_new(out,'REPORT.json',report)


def _entry_metadata(entry,snapshot_root,host,base,cache):
    folder=h.canonical_path(entry['path']);manifest_path=h.canonical_path(entry['manifest'])
    require(manifest_path.name=='MANIFEST.json' and manifest_path.parent.parent==snapshot_root/'attempts'
            and folder==manifest_path.parent/'data'/entry['run'],'snapshot_entry_path_changed')
    if str(manifest_path) not in cache:
        manifest_ref=h.pin(manifest_path);require(manifest_ref['sha256']==entry['manifest_sha256'],'manifest_index_pin_changed')
        manifest=h.checked(manifest_ref);request=h.checked(h.pin(manifest_path.parent/'REQUEST.json'))
        verified=h.checked(h.pin(manifest_path.parent/'VERIFIED.json'))
        # The accepted predecessor request omits host; its manifest binds it.
        # The public snapshot request adds host and must agree when present.
        require(manifest['host']==host and request.get('host',host)==host
                and manifest['base']==request['base']==base,'snapshot_host_scope_changed')
        cache[str(manifest_path)]=(manifest_ref,manifest,verified)
    manifest_ref,manifest,verified=cache[str(manifest_path)]
    require(manifest_ref['sha256']==entry['manifest_sha256']
            and sum(same(v,entry) for v in verified['runs'])==1,'exact_verified_entry_required')
    rows=[r for r in manifest['runs'] if r['run']==entry['run']];require(len(rows)==1,'unique_manifest_run_required')
    row=rows[0];files={r['path']:r for r in row['files']}
    require(len(files)==len(row['files']) and len(files)==entry['files'] and row['bytes']==entry['bytes']
            and row['summary_sha256']==entry['summary_sha256'],'snapshot_manifest_join_changed')
    selected={}
    for name in ('summary.json','allocation.json','spec.json','variant.json','model.json'):
        item=files[name];ref={'path':str(folder/name),'bytes':item['bytes'],'sha256':item['sha256']}
        selected[name]=(ref,h.checked(ref))
    require(selected['summary.json'][0]['sha256']==entry['summary_sha256'],'snapshot_summary_changed')
    return selected,manifest_ref


def index_prefix(configuration,output):
    cfg,_=settings(configuration);path=h.canonical_path(Path(cfg['snapshot_destination'])/'accepted.jsonl')
    started=h.now();raw=path.read_bytes();ended=h.now();prefix=raw[:raw.rfind(b'\n')+1]
    out=h.new_record_directory(output);retained=save_bytes(out/'INDEX_PREFIX.jsonl',prefix)
    return h.save_new(out,'REPORT.json',dict(source_path=str(path),prefix=retained,read_started_at=started,
        read_finished_at=ended,bytes_read=len(raw),unconsumed_tail_bytes=len(raw)-len(prefix),configuration=configuration))


def terminal_custody(configuration,index_reference,root_journals,receipt_map):
    """Join closed copied metadata to every frozen terminal ID, including failures."""
    cfg,fixed=settings(configuration);raw=h.checked_bytes(index_reference)
    require(not raw or raw.endswith(b'\n'),'immutable_index_prefix_required')
    entries=[census.strict_json(line) for line in raw.splitlines()];roots={};cache={}
    require(set(root_journals)=={r['output_root'] for r in fixed['predecessors']},'terminal_root_scope_changed')
    for old in fixed['predecessors']:
        jobs=h.checked(old['jobs']);config=h.checked(old['config']);ids=[j['allocation']['run_id'] for j in jobs]
        require(len(ids)==len(set(ids)),'duplicate_frozen_terminal_ID')
        relative=str(Path(old['output_root']).relative_to(cfg['remote_base']))
        selected=[r for r in entries if str(Path(r['run']).parent)==relative]
        byid={Path(r['run']).name:r for r in selected}
        require(len(byid)==len(selected) and set(byid)==set(ids),'terminal_snapshot_membership_incomplete')
        summaries={};local_summaries={};custody=[];missing=[]
        for job in jobs:
            rid=job['allocation']['run_id'];entry=byid[rid]
            values,manifest=_entry_metadata(entry,Path(cfg['snapshot_destination']),cfg['native_host'],cfg['remote_base'],cache)
            for name,expected in (('allocation',job['allocation']),('spec',job['spec']),('variant',job['variant']),('model',config)):
                require(same(values[name+'.json'][1],expected),'terminal_frozen_identity_changed')
            local,summary=values['summary.json'];closure=summary.get('sandbox_closure',{})
            require(summary['run_id']==rid and closure.get('state')=='closed' and closure.get('overlay_mounted') is False
                    and not summary.get('sandbox_close_error'),'terminal_copy_not_closed')
            local_summaries[rid]=local;summaries[rid]=dict(local,path=str(Path(old['output_root'])/rid/'summary.json'))
            technical=dict(actor_error=bool(summary.get('actor_error')),
                probe_statuses=[{'stage':p.get('stage'),'instrument':p.get('instrument'),
                                 'technical_status':p.get('technical_status')} for p in summary.get('probes',[])])
            if technical['actor_error'] or any(p['technical_status']!='complete' for p in technical['probe_statuses']):
                missing.append({'run_id':rid,'summary':summaries[rid],'summary_capture':local,'technical':technical})
            custody.append(dict(run_id=rid,entry=entry,manifest=manifest,
                                metadata={name:pair[0] for name,pair in values.items()}))
        journal=root_journals[old['output_root']];_,journal_raw=mapped(receipt_map,journal)
        require(journal['path']==old['output_root']+'/.dispatcher/journal.jsonl','terminal_journal_root_changed')
        counts=admission.finite_terminal_identity(jobs,config,journal_raw,{'completed_ids':ids,'summaries':local_summaries})
        for row in (census.strict_json(line) for line in journal_raw.splitlines()):
            if row['event']=='episode_finished':
                require(row.get('summary_sha256')==summaries[row['run_id']]['sha256'],
                        'terminal_final_summary_hash_changed')
        roots[old['output_root']]=dict(launch=old['launch'],journal=journal,completed_ids=ids,summaries=summaries,
            accepted_copy_custody=custody,terminal_counts=counts,technical_missingness=missing)
    return dict(at=h.now(),configuration=configuration,host_transfer=fixed['host_transfer'],roots=roots,
        accepted_index_prefix=index_reference,receipt_map=receipt_map,all_expected_terminal_custody=True,
        all_closed_verified=True,technical_missingness_preserved=True,no_unresolved_terminal_records=True,
        independent_acceptance_pending=True,
        limits='Qualified snapshot manifest/VERIFIED/index plus five exact metadata files per run. No payload replay or new capacity qualification. Missing technical results remain explicit; independent review supplies their diagnosis disposition.')


def relay_foundation(configuration):
    cfg,fixed=settings(configuration);reg=h.checked(cfg['foundation_registration']);started=h.now()
    tunnels=[monitor.process(row['process']) for row in reg['tunnels']]
    listener=monitor.listener_owned(reg['tunnels'][0]['process']['pid'],cfg['relay_port'])
    return dict(at=h.now(),started_at=started,configuration=configuration,registration=cfg['foundation_registration'],
                tunnels=tunnels,listener=listener,model=monitor.models(cfg['relay_port'],fixed),read_only=True)


def captured_report(reference):
    report=h.checked(reference);execution=h.checked(report['execution'])
    require(execution['returncode']==0 and execution['timed_out'] is False
            and execution['remote_completion_unknown'] is False and not h.checked_bytes(execution['stderr']),
            'incomplete_evidence_capture')
    intent=h.checked(execution['intent']);request=h.checked(intent['request'])
    require(request['configuration']==report['configuration'] and request['host']==report['host']
            and request['action']==report['action'],'evidence_request_scope_changed')
    raw=census.strict_json(h.checked_bytes(execution['stdout']))
    for key,value in raw.items():
        if key!='files':require(same(report[key],value),'evidence_capture_projection_changed')
    mapping=h.checked(report['receipt_map'])['files']
    require(len(mapping)==len(raw['files']),'evidence_capture_map_length_changed')
    for row,saved in zip(raw['files'],mapping):
        require(same({k:v for k,v in row.items() if k!='base64'},
                     {k:v for k,v in saved.items() if k!='local'}),'evidence_capture_map_changed')
        require(base64.b64decode(row['base64'],validate=True)==h.checked_bytes(saved['local']),
                'evidence_capture_content_changed')
    return report


def baseline_donors(configuration,host_reports):
    """Return ordered immutable baseline pins from accepted-scope raw captures."""
    cfg,_=settings(configuration)
    require(set(host_reports)=={cfg['native_host'],cfg['transfer_host']},'both_donor_hosts_required')
    rows={};keys=('host','output_root','mode','recorder_relevant')
    for host,reference in host_reports.items():
        report=captured_report(reference)
        require(report['configuration']==configuration and report['host']==host and report['action']=='foundation',
                'baseline_capture_scope_changed')
        for row in report['donor_cuts']:
            require(row['host']==host,'baseline_host_changed');mapped(report['receipt_map'],row['journal'])
            key=tuple(row[k] for k in keys);require(key not in rows,'duplicate_baseline_scope')
            rows[key]={k:row[k] for k in (*keys,'journal')}
    wanted=[tuple(row[k] for k in keys) for row in cfg['donor_scopes']]
    require(set(rows)==set(wanted),'baseline_donor_inventory_changed')
    return [rows[key] for key in wanted]


def foundation(configuration,policy_reference,legacy_reference,relay_reference,host_reports):
    cfg,fixed=settings(configuration);policy=h.checked(policy_reference)
    require(policy['policy_template']==cfg['policy_template'],'foundation_policy_changed')
    legacy=h.checked(legacy_reference);monitor.validate_legacy(legacy,h.checked(cfg['monitor_policy']))
    relay=h.checked(relay_reference);admission.recent(relay['started_at']);admission.recent(relay['at'])
    require(relay['configuration']==configuration and relay['registration']==cfg['foundation_registration']
            and relay['read_only'] is True,'relay_scope_changed')
    registration=h.checked(cfg['foundation_registration'])
    process_fields=('pid','process_starttime_ticks','argv')
    require([{k:r[k] for k in process_fields} for r in relay['tunnels']]
            ==[{k:r['process'][k] for k in process_fields} for r in registration['tunnels']],
            'relay_process_binding_changed')
    require(relay['listener']['pid']==registration['tunnels'][0]['process']['pid']
            and relay['model']['model']==fixed['model'] and relay['model']['max_model_len']==fixed['max_model_len'],
            'relay_endpoint_binding_changed')
    require(set(host_reports)=={cfg['native_host'],cfg['transfer_host']},'both_foundation_hosts_required')
    reports={host:captured_report(ref) for host,ref in host_reports.items()};stages=set();by_scope={};evidence=[legacy_reference,relay_reference,*host_reports.values()]
    for host,report in reports.items():
        require(report['action']=='foundation' and report['host']==host and report['configuration']==configuration
                and report['read_only'] is True,'foundation_capture_scope_changed')
        admission.recent(report['started_at']);admission.recent(report['at']);stages.add(report['stage'])
        evidence.extend([report['receipt_map'],report['execution']])
        for row in report['donor_cuts']:
            require(row['host']==host,'donor_capture_host_changed');local,_=mapped(report['receipt_map'],row['journal'])
            key=tuple(row[k] for k in ('host','output_root','mode','recorder_relevant'))
            require(key not in by_scope,'duplicate_donor_capture');by_scope[key]={**row,'capture':local['local']}
    require(len(stages)==1 and next(iter(stages)) in ('before_start','waiting_release'),'foundation_stages_disagree')
    cuts=[by_scope[tuple(row[k] for k in ('host','output_root','mode','recorder_relevant'))] for row in cfg['donor_scopes']]
    require(len(by_scope)==len(cuts),'extra_donor_scope')
    admission.donor_cuts_clear(fixed['history'],policy['donor_histories'],cuts)
    native=reports[cfg['native_host']]['observation'];transfer=reports[cfg['transfer_host']]['observation']
    require(native['no_owned_active_work'] is True and transfer['original_transfer_present'] is True
            and transfer['unused_MI_output_absent'] is True and native['provider']==fixed['provider']['process'],
            'foundation_not_clear')
    admission.resource_check(fixed['resource_decision'],native['resources'],
        h.checked(h.checked(fixed['control_template'])['prototypes']),fixed['workers'])
    observed=min([legacy['at'],relay['started_at'],*(r['started_at'] for r in reports.values())],key=dt.datetime.fromisoformat)
    return dict(at=observed,composed_at=h.now(),configuration=configuration,policy=policy_reference,
        host_transfer=fixed['host_transfer'],provider=fixed['provider'],legacy_owners=fixed['legacy_owners'],
        donor_cuts=cuts,evidence_pins=evidence,legacy_two_owners_healthy=True,provider_tunnels_healthy=True,
        original_transfer_present=True,unused_MI_output_absent=True,donor_histories_clear=True,native_no_active_owned_work=True,
        stage=next(iter(stages)),independent_acceptance_pending=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('collect','capture','terminal','foundation','relay','index'))
    parser.add_argument('--input');parser.add_argument('--review');parser.add_argument('--output')
    args=parser.parse_args()
    if args.action=='collect':print(json.dumps(collect(census.strict_json(sys.stdin.buffer.read()))));return
    require(args.input is not None and args.review is not None and args.output is not None,'explicit_evidence_inputs_required')
    inputs=h.checked(json.loads(args.input));review=json.loads(args.review)
    accepted_source(inputs['configuration'],review)
    if args.action=='capture':result=capture(inputs,review,args.output)
    elif args.action=='index':result=index_prefix(inputs['configuration'],args.output)
    else:
        if args.action=='terminal':value=terminal_custody(inputs['configuration'],inputs['index_prefix'],inputs['root_journals'],inputs['receipt_map'])
        elif args.action=='relay':value=relay_foundation(inputs['configuration'])
        else:value=foundation(inputs['configuration'],inputs['policy'],inputs['legacy_status'],inputs['relay'],inputs['host_reports'])
        out=h.new_record_directory(args.output);h.save_new(out,'INPUTS.json',inputs);result=h.save_new(out,'EVIDENCE.json',value)
    print(json.dumps(result))


if __name__=='__main__':main()
