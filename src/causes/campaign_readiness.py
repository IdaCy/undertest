"""Bind one retained ordinary watcher cycle to continuous startup readiness.

Only local metadata is read and copied. This does not synchronize evidence,
inspect live processes, authorize a release, or reinterpret generated results.
"""
from pathlib import Path
from collections import Counter
import argparse
import datetime as dt
import hashlib
import json
import os

from causes import campaign_handoff as h
from causes import campaign_monitor as monitor


def require(value,message):
    if not value:raise ValueError(message)


def stamp(value):
    value=dt.datetime.fromisoformat(value)
    require(value.tzinfo is not None,'timezone_required')
    return value


def fresh(value):
    require(0<=(dt.datetime.now(dt.timezone.utc)-stamp(value)).total_seconds()<=180,'readiness_observation_stale')


def accepted(reference,review,flag,key):
    value=h.checked(review)
    require(value.get(flag) is True and value.get(key)==reference,'unaccepted_readiness_binding:'+flag)
    return h.checked(reference)


def same_identity(left,right):
    return (type(left['pid']) is int and type(right['pid']) is int and left['pid']==right['pid']
            and str(left['process_starttime_ticks'])==str(right['process_starttime_ticks']) and left['argv']==right['argv'])


def captured_bytes(mapping,reference):
    rows=[row for row in h.checked(mapping)['files'] if row['remote']==reference]
    require(len(rows)==1,'unique_receipt_capture_required')
    raw=h.checked_bytes(rows[0]['local'])
    require(len(raw)==reference['bytes'] and hashlib.sha256(raw).hexdigest()==reference['sha256'],'capture_bytes_changed')
    return raw


def captured(mapping,reference):return json.loads(captured_bytes(mapping,reference))


def save_bytes(path,raw):
    path=h.canonical_path(path)
    with path.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    h.sync_directory(path.parent)
    return h.pin(path,raw)


def stat_identity(path):
    value=h.canonical_path(path).stat()
    return {k:getattr(value,k) for k in ('st_dev','st_ino','st_mode','st_size','st_mtime_ns','st_ctime_ns')}


def stable_copy(reference,directory,name):
    before=stat_identity(reference['path']);raw=h.checked_bytes(reference)
    require(stat_identity(reference['path'])==before,'metadata_changed_during_read')
    return dict(original=reference,copy=save_bytes(directory/name,raw),stat=before)


def watcher_check(inputs,source,cycle_start,cycle_end):
    proof=accepted(inputs['watcher_successor'],inputs['watcher_successor_review'],'accepted_JP_local_watcher_successor','evidence')
    current=h.checked(inputs['watcher_current_observation'])
    require(proof['source']==current['source']==source and same_identity(proof['process'],current['process']),
            'same_watcher_identity_required')
    for value in (proof,current):require(value.get('lock_owned') is True and value.get('stop_absent') is True,'watcher_lock_or_stop_changed')
    require(stamp(current['at'])>=cycle_end,'current_watcher_predates_cycle');fresh(current['at'])
    process=h.checked(proof['process_receipt']);start=h.checked(proof['start_receipt'])
    require(same_identity(process,proof['process']) and start['pid']==proof['process']['pid']
            and start['script_sha256']==source['sha256'] and source['path'] in proof['process']['argv'],'watcher_process_receipt_changed')
    require(stamp(start['started_at'])<=cycle_start,'cycle_predates_successor')
    for key in ('prior_retirement','prior_retirement_review','stop_retirement'):h.checked_bytes(proof[key])
    require(current['raw_pins'],'current_watcher_raw_evidence_required')
    for ref in current['raw_pins']:h.checked_bytes(ref)
    return proof,current


