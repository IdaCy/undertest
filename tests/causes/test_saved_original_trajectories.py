import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import saved_original_trajectories as subset


class Fixture:
    def __init__(self, root):
        self.root = Path(root)
        self.paths = {}
        self.fixed = [dict(run_id=run, task_id='task', variant=variant,
                           repetition=0, seed=i+10, terminal=run != 'unissued')
                      for i, (run, variant) in enumerate([
                          ('baseline', 'B00L00'), ('treatment', 'F07L05'),
                          ('unselected', 'F05L05'), ('unissued', 'F01L05')])]
        self.native = []
        for expected in self.fixed[:3]:
            assigned = expected['run_id'] != 'treatment'
            self.native.append(dict(run_id=expected['run_id'], model='mi27',
                task_id=expected['task_id'], variant_id=expected['variant'],
                allocation=dict(run_id=expected['run_id'], canonical_variant=expected['variant'],
                    repetition=0, seed=expected['seed'],
                    instruments=['evaluation_'+x for x in subset.THRESHOLDS] if assigned else ['evaluation_presence'],
                    reader_stages=['early'] if assigned else [],
                    reader_thresholds=list(subset.THRESHOLDS)),
                source='/saved/snapshot/'+expected['run_id'], summary_sha256='a'*64,
                context_sources={'early': {'context_sha256': 'b'*64}},
                self={'early': {'evaluation_presence': {'label': None,
                    'technical_status': 'invalid_answer_schema', 'reason': 'literal failure'}}},
                reader={}, reader_dispositions={'early': 'final_reader_technical_missing'} if assigned else {},
                extra={'native_failure_counter': 2, 'literal': 'keep \u2603 and \\n'}))
        self.mapping = dict(comparisons=[dict(stage='early', baseline='B00L00', variant='F07L05', members=[dict(
            task_id='task', draw_key=0, disposition='included',
            arms=dict(baseline={'original_run_id': 'baseline'}, treatment={'original_run_id': 'treatment'}))])])
        self.inputs = self.build()
        self.limits = dict(metadata_file_bytes=1024**2, metadata_total_bytes=8*1024**2,
                           payload_bytes=1024**2, output_bytes=1024**2)

    def put(self, name, value, *, raw=False):
        path = self.root / name
        body = value if raw else subset.encoded(value) + b'\n'
        path.write_bytes(body)
        self.paths[name] = path
        return dict(path=str(path), bytes=len(body), sha256=hashlib.sha256(body).hexdigest())

    def load(self, ref):
        return json.loads(Path(ref['path']).read_bytes())

    def build(self):
        inputs = dict(fixed_metadata=self.put('fixed.json', self.fixed),
            descriptive_inputs=self.put('descriptive.json', dict(model='mi27',
                rosters={'threshold_early': ['baseline', 'unselected']})),
            reader_freeze=self.put('freeze.json', dict(model='mi27',
                slots=[dict(run_id=run, stage='early') for run in ['baseline', 'unselected']])),
            mapping=self.put('mapping.json', self.mapping))
        inputs['mapping_review'] = self.put('mapping_peer.json', dict(
            accepted_frozen_neutral_matched_membership=True, findings=[],
            mapping=inputs['mapping'], actual=inputs['mapping']))
        inputs['mapping_adoption'] = self.put('mapping_adoption.json', dict(accepted=True, findings=[],
            mapping=inputs['mapping'], independent_review=inputs['mapping_review']))
        source = self.put('assembly_source.py', b'# frozen native producer\n', raw=True)
        binding = self.put('binding.json', dict(model='mi27', expected_record_count=3,
            descriptive_inputs=inputs['descriptive_inputs'], freeze_binding=inputs['reader_freeze']))
        payload = self.put('trajectories.jsonl', b''.join(subset.encoded(x)+b'\n' for x in self.native), raw=True)
        inputs['assembly_result'] = self.put('result.json', dict(source=source, binding=binding, records=3, outputs=[payload]))
        inputs['assembly_review'] = self.put('assembly_peer.json', dict(
            accepted_mi_final_input_assembly=True, findings=[], result=inputs['assembly_result'],
            source=source, binding=binding, trajectories=payload, rows=3))
        return inputs

    def update_payload(self, raw=None):
        payload = self.put('trajectories.jsonl', raw if raw is not None else
            b''.join(subset.encoded(x)+b'\n' for x in self.native), raw=True)
        result = self.load(self.inputs['assembly_result']); result['outputs'] = [payload]
        self.inputs['assembly_result'] = self.put('result.json', result)
        peer = self.load(self.inputs['assembly_review']);peer['result'] = self.inputs['assembly_result'];peer['trajectories'] = payload
        self.inputs['assembly_review'] = self.put('assembly_peer.json', peer)

    def update_mapping(self):
        self.inputs['mapping'] = self.put('mapping.json', self.mapping)
        peer = self.load(self.inputs['mapping_review']);peer['mapping'] = peer['actual'] = self.inputs['mapping']
        self.inputs['mapping_review'] = self.put('mapping_peer.json', peer)
        adoption = self.load(self.inputs['mapping_adoption']);adoption['mapping'] = self.inputs['mapping'];adoption['independent_review'] = self.inputs['mapping_review']
        self.inputs['mapping_adoption'] = self.put('mapping_adoption.json', adoption)


class OriginalTrajectorySubsetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.f = Fixture(self.temporary.name)

    def select(self, **changes):
        return subset.select_fixed_trajectories(inputs=changes.get('inputs', self.f.inputs),
                                                limits=changes.get('limits', self.f.limits))

    def test_native_objects_and_exact_line_provenance(self):
        before = {p.name:p.read_bytes() for p in self.f.root.iterdir()}
        value = self.select()
        self.assertEqual(value['target_runs'], ['baseline', 'treatment'])
        self.assertEqual([x['native'] for x in value['selected']], self.f.native[:2])
        self.assertEqual(value['complete_records_checked'], 3)
        self.assertEqual(value['assignment_counts'], {'assigned': 7, 'not_assigned': 5})
        self.assertEqual(value['payload_reads'], 1)
        payload = before['trajectories.jsonl']
        for i, selected in enumerate(value['selected']):
            provenance = selected['provenance'];line = payload[provenance['byte_offset']:provenance['byte_offset']+provenance['line_bytes']]
            self.assertEqual(provenance['row_index'], i)
            self.assertEqual(hashlib.sha256(line).hexdigest(), provenance['line_sha256'])
            self.assertEqual(subset.encoded(json.loads(line)), subset.encoded(selected['native']))
        self.assertFalse(value['physical_measurement_ids_assigned'])
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.f.root.iterdir()})

    def test_assignment_does_not_invent_measurement_status(self):
        row = self.select()['selected'][1]
        self.assertEqual(row['assignments'][0], {'method':'self','threshold':'presence','disposition':'assigned'})
        self.assertTrue(all(x['disposition']=='not_assigned' for x in row['assignments'][1:]))
        self.assertEqual(row['native']['self']['early']['evaluation_presence']['technical_status'], 'invalid_answer_schema')
        self.assertNotIn('physical_id', row)

    def test_metadata_and_payload_bounds_before_payload_read(self):
        for key, reason in [('payload_bytes','exact_bounded_trajectory_payload'),
                            ('metadata_total_bytes','metadata_total_bound')]:
            with self.subTest(key=key), patch.object(subset, 'read_pinned', wraps=subset.read_pinned) as read:
                with self.assertRaisesRegex(ValueError, reason):
                    self.select(limits=dict(self.f.limits, **{key:1}))
                self.assertFalse(any(call.args[0]['path'].endswith('trajectories.jsonl') for call in read.call_args_list))

    def test_wrong_campaign_target_refuses_before_payload(self):
        self.f.mapping['comparisons'][0]['members'][0]['arms']['baseline']['original_run_id']='another_campaign'
        self.f.update_mapping()
        with patch.object(subset, 'read_pinned', wraps=subset.read_pinned) as read:
            with self.assertRaisesRegex(ValueError,'target_outside_fixed_domain'): self.select()
            self.assertFalse(any(call.args[0]['path'].endswith('trajectories.jsonl') for call in read.call_args_list))

    def test_mapping_identity_and_acceptance(self):
        for mutation, reason in [('task','target_identity_mismatch'),('review','accepted_mapping_required')]:
            with self.subTest(mutation=mutation):
                self.f = Fixture(self.temporary.name)
                if mutation=='task':
                    self.f.mapping['comparisons'][0]['members'][0]['task_id']='different';self.f.update_mapping()
                else:
                    peer=self.f.load(self.f.inputs['mapping_review']);peer['accepted_frozen_neutral_matched_membership']=False
                    self.f.inputs['mapping_review']=self.f.put('mapping_peer.json',peer)
                with self.assertRaisesRegex(ValueError,reason):self.select()

    def test_other_or_unspecified_stage_refuses_before_payload(self):
        for stage in ('end', None):
            with self.subTest(stage=stage):
                self.f = Fixture(self.temporary.name)
                if stage is None:
                    del self.f.mapping['comparisons'][0]['stage']
                else:
                    self.f.mapping['comparisons'][0]['stage'] = stage
                self.f.update_mapping()
                with patch.object(subset, 'read_pinned', wraps=subset.read_pinned) as read:
                    with self.assertRaisesRegex(ValueError, 'early_comparison_required'):
                        self.select()
                    self.assertFalse(any(call.args[0]['path'].endswith('trajectories.jsonl')
                                         for call in read.call_args_list))

    def test_unselected_duplicate_or_wrong_domain_refuses(self):
        for replacement in ['baseline', 'unknown']:
            with self.subTest(replacement=replacement):
                self.f = Fixture(self.temporary.name);self.f.native[-1]['run_id']=replacement;self.f.update_payload()
                with self.assertRaisesRegex(ValueError,'trajectory_run_domain'):self.select()

    def test_missing_unselected_terminal_row_refuses(self):
        self.f.native.pop();self.f.update_payload()
        with self.assertRaisesRegex(ValueError,'complete_trajectory_domain'):self.select()

    def test_unselected_allocation_mismatch_refuses(self):
        self.f.native[-1]['allocation']['seed']+=1;self.f.update_payload()
        with self.assertRaisesRegex(ValueError,'trajectory_allocation_identity'):self.select()

    def test_unselected_assignment_mismatch_refuses(self):
        self.f.native[-1]['allocation']['instruments']=['evaluation_presence'];self.f.update_payload()
        with self.assertRaisesRegex(ValueError,'allocation_assignment_roster'):self.select()

    def test_reject_nonfinite_values_in_unselected_row(self):
        for token in [b'1e999',b'NaN',b'-Infinity']:
            with self.subTest(token=token):
                self.f = Fixture(self.temporary.name)
                rows=[subset.encoded(x) for x in self.f.native]
                rows[-1]=rows[-1][:-1]+b',"unselected_numeric":'+token+b'}'
                self.f.update_payload(b'\n'.join(rows)+b'\n')
                with self.assertRaisesRegex(ValueError,'nonfinite_json'):self.select()

    def test_duplicate_nested_key_in_unselected_row_refuses(self):
        rows=[subset.encoded(x) for x in self.f.native]
        rows[-1]=rows[-1][:-1]+b',"duplicate":{"x":1,"x":2}}'
        self.f.update_payload(b'\n'.join(rows)+b'\n')
        with self.assertRaisesRegex(ValueError,'duplicate_json_key'):self.select()

    def test_nonfinite_metadata_refuses(self):
        body=self.f.paths['fixed.json'].read_bytes().replace(b'"seed":10',b'"seed":1e999')
        self.f.inputs['fixed_metadata']=self.f.put('fixed.json',body,raw=True)
        with self.assertRaisesRegex(ValueError,'nonfinite_json'):self.select()

    def test_payload_tamper_blank_row_and_missing_newline(self):
        for kind, reason in [('tamper','stored_bytes_changed'),('blank','nonempty_jsonl_row'),('newline','jsonl_final_newline_required')]:
            with self.subTest(kind=kind):
                self.f = Fixture(self.temporary.name);body=self.f.paths['trajectories.jsonl'].read_bytes()
                if kind=='tamper':self.f.paths['trajectories.jsonl'].write_bytes(body+b' ')
                else:self.f.update_payload(body+b'\n' if kind=='blank' else body[:-1])
                with self.assertRaisesRegex(ValueError,reason):self.select()

    def test_changed_metadata_after_payload_refuses(self):
        original=subset.read_pinned
        def reading(ref,**kwargs):
            data=original(ref,**kwargs)
            if ref['path'].endswith('trajectories.jsonl'):
                path=self.f.paths['fixed.json'];path.write_bytes(path.read_bytes()+b' ')
            return data
        with patch.object(subset,'read_pinned',side_effect=reading):
            with self.assertRaisesRegex(ValueError,'stored_bytes_changed'):self.select()

    def test_output_bound_does_not_write(self):
        before={p.name:p.read_bytes() for p in self.f.root.iterdir()}
        with self.assertRaisesRegex(ValueError,'output_bound'):
            self.select(limits=dict(self.f.limits,output_bytes=1))
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.f.root.iterdir()})

    def test_assembly_review_and_roster_join_refuse(self):
        for kind, reason in [('peer','accepted_assembly_required'),('freeze','reader_threshold_roster_join')]:
            with self.subTest(kind=kind):
                self.f = Fixture(self.temporary.name)
                if kind=='peer':
                    peer=self.f.load(self.f.inputs['assembly_review']);peer['findings']=['hold']
                    self.f.inputs['assembly_review']=self.f.put('assembly_peer.json',peer)
                else:
                    freeze=self.f.load(self.f.inputs['reader_freeze']);freeze['slots'].append(copy.deepcopy(freeze['slots'][0]))
                    ref=self.f.put('freeze.json',freeze);self.f.inputs['reader_freeze']=ref
                    binding=self.f.load(self.f.load(self.f.inputs['assembly_result'])['binding']);binding['freeze_binding']=ref
                    ref=self.f.put('binding.json',binding)
                    result=self.f.load(self.f.inputs['assembly_result']);result['binding']=ref
                    self.f.inputs['assembly_result']=self.f.put('result.json',result)
                    peer=self.f.load(self.f.inputs['assembly_review']);peer['binding']=ref;peer['result']=self.f.inputs['assembly_result']
                    self.f.inputs['assembly_review']=self.f.put('assembly_peer.json',peer)
                with self.assertRaisesRegex(ValueError,reason):self.select()


if __name__ == '__main__':
    unittest.main()
