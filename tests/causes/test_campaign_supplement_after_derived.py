"""Source-transition unit boundaries and exact saved complement, all synthetic."""
import copy
import types
import unittest

from causes import campaign_supplement_source_lineage as lineage
from causes import campaign_supplement_mixed_owner as mixed
from test_campaign_supplement_source_lineage import fixture as original_lineage
from test_campaign_supplement_mixed_owner import Memory
from test_campaign_supplement_mixed_owner import fixture as mixed_fixture
from test_campaign_supplement_original_transfer import TransferFixture
from causes import campaign_supplement as core
from causes import campaign_supplement_completed_actor as direct
from pathlib import Path
import hashlib
import json


def transition_fixture(*, resumed=False, change=lambda name, value: None):
    s, old, original = original_lineage()
    bank=s.put({'synthetic_adopted_bank':True})
    first=dict(derived_bank=bank,continuation_scope='adopted_derived_bank',
        sources=old['sources'],retain_admission_after_healthy_harvest=True,
        native_source_migration=old['reference'],predecessor_custody=old['predecessor'])
    change('first',first)
    first_ref=s.put(first);head=s.closed(first_ref)
    links=[dict(kind='derived',plan=first_ref,custody=head)]
    prior=first;prior_ref=first_ref
    if resumed:
        prior={**first,'sources':{**first['sources'],'driver':s.put(b'reviewed synthetic resume driver')},
            'continuation_scope':'derived_bank_remaining','predecessor_custody':head}
        change('resume',prior)
        prior_ref=s.put(prior);head=s.closed(prior_ref)
        links.append(dict(kind='derived',plan=prior_ref,custody=head))
    sources=dict(native_queue=old['sources']['native_queue'],completed_actor=s.put(b'new actor resolver'),
        mixed_owner=s.put(b'new partition helper'),source_lineage=s.put(b'new lineage helper'),
        driver=s.put(b'new original driver'))
    binder=s.put(b'new binder');selected={**sources,'binder':binder}
    peer=s.put(dict(accepted_original_after_derived_source=True,findings=[],sources=selected,
        previous_completed_actor_source=prior['sources']['completed_actor']))
    adoption=s.put(dict(schema='causes.original_after_derived_source_adoption.v1',
        accepted_source_for_prospective_transition=True,sources=selected,independent_review=peer,findings=[]))
    partition=s.put({'synthetic_partition_descriptor':True})
    transition=dict(schema=lineage.AFTER_DERIVED,kind=lineage.KIND,owner_partition=partition,
        predecessor_plan=prior_ref,previous_lineage=prior['native_source_migration'],
        previous_source=prior['sources']['native_queue'],
        previous_runtime_completed_actor_source=prior['sources']['completed_actor'],
        source=sources['native_queue'],completed_actor_source=sources['completed_actor'],
        mixed_owner_source=sources['mixed_owner'],lineage_source=sources['source_lineage'],
        driver_source=sources['driver'],binder_source=binder,source_review=peer,root_source_adoption=adoption)
    change('transition',transition)
    # The independent mixed-owner tests authenticate complete closure/partition
    # graphs. This unit isolates source tracing using that explicit API boundary.
    def resolved(reference, *, predecessor, source, read):
        assert reference==partition and predecessor==head and source==sources['mixed_owner']
        return dict(derived_adoption=bank,original_base=old['predecessor'],links=links,required_pins=[])
    helper=types.SimpleNamespace(_reader=mixed._reader,resolve=resolved,MixedOwnerError=mixed.MixedOwnerError)
    return s,dict(reference=s.put(transition),predecessor=head,sources=sources,read=s.read,mixed_owner=helper),original


