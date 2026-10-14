import copy
from fractions import Fraction
import unittest

from causes import neutral_four_cell as four


def fixture(draws=(1,), *, shared_version=False):
    records={};members=[]
    for task,n in enumerate(draws):
        for draw in range(n):
            pair=dict(task_id='task'+str(task),draw_key=draw,baseline_run_id=f'B{task}/{draw}',
                      treatment_run_id=f'A{task}/{draw}',disposition='included',instruments={})
            for instrument in four.INSTRUMENTS:
                method,threshold=instrument.split(':');cells={}
                for cell in four.CELLS:
                    run=pair['baseline_run_id' if cell.startswith('baseline') else 'treatment_run_id']
                    version='same' if shared_version else cell.rsplit('_',1)[1]
                    identity='/'.join((run,instrument,version))
                    row=dict(id=identity,original_run_id=run,stage='early',method=method,awareness_type='evaluation',
                        threshold=threshold,context_sha256='a'*64,protocol_sha256='b'*64,label='no',status='observed',reason=None)
                    records[identity]=row
                    cells[cell]={k:row[k] for k in ('id','context_sha256','protocol_sha256')}
                pair['instruments'][instrument]=cells
            members.append(pair)
    return [dict(factor='variant',neutral_view='initial_only',members=members)],list(records.values())


def value(result, instrument='self:presence', effect='neutral_treatment_minus_baseline'):
    return result['comparisons'][0]['instruments'][instrument]['effects'][effect]


