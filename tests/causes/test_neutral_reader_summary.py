import copy
import hashlib
import json
import unittest

from causes import neutral_four_cell as four


def fixture(draws=(1,), variants=('V1',), unsupported=()):
    protocols = {name:dict(instrument=name, configured_semantics='synthetic') for name in four.THRESHOLDS}
    registry = dict(schema='causes.saved_neutral_reader_registry.v1', inference_released=False,
                    protocols=protocols, comparisons=[], measurements=[], origins={}, assignments=[])
    by_id = {}
    for variant in variants:
        members = []
        for task, count in enumerate(draws):
            for draw in range(count):
                baseline, treatment = f'B/{task}/{draw}', f'{variant}/{task}/{draw}'
                member = dict(task_id=f'task{task}', draw_key=draw)
                if (variant, task, draw) in unsupported:
                    member.update(disposition='unsupported', reason='saved coverage not qualified',
                                  baseline_run_id=baseline, treatment_run_id=treatment)
                else:
                    member.update(disposition='included', arms={})
                    for arm, run in (('baseline', baseline), ('treatment', treatment)):
                        key = 'context/'+run
                        member['arms'][arm] = dict(original_run_id=run, reader_key=key,
                            input_disposition='prepared', neutral_view='initial_only')
                        ids = {}
                        for threshold in four.THRESHOLDS:
                            identity = key+'/'+threshold; ids[threshold] = identity
                            digest = hashlib.sha256(json.dumps(protocols[threshold], sort_keys=True,
                                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()
                            row = dict(id=identity, original_run_id=run, stage='early', method='reader',
                                awareness_type='evaluation', threshold=threshold, context_sha256='a'*64,
                                protocol_sha256=digest, label='no', status='observed', reason=None)
                            if identity not in by_id:
                                by_id[identity] = row; registry['measurements'].append(row)
                                registry['origins'][identity] = dict(physical=dict(context_key=key,
                                    view_kind='initial_only'), aliases=[])
                        registry['assignments'].append(dict(variant=variant, measurement_key=key, instruments=ids))
                members.append(member)
        included = [x for x in members if x['disposition']=='included']
        registry['comparisons'].append(dict(variant=variant, baseline='B', stage='early', members=members,
            fixed_pairs=len(members), included_pairs=len(included),
            included_tasks=len({x['task_id'] for x in included})))
    scopes = {v:'Frozen coverage-selected saved histories; descriptive only.' for v in variants}
    family = [dict(variant=v, baseline='B', readout=method+'_evaluation_presence')
              for v in variants for method in ('self', 'reader')]
    return registry, scopes, family


def describe(registry, scopes, family, **kwargs):
    return four.describe_neutral_readers(registry, scopes=scopes, primary_family=family, **kwargs)


def threshold(result, scope='full_fixed', comparison=0, name='presence'):
    return result['comparisons'][comparison][scope]['thresholds'][name]


class NeutralReaderSummaryTests(unittest.TestCase):
    def test_full_synthetic_five_comparison_roster_is_retained(self):
        args = fixture((5,)*60, variants=('V1','V2','V3'))
        coverage = (1,)*16+(2,)*7+(3,)*9+(4,)*2+(5,)
        excluded = {('V4',t,d) for t in range(60) for d in range(5)
                    if t >= len(coverage) or d >= coverage[t]}
        extras = [fixture((5,)*60, variants=('V4',), unsupported=excluded),
                  fixture((1,)*60, variants=('V5',),
                          unsupported={('V5',t,0) for t in range(3,60)})]
        for extra, scopes, family in extras:
            existing = {x['id'] for x in args[0]['measurements']}
            args[0]['measurements'].extend(x for x in extra['measurements'] if x['id'] not in existing)
            args[0]['origins'].update(extra['origins'])
            args[0]['assignments'].extend(extra['assignments'])
            args[0]['comparisons'].extend(extra['comparisons'])
            args[1].update(scopes); args[2].extend(family)
        result = describe(*args)
        self.assertEqual(sum(len(c['fixed_members']) for c in result['comparisons']),1260)
        self.assertEqual(sum(c['coverage_qualified']['pairs'] for c in result['comparisons']),973)
        self.assertEqual(sum(x['disposition']=='unsupported' for c in result['comparisons']
                             for x in c['fixed_members']),287)
        self.assertEqual([len(c['coverage_qualified']['task_draw_counts']) for c in result['comparisons']],
                         [60,60,60,35,3])
        self.assertEqual(result['physical_measurements'],3819)
        self.assertEqual(result['primary_family_size'],10)

    def test_three_thresholds_and_unchanged_primary_family(self):
        args = fixture(variants=('V1','V2','V3','V4','V5')); before = copy.deepcopy(args)
        result = describe(*args)
        self.assertEqual(args, before)
        self.assertEqual(result['primary_family'], args[2]); self.assertEqual(result['primary_family_size'], 10)
        self.assertEqual(set(result['comparisons'][0]['full_fixed']['thresholds']), set(four.THRESHOLDS))
        self.assertFalse(result['direct_self_reports_evaluated']); self.assertFalse(result['inference_released'])
        self.assertFalse(result['missing_bounds_are_confidence_intervals'])
        self.assertNotIn('approximate_p', str(result)); self.assertNotIn('se_upper_bound', str(result))

    def test_unequal_draws_weight_tasks_equally(self):
        args = fixture((1,3))
        for row in args[0]['measurements']:
            if row['original_run_id']=='V1/0/0': row['label']='yes'
        result = describe(*args); effect = threshold(result)['treatment_minus_baseline']
        self.assertEqual(effect['point_estimate'], .5)
        self.assertEqual(effect['exact_bounds'], [{'numerator':1,'denominator':2}]*2)
        self.assertEqual(result['comparisons'][0]['full_fixed'], result['comparisons'][0]['coverage_qualified'])

    def test_full_denominator_includes_unsupported_tasks_and_draws(self):
        args = fixture((2,1), unsupported={('V1',0,1),('V1',1,0)})
        for row in args[0]['measurements']:
            if row['original_run_id'].startswith('V1/'): row['label']='yes'
        result = describe(*args); full = threshold(result); qualified = threshold(result,'coverage_qualified')
        self.assertEqual(full['treatment_minus_baseline']['bounds'],[-.5,1])
        self.assertEqual(full['treatment_minus_baseline']['unsupported_pair_weight'],{'numerator':3,'denominator':4})
        self.assertEqual(full['arms']['baseline']['affirmative_rate']['bounds'],[0,.75])
        self.assertEqual(full['arms']['treatment']['affirmative_rate']['bounds'],[.25,1])
        self.assertEqual(qualified['treatment_minus_baseline']['point_estimate'],1)
        self.assertEqual(result['comparisons'][0]['full_fixed']['pairs'],3)
        self.assertEqual(result['comparisons'][0]['coverage_qualified']['pairs'],1)
        unsupported = [x for x in result['comparisons'][0]['fixed_members'] if x['disposition']=='unsupported']
        self.assertEqual(len(unsupported),2); self.assertTrue(all(x['reason'] for x in unsupported))
        self.assertEqual(result['physical_measurements'],6)

    def test_fully_unsupported_roster_does_not_fabricate_requests(self):
        args = fixture(unsupported={('V1',0,0)}); result = describe(*args)
        self.assertEqual(result['physical_measurements'],0)
        self.assertEqual(threshold(result)['treatment_minus_baseline']['bounds'],[-1,1])
        self.assertEqual(result['comparisons'][0]['coverage_qualified']['thresholds'],{})
        self.assertEqual(threshold(result)['treatment_minus_baseline']['unresolved_physical_ids'],[])

    def test_missing_does_not_use_complete_case_denominator(self):
        args = fixture((2,))
        for row in args[0]['measurements']:
            if row['original_run_id']=='V1/0/0': row['label']='yes'
            if row['original_run_id']=='V1/0/1': row.update(label=None,status='technical_missing',reason='native failure')
        result = describe(*args); cell = threshold(result)['arms']['treatment']
        self.assertEqual(cell['assigned'],2); self.assertEqual(cell['missing'],1)
        self.assertEqual(cell['affirmative_rate']['bounds'],[.5,1])
        self.assertEqual(threshold(result)['treatment_minus_baseline']['bounds'],[.5,1])

    def test_native_nonbinary_categories_keep_frozen_affirmative_scoring(self):
        args = fixture((3,)); labels = four.READER_LABELS[2:]
        for row in args[0]['measurements']:
            if row['original_run_id'].startswith('V1/'): row['label']=labels[int(row['original_run_id'].rsplit('/',1)[1])]
        cell = threshold(describe(*args))['arms']['treatment']
        self.assertEqual(cell['observed'],3); self.assertEqual(cell['missing'],0)
        self.assertEqual(cell['affirmative_rate']['bounds'],[0,0])
        self.assertEqual(cell['categories'],dict.fromkeys(labels,1))

    def test_shared_missing_control_is_one_physical_instance(self):
        args = fixture(variants=('V1','V2'))
        for row in args[0]['measurements']:
            if row['original_run_id'].startswith('B/'): row.update(label=None,status='technical_missing',reason='same failed control')
        result = describe(*args)
        self.assertEqual(result['physical_measurements'],9)
        a = threshold(result)['treatment_minus_baseline']; b = threshold(result,comparison=1)['treatment_minus_baseline']
        self.assertEqual(a['bounds'],[-1,0]); self.assertEqual(a['unresolved_physical_ids'],b['unresolved_physical_ids'])
        self.assertEqual(len(a['unresolved_physical_ids']),1)

    def test_thresholds_are_not_pooled(self):
        args = fixture()
        for row in args[0]['measurements']:
            if row['original_run_id'].startswith('V1/') and row['threshold']=='presence': row['label']='yes'
        result = describe(*args)
        self.assertEqual(threshold(result)['treatment_minus_baseline']['point_estimate'],1)
        self.assertEqual(threshold(result,name='strongly_favors')['treatment_minus_baseline']['point_estimate'],0)

    def test_unavailable_input_has_no_context_or_observation(self):
        args = fixture(); member = args[0]['comparisons'][0]['members'][0]
        member['arms']['baseline']['input_disposition']='unavailable'
        for row in args[0]['measurements']:
            if row['original_run_id'].startswith('B/'):
                row.update(status='unavailable',label=None,reason='frozen input missing',context_sha256=None)
        result = describe(*args)
        self.assertEqual(threshold(result)['treatment_minus_baseline']['bounds'],[-1,0])
        member['arms']['baseline']['input_disposition']='prepared'
        with self.assertRaisesRegex(ValueError,'neutral_reader_physical_join'): describe(*args)

    def test_assignment_and_context_view_joins_refuse(self):
        for kind in ('key','view','threshold','run','context','disposition'):
            args = fixture(); r=args[0]; row=r['measurements'][0]; origin=r['origins'][row['id']]['physical']
            if kind=='key': origin['context_key']='other'
            elif kind=='view': origin['view_kind']='other'
            elif kind=='threshold': row['threshold']='strongly_favors'
            elif kind=='run': row['original_run_id']='other'
            elif kind=='context': row['context_sha256']='f'*64
            else:r['comparisons'][0]['members'][0]['arms']['baseline']['input_disposition']='unavailable'
            with self.subTest(kind=kind),self.assertRaises(ValueError): describe(*args)

    def test_protocol_stage_and_method_refuse(self):
        for field,value in (('protocol_sha256','f'*64),('stage','end'),('method','self'),('awareness_type','simulation')):
            args=fixture();args[0]['measurements'][0][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'neutral_reader_identity'):describe(*args)
        args=fixture();args[0]['comparisons'][0]['stage']='end'
        with self.assertRaisesRegex(ValueError,'neutral_reader_comparison'):describe(*args)

    def test_duplicates_and_unmapped_records_refuse(self):
        for kind in ('measurement','assignment','member','extra'):
            args=fixture();r=args[0]
            if kind=='measurement':r['measurements'].append(copy.deepcopy(r['measurements'][0]))
            elif kind=='assignment':r['assignments'].append(copy.deepcopy(r['assignments'][0]))
            elif kind=='member':r['comparisons'][0]['members'].append(copy.deepcopy(r['comparisons'][0]['members'][0]))
            else:
                row=copy.deepcopy(r['measurements'][0]);row['id']='extra';r['measurements'].append(row)
                r['origins']['extra']=copy.deepcopy(r['origins'][r['measurements'][0]['id']])
            with self.subTest(kind=kind),self.assertRaises(ValueError):describe(*args)

    def test_cross_draw_reuse_refuses(self):
        args=fixture(variants=('V1','V2'));args[0]['comparisons'][1]['members'][0]['draw_key']=1
        with self.assertRaisesRegex(ValueError,'neutral_reader_cross_task_or_draw'):describe(*args)

    def test_scope_family_and_declared_counts_refuse(self):
        for kind in ('scope','family','count','task_count'):
            args=fixture()
            if kind=='scope':args[1]['V1']=''
            elif kind=='family':args[2].pop()
            elif kind=='count':args[0]['comparisons'][0]['fixed_pairs']+=1
            else:args[0]['comparisons'][0]['included_tasks']+=1
            with self.subTest(kind=kind),self.assertRaises(ValueError):describe(*args)

    def test_invalid_labels_nonfinite_protocol_and_output_bounds_refuse(self):
        args=fixture();args[0]['measurements'][0]['label']='unknown'
        with self.assertRaises(ValueError):describe(*args)
        args=fixture();args[0]['protocols']['presence']['bad']=float('inf')
        with self.assertRaises(ValueError):describe(*args)
        args=fixture()
        for limit in (0,1,True):
            with self.subTest(limit=limit),self.assertRaisesRegex(ValueError,'neutral_reader_result_bound'):
                describe(*args,maximum_result_bytes=limit)


if __name__ == '__main__':
    unittest.main()
