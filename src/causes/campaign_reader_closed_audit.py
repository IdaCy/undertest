"""Audit a saved finite reader closure using unchanged native admission/history checks."""
from pathlib import Path
import argparse,collections,datetime,hashlib,importlib.util,json,os,sys
def pin(p):
 p=Path(p)
 with p.open('rb')as f:h=hashlib.file_digest(f,'sha256').hexdigest()
 return dict(path=str(p),bytes=p.stat().st_size,sha256=h)
def verify(r):assert pin(r['path'])==r,r;return Path(r['path'])
def get(p):return json.loads(Path(p).read_bytes())
def ck(r):return get(verify(r))
def imp(name,r):
 verify(r);s=importlib.util.spec_from_file_location(name,r['path']);m=importlib.util.module_from_spec(s);sys.modules[name]=m;s.loader.exec_module(m);return m
def runtime_for_plan(runtime,plan,plan_ref=None):
 if plan.get('schema')=='causes.derived_reader_plan.v1':
  assert plan_ref is not None and ck(plan_ref)==plan, 'derived_runtime_plan_pin'
  return runtime.bind_for_plan(plan_ref)
 return runtime.bind_for_plan(plan) if 'cohort_extension' in plan else runtime

def validate_loaded_preparation(kind,loaded,prepared):
 expected=prepared
 if kind=='derived':
  protocol=prepared['protocol']
  expected=dict(prepared,checker_config=protocol['checker_config'],prompts=protocol['prompts'],cohort=prepared['assignments'])
 assert loaded==expected, 'loaded_preparation_changed'

def native_reader_source(kind,prepared):
 return prepared['protocol']['reader_source'] if kind=='derived' else prepared['reader_source']
def validate_descriptor(settings):
 fields={'schema','root','actual','plan','prior_progress','direct_binding','capture_execution','capture_source','reader_source','prepared','startup_actual_path','output'}
 assert type(settings) is dict, 'descriptor_fields'
 version=settings.get('schema')
 if version=='causes.saved_reader_closed_audit_descriptor.v2':
  fields|={'cohort_kind','process_journal_progress'}
 assert set(settings)==fields, 'descriptor_fields'
 assert version in ('causes.saved_reader_closed_audit_descriptor.v1','causes.saved_reader_closed_audit_descriptor.v2'), 'descriptor_schema'
 assert settings.get('cohort_kind','fresh_candidate') in ('fresh_candidate','saved_corpus','derived'), 'cohort_kind'
 for key in ('root','startup_actual_path','output'):
  p=Path(settings[key]);assert p.is_absolute() and p.resolve()==p and not p.is_symlink(), 'noncanonical_descriptor_path'
 for key in ('actual','plan','capture_execution','capture_source','reader_source','prepared'):
  assert type(settings[key]) is dict and set(settings[key])=={'path','bytes','sha256'}, 'exact_pin_required'
  verify(settings[key])
 custody_keys=('prior_progress','direct_binding','process_journal_progress')
 assert sum(settings.get(key) is not None for key in custody_keys)==1, 'one_prior_custody_mode'
 for key in custody_keys:
  if settings.get(key) is not None:
   assert type(settings[key]) is dict and set(settings[key])=={'path','bytes','sha256'}, 'exact_pin_required'
   verify(settings[key])
 protected={settings[k]['path']for k in ('actual','plan','capture_execution','capture_source','reader_source','prepared')}
 protected.update(settings[k]['path']for k in custody_keys if settings.get(k) is not None)
 assert settings['output'] not in protected, 'output_overwrites_input'

