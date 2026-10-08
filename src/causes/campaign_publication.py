"""Prepare and guard the metadata publication for a waiting continuous owner."""
from pathlib import Path
import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import uuid

from . import campaign_handoff as h
from . import campaign_monitor as monitor
from . import campaign_observation as observation


ROLES = ('JP_sync_roots','monitor_configuration','JP_sync_configuration','watcher_configuration',
         'authorization_pointer','JP_sync','monitor','watcher')


def require(value, message):
    if not value: raise ValueError(message)


def same(a, b):
    return h.encoded(a) == h.encoded(b)


def accepted(reference, review, flag, key):
    value = h.checked(review)
    require(value.get(flag) is True and same(value.get(key),reference), 'independent_acceptance_missing:'+flag)
    return h.checked(reference)


class CapturedReader:
    def __init__(self, reference):
        self.reference = reference
        self.rows = h.checked(reference)['files']
        self.mapping = {}
        for row in self.rows:
            remote, local = row['remote'],row['local']
            require(set(remote) == set(local) == {'path','bytes','sha256'}, 'receipt_pin_shape')
            require(type(remote['bytes']) is int and remote['bytes'] == local['bytes']
                    and remote['sha256'] == local['sha256'], 'receipt_capture_content_mismatch')
            key = h.encoded(remote)
            require(key not in self.mapping, 'duplicate_remote_receipt')
            h.checked_bytes(local); self.mapping[key] = local

    def local(self, ref):
        return self.mapping.get(h.encoded(ref),ref)

    def __call__(self, ref):
        return h.checked(self.local(ref))


def current_source():
    return h.pin(__file__)


def identity(path):
    path = h.canonical_path(path)
    if not os.path.lexists(path): return {'exists':False}
    value = h.stat_identity(path)
    require(stat.S_ISREG(value['st_mode']), 'publication_target_not_regular')
    return {'exists':True,**value}


