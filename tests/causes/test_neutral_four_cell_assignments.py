import copy
from fractions import Fraction
import itertools
import json
import unittest

from causes.neutral_four_cell import CELLS, COEFFICIENTS, INSTRUMENTS
from causes.neutral_four_cell_assignments import describe_four_cells_with_assignments


PIN = dict(path='/synthetic/frozen.json', bytes=1, sha256='a' * 64)
CONTEXT = 'b' * 64
PROTOCOL = 'c' * 64


def member(factor, task='task', draw=0, included=True):
    runs = dict(baseline=f'base-{task}-{draw}', treatment=f'{factor}-{task}-{draw}')
    instruments = {}
    for instrument in INSTRUMENTS:
        method, threshold = instrument.split(':')
        instruments[instrument] = {}
        for name in CELLS:
            arm, view = name.split('_')
            view = 'original' if view == 'original' else 'neutral'
            run = runs[arm]
            instruments[instrument][name] = dict(id=None, identity_kind=None,
                state='pending', reason='no accepted result yet', context_sha256=CONTEXT,
                protocol_sha256=PROTOCOL,
                slot=dict(key=f'{run}/{view}/{instrument}', run_id=run, view=view,
                    stage='early', method=method, threshold=threshold, source=copy.deepcopy(PIN)),
                qualification=dict(state='qualified', proof=copy.deepcopy(PIN), reason=None),
                logical_alias=None)
    return dict(task_id=task, draw_key=draw, baseline_run_id=runs['baseline'],
        treatment_run_id=runs['treatment'], disposition='included' if included else 'unsupported',
        reason=None if included else 'frozen view unavailable', instruments=instruments)


def comparison(factor='variant', members=None):
    return dict(factor=factor, baseline='control', neutral_view='neutral', scope='frozen synthetic cohort',
                neutral_views_by_arm=dict(baseline='neutral', treatment='neutral'),
                members=members if members is not None else [member(factor)])


def family(comparisons):
    return [dict(baseline=x['baseline'], variant=x['factor'], readout=method + '_evaluation_presence')
            for x in comparisons for method in ('self', 'reader')]


def present(ref, registry, *, identity=None, kind='native_record', status='observed', label='no'):
    identity = identity or ref['slot']['key']
    ref.update(id=identity, identity_kind=kind, state='present', reason=None, logical_alias=None)
    if status == 'unavailable':
        ref['context_sha256'] = None
    row = dict(id=identity, identity_kind=kind, original_run_id=ref['slot']['run_id'],
        stage='early', method=ref['slot']['method'], awareness_type='evaluation', threshold=ref['slot']['threshold'],
        context_sha256=ref['context_sha256'], protocol_sha256=ref['protocol_sha256'],
        status=status, label=label if status == 'observed' else None,
        reason=None if status == 'observed' else 'retained native missing reason')
    if identity in {x['id'] for x in registry}:
        assert next(x for x in registry if x['id'] == identity) == row
    else:
        registry.append(row)
    return row


def filled(members=None, factor='variant'):
    c = comparison(factor, members)
    registry = []
    for m in c['members']:
        for cells in m['instruments'].values():
            for ref in cells.values():
                present(ref, registry)
    return [c], registry


def run(comparisons, registry, **kwargs):
    return describe_four_cells_with_assignments(comparisons, registry, primary_family=family(comparisons), **kwargs)


def bounds(summary, effect='difference_of_contrasts', instrument='self:presence', scope='full_fixed'):
    value = summary['comparisons'][0][scope]['instruments'][instrument]['effects'][effect]
    return tuple(Fraction(x['numerator'], x['denominator']) for x in value['exact_missing_label_bounds'])


