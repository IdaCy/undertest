"""Pure descriptive arithmetic over a frozen four-cell assignment ledger.

Callers authenticate registry, membership, protocol and alias proofs before use.
Proof references here are immutable identities, not acceptance inferred from a
filename. This module performs no reads, parsing, requests or qualification.
"""
from collections import Counter, defaultdict
import copy
from fractions import Fraction
import json
import re

from causes.campaign_analysis import SELF_LABELS, READER_LABELS, binary
from causes.neutral_four_cell import CELLS, COEFFICIENTS, INSTRUMENTS, STATUSES, require


KINDS = ('physical_request', 'native_record', 'assigned_slot')
NULL_STATES = ('not_assigned', 'absent_instance', 'pending', 'unsupported', 'not_in_resolved_inventory', 'unavailable')
MEASUREMENT_FIELDS = {'id', 'identity_kind', 'original_run_id', 'stage', 'method', 'awareness_type',
                      'threshold', 'context_sha256', 'protocol_sha256', 'label', 'status', 'reason'}


def _hash(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _text(value):
    return type(value) is str and bool(value.strip())


def _pin(value):
    require(type(value) is dict and set(value) == {'path', 'bytes', 'sha256'}
            and _text(value['path']) and type(value['bytes']) is int and value['bytes'] >= 0
            and _hash(value['sha256']), 'frozen_proof_reference')


def _fraction(value):
    return dict(numerator=value.numerator, denominator=value.denominator)


def _registry(measurements):
    require(type(measurements) is list, 'measurement_list')
    result = {}
    for row in measurements:
        require(type(row) is dict and set(row) == MEASUREMENT_FIELDS
                and _text(row['id']) and row['id'] not in result, 'unique_registry_record')
        require(row['identity_kind'] in KINDS and _text(row['original_run_id'])
                and row['stage'] == 'early' and row['awareness_type'] == 'evaluation'
                and type(row['method']) is str and type(row['threshold']) is str
                and row['method'] + ':' + row['threshold'] in INSTRUMENTS
                and _hash(row['protocol_sha256']), 'registry_identity')
        require(row['status'] in STATUSES, 'registry_status')
        require(row['context_sha256'] is None if row['status'] == 'unavailable'
                else _hash(row['context_sha256']), 'registry_context')
        labels = SELF_LABELS if row['method'] == 'self' else READER_LABELS
        require((row['status'] == 'observed' and row['label'] in labels and row['reason'] is None)
                or (row['status'] != 'observed' and row['label'] is None and _text(row['reason'])),
                'registry_label_status')
        require(row['identity_kind'] != 'assigned_slot'
                or row['status'] in ('unavailable', 'pending'), 'nonphysical_assigned_slot')
        result[row['id']] = row
    return result


def _cell(reference, registry, run, view, instrument):
    fields = {'id', 'identity_kind', 'state', 'reason', 'context_sha256', 'protocol_sha256',
              'slot', 'qualification', 'logical_alias'}
    require(type(reference) is dict and set(reference) == fields, 'typed_cell')
    method, threshold = instrument.split(':')
    slot = reference['slot']
    require(type(slot) is dict and set(slot) == {'key', 'run_id', 'view', 'stage', 'method', 'threshold', 'source'}
            and _text(slot['key']) and slot['run_id'] == run and slot['view'] == view
            and slot['stage'] == 'early' and slot['method'] == method and slot['threshold'] == threshold,
            'frozen_slot_identity')
    _pin(slot['source'])
    require(reference['context_sha256'] is None or _hash(reference['context_sha256']), 'cell_context')
    require(reference['protocol_sha256'] is None or _hash(reference['protocol_sha256']), 'cell_protocol')
    q = reference['qualification']
    require(type(q) is dict and set(q) == {'state', 'proof', 'reason'}, 'qualification_shape')
    if q['state'] == 'qualified':
        require(q['reason'] is None and _hash(reference['protocol_sha256']), 'qualified_protocol')
        _pin(q['proof'])
    else:
        require(q['state'] == 'unresolved' and q['proof'] is None and _text(q['reason']), 'unresolved_qualification')
    if reference['state'] == 'present':
        identity = reference['id']
        require(_text(identity) and identity in registry and reference['reason'] is None and q['state'] == 'qualified'
                and reference['logical_alias'] is None, 'present_record_join')
        row = registry[identity]
        require(row['identity_kind'] == reference['identity_kind'] and row['original_run_id'] == run
                and row['method'] == method and row['threshold'] == threshold
                and row['context_sha256'] == reference['context_sha256']
                and row['protocol_sha256'] == reference['protocol_sha256'], 'cell_registry_identity')
        return row
    require(reference['state'] in NULL_STATES and reference['id'] is None
            and reference['identity_kind'] is None and _text(reference['reason']), 'nonphysical_null_slot')
    if reference['state'] == 'unavailable':
        require(reference['context_sha256'] is None and q['state'] == 'qualified',
                'frozen_unavailable_input_proof')
    alias = reference['logical_alias']
    if alias is not None:
        require(q['state'] == 'qualified' and _hash(reference['context_sha256'])
                and type(alias) is dict and set(alias) == {'key', 'slot_keys', 'proof'}
                and _text(alias['key']) and type(alias['slot_keys']) is list
                and len(alias['slot_keys']) >= 2 and all(_text(x) for x in alias['slot_keys'])
                and len(set(alias['slot_keys'])) == len(alias['slot_keys'])
                and slot['key'] in alias['slot_keys'], 'logical_alias_scope')
        _pin(alias['proof'])
    return None


def _bounds(coefficients, variables, unresolved_semantics):
    lower = upper = Fraction()
    unknown = []
    cancelled = 0
    for key, coefficient in sorted(coefficients.items()):
        if not coefficient:
            cancelled += 1
            continue
        value = variables[key]
        label = binary(value['label'])
        if label is None:
            lower += min(0, coefficient)
            upper += max(0, coefficient)
            unknown.append([value['index'], coefficient.numerator, coefficient.denominator])
        else:
            lower += coefficient * label
            upper += coefficient * label
    qualified = not unresolved_semantics
    return dict(equal_task_effect=float(lower) if lower == upper and qualified else None,
        exact_missing_label_bounds=[_fraction(lower), _fraction(upper)],
        missing_label_bounds=[float(lower), float(upper)],
        point_identified=lower == upper and qualified,
        arithmetic_point_identified=lower == upper,
        configured_instrument_qualified=qualified,
        unresolved_semantic_slots=sorted(unresolved_semantics),
        unresolved_coefficients=unknown, zero_coefficient_variables=cancelled)


def _scope(members, registry, variables):
    tasks = Counter(x['task_id'] for x in members)
    if not members:
        return dict(pairs=0, tasks=0, task_draw_counts={}, instruments={})
    instruments = {}
    for instrument in INSTRUMENTS:
        coefficients = {k: defaultdict(Fraction) for k in COEFFICIENTS}
        unresolved = {k: set() for k in COEFFICIENTS}
        rates = {name: defaultdict(Fraction) for name in CELLS}
        rate_unresolved = {name: set() for name in CELLS}
        cells = {name: dict(slots=0, represented=0, observed=0, positive=0,
            states=Counter(), statuses=Counter(), categories=Counter(), kinds=Counter(),
            category_weights=defaultdict(Fraction), identities=set(), logical_slots=set()) for name in CELLS}
        transitions = {arm: Counter() for arm in ('baseline', 'treatment')}
        unsupported = Fraction()
        for member in members:
            weight = Fraction(1, len(tasks) * tasks[member['task_id']])
            unsupported += weight * (member['disposition'] == 'unsupported')
            refs = member['instruments'][instrument]
            for effect, vector in COEFFICIENTS.items():
                for name, coefficient in zip(CELLS, vector):
                    if not coefficient:
                        continue
                    ref = refs[name]
                    coefficients[effect][ref['_variable']] += weight * coefficient
                    if ref['qualification']['state'] == 'unresolved':
                        unresolved[effect].add(ref['_occurrence'])
            for name, ref in refs.items():
                cell = cells[name]
                cell['slots'] += 1
                cell['states'][ref['state']] += 1
                cell['logical_slots'].add(ref['slot']['key'])
                row = registry[ref['id']] if ref['state'] == 'present' else None
                label = row['label'] if row is not None else None
                category = label if label is not None else 'missing'
                cell['categories'][category] += 1
                cell['category_weights'][category] += weight
                if row is not None:
                    cell['represented'] += 1
                    cell['observed'] += row['status'] == 'observed'
                    cell['positive'] += row['label'] == 'yes'
                    cell['statuses'][row['status']] += 1
                    cell['kinds'][row['identity_kind']] += 1
                    cell['identities'].add(row['id'])
                rates[name][ref['_variable']] += weight
                if ref['qualification']['state'] == 'unresolved':
                    rate_unresolved[name].add(ref['_occurrence'])
            for arm, table in transitions.items():
                values = []
                for name in (arm + '_original', arm + '_neutral'):
                    ref = refs[name]
                    row = registry[ref['id']] if ref['state'] == 'present' else None
                    values.extend((ref['state'], row['status'] if row else None,
                                   row['label'] if row else None, row['reason'] if row else ref['reason']))
                table[tuple(values)] += 1
        for name, cell in cells.items():
            cell['missing'] = cell['slots'] - cell['observed']
            cell['unique_registry_ids'] = len(cell.pop('identities'))
            cell['unique_logical_slot_keys'] = len(cell.pop('logical_slots'))
            for key in ('states', 'statuses', 'categories', 'kinds'):
                cell[key] = dict(cell[key])
            cell['category_weights'] = {k: _fraction(v) for k, v in cell['category_weights'].items()}
            cell['affirmative_rate'] = _bounds(rates[name], variables, rate_unresolved[name])
        instruments[instrument] = dict(cells=cells,
            effects={k: _bounds(coefficients[k], variables, unresolved[k]) for k in COEFFICIENTS},
            transitions={arm: [dict(original_state=k[0], original_status=k[1], original_label=k[2],
                original_reason=k[3], neutral_state=k[4], neutral_status=k[5], neutral_label=k[6],
                neutral_reason=k[7], count=v) for k, v in table.items()] for arm, table in transitions.items()})
    return dict(pairs=len(members), tasks=len(tasks), task_draw_counts=dict(tasks),
                unsupported_pair_weight=_fraction(unsupported), instruments=instruments)


def describe_four_cells_with_assignments(comparisons, measurements, *, primary_family,
                                          maximum_result_bytes=16*1024**2):
    """Describe full fixed and coverage-qualified rosters without inventing IDs.

    Every member, including unsupported members, binds six instruments and four
    typed cells. Each cell has an exact frozen slot, source reference, identity
    or explicit null state, context/protocol hashes and a qualified/unresolved
    join. Known records under unsupported members require qualified joins too.
    Registry rows extend the earlier measurement shape with ``identity_kind``.
    ``neutral_views_by_arm`` binds the baseline and treatment view separately;
    the comparison's ``neutral_view`` label equals its treatment view. A shared
    initial-summary baseline must not be relabeled as an external-cue treatment.

    A null slot is an independent [0,1] variable by default. Only an explicit
    logical_alias {key, slot_keys, proof}, covering all used matching endpoints,
    allows nonphysical cancellation. Aliases require the same run/instrument,
    state, known context/protocol and task/draw; view names alone prove nothing.
    Existing registry IDs retain their physical_request/native_record/assigned_
    slot kinds when coefficients combine. All five coefficient vectors and
    affirmative scoring are inherited unchanged from the earlier consumer.
    A null unavailable slot requires null context and a qualified frozen input
    availability proof. It must not be assigned an invented registry ID.

    Unresolved semantic joins are reported; their arithmetic ranges do not
    establish a configured-instrument comparison or a point estimate. Qualified
    means configured semantics only, never historical model-weight identity.
    Full bounds can refine earlier all-unsupported-unknown bounds by retaining
    authenticated known cells. Neither scope uses complete-case selection.

    ``unresolved_coefficients`` contains [variable_index, numerator, denominator]
    triples referring to the single top-level ``uncertainty_variables`` table.
    This retains exact identities and coefficients without repeating provenance
    in each cell, contrast and denominator scope.
    """
    require(type(maximum_result_bytes) is int and maximum_result_bytes > 0, 'result_limit')
    registry = _registry(measurements)
    require(type(comparisons) is list and comparisons, 'comparison_list')
    comparisons = copy.deepcopy(comparisons)
    used, factors, variables, physical_tasks, slots, aliases = set(), set(), {}, {}, {}, {}
    for ci, comparison in enumerate(comparisons):
        require(type(comparison) is dict and set(comparison) == {'factor', 'baseline', 'neutral_view', 'neutral_views_by_arm', 'scope', 'members'}
                and all(_text(comparison[k]) for k in ('factor', 'baseline', 'neutral_view', 'scope'))
                and comparison['factor'] not in factors and comparison['neutral_view'] != 'original'
                and type(comparison['members']) is list and comparison['members'], 'frozen_comparison')
        views = comparison['neutral_views_by_arm']
        require(type(views) is dict and set(views) == {'baseline', 'treatment'}
                and all(_text(v) and v != 'original' for v in views.values())
                and views['treatment'] == comparison['neutral_view'], 'exact_neutral_views_by_arm')
        factors.add(comparison['factor'])
        pairs, actors = set(), set()
        for mi, member in enumerate(comparison['members']):
            require(type(member) is dict and set(member) == {'task_id', 'draw_key', 'baseline_run_id',
                'treatment_run_id', 'disposition', 'reason', 'instruments'}
                and _text(member['task_id']) and type(member['draw_key']) is int and member['draw_key'] >= 0
                and _text(member['baseline_run_id']) and _text(member['treatment_run_id'])
                and member['baseline_run_id'] != member['treatment_run_id'], 'frozen_member')
            pair = (member['task_id'], member['draw_key'])
            require(pair not in pairs, 'duplicate_pair')
            pairs.add(pair)
            for run in (member['baseline_run_id'], member['treatment_run_id']):
                require(run not in actors, 'actor_repeated_within_comparison')
                actors.add(run)
            require((member['disposition'] == 'included' and member['reason'] is None)
                    or (member['disposition'] == 'unsupported' and _text(member['reason'])), 'qualification_disposition')
            require(type(member['instruments']) is dict and set(member['instruments']) == set(INSTRUMENTS),
                    'six_instrument_membership')
            for instrument, cells in member['instruments'].items():
                require(type(cells) is dict and set(cells) == set(CELLS), 'four_cell_membership')
                protocols = set()
                for name, ref in cells.items():
                    run = member['baseline_run_id'] if name.startswith('baseline_') else member['treatment_run_id']
                    arm = 'baseline' if name.startswith('baseline_') else 'treatment'
                    view = 'original' if name.endswith('_original') else views[arm]
                    row = _cell(ref, registry, run, view, instrument)
                    if ref['protocol_sha256'] is not None:
                        protocols.add(ref['protocol_sha256'])
                    slot_key = ref['slot']['key']
                    signature = {k: ref[k] for k in ('slot', 'state', 'id', 'identity_kind', 'context_sha256', 'protocol_sha256')}
                    require(slot_key not in slots or slots[slot_key] == signature, 'conflicting_logical_slot')
                    slots[slot_key] = copy.deepcopy(signature)
                    occurrence = f'{ci}/{mi}/{instrument}/{name}'
                    ref['_occurrence'] = occurrence
                    if row is not None:
                        identity = row['id']
                        used.add(identity)
                        require(identity not in physical_tasks or physical_tasks[identity] == pair,
                                'registry_id_cross_task_or_draw')
                        physical_tasks[identity] = pair
                        variable = 'record:' + identity
                        variables[variable] = dict(kind=row['identity_kind'], id=identity, label=row['label'], slot_keys=[])
                    elif ref['logical_alias'] is not None:
                        alias = ref['logical_alias']
                        variable = 'logical_alias:' + alias['key']
                        endpoint = (run, instrument, ref['state'], ref['context_sha256'], ref['protocol_sha256'], pair)
                        signature = dict(proof=alias['proof'], slot_keys=sorted(alias['slot_keys']), endpoint=endpoint)
                        if alias['key'] in aliases:
                            require(aliases[alias['key']]['signature'] == signature, 'logical_alias_identity')
                        else:
                            aliases[alias['key']] = dict(signature=signature, seen=set())
                        aliases[alias['key']]['seen'].add(slot_key)
                        variables[variable] = dict(kind='nonphysical_logical_alias', id=None, label=None,
                                                   slot_keys=sorted(alias['slot_keys']))
                    else:
                        variable = 'unresolved_slot:' + occurrence
                        variables[variable] = dict(kind='nonphysical_slot', id=None, label=None, slot_keys=[slot_key])
                    ref['_variable'] = variable
                require(len(protocols) <= 1, 'configured_instrument_protocol_mismatch')
    require(used == set(registry), 'extra_or_unmapped_registry_records')
    for alias in aliases.values():
        require(alias['seen'] == set(alias['signature']['slot_keys']), 'logical_alias_complete_membership')
    uncertainty_variables = []
    for key, value in sorted(variables.items()):
        if value['label'] is None:
            value['index'] = len(uncertainty_variables)
            uncertainty_variables.append(dict(key=key, identity_kind=value['kind'],
                id=value['id'], slot_keys=value['slot_keys']))
    require(type(primary_family) is list and len(primary_family) == 2 * len(comparisons)
            and all(type(x) is dict and set(x) == {'baseline', 'variant', 'readout'} for x in primary_family)
            and {(x['baseline'], x['variant'], x['readout']) for x in primary_family}
            == {(c['baseline'], c['factor'], method + '_evaluation_presence') for c in comparisons
                for method in ('self', 'reader')}, 'unchanged_primary_presence_family')
    summaries = []
    for comparison in comparisons:
        members = comparison['members']
        summaries.append(dict(factor=comparison['factor'], baseline=comparison['baseline'],
            neutral_view=comparison['neutral_view'], scope=comparison['scope'],
            neutral_views_by_arm=copy.deepcopy(comparison['neutral_views_by_arm']),
            fixed_members=[{k: copy.deepcopy(v) for k, v in x.items() if k != 'instruments'} for x in members],
            full_fixed=_scope(members, registry, variables),
            coverage_qualified=_scope([x for x in members if x['disposition'] == 'included'], registry, variables)))
    result = dict(schema='causes.neutral_four_cell_assignment_descriptive.v1', comparisons=summaries,
        primary_family=copy.deepcopy(primary_family), registry_counts=dict(Counter(x['identity_kind'] for x in registry.values())),
        unique_registry_records=len(registry), unique_logical_slots=len(slots), logical_alias_groups=len(aliases),
        uncertainty_variables=uncertainty_variables,
        inference_released=False, missing_bounds_are_confidence_intervals=False,
        historical_provider_weights_equated=False,
        full_bound_method='Retain authenticated known cells under every fixed member, including unsupported members; unresolved cells alone receive [0,1]. This may refine earlier all-unsupported-unknown bounds.',
        selection='Full fixed task/draw weights and separately coverage-qualified task/draw weights; never complete cases.',
        interpretation='Descriptive selected saved histories; no significance, equivalence, power, population or latent-state claim.')
    require(len(json.dumps(result, sort_keys=True, allow_nan=False).encode()) <= maximum_result_bytes, 'serialized_result_bound')
    return result