def validate_cohort_counts(kind,runtime,plan,prep,assigned):
 counts=prep['counts']
 if kind=='fresh_candidate':
  assert counts['selected']==runtime.checked(runtime.KNOWN['fresh_known'])['global_count'] and len(assigned)==counts['copied']
  assert counts['reader_ready']+counts['unavailable']==counts['copied'] and counts['copied']+counts['pending']==counts['selected']
 elif kind=='derived':
  assert plan['schema']=='causes.derived_reader_plan.v1' and prep['schema']=='causes.frozen_derived_reader_preparation.v1', 'derived_cohort_schema'
  assert plan['cohort']==plan['additive_assigned']==prep['assignments'] and plan['additive_ready']==prep['ready'], 'derived_cohort_join'
  assert set(counts)=={'assigned','ready','unavailable','requests'} and all(type(value) is int and value>=0 for value in counts.values()), 'derived_integer_counts'
  ready=ck(prep['ready']);missing=ck(prep['unavailable']);frozen=ck(prep['assignments'])
  assert counts==dict(assigned=len(assigned),ready=len(ready),unavailable=len(missing),requests=3*len(ready)), 'derived_cohort_counts'
  assert len(assigned)==len(frozen)==len(ready)+len(missing) and type(prep['missing_slots']) is int and prep['missing_slots']==3*len(missing), 'derived_missing_accounting'
  identity=lambda row:tuple(row[key] for key in ('run_id','view_kind','stage'))
  assert [identity(row) for row in assigned]==[identity(row) for row in frozen] and len({identity(row) for row in assigned})==len(assigned), 'derived_assignment_order'
  by={row['measurement_key']:row for row in ready+missing}
  assert len(by)==len(ready)+len(missing)==len(assigned) and all(type(key) is str and key.startswith('derived_reader_') for key in by), 'derived_measurement_identity'
  assert all(by.get(row['measurement_key'])==row for row in assigned), 'derived_assigned_rows'
  assert all(row['disposition']=='prepared' for row in ready) and all(row['disposition']=='unavailable' for row in missing), 'derived_disposition'
 else:
  assert kind=='saved_corpus' and plan['cohort']==prep['cohort'], 'saved_cohort_join'
  cohort=ck(prep['cohort']);peer=ck(prep['cohort_review'])
  assert peer.get('accepted_original_candidate_measurement_cohort') is True and peer['cohort']==prep['cohort'] and peer['findings']==[], 'saved_cohort_acceptance'
  assert all(type(counts[key]) is int and counts[key]>=0 for key in ('selected','context_ready','reader_ready','reader_requests','unavailable')), 'saved_cohort_integer_counts'
  run_ids=cohort['selected_run_ids']
  assert type(run_ids) is list and len(run_ids)==len(set(run_ids))==cohort['assigned_contexts']==counts['selected'], 'saved_cohort_membership'
  assert len(assigned)==counts['selected']==counts['reader_ready']+counts['unavailable'], 'saved_cohort_counts'
  assert {row['run_id'] for row in assigned}==set(run_ids), 'saved_cohort_assigned_runs'
  assert counts['context_ready']==counts['reader_ready'] and counts['reader_requests']==3*counts['reader_ready'], 'saved_cohort_reader_counts'

