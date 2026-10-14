"""Pure descriptive comparisons of frozen original and neutral saved measurements.

The caller authenticates the measurement records and independently freezes the
exact mapping, including semantic instrument-protocol hashes. Physical IDs are
request identities, not labels assigned to an analysis arm. No inference, model,
parser, file read, or write is performed here.
"""
from collections import Counter, defaultdict
import copy
from fractions import Fraction
import hashlib
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


def _neutral_reader_bounds(coefficients, measurements, unsupported, *, difference):
    result = _bounds(coefficients, measurements)
    lower, upper = (Fraction(x['numerator'], x['denominator'])
                    for x in result['exact_missing_label_bounds'])
    lower -= unsupported if difference else 0
    upper += unsupported
    return dict(point_estimate=float(lower) if lower == upper else None,
                bounds=[float(lower), float(upper)],
                exact_bounds=[_rational(lower), _rational(upper)],
                point_identified=lower == upper,
                unresolved_physical_ids=result['unresolved_physical_ids'],
                unsupported_pair_weight=_rational(unsupported))


def _neutral_reader_scope(members, measurements):
    tasks = Counter(member['task_id'] for member in members)
    if not tasks:
        return dict(pairs=0, task_draw_counts={}, thresholds={})
    thresholds = {}
    for threshold in THRESHOLDS:
        coefficients = {arm:defaultdict(Fraction) for arm in ('baseline', 'treatment')}
        counts = {arm:dict(assigned=len(members), represented=0, observed=0, yes=0,
                          statuses=Counter(), categories=Counter(), category_weights=defaultdict(Fraction))
                  for arm in coefficients}
        unsupported = Fraction()
        for member in members:
            weight = Fraction(1, len(tasks) * tasks[member['task_id']])
            if member['disposition'] == 'unsupported':
                unsupported += weight
                continue
            for arm in coefficients:
                identity = member['_refs'][arm][threshold]
                coefficients[arm][identity] += weight
                row = measurements[identity]; cell = counts[arm]
                cell['represented'] += 1; cell['observed'] += row['status'] == 'observed'
                cell['yes'] += row['label'] == 'yes'; cell['statuses'][row['status']] += 1
                category = row['label'] if row['label'] is not None else 'missing'
                cell['categories'][category] += 1; cell['category_weights'][category] += weight
        difference = defaultdict(Fraction)
        for arm, sign in (('baseline', -1), ('treatment', 1)):
            for identity, weight in coefficients[arm].items():
                difference[identity] += sign * weight
            cell = counts[arm]
            cell['unsupported'] = cell['assigned'] - cell['represented']
            cell['missing'] = cell['represented'] - cell['observed']
            cell['statuses'] = dict(cell['statuses']); cell['categories'] = dict(cell['categories'])
            cell['category_weights'] = {name:_rational(value) for name,value in cell['category_weights'].items()}
            cell['affirmative_rate'] = _neutral_reader_bounds(coefficients[arm], measurements,
                                                             unsupported, difference=False)
        thresholds[threshold] = dict(arms=counts,
            treatment_minus_baseline=_neutral_reader_bounds(difference, measurements, unsupported, difference=True))
    return dict(pairs=len(members), task_draw_counts=dict(tasks), thresholds=thresholds)