class AssignmentConsumerTest(unittest.TestCase):
    def test_null_unavailable_requires_frozen_availability_and_no_context(self):
        c = comparison()
        ref = c['members'][0]['instruments']['self:presence']['treatment_neutral']
        ref.update(state='unavailable', context_sha256=None, reason='frozen input context unavailable')
        result = run([c], [])
        cell = result['comparisons'][0]['full_fixed']['instruments']['self:presence']['cells']['treatment_neutral']
        self.assertEqual(cell['states'], {'unavailable': 1})
        self.assertEqual(cell['unique_registry_ids'], 0)
        ref['context_sha256'] = CONTEXT
        with self.assertRaisesRegex(ValueError, 'frozen_unavailable_input_proof'):
            run([c], [])
        ref['context_sha256'] = None
        ref['qualification'] = dict(state='unresolved', proof=None, reason='unproven input availability')
        with self.assertRaisesRegex(ValueError, 'frozen_unavailable_input_proof'):
            run([c], [])

    def test_distinct_baseline_and_external_treatment_views_preserved(self):
        comparisons, registry = filled()
        c = comparisons[0]
        c['neutral_view'] = 'grouped_startup'
        c['neutral_views_by_arm'] = dict(baseline='initial_summary', treatment='grouped_startup')
        for cells in c['members'][0]['instruments'].values():
            for arm, view in c['neutral_views_by_arm'].items():
                slot = cells[arm + '_neutral']['slot']
                slot['view'] = view
                slot['key'] += '/' + view
        result = run(comparisons, registry)
        self.assertEqual(result['comparisons'][0]['neutral_views_by_arm'], c['neutral_views_by_arm'])
        self.assertEqual(bounds(result), (0, 0))
        c['members'][0]['instruments']['reader:presence']['baseline_neutral']['slot']['view'] = 'grouped_startup'
        with self.assertRaisesRegex(ValueError, 'frozen_slot_identity'):
            run(comparisons, registry)

    def test_neutral_view_roster_and_treatment_label_must_join(self):
        c = comparison()
        c['neutral_views_by_arm'].pop('baseline')
        with self.assertRaisesRegex(ValueError, 'exact_neutral_views_by_arm'):
            run([c], [])
        c = comparison()
        c['neutral_views_by_arm']['treatment'] = 'different'
        with self.assertRaisesRegex(ValueError, 'exact_neutral_views_by_arm'):
            run([c], [])

    def test_all_five_vectors_and_six_instruments(self):
        comparisons, registry = filled()
        labels = ('no', 'yes', 'yes', 'no')
        rows = {r['id']: r for r in registry}
        for cells in comparisons[0]['members'][0]['instruments'].values():
            for name, label in zip(CELLS, labels):
                rows[cells[name]['id']]['label'] = label
        result = run(comparisons, registry)
        expected = (1, -1, 1, -1, -2)
        for instrument in INSTRUMENTS:
            for effect, value in zip(COEFFICIENTS, expected):
                self.assertEqual(bounds(result, effect, instrument), (value, value))
        self.assertFalse(result['inference_released'])
        self.assertFalse(result['historical_provider_weights_equated'])

    def test_native_categories_preserved_with_affirmative_scoring(self):
        comparisons, registry = filled()
        for row in registry:
            row['label'] = 'insufficient_evidence' if row['method'] == 'self' else 'not directly interpretable as such'
        result = run(comparisons, registry)
        for instrument in INSTRUMENTS:
            table = result['comparisons'][0]['full_fixed']['instruments'][instrument]['cells']['baseline_original']
            label = 'insufficient_evidence' if instrument.startswith('self:') else 'not directly interpretable as such'
            self.assertEqual(table['categories'], {label: 1})
            self.assertEqual((table['observed'], table['positive'], table['missing']), (1, 0, 0))
            self.assertEqual(table['affirmative_rate']['missing_label_bounds'], [0.0, 0.0])

    def test_explicit_null_states_and_existing_unavailable_distinct(self):
        c = comparison()
        registry = []
        cells = c['members'][0]['instruments']['self:presence']
        cells['baseline_original']['state'] = 'not_assigned'
        cells['treatment_original']['state'] = 'absent_instance'
        present(cells['baseline_neutral'], registry, kind='assigned_slot', status='unavailable')
        cells['treatment_neutral']['state'] = 'not_in_resolved_inventory'
        c['members'][0]['instruments']['reader:presence']['treatment_neutral']['state'] = 'unsupported'
        result = run([c], registry)
        tables = result['comparisons'][0]['full_fixed']['instruments']['self:presence']['cells']
        self.assertEqual(tables['baseline_original']['states'], {'not_assigned': 1})
        self.assertEqual(tables['treatment_original']['states'], {'absent_instance': 1})
        self.assertEqual(tables['baseline_neutral']['statuses'], {'unavailable': 1})
        self.assertEqual(tables['baseline_neutral']['kinds'], {'assigned_slot': 1})
        self.assertEqual(tables['treatment_neutral']['states'], {'not_in_resolved_inventory': 1})
        self.assertEqual(result['unique_registry_records'], 1)
        self.assertEqual(bounds(result), (-2, 2))

    def test_unrelated_nulls_never_cancel(self):
        c = comparison()
        result = run([c], [])
        self.assertEqual(bounds(result), (-2, 2))
        effect = result['comparisons'][0]['full_fixed']['instruments']['self:presence']['effects']['difference_of_contrasts']
        self.assertEqual(len(effect['unresolved_coefficients']), 4)
        self.assertEqual(len({x[0] for x in effect['unresolved_coefficients']}), 4)
        self.assertTrue(all(result['uncertainty_variables'][x[0]]['id'] is None
                            for x in effect['unresolved_coefficients']))

    def test_existing_identity_kinds_cancel_exactly(self):
        for kind in ('physical_request', 'native_record', 'assigned_slot'):
            with self.subTest(kind=kind):
                c = comparison()
                registry = []
                for cells in c['members'][0]['instruments'].values():
                    for arm in ('baseline', 'treatment'):
                        for view in ('original', 'neutral'):
                            ref = cells[arm + '_' + view]
                            present(ref, registry, identity=ref['slot']['run_id'] + '/' + ref['slot']['method'] + '/' + ref['slot']['threshold'],
                                    kind=kind, status='pending' if kind == 'assigned_slot' else 'technical_missing')
                result = run([c], registry)
                self.assertEqual(bounds(result), (0, 0))
                self.assertEqual(bounds(result, 'original_treatment_minus_baseline'), (-1, 1))
                self.assertEqual(result['registry_counts'], {kind: 12})

    def alias_baseline(self, c):
        cells = c['members'][0]['instruments']['self:presence']
        keys = [cells[name]['slot']['key'] for name in ('baseline_original', 'baseline_neutral')]
        alias = dict(key='baseline-same-unresolved-endpoint', slot_keys=keys, proof=copy.deepcopy(PIN))
        for name in ('baseline_original', 'baseline_neutral'):
            cells[name]['logical_alias'] = copy.deepcopy(alias)
        return cells

    def test_explicit_nonphysical_alias_cancels_only_proven_endpoints(self):
        c = comparison()
        self.alias_baseline(c)
        result = run([c], [])
        self.assertEqual(bounds(result), (-1, 1))
        self.assertEqual(bounds(result, 'baseline_neutral_minus_original'), (0, 0))
        self.assertEqual(result['logical_alias_groups'], 1)
        self.assertEqual(result['unique_registry_records'], 0)

    def test_alias_rejects_missing_proof_incomplete_roster_and_wrong_identity(self):
        for mutation, reason in (
            (lambda cells: cells['baseline_neutral']['logical_alias'].update(proof=None), 'frozen_proof_reference'),
            (lambda cells: cells['baseline_neutral'].update(logical_alias=None), 'logical_alias_complete_membership'),
            (lambda cells: cells['baseline_neutral'].update(context_sha256='d' * 64), 'logical_alias_identity'),
            (lambda cells: cells['baseline_neutral'].update(state='not_assigned'), 'logical_alias_identity'),
        ):
            c = comparison()
            cells = self.alias_baseline(c)
            mutation(cells)
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                run([c], [])

    def test_unsupported_known_cells_refine_full_bounds_without_changing_qualified_roster(self):
        ms = [member('variant', 'a'), member('variant', 'b', included=False)]
        comparisons, registry = filled([ms[0]])
        comparisons[0]['members'].append(ms[1])
        for cells in ms[1]['instruments'].values():
            for name in ('baseline_original', 'baseline_neutral'):
                present(cells[name], registry)
        result = run(comparisons, registry)
        full, qualified = (result['comparisons'][0][key] for key in ('full_fixed', 'coverage_qualified'))
        self.assertEqual((full['pairs'], qualified['pairs']), (2, 1))
        self.assertEqual(bounds(result, 'neutral_treatment_minus_baseline'), (0, Fraction(1, 2)))
        self.assertEqual(bounds(result, 'neutral_treatment_minus_baseline', scope='coverage_qualified'), (0, 0))
        self.assertIn('refine earlier', result['full_bound_method'])
        self.assertEqual(result['comparisons'][0]['fixed_members'][1]['reason'], 'frozen view unavailable')

    def test_missing_qualified_members_remain_in_denominator(self):
        c = comparison(members=[member('variant', 'a'), member('variant', 'b')])
        registry = []
        for cells in c['members'][0]['instruments'].values():
            for ref in cells.values():
                present(ref, registry)
        result = run([c], registry)
        self.assertEqual(result['comparisons'][0]['coverage_qualified']['pairs'], 2)
        self.assertEqual(bounds(result, 'neutral_treatment_minus_baseline', scope='coverage_qualified'), (Fraction(-1, 2), Fraction(1, 2)))

    def test_variable_task_weights_match_exhaustive_binary_assignments(self):
        ms = [member('variant', 'a', 0), member('variant', 'a', 1), member('variant', 'b', 0)]
        comparisons, registry = filled(ms)
        rows = {x['id']: x for x in registry}
        unknown_ids = []
        instrument = 'reader:presence'
        weights = [Fraction(1, 4), Fraction(1, 4), Fraction(1, 2)]
        for i, m in enumerate(ms):
            cells = m['instruments'][instrument]
            rows[cells['baseline_original']['id']]['label'] = 'yes' if i == 0 else 'no'
            for name in (('treatment_original',) if i == 0 else ('treatment_neutral',)):
                row = rows[cells[name]['id']]
                row.update(status='technical_missing', label=None, reason='native failure retained')
                unknown_ids.append(row['id'])
        result = run(comparisons, registry)
        for effect, vector in COEFFICIENTS.items():
            values = []
            for bits in itertools.product((0, 1), repeat=len(unknown_ids)):
                assignments = dict(zip(unknown_ids, bits))
                total = Fraction()
                for m, weight in zip(ms, weights):
                    for name, coefficient in zip(CELLS, vector):
                        row = rows[m['instruments'][instrument][name]['id']]
                        value = assignments[row['id']] if row['id'] in assignments else int(row['label'] == 'yes')
                        total += coefficient * weight * value
                values.append(total)
            self.assertEqual(bounds(result, effect, instrument), (min(values), max(values)))

    def test_five_frozen_size_cohorts_and_unequal_qualified_draws(self):
        comparisons = []
        frequencies = [1] * 16 + [2] * 7 + [3] * 9 + [4] * 2 + [5]
        for factor in ('a', 'b', 'c', 'subset', 'external'):
            ms = []
            for task in range(60):
                for draw in range(1 if factor == 'external' else 5):
                    included = factor in ('a', 'b', 'c') or (factor == 'external' and task < 3) or (
                        factor == 'subset' and task < len(frequencies) and draw < frequencies[task])
                    ms.append(member(factor, str(task), draw, included))
            comparisons.append(comparison(factor, ms))
        result = run(comparisons, [])
        self.assertEqual(sum(c['full_fixed']['pairs'] for c in result['comparisons']), 1260)
        self.assertEqual(sum(c['coverage_qualified']['pairs'] for c in result['comparisons']), 973)
        subset = result['comparisons'][3]
        self.assertEqual((subset['full_fixed']['tasks'], subset['coverage_qualified']['tasks']), (60, 35))
        self.assertEqual(sorted(subset['coverage_qualified']['task_draw_counts'].values()), sorted(frequencies))
        self.assertEqual(len(result['primary_family']), 10)
        self.assertLess(len(json.dumps(result, sort_keys=True, allow_nan=False).encode()), 16 * 1024**2)

    def test_unresolved_protocol_does_not_claim_point_identification(self):
        c = comparison()
        cell = c['members'][0]['instruments']['self:presence']['baseline_original']
        cell.update(context_sha256=None, protocol_sha256=None,
                    qualification=dict(state='unresolved', proof=None, reason='no accepted semantic join'))
        result = run([c], [])
        effect = result['comparisons'][0]['full_fixed']['instruments']['self:presence']['effects']['difference_of_contrasts']
        self.assertFalse(effect['configured_instrument_qualified'])
        self.assertFalse(effect['point_identified'])
        self.assertEqual(len(effect['unresolved_semantic_slots']), 1)

    def test_present_observation_cannot_have_unresolved_join(self):
        comparisons, registry = filled()
        ref = comparisons[0]['members'][0]['instruments']['self:presence']['baseline_original']
        ref['qualification'] = dict(state='unresolved', proof=None, reason='unknown')
        with self.assertRaisesRegex(ValueError, 'present_record_join'):
            run(comparisons, registry)

    def test_wrong_stage_run_context_protocol_and_instrument_rejected(self):
        mutations = (
            (lambda ref: ref['slot'].update(stage='end'), 'frozen_slot_identity'),
            (lambda ref: ref['slot'].update(run_id='different'), 'frozen_slot_identity'),
            (lambda ref: ref['slot'].update(threshold='strongly_favors'), 'frozen_slot_identity'),
            (lambda ref: ref.update(context_sha256='d' * 64), 'cell_registry_identity'),
            (lambda ref: ref.update(protocol_sha256='d' * 64), 'cell_registry_identity'),
        )
        for mutate, reason in mutations:
            comparisons, registry = filled()
            mutate(comparisons[0]['members'][0]['instruments']['self:presence']['baseline_original'])
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                run(comparisons, registry)

    def test_configured_protocol_difference_cannot_be_silently_combined(self):
        c = comparison()
        c['members'][0]['instruments']['reader:presence']['baseline_neutral']['protocol_sha256'] = 'd' * 64
        with self.assertRaisesRegex(ValueError, 'configured_instrument_protocol_mismatch'):
            run([c], [])

    def test_unassigned_with_record_and_nonphysical_observation_refuse(self):
        comparisons, registry = filled()
        ref = comparisons[0]['members'][0]['instruments']['self:presence']['baseline_original']
        ref['state'] = 'not_assigned'
        with self.assertRaisesRegex(ValueError, 'nonphysical_null_slot'):
            run(comparisons, registry)
        comparisons, registry = filled()
        registry[0]['identity_kind'] = 'assigned_slot'
        with self.assertRaisesRegex(ValueError, 'nonphysical_assigned_slot'):
            run(comparisons, registry)

    def test_duplicate_or_repeated_actor_and_incomplete_ledger_refuse(self):
        c = comparison()
        c['members'].append(copy.deepcopy(c['members'][0]))
        with self.assertRaisesRegex(ValueError, 'duplicate_pair'):
            run([c], [])
        c['members'][1]['task_id'] = 'other'
        with self.assertRaisesRegex(ValueError, 'actor_repeated_within_comparison'):
            run([c], [])
        c = comparison()
        del c['members'][0]['instruments']['self:presence']['baseline_original']
        with self.assertRaisesRegex(ValueError, 'four_cell_membership'):
            run([c], [])
        c = comparison()
        del c['members'][0]['instruments']['reader:presence']
        with self.assertRaisesRegex(ValueError, 'six_instrument_membership'):
            run([c], [])

    def test_extra_registry_and_duplicate_registry_refuse(self):
        comparisons, registry = filled()
        extra = copy.deepcopy(registry[0])
        extra['id'] = 'unused'
        with self.assertRaisesRegex(ValueError, 'extra_or_unmapped_registry_records'):
            run(comparisons, registry + [extra])
        with self.assertRaisesRegex(ValueError, 'unique_registry_record'):
            run(comparisons, registry + [registry[0]])

    def test_shared_control_ids_across_comparisons_keep_same_task_draw(self):
        first, registry = filled(factor='a')
        second, registry2 = filled(factor='b')
        unique = {r['id']: r for r in registry + registry2}
        result = run(first + second, list(unique.values()))
        self.assertEqual(result['unique_registry_records'], 36)
        second[0]['members'][0]['task_id'] = 'other'
        with self.assertRaisesRegex(ValueError, 'registry_id_cross_task_or_draw'):
            run(first + second, list(unique.values()))

    def test_shared_logical_slot_cannot_change_identity(self):
        a, b = comparison('a'), comparison('b')
        b['members'][0]['instruments']['self:presence']['baseline_original']['state'] = 'not_assigned'
        with self.assertRaisesRegex(ValueError, 'conflicting_logical_slot'):
            run([a, b], [])

    def test_failed_native_label_and_null_context_validation(self):
        comparisons, registry = filled()
        registry[0]['status'] = 'technical_missing'
        with self.assertRaisesRegex(ValueError, 'registry_label_status'):
            run(comparisons, registry)
        comparisons, registry = filled()
        registry[0]['context_sha256'] = None
        with self.assertRaisesRegex(ValueError, 'registry_context'):
            run(comparisons, registry)

    def test_no_mutation_and_exact_result_bound(self):
        comparisons, registry = filled()
        before = copy.deepcopy((comparisons, registry))
        result = run(comparisons, registry)
        self.assertEqual((comparisons, registry), before)
        size = len(json.dumps(result, sort_keys=True, allow_nan=False).encode())
        self.assertEqual(run(comparisons, registry, maximum_result_bytes=size), result)
        with self.assertRaisesRegex(ValueError, 'serialized_result_bound'):
            run(comparisons, registry, maximum_result_bytes=size - 1)
        with self.assertRaisesRegex(ValueError, 'result_limit'):
            run(comparisons, registry, maximum_result_bytes=True)

    def test_primary_family_cannot_omit_or_expand_endpoints(self):
        comparisons, registry = filled()
        for value in ([], family(comparisons) + family(comparisons)):
            with self.assertRaisesRegex(ValueError, 'unchanged_primary_presence_family'):
                describe_four_cells_with_assignments(comparisons, registry, primary_family=value)


if __name__ == '__main__':
    unittest.main()