def validate_process_journal_progress(reference,settings,actual,process,supervisor,closed_journal,ready_count):
 """Check dated process/journal snapshots without asserting an earlier lock inode."""
 custody=ck(reference)
 assert set(custody)=={'schema','startup','startup_review','startup_review_flag','observations'}, 'process_journal_custody_fields'
 assert custody['schema']=='causes.reader_process_journal_progress_custody.v1', 'process_journal_custody_schema'
 flag=custody['startup_review_flag'];assert type(flag) is str and flag.startswith('accepted_'), 'startup_review_flag'
 peer=ck(custody['startup_review'])
 assert peer.get(flag) is True and peer['actual']==custody['startup'] and peer['findings']==[], 'startup_acceptance'
 assert custody['startup']['path']==settings['startup_actual_path'], 'startup_path'
 assert type(custody['observations']) is list, 'progress_observations'
 references=[custody['startup']]+custody['observations']
 assert len({ref['path'] for ref in references})==len(references), 'duplicate_progress_observation'
 previous=b'';previous_at=None;checked=[]
 for index,ref in enumerate(references):
  assert type(ref) is dict and set(ref)=={'path','bytes','sha256'}, 'exact_progress_pin'
  assert settings['output'] not in (ref['path'],custody['startup_review']['path']), 'output_overwrites_progress'
  observation=ck(ref)
  if index:assert observation['previous']==references[index-1], 'progress_previous_join'
  for key in ('plan','packet'):assert observation[key]==actual[key], 'progress_'+key
  assert observation['original_process']==actual['actor_process'] and observation['supervisor_intent']==actual['supervisor_intent'], 'progress_original_process'
  assert observation['journal_original_path']==actual['journal_original']['path'], 'progress_journal_path'
  for side in ('process_before','process_after'):
   for key,identity in (('reader',process['identity']),('supervisor',supervisor['supervisor'])):
    seen=observation[side][key]
    assert all(seen[field]==identity[field] for field in ('pid','start_ticks','argv')) and seen.get('state') not in ('Z','X'), 'progress_process_identity'
  snapshot=verify(observation['journal_snapshot']).read_bytes()
  assert snapshot.endswith(b'\n') and snapshot.startswith(previous) and closed_journal.startswith(snapshot), 'progress_journal_prefix'
  assert settings['output']!=observation['journal_snapshot']['path'], 'output_overwrites_progress'
  rows=[json.loads(line) for line in snapshot.splitlines()]
  claims=[row['key'] for row in rows if row['event']=='claimed']
  terminals=[row['key'] for row in rows if row['event'] in ('finished','unresolved_current_reader_attempt')]
  assert len(claims)==len(set(claims)) and len(terminals)==len(set(terminals)) and set(terminals)<=set(claims), 'progress_claim_accounting'
  expected=dict(claimed_contexts=len(claims),terminal_contexts=len(terminals),active_contexts=len(claims)-len(terminals),unclaimed_contexts=ready_count-len(claims))
  assert expected['unclaimed_contexts']>=0 and all(type(observation['counts'][key]) is int and observation['counts'][key]==value for key,value in expected.items()), 'progress_counts'
  event_counts=observation['counts']['event_counts']
  assert all(type(value) is int for value in event_counts.values()) and event_counts==dict(collections.Counter(row['event'] for row in rows)), 'progress_event_counts'
  observed_at=datetime.datetime.fromisoformat(observation['observed_to']);observed_from=datetime.datetime.fromisoformat(observation['observed_from'])
  assert observed_at.tzinfo is not None and observed_from.tzinfo is not None and observed_from<=observed_at and (previous_at is None or observed_from>previous_at) and observed_at<=datetime.datetime.fromisoformat(actual['observed_at']), 'progress_time_order'
  assert observation.get('at',observation['observed_to'])==observation['observed_to'], 'progress_timestamp_join'
  assert observation['complete_exists'] is False and observation.get('process_identity_matched',True) is True and observation['findings']==[], 'progress_live_scope'
  checked.append(dict(actual=ref,journal=observation['journal_snapshot'],**expected));previous=snapshot;previous_at=observed_at
 return dict(custody=reference,startup_review=custody['startup_review'],observations=checked,journal_prefixes_verified=True,historical_watch_lock_identity_claimed=False)

def previous_history(plan):
 history=[];seen=set();expected=plan['prior_arrival6_final_cut']
 for entry in plan['intervening_reader_cuts']:
  actual=ck(entry['actual']);peer=ck(entry['review']);old_plan=ck(actual['plan'])
  assert peer['accepted_finite_reader_route_closed_cut'] is True and peer['actual']==entry['actual'] and peer['findings']==[]
  assert all(peer[k]==actual[k]for k in ('plan','reader_source','root','journal','runtime_result'))
  assert actual['route_predecessor']==expected and actual['all_claims_accounted'] is True
  keys=actual['claimed_keys'];assert len(keys)==len(set(keys))==actual['claimed']==actual['finished_or_unresolved']
  assert actual['active']==0 and actual['quarantine'] is None and not seen.intersection(keys)
  # Natural finite predecessor selections retain their full census; older qualified
  # intentional historical closures are checked by unchanged native load below.
  if old_plan.get('schema') in ('causes.fresh_candidate_reader_plan.v1','causes.derived_reader_plan.v1'):
   assert actual['ready']==actual['claimed']==old_plan['expected_unclaimed_contexts'] and actual['unclaimed']==0
  seen.update(keys);history.append(actual);expected=entry['actual']
 assert history and plan['route_predecessor']==expected
 return history,seen

