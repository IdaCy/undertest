from pathlib import Path
import importlib.util,json,os,hashlib,sys

def pin(path):
 path=Path(path);body=path.read_bytes();return dict(path=str(path),bytes=len(body),sha256=hashlib.sha256(body).hexdigest())
def checked(ref):
 assert pin(ref['path'])==ref;return Path(ref['path']).read_bytes()
def read(ref):return json.loads(checked(ref))
def load(ref,name):
 checked(ref);spec=importlib.util.spec_from_file_location(name,ref['path']);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def capture(request_ref):
 request=read(request_ref);assert request['schema']=='causes.subset_self_status_request.v1'
 assert request['authorized'] is True and request['maximum_captures']==1 and request['automatic_retry'] is False
 prep=read(request['source_preparation']);peer=read(request['source_preparation_review'])
 assert peer['accepted_subset_self_status_capture_preparation'] is True and peer['preparation']==request['source_preparation'] and peer['source']==prep['source'] and not peer.get('findings')
 assert prep['source']==pin(__file__)
 p=Path(request['feed_directory']);requested_output=Path(request['output'])
 assert p.is_absolute() and p.resolve()==p and not p.is_symlink()
 assert requested_output.is_absolute() and requested_output.parent==p and requested_output.resolve()==requested_output and not requested_output.is_symlink()
 d=load(prep['durable_source'],'candidate_feed_durable')
 plan_ref=d.pin(p/'PLAN.v1.json');plan=d.read(plan_ref);review_ref=d.pin(p/'INDEPENDENT_REVIEW.v1.json');review=d.read(review_ref)
 packet_ref=d.pin(p/'ROOT_DURABLE_PACKET.v1.json');packet=d.read(packet_ref);durable_adoption_ref=d.pin(p/'ROOT_DURABLE_ADOPTION.v1.json');adoption=d.read(durable_adoption_ref)
 launch_ref=d.pin(p/'ROOT_LAUNCH_RESULT.v1.json');launch=d.read(launch_ref);root_adoption=d.read(d.pin(p/'ROOT_ADOPTION.v1.json'))
 assert review['accepted_individual_variant_self_probe_feed_preparation'] is True and review['plan']==plan_ref and review['source']==plan['source']
 assert root_adoption['root_authorized'] is True and root_adoption['plan']==plan_ref and root_adoption['review']==review_ref and root_adoption['maximum_invocations']==1 and root_adoption['automatic_retry'] is False
 assert launch['returncode']==0 and launch['stderr']=='' and adoption['root_authorized'] is True and adoption['packet']==packet_ref and adoption['attempt_id']==packet['attempt_id'] and adoption['maximum_invocations_per_actor']==1 and adoption['automatic_retry'] is False
 dur=Path(packet['output_directory']);watch=Path(plan['watch_directory']);out=requested_output;out.mkdir()
 d.save(out/'INTENT.v1.json',dict(at=d.now(),request=request_ref,source=pin(__file__),maximum_captures=1,automatic_retry=False))
 startup=read(request['startup']);startup_peer=read(request['startup_review']);previous=read(request['previous_actual']);previous_peer=read(request['previous_review'])
 assert startup_peer['accepted_individual_variant_self_probe_feed_startup_actual'] is True and startup_peer['actual']==request['startup'] and not startup_peer.get('findings')
 assert previous_peer['actual']==request['previous_actual'] and not previous_peer.get('findings') and (previous_peer.get('accepted_individual_variant_self_probe_feed_startup_actual') is True or previous_peer.get('accepted_individual_variant_self_probe_feed_index_cut') is True)
 assert startup['plan']==previous['plan']==plan_ref and startup['packet']==previous['packet']==packet_ref
 intent=d.read(d.pin(dur/'INTENT.json'));process=d.read(d.pin(dur/'copier/PROCESS.json'))
 sup=intent['supervisor'];child=process['identity']
 def capture(expected):
  ident=d.identity(expected['pid']);assert all(ident[k]==expected[k] for k in ('pid','start_ticks','argv')) and ident['state'] not in ('Z','X')
  proc=Path('/proc')/str(expected['pid'])
  return dict(identity=ident,raw_stat=(proc/'stat').read_text())
 expected=dict(supervisor=sup,copier=child)
 lock=watch/'watch.lock';st=lock.stat();key=f'{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}'
 assert dict(path=str(lock),device=st.st_dev,inode=st.st_ino)=={k:startup['watch_lock'][k] for k in ('path','device','inode')}
 completion=None;c=None
 if request.get('completed_feed_receipt') is not None:
  c=load(prep['completed_helper_source'],'accepted_self_completed_custody')
  completion=c.completed_receipts(request['completed_feed_receipt'],dict(startup,_path=request['startup']['path']),packet,plan,expected)
 before=c.retired_identity(expected,key) if completion else {name:capture(x) for name,x in expected.items()}
 index=Path(plan['monitor_paths']['index']);raw_index=index.read_bytes();complete=raw_index[:raw_index.rfind(b'\n')+1];assert complete
 cycles=[x for x in watch.iterdir() if x.is_dir() and x.name.isdigit() and (x/'RESULT.json').is_file()];assert cycles
 eligible=[x for x in cycles if d.read(d.pin(x/'RESULT.json'))['index_bytes']<=len(complete)];assert eligible
 latest=max(eligible,key=lambda x:int(x.name))
 names=[dur/'INTENT.json',dur/'LAUNCHED.json',dur/'copier/INTENT.json',dur/'copier/PROCESS.json',watch/'START.json',latest/'INTENT.json',latest/'RESULT.json']
 native={}
 for n in names:native[str(n)]=d.pin(n)
 start=d.read(native[str(watch/'START.json')]);assert start['configuration']==d.read(plan['configuration'])
 assert intent['packet']==packet_ref and intent['adoption']==durable_adoption_ref
 assert child['argv']==packet['actors'][0]['argv']
 cycle=d.read(native[str(latest/'RESULT.json')]);assert cycle['kind']=='synchronized'
 stderr=(dur/'copier/stderr').read_bytes();assert stderr==b''
 absent=[dur/'LAUNCH_ERROR.json',watch/'STOP',watch/'STOPPED.json',watch/'FAILED.json']
 if completion is None:absent += [dur/'COMPLETE.json',dur/'copier/EXECUTION.json',watch/'COMPLETE.json']
 assert all(not x.exists() for x in absent)
 lock=watch/'watch.lock';st=lock.stat();raw=Path('/proc/locks').read_text()
 key=f'{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}'
 rows=[line for line in raw.splitlines() if key in line.split()]
 if completion:assert rows==[],rows
 else:assert rows and len({tuple(row.split()[1:]) for row in rows})==1 and rows[0].split()[1:4]==['FLOCK','ADVISORY','WRITE'] and int(rows[0].split()[4])==child['pid'],rows
 idx=complete[:cycle['index_bytes']];assert idx.endswith(b'\n') and hashlib.sha256(idx).hexdigest()==cycle['index_sha256']
 assert idx.startswith(checked(startup['index']['saved'])) and idx.startswith(checked(previous['index']['saved']))
 (out/'INDEX.prefix.jsonl').open('xb').write(idx)
 rows_index=[json.loads(x) for x in idx.splitlines()];assert rows_index and len({x['run'] for x in rows_index})==len(rows_index)
 assert {x['run'] for x in rows_index}<=set(start['configuration']['selected_runs'])

 if completion:c.validate_completed_index(completion,idx,raw_index,start['configuration']['selected_runs'],rows_index,cycle,latest)
 after=c.retired_identity(expected,key) if completion else {name:capture(x) for name,x in expected.items()}
 actual=d.save(out/'ACTUAL.v1.json',dict(schema='causes.individual_variant_self_probe_feed_index_cut_actual.v1',request=request_ref,new_startup_claimed=False,lifecycle='completed' if completion else 'live',completed_feed=completion,completed_helper_source=prep['completed_helper_source'] if completion else None,observation_kind='dated_copy_progress',at=d.now(),source=d.pin(__file__),plan=plan_ref,source_review=review_ref,packet=packet_ref,root_launch=launch_ref,durable_adoption=durable_adoption_ref,native=native,before=before,after=after,watch_lock=dict(path=str(lock),device=st.st_dev,inode=st.st_ino,rows=rows,proc_locks_raw=raw),absent_paths=[str(x) for x in absent],stderr_bytes=0,latest_completed_cycle=cycle,latest_cycle_directory=str(latest),index=dict(original_path=str(index),saved=d.pin(out/'INDEX.prefix.jsonl'),bytes=len(idx),sha256=hashlib.sha256(idx).hexdigest()),accepted=len(rows_index),remaining=len(start['configuration']['selected_runs'])-len(rows_index),copy_count_at_latest_cycle=cycle['native_result']['verified_runs'],opaque_self_probe_custody_only=True,all_selected_self_probe_copies_complete=bool(completion),producer_liveness_queried=False,model_calls=0))
 return actual



if __name__=='__main__':
 assert len(sys.argv)==2
 print(json.dumps(capture(json.loads(sys.argv[1]))))
