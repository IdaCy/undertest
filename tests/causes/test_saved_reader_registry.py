"""Synthetic accepted-index fixtures; no actual outcomes or native parsers."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import saved_reader_registry as saved


class Fixture:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.number = 0
        self.reader_source = self.raw(b'synthetic native source', 'reader.py')
        self.producer = self.raw(b'synthetic readout source', 'readout.py')
        self.config = dict(model='example-reader', max_retries=0)
        self.protocol = dict(schema='causes.derived_reader_protocol.v1',
            checker_config=self.put(self.config), reader_source=self.reader_source,
            prompts={name: self.raw(name.encode(), name+'.txt') for name in ('rubric', *saved.THRESHOLDS)},
            prompts_directory=str(self.directory))
        self.protocols = {name: dict(schema='causes.saved_reader_instrument_protocol.v1',
            reader_source_sha256=self.reader_source['sha256'], config_sha256=saved.digest(self.config),
            rubric_sha256=self.protocol['prompts']['rubric']['sha256'],
            threshold_sha256=self.protocol['prompts'][name]['sha256'], schema_sha256=saved.digest(name),
            instrument=name, phase='prefix', awareness_type='evaluation') for name in saved.THRESHOLDS}

    def raw(self, raw, suffix='json'):
        self.number += 1
        path = self.directory / (str(self.number)+'_'+suffix)
        path.write_bytes(raw)
        return dict(path=str(path), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())

    def put(self, value):
        return self.raw(saved.encoded(value)+b'\n')

    def get(self, reference):
        return json.loads(Path(reference['path']).read_bytes())

    def row(self, run, *, unavailable=False, view='neutral'):
        result = dict(run_id=run, stage='early', view_kind=view,
                      measurement_key='derived_reader_'+saved.digest([run, view]),
                      disposition='unavailable' if unavailable else 'prepared',
                      protocol_sha256=saved.digest(self.protocols))
        if unavailable:
            result.update(view=None, payloads={}, reason='frozen unavailable context')
        else:
            result.update(view={'saved': 'synthetic'}, view_canonical_sha256=saved.digest(['view', run, view]),
                context_canonical_sha256=saved.digest(['prepared-context', run, view]),
                targets_sha256=saved.digest(['targets', run]), payloads={})
            for name in saved.THRESHOLDS:
                result['payloads'][name] = dict(canonical_sha256=saved.digest([run, view, name]),
                    body_bytes=100, file_sha256=saved.digest(['request bytes', run, view, name]),
                    pins={k: self.protocols[name][k] for k in
                        ('config_sha256', 'rubric_sha256', 'threshold_sha256', 'schema_sha256')})
        return result

    def prep(self, rows, pairs=None):
        ready = [r for r in rows if r['disposition'] == 'prepared']
        unavailable = [r for r in rows if r['disposition'] == 'unavailable']
        value = dict(schema='causes.frozen_derived_reader_preparation.v1', source=self.producer,
            protocol=self.protocol, assignments=self.put([{k:r[k] for k in saved.IDENTITY} for r in rows]),
            ready=self.put(ready), unavailable=self.put(unavailable), pairs=self.put(pairs or []),
            counts=dict(assigned=len(rows), ready=len(ready), unavailable=len(unavailable), requests=3*len(ready)))
        reference = self.put(value)
        peer = self.put(dict(accepted_frozen_derived_reader_inputs=True, findings=[],
                             preparation=reference, source=self.producer))
        return reference, peer, value

    def closure(self, prep, peer, rows, *, root='capture', union=False):
        ready = [r for r in rows if r['disposition'] == 'prepared']
        root = str(self.directory/root)
        actual = dict(schema='causes.finite_reader_route_closed_cut.v1', prepared=prep,
            root=root, plan=self.put({'saved_plan': True}), journal=self.put({'saved_journal': True}),
            all_claims_accounted=True, active=0, unclaimed=0, ready=len(ready), claimed=len(ready),
            finished_or_unresolved=len(ready), claimed_keys=[r['measurement_key'] for r in ready])
        if union:
            actual = dict(schema='causes.derived_reader_closed_union.v1', inputs=prep, inputs_review=peer,
                source=self.producer, binding=self.put({}), binding_review=self.put({}),
                continuation_actual=self.put({}), continuation_review=self.put({}),
                context_locations=[dict(key=r['measurement_key'], root=root) for r in ready],
                original_unresolved_key=ready[-1]['measurement_key'],
                original_missing_instruments=['strongly_favors'], qualification={'preserved': 'citation failure'})
        reference = self.put(actual)
        if union:
            review = dict(accepted_derived_reader_closed_union=True, findings=[], union=reference,
                **{k:actual[k] for k in ('source', 'binding', 'binding_review', 'continuation_actual', 'continuation_review')})
        else:
            review = dict(accepted_derived_reader_batch_closed_cut=True,
                accepted_finite_reader_route_closed_cut=True, cohort_kind='derived', findings=[], actual=reference,
                **{k:actual[k] for k in ('prepared', 'root', 'plan', 'journal')})
        return dict(preparation=prep, input_review=peer, closed=reference, closed_review=self.put(review)), root

    def build(self, *, missing=False, union=False, labels=None, partial=False, second=False, alias_protocol=False):
        baseline = self.row('run-baseline', unavailable=missing)
        treatment = self.row('run-treatment')
        descriptors, comparisons = [], []
        first_custody = None
        for number in range(2 if second else 1):
            if number and alias_protocol:
                self.protocol = copy.deepcopy(self.protocol)
                self.protocol['checker_config'] = self.put(self.config)
                self.protocol['reader_source'] = self.raw(b'synthetic native source', 'reader-copy.py')
            variant = 'F'+str(number)
            current = treatment if number == 0 else self.row('run-second', view='external-neutral')
            rows = [baseline, current]
            pair = dict(task_id='repository-1', draw_key=0, variant=variant, arms={
                arm:dict(run_id=r['run_id'], measurement_key=r['measurement_key'], disposition=r['disposition'])
                for arm,r in zip(('baseline','treatment'), rows)})
            pr, ip, prep = self.prep(rows, [pair])
            spec = dict(schema='causes.frozen_derived_reader_readout_specification.v1',
                awareness_type='evaluation', thresholds=list(saved.THRESHOLDS), inference_released=False,
                measurement_type='independent_verbalized_awareness_reader',
                **{k:prep[k] for k in ('ready','unavailable','pairs')},
                counts=dict(assigned_arms=2, ready_arms=1 if missing else 2, unavailable_arms=int(missing), pairs=1, tasks=1))
            sr = self.put(spec)
            sp = self.put(dict(accepted_frozen_derived_reader_readout_specification=True,
                              findings=[], specification=sr, preparation=pr))
            original, root = self.closure(pr, ip, rows, union=union)
            if number == 0:
                first_custody = original
            out = dict(schema='causes.derived_reader_readout.v1', source=self.producer,
                source_review=self.put({'accepted_source': True}),
                modules={'campaign_reader':self.reader_source}, specification=sr, specification_review=sp,
                preparation=pr, input_review=ip, counts=spec['counts'], inference_released=False,
                direct_self_report_included=False, rows=[])
            if union or number:
                if number:
                    npr, nip, _ = self.prep([current]); new, _ = self.closure(npr,nip,[current])
                    sources = {'reused':first_custody, 'new':new}
                    choice = {baseline['measurement_key']:'reused', current['measurement_key']:'new'}
                else:
                    sources = {'union':original}; choice={r['measurement_key']:'union' for r in rows}
                mapped = dict(schema='causes.derived_reader_composite_mapping.v1', preparation=pr,
                    specification=sr, sources=sources, rows=[dict(measurement_key=r['measurement_key'],
                        source=choice[r['measurement_key']]) for r in rows if r['disposition']=='prepared'])
                mp = self.put(mapped)
                out.update(schema='causes.derived_reader_composite_readout.v1', sources=sources, mapping=mp,
                    mapping_review=self.put(dict(accepted_derived_reader_composite_mapping=True, findings=[], mapping=mp)))
            else:
                out.update(closed=original['closed'], closed_review=original['closed_review'],
                           journal=self.get(original['closed'])['journal'])
            invalid = []
            for r in rows:
                absent = r['disposition']=='unavailable'
                item = dict(measurement_key=r['measurement_key'], disposition=r['disposition'],
                            state='unavailable' if absent else 'finished', instruments={})
                if absent:
                    item['reason']=r['reason']
                else:
                    item.update(result=dict(path=str(Path(root)/r['measurement_key']/'RESULT.json'), bytes=10,
                        sha256=saved.digest(['result',r['run_id']])),
                        evidence=dict(files=24,bytes=1000,relative_file_manifest_sha256=saved.digest(r['run_id'])))
                if union or number:
                    item['source']=None if absent else choice[r['measurement_key']]
                for i,name in enumerate(saved.THRESHOLDS):
                    label=(labels or ['no']*3)[i]
                    status='validated_partial_observation' if partial else 'validated'
                    if absent:label,status=None,'unavailable'
                    if union and r is current and name=='strongly_favors':
                        label,status=None,'technical_missing'
                        item.update(state='unresolved_current_reader_attempt', qualification={'preserved':'citation failure'})
                        invalid.append(dict(key=r['measurement_key'],instrument=name,
                            error='evidence_not_exact_channel_substring:evaluation',
                            result=dict(path=str(Path(root)/r['measurement_key']/name/'result.json'),
                                        bytes=8,sha256=saved.digest('failure'))))
                    item['instruments'][name]=dict(status=status,label=label)
                out['rows'].append(item)
            readout = self.put(out)
            review = self.put(dict(accepted_saved_derived_reader_readout=True,findings=[],result=readout,
                invalid_records=invalid,**{k:v for k,v in out.items() if k not in
                    ('schema','rows','inference_released','direct_self_report_included')}))
            descriptors.append(dict(variant=variant,readout=readout,review=review))
            member = dict(task_id='repository-1',draw_key=0,disposition='included',arms={
                arm:dict(original_run_id=r['run_id'],reader_key=r['measurement_key'],direct_key='not-consumed',
                         neutral_view=r['view_kind'],input_disposition=r['disposition'])
                for arm,r in zip(('baseline','treatment'), rows)})
            comparisons.append(dict(variant=variant,stage='early',reader_specification=sr,reader_pairs=prep['pairs'],
                fixed_pairs=2,included_pairs=1,members=[member,dict(task_id='unsupported-task',draw_key=0,
                    baseline_run_id='unseen-base',treatment_run_id='unseen-treatment',disposition='unsupported',
                    reason='external proof unavailable')],full_status={'retained':'synthetic full status'}))
        membership=self.put(dict(schema='causes.frozen_neutral_matched_member_mapping.v1',comparisons=comparisons))
        mp=self.put(dict(accepted_frozen_neutral_matched_membership=True,findings=[],actual=membership))
        return dict(descriptors=descriptors,membership=membership,membership_review=mp,protocols=copy.deepcopy(self.protocols))

    def change_output(self, args, change, index=0):
        descriptor=args['descriptors'][index];out=self.get(descriptor['readout']);change(out)
        descriptor['readout']=self.put(out);peer=self.get(descriptor['review']);peer['result']=descriptor['readout']
        for k in out:
            if k in peer:peer[k]=out[k]
        descriptor['review']=self.put(peer)


class SavedReaderRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.f=Fixture(self.temp.name)

    def run_fixture(self, **options):
        args=self.f.build(**options)
        return args,saved.resolve_saved_readers(**args)

    def test_simple_observed_and_unsupported_preserved(self):
        args,result=self.run_fixture()
        self.assertEqual(len(result['measurements']),6)
        self.assertEqual(result['comparisons'],self.f.get(args['membership'])['comparisons'])
        self.assertTrue(all(r['status']=='observed' for r in result['measurements']))
        self.assertEqual(result['parser_replays'],0)

    def test_all_native_labels_are_literal(self):
        for labels in [list(saved.READER_LABELS[:3]),list(saved.READER_LABELS[2:])]:
            with self.subTest(labels=labels):
                _,result=self.run_fixture(labels=labels)
                self.assertEqual([x['label'] for x in result['measurements'][:3]],labels)

    def test_unavailable_has_no_context_or_request(self):
        _,result=self.run_fixture(missing=True)
        for row in result['measurements'][:3]:
            self.assertEqual(row['status'],'unavailable');self.assertIsNone(row['context_sha256'])
            self.assertIsNone(result['origins'][row['id']]['physical']['request'])

    def test_unavailable_cannot_claim_a_native_failure(self):
        args=self.f.build(missing=True);descriptor=args['descriptors'][0]
        out=self.f.get(descriptor['readout']);peer=self.f.get(descriptor['review'])
        peer['invalid_records']=[dict(key=out['rows'][0]['measurement_key'],instrument='presence',
            error='purported native failure',result=dict(path=str(self.f.directory/'unrelated-result.json'),
                bytes=8,sha256=saved.digest('not an attempted request')))]
        descriptor['review']=self.f.put(peer)
        with self.assertRaisesRegex(ValueError,'invalid_annotation_record'):
            saved.resolve_saved_readers(**args)

    def test_qualified_union_keeps_other_instruments_and_failure(self):
        _,result=self.run_fixture(union=True)
        self.assertEqual([x['status'] for x in result['measurements'][3:]],
                         ['observed','technical_missing','observed'])
        self.assertIn('evidence_not_exact',result['measurements'][4]['reason'])
        self.assertEqual(result['origins'][result['measurements'][4]['id']]['aliases'][0]['native_state'],
                         'unresolved_current_reader_attempt')

    def test_partial_observation_null_is_missing(self):
        _,result=self.run_fixture(labels=[None,'yes',None],partial=True)
        self.assertEqual([x['status'] for x in result['measurements'][:3]],
                         ['technical_missing','observed','technical_missing'])
        self.assertEqual(result['measurements'][0]['reason'],'technical_missing_reasoning')

    def test_shared_baseline_is_one_physical_measurement(self):
        _,result=self.run_fixture(second=True)
        self.assertEqual(len(result['measurements']),9)
        self.assertEqual(len(result['assignments']),4)
        self.assertEqual(result['assignments'][0]['instruments'],result['assignments'][2]['instruments'])
        self.assertEqual(len(result['origins'][result['measurements'][0]['id']]['aliases']),2)

    def test_identical_protocol_bytes_at_distinct_pinned_paths(self):
        _,result=self.run_fixture(second=True,alias_protocol=True)
        self.assertEqual(len(result['measurements']),9)
        self.assertEqual(result['assignments'][0]['instruments'],result['assignments'][2]['instruments'])

    def test_mutated_readout_pin_refused(self):
        args=self.f.build();p=Path(args['descriptors'][0]['readout']['path']);p.write_bytes(p.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'stored_bytes_changed'):saved.resolve_saved_readers(**args)

    def test_unaccepted_peer_refused(self):
        args=self.f.build();d=args['descriptors'][0];p=self.f.get(d['review']);p['accepted_saved_derived_reader_readout']=False
        d['review']=self.f.put(p)
        with self.assertRaisesRegex(ValueError,'accepted_peer'):saved.resolve_saved_readers(**args)

    def test_row_order_refused_even_with_matching_peer(self):
        args=self.f.build();self.f.change_output(args,lambda o:o['rows'].reverse())
        with self.assertRaisesRegex(ValueError,'saved_row_order'):saved.resolve_saved_readers(**args)

    def test_observed_unavailable_refused(self):
        args=self.f.build(missing=True)
        self.f.change_output(args,lambda o:o['rows'][0]['instruments']['presence'].update(label='no'))
        with self.assertRaisesRegex(ValueError,'unavailable_label'):saved.resolve_saved_readers(**args)

    def test_native_label_and_status_mismatch_refused(self):
        args=self.f.build();self.f.change_output(args,lambda o:o['rows'][0]['instruments']['presence'].update(status='technical_missing'))
        with self.assertRaisesRegex(ValueError,'observed_label'):saved.resolve_saved_readers(**args)

    def test_union_cannot_be_relabeled_clean(self):
        args=self.f.build(union=True);self.f.change_output(args,lambda o:o['rows'][1].update(state='finished'))
        with self.assertRaisesRegex(ValueError,'qualified_terminal_state'):saved.resolve_saved_readers(**args)

    def test_shared_physical_label_conflict_refused(self):
        args=self.f.build(second=True)
        self.f.change_output(args,lambda o:o['rows'][0]['instruments']['presence'].update(label='yes'),index=1)
        with self.assertRaisesRegex(ValueError,'shared_measurement_conflict'):saved.resolve_saved_readers(**args)

    def test_wrong_capture_root_refused(self):
        args=self.f.build();self.f.change_output(args,lambda o:o['rows'][0]['result'].update(path='/wrong/RESULT.json'))
        with self.assertRaisesRegex(ValueError,'physical_result'):saved.resolve_saved_readers(**args)

    def test_semantic_protocol_change_refused(self):
        args=self.f.build();args['protocols']['presence']['threshold_sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'protocol_identity'):saved.resolve_saved_readers(**args)

    def test_other_awareness_type_cannot_be_relabeled_evaluation(self):
        args=self.f.build();d=args['descriptors'][0];out=self.f.get(d['readout'])
        spec=self.f.get(out['specification']);spec['awareness_type']='capability'
        out['specification']=self.f.put(spec)
        out['specification_review']=self.f.put(dict(accepted_frozen_derived_reader_readout_specification=True,
            findings=[],specification=out['specification'],preparation=out['preparation']))
        self.f.change_output(args,lambda value:(value.clear(),value.update(out)))
        with self.assertRaisesRegex(ValueError,'reader_instrument_scope'):saved.resolve_saved_readers(**args)

    def test_schema_protocol_change_refused(self):
        args=self.f.build();args['protocols']['presence']['schema_sha256']='0'*64
        with self.assertRaisesRegex(ValueError,'payload_protocol'):saved.resolve_saved_readers(**args)

    def test_stage_and_view_mapping_changes_refused(self):
        for key,value in [('neutral_view','another-view'),('original_run_id','another-run')]:
            args=self.f.build();m=self.f.get(args['membership']);m['comparisons'][0]['members'][0]['arms']['baseline'][key]=value
            args['membership']=self.f.put(m);args['membership_review']=self.f.put(dict(
                accepted_frozen_neutral_matched_membership=True,findings=[],actual=args['membership']))
            with self.assertRaisesRegex(ValueError,'arm_identity'):saved.resolve_saved_readers(**args)

    def test_source_context_domain_conflict_refused(self):
        args=self.f.build(second=True);d=args['descriptors'][1];out=self.f.get(d['readout']);prep=self.f.get(out['preparation'])
        ready=self.f.get(prep['ready']);ready[0]['context_canonical_sha256']=saved.digest('message-array-not-prepared-context')
        prep['ready']=self.f.put(ready);prep_ref=self.f.put(prep)
        out['preparation']=prep_ref
        out['input_review']=self.f.put(dict(accepted_frozen_derived_reader_inputs=True,findings=[],preparation=prep_ref,source=prep['source']))
        # The original authenticated source row remains unchanged; the global join must refuse.
        spec=self.f.get(out['specification']);spec['ready']=prep['ready'];out['specification']=self.f.put(spec)
        out['specification_review']=self.f.put(dict(accepted_frozen_derived_reader_readout_specification=True,
            findings=[],specification=out['specification'],preparation=prep_ref))
        mapping=self.f.get(out['mapping']);mapping.update(preparation=prep_ref,specification=out['specification'])
        out['mapping']=self.f.put(mapping);out['mapping_review']=self.f.put(dict(accepted_derived_reader_composite_mapping=True,findings=[],mapping=out['mapping']))
        m=self.f.get(args['membership']);m['comparisons'][1]['reader_specification']=out['specification']
        args['membership']=self.f.put(m);args['membership_review']=self.f.put(dict(accepted_frozen_neutral_matched_membership=True,findings=[],actual=args['membership']))
        self.f.change_output(args,lambda value:(value.clear(),value.update(out)),index=1)
        with self.assertRaisesRegex(ValueError,'physical_input_identity'):saved.resolve_saved_readers(**args)

    def test_document_and_total_bounds_precede_body_read(self):
        args=self.f.build()
        with patch.object(saved,'read_pinned',side_effect=AssertionError('read must not occur')):
            with self.assertRaisesRegex(ValueError,'document_bound'):
                saved.resolve_saved_readers(**args,maximum_document_bytes=1)
            with self.assertRaisesRegex(ValueError,'total_input_bound'):
                saved.resolve_saved_readers(**args,maximum_total_bytes=1)

    def test_result_bound(self):
        args=self.f.build()
        with self.assertRaisesRegex(ValueError,'result_bound'):saved.resolve_saved_readers(**args,maximum_result_bytes=1)

    def test_float_overflow_in_unused_metadata_refused_and_finite_exponent_allowed(self):
        for literal in ('1e999','-1e999','1e100'):
            args=self.f.build();value=self.f.get(args['membership'])
            raw=saved.encoded(value)[:-1]+b', "unused_number": '+literal.encode()+b'}'
            args['membership']=self.f.raw(raw)
            args['membership_review']=self.f.put(dict(accepted_frozen_neutral_matched_membership=True,
                findings=[],actual=args['membership']))
            if literal=='1e100':
                self.assertEqual(len(saved.resolve_saved_readers(**args)['measurements']),6)
            else:
                with self.assertRaisesRegex(ValueError,'nonfinite_json_float'):saved.resolve_saved_readers(**args)

    def test_postread_mutation_refused(self):
        args=self.f.build();target=args['membership'];original=saved.read_pinned;seen=0
        def reader(ref,**kwargs):
            nonlocal seen
            raw=original(ref,**kwargs)
            if ref==target:
                seen+=1
                if seen==1:Path(ref['path']).write_bytes(raw+b' ')
            return raw
        with patch.object(saved,'read_pinned',side_effect=reader):
            with self.assertRaisesRegex(ValueError,'stored_bytes_changed'):saved.resolve_saved_readers(**args)

    def test_only_declared_metadata_files_are_read(self):
        args=self.f.build(union=True);original=saved.read_pinned;paths=[]
        def reader(ref,**kwargs):paths.append(ref['path']);return original(ref,**kwargs)
        with patch.object(saved,'read_pinned',side_effect=reader):result=saved.resolve_saved_readers(**args)
        self.assertEqual(set(paths),{x['path'] for x in result['input_references']})
        self.assertFalse(any('/capture/' in p for p in paths))
        self.assertTrue(all(Path(p).parent==self.f.directory for p in paths))


if __name__ == '__main__':
    unittest.main()
