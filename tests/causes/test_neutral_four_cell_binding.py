"""Synthetic authenticated shapes only; no saved scientific inputs are read."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from causes import neutral_four_cell_binding as binding
from causes.neutral_four_cell import THRESHOLDS, INSTRUMENTS, CELLS
from causes.neutral_four_cell_assignments import describe_four_cells_with_assignments


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def pin(name):
    return dict(path='/synthetic/' + name, bytes=len(name), sha256=digest(name))


def protocols():
    return dict(self={name: dict(schema='causes.saved_native_direct_instrument_protocol.v1',
        model='model', config_without_endpoint_sha256=digest('configuration'),
        runner_source={k: v for k, v in pin('runner').items() if k != 'path'},
        question=dict(id='evaluation_' + name, threshold=name, awareness_type='evaluation', text=name))
        for name in THRESHOLDS}, reader={name: dict(schema='causes.saved_reader_instrument_protocol.v1',
        instrument=name, phase='prefix', awareness_type='evaluation', config_sha256=digest('reader-config'),
        rubric_sha256=digest('rubric'), threshold_sha256=digest(name), schema_sha256=digest('schema'),
        reader_source_sha256=digest('reader-source')) for name in THRESHOLDS})


def arm(run, view, unavailable):
    return dict(original_run_id=run, neutral_view=view,
        input_disposition='unavailable' if run in unavailable else 'prepared',
        direct_key='derived_probe_' + digest([run, view]), reader_key='reader_' + digest([run, view]))


def direct_row(a, proto):
    run, key, view = a['original_run_id'], a['direct_key'], a['neutral_view']
    row = dict(schema='causes.derived_probe_input.v1', run_id=run, view_kind=view, stage='early',
        measurement_key=key, disposition=a['input_disposition'], protocol_sha256=digest('derived-protocol'),
        provenance=[pin('origin/' + run)], runtime_authorized=False)
    if a['input_disposition'] == 'unavailable':
        row.update(reason='frozen_context_absent', view=None, jobs=[])
        return row
    row.update(view=pin('view/' + run + '/' + view), view_canonical_sha256=digest(['view', run, view]),
        context_canonical_sha256=digest(['native-array', run, view]),
        original=dict(run_id=run, seed=31, repetition=0, canonical_variant=run.split('_')[0]),
        origin=dict(config=pin('config'), allocation=pin('allocation/' + run)), jobs=[])
    for i, name in enumerate(THRESHOLDS):
        job = dict(family='derived_probe_family', model='model_namespace', run_id=key,
            stage='early', question_id='evaluation_' + name, original_run_id=run, original_seed=31,
            seed=97+i, context_canonical_sha256=row['context_canonical_sha256'],
            question_canonical_sha256=digest(proto['self'][name]['question']),
            request_body_sha256=digest(['body', key, name]), request_body_bytes=231,
            request_file_sha256=digest(['file', key, name]),
            derived_view_canonical_sha256=row['view_canonical_sha256'], protocol_sha256=row['protocol_sha256'])
        job['measurement_id'] = '/'.join(job[k] for k in ('family', 'model', 'run_id', 'stage', 'question_id'))
        row['jobs'].append(job)
    return row


def fixture(spec=None, unavailable=()):
    """spec entries are (variant, fixed, included, treatment view)."""
    spec = spec or [('A', 2, 2, 'initial'), ('B', 2, 1, 'initial'), ('C', 1, 1, 'grouped')]
    unavailable = set(unavailable)
    proto = protocols()
    membership = dict(schema='causes.frozen_neutral_matched_member_mapping.v1', comparisons=[],
        neutral_direct_semantic_protocols=[dict(question_id='evaluation_' + name,
            semantic_protocol=proto['self'][name], sha256=digest(proto['self'][name])) for name in THRESHOLDS])
    inputs = []
    for variant, fixed, included, view in spec:
        c = dict(variant=variant, baseline='baseline', stage='early', fixed_pairs=fixed,
            included_pairs=included, included_tasks=len({i//5 for i in range(included)}),
            direct_rows=pin(variant + '/ROWS'), members=[])
        current = []
        for i in range(fixed):
            m = dict(task_id='task_' + str(i//5), draw_key=i%5,
                     disposition='included' if i < included else 'unsupported')
            if i < included:
                m['arms'] = dict(baseline=arm('base_' + str(i), 'initial', unavailable),
                                treatment=arm(variant + '_' + str(i), view, unavailable))
                current.extend(direct_row(a, proto) for a in m['arms'].values())
            else:
                m.update(baseline_run_id='base_' + str(i), treatment_run_id=variant + '_' + str(i),
                         reason='unsupported_saved_history')
            c['members'].append(m)
        membership['comparisons'].append(c)
        inputs.append(dict(variant=variant, rows=c['direct_rows'], preparation=pin(variant+'/preparation'),
                           review=pin(variant+'/review'), value=current))
    evidence = {k: dict(data=pin(k+'/data'), review=pin(k+'/review'))
                for k in ('membership', 'original', 'neutral_reader')}
    evidence.update(qualification=pin('qualification'), neutral_direct=None)
    original = dict(schema='causes.saved_original_measurement_registry.v1', inference_released=False,
        membership=dict(actual=evidence['membership']['data'], review=evidence['membership']['review']),
        comparisons=copy.deepcopy(membership['comparisons']), protocols=copy.deepcopy(proto),
        measurements=[], assignments=[], provenance=[])
    reader = dict(schema='causes.saved_neutral_reader_registry.v1', inference_released=False,
        membership=evidence['membership']['data'], membership_review=evidence['membership']['review'],
        comparisons=copy.deepcopy(membership['comparisons']), protocols=copy.deepcopy(proto['reader']),
        measurements=[], assignments=[], origins={})
    runs = list(dict.fromkeys(row['run_id'] for item in inputs for row in item['value']))
    for run in runs:
        for method in ('self', 'reader'):
            for name in THRESHOLDS:
                physical = (dict(kind='native_snapshot_measurement', source='/synthetic/snapshot/'+run,
                    summary_sha256=digest(['summary',run]), stage='early', instrument='evaluation_'+name)
                    if method == 'self' else dict(kind='native_reader_result', result=pin('result/'+run),
                                                  stage='early', instrument=name))
                identity = 'original_record_' + digest(physical)
                context = digest(['native-array' if method == 'self' else 'prepared-object',run,'original'])
                original['measurements'].append(dict(id=identity, original_run_id=run, stage='early',
                    method=method, threshold=name, awareness_type='evaluation', context_sha256=context,
                    protocol_sha256=digest(proto[method][name]), label='no', status='observed', reason=None))
                original['provenance'].append(dict(id=identity, physical=physical,
                    native_context_sha256=digest(['native-array',run,'original'])))
                original['assignments'].append(dict(original_run_id=run, stage='early', method=method,
                    threshold=name, disposition='assigned', measurement_id=identity, record_state='present'))
    seen = set()
    for c, item in zip(membership['comparisons'], inputs):
        rows = {r['measurement_key']:r for r in item['value']}
        for m in c['members']:
            if m['disposition'] != 'included':
                continue
            for a in m['arms'].values():
                key, run = a['reader_key'], a['original_run_id']
                row = rows[a['direct_key']]; missing = a['input_disposition'] == 'unavailable'
                ids = {}
                for name in THRESHOLDS:
                    request = None if missing else pin('request/'+key+'/'+name)
                    identity = ('reader_unavailable_'+digest([key,name]) if missing else
                                'reader_physical_'+digest(request))
                    ids[name] = identity
                    if identity not in seen:
                        reader['measurements'].append(dict(id=identity, original_run_id=run, stage='early',
                            method='reader', threshold=name, awareness_type='evaluation',
                            context_sha256=None if missing else digest(['prepared-object',run,a['neutral_view']]),
                            protocol_sha256=digest(proto['reader'][name]), label=None if missing else 'no',
                            status='unavailable' if missing else 'observed', reason='frozen_context_absent' if missing else None))
                        reader['origins'][identity] = dict(physical=dict(request=request,
                            result=None if missing else pin('readout-result/'+key), context_key=key,
                            view_kind=a['neutral_view'], view_sha256=row.get('view_canonical_sha256'),
                            native_status='unavailable' if missing else 'complete',
                            canonical_request_sha256=None if missing else digest(['payload',key,name])), aliases=[])
                        seen.add(identity)
                    reader['origins'][identity]['aliases'].append(dict(variant=c['variant'], readout_row=key))
                reader['assignments'].append(dict(variant=c['variant'], measurement_key=key, instruments=ids))
    return dict(membership=membership, original=original, neutral_reader=reader, direct_inputs=inputs,
                evidence=evidence)


def remove_original(f, run, method, threshold, state):
    a = next(x for x in f['original']['assignments'] if
             (x['original_run_id'],x['method'],x['threshold']) == (run,method,threshold))
    identity = a['measurement_id']
    f['original']['measurements'] = [r for r in f['original']['measurements'] if r['id'] != identity]
    f['original']['provenance'] = [p for p in f['original']['provenance'] if p['id'] != identity]
    a.update(measurement_id=None, record_state=state,
             disposition='not_assigned' if state == 'not_assigned' else 'assigned')


def add_direct_result(f):
    unique = {r['measurement_key']:r for item in f['direct_inputs'] for r in item['value']}
    output = dict(schema='causes.saved_derived_direct_readout.v1', inference_released=False,
        instrument_protocols={'evaluation_'+k:v for k,v in f['original']['protocols']['self'].items()},
        origins=[dict(closed=pin('closed-origin'))], measurements=[])
    for key, row in unique.items():
        for i, name in enumerate(THRESHOLDS):
            job = row['jobs'][i] if row['disposition'] == 'prepared' else None
            output['measurements'].append(dict(measurement_id=job['measurement_id'] if job else
                '/'.join(('derived_probe_family','model_namespace',key,'early','evaluation_'+name)),
                original_run_id=row['run_id'], measurement_key=key, view_kind=row['view_kind'], stage='early',
                method='self', awareness_type='evaluation', threshold=name, derived_protocol_sha256=row['protocol_sha256'],
                instrument_protocol_sha256=digest(f['original']['protocols']['self'][name]),
                context_canonical_sha256=job['context_canonical_sha256'] if job else None, job=copy.deepcopy(job),
                label='yes' if job else None, status='observed' if job else 'unavailable',
                reason=None if job else row['reason'],
                origin=dict(source_index=0,index=i,final=pin('final/'+key+'/'+name)) if job else None))
    f['neutral_direct'] = output
    f['evidence']['neutral_direct'] = dict(data=pin('direct-result'),review=pin('direct-result-peer'))


class BindingTests(unittest.TestCase):
    def test_complete_roster_typed_identity_and_consumer_compatibility(self):
        f = fixture(unavailable=['A_1'])
        remove_original(f,'A_0','reader','presence','not_assigned')
        remove_original(f,'A_1','self','presence','absent_instance')
        result = binding.bind_four_cells(**f)
        args = result['arguments']; c = args['comparisons']
        self.assertEqual([len(x['members']) for x in c], [2,2,1])
        self.assertEqual(result['provenance']['fixed_pairs'],5)
        self.assertEqual(result['provenance']['included_pairs'],4)
        self.assertEqual(c[0]['members'][0]['instruments']['reader:presence']['treatment_original']['state'],'not_assigned')
        absent = c[0]['members'][1]['instruments']['self:presence']['treatment_original']
        self.assertEqual((absent['id'],absent['state']),(None,'absent_instance'))
        unknown = c[1]['members'][1]['instruments']['reader:presence']['treatment_original']
        self.assertEqual(unknown['state'],'not_in_resolved_inventory')
        self.assertEqual(unknown['qualification']['state'],'unresolved')
        self.assertEqual(c[1]['members'][1]['instruments']['reader:presence']['baseline_neutral']['state'],'present')
        self.assertEqual(c[1]['members'][1]['instruments']['reader:presence']['treatment_neutral']['state'],'unsupported')
        self.assertEqual(c[0]['members'][0]['instruments']['self:presence']['treatment_neutral']['state'],'pending')
        self.assertEqual(c[0]['members'][1]['instruments']['self:presence']['treatment_neutral']['state'],'unavailable')
        kinds = {r['identity_kind'] for r in args['measurements']}
        self.assertEqual(kinds,{'native_record','physical_request','assigned_slot'})
        # Only this synthetic test invokes the unchanged consumer, never the binder.
        summary = describe_four_cells_with_assignments(**args)
        self.assertEqual(summary['comparisons'][1]['full_fixed']['pairs'],2)
        self.assertEqual(summary['comparisons'][1]['coverage_qualified']['pairs'],1)
        self.assertFalse(summary['inference_released'])

    def test_distinct_arm_views_and_hash_domains_preserved(self):
        f = fixture(); r = binding.bind_four_cells(**f)
        c = r['arguments']['comparisons'][2]
        self.assertEqual(c['neutral_views_by_arm'],dict(baseline='initial',treatment='grouped'))
        m = c['members'][0]['instruments']
        self.assertNotEqual(m['self:presence']['treatment_neutral']['context_sha256'],
                            m['reader:presence']['treatment_neutral']['context_sha256'])
        self.assertNotEqual(m['self:presence']['treatment_original']['context_sha256'],
                            m['reader:presence']['treatment_original']['context_sha256'])

    def test_shared_baseline_canonical_input_order_and_outer_aliases(self):
        f = fixture(); f['direct_inputs'] = f['direct_inputs'][::-1]
        r = binding.bind_four_cells(**f)
        p = r['provenance']['direct_inputs']
        self.assertEqual(p['sources'][0]['variant'],'C')
        key = f['membership']['comparisons'][0]['members'][0]['arms']['baseline']['direct_key']
        a = next(x for x in p['rows'] if x['key'] == key)
        self.assertEqual(a['canonical']['source'],0)
        self.assertEqual(len(a['aliases']),3)
        refs = [c['members'][0]['instruments']['self:presence']['baseline_neutral']
                for c in r['arguments']['comparisons']]
        self.assertTrue(all(x == refs[0] for x in refs))
        self.assertTrue(all(x['logical_alias'] is None and x['id'] is None for x in refs))

    def test_whole_shared_row_equality_includes_provenance_and_seed(self):
        for mutate in (lambda r:r['provenance'].append(pin('extra')),
                       lambda r:r['jobs'][0].update(seed=999)):
            f = fixture(); mutate(f['direct_inputs'][1]['value'][0])
            with self.assertRaisesRegex(ValueError,'shared_direct_row_changed'):
                binding.bind_four_cells(**f)

    def test_direct_job_identity_mismatches(self):
        changes = [('original_run_id','other'),('context_canonical_sha256',digest('other')),
                   ('question_canonical_sha256',digest('wrong-question')),('original_seed',True),
                   ('seed',False),('measurement_id','fake'),('request_file_sha256','bad'),
                   ('protocol_sha256',digest('other-protocol'))]
        for key,value in changes:
            f = fixture(); f['direct_inputs'][0]['value'][1]['jobs'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                binding.bind_four_cells(**f)

    def test_neutral_view_hash_must_bridge_direct_and_reader(self):
        f = fixture(); next(iter(f['neutral_reader']['origins'].values()))['physical']['view_sha256']=digest('other')
        with self.assertRaisesRegex(ValueError,'neutral_view_hash_bridge'):
            binding.bind_four_cells(**f)

    def test_registry_protocol_and_native_identity_mismatches(self):
        for part in ('protocol','identity','native-context'):
            f=fixture()
            if part == 'protocol': f['original']['measurements'][0]['protocol_sha256']=digest('bad')
            elif part == 'identity': f['original']['provenance'][0]['physical']['source'] += '/changed'
            else: f['original']['provenance'][0]['native_context_sha256']=digest('wrong-domain')
            with self.subTest(part=part),self.assertRaises(ValueError): binding.bind_four_cells(**f)

    def test_original_assignment_roster_and_null_id_are_exact(self):
        for action in ('missing','duplicate','invented-null'):
            f=fixture()
            if action == 'missing': f['original']['assignments'].pop()
            elif action == 'duplicate': f['original']['assignments'].append(copy.deepcopy(f['original']['assignments'][0]))
            else: f['original']['assignments'][0].update(record_state='not_assigned',disposition='not_assigned')
            with self.subTest(action=action),self.assertRaises(ValueError): binding.bind_four_cells(**f)

    def test_reader_alias_request_and_view_mismatches(self):
        for action in ('alias','request','view','assignment'):
            f=fixture(); origin=next(iter(f['neutral_reader']['origins'].values()))
            if action=='alias': origin['aliases'].pop()
            elif action=='request': origin['physical']['request']['path'] += '/other'
            elif action=='view': origin['physical']['view_kind']='other'
            else: f['neutral_reader']['assignments'].pop()
            with self.subTest(action=action),self.assertRaises(ValueError): binding.bind_four_cells(**f)

    def test_unavailable_cannot_acquire_context_or_native_label(self):
        for action in ('context','label','physical'):
            f=fixture(unavailable=['A_1']); row=next(r for r in f['neutral_reader']['measurements'] if r['status']=='unavailable')
            if action=='context': row['context_sha256']=digest('invented')
            elif action=='label': row['label']='yes'
            else: f['neutral_reader']['origins'][row['id']]['physical']['request']=pin('invented')
            with self.subTest(action=action),self.assertRaises(ValueError): binding.bind_four_cells(**f)

    def test_technical_missing_and_nonbinary_labels_remain_literal(self):
        f=fixture()
        for i, row in enumerate(f['neutral_reader']['measurements'][:2]):
            row.update(label=None if i==0 else 'not directly interpretable as such',
                       status='technical_missing' if i==0 else 'observed',
                       reason='invalid_citation' if i==0 else None)
        r=binding.bind_four_cells(**f)
        index={x['id']:x for x in r['arguments']['measurements']}
        for row in f['neutral_reader']['measurements'][:2]:
            self.assertEqual({k:index[row['id']][k] for k in row},row)

    def test_future_direct_native_and_assigned_identity_without_http_claim(self):
        f=fixture(unavailable=['A_1']); add_direct_result(f)
        row=f['neutral_direct']['measurements'][0]
        row.update(label=None,status='technical_missing',reason='saved_invalid_schema',native_label=None)
        row=f['neutral_direct']['measurements'][1]
        row.update(label=None,status='pending',reason='unclaimed_at_accepted_cut',origin=None)
        r=binding.bind_four_cells(**f)
        index={x['id']:x for x in r['arguments']['measurements']}
        self.assertEqual(index[f['neutral_direct']['measurements'][0]['measurement_id']]['identity_kind'],'native_record')
        self.assertEqual(index[f['neutral_direct']['measurements'][1]['measurement_id']]['identity_kind'],'assigned_slot')
        self.assertTrue(r['provenance']['neutral_direct_result_bound'])
        describe_four_cells_with_assignments(**r['arguments'])

    def test_future_direct_job_origin_and_complete_roster_must_match(self):
        for action in ('job','origin','missing','unavailable-id'):
            f=fixture(unavailable=['A_1']); add_direct_result(f)
            if action=='job': f['neutral_direct']['measurements'][0]['job']['seed']+=1
            elif action=='origin': f['neutral_direct']['measurements'][0]['origin']['source_index']=True
            elif action=='missing': f['neutral_direct']['measurements'].pop()
            else: next(r for r in f['neutral_direct']['measurements'] if r['status']=='unavailable')['measurement_id']='fake'
            with self.subTest(action=action),self.assertRaises(ValueError): binding.bind_four_cells(**f)

    def test_future_direct_structured_failure_reason_is_lossless(self):
        f=fixture(); add_direct_result(f)
        reasons = [[dict(stage='native_admission', measurement_id='saved-id', error='refused')],
                   [dict(stage='qualification', error='invalid evidence')],
                   dict(kind='transport', details=['saved failure',7])]
        states = ['invalid_admission','qualification_failure','technical_missing']
        for row,reason,state in zip(f['neutral_direct']['measurements'],reasons,states):
            row.update(label=None,status=state,reason=reason)
        before=copy.deepcopy(f)
        result=binding.bind_four_cells(**f)
        index={r['id']:r for r in result['arguments']['measurements']}
        for row,rendered in zip(f['neutral_direct']['measurements'],result['provenance']['direct_reason_renderings']):
            self.assertEqual(rendered['native_reason'],row['reason'])
            self.assertEqual(rendered['consumer_reason'],'native_reason_json:'+encoded(row['reason']).decode())
            self.assertEqual(index[row['measurement_id']]['reason'],rendered['consumer_reason'])
            self.assertEqual(index[row['measurement_id']]['status'],row['status'])
        self.assertEqual(f,before)
        describe_four_cells_with_assignments(**result['arguments'])
        self.assertNotIn('direct_reason_renderings',binding.bind_four_cells(**fixture())['provenance'])

    def test_future_direct_invalid_reason_shapes_refused(self):
        for reason in ([],{},False,7,[dict(error=float('inf'))]):
            f=fixture(); add_direct_result(f)
            f['neutral_direct']['measurements'][0].update(label=None,status='technical_missing',reason=reason)
            with self.subTest(reason=reason),self.assertRaises(ValueError): binding.bind_four_cells(**f)

    def test_protocol_roster_end_stage_and_frozen_counts_refused(self):
        for action in ('protocol','stage','count','task'):
            f=fixture(); c=f['membership']['comparisons'][0]
            if action=='protocol': f['membership']['neutral_direct_semantic_protocols'][0]['semantic_protocol']['model']='other'
            elif action=='stage': c['stage']='end'
            elif action=='count': c['fixed_pairs']+=1
            else: c['members'][1]['draw_key']=0
            f['original']['comparisons']=copy.deepcopy(f['membership']['comparisons'])
            f['neutral_reader']['comparisons']=copy.deepcopy(f['membership']['comparisons'])
            with self.subTest(action=action),self.assertRaises(ValueError): binding.bind_four_cells(**f)

    def test_authentication_descriptors_membership_and_input_sources_match(self):
        for action in ('membership','source','direct-review','extra-evidence'):
            f=fixture()
            if action=='membership': f['original']['membership']['actual']=pin('other')
            elif action=='source': f['direct_inputs'][0]['rows']=pin('other')
            elif action=='direct-review': f['evidence']['neutral_direct']=dict(data=pin('x'),review=pin('y'))
            else: f['evidence']['extra']=pin('z')
            with self.subTest(action=action),self.assertRaises(ValueError): binding.bind_four_cells(**f)

    def test_pure_deterministic_no_input_mutation_and_exact_output_bound(self):
        f=fixture(); old=copy.deepcopy(f)
        with patch('builtins.open',side_effect=AssertionError('I/O forbidden')):
            r=binding.bind_four_cells(**f)
        self.assertEqual(f,old)
        size=len(encoded(r))
        self.assertEqual(r,binding.bind_four_cells(**f,maximum_result_bytes=size))
        with self.assertRaisesRegex(ValueError,'binding_serialized_bound'):
            binding.bind_four_cells(**f,maximum_result_bytes=size-1)
        r['arguments']['comparisons'][0]['members'][0]['reason']='mutated-return'
        self.assertEqual(f,old)

    def test_full_five_family_roster_has_30240_cells_and_3756_ready_jobs(self):
        unavailable=['base_'+str(i) for i in range(11)]+['A_'+str(i) for i in range(10)]
        f=fixture([('A',300,300,'initial'),('B',300,300,'initial'),('C',300,300,'initial'),
                   ('D',300,70,'initial'),('E',60,3,'grouped')],unavailable)
        # Bank order can differ from comparison display order.
        f['direct_inputs'][-2:]=f['direct_inputs'][-2:][::-1]
        r=binding.bind_four_cells(**f)
        p=r['provenance']; self.assertEqual((p['fixed_pairs'],p['included_pairs']),(1260,973))
        self.assertEqual((p['direct_inputs']['prepared_jobs'],p['direct_inputs']['unavailable_slots']),(3756,63))
        self.assertEqual(sum(len(c['members'])*len(INSTRUMENTS)*len(CELLS) for c in r['arguments']['comparisons']),30240)
        self.assertLess(len(encoded(r)),64*1024**2)
        self.assertTrue(all(ref['logical_alias'] is None for c in r['arguments']['comparisons']
            for m in c['members'] for cells in m['instruments'].values() for ref in cells.values()))


if __name__ == '__main__':
    unittest.main()