def write_bytes(directory, name, raw):
    require(Path(name).name == name, 'publication_receipt_name')
    path = h.canonical_path(directory)/name
    with path.open('xb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    h.sync_directory(path.parent)
    return h.pin(path,raw)


def validate_template(template):
    require(template['schema'] == 'causes.continuous_publication_template.v1', 'publication_template_schema')
    metadata = h.canonical_path(template['metadata_directory'])
    require(set(template['targets']) == set(ROLES), 'exact_publication_roles_required')
    paths = list(template['targets'].values())
    require(len(set(paths)) == len(paths), 'duplicate_publication_target')
    for path in paths: require(h.canonical_path(path).parent == metadata, 'publication_target_scope')
    handoff = h.checked(template['watcher_handoff'])
    accepted(template['watcher_handoff'],template['watcher_handoff_review'],
             'accepted_campaign_watcher_handoff_source','configuration')
    require(h.canonical_path(handoff['directory']) == metadata/'watch', 'watcher_directory_scope')
    require(handoff['predecessor']['source']['path'] == template['targets']['watcher'], 'watcher_predecessor_scope')
    require(handoff['successor']['source']['path'] == template['targets']['watcher']
            and handoff['successor']['configuration']['path'] == template['targets']['watcher_configuration'], 'watcher_successor_scope')
    require(type(handoff['predecessor']['process']['pid']) is int, 'watcher_pid_type')
    for ref in template['preserved']:
        require(ref['path'] not in paths, 'target_cannot_be_preserved')
        h.checked_bytes(ref)
    for path in template['pause_paths']: h.canonical_path(path)
    require(same(template['pause_paths'],handoff['pause_paths']), 'watcher_pause_scope_changed')
    guard = template['waiting_guard']
    review = h.checked(guard['source_review'])
    require(review.get('accepted_continuous_admission_source') is True
            and same({k:review['source'][k] for k in ('bytes','sha256')},
                     {k:guard['source'][k] for k in ('bytes','sha256')}), 'waiting_source_not_accepted')
    package = h.checked(guard['package'])
    require(same(review['package'],guard['package']) and same(guard['sources'],package['execution_sources'])
            and guard['source'] in guard['sources']
            and Path(guard['source']['path']).name == 'campaign_admission.py'
            and str(Path(guard['source']['path']).parent.parent) == guard['pythonpath'], 'waiting_source_inventory_changed')
    for ref in guard['sources']: h.checked_bytes(ref)
    return template


def validate_observation_candidate(config):
    """Validate an immutable byte-identical deployment copy from this source tree."""
    local = copy.deepcopy(config)
    expected = {Path(module.__file__).name:h.pin(module.__file__)
                for module in (observation,monitor,observation.snapshot)}
    require(set(config['sources']) == set(expected), 'observation_source_inventory_changed')
    for name,ref in config['sources'].items():
        h.checked_bytes(ref)
        require(same({k:ref[k] for k in ('bytes','sha256')},
                     {k:expected[name][k] for k in ('bytes','sha256')}), 'observation_source_bytes_changed')
    local['sources'] = expected
    return observation.validate_config(local)


def register(inputs_ref, output):
    x = h.checked(inputs_ref)
    require(x.get('root_authorized_registration') is True, 'registration_preparation_authority_required')
    template = validate_template(h.checked(x['template']))
    adapter = accepted(template['adapter_package'],template['adapter_review'],'accepted_campaign_observation_adapters','package')
    report = accepted(x['waiting_report'],x['waiting_review'],'accepted_continuous_waiting_owner','report')
    reader = CapturedReader(report['receipt_map'])
    launch = reader(report['launch'])
    require(launch['phase'] == 'continuous' and launch['max_full'] == 0
            and launch['zero_initial_claims'] is True and launch['zero_persisted_blocks'] is True,
            'continuous_waiting_scope')
    require(same(report['process'],launch['process']) and same(report['owned_barrier'],launch['owned_barrier'])
            and report['zero_initial_claims'] is True and report['zero_persisted_blocks'] is True, 'waiting_report_binding')
    policy = copy.deepcopy(h.checked(template['monitor_policy_template']))
    phase = policy['phases'][-1]
    require(phase['phase'] == launch['phase'] and phase['mode'] == 'continuous'
            and phase['output_root'] == launch['output_root'] and phase['workers'] == launch['workers']
            and phase['runtime_cohort'] == launch['runtime_cohort'], 'continuous_policy_scope')
    require(reader(launch['initial_jobs']) == [], 'initial_jobs_not_empty')
    predecessor = h.checked(x['predecessor_authorization']); old = h.checked(predecessor['registration'])
    monitor.validate_registration(old,policy,reader)
    require(old['phase'] == policy['phases'][-2]['phase']
            and same(old['controller']['process'],launch['prior_local_owners'][-1]['process']), 'predecessor_not_current_main')
    phase.update(process=launch['process'],controller_templates=launch['controller_templates'],
                 continuation=launch['continuation'],donor_histories=launch['donor_histories'])
    for key in ('controller_jobs','controller_config'):
        require(same(phase[key],launch[key]), 'frozen_controller_input_changed')
    require(same(launch['host_transfer'],policy['host_transfer']), 'original_transfer_changed')
    source = next(row for row in adapter['code_snapshots'] if Path(row['path']).name == 'campaign_monitor.py')
    reg = {'schema':'caus.jp_companion_registration.v1','status':'active','root_authorized':True,
        'phase':launch['phase'],'controller_host':launch['controller_host'],'runtime_cohort':launch['runtime_cohort'],
        'controller_templates':launch['controller_templates'],'controller_jobs':launch['controller_jobs'],
        'controller_config':launch['controller_config'],'capacity_qualification':launch['capacity_qualification'],
        'prior_local_owners':launch['prior_local_owners'],'host_transfer':launch['host_transfer'],
        'provider':policy['provider'],'tunnels':policy['tunnels'],'reverse_listener_process':policy['reverse_listener_process'],
        'continuation':launch['continuation'],'donor_histories':launch['donor_histories'],
        'companion_source_sha256':source['sha256'],'predecessor_authorization':x['predecessor_authorization'],
        'prior_retirement':launch['retirement'],'barrier_ownership_receipt':launch['owned_barrier'],
        'admission_policy':launch['policy'],'runtime_package':launch['runtime_package'],
        'waiting_report':x['waiting_report'],'waiting_review':x['waiting_review'],'receipt_map':report['receipt_map'],
        'controller':{**{k:launch[k] for k in ('output_root','process','pythonpath','workers','source_pins','input_pins',
                                            'queue_identity','admission_control')},
                      'model':policy['model_key'],'launch_receipt':report['launch']}}
    monitor.validate_registration(reg,policy,reader)
    out = h.new_record_directory(output)
    h.save_new(out,'INPUTS.json',x)
    policy_ref = h.save_new(out,'MONITOR_POLICY.json',policy)
    reg_ref = h.save_new(out,'REGISTRATION.json',reg)
    observation_config = copy.deepcopy(h.checked(adapter['observation_configuration']))
    observation_config['monitor'] = {'policy':policy_ref,'authorization_path':template['targets']['authorization_pointer']}
    observation_config['snapshot']['roots'] = policy['sync_roots']
    validate_observation_candidate(observation_config)
    observation_ref = h.save_new(out,'OBSERVATION_CONFIG.json',observation_config)
    after = {'JP_sync_roots':h.save_new(out,'JP_SYNC_ROOTS.json',{'roots':policy['sync_roots']}),
             'watcher':adapter['watcher']['source'],'watcher_configuration':adapter['watcher']['configuration']}
    for row in adapter['entrypoints']:
        config = copy.deepcopy(h.checked(row['configuration'])); config['observation_config'] = observation_ref
        role = 'monitor' if config['action'] == 'status' else 'JP_sync'
        after[role] = row['source']
        after[role+'_configuration'] = h.save_new(out,role.upper()+'_ENTRYPOINT.json',config)
    handoff = h.checked(template['watcher_handoff'])
    for role,key in (('watcher','source'),('watcher_configuration','configuration')):
        require(same(dict(after[role],path=template['targets'][role]),handoff['successor'][key]), 'watcher_handoff_after_changed')
    configuration = {'schema':'causes.continuous_publication_configuration.v1','template':x['template'],
        'metadata_directory':template['metadata_directory'],'targets':template['targets'],'after':after,
        'monitor_policy':policy_ref,'observation_configuration':observation_ref,'companion_source':source,
        'registration':reg_ref,'waiting_report':x['waiting_report'],'waiting_review':x['waiting_review'],
        'receipt_map':report['receipt_map'],'watcher_handoff':template['watcher_handoff'],
        'preserved':template['preserved'],'pause_paths':template['pause_paths'],
        'waiting_guard':template['waiting_guard'],'adapter_package':template['adapter_package'],'adapter_review':template['adapter_review']}
    config_ref = h.save_new(out,'CONFIGURATION.json',configuration)
    return h.save_new(out,'RESULT.json',{'at':h.now(),'source':current_source(),'inputs':inputs_ref,
        'registration':reg_ref,'monitor_policy':policy_ref,'configuration':config_ref,'published':False})


def check_before(rows):
    for row in rows:
        require(same(identity(row['target']),row['before_stat']), 'publication_before_identity_changed')
        if row['before'] is not None:
            require(same(h.pin(row['target']),row['before']), 'publication_before_bytes_changed')


def check_preserved(rows):
    for row in rows:
        require(same(identity(row['target']),row['stat']) and same(h.pin(row['target']),row['pin']), 'preserved_metadata_changed')


def prepare(inputs_ref, output):
    x = h.checked(inputs_ref); config = h.checked(x['configuration'])
    require(config['schema'] == 'causes.continuous_publication_configuration.v1','publication_configuration_schema')
    template = validate_template(h.checked(config['template']))
    require(same(config['targets'],template['targets']) and config['watcher_handoff'] == template['watcher_handoff'], 'publication_template_changed')
    reg = accepted(config['registration'],x['registration_review'],'accepted_companion_registration','registration')
    policy = h.checked(config['monitor_policy']); reader = CapturedReader(config['receipt_map'])
    monitor.validate_registration(reg,policy,reader)
    pointer = {'schema':'caus.jp_companion_authorization.v1','companion_module':config['companion_source'],
               'policy':config['monitor_policy'],'registration':config['registration'],'independent_review':x['registration_review']}
    out = h.new_record_directory(output); h.save_new(out,'INPUTS.json',x)
    after = dict(config['after'],authorization_pointer=h.save_new(out,'AUTHORIZATION.CANDIDATE.json',pointer))
    rows = []
    for i,role in enumerate(ROLES):
        target = config['targets'][role]; state = identity(target)
        before = h.pin(target) if state['exists'] else None
        archive = write_bytes(out,'BEFORE_'+str(i),h.checked_bytes(before)) if before is not None else None
        h.checked_bytes(after[role])
        rows.append({'role':role,'target':target,'after':after[role],'before':before,'before_archive':archive,'before_stat':state})
    preserved = [{'target':ref['path'],'pin':ref,'stat':identity(ref['path'])} for ref in config['preserved']]
    check_before(rows); check_preserved(preserved)
    mc = monitor.MonitorConfig(config['monitor_policy'],after['authorization_pointer']['path'])
    monitor.validate_authorization(mc)
    return h.save_new(out,'PROPOSAL.json',{'at':h.now(),'schema':'causes.continuous_publication_proposal.v1',
        'source':current_source(),'configuration':x['configuration'],'watcher_handoff':config['watcher_handoff'],
        'registration':config['registration'],'registration_review':x['registration_review'],
        'monitor_policy':config['monitor_policy'],'observation_configuration':config['observation_configuration'],
        'waiting_report':config['waiting_report'],'waiting_review':config['waiting_review'],
        'rows':rows,'preserved':preserved,'requires_watcher_retirement':True,'model_requests_authorized':False})


def fresh(authority_ref):
    value = h.checked(authority_ref); now = dt.datetime.now(dt.timezone.utc)
    issued,expires = (dt.datetime.fromisoformat(value[key]) for key in ('issued_at','expires_at'))
    require(value.get('root_authorized') is True and value.get('no_user_revocation') is True
            and value['action'] == 'publish_continuous_observation' and value.get('model_requests_authorized') is False,
            'publication_authority_scope')
    require(0 <= (now-issued).total_seconds() <= 180 and now <= expires
            and 0 < (expires-issued).total_seconds() <= 180, 'publication_authority_expired')
    return value


def retired(config, authority, proposal_ref, lock_held=False):
    handoff = h.checked(config['watcher_handoff'])
    value = accepted(authority['watcher_retirement'],authority['watcher_retirement_review'],'accepted_JP_watcher_retirement','retirement')
    require(value['retired'] is True and value['lock_free'] is True
            and same(value['process'],handoff['predecessor']['process']) and same(value['source'],handoff['predecessor']['source'])
            and same(value['configuration'],config['watcher_handoff']) and same(value['proposal'],proposal_ref), 'watcher_retirement_binding')
    state = h.retirement_state(value['process'])
    require(state['absent'] or state.get('state') in ('Z','X','x'), 'previous_watcher_live')
    directory = h.canonical_path(handoff['directory'])
    lock = h.stat_identity(h.canonical_path(directory/'watch.lock'))
    require(stat.S_ISREG(lock['st_mode'])
            and same([lock['st_dev'],lock['st_ino']],value['lock_identity']), 'retired_watcher_lock_identity_changed')
    if not lock_held: require(h.lock_free(directory/'watch.lock'), 'watcher_lock_not_free')
    owner = h.checked(value['stop_ownership']); stop = directory/'STOP'
    require(owner['marker'] == value['owned_stop'] and owner['marker']['path'] == str(stop), 'watcher_STOP_scope')
    require(h.checked_bytes(owner['marker']) == h.checked_bytes(owner['archived_marker']), 'watcher_STOP_bytes_changed')
    actual = h.stat_identity(stop)
    require(same({key:actual[key] for key in owner['stat']},owner['stat']), 'watcher_STOP_identity_changed')
    cycles = sorted(p for p in directory.iterdir() if p.is_dir() and re.fullmatch(r'[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}',p.name))
    require(cycles and str(cycles[-1]) == value['final_cycle_directory']
            and value['final_completed_cycle']['path'] == str(cycles[-1]/'RESULT.json'), 'newest_watch_cycle_not_closed')
    cycle = h.checked(value['final_completed_cycle'])
    require(isinstance(cycle.get('checks'),list) and bool(cycle.get('ended_at')), 'newest_watch_cycle_not_closed')
    return value


WAITING_REMOTE = r'''
import hashlib,importlib,json,sys
from pathlib import Path
x=json.load(sys.stdin)
for ref in x['sources']:
 p=Path(ref['path']);assert p.resolve()==p and not any(q.is_symlink() for q in (p,*p.parents))
 raw=p.read_bytes();assert len(raw)==ref['bytes'] and hashlib.sha256(raw).hexdigest()==ref['sha256']
sys.path.insert(0,x['pythonpath'])
m=importlib.import_module('causes.campaign_admission')
assert str(Path(m.__file__).resolve())==x['source']['path']
assert x['source'] in x['sources']
print(json.dumps(m.observe_waiting(x['policy'],x['launch'])))
'''


def observe_candidate(proposal, config, out):
    candidate = next(r['after'] for r in proposal['rows'] if r['role'] == 'authorization_pointer')
    observed_config = h.checked(config['observation_configuration'])
    validate_observation_candidate(observed_config)
    mc = monitor.MonitorConfig(config['monitor_policy'],candidate['path'])
    value = monitor.observe(mc,observation.LegacyObserver(observed_config))
    h.save_new(out,'COMPANION.json',value)
    require(value['valid'] is True and value['required'] is True and not any(value['alarms'].values()), 'candidate_monitor_unhealthy')
    reg = h.checked(proposal['registration']); guard = dict(config['waiting_guard'],
        policy=reg['admission_policy'],launch=reg['controller']['launch_receipt'])
    require(guard['host'] == mc.policy['controller_host'], 'waiting_guard_host_mismatch')
    h.save_new(out,'WAITING_REQUEST.json',guard)
    result = subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8',guard['host'],'python3','-c',shlex.quote(WAITING_REMOTE)],
                            input=h.encoded(guard),capture_output=True,timeout=35)
    stdout = write_bytes(out,'WAITING.stdout',result.stdout); stderr = write_bytes(out,'WAITING.stderr',result.stderr)
    h.save_new(out,'WAITING_EXECUTION.json',{'at':h.now(),'returncode':result.returncode,'stdout':stdout,'stderr':stderr})
    require(result.returncode == 0,'waiting_guard_failed')
    waiting = observation.strict_json(result.stdout)
    for key in ('zero_initial_claims','zero_persisted_blocks','previous_owners_retired','controls_clear'):
        require(waiting[key] is True,'waiting_guard_missing:'+key)
    require(same(waiting['launch'],reg['controller']['launch_receipt']) and same(waiting['process'],reg['controller']['process'])
            and same(waiting['owned_barrier'],reg['barrier_ownership_receipt']), 'waiting_guard_owner_changed')
    return h.save_new(out,'WAITING.json',waiting)