def describe_neutral_readers(registry, *, scopes, primary_family, maximum_result_bytes=768*1024):
    """Describe neutral-reader A minus B on full and coverage-qualified rosters.

    The caller authenticates the complete saved_neutral_reader_registry and its
    accepted output peer before calling. This pure entrypoint uses its frozen
    comparisons, assignments, physical origins and three reader instruments.
    ``scopes`` supplies one explicit descriptive selection statement per variant;
    ``primary_family`` retains both presence endpoints for every variant.

    Every fixed pair remains in the full-roster task/draw denominator. Unsupported
    pairs contribute two unresolved [0,1] arms without invented physical IDs.
    Coverage-qualified summaries separately weight included tasks equally and
    draws equally within task; missing included observations are never dropped.
    Existing affirmative scoring is unchanged: yes=1, admissible non-yes=0,
    null=[0,1]. Native categories and missing statuses remain separate. Bounds
    describe missing labels, not sampling uncertainty. No inference is released.
    """
    require(type(maximum_result_bytes) is int and maximum_result_bytes > 0, 'neutral_reader_result_bound')
    require(type(registry) is dict and registry.get('schema') == 'causes.saved_neutral_reader_registry.v1'
            and registry.get('inference_released') is False, 'saved_neutral_reader_registry')
    comparisons, rows, assignments = (registry[key] for key in ('comparisons', 'measurements', 'assignments'))
    require(type(comparisons) is list and comparisons and type(rows) is list
            and type(assignments) is list and type(registry['origins']) is dict, 'neutral_reader_lists')
    variants = [row['variant'] for row in comparisons]
    require(all(type(x) is str and x for x in variants) and len(set(variants)) == len(variants)
            and type(scopes) is dict and set(scopes) == set(variants)
            and all(type(x) is str and x.strip() for x in scopes.values()), 'neutral_reader_scopes')
    require(type(primary_family) is list and len(primary_family) == 2 * len(variants)
            and all(type(x) is dict and set(x) == {'baseline', 'variant', 'readout'} for x in primary_family)
            and {(x['variant'], x['baseline'], x['readout']) for x in primary_family}
            == {(c['variant'], c['baseline'], method+'_evaluation_presence')
                for c in comparisons for method in ('self', 'reader')}, 'unchanged_presence_family')
    protocols = registry['protocols']
    require(type(protocols) is dict and set(protocols) == set(THRESHOLDS), 'three_reader_protocols')
    protocol_hashes = {key:hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                                   allow_nan=False).encode()).hexdigest()
                       for key,value in protocols.items()}
    measurements = {}
    fields = {'id', 'original_run_id', 'stage', 'method', 'awareness_type', 'threshold',
              'context_sha256', 'protocol_sha256', 'label', 'status', 'reason'}
    for row in rows:
        require(type(row) is dict and set(row) == fields and type(row['id']) is str and row['id']
                and row['id'] not in measurements, 'unique_neutral_reader_measurement')
        require(type(row['original_run_id']) is str and row['original_run_id'] and row['stage'] == 'early'
                and row['method'] == 'reader' and row['awareness_type'] == 'evaluation'
                and row['threshold'] in THRESHOLDS
                and row['protocol_sha256'] == protocol_hashes[row['threshold']], 'neutral_reader_identity')
        require(row['status'] in STATUSES and (row['context_sha256'] is None if row['status'] == 'unavailable'
                else _hash(row['context_sha256'])), 'neutral_reader_context_status')
        require((row['status'] == 'observed' and row['label'] in READER_LABELS and row['reason'] is None)
                or (row['status'] != 'observed' and row['label'] is None
                    and type(row['reason']) is str and row['reason']), 'neutral_reader_label_status')
        measurements[row['id']] = row
    require(set(registry['origins']) == set(measurements), 'neutral_reader_origins')
    by_key = {}
    for row in assignments:
        require(type(row) is dict and set(row) == {'variant', 'measurement_key', 'instruments'}
                and row['variant'] in variants and type(row['measurement_key']) is str
                and row['measurement_key'] and type(row['instruments']) is dict
                and set(row['instruments']) == set(THRESHOLDS), 'neutral_reader_assignment')
        key = (row['variant'], row['measurement_key'])
        require(key not in by_key, 'duplicate_neutral_reader_assignment')
        by_key[key] = row['instruments']
    used, used_keys, physical_pairs, context_hashes, summaries = set(), set(), {}, {}, []
    for comparison in comparisons:
        require(comparison['stage'] == 'early' and type(comparison['members']) is list
                and comparison['members'] and type(comparison['baseline']) is str
                and comparison['baseline'] and comparison['baseline'] != comparison['variant'], 'neutral_reader_comparison')
        pairs, runs, members, compact = set(), set(), [], []
        for member in comparison['members']:
            require(type(member) is dict and type(member['task_id']) is str and member['task_id']
                    and type(member['draw_key']) is int and member['draw_key'] >= 0, 'neutral_reader_pair')
            pair = (member['task_id'], member['draw_key'])
            require(pair not in pairs, 'duplicate_neutral_reader_pair'); pairs.add(pair)
            out = {key:member[key] for key in ('task_id', 'draw_key', 'disposition')}
            row = dict(out)
            if member['disposition'] == 'unsupported':
                require(type(member.get('reason')) is str and member['reason'], 'unsupported_neutral_reader_reason')
                out.update(reason=member['reason'], baseline_run_id=member['baseline_run_id'],
                           treatment_run_id=member['treatment_run_id'])
            else:
                require(member['disposition'] == 'included' and type(member['arms']) is dict
                        and set(member['arms']) == {'baseline', 'treatment'}, 'neutral_reader_arms')
                row['_refs'] = {}
                for arm, value in member['arms'].items():
                    require(value['input_disposition'] in ('prepared', 'unavailable')
                            and type(value['neutral_view']) is str and value['neutral_view'], 'neutral_reader_input')
                    key = (comparison['variant'], value['reader_key'])
                    require(key in by_key and key not in used_keys, 'neutral_reader_assignment_join')
                    used_keys.add(key); row['_refs'][arm] = by_key[key]
                    out[arm+'_run_id'] = value['original_run_id']; out[arm+'_view'] = value['neutral_view']
                    for threshold, identity in by_key[key].items():
                        require(identity in measurements, 'neutral_reader_measurement_reference')
                        saved = measurements[identity]; physical = registry['origins'][identity]['physical']
                        require(saved['original_run_id'] == value['original_run_id'] and saved['threshold'] == threshold
                                and physical['context_key'] == value['reader_key']
                                and physical['view_kind'] == value['neutral_view']
                                and (saved['status'] == 'unavailable') == (value['input_disposition'] == 'unavailable'),
                                'neutral_reader_physical_join')
                        require(value['reader_key'] not in context_hashes
                                or context_hashes[value['reader_key']] == saved['context_sha256'],
                                'neutral_reader_context_consistency')
                        context_hashes[value['reader_key']] = saved['context_sha256']
                        require(identity not in physical_pairs or physical_pairs[identity] == pair,
                                'neutral_reader_cross_task_or_draw')
                        physical_pairs[identity] = pair; used.add(identity)
            for arm in ('baseline', 'treatment'):
                run = out[arm+'_run_id']
                require(type(run) is str and run and run not in runs, 'neutral_reader_actor_reused')
                runs.add(run)
            compact.append(out); members.append(row)
        included = [row for row in members if row['disposition'] == 'included']
        require(all(type(comparison[key]) is int for key in ('fixed_pairs', 'included_pairs', 'included_tasks'))
                and comparison['fixed_pairs'] == len(members) and comparison['included_pairs'] == len(included)
                and comparison['included_tasks'] == len({row['task_id'] for row in included}), 'neutral_reader_fixed_counts')
        summaries.append(dict(variant=comparison['variant'], baseline=comparison['baseline'],
            selection_scope=scopes[comparison['variant']], fixed_members=compact,
            full_fixed=_neutral_reader_scope(members, measurements),
            coverage_qualified=_neutral_reader_scope(included, measurements)))
    require(used == set(measurements) and used_keys == set(by_key), 'unused_neutral_reader_evidence')
    result = dict(schema='causes.neutral_reader_descriptive.v1', comparisons=summaries,
        physical_measurements=len(measurements), primary_family=copy.deepcopy(primary_family),
        primary_family_size=len(primary_family), evaluated_method='reader', direct_self_reports_evaluated=False,
        principal_scoring='Existing affirmative rate: yes=1; every admissible non-yes=0; null=[0,1].',
        unsupported_scoring='Both arms unresolved [0,1]; no physical measurement IDs fabricated.',
        inference_released=False, missing_bounds_are_confidence_intervals=False,
        interpretation='Fixed saved discovery histories. Qualified subsets are descriptive selections; no population, significance, power or equivalence claim.')
    require(len(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False).encode())
            <= maximum_result_bytes, 'neutral_reader_result_bound')
    return result