def review_saved_closed(descriptor_ref):
 settings=ck(descriptor_ref);validate_descriptor(settings)
 cohort_kind=settings.get('cohort_kind','fresh_candidate');simple_ref=settings.get('process_journal_progress')
 O=Path(settings['actual']['path']).parent;PRIOR=None if settings['prior_progress'] is None else Path(settings['prior_progress']['path']);CAPTURE_EXECUTION=Path(settings['capture_execution']['path'])
 ap=settings['actual'];a=ck(ap);verify(settings['capture_execution']);output=Path(settings['output'])
 assert not output.exists() and output.parent.is_dir()
 for k in ['request','intent','source','plan','plan_review','reader_source','prepared','route_predecessor','journal','journal_original','runtime_result','route_state_carry','identity','actor_process','supervisor_intent','packet','durable_adoption','execution','complete','submit_intent','submit_result']:verify(a[k])
 rq=ck(a['request']);sp=ck(rq['source_review']);root=ck(rq['root_capture_adoption']);assert sp['accepted_finite_reader_priority_custody_preparation']is True and sp['source']==a['source'] and sp['findings']==[]
 assert rq['capture_authorized']is True and rq['finality_authorized']is False and rq['output']==str(O) and rq['owned_pause']is rq['owned_pause_review']is None
 assert root['root_authorized_capture']is True and root['maximum_invocations']==1 and root['model_calls_authorized']is root['label_reads_authorized']is False
 for k in ['source','plan','plan_review','output','submit_intent','submit_result']:assert root[k]==rq[k]
 for k in ['source','plan','plan_review','submit_intent','submit_result']:assert rq[k]==a[k]
 assert ck(a['intent'])==dict(request=a['request'],source=a['source'],root_capture_adoption=rq['root_capture_adoption'],automatic_retry=False)
 plan=ck(a['plan']);pp=ck(a['plan_review']);prep=ck(a['prepared'])
 plan_flag={'fresh_candidate':'accepted_fresh_candidate_reader_plan','saved_corpus':'accepted_saved_corpus_reader_plan','derived':'accepted_derived_reader_plan'}[cohort_kind]
 assert pp[plan_flag]is True and pp['plan']==a['plan'] and pp['source']==a['reader_source']==plan['source'] and a['plan']==settings['plan']
 if cohort_kind=='saved_corpus':assert plan['schema']=='causes.saved_corpus_reader_plan.v1', 'saved_plan_schema'
 if cohort_kind=='derived':
  assert plan['schema']=='causes.derived_reader_plan.v1' and pp['binding']==plan['derived_binding'] and pp['inputs']==plan['prepared'], 'derived_plan_join'
 history,all_previous=previous_history(plan)
 assert a['route_predecessor']==plan['route_predecessor']==plan['intervening_reader_cuts'][-1]['actual']
 prior=priorpeerref=direct_binding_ref=None
 if PRIOR is not None:
  prior=ck(pin(PRIOR));priorpeerref=pin(PRIOR.parent/'INDEPENDENT_REVIEW.v1.json');priorpeer=ck(priorpeerref)
  assert priorpeer['accepted_fresh_candidate_reader_progress']is True and priorpeer['actual']==pin(PRIOR) and priorpeer['findings']==[]
  for field in ('plan','reader_source','packet'):assert a[field]==prior[field]
 elif simple_ref is None:
  direct_binding_ref=settings['direct_binding'];assert direct_binding_ref is not None;direct=ck(direct_binding_ref)
  assert direct['startup_invoked']is direct['notifier_invoked']is False and direct['maximum_invocations']==1 and direct['whole_inverse_equal']is True
  assert direct['complete']==a['complete'] and not Path(settings['startup_actual_path']).exists()
  verify(direct['source']);verify(direct['result']);verify(direct['source_review'])
  original_complete=ck(a['complete']);original_capture=ck(pin(CAPTURE_EXECUTION))
  assert original_capture['original_complete']==a['complete'] and original_capture['maximum_invocations']==1 and original_capture['automatic_retry']is False
  assert original_complete['all_actors_succeeded']is True and original_complete['launch_error']is None
  assert datetime.datetime.fromisoformat(original_complete['at'])<=datetime.datetime.fromisoformat(direct['at'])<=datetime.datetime.fromisoformat(original_capture['started_at'])
 assert a['source']==settings['capture_source']
 assert a['reader_source']==settings['reader_source'] and a['prepared']==settings['prepared']
 carry=ck(a['route_state_carry']);identity=ck(a['identity']);assert identity['plan']==plan and identity['source']==a['reader_source'] and identity['workers']==4 and identity['maximum_threshold_calls']==12
 packet=ck(a['packet']);adoption=ck(a['durable_adoption']);submit=ck(a['submit_intent']);submitted=ck(a['submit_result']);sup=ck(a['supervisor_intent']);proc=ck(a['actor_process']);ai=ck(proc['intent']);ex=ck(a['execution']);complete=ck(a['complete'])
 assert len(packet['actors'])==1 and packet['actors'][0]['name']=='reader';actor=packet['actors'][0]
 assert ai['actor']==actor and ai['packet']==sup['packet']==submit['packet']==a['packet'] and ai['adoption']==sup['adoption']==submit['adoption']==a['durable_adoption']
 assert proc['identity']['argv']==actor['argv'] and actor['source']==a['reader_source']
 assert pin(actor['argv'][actor['argv'].index('--plan')+1])==a['plan'] and pin(actor['argv'][actor['argv'].index('--plan-review')+1])==a['plan_review']
 assert adoption['packet']==a['packet'] and adoption['root_authorized']is True and adoption['attempt_id']==packet['attempt_id'] and adoption['automatic_retry']is False and adoption['maximum_invocations_per_actor']==1
 assert submitted['returncode']==0 and submitted['error']is None and submitted['automatic_retry']is False
 assert ex['intent']==proc['intent'] and ex['returncode']==0 and ex['source_stable']is True and ex['source_error']is None and ex['automatic_retry']is False and ex['signals_sent']is False
 assert complete['executions']==[a['execution']] and complete['all_actors_succeeded']is True and complete['launch_error']is None and complete['automatic_retry']is False and verify(ex['stderr']).read_bytes()==b''
 assert a['before']==a['after']
 for side in ['before','after']:
  x=a[side]
  for k,who in [('actor',proc['identity']),('supervisor',sup['supervisor'])]:assert x[k]==dict(absent=True,pid=who['pid'])
  assert x['lock_free']is True and x['lock']['path']==str(Path(a['root'])/'.watch.lock') and x['lock']['bytes']==0
  if prior is not None:
   assert all(x['lock'][k]==prior['before']['lock'][k] for k in ('path','device','inode'))
   for kind,who in [('actor',proc['identity']),('supervisor',sup['supervisor'])]:assert all(who[k]==prior['before'][kind]['actual'][k]for k in ('pid','start_ticks','argv'))
 raw=verify(a['journal']).read_bytes();assert raw==verify(a['journal_original']).read_bytes() and raw.endswith(b'\n');rows=[json.loads(x)for x in raw.splitlines()];result=ck(a['runtime_result']);assert result['journal']==a['journal_original']
 if prior is not None:assert raw.startswith(verify(prior['journal']['saved']).read_bytes())
 runtime=imp('historical_continuation_saved_reader_review',a['reader_source'])
 runtime=runtime_for_plan(runtime,plan,a['plan'])
 mods=runtime.native_modules();capture=imp('historical_continuation_saved_closed_capture',a['source'])
 loaded=runtime.load(a['plan'],a['plan_review'],mods);loaded_plan,loaded_prep,assigned,ready,config,baseline,states=loaded
 assert loaded_plan==plan
 validate_loaded_preparation(cohort_kind,loaded_prep,prep)
 validate_cohort_counts(cohort_kind,runtime,plan,prep,assigned)
 all_ready=ck(plan['additive_ready']);assert len(all_ready)==prep['counts']['ready' if cohort_kind=='derived' else 'reader_ready'] and len(ready)==plan['expected_unclaimed_contexts']>0
 readykeys={r['measurement_key']for r in ready};assert not readykeys.intersection(all_previous) and len(readykeys)==len(ready)
 assert set(plan['previous_cohort_claimed_keys'])==all_previous.intersection(r['measurement_key']for r in all_ready) and len(plan['previous_cohort_claimed_keys'])==len(set(plan['previous_cohort_claimed_keys']))
 simple_progress=None
 if simple_ref is not None:simple_progress=validate_process_journal_progress(simple_ref,settings,a,proc,sup,raw,len(ready))
 v=capture.validate_history(rows,result,plan,assigned,ready,carry,states,mods)
 assert rows[0]['carry']==a['route_state_carry'] and a['claimed_keys']==v['claims'] and a['unclaimed_keys']==v['unclaimed']==[]
 assert a['assigned']==len(assigned) and a['ready']==a['claimed']==a['finished_or_unresolved']==len(ready) and a['active']==a['unclaimed']==0 and a['all_claims_accounted']is True
 assert a['initial_consecutive_failed_contexts']==v['inherited']==history[-1]['final_consecutive_failed_contexts'] and a['final_consecutive_failed_contexts']==v['final_counter'] and 0<=v['final_counter']<3 and a['quarantine']==v['quarantine']==None
 assert a['native_failure_events']==v['failures'] and a['annotation_missing_contexts']==v['annotation_missing']
 assert a['stop_reason']is result['stop_reason']is None and rows[-1]['event']=='finite_selection_complete' and a['owned_pause']is None and a['automatic_retry']is False and a['final_for_reader_analysis']is False
 capture.validate_stdout(verify(ex['stdout']).read_bytes(),result,a['root'])
 assert len(a['actual_request_metadata'])+len(a['missing_request_metadata'])==3*len(ready)
 by={r['measurement_key']:r for r in ready};order=[(k,i)for k in v['claims']for i in mods['campaign_reader'].INSTRUMENTS]
 missing_keys={(x['key'],x['instrument'])for x in a['missing_request_metadata']};assert len(missing_keys)==len(a['missing_request_metadata']) and missing_keys<=set(order)
 assert[(x['key'],x['instrument'])for x in a['actual_request_metadata']]==[x for x in order if x not in missing_keys]
 for key,instrument in missing_keys:
  directory=Path(a['root'])/key/instrument;assert not(directory/'request.json').exists() or not(directory/'attempt.json').exists()
 for m in a['actual_request_metadata']:
  e=by[m['key']]['payloads'][m['instrument']];q=m['request'];attempt=ck(m['attempt']);verify(q)
  assert q['path']==str(Path(a['root'])/m['key']/m['instrument']/'request.json')
  assert 'file_sha256'in e and q['sha256']==e['file_sha256'] and q['bytes']==e['body_bytes']+1
  # The immutable prepared whole-file pin proves the wire body without decoding its scientific context.
  assert attempt['instrument']==m['instrument'] and attempt['request_sha256']==e['canonical_sha256'] and attempt['source_sha256']==native_reader_source(cohort_kind,prep)['sha256'] and attempt['max_retries']==0 and attempt['store']is False and all(attempt[k]==value for k,value in e['pins'].items())
 capture_execution=ck(pin(CAPTURE_EXECUTION));assert capture_execution['returncode']==0 and capture_execution.get('automatic_retry',False)is False and verify(capture_execution['stderr']).read_bytes()==b'' and json.loads(verify(capture_execution['stdout']).read_bytes())==ap
 counts=collections.Counter((i,s)for e in rows if e['event']=='finished'for i,s in e['instrument_statuses'].items())
 out=dict(schema='causes.finite_reader_route_closed_cut_independent_review.v1',at=datetime.datetime.now(datetime.timezone.utc).isoformat(),accepted_finite_reader_route_closed_cut=True,accepted_fresh_candidate_reader_batch_closed_cut=cohort_kind=='fresh_candidate',actual=ap,**{k:a[k]for k in ['source','request','plan','plan_review','reader_source','root','journal','journal_original','runtime_result','prepared','route_predecessor','route_state_carry','identity','actor_process','supervisor_intent','packet','durable_adoption','execution','complete','submit_intent','submit_result']},source_review=rq['source_review'],root_capture_adoption=rq['root_capture_adoption'],intervening_reader_cuts=plan['intervening_reader_cuts'],prior_claimed_counts=[x['claimed']for x in history],prior_claimed_unique=len(all_previous),prior=pin(PRIOR) if PRIOR is not None else None,prior_review=priorpeerref,direct_closed_binding=direct_binding_ref,prior_progress_prefix_verified=prior is not None,capture_execution=pin(CAPTURE_EXECUTION),all_claims_accounted=True,assigned=len(assigned),ready=len(ready),claimed=len(ready),finished_or_unresolved=len(ready),active=0,unclaimed=0,actual_request_metadata_verified=len(a['actual_request_metadata']),missing_request_metadata=a['missing_request_metadata'],annotation_missing_contexts=len(v['annotation_missing']),native_failure_events=v['failures'],instrument_status_counts=[dict(instrument=i,status=s,count=n)for(i,s),n in sorted(counts.items())],initial_consecutive_failed_contexts=v['inherited'],final_consecutive_failed_contexts=v['final_counter'],quarantine=None,stop_reason=None,owned_pause=None,final_for_reader_analysis=False,findings=[],checks=['Exact descriptor-pinned plan, native closed source, accepted inputs/carry, sole capture/root adoption and optional genuine last-progress joins; direct closure requires original successful COMPLETE before first-capture binding.','Exact original native supervisor/reader packet, argv, rc0 COMPLETE/EXECUTION, empty stderr, native stdout/RESULT and saved original process absence/same free watch lock.','Any genuine prior progress journal prefix is retained; when absent, no prefix or earlier lock identity is claimed. Native load and complete-history replay reproduce the accepted nonempty ready count, ordered claims/terminals, zero unclaimed and every prior closed history, excluding every attempted key.','Every three-instrument slot accounted as exact original prepared request whole hash/attempt metadata or an explicit native absent-metadata entry; no semantic response/context decoding and no attempt replay.'],limitations=['Process absence/free lock are the saved genuine collector observations; no fresh process or lock observation by reviewer.','This operational peer closes only the exact accepted selection. Every prior attempt, including failed or unresolved attempts, remains excluded; cumulative counts retain the native frozen roster. No global study completion or runtime/import authority is asserted.'],review_source=pin(Path(__file__).resolve()),descriptor=descriptor_ref,reviewer='caus_prearm_recover',model_calls=0,host_calls=0,new_captures=0,response_or_judgment_reads=0)
 if settings['schema']=='causes.saved_reader_closed_audit_descriptor.v2':
  out.update(cohort_kind=cohort_kind,accepted_saved_corpus_reader_batch_closed_cut=cohort_kind=='saved_corpus',process_journal_progress=simple_progress)
  if cohort_kind=='derived':out['accepted_derived_reader_batch_closed_cut']=True
  if simple_progress is not None:
   out['prior_progress_prefix_verified']=True
   out['checks'][2]='Exact saved process identities and all declared dated journal prefixes verified; no historical watch-lock inode is claimed. Unchanged native load/history/request checks retain every prior attempt and fully account for the accepted selection.'
 return out

def main(argv=None):
 parser=argparse.ArgumentParser(description=__doc__)
 parser.add_argument('--descriptor',required=True,type=json.loads)
 args=parser.parse_args(argv);settings=ck(args.descriptor);output=Path(settings['output']);root=Path(settings['root'])
 def audit(event,args):
  if event in ('subprocess.Popen','os.system','os.kill','os.killpg','socket.connect','socket.bind'):
   raise AssertionError('no_external_action_in_closed_review:'+event)
  if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
   path=Path(os.fsdecode(args[0])).resolve()
   assert path!=root/'.env' and path.name not in ('response.raw','response.json','parsed.json','result.json','judgment.json','initial_context.json','early_context.json','end_context.json'),'no_credentials_or_semantic_reads'
   if args[2]&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):assert path==output,'only_final_peer_write'
 sys.addaudithook(audit)
 out=review_saved_closed(args.descriptor)
 with output.open('x')as f:json.dump(out,f,sort_keys=True,indent=2);f.write('\n')
 print(json.dumps(pin(output),sort_keys=True))

if __name__=='__main__':main()