class FourCellTests(unittest.TestCase):
    def test_six_instruments_and_no_inference_fields(self):
        comparisons,rows=fixture();result=four.describe_four_cells(comparisons,rows)
        self.assertEqual(set(result['comparisons'][0]['instruments']),set(four.INSTRUMENTS))
        self.assertFalse(result['inference_released']);self.assertEqual(value(result)['equal_task_effect'],0)
        self.assertNotIn('se_upper_bound',str(result));self.assertNotIn('approximate_p',str(result))

    def test_unequal_task_draws_are_equal_task_weighted(self):
        comparisons,rows=fixture((1,3))
        for row in rows:
            if row['original_run_id']=='A0/0' and row['id'].endswith('/neutral'):row['label']='yes'
        result=four.describe_four_cells(comparisons,rows)
        self.assertEqual(value(result)['equal_task_effect'],.5)
        self.assertEqual(value(result)['exact_missing_label_bounds'],[dict(numerator=1,denominator=2)]*2)

    def test_four_cell_interaction_range_is_two(self):
        comparisons,rows=fixture()
        for row in rows:
            if (row['original_run_id'].startswith('B') and row['id'].endswith('/original')) or (
                    row['original_run_id'].startswith('A') and row['id'].endswith('/neutral')):row['label']='yes'
        result=four.describe_four_cells(comparisons,rows)
        self.assertEqual(value(result,effect='difference_of_contrasts')['equal_task_effect'],2)

    def test_all_missing_interaction_bounds(self):
        comparisons,rows=fixture()
        for row in rows:row.update(label=None,status='pending',reason='future original or derived result')
        result=four.describe_four_cells(comparisons,rows)
        self.assertEqual(value(result,effect='difference_of_contrasts')['missing_label_bounds'],[-2,2])
        self.assertIsNone(value(result)['equal_task_effect'])

    def test_shared_physical_identity_cancels_before_missing_bounds(self):
        comparisons,rows=fixture(shared_version=True)
        for row in rows:row.update(label=None,status='technical_missing',reason='same physical missing response')
        result=four.describe_four_cells(comparisons,rows)
        self.assertEqual(value(result,effect='difference_of_contrasts')['missing_label_bounds'],[0,0])
        self.assertEqual(value(result,effect='baseline_neutral_minus_original')['equal_task_effect'],0)
        self.assertEqual(value(result)['missing_label_bounds'],[-1,1])

    def test_native_categories_not_replaced_with_no(self):
        comparisons,rows=fixture()
        for row in rows:
            row['label']='insufficient_evidence' if row['method']=='self' else 'insufficient evidence to tell'
        result=four.describe_four_cells(comparisons,rows)
        cell=result['comparisons'][0]['instruments']['reader:presence']['cells']['baseline_original']
        self.assertEqual(cell['categories'],{'insufficient evidence to tell':1});self.assertEqual(cell['positive'],0)

    def test_unsupported_members_preserved_without_measurement_substitution(self):
        comparisons,rows=fixture()
        comparisons[0]['members'].append(dict(task_id='other',draw_key=0,baseline_run_id='Bother',
            treatment_run_id='Aother',disposition='unsupported',reason='unproved external startup state'))
        result=four.describe_four_cells(comparisons,rows)['comparisons'][0]
        self.assertEqual((result['fixed_pairs'],result['included_pairs']),(2,1))
        self.assertEqual(result['unsupported_members'][0]['reason'],'unproved external startup state')

    def test_unavailable_context_is_null_and_never_an_observed_or_attempted_cell(self):
        comparisons,rows=fixture();row=rows[0]
        row.update(label=None,status='unavailable',reason='original context absent',context_sha256=None)
        comparisons[0]['members'][0]['instruments']['self:presence']['baseline_original']['context_sha256']=None
        result=four.describe_four_cells(comparisons,rows)
        self.assertEqual(value(result,effect='original_treatment_minus_baseline')['missing_label_bounds'],[-1,0])
        for status in ('observed','pending','technical_missing','invalid_admission','qualification_failure'):
            row['status']=status
            with self.subTest(status=status),self.assertRaisesRegex(ValueError,'measurement_context_identity'):
                four.describe_four_cells(comparisons,rows)
        row.update(status='unavailable',context_sha256='a'*64)
        with self.assertRaisesRegex(ValueError,'measurement_context_identity'):four.describe_four_cells(comparisons,rows)

    def test_duplicate_registry_and_extra_unmapped_row_refused(self):
        comparisons,rows=fixture()
        for new in [copy.deepcopy(rows[0]),dict(rows[0],id='extra')]:
            with self.assertRaises(ValueError):four.describe_four_cells(comparisons,rows+[new])

    def test_wrong_stage_method_threshold_run_context_refused(self):
        for field,new in [('stage','end'),('method','reader'),('threshold','strongly_favors'),
                          ('original_run_id','other'),('context_sha256','f'*64)]:
            comparisons,rows=fixture();rows[0][field]=new
            with self.subTest(field=field),self.assertRaises(ValueError):four.describe_four_cells(comparisons,rows)

    def test_protocol_change_refused_even_if_cell_reference_is_updated(self):
        comparisons,rows=fixture();row=rows[0];row['protocol_sha256']='f'*64
        comparisons[0]['members'][0]['instruments']['self:presence']['baseline_original']['protocol_sha256']='f'*64
        with self.assertRaisesRegex(ValueError,'original_neutral_instrument_protocol'):four.describe_four_cells(comparisons,rows)

    def test_missing_instrument_or_wrong_missing_label_refused(self):
        comparisons,rows=fixture();del comparisons[0]['members'][0]['instruments']['reader:presence']
        with self.assertRaises(ValueError):four.describe_four_cells(comparisons,rows)
        comparisons,rows=fixture();rows[0].update(status='pending',reason='pending')
        with self.assertRaises(ValueError):four.describe_four_cells(comparisons,rows)

    def test_same_baseline_across_distinct_view_comparisons_is_not_recopied(self):
        comparisons,rows=fixture();second=copy.deepcopy(comparisons[0]);second['factor']='other'
        second['neutral_view']='grouped_startup';result=four.describe_four_cells(comparisons+[second],rows)
        self.assertEqual(result['physical_measurements'],len(rows));self.assertEqual(len(result['comparisons']),2)
        self.assertEqual([c['neutral_view'] for c in result['comparisons']],['initial_only','grouped_startup'])

    def test_duplicate_pair_and_output_limit(self):
        comparisons,rows=fixture();comparisons[0]['members']*=2
        with self.assertRaises(ValueError):four.describe_four_cells(comparisons,rows)
        comparisons,rows=fixture()
        with self.assertRaises(ValueError):four.describe_four_cells(comparisons,rows,maximum_result_bytes=1)


if __name__ == '__main__':unittest.main()