class AfterDerivedLineageTests(unittest.TestCase):
    def test_first_adopted_invocation_keeps_true_predecessor(self):
        s,args,origin=transition_fixture();before=copy.deepcopy(s.data)
        result=lineage.validate_after_derived(**args)
        self.assertEqual(result['original_migration'],origin)
        self.assertEqual(result['predecessor'],args['predecessor'])
        self.assertEqual(result['derived_invocations'],1)
        self.assertEqual(before,s.data)

    def test_same_adoption_suffix_keeps_each_driver_and_first_lineage(self):
        s,args,origin=transition_fixture(resumed=True)
        result=lineage.validate_after_derived(**args)
        self.assertEqual(result['derived_invocations'],2)
        immediate=s.value(args['predecessor']['actual'])['plan']
        self.assertNotEqual(result['first_derived_plan'],immediate)
        self.assertEqual(result['predecessor'],args['predecessor'])
        self.assertEqual(result['original_migration'],origin)
        for ref in (immediate,result['first_derived_plan']):
            self.assertIn(s.value(ref)['sources']['driver'],result['required_pins'])

    def test_changed_retained_native_helper_refused(self):
        def change(name,value):
            if name=='resume':value['sources']['completed_actor']=value['sources']['driver']
        _,args,_=transition_fixture(resumed=True,change=change)
        with self.assertRaisesRegex(lineage.SourceLineageError,'continuation_sources'):
            lineage.validate_after_derived(**args)

    def test_previous_plan_substitution_refused(self):
        s,args,_=transition_fixture(resumed=True)
        value=s.value(args['reference']);value['predecessor_plan']=s.put({'earlier':True})
        args['reference']=s.put(value)
        with self.assertRaisesRegex(lineage.SourceLineageError,'exact_endpoint'):
            lineage.validate_after_derived(**args)

    def test_source_adoption_or_bytes_change_refused(self):
        s,args,_=transition_fixture();value=s.value(args['reference'])
        peer=s.value(value['source_review']);peer['findings']=['unresolved']
        value['source_review']=s.put(peer);args['reference']=s.put(value)
        with self.assertRaisesRegex(lineage.SourceLineageError,'source_acceptance'):
            lineage.validate_after_derived(**args)
        s,args,_=transition_fixture();s.data[args['sources']['driver']['path']]=b'changed'
        with self.assertRaisesRegex(lineage.SourceLineageError,'source_bytes'):
            lineage.validate_after_derived(**args)


def complement_fixture():
    m=Memory();roles={'mi':'mi','ny_saved_only':'ny','texas':'texas'}
    refs={role:m.put({'original_unstarted':role}) for role in roles.values()}
    pool=[]
    for role in roles:
        for ordinal in (1,3):
            pool.append(dict(role=role,ordinal=ordinal,measurement_id=role+str(ordinal),
                run_id='run_'+role+str(ordinal),question_id='strong',seed=ordinal,
                request_body_sha256='a'*64,input_sha256='b'*64))
    def selected(row):
        return dict(original_scope=roles[row['role']],original_ordinal=row['ordinal'],
            unstarted_ref=refs[roles[row['role']]],**{k:row[k] for k in
                ('measurement_id','run_id','question_id','seed','request_body_sha256','input_sha256')})
    excluded=dict(schema='causes.saved_unstarted_priority_subset.v1',runtime_authorized=False,
        assignments=m.put({'assignment':'frozen'}),selection=m.put({'selection':'frozen'}),
        rows=[selected(pool[0]),selected(pool[4])])
    rows=[selected(row) for index,row in enumerate(pool) if index not in (0,4)]
    ids=[row['measurement_id'] for row in rows]
    metadata=dict(schema='causes.saved_unstarted_complement_subset.v1',runtime_authorized=False,
        pool=m.put(pool),excluded=m.put(excluded),assignments=excluded['assignments'],
        selection=excluded['selection'],source_unstarted=refs,pool_role_to_original_scope=roles,
        rows=rows,pool_count=6,excluded_count=2,selected_count=4,
        selected_sorted_ids_sha256=mixed.digest(sorted(ids)),selected_ordered_ids_sha256=mixed.digest(ids))
    return m,metadata


class ComplementTests(unittest.TestCase):
    def test_exact_pool_complement_order(self):
        m,meta=complement_fixture();before=copy.deepcopy(m.files)
        self.assertEqual(mixed.validate_complement(meta,value=m.get),meta['rows'])
        self.assertEqual([r['measurement_id'] for r in meta['rows']],['mi3','ny_saved_only1','ny_saved_only3','texas3'])
        self.assertEqual(before,m.files)

    def test_reordered_or_changed_request_refused(self):
        for key in ('order','seed','input_sha256','original_ordinal'):
            m,meta=complement_fixture()
            if key=='order':meta['rows'].reverse()
            else:meta['rows'][0][key]='different'
            with self.assertRaises(mixed.MixedOwnerError):mixed.validate_complement(meta,value=m.get)


class MemoryTransfer(TransferFixture):
    def __init__(self):
        self.files={}
        super().__init__(Path('/synthetic-transfer'),historical=True)

    def save(self,value):
        path=str(self.root/(str(self.n)+'.json'));self.n+=1
        raw=core.encoded(value);self.files[path]=raw
        return dict(path=path,bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())

    def resolve(self):
        return direct.original_transfer_jobs(self.plan,self.previous,read=lambda ref:self.files[ref['path']])