def sync_check(cycle,script,scope,output,label):
    stream=cycle/(script+'.stdout');body=json.loads(stream.read_bytes());attempt=h.canonical_path(body['attempt'])
    destination=h.canonical_path(scope['destination']);roots=scope['roots']
    require(attempt.parent==destination/'attempts','wrong_sync_destination')
    pins={n:h.pin(attempt/(n+'.json')) for n in ('REQUEST','MANIFEST','VERIFIED')}
    request,manifest,verified=[h.checked(pins[n]) for n in ('REQUEST','MANIFEST','VERIFIED')]
    require(request['base']==manifest['base']==scope['remote_base'] and request['roots']==roots
            and manifest['host']==scope['host'] and request.get('host',scope['host'])==scope['host'],'sync_host_or_roots_changed')
    rows=verified['runs'];original={r['run']:r for r in manifest['runs']}
    require(len(original)==len(manifest['runs'])==len(rows) and len({r['run'] for r in rows})==len(rows)
            and body['verified_runs']==len(rows) and body['verified_bytes']==sum(r['bytes'] for r in rows),'sync_count_changed')
    for row in rows:
        rel=Path(row['run'])
        require(not rel.is_absolute() and '..' not in rel.parts and str(rel.parent) in roots
                and row['path']==str(attempt/'data'/rel) and row['manifest']==pins['MANIFEST']['path']
                and row['manifest_sha256']==pins['MANIFEST']['sha256'],'sync_receipt_path_changed')
        source=original[row['run']]
        require((row['files'],row['bytes'],row['summary_sha256'])==(len(source['files']),source['bytes'],source['summary_sha256']),
                'sync_manifest_join_changed')
    require(body['skipped']==manifest['skipped'],'sync_skipped_disposition_changed')
    path=destination/'accepted.jsonl';before=h.now();raw=path.read_bytes();after=h.now()
    prefix=raw[:raw.rfind(b'\n')+1];parsed=[json.loads(line) for line in prefix.splitlines()]
    indexed=Counter(h.encoded(row) for row in parsed)
    for row in rows:require(indexed[h.encoded(row)]==1,'verified_row_not_once_in_retained_index')
    retained=save_bytes(output/(label+'_INDEX_PREFIX.jsonl'),prefix)
    return dict(stdout=h.pin(stream),attempt=str(attempt),request=pins['REQUEST'],manifest=pins['MANIFEST'],verified=pins['VERIFIED'],
                roots=roots,verified_runs=len(rows),verified_run_ids=[r['run'] for r in rows],verified_bytes=body['verified_bytes'],
                skipped=manifest['skipped'],index=dict(source_path=str(path),prefix=retained,read_started_at=before,read_finished_at=after,
                bytes_read=len(raw),unconsumed_tail_bytes=len(raw)-len(prefix),matching_verified_rows_once=True),no_new_snapshot_content_rehash=True)


def waiting_progress(observation,launch):
    require(same_identity(observation['controller'],launch['process']) and observation['lock_owner']==[launch['process']['pid']]
            and observation.get('prior_local_owners_closed') is True,'waiting_owner_changed')
    journal=observation['journal'];counts=journal['event_counts']
    require(set(counts)<={'controller_started','admission_holds_bound','admission_barrier_waiting'}
            and counts.get('controller_started')==1 and counts.get('admission_barrier_waiting')==1
            and journal['lifetime_route_failures']==0 and not journal['quarantines'] and journal['unconsumed_tail_bytes']==0,
            'waiting_owner_issued_or_faulted')
    continuation=observation['continuation'];progress=continuation['progress']
    require(h.encoded(progress)==h.encoded(dict(waiting_behind_barrier=True,initial_jobs=0,claimed=0,published_blocks=0,first_block_observed=False))
            and continuation['first_published_block'] is None and continuation['control']==launch['continuation']['control']
            and continuation['binding']==launch['continuation']['binding'],'continuous_waiting_progress_changed')


def publication_complete(publication,proposal):
    targets=[row['target'] for row in proposal['rows']]
    expected=[dict(row['after'],path=row['target']) for row in proposal['rows']]
    expected.extend(row['pin'] for row in proposal['preserved'])
    require(targets and len(set(targets))==len(targets)
            and publication.get('applied')==targets,'publication_not_completely_applied')
    require(all(row['pin']['path']==row['target'] for row in proposal['preserved'])
            and len({row['path'] for row in expected})==len(expected)
            and publication['current']==expected,'publication_final_pins_changed')