def apply(authority_ref, output, execute=False):
    authority = fresh(authority_ref)
    proposal = accepted(authority['proposal'],authority['proposal_review'],'accepted_continuous_publication_candidate','proposal')
    require(proposal['source']['sha256'] == current_source()['sha256']
            and proposal['source']['bytes'] == current_source()['bytes'], 'publication_source_changed')
    config = h.checked(proposal['configuration']); template = validate_template(h.checked(config['template']))
    require(same(proposal['watcher_handoff'],config['watcher_handoff']) and proposal['requires_watcher_retirement'] is True,
            'watcher_handoff_required')
    require([r['role'] for r in proposal['rows']] == list(ROLES)
            and [r['target'] for r in proposal['rows']] == [template['targets'][role] for role in ROLES], 'publication_target_inventory')
    for row in proposal['rows']: h.checked_bytes(row['after'])
    check_before(proposal['rows']); check_preserved(proposal['preserved'])
    for path in config['pause_paths']:require(not os.path.lexists(h.canonical_path(path)), 'publication_pause_present')
    retired(config,authority,authority['proposal'])
    if not execute:return {'validated':True,'executed':False,'proposal':authority['proposal']}
    out = h.new_record_directory(output); done = []; pending = None
    h.save_new(out,'ROOT_AUTHORITY.json',authority); h.save_new(out,'PROPOSAL.json',proposal)
    directory = h.canonical_path(h.checked(config['watcher_handoff'])['directory'])
    publication_lock_path = h.canonical_path(str(h.canonical_path(config['metadata_directory'])/'.CONTINUOUS_PUBLICATION.lock'))
    with publication_lock_path.open('a+b') as publication_lock:
        fcntl.flock(publication_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with (directory/'watch.lock').open('rb') as watch_lock:
            fcntl.flock(watch_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            def locks_unchanged():
                for path,stream in ((publication_lock_path,publication_lock),(directory/'watch.lock',watch_lock)):
                    a,b = h.canonical_path(path).stat(),os.fstat(stream.fileno())
                    require((a.st_dev,a.st_ino) == (b.st_dev,b.st_ino),'publication_lock_inode_changed')
            try:
                locks_unchanged()
                fresh(authority_ref);retired(config,authority,authority['proposal'],True)
                check_before(proposal['rows']);check_preserved(proposal['preserved'])
                observe_candidate(proposal,config,out);fresh(authority_ref)
                h.save_new(out,'INTENT.json',{'at':h.now(),'authority':authority_ref,'proposal':authority['proposal'],
                    'ordered_targets':[r['target'] for r in proposal['rows']],
                    'scope':'Metadata only; watcher STOP and actor barrier remain; no actor/provider requests or restart.'})
                for index,row in enumerate(proposal['rows']):
                    locks_unchanged()
                    fresh(authority_ref);retired(config,authority,authority['proposal'],True)
                    for path in config['pause_paths']:require(not os.path.lexists(h.canonical_path(path)), 'publication_pause_present')
                    check_before([row]);check_preserved(proposal['preserved'])
                    raw=h.checked_bytes(row['after']);write_bytes(out,'AFTER_'+str(index),raw)
                    if row['before'] is not None:write_bytes(out,'BEFORE_'+str(index),h.checked_bytes(row['before_archive']))
                    h.save_new(out,'STEP_'+str(index)+'_INTENT.json',row)
                    target=h.canonical_path(row['target']);pending=target.parent/('.'+target.name+'.continuous_'+uuid.uuid4().hex)
                    write_bytes(pending.parent,pending.name,raw)
                    if row['before_stat']['exists']:
                        os.chmod(pending,stat.S_IMODE(row['before_stat']['st_mode']));os.chown(pending,row['before_stat']['st_uid'],row['before_stat']['st_gid'])
                    else:os.chmod(pending,0o600)
                    with pending.open('rb') as stream:os.fsync(stream.fileno())
                    h.sync_directory(pending.parent);check_before([row]);fresh(authority_ref);locks_unchanged()
                    os.replace(pending,target);h.sync_directory(target.parent);done.append(str(target))
                    require(target.read_bytes()==raw,'publication_readback_failed')
                    h.save_new(out,'STEP_'+str(index)+'_RESULT.json',{'at':h.now(),'actual':h.pin(target),'stat':identity(target)})
                check_preserved(proposal['preserved'])
                return h.save_new(out,'RESULT.json',{'at':h.now(),'proposal':authority['proposal'],'applied':done,
                    'current':[h.pin(row['target']) for row in proposal['rows']]+[row['pin'] for row in proposal['preserved']],
                    'watcher_restart_ordinary_custody_and_barrier_release_pending':True,'requests_authorized':False})
            except BaseException as error:
                h.save_new(out,'FAILED.json',{'at':h.now(),'error_type':type(error).__name__,'error':str(error)[:500],
                    'applied':done,'pending':str(pending) if pending is not None else None,
                    'partial_publication_possible':bool(done),'no_automatic_retry':True})
                raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('register','prepare','apply'))
    parser.add_argument('--input',required=True,type=Path);parser.add_argument('--input-sha256',required=True)
    parser.add_argument('--output',required=True,type=Path);parser.add_argument('--execute',action='store_true')
    args=parser.parse_args();ref=h.pin(args.input)
    require(ref['sha256']==args.input_sha256,'publication_input_changed')
    require(args.action=='apply' or not args.execute,'execute_flag_only_for_apply')
    result=apply(ref,args.output,args.execute) if args.action=='apply' else globals()[args.action](ref,args.output)
    print(json.dumps(result))


if __name__ == '__main__':main()
