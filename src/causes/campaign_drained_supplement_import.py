"""Bind one accepted drained supplementary cut through pinned native saved-data auditors."""
from pathlib import Path
import argparse,collections,datetime,hashlib,importlib.util,json,sys,ijson

def pin(p):
 p=Path(p);h=hashlib.sha256();n=0
 with p.open('rb')as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b);n+=len(b)
 return dict(path=str(p),bytes=n,sha256=h.hexdigest())
def checked(r):
 assert pin(r['path'])==r,r['path'];return json.loads(Path(r['path']).read_bytes())
def module(r,name):
 assert pin(r['path'])==r;s=importlib.util.spec_from_file_location(name,r['path']);m=importlib.util.module_from_spec(s);sys.modules[name]=m;s.loader.exec_module(m);assert pin(m.__file__)==r;return m
def save(p,v):
 with p.open('x')as f:json.dump(v,f,sort_keys=True,ensure_ascii=False,allow_nan=False);f.write('\n')
 return pin(p)
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--request',required=True);ap.add_argument('--authority',required=True);args=ap.parse_args();rr=pin(args.request);r=checked(rr);ar=pin(args.authority);authority=checked(ar)
 assert r['source']==pin(__file__)and rr in authority['requests'] and authority['source']==r['source']and authority['accepted_saved_drained_supplement_import_execution']is True
 peer=checked(authority['preparation_review']);assert peer['accepted_saved_drained_supplement_import_preparation']is True and rr in peer['requests'] and peer['source']==r['source']and not peer.get('findings')
 e=r['expected'];assert set(e)=={'assigned','terminal','unstarted','existing','append','technical_failures'} and all(type(v)is int and v>=0 for v in e.values())
 assert e['assigned']==e['terminal']+e['unstarted'] and e['terminal']==e['existing']+e['append'] and e['technical_failures']<=e['terminal']
 output=Path(r['output']);assert output.is_absolute()and not output.exists()
 # Initialize only the exact saved request/parser graph. Never invoke a queue, worker, controller or importer CLI.
 s=r['sources'];reader=module(s['read_guard'],'drained_supplement_read_guard');mat=module(s['materializer'],'saved_corpus_self_input_binding');parent=module(s['parent'],'drained_supplement_native_parent');m=parent.native_modules();m['runner']=module(s['runner'],'drained_supplement_original_runner');m['core']=module(s['queue_core'],'causes.campaign_supplement');sys.modules['causes'].campaign_supplement=m['core'];collector=module(s['collector'],'causes.campaign_supplement_collect');adapter=module(s['adapter'],'drained_supplement_endpoint_adapter');api=module(s['binder'],'drained_supplement_measurement_binder');reuse=module(s['reuse'],'drained_supplement_measurement_keys');partition=module(s['partition'],'drained_supplement_partition')
 assert collector.core is m['core']and adapter.SOURCE==s['materializer'];assert mat.KNOWN['parent']==s['parent']and mat.KNOWN['runner']==s['runner']
 allow=checked(r['allowlist']);guard=reader.ReadGuard(allow['files']+[rr,ar,authority['preparation_review']],output,r['source_files'],[],json_cache_bytes=64*1024*1024,json_cache_entries=64);sys.addaudithook(guard.audit)
 output.mkdir();save(output/'INTENT.v1.json',dict(request=rr,authority=ar,source=r['source'],executor=r['executor'],model_calls=0,host_calls=0))
 get=guard.checked_json;d=get(r['drained']);dr=get(r['drained_review']);p=get(d['plan']);jobs=get(d['original_jobs']);tc=get(d['terminal_custody']);unstarted=get(d['unstarted']);membership=get(r['membership']);a=get(r['assignments']);base=get(r['base_result']);bp=get(r['base_review'])
 assert dr[r['drained_acceptance_flag']]is True and not dr['findings']and dr['actual']==r['drained']
 for k in ('plan','original_jobs','journal','terminal_custody','unstarted','runtime_result','runtime_source','queue_source'):assert dr[k]==d[k]
 assert d['all_claimed_terminal']is True and d['all_assigned_terminal']is False and d['counts']==dict(active=0,assigned=e['assigned'],claimed=e['terminal'],consecutive_failures=0,technical_failures=e['technical_failures'],terminal=e['terminal'],unstarted=e['unstarted'])
 assert p['jobs']==d['original_jobs']and d['queue_source']==s['queue_core']and p['family']==r['family']and p['model']==r['model']
 gp=get(p['global_prepared']);gpr=get(p['global_prepared_review']);assert gpr['accepted_saved_corpus_measurement_inputs']is True and gpr['preparation']==p['global_prepared']and gp['self_jobs']==p['global_jobs']
 for k in ('source','cohort','ready','self_jobs','unavailable'):assert gpr[k]==gp[k]
 globaljobs=get(p['global_jobs']);globalby={j['measurement_id']:j for j in globaljobs};assert len(globalby)==len(globaljobs)and all(globalby[j['measurement_id']]==j for j in jobs)
 by={j['measurement_id']:j for j in jobs};assert len(by)==len(jobs)==e['assigned'];history=m['core'].read_history(Path(d['journal']['path']));part=partition.drained_partition(jobs,history,tc,unstarted['rows'],journal_index=collector.journal_index,digest=m['core'].digest)
 claims,terminals=part['claims'],part['finals'];assert part['counts']=={k:e[k]for k in ('assigned','terminal','unstarted','technical_failures')}
 assert membership['actual']==r['drained']and membership['review']==r['drained_review']and set(membership['terminal_run_ids'])=={by[x]['run_id']for x in terminals}
 assert bp[r['base_acceptance_flag']]is True and bp['actual']==r['base_result']and bp['outputs']==base['outputs']and bp['source']==base['source']and bp['assignments']==base['assignments']and bp['manifest']==base['manifest'];assert base['outputs']['MEASUREMENT_CUSTODY']==r['base_custody']
 candidate_keys={(by[mid]['run_id'],'self_'+by[mid]['question_id'])for mid in terminals};oldkeys=set();existing={};guard.record(r['base_custody'])
 with open(r['base_custody']['path'],'rb')as f:
  for row in ijson.items(f,'item'):
   key=(row['run_id'],row['readout']);assert key not in oldkeys;oldkeys.add(key)
   if key in candidate_keys:existing[key]=row
 assert len(oldkeys)==r['base_count']
 previous=get(r['initial_segments']);previous_review=get(r['initial_segments_review'])
 assert previous_review[r['initial_segments_acceptance_flag']]is True and previous_review['actual']==r['initial_segments']and not previous_review['findings']
 assert previous_review['manifest']==previous['manifest']and previous_review['assignments']==previous['assignments']==r['assignments']
 manifest=get(previous['manifest']);assert manifest['assignments']==r['assignments']and manifest['segments'][0]['payload']==r['base_custody']
 for segment in manifest['segments'][1:]:
  added=get(segment['payload']);added_keys=reuse.measurement_keys(added,epochs=a['epochs'],readout_names=a['readout_names'])
  assert len(added)==segment['physical_measurements']and not oldkeys&added_keys;oldkeys.update(added_keys)
  for row in added:
   key=(row['run_id'],row['readout'])
   if key in candidate_keys:existing[key]=row
 assert len(oldkeys)==previous['terminal_slots']==manifest['physical_measurements']==r['accepted_count']and len(existing)==e['existing']
 contexts={};allslots=[];newslots=[];reused=[];failures=[]
 for job in jobs:
  mid=job['measurement_id']
  if mid not in terminals:continue
  key=(job['run_id'],job['stage']);binding=dict(original_bindings=job['original_refs'])
  assert key not in contexts or contexts[key]==binding;contexts[key]=binding
  row=dict(run_id=job['run_id'],stage='early',source_kind='historical_extra_self',instruments=[job['question_id']],measurement_id=mid,job_sha256=m['core'].digest(job),overlay_manifest=p['global_jobs'],prepared_self_jobs=p['global_jobs'],terminal=terminals[mid],root=p['output_root'],endpoint=p['endpoint'],custody=dict(actual=r['drained'],review=r['drained_review'],accepted_flag=r['drained_acceptance_flag'],closed_run_id=job['run_id'],terminal=True,membership=r['membership']))
  slot=api.bind_supplement(row,job=job,contexts=contexts,config=get(job['frozen_config']),runner=m['runner'],modules=m,adapter=adapter,collector=collector,record=guard)[0];slot['epoch']=a['epochs'][slot['run_id']];allslots.append(slot);physical=(slot['run_id'],slot['readout'])
  if physical in existing:
   old=existing[physical];assert all(old[k]==slot[k]for k in ('status','label','reason','epoch'));assert old['evidence']['final']==slot['evidence']['final']and old['evidence']['measurement_id']==mid;reused.append(dict(run_id=slot['run_id'],readout=slot['readout'],measurement_id=mid,status=slot['status'],original_final=old['evidence']['final']))
  else:newslots.append(slot)
  if slot['status']=='missing':failures.append(dict(measurement_id=mid,run_id=slot['run_id'],readout=slot['readout'],reason=slot['reason'],already_occupied=physical in existing))
  if len(allslots)%500==0:print(json.dumps(dict(verified=len(allslots),target=e['terminal'])),flush=True)
 assert len(allslots)==e['terminal'] and len(newslots)==e['append'] and len(reused)==e['existing'] and len(failures)==e['technical_failures']
 keys=reuse.measurement_keys(allslots,epochs=a['epochs'],readout_names=a['readout_names'],require_epoch=True);newkeys=reuse.measurement_keys(newslots,epochs=a['epochs'],readout_names=a['readout_names'],require_epoch=True);assert keys==candidate_keys and not newkeys&oldkeys
 for j in jobs:
  if j['measurement_id']not in terminals:assert (j['run_id'],'self_'+j['question_id'])not in oldkeys|newkeys
 outputs=dict(slots=save(output/'NEW_SLOTS.v1.json',newslots),verified_slots=save(output/'ALL_VERIFIED_SLOTS.v1.json',allslots),reused=save(output/'REUSED_KEYS.v1.json',reused))
 guard.finish();result=dict(schema='causes.saved_drained_supplement_import.v1',role=r['role'],source=r['source'],request=rr,drained=r['drained'],drained_review=r['drained_review'],original_jobs=d['original_jobs'],journal=d['journal'],terminal_custody=d['terminal_custody'],unstarted=d['unstarted'],assignments=r['assignments'],base_result=r['base_result'],base_review=r['base_review'],base_custody=r['base_custody'],initial_segments=r['initial_segments'],initial_segments_review=r['initial_segments_review'],outputs=outputs,counts=dict(assigned=e['assigned'],terminal_verified=e['terminal'],already_occupied=e['existing'],appended=e['append'],pending=e['unstarted'],technical_failures=e['technical_failures'],appended_observed=sum(x['status']=='observed'for x in newslots),appended_missing=sum(x['status']=='missing'for x in newslots)),failures=failures,public_terminal_verifier=s['collector'],endpoint_adapter=s['adapter'],measurement_binder=s['binder'],native_runner=s['runner'],all_claimed_terminal=True,all_assigned_terminal=False,all_original_failure_slots_occupied=True,raw_captures_copied=False,old_base_or_report_modified=False,analysis_performed=False,final_holm78_available=False,host_calls=0,model_calls=0,executor=r['executor'],at=datetime.datetime.now(datetime.timezone.utc).isoformat())
 print(json.dumps(save(output/'RESULT.v1.json',result)),flush=True)
if __name__=='__main__':main()
