"""Configured monitor and completed-snapshot callers with a pinned legacy observer."""
from pathlib import Path, PurePosixPath
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import uuid

from . import campaign_monitor as monitor
from . import campaign_snapshot as snapshot

REMOTE = r'''

from pathlib import Path
import datetime,hashlib,json,os,shutil,subprocess,sys,urllib.request
request=json.load(sys.stdin);base=Path(request['base']);groups=[]
for root in request['roots']:
 p=base/root
 if p.resolve()!=p or not p.is_relative_to(base):raise ValueError('noncanonical root')
 row={'root':root,'counts':{'started':0,'early_captured':0,'actor_finished':0,'complete':0,'actor_error':0,'probe_failure':0,'closure_requires_review':0}}
 if p.is_dir():
  for run in p.iterdir():
   if run.name.startswith('.') or run.is_symlink() or not run.is_dir():continue
   row['counts']['started']+=1
   for name,key in [('early_capture.json','early_captured'),('actor_summary.json','actor_finished'),('summary.json','complete')]:
    f=run/name
    if f.is_file():row['counts'][key]+=1
   final=run/'summary.json'
   if final.is_file():
    d=json.loads(final.read_text())
    row['counts']['actor_error']+=bool(d.get('actor_error'))
    row['counts']['probe_failure']+=sum(x.get('technical_status')!='complete' for x in d.get('probes',[]))
    row['counts']['closure_requires_review']+=d.get('sandbox_closure',{}).get('state')!='closed'
 journal=p/'.dispatcher/journal.jsonl'
 if journal.is_file():
  events={};quarantines={};claimed=set();finished=set()
  for line in journal.read_text().splitlines():
   event=json.loads(line);kind=event['event'];events[kind]=events.get(kind,0)+1
   if kind=='claimed':claimed.add(event['run_id'])
   if kind in ['episode_finished','unresolved_current_attempt','existing_completed_summary','unresolved_prior_attempt','unresolved_completed_evidence','unresolved_identity_conflict']:finished.add(event['run_id'])
   if kind=='route_quarantined':quarantines[event['route']]={'quarantine_id':event['quarantine_id'],'at':event['at']}
   if kind=='route_requalified':quarantines.pop(event['route'],None)
  row['controller']={'event_counts':events,'claimed':len(claimed),'final_dispositions':len(finished),'active_or_unresolved_claims':len(claimed-finished),'quarantines':quarantines}
 groups.append(row)
memory={}
for line in Path('/proc/meminfo').read_text().splitlines():
 k,v=line.split(':',1)
 if k in ['MemTotal','MemAvailable','SwapTotal','SwapFree']:memory[k+'_KiB']=int(v.strip().split()[0])
disks=[]
for p in [Path(value) for value in request['observation']['disk_paths']]:
 if p.exists():
  d=shutil.disk_usage(p);disks.append({'path':str(p),'total':d.total,'used':d.used,'free':d.free})
cmd=['docker','ps','--filter',request['observation']['docker_filter'],'--format','{{.Names}}']
p=subprocess.run(cmd,capture_output=True,text=True,timeout=15)
endpoints=[]
for port,expected in request['observation']['endpoints']:
 row={'port':port,'expected_model':expected}
 try:
  with urllib.request.urlopen('http://127.0.0.1:'+str(port)+'/v1/models',timeout=5) as response:
   row['http_status']=response.status;body=json.load(response)
  row['model_ids']=[x['id'] for x in body['data']];row['expected_model_available']=expected in row['model_ids']
 except Exception as error:row['error']=repr(error);row['expected_model_available']=False
 endpoints.append(row)
identity={'files':[],'controllers':[],'kernel_profile_name_loaded':False,'policy_byte_attestation':'retained_load_receipt_plus_expected_source_hash_only'}
for name,expected in request['identity']['sources'].items():
 path=base/'code/bundles'/request['identity']['bundle']/'causes'/name
 row={'path':str(path),'expected_sha256':expected}
 try:
  assert not any(part.is_symlink() for part in (path,*path.parents))
  row['actual_sha256']=hashlib.sha256(path.read_bytes()).hexdigest();row['matches']=row['actual_sha256']==expected
 except Exception as error:row.update(matches=False,error=type(error).__name__)
 identity['files'].append(row)
identity['overlay'] = {'authorized': 'overlay_authorization' in request['identity'], 'valid': False}
expected_controllers = request['identity']['controllers']
overlay_error = request['identity'].get('overlay_authorization_error')
if 'overlay_authorization' in request['identity']:
 try:
  import importlib.util
  auth=request['identity']['overlay_authorization'];module_pin=auth['overlay_module'];module_path=Path(module_pin['path'])
  if module_path.resolve()!=module_path or any(p.is_symlink() for p in (module_path,*module_path.parents)):raise ValueError('noncanonical overlay module')
  module_data=module_path.read_bytes()
  if len(module_data)!=module_pin['bytes'] or hashlib.sha256(module_data).hexdigest()!=module_pin['sha256']:raise ValueError('overlay module pin')
  spec=importlib.util.spec_from_file_location('approved_identity_overlay',module_path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  resolved=module.resolve_controllers(auth)
  expected_controllers=resolved['controllers'];identity['overlay'].update(valid=True,receipt=resolved['identity_receipt'],phase=resolved['phase'],manifest_sha256=resolved['manifest_sha256'],succession_observations=resolved.get('succession_observations',[]))
 except Exception as error:overlay_error=type(error).__name__+': '+str(error)
if overlay_error:identity['overlay']['error']=overlay_error
for expected in expected_controllers:
 proc=Path('/proc')/str(expected['pid']);row={'pid':expected['pid'],'model':expected['model'],'live':proc.exists(),'argv_matches':False}
 try:
  actual=[part.decode() for part in (proc/'cmdline').read_bytes().split(b'\0') if part]
  row['argv_matches']=actual==expected['argv']
  if 'process_starttime_ticks' in expected:
   fields=(proc/'stat').read_text().rsplit(')',1)[1].split();row['starttime_matches']=fields[19]==expected['process_starttime_ticks'];row['live']=fields[0] not in ['Z','X','x']
 except Exception as error:row['error']=type(error).__name__
 identity['controllers'].append(row)
try:identity['kernel_profile_name_loaded']=(request['observation']['profile_name']+' (enforce)') in Path('/sys/kernel/security/apparmor/profiles').read_text()
except Exception as error:identity['profile_error']=type(error).__name__
identity['all_expected_identities_match']=all(row['matches'] for row in identity['files']) and all(row['live'] and row['argv_matches'] and row.get('starttime_matches',True) for row in identity['controllers']) and not overlay_error and identity['kernel_profile_name_loaded']
print(json.dumps({'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'host':request['host'],'groups':groups,'cpu_count':os.cpu_count(),'loadavg':list(os.getloadavg()),'memory':memory,'disks':disks,'running_campaign_containers':p.stdout.splitlines(),'docker_returncode':p.returncode,'actor_endpoints':endpoints,'source_identity':identity,'alarms':{'root_free_below_80GiB':disks[0]['free']<request['observation']['root_free_floor_bytes'],'available_memory_below_16GiB':memory['MemAvailable_KiB']<request['observation']['available_memory_floor_kib'],'actor_endpoint_unavailable':any(not row['expected_model_available'] for row in endpoints),'source_or_controller_identity_mismatch':not identity['all_expected_identities_match']}}))

'''


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('nonfinite_json')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=constant)


