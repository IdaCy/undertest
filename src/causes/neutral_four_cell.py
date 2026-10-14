"""Pure descriptive comparisons of frozen original and neutral saved measurements.

The caller authenticates the measurement records and independently freezes the
exact mapping, including semantic instrument-protocol hashes. Physical IDs are
request identities, not labels assigned to an analysis arm. No inference, model,
parser, file read, or write is performed here.
"""
from collections import Counter, defaultdict
import copy
from fractions import Fraction
import json
import re

from causes.campaign_analysis import SELF_LABELS, READER_LABELS, binary


THRESHOLDS = ('presence', 'strongly_favors', 'leans_towards_or_favors')
INSTRUMENTS = tuple(method + ':' + level for method in ('self', 'reader') for level in THRESHOLDS)
CELLS = ('baseline_original', 'treatment_original', 'baseline_neutral', 'treatment_neutral')
COEFFICIENTS = {
    'original_treatment_minus_baseline': (-1, 1, 0, 0),
    'neutral_treatment_minus_baseline': (0, 0, -1, 1),
    'baseline_neutral_minus_original': (-1, 0, 1, 0),
    'treatment_neutral_minus_original': (0, -1, 0, 1),
    'difference_of_contrasts': (1, -1, -1, 1),
}
STATUSES = ('observed', 'technical_missing', 'unavailable', 'pending',
            'invalid_admission', 'qualification_failure')


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def _hash(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _rational(value):
    return dict(numerator=value.numerator, denominator=value.denominator)


def _bounds(coefficients, measurements):
    lower = upper = Fraction()
    unresolved = []
    for identity, coefficient in sorted(coefficients.items()):
        if not coefficient:
            continue
        value = binary(measurements[identity]['label'])
        if value is None:
            lower += min(0, coefficient); upper += max(0, coefficient)
            unresolved.append(identity)
        else:
            lower += coefficient * value; upper += coefficient * value
    return dict(equal_task_effect=float(lower) if lower == upper else None,
                exact_missing_label_bounds=[_rational(lower), _rational(upper)],
                missing_label_bounds=[float(lower), float(upper)],
                point_identified=lower == upper, unresolved_physical_ids=unresolved)


def describe_four_cells(comparisons, measurements, *, maximum_result_bytes=12*1024**2):
    """Join all six instruments, preserving unsupported members and shared IDs.

    Each comparison has ``factor``, ``neutral_view`` and all fixed ``members``.
    A member binds task_id, draw_key, baseline_run_id, treatment_run_id and
    disposition. Included members supply six instrument maps, each with four
    cells {id, context_sha256, protocol_sha256}; unsupported members instead
    have a nonempty reason. Within each arm, original and neutral use exactly
    the same semantic instrument protocol. This protocol hash must be derived
    from the accepted instrument/config, not from a view or analysis name.

    Every measurement supplies id, original_run_id, stage='early', method,
    awareness_type='evaluation', threshold, context_sha256, protocol_sha256,
    label, status and reason. Unavailable inputs alone have a null context hash;
    their IDs identify assigned slots, not fabricated requests. The complete
    supplied registry must be used.
    Repeated physical IDs are counted once for uncertainty bounds after their
    signed coefficients are combined. All five summaries are descriptive;
    missing-label bounds are not confidence intervals.
    """
    require(type(comparisons) is list and comparisons and type(measurements) is list,
            'comparison_and_measurement_lists')
    registry = {}
    measurement_fields = {'id', 'original_run_id', 'stage', 'method', 'awareness_type', 'threshold',
                          'context_sha256', 'protocol_sha256', 'label', 'status', 'reason'}
    for row in measurements:
        require(type(row) is dict and set(row) == measurement_fields
                and type(row['id']) is str and row['id'] and row['id'] not in registry,
                'unique_physical_measurement')
        require(type(row['original_run_id']) is str and row['original_run_id'] and row['stage'] == 'early'
                and row['method'] in ('self', 'reader') and row['awareness_type'] == 'evaluation'
                and row['threshold'] in THRESHOLDS
                and _hash(row['protocol_sha256']), 'measurement_identity')
        require(row['status'] in STATUSES, 'measurement_status')
        require(row['context_sha256'] is None if row['status'] == 'unavailable'
                else _hash(row['context_sha256']), 'measurement_context_identity')
        labels = SELF_LABELS if row['method'] == 'self' else READER_LABELS
        require((row['status'] == 'observed' and row['label'] in labels and row['reason'] is None)
                or (row['status'] != 'observed' and row['label'] is None
                    and type(row['reason']) is str and row['reason']), 'label_status_identity')
        registry[row['id']] = row
    factors, used, physical_tasks, prepared = set(), set(), {}, []
    for comparison in comparisons:
        require(type(comparison) is dict and set(comparison) == {'factor', 'neutral_view', 'members'}
                and type(comparison['factor']) is str and comparison['factor']
                and comparison['factor'] not in factors and type(comparison['neutral_view']) is str
                and comparison['neutral_view'] and type(comparison['members']) is list
                and comparison['members'], 'frozen_comparison')
        factors.add(comparison['factor']); pairs = set(); tasks = defaultdict(list); unsupported = []
        run_tasks = {}
        for member in comparison['members']:
            common = {'task_id', 'draw_key', 'baseline_run_id', 'treatment_run_id', 'disposition'}
            require(type(member) is dict and common <= set(member)
                    and type(member['task_id']) is str and member['task_id']
                    and type(member['draw_key']) is int and member['draw_key'] >= 0
                    and type(member['baseline_run_id']) is str and member['baseline_run_id']
                    and type(member['treatment_run_id']) is str and member['treatment_run_id']
                    and member['baseline_run_id'] != member['treatment_run_id'], 'frozen_pair_identity')
            pair = (member['task_id'], member['draw_key'])
            require(pair not in pairs, 'duplicate_pair'); pairs.add(pair)
            for key in ('baseline_run_id', 'treatment_run_id'):
                run = member[key]
                require(run not in run_tasks, 'physical_actor_repeated_within_comparison')
                run_tasks[run] = pair
            if member['disposition'] == 'unsupported':
                require(set(member) == common | {'reason'} and type(member['reason']) is str and member['reason'],
                        'unsupported_reason')
                unsupported.append(copy.deepcopy(member)); continue
            require(member['disposition'] == 'included' and set(member) == common | {'instruments'}
                    and type(member['instruments']) is dict and set(member['instruments']) == set(INSTRUMENTS),
                    'six_instrument_membership')
            for instrument, cells in member['instruments'].items():
                method, threshold = instrument.split(':')
                require(type(cells) is dict and set(cells) == set(CELLS), 'four_cell_membership')
                for name, reference in cells.items():
                    require(type(reference) is dict and set(reference) == {'id', 'context_sha256', 'protocol_sha256'}
                            and reference['id'] in registry, 'cell_measurement_reference')
                    row = registry[reference['id']]
                    run = member['baseline_run_id'] if name.startswith('baseline_') else member['treatment_run_id']
                    require(row['original_run_id'] == run and row['method'] == method and row['threshold'] == threshold
                            and row['context_sha256'] == reference['context_sha256']
                            and row['protocol_sha256'] == reference['protocol_sha256'], 'cell_identity_join')
                    identity = reference['id']; used.add(identity)
                    require(identity not in physical_tasks or physical_tasks[identity] == pair,
                            'physical_measurement_cross_task_or_draw')
                    physical_tasks[identity] = pair
                for arm in ('baseline', 'treatment'):
                    require(cells[arm+'_original']['protocol_sha256'] == cells[arm+'_neutral']['protocol_sha256'],
                            'original_neutral_instrument_protocol')
            tasks[member['task_id']].append(member)
        require(tasks, 'no_supported_pairs')
        prepared.append((comparison, tasks, unsupported))
    require(used == set(registry), 'extra_or_unmapped_measurements')
    summaries = []
    for comparison, tasks, unsupported in prepared:
        instruments = {}
        for instrument in INSTRUMENTS:
            coefficients = {name: defaultdict(Fraction) for name in COEFFICIENTS}
            cells = {name: dict(assigned=0, observed=0, positive=0, statuses=Counter(), categories=Counter(),
                               category_weights=defaultdict(Fraction)) for name in CELLS}
            transitions = {arm: Counter() for arm in ('baseline', 'treatment')}
            for task, members in tasks.items():
                weight = Fraction(1, len(tasks)*len(members))
                for member in members:
                    refs = member['instruments'][instrument]
                    for name, coefficient_vector in COEFFICIENTS.items():
                        for cell, coefficient in zip(CELLS, coefficient_vector):
                            coefficients[name][refs[cell]['id']] += weight * coefficient
                    for name in CELLS:
                        row = registry[refs[name]['id']]; cell = cells[name]
                        cell['assigned'] += 1; cell['observed'] += row['status'] == 'observed'
                        cell['positive'] += row['label'] == 'yes'; cell['statuses'][row['status']] += 1
                        category = row['label'] if row['label'] is not None else 'missing'
                        cell['categories'][category] += 1; cell['category_weights'][category] += weight
                    for arm in transitions:
                        original = registry[refs[arm+'_original']['id']]; neutral = registry[refs[arm+'_neutral']['id']]
                        transitions[arm][(original['label'], neutral['label'], original['status'], neutral['status'])] += 1
            for cell in cells.values():
                cell['missing'] = cell['assigned'] - cell['observed']
                cell['statuses'] = dict(cell['statuses']); cell['categories'] = dict(cell['categories'])
                cell['category_weights'] = {k:_rational(v) for k,v in cell['category_weights'].items()}
            instruments[instrument] = dict(cells=cells,
                effects={name:_bounds(values, registry) for name,values in coefficients.items()},
                transitions={arm:[dict(original_label=k[0], neutral_label=k[1], original_status=k[2],
                                       neutral_status=k[3], count=n) for k,n in table.items()]
                             for arm,table in transitions.items()})
        summaries.append(dict(factor=comparison['factor'], neutral_view=comparison['neutral_view'],
            fixed_pairs=len(comparison['members']), included_pairs=sum(map(len, tasks.values())),
            task_draw_counts={task:len(rows) for task,rows in tasks.items()},
            unsupported_members=unsupported, instruments=instruments))
    result = dict(schema='causes.neutral_four_cell_descriptive.v1', comparisons=summaries,
                  physical_measurements=len(registry), inference_released=False,
                  missing_bounds_are_confidence_intervals=False,
                  interpretation='Fixed selected saved histories; no significance, equivalence, power, or population claim.')
    require(type(maximum_result_bytes) is int and maximum_result_bytes > 0
            and len(json.dumps(result, sort_keys=True, allow_nan=False).encode()) <= maximum_result_bytes,
            'four_cell_result_bound')
    return result
