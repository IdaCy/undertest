"""Compose descriptive tables from explicit, already verified study inputs.

This module does no file discovery, inference, or output publication. Each call
returns the five legacy table objects, each keyed by the supplied model name.
The legacy ``dose_levels`` name denotes categorical conditions compared with
their declared control, not an ordinal dose or trend. Reader end-stage
judgments describe cumulative generated expression, not current belief.
"""

from collections.abc import Mapping, Sequence
from itertools import combinations
import json

from .campaign_descriptive import TYPES, THRESHOLDS, measurement, distribution, paired


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _name(value):
    return isinstance(value, str) and bool(value)


def _ordered_names(values, reason):
    _require(isinstance(values, Sequence) and not isinstance(values, (str, bytes)), reason)
    result = list(values)
    _require(bool(result) and all(_name(value) for value in result), reason)
    _require(len(result) == len(set(result)), reason)
    return result


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def compose_tables(model, jobs, records, *, rosters, condition_families,
                   variant_bindings, quality_unavailable=()):
    """Return threshold, awareness, method, condition and roster tables.

    ``jobs`` and ``records`` are per-model mappings keyed by frozen run ID.
    Records use the public trajectory schema: allocation/task identity,
    context_sources, self, reader and reader_dispositions. An absent record is
    pending unless its ID is explicitly quality-unavailable.

    ``rosters`` supplies ordered run IDs for threshold_early, threshold_end and
    awareness_common. Their membership must equal the corresponding assignment
    flags in jobs; results never select or shrink an assigned population.

    Each condition_families entry supplies ``tasks`` in matched order, a
    ``control`` variant ID, and categorical ``levels`` in
    presentation order. variant_bindings[task][level] supplies ``eligible`` and,
    for aliases, ``alias_of``. Only a unique descriptive_first_rep at repetition
    zero supplies a physical condition draw. Aliases are direct and task-local.

    Frozen-input provenance and upstream evidence verification are the caller's
    responsibility. This function checks the joins it consumes, never mutates
    inputs, and preserves all five categories and missingness dispositions.
    Observed-only summaries are descriptive; assigned bounds retain missing IDs.
    """
    _require(_name(model), 'invalid_model')
    _require(isinstance(jobs, Mapping) and isinstance(records, Mapping), 'invalid_record_maps')
    _require(bool(jobs) and all(_name(rid) for rid in jobs), 'invalid_job_ids')
    _require(set(records) <= set(jobs), 'nonfixed_assignment_in_records')
    unavailable = list(quality_unavailable)
    _require(all(_name(rid) for rid in unavailable) and len(unavailable) == len(set(unavailable)),
             'invalid_quality_unavailable_ids')
    unavailable = frozenset(unavailable)
    _require(unavailable <= set(jobs), 'unknown_quality_unavailable_assignment')
    _require(not unavailable.intersection(records), 'quality_unissued_disposition_conflicts_with_observed_episode')
    for rid, job in jobs.items():
        _require(job['allocation']['run_id'] == rid and _name(job['spec']['task_id']),
                 'job_identity_mismatch')
    for rid, row in records.items():
        job = jobs[rid]
        _require(row['model'] == model and row['run_id'] == rid
                 and row['task_id'] == job['spec']['task_id']
                 and _canonical(row['allocation']) == _canonical(job['allocation']),
                 'record_assignment_mismatch')

    _require(isinstance(rosters, Mapping) and set(rosters) == {
        'threshold_early', 'threshold_end', 'awareness_common'}, 'invalid_roster_keys')
    selected = _ordered_names(rosters['threshold_early'], 'invalid_early_roster')
    full = _ordered_names(rosters['threshold_end'], 'invalid_end_roster')
    common = _ordered_names(rosters['awareness_common'], 'invalid_common_roster')
    expected_early = {rid for rid, job in jobs.items() if 'early' in job['allocation']['reader_stages']}
    expected_end = {rid for rid, job in jobs.items() if job['allocation']['continue_to_end']}
    expected_common = {rid for rid in expected_end
                       if all(kind + '_presence' in jobs[rid]['allocation']['instruments'] for kind in TYPES)}
    _require(set(selected) == expected_early and set(full) == expected_end
             and set(common) == expected_common, 'frozen_roster_assignment_mismatch')
    _require(set(common) <= set(selected), 'common_roster_not_assigned_early_reader')

    thresholds, awareness, methods, conditions = {}, {}, {}, {}
    resolved = {'threshold_early': selected, 'threshold_end': full,
                'awareness_common': common, 'dose': {}}

    def values(ids, stage, method, kind='evaluation', threshold='presence'):
        return [measurement(records, jobs, rid, stage, method, kind, threshold, unavailable)
                for rid in ids]

    for stage, ids in [('early', selected), ('end', full)]:
        thresholds[stage] = {}
        for method in ('self', 'reader'):
            observed = {threshold: values(ids, stage, method, threshold=threshold)
                        for threshold in THRESHOLDS}
            thresholds[stage][method] = {
                'distributions': {key: distribution(value) for key, value in observed.items()},
                'paired_thresholds': {left + '__to__' + right: paired(observed[left], observed[right])
                                      for left, right in combinations(THRESHOLDS, 2)}}
        methods[stage] = {
            'evaluation_all_selected': {
                threshold: paired(values(ids, stage, 'self', threshold=threshold),
                                  values(ids, stage, 'reader', threshold=threshold))
                for threshold in THRESHOLDS},
            'all_awareness_common_presence': {
                kind: paired(values(common, stage, 'self', kind), values(common, stage, 'reader', kind))
                for kind in TYPES}}
        awareness[stage] = {}
        for name, pool, method, levels in [
                ('common_self', common, 'self', ('presence',)),
                ('common_reader', common, 'reader', ('presence',)),
                ('all_selected_reader', ids, 'reader', THRESHOLDS)]:
            awareness[stage][name] = {}
            for level in levels:
                observed = {kind: values(pool, stage, method, kind, level) for kind in TYPES}
                awareness[stage][name][level] = {
                    'distributions': {kind: distribution(value) for kind, value in observed.items()},
                    'paired_awareness_types': {
                        left + '__to__' + right: paired(observed[left], observed[right])
                        for left, right in combinations(TYPES, 2)}}

    initial = {}
    for rid, job in jobs.items():
        allocation = job['allocation']
        if allocation['descriptive_first_rep']:
            canonical = allocation['canonical_variant']
            key = job['spec']['task_id'], canonical
            _require(_name(canonical) and key not in initial
                     and type(allocation['repetition']) is int and allocation['repetition'] == 0,
                     'invalid_initial_draw_assignment')
            initial[key] = rid
    _require(isinstance(condition_families, Mapping) and isinstance(variant_bindings, Mapping),
             'invalid_condition_maps')
    for factor, family in condition_families.items():
        _require(_name(factor) and isinstance(family, Mapping), 'invalid_condition_family')
        tasks = _ordered_names(family['tasks'], 'invalid_condition_task_roster')
        control = family['control']
        _require(_name(control), 'invalid_condition_control')
        levels = list(dict.fromkeys([control] + _ordered_names(family['levels'], 'invalid_condition_levels')))
        cells, ids = {}, {}
        for level in levels:
            ids[level] = []
            for task in tasks:
                _require(task in variant_bindings and level in variant_bindings[task],
                         'missing_condition_variant')
                variant = variant_bindings[task][level]
                _require(variant['eligible'] is True, 'ineligible_common_condition_cell')
                canonical = variant.get('alias_of', level)
                _require(_name(canonical) and canonical in variant_bindings[task], 'invalid_condition_alias')
                target = variant_bindings[task][canonical]
                _require(target['eligible'] is True and target.get('alias_of', canonical) == canonical,
                         'condition_alias_not_direct_canonical')
                _require((task, canonical) in initial, 'missing_initial_condition_draw')
                ids[level].append(initial[task, canonical])
            _require(len(set(ids[level])) == len(tasks), 'condition_reuses_assignment_across_tasks')
            cells[level] = values(ids[level], 'early', 'self')
        resolved['dose'][factor] = {'tasks_in_order': tasks, 'initial_draws_by_level': ids}
        conditions[factor] = {
            'task_count': len(tasks), 'control': control,
            'distributions': {level: distribution(value) for level, value in cells.items()},
            'level_minus_control': {level: paired(cells[control], cells[level])
                                   for level in levels if level != control},
            'shared_physical_draws_across_levels':
                len(ids) * len(tasks) - len({rid for members in ids.values() for rid in members})}
    return {'threshold_pairs': {model: thresholds}, 'awareness_types': {model: awareness},
            'self_reader_pairs': {model: methods}, 'dose_levels': {model: conditions},
            'ROSTERS': {model: resolved}}
