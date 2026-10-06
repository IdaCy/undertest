import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_handoff as h
from causes import campaign_readiness as r


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve()

    def save(self,name,value,raw=False):
        path=self.base/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(value if raw else h.encoded(value)+b'\n')
        return h.pin(path)

    def test_capture_requires_one_exact_original_to_local_mapping(self):
        local=self.save('capture/value.json',{'value':1});remote=dict(local,path='/remote/original/value.json')
        mapping=self.save('CAPTURE.json',{'files':[dict(remote=remote,local=local)]})
        self.assertEqual(r.captured(mapping,remote),{'value':1})
        duplicate=self.save('DUPLICATE.json',{'files':[dict(remote=remote,local=local)]*2})
        with self.assertRaisesRegex(ValueError,'unique_receipt'):r.captured(duplicate,remote)
        changed=self.save('CHANGED.json',{'files':[dict(remote=remote,local=self.save('capture/changed',{'value':2}))]})
        with self.assertRaisesRegex(ValueError,'capture_bytes'):r.captured(changed,remote)

    def test_stable_metadata_copy_preserves_original_pin_and_detects_midread_change(self):
        reference=self.save('LIVE.json',{'roots':['one','two']});output=self.base/'output';output.mkdir()
        copied=r.stable_copy(reference,output,'COPY.json')
        self.assertEqual(copied['original'],reference)
        self.assertEqual(h.checked_bytes(copied['copy']),h.checked_bytes(reference))
        stat=r.stat_identity(reference['path'])
        with patch.object(r,'stat_identity',side_effect=[stat,{**stat,'st_ino':stat['st_ino']+1}]),self.assertRaisesRegex(ValueError,'during_read'):
            r.stable_copy(reference,output,'BAD.json')
        self.assertFalse((output/'BAD.json').exists())

    def waiting(self):
        process={'pid':12,'process_starttime_ticks':'345','argv':['python3','-m','fixture']}
        continuation={'control':{'path':'/fixture/CONTROL'},'binding':{'family':'fixture'}}
        launch={'process':process,'continuation':continuation}
        observation={'controller':process,'lock_owner':[12],'prior_local_owners_closed':True,
            'journal':{'event_counts':{'controller_started':1,'admission_holds_bound':1,'admission_barrier_waiting':1},
                       'lifetime_route_failures':0,'quarantines':{},'unconsumed_tail_bytes':0},
            'continuation':dict(continuation,progress=dict(waiting_behind_barrier=True,initial_jobs=0,claimed=0,
                published_blocks=0,first_block_observed=False),first_published_block=None)}
        return observation,launch

    def test_continuous_readiness_requires_empty_initial_queue_and_unreleased_barrier(self):
        observation,launch=self.waiting();r.waiting_progress(observation,launch)
        for key,value in [('initial_jobs',4477),('claimed',1),('published_blocks',1),('waiting_behind_barrier',False),('claimed',0.0)]:
            changed=copy.deepcopy(observation);changed['continuation']['progress'][key]=value
            with self.subTest(key=key,value=value),self.assertRaisesRegex(ValueError,'progress_changed'):r.waiting_progress(changed,launch)
        changed=copy.deepcopy(observation);changed['journal']['event_counts']['claimed']=1
        with self.assertRaisesRegex(ValueError,'issued_or_faulted'):r.waiting_progress(changed,launch)

    def sync(self):
        cycle=self.base/'cycle';cycle.mkdir();destination=self.base/'snapshots';attempt=destination/'attempts/one';attempt.mkdir(parents=True)
        roots=['runs/old','runs/waiting'];scope={'destination':str(destination),'host':'fixture','remote_base':'/fixture','roots':roots}
        self.save('snapshots/attempts/one/REQUEST.json',dict(host='fixture',base='/fixture',roots=roots))
        manifest=self.save('snapshots/attempts/one/MANIFEST.json',dict(host='fixture',base='/fixture',runs=[
            dict(run='runs/old/id',files=[{'path':'summary.json'}],bytes=9,summary_sha256='s')],skipped=[]))
        row=dict(run='runs/old/id',path=str(attempt/'data/runs/old/id'),manifest=manifest['path'],manifest_sha256=manifest['sha256'],files=1,bytes=9,summary_sha256='s',verified_at=h.now())
        self.save('snapshots/attempts/one/VERIFIED.json',dict(runs=[row]))
        self.save('cycle/sync.py.stdout',dict(attempt=str(attempt),verified_runs=1,verified_bytes=9,skipped=[]))
        self.save('snapshots/accepted.jsonl',h.encoded(row)+b'\n'+b'{"unfinished":',raw=True)
        output=self.base/'output';output.mkdir()
        return cycle,scope,output,row

    def test_sync_checks_manifest_index_and_retains_complete_prefix_with_tail_limit(self):
        cycle,scope,output,row=self.sync()
        result=r.sync_check(cycle,'sync.py',scope,output,'JP')
        self.assertEqual(result['verified_run_ids'],['runs/old/id'])
        self.assertEqual(result['index']['unconsumed_tail_bytes'],len(b'{"unfinished":'))
        self.assertEqual(h.checked_bytes(result['index']['prefix']),h.encoded(row)+b'\n')
        self.assertTrue(result['no_new_snapshot_content_rehash'])

    def test_sync_root_drift_and_duplicate_accepted_receipt_refuse(self):
        cycle,scope,output,row=self.sync()
        with self.assertRaisesRegex(ValueError,'roots_changed'):r.sync_check(cycle,'sync.py',{**scope,'roots':['runs/old']},output,'JP')
        Path(scope['destination'],'accepted.jsonl').write_bytes((h.encoded(row)+b'\n')*2)
        with self.assertRaisesRegex(ValueError,'not_once'):r.sync_check(cycle,'sync.py',scope,output,'JP')
        self.assertFalse((output/'JP_INDEX_PREFIX.jsonl').exists())

    def test_watcher_identity_cannot_be_merged_by_numeric_pid_coercion(self):
        identity={'pid':12,'process_starttime_ticks':'345','argv':['python3','watch.py']}
        self.assertTrue(r.same_identity(identity,dict(identity,process_starttime_ticks=345)))
        self.assertFalse(r.same_identity(identity,dict(identity,pid='12')))
        self.assertFalse(r.same_identity(identity,dict(identity,process_starttime_ticks='346')))

    def test_publication_requires_complete_ordered_targets_and_preserved_pins(self):
        source=self.save('candidate.json',{'new':True});old=self.save('preserved.json',{'roots':['old']})
        target=str(self.base/'installed.json')
        proposal={'rows':[{'target':target,'after':source}],
                  'preserved':[{'target':old['path'],'pin':old}]}
        result={'applied':[target],'current':[dict(source,path=target),old]}
        r.publication_complete(result,proposal)
        for applied in (True,[],[target,target]):
            with self.subTest(applied=applied),self.assertRaisesRegex(ValueError,'completely_applied'):
                r.publication_complete(dict(result,applied=applied),proposal)
        for current in (result['current'][:1],result['current'][::-1],
                        [dict(result['current'][0],sha256='changed'),old]):
            with self.subTest(current=current),self.assertRaisesRegex(ValueError,'final_pins_changed'):
                r.publication_complete(dict(result,current=current),proposal)


if __name__=='__main__':unittest.main()
