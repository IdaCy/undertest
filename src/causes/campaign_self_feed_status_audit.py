"""Audit saved self-copy status, including completed original copier custody."""
import argparse,collections,datetime,hashlib,importlib.util,json,os,socket,subprocess,sys
from pathlib import Path
from unittest.mock import patch

def audit(request_ref,actual_ref,execution_ref,reviewer,write=False,auditor_review_ref=None):
 refs={}
 def pin(p):
  p=Path(p).resolve();z=p.read_bytes();return dict(path=str(p),bytes=len(z),sha256=hashlib.sha256(z).hexdigest())
 def raw(r):
  assert pin(r['path'])==r,r;refs[r['path']]=r;return Path(r['path']).read_bytes()
 def ck(r):return json.loads(raw(r))
 def file(p):return ck(pin(p))
 request=ck(request_ref);assert request['schema']=='causes.subset_self_status_request.v1'
 assert request['authorized'] is True and request['maximum_captures']==1 and request['automatic_retry'] is False
 P=Path(request['feed_directory']);O=Path(actual_ref['path']).parent
 assert O==Path(request['output']) and O.parent==P and O.resolve()==O and not O.is_symlink()
 prep=ck(request['source_preparation']);sp=ck(request['source_preparation_review'])
 assert sp['accepted_subset_self_status_capture_preparation'] is True and sp['accepted_subset_self_status_saved_auditor'] is True and not sp.get('findings')
 assert sp['preparation']==request['source_preparation'] and sp['source']==prep['source'] and sp['saved_auditor']==prep['saved_auditor']
 if auditor_review_ref is None:
  assert prep['saved_auditor']==pin(__file__)
 else:
  arp=ck(auditor_review_ref)
  assert arp['accepted_subset_self_status_saved_auditor_successor'] is True and not arp.get('findings')
  assert arp['source']==pin(__file__) and arp['parent']==prep['saved_auditor'] and arp['capture_preparation']==request['source_preparation']
  raw(arp['parent'])
 startup=ck(request['startup']);startup_peer=ck(request['startup_review'])
 assert startup_peer['accepted_individual_variant_self_probe_feed_startup_actual'] is True and startup_peer['actual']==request['startup'] and not startup_peer.get('findings')
 previous=ck(request['previous_actual']);previous_peer=ck(request['previous_review'])
 assert previous_peer['actual']==request['previous_actual'] and not previous_peer.get('findings') and (previous_peer.get('accepted_individual_variant_self_probe_feed_startup_actual') is True or previous_peer.get('accepted_individual_variant_self_probe_feed_index_cut') is True)
 execution=ck(execution_ref);ei=ck(execution['intent'])
 assert execution['returncode']==0 and execution['error'] is None and raw(execution['stderr'])==b'' and ck(execution['stdout'])==actual_ref
 assert ei['request']==request_ref and ei['source']==prep['source'] and ei['maximum_invocations']==1 and ei['automatic_retry'] is False
 assert ei['argv']==[request['interpreter'],'-B',prep['source']['path'],json.dumps(request_ref,sort_keys=True,separators=(',',':'))]
 ap=pin(O/'ACTUAL.v1.json');assert ap==actual_ref
 a=ck(ap);assert a['schema']=='causes.individual_variant_self_probe_feed_index_cut_actual.v1' and a['request']==request_ref and a['source']==prep['source'] and a['new_startup_claimed'] is False and a['observation_kind']=='dated_copy_progress'
 assert a['plan']==startup['plan']==previous['plan'] and a['packet']==startup['packet']==previous['packet']
 plan=ck(a['plan']);review=ck(a['source_review']);raw(a['source']);raw(plan['source'])
 assert review['accepted_individual_variant_self_probe_feed_preparation']is True and review['plan']==a['plan'] and review['source']==plan['source']
 derivref=prep['capture_derivation'];deriv=ck(derivref);assert deriv['source']==a['source']
 text=raw(deriv['parent']).decode();assert text.startswith(deriv['parent_header']);text=text[len(deriv['parent_header']):]
 for before,after in deriv['body_changes']:
  assert text.count(before)==1;text=text.replace(before,after)
 import textwrap
 assert (deriv['new_header']+textwrap.indent(text,deriv['indent'])+deriv['footer']).encode()==raw(a['source'])
 config=ck(plan['configuration']);reg=ck(plan['registration']);assert review['configuration']==plan['configuration'] and review['registration']==plan['registration']
 assert config['reader_inputs_only']is plan['reader_inputs_only']is False and config['evidence_profile']==plan['evidence_profile']=='early_self_probes' and config['limit']==8 and config['free_space_floor_bytes']==80*1024**3
 assert reg['execution_host']==request['host']==P.parent.name and config['destination']==plan['destination'] and config['remote_base']==plan['remote_base']
 selected=config['selected_runs'];assert len(selected)==len(set(selected))==len(reg['selected_run_ids'])
 assert selected==[str(Path(reg['output_root']).relative_to(reg['remote_base'])/run)for run in reg['selected_run_ids']]
 packet=ck(a['packet']);da=ck(a['durable_adoption']);launch=ck(a['root_launch']);rootadref=pin(P/'ROOT_ADOPTION.v1.json');rootad=ck(rootadref)
 assert da['root_authorized']is True and da['packet']==a['packet'] and da['attempt_id']==packet['attempt_id'] and da['maximum_invocations_per_actor']==1 and da['automatic_retry']is False
 assert rootad['root_authorized']is True and rootad['plan']==a['plan'] and rootad['review']==a['source_review'] and rootad['automatic_retry']is False and rootad['maximum_invocations']==1
 assert launch['returncode']==0 and launch['stderr']==''
 assert len(packet['actors'])==1 and packet['actors'][0]['name']=='copier' and packet['actors'][0]['source']==plan['source']
 for ref in packet['actors'][0]['pins']:raw(ref)
 raw(packet['supervisor_source']);actor=packet['actors'][0];argv=actor['argv']
 assert json.loads(argv[argv.index('--plan')+1])==a['plan'] and json.loads(argv[argv.index('--review')+1])==a['source_review'] and json.loads(argv[argv.index('--adoption')+1])==rootadref
 dur=Path(packet['output_directory']);watch=Path(plan['watch_directory'])
 native={path:ck(ref)for path,ref in a['native'].items()}
 intent=native[str(dur/'INTENT.json')];process=native[str(dur/'copier/PROCESS.json')];ai=native[str(dur/'copier/INTENT.json')]
 assert intent['packet']==ai['packet']==a['packet'] and intent['adoption']==ai['adoption']==a['durable_adoption']
 assert ai['actor']==actor and process['intent']==a['native'][str(dur/'copier/INTENT.json')]
 assert intent['attempt_id']==ai['attempt_id']==packet['attempt_id'] and intent['automatic_retry']is ai['automatic_retry']is False
 assert process['identity']['argv']==argv
 expected={'supervisor':intent['supervisor'],'copier':process['identity']}
 completed=a['lifecycle']=='completed';assert a['lifecycle'] in ('live','completed')
 completion=None;c=None
 if completed:
  assert request.get('completed_feed_receipt') is not None and a['completed_helper_source']==prep['completed_helper_source']
  helper=prep['completed_helper_source'];raw(helper);spec=importlib.util.spec_from_file_location('saved_self_completed_custody',helper['path']);c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
  completion=c.completed_receipts(request['completed_feed_receipt'],dict(startup,_path=request['startup']['path']),packet,plan,expected)
  assert completion==a['completed_feed']
  for ref in completion['records'].values():raw(ref)
  for side in ('before','after'):
   retired=a[side];assert retired['original_instances_retired'] is True and set(retired['processes'])==set(expected)
   for name,e in expected.items():
    z=retired['processes'][name];assert z['original']==e and z['original_instance_retired'] is True
    if z['absent']:assert z['raw_stat'] is None
    else:
     stat=z['raw_stat'];parts=stat[stat.rindex(')')+2:].split();assert int(stat.split()[0])==e['pid'] and (parts[19]!=str(e['start_ticks']) or parts[0] in ('Z','X'))
 else:
  assert request.get('completed_feed_receipt') is None and a['completed_feed'] is None and a['completed_helper_source'] is None
  for side in ('before','after'):
   for name,e in expected.items():
    z=a[side][name];ident=z['identity'];assert {k:ident[k]for k in ('pid','start_ticks','argv')}=={k:e[k]for k in ('pid','start_ticks','argv')} and ident['state'] not in ('Z','X')
    stat=z['raw_stat'];parts=stat[stat.rindex(')')+2:].split();assert int(stat.split()[0])==ident['pid'] and parts[0] not in ('Z','X') and parts[19]==ident['start_ticks']
 lock=a['watch_lock'];assert lock['path']==str(watch/'watch.lock')
 assert {k:lock[k] for k in ('path','device','inode')}=={k:startup['watch_lock'][k] for k in ('path','device','inode')}
 key=f"{os.major(lock['device']):02x}:{os.minor(lock['device']):02x}:{lock['inode']}"
 rows=[s for s in lock['proc_locks_raw'].splitlines()if key in s.split()]
 if completed:
  assert rows==lock['rows']==[]
  for side in ('before','after'):assert a[side]['proc_locks_path']=='/proc/locks' and [row for row in a[side]['raw_locks'].splitlines()if key in row.split()]==a[side]['lock_rows']==[]
 else:assert rows==lock['rows'] and rows and len({tuple(row.split()[1:]) for row in rows})==1 and rows[0].split()[1:4]==['FLOCK','ADVISORY','WRITE'] and int(rows[0].split()[4])==expected['copier']['pid']
 start=native[str(watch/'START.json')];assert start['configuration']==config and start['interval_seconds']==plan['interval_seconds']==30 and start['automatic_retry']is False
 auth=start['authorization'];assert auth['custody_kind']=='actor_self_probe_evidence' and all(ref in auth['evidence']for ref in (a['plan'],a['source_review'],rootadref,plan['source'],plan['configuration'],plan['registration']))
 for ref in auth['evidence']:raw(ref)
 idxraw=raw(a['index']['saved']);assert idxraw.endswith(b'\n') and len(idxraw)==a['index']['bytes'] and hashlib.sha256(idxraw).hexdigest()==a['index']['sha256']
 index=[json.loads(x)for x in idxraw.splitlines()];assert len(index)==len({r['run']for r in index})==a['accepted'] and a['remaining']==len(selected)-len(index)
 assert idxraw.startswith(raw(startup['index']['saved'])) and idxraw.startswith(raw(previous['index']['saved']))
 assert datetime.datetime.fromisoformat(a['at'])>=datetime.datetime.fromisoformat(previous['at'])>=datetime.datetime.fromisoformat(startup['at'])
 assert previous['accepted']<=a['accepted'] and previous['remaining']>=a['remaining']
 assert {r['run']for r in index}<=set(selected) and a['index']['original_path']==plan['monitor_paths']['index']
 assert a['stderr_bytes']==0 and a['model_calls']==0 and a['producer_liveness_queried']is False and a['all_selected_self_probe_copies_complete']is completed and a['opaque_self_probe_custody_only']is True
 expected_absent=[dur/'LAUNCH_ERROR.json',watch/'STOP',watch/'STOPPED.json',watch/'FAILED.json']
 if not completed:expected_absent += [dur/'COMPLETE.json',dur/'copier/EXECUTION.json',watch/'COMPLETE.json']
 assert set(a['absent_paths'])==set(map(str,expected_absent))
 latest=a['latest_completed_cycle'];assert latest==native[str(Path(a['latest_cycle_directory'])/'RESULT.json')] and type(latest['cycle']) is int and latest['cycle']>=startup['latest_completed_cycle']['cycle'] and latest['kind']=='synchronized'
 assert latest['index_bytes']==len(idxraw) and latest['index_sha256']==a['index']['sha256'] and a['copy_count_at_latest_cycle']==latest['native_result']['verified_runs']
 if completed:c.validate_completed_index(completion,idxraw,idxraw,selected,index,latest,Path(a['latest_cycle_directory']))
 def deny(*args,**kwargs):raise AssertionError('saved_metadata_only')
 with patch.object(subprocess,'run',deny),patch.object(subprocess,'Popen',deny),patch.object(socket.socket,'connect',deny),patch.object(os,'kill',deny):
  ref=plan['sources']['snapshot'];raw(ref);spec=importlib.util.spec_from_file_location('review_native_candidate_snapshot',ref['path']);snap=importlib.util.module_from_spec(spec);sys.modules[spec.name]=snap;spec.loader.exec_module(snap)
  cfg=snap.SnapshotConfig(**config);receipt_rows=[];attempts=[];prior=[];file_count=0;total_bytes=0
  for number in range(latest['cycle']+1):
   cref=pin(watch/f'{number:08d}'/'RESULT.json');cycle=ck(cref);cintent=ck(pin(watch/f'{number:08d}'/'INTENT.json'))
   assert cycle['cycle']==number and cycle['kind']=='synchronized'
   attempt=Path(cycle['native_result']['attempt']);assert attempt.parent==Path(plan['destination'])/'self_probe_attempts'
   req=ck(pin(attempt/'REQUEST.json'));assert req==dict(host=config['host'],base=config['remote_base'],roots=config['roots'],accepted=prior,limit=8,selected_runs=selected,evidence_profile="early_self_probes")
   aref=pin(attempt/'AUTHORIZATION.json');authorization=ck(aref);assert authorization==auth
   mr=pin(attempt/'MANIFEST.json');manifest=ck(mr);assert json.loads(raw(pin(attempt/'remote.stdout')))==manifest and raw(pin(attempt/'remote.stderr'))==b''
   if any(row['files'] for row in manifest['runs']):assert raw(pin(attempt/'rsync.stderr'))==b''
   else:assert not(attempt/'rsync.stderr').exists() and not(attempt/'rsync.stdout').exists()
   snap.validate_manifest(manifest,cfg,set(prior))
   vr=pin(attempt/'VERIFIED.json');verified=ck(vr);assert verified['schema']=='caus.self_probe_verified.v1' and len(verified['runs'])==len(manifest['runs'])
   listing=[]
   for row,receipt in zip(manifest['runs'],verified['runs']):
    assert receipt==dict(run=row['run'],path=str(attempt/'data'/row['run']),manifest=str(attempt/'MANIFEST.json'),manifest_sha256=mr['sha256'],files=len(row['files']),bytes=row['bytes'],summary_sha256=row['summary_sha256'],verified_at=receipt['verified_at'],schema='caus.self_probe_receipt.v1',custody_kind='actor_self_probe_evidence')
    assert datetime.datetime.fromisoformat(receipt['verified_at'])<=datetime.datetime.fromisoformat(cycle['at'])
    listing.extend(str(Path(row['run'])/item['path'])for item in row['files']);file_count+=len(row['files']);total_bytes+=row['bytes']
   assert raw(pin(attempt/'FILES.nul'))==b''.join(x.encode()+b'\0'for x in listing)
   assert cycle['native_result']['verified_runs']==len(verified['runs']) and cycle['native_result']['verified_bytes']==sum(r['bytes']for r in verified['runs'])
   receipt_rows.extend(verified['runs']);prior.extend(r['run']for r in verified['runs'])
   prefix=b''.join(line+b'\n'for line in idxraw.splitlines()[:len(receipt_rows)])
   assert receipt_rows==index[:len(receipt_rows)] and cycle['index_bytes']==len(prefix) and cycle['index_sha256']==hashlib.sha256(prefix).hexdigest()
   attempts.append(dict(cycle=cref,manifest=mr,verified=vr,request=pin(attempt/'REQUEST.json'),authorization=aref,run_count=len(verified['runs'])))
 assert receipt_rows==index
 peer=dict(schema='causes.individual_variant_self_probe_feed_index_cut_independent_review.v1',at=datetime.datetime.now(datetime.timezone.utc).isoformat(),accepted_individual_variant_self_probe_feed_index_cut=True,new_startup_claimed=False,actual=ap,source=a['source'],plan=a['plan'],source_review=a['source_review'],packet=a['packet'],root_launch=a['root_launch'],durable_adoption=a['durable_adoption'],configuration=plan['configuration'],registration=plan['registration'],capture_source_derivation=derivref,index=a['index']['saved'],observed_at=a['at'],accepted=a['accepted'],remaining=a['remaining'],files=file_count,bytes=total_bytes,attempts=attempts,findings=[],lifecycle=a['lifecycle'],completed_feed=completion,completed_helper_source=a['completed_helper_source'],opaque_self_probe_custody_only=True,all_selected_self_probe_copies_complete=completed,logical_role=reg['logical_role'],execution_host=reg['execution_host'],reviewer=reviewer,review_source=pin(__file__),auditor_source_review=auditor_review_ref,source_preparation_review=request['source_preparation_review'],request=request_ref,capture_execution=execution_ref,capture_intent=execution['intent'],original_startup=request['startup'],original_startup_review=request['startup_review'],previous_actual=request['previous_actual'],previous_review=request['previous_review'],checks=dict(accepted_plan_source_root_adoption_and_durable_chain=True,original_live_identity_and_owned_flock_both_sides=not completed,original_instances_retired_and_flock_free_both_sides=completed,successful_durable_closure_and_exact_selected_index=completed,native_start_exact_configuration_and_authorization=True,all_native_cycle_index_prefixes=True,all_native_receipts_manifest_and_verified_joins=True,native_self_probe_manifest_validator_all_attempts=True,exact_files_list_no_payload_rehash=True,selected_finite_membership_only=True,original_startup_and_previous_literal_prefixes=True),limitations=['Dated saved copier custody only; no new startup or producer liveness claim.','Native manifests and VERIFIED metadata preserve opaque self-probe custody, not scientific success. No copied request, response, context, probe or label payload was opened or rehashed.','Completed lifecycle requires original successful durable receipt, complete selected index, retired identities and free lock on both saved sides. Live lifecycle preserves current original copier identity and owned lock.','Empty native copy cycles retain zero VERIFIED rows, empty FILES.nul and absent rsync logs.'],effects=dict(host_calls=0,model_calls=0,process_or_lock_queries=0,capture_invocations=0,payload_reads=0),checked_refs=list(refs.values()))
 if write:
  with (O/'INDEPENDENT_REVIEW.v1.json').open('x')as f:json.dump(peer,f,indent=2,sort_keys=True);f.write('\n')
  return pin(O/'INDEPENDENT_REVIEW.v1.json')
 return peer


def main():
 ap=argparse.ArgumentParser()
 for name in ('request','actual','execution'):ap.add_argument('--'+name,type=json.loads,required=True)
 ap.add_argument('--reviewer',required=True)
 ap.add_argument('--auditor-review',type=json.loads)
 args=ap.parse_args()
 print(json.dumps(audit(args.request,args.actual,args.execution,args.reviewer,write=True,auditor_review_ref=args.auditor_review)))

if __name__=='__main__':main()