def pin(path):
    path = monitor.canonical_path(path)
    raw = path.read_bytes()
    return {'path':str(path), 'bytes':len(raw), 'sha256':hashlib.sha256(raw).hexdigest()}


def checked_json(ref):
    return strict_json(monitor.checked(ref))


def save(path, value):
    raw = value if isinstance(value, bytes) else monitor.encoded(value) + b'\n'
    with Path(path).open('xb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    snapshot.fsync_directory(Path(path).parent)
    return pin(path)


def validate_config(config):
    monitor.require(config['schema'] == 'causes.campaign_observation_config.v1', 'observation_config_schema')
    wanted = {Path(__file__).name:__file__, Path(monitor.__file__).name:monitor.__file__,
              Path(snapshot.__file__).name:snapshot.__file__}
    monitor.require(set(config['sources']) == set(wanted), 'exact_observation_sources_required')
    for name, path in wanted.items():
        monitor.require(config['sources'][name] == pin(path), 'observation_source_changed')
    legacy = config['legacy']
    monitor.require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', legacy['host']) is not None, 'invalid_legacy_host')
    base = PurePosixPath(legacy['remote_base'])
    monitor.require(base.is_absolute() and str(base) == legacy['remote_base'] and '..' not in base.parts, 'invalid_legacy_base')
    roots = checked_json(legacy['sync_roots'])['roots']
    monitor.require(isinstance(roots,list) and roots and len(roots) == len(set(roots)), 'explicit_legacy_roots_required')
    for root in roots: snapshot.relative_path(root)
    for key in ('launch','overlay_authorization'): checked_json(legacy[key])
    params = legacy['observation']
    monitor.require(isinstance(params['disk_paths'],list) and params['disk_paths']
                    and params['disk_paths'][0] == legacy['remote_base'], 'legacy_primary_disk_required')
    for path in params['disk_paths']:
        p = PurePosixPath(path)
        monitor.require(p.is_absolute() and str(p) == path and '..' not in p.parts, 'invalid_disk_path')
    monitor.require(isinstance(params['docker_filter'],str) and params['docker_filter'].startswith('label='), 'invalid_container_filter')
    monitor.require(isinstance(params['profile_name'],str) and params['profile_name'], 'profile_name_required')
    for key in ('root_free_floor_bytes','available_memory_floor_kib'):
        monitor.require(type(params[key]) is int and params[key] >= 0, 'invalid_legacy_floor')
    monitor.require(params['root_free_floor_bytes'] == 80*1024**3 and params['available_memory_floor_kib'] == 16*1024**2, 'legacy_named_alarm_floors_changed')
    monitor.require(isinstance(params['endpoints'],list) and params['endpoints'], 'explicit_endpoints_required')
    for port, model in params['endpoints']:
        monitor.require(type(port) is int and 0 < port < 65536 and isinstance(model,str) and model, 'invalid_legacy_endpoint')
    mc = monitor.MonitorConfig(config['monitor']['policy'], config['monitor']['authorization_path'])
    monitor.require(legacy['host'] == mc.policy['transfer_host'] and legacy['remote_base'] == mc.policy['remote_base'], 'legacy_monitor_host_mismatch')
    monitor.require(legacy['sync_roots'] == mc.policy['legacy']['sync_roots']
                    and {'roots':roots} == mc.policy['legacy']['sync_roots_value'], 'legacy_monitor_roots_mismatch')
    monitor.require(legacy['overlay_authorization'] in mc.policy['legacy']['local_pins'], 'legacy_overlay_policy_mismatch')
    sc = snapshot.SnapshotConfig(**dict(config['snapshot'], roots=tuple(config['snapshot']['roots']),
                                       destination=Path(config['snapshot']['destination'])))
    sc.validate()
    monitor.require(sc.host == mc.policy['controller_host'] and sc.remote_base == mc.policy['remote_base']
                    and list(sc.roots) == mc.policy['sync_roots'], 'snapshot_policy_scope_mismatch')
    for key in ('status_directory','transport_directory'): monitor.canonical_path(config[key])
    return mc, sc


def legacy_request(config):
    legacy = config['legacy']; base = Path(legacy['remote_base'])
    launch = checked_json(legacy['launch'])
    request = {'host':legacy['host'], 'base':str(base), 'observation':legacy['observation'],
               'roots':checked_json(legacy['sync_roots'])['roots'],
               'identity':{'bundle':launch['bundle'], 'sources':launch['source_identities'],
                           'controllers':[{key:row[key] for key in ('pid','model','argv')} for row in launch['processes']]}}
    authorization_path = Path(legacy['overlay_authorization']['path'])
    try:
        auth = checked_json(legacy['overlay_authorization']); module_pin = auth['overlay_module']
        source = monitor.checked(module_pin)
        spec = importlib.util.spec_from_file_location('qualified_legacy_identity_overlay',module_pin['path'])
        module = importlib.util.module_from_spec(spec)
        exec(compile(source,module_pin['path'],'exec'),module.__dict__)
        auth = module.validate_authorization(authorization_path)
        monitor.checked(legacy['overlay_authorization'])
        request['identity']['overlay_authorization'] = auth
        manifest = strict_json(module.pinned(auth['manifest']))
        request['roots'] = sorted(set(request['roots']) | {str(Path(value).relative_to(base)) for value in manifest['outputs'].values()})
    except Exception as error:
        request['identity']['overlay_authorization_error'] = type(error).__name__ + ': ' + str(error)
    return request


class LegacyObserver:
    def __init__(self, config):
        self.config = config

    def __call__(self):
        request = legacy_request(self.config)
        root = monitor.canonical_path(self.config['transport_directory'])
        root.mkdir(parents=True,exist_ok=True)
        out = root / (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ') + '_' + uuid.uuid4().hex[:8])
        out.mkdir(); snapshot.fsync_directory(root)
        save(out/'REQUEST.json',request)
        save(out/'REMOTE.py',REMOTE.encode())
        command = ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',self.config['legacy']['host'],
                   'python3','-c',shlex.quote(REMOTE)]
        save(out/'INTENT.json',{'at':dt.datetime.now(dt.timezone.utc).isoformat(),'command':command,
                              'source':pin(__file__),'read_only':True})
        try:
            result = subprocess.run(command,input=json.dumps(request).encode(),capture_output=True,timeout=45)
        except Exception as error:
            save(out/'FAILED.json',{'error_type':type(error).__name__,'error':str(error)[:500]})
            raise
        stdout = save(out/'stdout',result.stdout); stderr = save(out/'stderr',result.stderr)
        receipt = save(out/'EXECUTION.json',{'at':dt.datetime.now(dt.timezone.utc).isoformat(),
                                           'returncode':result.returncode,'stdout':stdout,'stderr':stderr})
        monitor.require(result.returncode == 0, 'legacy_observation_failed')
        value = strict_json(result.stdout)
        monitor.require(value['host'] == self.config['legacy']['host'], 'legacy_response_host_changed')
        value['legacy_transport'] = receipt
        return value


def status(config):
    mc, _ = validate_config(config)
    legacy = LegacyObserver(config)()
    companion = monitor.observe(mc, lambda:legacy)
    value = monitor.merge_monitor(legacy, companion)
    directory = monitor.canonical_path(config['status_directory'])
    monitor.require(directory.is_dir(), 'status_directory_missing')
    path = directory / (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_job_status.json')
    return save(path,value), value


def synchronize(config):
    mc, sc = validate_config(config)
    return snapshot.synchronize_completed(sc,monitor.snapshot_authorizer(mc,LegacyObserver(config)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('validate','status','sync'))
    parser.add_argument('--config',required=True,type=Path)
    parser.add_argument('--config-sha256',required=True)
    args = parser.parse_args()
    ref = pin(args.config)
    monitor.require(ref['sha256'] == args.config_sha256, 'observation_config_changed')
    config = checked_json(ref)
    validate_config(config)
    if args.action == 'validate':
        print(json.dumps({'validated':True,'executed':False,'config':ref})); return
    if args.action == 'status':
        ref,value = status(config); print(ref['path']); print(json.dumps(value))
    else:
        print(json.dumps(synchronize(config)))


if __name__ == '__main__':
    main()
