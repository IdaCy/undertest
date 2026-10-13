import copy
import hashlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from src.causes import campaign_selected_reader_feed as feed


class SelectedReaderFeedTests(unittest.TestCase):
    def setUp(self):
        self.docs = {}
        tools = ['presence', 'strongly_favors', 'leans_towards_or_favors']
        jobs = [dict(allocation=dict(run_id=f'run{i}', continue_to_end=False,
                    instruments=['evaluation_presence', 'evaluation_strongly_favors', 'evaluation_leans_towards_or_favors'],
                    reader_stages=['early'], reader_thresholds=tools), spec=dict(task_id=f'task{i}')) for i in range(6)]
        selection = dict(stage='early', reader_instruments=tools, global_jobs='global', assignments='assignments',
                         contexts=6, rows=[], stages=[], host_partitions={}, selected_runs=[])
        registration = dict(schema='causes.selected_actor_reader_feed_registration.v1', selection='selection',
                            selection_review='selection_peer', configuration='configuration', producers=[],
                            execution_host='producer', remote_base='/campaign', destination='/saved',
                            self_probe_custody=dict(originals_retained_on_producer=True, local_copy_included=False))
        for role, ordinals in [('a', [0, 2, 4]), ('b', [1, 3, 5])]:
            selection['host_partitions'][role] = dict(jobs=role+'_jobs', ordinals=role+'_ordinals')
            self.docs[role+'_jobs'] = [jobs[i] for i in ordinals];self.docs[role+'_ordinals'] = ordinals
            for pos, stage in enumerate(['first8', 'next64', 'remaining']):
                name = role+'_'+stage;output = '/campaign/'+name
                selection['stages'].append(dict(stage=stage, role=role, host_ordinal_range=[pos,pos+1],
                                               contexts=1, output_root=output, relative_root=name))
                plan = dict(source='runtime_source', runtime_authorized=True, role=role, execution_host='producer',
                            output=output, jobs=name+'_jobs', config='actor_config', global_jobs='global',
                            host_jobs=role+'_jobs', host_ordinals=role+'_ordinals', max_full=0, prefix_policy=None,
                            extension_prototypes=[], workers=4 if stage=='first8' else 8, ordinal_range=[pos,pos+1])
                self.docs[name+'_plan'] = plan;self.docs[name+'_jobs'] = [jobs[ordinals[pos]]]
                flag = 'accepted_requested_fixed8_actor_plan' if stage=='first8' else 'accepted_ordinal_actor_plan'
                self.docs[name+'_review'] = {flag:True, 'findings':[], 'plan':name+'_plan', 'source':'runtime_source'}
                registration['producers'].append(dict(stage=stage, role=role, plan=name+'_plan', review=name+'_review',
                    execution_host='producer', output_root=output, jobs=name+'_jobs', config='actor_config'))
        for i, job in enumerate(jobs):
            role='a' if i%2==0 else 'b';pos=i//2;stage=['first8','next64','remaining'][pos]
            selection['rows'].append(dict(global_ordinal=i, run_id=job['allocation']['run_id'], provider_role=role,
                host_ordinal=pos, stage=stage, execution_host='producer', relative_run=f'{role}_{stage}/run{i}',
                job_sha256=hashlib.sha256(feed.encoded(job)).hexdigest()))
        selection['selected_runs'] = [x['relative_run'] for x in selection['rows']]
        config=dict(host='producer', remote_base='/campaign', roots=[s['relative_root'] for s in selection['stages']],
                    destination='/saved', free_space_floor_bytes=80*1024**3, limit=8,
                    selected_runs=selection['selected_runs'], reader_inputs_only=True)
        self.docs.update(global_=jobs, selection=selection, configuration=config, actor_config={},
            selection_peer=dict(accepted_requested_actor_reader_selection_preparation=True,findings=[],selection='selection',
                                configuration='configuration',global_jobs='global',assignments='assignments'))
        self.docs['global']=self.docs.pop('global_');self.registration=registration

    def validate(self):
        return feed.validate_registration(self.registration, lambda ref: copy.deepcopy(self.docs[ref]))

    def test_complete_exact_physical_selection(self):
        self.assertEqual(self.validate(),self.docs['configuration'])

    def test_changed_job_under_an_old_accepted_peer(self):
        self.docs['a_next64_jobs'][0]=self.docs['global'][0]
        with self.assertRaisesRegex(ValueError,'stage_jobs'):self.validate()

    def test_provider_executor_is_not_inferred_from_role(self):
        self.docs['b_first8_plan']['execution_host']='foreign_provider'
        with self.assertRaisesRegex(ValueError,'producer_identity'):self.validate()

    def test_wrong_plan_join_even_with_accepted_flag(self):
        self.docs['a_next64_review']['plan']='other'
        with self.assertRaisesRegex(ValueError,'producer_plan_review'):self.validate()

    def test_pending_plan_is_not_copy_authority(self):
        self.docs['b_remaining_plan']['runtime_authorized']=False
        with self.assertRaisesRegex(ValueError,'producer_identity'):self.validate()

    def test_absent_prefix_policy_matches_native_finite_plan(self):
        for name,value in self.docs.items():
            if name.endswith('_plan'):value.pop('prefix_policy')
        self.assertEqual(self.validate(),self.docs['configuration'])

    def test_nonempty_prefix_policy_refuses(self):
        self.docs['a_next64_plan']['prefix_policy']={'extend':True}
        with self.assertRaisesRegex(ValueError,'producer_protocol'):self.validate()

    def test_missing_producer_refuses(self):
        self.registration['producers'].pop()
        with self.assertRaisesRegex(ValueError,'producer_partition'):self.validate()

    def test_duplicate_producer_refuses(self):
        self.registration['producers'].append(copy.deepcopy(self.registration['producers'][0]))
        with self.assertRaisesRegex(ValueError,'producer_partition'):self.validate()

    def test_changed_host_partition_refuses(self):
        self.docs['a_ordinals']=[0,2,3]
        with self.assertRaisesRegex(ValueError,'host_membership'):self.validate()

    def test_changed_global_order_refuses(self):
        self.docs['selection']['rows'][0],self.docs['selection']['rows'][1]=self.docs['selection']['rows'][1],self.docs['selection']['rows'][0]
        with self.assertRaisesRegex(ValueError,'selected_job_identity'):self.validate()

    def test_changed_physical_root_refuses(self):
        self.docs['selection']['rows'][2]['relative_run']='other/run2'
        with self.assertRaisesRegex(ValueError,'selected_stage'):self.validate()

    def test_changed_ordinal_range_refuses(self):
        self.docs['a_next64_plan']['ordinal_range']=[2,3]
        with self.assertRaisesRegex(ValueError,'producer_ordinal_range'):self.validate()

    def test_outside_selection_configuration_refuses(self):
        self.docs['configuration']['selected_runs']=self.docs['configuration']['selected_runs']+['other/run']
        with self.assertRaisesRegex(ValueError,'copy_configuration'):self.validate()

    def test_complete_self_evidence_copy_is_not_authorized(self):
        self.registration['self_probe_custody']['local_copy_included']=True
        with self.assertRaisesRegex(ValueError,'self_custody_scope'):self.validate()

    def test_larger_copy_scope_or_lower_floor_refuses(self):
        for field,value in [('reader_inputs_only',False),('free_space_floor_bytes',79*1024**3),('limit',9)]:
            with self.subTest(field=field):
                old=self.docs['configuration'][field];self.docs['configuration'][field]=value
                with self.assertRaisesRegex(ValueError,'copy_configuration'):self.validate()
                self.docs['configuration'][field]=old

    def test_json_distinct_job_content_refuses(self):
        self.docs['a_next64_jobs']=copy.deepcopy(self.docs['a_next64_jobs'])
        self.docs['a_next64_jobs'][0]['allocation']['continue_to_end']=0
        with self.assertRaisesRegex(ValueError,'stage_jobs'):self.validate()

    def complete_load(self, observe=None, **changes):
        """Use synthetic custody files with the repository's real native constructors."""
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);written={}
            def save(name,value):
                p=root/(name+'.json');p.write_text(json.dumps(value,sort_keys=True)+'\n');return feed.pin(p)
            def convert(value):
                if isinstance(value,dict):return {k:convert(v)for k,v in value.items()}
                if isinstance(value,list):return [convert(v)for v in value]
                if isinstance(value,str)and value in self.docs:
                    if value not in written:written[value]=save(value,convert(self.docs[value]))
                    return written[value]
                return value
            registration=convert(self.registration);reg=save('registration',registration)
            repository=Path(__file__).resolve().parents[2]
            parent=root/'parent.py';parent.write_text('import sys\n'+f'sys.path.insert(0, {str(repository/"src")!r})\n'+'from causes import campaign_handoff as h\nfrom causes import campaign_snapshot as snapshot\n')
            sources={key:feed.pin(repository/'src/causes'/('campaign_'+key+'.py'))for key in ['handoff','snapshot']}
            sources['watch']=feed.pin(repository/'src/causes/campaign_snapshot_watch.py')
            ssh=root/'ssh_config';ssh.write_text('# synthetic test input\n')
            p=dict(schema='causes.selected_actor_reader_feed_plan.v1',runtime_authorized=True,
                   producer_plan_binding_complete=True,source=feed.pin(feed.__file__),registration=reg,
                   configuration=registration['configuration'],remote_base='/campaign',destination='/saved',
                   transport_parent=feed.pin(parent),sources=sources,ssh_config=feed.pin(ssh),interval_seconds=180,
                   maximum_local_copy_bytes=45_000_000_000, watch_directory=str(root/'watch'))
            p.update(changes);plan=save('plan',p)
            review=save('review',dict(accepted_selected_actor_reader_feed_preparation=True,plan=plan,source=p['source'],findings=[]))
            authority=save('authority',dict(root_authorized=True,no_user_revocation=True,plan=plan,review=review,maximum_invocations=1,automatic_retry=False))
            result=feed.load(plan,review,authority)
            if observe is not None:observe(result,root,written)
            return result

    def test_complete_load_with_real_native_constructor(self):
        p,config,watch,authorize,parent=self.complete_load()
        self.assertEqual(config.host,'producer');self.assertTrue(config.reader_inputs_only)
        self.assertIs(watch.snapshot,parent.snapshot)

    def test_pending_flags_and_schema_refuse_before_import(self):
        for changes in [dict(runtime_authorized=False),dict(producer_plan_binding_complete=False),dict(schema='other')]:
            with self.subTest(changes=changes),patch.object(feed.importlib.util,'spec_from_file_location',side_effect=AssertionError('native import reached')):
                with self.assertRaisesRegex(ValueError,'pending_or_untyped_copy_plan'):self.complete_load(**changes)

    def test_authorize_rehashes_transitive_inputs_without_rederiving_jobs(self):
        def observe(result,root,written):
            _,config,_,authorize,_=result
            with patch.object(feed,'validate_registration',side_effect=AssertionError('rederived jobs')):
                first=authorize(config);self.assertEqual(first,authorize(config))
                for name in ['global','a_jobs','a_ordinals','a_remaining_jobs','actor_config','a_remaining_review']:
                    with self.subTest(name=name):
                        path=Path(written[name]['path']);original=path.read_bytes()
                        path.write_bytes(original+b' ')
                        try:
                            with self.assertRaises((ValueError,AssertionError)):authorize(config)
                        finally:path.write_bytes(original)
        self.complete_load(observe=observe)

    def test_local_storage_wait_authorization_checks_retained_metadata(self):
        def observe(result, root, written):
            p, config, _, authorize, _ = result
            directory = Path(p['watch_directory']); directory.mkdir()
            with (directory/'retained_wait_receipts').open('wb') as stream:
                stream.truncate(p['maximum_local_copy_bytes'] - 2*1024**3 + 1)
            with patch.object(feed, 'validate_registration', side_effect=AssertionError('rederived jobs')), patch.object(feed.subprocess, 'run') as transport:
                with self.assertRaisesRegex(ValueError, 'local_copy_budget_hold'):
                    authorize(config)
                transport.assert_not_called()
        self.complete_load(observe=observe)

    def test_budget_counts_payload_and_watcher_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);payload=root/'payload';watch=root/'watch'
            payload.mkdir();watch.mkdir();(payload/'data').write_bytes(b'a'*19);(watch/'receipt').write_bytes(b'b'*23)
            self.assertEqual(feed.check_local_budget([payload,watch],3*1024**3),42)
            with (watch/'large_receipt').open('wb') as stream:stream.truncate(1024**3)
            with self.assertRaisesRegex(ValueError,'local_copy_budget_hold'):
                feed.check_local_budget([payload,watch],3*1024**3)

    def test_budget_rejects_symlinks_and_invalid_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'link').symlink_to('/tmp')
            with self.assertRaisesRegex(ValueError,'copy_budget_symlink'):feed.check_local_budget([root],3*1024**3)
            for limit in [True,50_000_000_000,1024**3]:
                with self.subTest(limit=limit),self.assertRaisesRegex(ValueError,'invalid_copy_budget'):
                    feed.check_local_budget([],limit)

    def test_budget_hold_precedes_transport_and_restores_native_module(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);original=object();snapshot=types.SimpleNamespace(subprocess=original)
            parent=types.SimpleNamespace(snapshot=snapshot,h=types.SimpleNamespace(checked_bytes=lambda ref:None),
                                         strict_transport=lambda argv,p:argv)
            config=types.SimpleNamespace(destination=root/'destination')
            p=dict(watch_directory=str(root/'watch'),ssh_config={},maximum_local_copy_bytes=45_000_000_000,interval_seconds=180)
            def watch(*args,**kwargs):snapshot.subprocess.run(['ssh','synthetic'])
            loaded=(p,config,types.SimpleNamespace(watch_completed=watch),None,parent)
            with patch.object(feed,'load',return_value=loaded),patch.object(feed,'check_local_budget',side_effect=ValueError('local_copy_budget_hold')),patch.object(feed.subprocess,'run') as transport:
                with self.assertRaisesRegex(ValueError,'local_copy_budget_hold'):feed.main(None,None,None)
                transport.assert_not_called()
            self.assertIs(snapshot.subprocess,original)

    def test_main_passes_the_reviewed_idle_interval_to_native_watch(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);snapshot=types.SimpleNamespace(subprocess=object())
            parent=types.SimpleNamespace(snapshot=snapshot)
            config=types.SimpleNamespace(destination=root/'destination')
            p=dict(watch_directory=str(root/'watch'),interval_seconds=180)
            watch=types.SimpleNamespace(watch_completed=lambda *a,**k:{'interval':k['interval_seconds']})
            with patch.object(feed,'load',return_value=(p,config,watch,None,parent)),patch('builtins.print') as output:
                feed.main(None,None,None)
                self.assertEqual(json.loads(output.call_args.args[0]),{'interval':180})


if __name__=='__main__':unittest.main()