def transfer_fixture():
    f=MemoryTransfer();m,partition,head,helper=mixed_fixture()
    f.files.update(m.files);f.previous=m.get(head['actual']);f.owner=head
    f.plan.update(predecessor_custody=head,initial_failures=f.previous['consecutive_failures'],
        mixed_owner_partition=partition,sources={'mixed_owner':helper})
    f.manifest.update(execution_predecessor=head,mixed_owner_partition=partition)
    rolelabels={'mi':'mi','ny':'ny_saved_only','texas':'texas'}
    pool=[]
    for row in f.priority_rows:
        pool.append(dict(role=rolelabels[row['original_scope']],ordinal=row['original_ordinal'],
            **{k:row[k] for k in ('measurement_id','run_id','question_id','seed','request_body_sha256','input_sha256')}))
    excluded={**f.metadata,'rows':f.priority_rows[::2]}
    rows=f.priority_rows[1::2];ids=[row['measurement_id'] for row in rows]
    counts={role:f.actuals[role]['counts']['technical_failures'] for role in f.actuals}
    metadata=dict(schema='causes.saved_unstarted_complement_subset.v1',runtime_authorized=False,
        pool=f.save(pool),excluded=f.save(excluded),assignments=excluded['assignments'],selection=excluded['selection'],
        source_unstarted={role:f.actuals[role]['unstarted'] for role in f.actuals},
        pool_role_to_original_scope={v:k for k,v in rolelabels.items()},rows=rows,
        pool_count=6,excluded_count=3,selected_count=3,selected_sorted_ids_sha256=core.digest(sorted(ids)),
        selected_ordered_ids_sha256=core.digest(ids),historical_terminal_failures=counts)
    f.selected=[f.bank_jobs[role][2] for role in ('mi','ny','texas')]
    f.plan['jobs_sha256']=core.digest(f.selected)
    f.manifest.update(selection={'mode':'saved_complement','metadata':f.save(metadata)},
        job_count=3,jobs_sha256=core.digest(f.selected),donor_terminal_failures=counts)
    f.bind();return f


class MixedTransferTests(unittest.TestCase):
    def test_complete_mixed_owner_exact_complement_and_donor_locks(self):
        f=transfer_fixture();before=copy.deepcopy(f.files);result=f.resolve()
        self.assertEqual(result['jobs'],f.selected)
        self.assertEqual(len(result['donor_occupied_ids']),3)
        self.assertEqual(len(result['donor_locks']),3)
        self.assertEqual(result['donor_counters'],{'mi':0,'ny':0,'texas':0})
        self.assertEqual(f.files,before)

    def test_donor_failure_history_and_partition_cannot_change(self):
        f=transfer_fixture();f.manifest['donor_terminal_failures']={**f.manifest['donor_terminal_failures'],'mi':0};f.bind()
        with self.assertRaisesRegex(core.IntegrityError,'failure'):f.resolve()
        f=transfer_fixture();f.manifest['mixed_owner_partition']=None;f.bind()
        with self.assertRaisesRegex(core.IntegrityError,'mixed_partition'):f.resolve()

    def test_mixed_owner_requires_opt_in_and_complete_current_counter(self):
        f=transfer_fixture();del f.plan['mixed_owner_partition']
        with self.assertRaises(core.IntegrityError):f.resolve()
        f=transfer_fixture();f.plan['initial_failures']+=1
        with self.assertRaisesRegex(core.IntegrityError,'predecessor_not_complete'):f.resolve()

    def test_attempted_donor_id_cannot_enter_selected_complement(self):
        f=transfer_fixture();metadata=json.loads(f.files[f.manifest['selection']['metadata']['path']])
        metadata['rows'][0]['measurement_id']=f.bank_jobs['mi'][0]['measurement_id']
        f.manifest['selection']['metadata']=f.save(metadata);f.bind()
        with self.assertRaises(core.IntegrityError):f.resolve()

    def test_priority_identity_and_role_alias_refused(self):
        m,meta=complement_fixture();excluded=m.get(meta['excluded'])
        excluded['rows'][0]['request_body_sha256']='c'*64;meta['excluded']=m.put(excluded)
        with self.assertRaisesRegex(mixed.MixedOwnerError,'excluded_identity'):
            mixed.validate_complement(meta,value=m.get)
        m,meta=complement_fixture();meta['pool_role_to_original_scope']['ny_saved_only']='mi'
        with self.assertRaisesRegex(mixed.MixedOwnerError,'saved_role_labels'):
            mixed.validate_complement(meta,value=m.get)

    def test_excluded_outside_pool_and_duplicate_refused(self):
        for mode in ('outside','duplicate'):
            m,meta=complement_fixture();excluded=m.get(meta['excluded'])
            if mode=='outside':excluded['rows'][0]['measurement_id']='unknown'
            else:excluded['rows'].append(excluded['rows'][0])
            meta['excluded']=m.put(excluded)
            with self.assertRaises(mixed.MixedOwnerError):mixed.validate_complement(meta,value=m.get)


if __name__=='__main__':unittest.main()
