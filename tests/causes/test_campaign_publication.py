import copy
import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_publication as p
from tests.causes.test_campaign_observation import fixture as observation_fixture


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.metadata = self.root/'metadata';self.metadata.mkdir()
        self.watch = self.metadata/'watch';self.watch.mkdir()
        (self.watch/'watch.lock').write_bytes(b'')
        self.sources = self.root/'code'/'causes';self.sources.mkdir(parents=True)
        admission = self.sources/'campaign_admission.py';admission.write_text('# immutable fixture\n')
        source = p.h.pin(admission)
        package = self.save('admission-package',{'execution_sources':[source]})
        review = self.save('admission-review',{'accepted_continuous_admission_source':True,'source':source,'package':package})
        self.targets = {role:str(self.metadata/(role+'.json')) for role in p.ROLES}
        for path in self.targets.values():Path(path).write_bytes(b'old\n')
        self.after = {role:self.save('after-'+role,{'role':role}) for role in p.ROLES if role!='authorization_pointer'}
        self.process = {'pid':2147483647,'process_starttime_ticks':1,'argv':['python3',self.targets['watcher']]}
        handoff = {'directory':str(self.watch),'pause_paths':[str(self.root/'PAUSE')],
            'predecessor':{'process':self.process,'source':p.h.pin(self.targets['watcher'])},
            'successor':{key:dict(self.after[role],path=self.targets[role])
                         for key,role in [('source','watcher'),('configuration','watcher_configuration')]}}
        self.handoff = self.save('handoff',handoff)
        handoff_review = self.save('handoff-review',{'accepted_campaign_watcher_handoff_source':True,'configuration':self.handoff})
        self.preserved = self.save('preserved',{'unchanged':'legacy'})
        template = {'schema':'causes.continuous_publication_template.v1','metadata_directory':str(self.metadata),
            'targets':self.targets,'watcher_handoff':self.handoff,'watcher_handoff_review':handoff_review,'preserved':[self.preserved],
            'pause_paths':handoff['pause_paths'],'waiting_guard':{'host':'fixture','source':source,'sources':[source],
                'source_review':review,'package':package,'pythonpath':str(self.sources.parent)}}
        self.template = self.save('template',template)
        self.registration = self.save('registration',{})
        registration_review = self.save('registration-review',{'accepted_companion_registration':True,'registration':self.registration})
        self.config = {'schema':'causes.continuous_publication_configuration.v1','template':self.template,
            'metadata_directory':str(self.metadata),'targets':self.targets,'after':self.after,
            'monitor_policy':self.save('policy',{}),'observation_configuration':self.save('observation',{}),
            'companion_source':p.h.pin(p.monitor.__file__),'registration':self.registration,
            'waiting_report':self.save('waiting',{}),'waiting_review':self.save('waiting-review',{}),
            'receipt_map':self.save('map',{'files':[]}),'watcher_handoff':self.handoff,
            'preserved':[self.preserved],'pause_paths':handoff['pause_paths'],'waiting_guard':template['waiting_guard']}
        configuration = self.save('configuration',self.config)
        inputs = self.save('prepare-inputs',{'configuration':configuration,'registration_review':registration_review})
        with patch.object(p.monitor,'validate_registration'),patch.object(p.monitor,'validate_authorization'),patch.object(p.monitor,'validate_policy'):
            self.proposal = p.prepare(inputs,self.root/'proposal')
        proposal_review = self.save('proposal-review',{'accepted_continuous_publication_candidate':True,'proposal':self.proposal})
        stop = self.watch/'STOP';stop.write_bytes(b'owned\n')
        archived = self.root/'stop-created';archived.write_bytes(stop.read_bytes())
        ownership = self.save('stop-ownership',{'marker':p.h.pin(stop),'stat':p.h.stat_identity(stop),
                                             'archived_marker':p.h.pin(archived)})
        cycle = self.watch/'20260101T000000Z_abcdef01';cycle.mkdir()
        final = p.h.save_new(cycle,'RESULT.json',{'checks':[],'ended_at':'2026-01-01T00:00:01+00:00'})
        retired = {'retired':True,'lock_free':True,'process':self.process,
            'source':handoff['predecessor']['source'],'configuration':self.handoff,'proposal':self.proposal,
            'lock_identity':[(self.watch/'watch.lock').stat().st_dev,(self.watch/'watch.lock').stat().st_ino],
            'stop_ownership':ownership,'owned_stop':p.h.pin(stop),
            'final_cycle_directory':str(cycle),'final_completed_cycle':final}
        self.retirement = self.save('retirement',retired)
        retirement_review = self.save('retirement-review',{'accepted_JP_watcher_retirement':True,'retirement':self.retirement})
        now = dt.datetime.now(dt.timezone.utc)
        self.authority_value = {'root_authorized':True,'no_user_revocation':True,
            'action':'publish_continuous_observation','model_requests_authorized':False,
            'issued_at':now.isoformat(),'expires_at':(now+dt.timedelta(seconds=180)).isoformat(),
            'proposal':self.proposal,'proposal_review':proposal_review,
            'watcher_retirement':self.retirement,'watcher_retirement_review':retirement_review}
        self.authority = self.save('authority',self.authority_value)

    def save(self,name,value):
        return p.h.save_new(self.root,name+'.json',value)

    def test_prepare_preserves_original_target_and_separate_archive(self):
        proposal = p.h.checked(self.proposal)
        for row in proposal['rows']:
            self.assertEqual(row['before']['path'],row['target'])
            self.assertNotEqual(row['before_archive']['path'],row['target'])
            self.assertEqual(p.h.checked_bytes(row['before']),p.h.checked_bytes(row['before_archive']))
        self.assertEqual([r['role'] for r in proposal['rows']],list(p.ROLES))

    def test_default_validation_has_no_observation_or_target_write(self):
        with patch.object(p,'observe_candidate') as observe:
            result = p.apply(self.authority,self.root/'never-created')
        self.assertFalse(result['executed']);observe.assert_not_called()
        self.assertFalse((self.root/'never-created').exists())
        self.assertTrue(all(Path(path).read_bytes()==b'old\n' for path in self.targets.values()))

    def test_success_preserves_stop_and_records_all_after_and_preserved_pins(self):
        stop_identity = p.h.stat_identity(self.watch/'STOP')
        with patch.object(p,'observe_candidate',return_value={}):
            result = p.h.checked(p.apply(self.authority,self.root/'applied',True))
        expected = [dict(row['after'],path=row['target']) for row in p.h.checked(self.proposal)['rows']]+[self.preserved]
        self.assertEqual(result['current'],expected)
        self.assertEqual(result['applied'],list(self.targets.values()))
        self.assertFalse(result['requests_authorized'])
        self.assertEqual(p.h.stat_identity(self.watch/'STOP'),stop_identity)
        self.assertEqual(len(list((self.root/'applied').glob('STEP_*_RESULT.json'))),8)

    def test_replace_failure_preserves_exact_partial_state_and_no_retry(self):
        real = p.os.replace;calls=[]
        def replace(source,target):
            calls.append(str(target))
            if len(calls)==3:raise OSError('injected atomic replacement failure')
            return real(source,target)
        with patch.object(p,'observe_candidate',return_value={}),patch.object(p.os,'replace',side_effect=replace):
            with self.assertRaisesRegex(OSError,'injected'):
                p.apply(self.authority,self.root/'partial',True)
        failure = json.loads((self.root/'partial'/'FAILED.json').read_text())
        self.assertEqual(failure['applied'],list(self.targets.values())[:2])
        self.assertTrue(failure['no_automatic_retry']);self.assertTrue(Path(failure['pending']).exists())
        self.assertEqual(Path(list(self.targets.values())[2]).read_bytes(),b'old\n')
        self.assertFalse((self.root/'partial'/'RESULT.json').exists())

    def test_stale_authority_and_replaced_stop_refuse_before_observation(self):
        old = copy.deepcopy(self.authority_value)
        now=dt.datetime.now(dt.timezone.utc)
        old.update(issued_at=(now-dt.timedelta(seconds=181)).isoformat(),expires_at=(now-dt.timedelta(seconds=1)).isoformat())
        with patch.object(p,'observe_candidate') as observe:
            with self.assertRaisesRegex(ValueError,'expired'):p.apply(self.save('stale',old),self.root/'no-stale',True)
            stop=self.watch/'STOP';stop.unlink();stop.write_bytes(b'owned\n')
            with self.assertRaisesRegex(ValueError,'STOP_identity'):p.apply(self.authority,self.root/'no-stop',True)
        observe.assert_not_called()

    def test_file_and_lock_replacement_are_detected(self):
        Path(self.preserved['path']).write_text('changed')
        with self.assertRaisesRegex(ValueError,'handoff_input_pin_changed|preserved_metadata_changed'):
            p.apply(self.authority,self.root/'no-preserved',True)
        # Restore exact bytes and original proposal's stat is deliberately still stale.
        self.assertFalse((self.root/'no-preserved').exists())

    def test_lock_replacement_during_readonly_observation_refuses_first_write(self):
        def replace_lock(*args):
            lock=self.watch/'watch.lock';lock.unlink();lock.write_bytes(b'')
        with patch.object(p,'observe_candidate',side_effect=replace_lock):
            with self.assertRaisesRegex(ValueError,'lock_inode_changed'):
                p.apply(self.authority,self.root/'lock-failure',True)
        self.assertTrue(all(Path(path).read_bytes()==b'old\n' for path in self.targets.values()))
        self.assertEqual(json.loads((self.root/'lock-failure'/'FAILED.json').read_text())['applied'],[])

    def test_replaced_retired_lock_refuses_before_output_or_observation(self):
        lock=self.watch/'watch.lock'
        replacement=self.watch/'replacement.lock';replacement.write_bytes(b'')
        os.replace(replacement,lock)
        with patch.object(p,'observe_candidate') as observe:
            for execute in (False,True):
                with self.assertRaisesRegex(ValueError,'retired_watcher_lock_identity_changed'):
                    p.apply(self.authority,self.root/'no-replaced-lock',execute)
            observe.assert_not_called()
        self.assertFalse((self.root/'no-replaced-lock').exists())
        self.assertTrue(all(Path(path).read_bytes()==b'old\n' for path in self.targets.values()))

    def test_captured_remote_receipts_preserve_original_pin_and_reject_conflicts(self):
        local=self.save('captured',{'actual':1});remote=dict(local,path='/remote/actual.json')
        reference=self.save('capture-map',{'files':[{'remote':remote,'local':local}]})
        reader=p.CapturedReader(reference)
        self.assertEqual(reader(remote),{'actual':1});self.assertEqual(remote['path'],'/remote/actual.json')
        duplicate=self.save('duplicate-map',{'files':[{'remote':remote,'local':local}]*2})
        with self.assertRaisesRegex(ValueError,'duplicate_remote'):p.CapturedReader(duplicate)
        bad=dict(remote,bytes=True)
        with self.assertRaisesRegex(ValueError,'content_mismatch'):
            p.CapturedReader(self.save('typed-map',{'files':[{'remote':bad,'local':local}]}))

    def test_byte_identical_observation_snapshot_validates_without_changing_config(self):
        config=observation_fixture(self.root)
        originals=copy.deepcopy(config['sources'])
        for name,ref in originals.items():
            destination=self.root/('snapshot-'+name);destination.write_bytes(p.h.checked_bytes(ref))
            config['sources'][name]=p.h.pin(destination)
        saved=copy.deepcopy(config)
        p.validate_observation_candidate(config)
        self.assertEqual(config,saved)
        ref=config['sources']['campaign_monitor.py'];Path(ref['path']).write_text('changed')
        with self.assertRaises(ValueError):p.validate_observation_candidate(config)


if __name__=='__main__':unittest.main()