def build_readiness(config_reference,inputs_reference,review_reference,output):
    cfg=h.checked(config_reference);inputs=h.checked(inputs_reference);review=h.checked(review_reference)
    require(cfg['schema']=='causes.continuous_readiness_config.v1' and review.get('accepted_continuous_readiness_source') is True
            and review.get('configuration')==config_reference and review['source']['sha256']==h.pin(__file__)['sha256'],
            'unaccepted_readiness_source_or_config')
    require(cfg['root_counts']=={'MI':13,'JP':4},'continuous_readiness_root_contract_changed')
    for module in (__file__,h.__file__,monitor.__file__):
        require(cfg['source_hashes'][Path(module).name]==h.pin(module)['sha256'],'readiness_module_changed')
    publication=accepted(inputs['publication_result'],inputs['publication_review'],'accepted_continuous_publication_actual','result')
    proposal=h.checked(publication['proposal']);publication_complete(publication,proposal)
    reg=h.checked(proposal['registration']);policy=h.checked(proposal['monitor_policy'])
    accepted(proposal['registration'],proposal['registration_review'],'accepted_companion_registration','registration')
    monitor.validate_registration(reg,policy,reader=h.checked)
    waiting=accepted(inputs['waiting_report'],inputs['waiting_review'],'accepted_continuous_waiting_owner','report')
    launch=captured(waiting['receipt_map'],waiting['launch']);owned=captured(waiting['receipt_map'],launch['owned_barrier'])
    require(reg['phase']==launch['phase']==cfg['phase'] and waiting['launch']==reg['controller']['launch_receipt']
            and launch['process']==reg['controller']['process'] and launch['owned_barrier']==reg['barrier_ownership_receipt']
            and waiting['process']==launch['process'] and waiting['owned_barrier']==launch['owned_barrier'], 'waiting_launch_binding_changed')
    for key in ('zero_initial_claims','zero_persisted_blocks'):require(waiting.get(key) is True and launch.get(key) is True,'waiting_scope_not_empty')
    require(owned['output_root']==launch['output_root'] and owned['marker']['path']==cfg['barrier_path'],'owned_barrier_scope_changed')
    require(captured_bytes(waiting['receipt_map'],owned['marker'])==captured_bytes(waiting['receipt_map'],owned['archived_marker']),
            'owned_barrier_archive_changed')
    cycle_reference=inputs['cycle_result'];cycle=h.canonical_path(cycle_reference['path']).parent;result=h.checked(cycle_reference)
    require(cycle.parent==h.canonical_path(cfg['cycle_directory']) and Path(cycle_reference['path']).name=='RESULT.json','not_ordinary_cycle_path')
    start=dt.datetime.strptime(cycle.name.split('_')[0],'%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc);end=stamp(result['ended_at'])
    require(stamp(publication['at'])<start<end,'cycle_does_not_follow_publication')
    scripts=[row['script'] for row in result['checks']]
    require(scripts in (cfg['scripts'],cfg['scripts']+cfg['optional_tail_scripts'])
            and all(row['returncode']==0 and row['timeout'] is False for row in result['checks']),'ordinary_cycle_failed')
    current={r['path']:r for r in publication['current']}
    require(len(current)==len(publication['current']),'duplicate_publication_target')
    require(set(current)<=set(cfg['live_metadata_paths'].values()),'publication_target_missing_from_immutable_custody')
    live={}
    for key,path in cfg['live_metadata_paths'].items():
        reference=h.pin(path)
        if path in current:require(reference==current[path],'publication_target_changed')
        else:
            preserved=[r for r in proposal['preserved'] if r['target']==path]
            require(len(preserved)==1 and preserved[0]['pin']==reference,'unbound_preserved_metadata')
        live[key]=reference
    for reference in publication['current']:h.checked_bytes(reference)
    watcher,watcher_current=watcher_check(inputs,live['watcher_source'],start,end)
    lines=(cycle/(cfg['status_script']+'.stdout')).read_text().splitlines()
    status_ref=h.pin(h.canonical_path(lines[0]));status=h.checked(status_ref)
    require(json.loads('\n'.join(lines[1:]))==status,'status_stdout_changed')
    companion=status['companion']
    require(not any(status['alarms'].values()) and companion.get('required') is True and companion.get('valid') is True
            and not any(companion['alarms'].values()),'monitor_not_healthy')
    monitor.validate_legacy(status,policy)
    require(start<=stamp(companion['started_at'])<=stamp(companion['at'])<=end,'companion_outside_cycle');fresh(companion['at'])
    require(companion['registration']==proposal['registration'] and companion['review']==proposal['registration_review']
            and companion['authorization']==live['authorization_pointer'] and companion['policy']==proposal['monitor_policy'],
            'companion_registration_changed')
    hosts={r['host']:r['observation'] for r in companion['hosts']}
    require(len(hosts)==len(companion['hosts'])==2 and set(hosts)=={policy['controller_host'],policy['transfer_host']}
            and all(v.get('valid') is True for v in hosts.values()),'both_host_observations_required')
    waiting_progress(hosts[policy['controller_host']],launch)
    require(hosts[policy['transfer_host']].get('unused_MI_output_absent') is True,'unused_MI_root_not_absent')
    pointer=h.checked(live['authorization_pointer'])
    require(pointer['registration']==proposal['registration'] and pointer['independent_review']==proposal['registration_review'],
            'authorization_pointer_changed')
    for label in ('MI','JP'):
        roots=h.checked(live[label+'_sync_roots'])['roots']
        require(roots==cfg['sync'][label]['roots'] and len(set(roots))==len(roots)==cfg['root_counts'][label], 'sync_root_list_changed')
    require(cfg['sync']['MI']['roots']==policy['legacy']['sync_roots_value']['roots']
            and cfg['sync']['JP']['roots']==policy['sync_roots'],'monitor_sync_scope_changed')
    out=h.canonical_path(output);out.mkdir(exist_ok=False);h.sync_directory(out.parent)
    try:
        save_bytes(out/'INPUTS.json',h.checked_bytes(inputs_reference))
        copies={k:stable_copy(ref,out,k.upper()+'.observed') for k,ref in live.items()}
        copies['monitor_policy']=stable_copy(proposal['monitor_policy'],out,'MONITOR_POLICY.observed')
        syncs={label:sync_check(cycle,cfg['sync'][label]['script'],cfg['sync'][label],out,label) for label in ('MI','JP')}
        waiting_root=str(Path(launch['output_root']).relative_to(cfg['sync']['JP']['remote_base']))
        require(not any(str(Path(r).parent)==waiting_root for r in syncs['JP']['verified_run_ids']),'waiting_root_has_snapshot_data')
        serving=h.pin(h.canonical_path((cycle/(cfg['serving_script']+'.stdout')).read_text().splitlines()[0]))
        streams=[stable_copy(h.pin(cycle/(script+suffix)),out,'CYCLE_'+script+suffix) for script in scripts for suffix in ('.stdout','.stderr')]
        watch=h.save_new(out,'WATCH_CYCLE.json',dict(at=companion['at'],started_at=start.isoformat(),ended_at=result['ended_at'],
            cycle_result=cycle_reference,cycle_streams=streams,status=status_ref,serving_activity=serving,companion=companion,
            publication_result=inputs['publication_result'],publication_review=inputs['publication_review'],
            watcher_successor=inputs['watcher_successor'],watcher_successor_review=inputs['watcher_successor_review'],
            current_watcher_observation=inputs['watcher_current_observation'],ordinary_watcher=watcher['process'],
            MI_sync=syncs['MI'],JP_sync=syncs['JP'],all_checks_passed=True))
        for row in copies.values():
            require(h.pin(row['original']['path'])==row['original'] and stat_identity(row['original']['path'])==row['stat'],
                    'metadata_changed_before_readiness_seal')
        readiness=dict(at=companion['at'],phase=cfg['phase'],launch=waiting['launch'],owned_barrier=launch['owned_barrier'],
            zero_initial_claims=True,zero_persisted_blocks=True,legacy_MI_Tex_healthy=True,mandatory_JP_companion_valid=True,
            all_13_MI_roots_requested=cfg['root_counts']['MI']==13,all_4_JP_roots_requested=cfg['root_counts']['JP']==4,
            same_owned_watcher_successor=True,registration=proposal['registration'],watcher_cycle=watch,
            **{k:copies[k]['copy'] for k in ('authorization_pointer','monitor_policy','MI_sync_roots','JP_sync_roots')},
            live_metadata_provenance=copies,inputs=inputs_reference,configuration=config_reference,source_review=review_reference,
            publication_result=inputs['publication_result'],publication_review=inputs['publication_review'],
            waiting_report=inputs['waiting_report'],waiting_review=inputs['waiting_review'],
            limits='Retained ordinary cycle, qualified sync manifest/receipt/index joins and immutable metadata copies. No new archive rehash, synchronization, process inspection, model call or control action. Observation time is the companion cut.')
        ref=h.save_new(out,'READINESS.json',readiness)
        h.save_new(out,'RESULT.json',dict(at=h.now(),readiness=ref,independent_acceptance_pending=True,
            required_review=dict(accepted_continuous_startup_watch=True,readiness=ref)))
        return ref
    except BaseException as error:
        h.save_new(out,'FAILED.json',dict(at=h.now(),error_type=type(error).__name__,error=str(error)[:500],partial_metadata_preserved=True))
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('config','inputs','review'):parser.add_argument('--'+name,required=True,help='Exact JSON pin')
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    print(json.dumps(build_readiness(*(json.loads(getattr(args,k)) for k in ('config','inputs','review')),args.output),sort_keys=True))


if __name__=='__main__':main()
