"""Pure secondary projection; caller must first satisfy IMPLEMENTATION_PLAN custody gates.

No file/network/controller loader is provided. Inputs are original closed actor
records; no awareness result is an input. This module grants no custody acceptance.
"""
import math
from collections import Counter
from statistics import mean, median


def need(value, message):
    if not value:
        raise ValueError(message)


def finite_seconds(value):
    need(value is None or (type(value) in (int, float) and math.isfinite(value)
                          and value >= 0), "finite nonnegative elapsed seconds")
    return value


def project(row, disposition, result=None, checkpoint=None):
    """Project already authenticated originals; no outcome is inferred from absence."""
    aid = row['assignment_id']
    need(row['continue_full'] is True and disposition['id'] == aid, 'selected original')
    unissued = disposition.get('status', '').startswith('unstarted')
    need(not unissued or (result is None and checkpoint is None), 'unissued has no actor evidence')
    if not unissued:
        need(disposition.get('genuine_wait') is True and
             disposition.get('resources_releasable') is True, 'closed started actor')
    if result is not None:
        need(result['id'] == aid and result['resources_releasable'] is True, 'original result identity/closure')
    completion = result.get('completion') if result else None
    checker = result.get('checker') if result else None
    natural = elapsed = preparation_elapsed = actor_started = None
    if completion is not None:
        need(type(completion.get('actor_started')) is bool, 'original actor clock state')
        actor_started = completion['actor_started']
        elapsed = finite_seconds(completion.get('elapsed_seconds'))
        preparation_elapsed = finite_seconds(completion.get('preparation_and_actor_seconds'))
        need(actor_started or elapsed is None, 'no actor clock means no actor elapsed')
        need(completion.get('status') == result.get('status'), 'original completion status')
        if actor_started and isinstance(completion.get('reason'), str) and completion['reason']:
            natural = completion['reason'] == 'natural_completion'
            need(not natural or completion['status'] == 'completed', 'natural completion status')
    native = 'unissued' if unissued else 'unknown'
    native_value = None
    if isinstance(checker, dict):
        native_value = checker.get('scoped_task_native_passed')
        need(native_value is None or type(native_value) is bool, 'original nullable native field')
        # Raw selected tests / display score never replace the established scoped field.
        if (type(native_value) is bool and checker.get('genuine_process_wait') is True
                and type(checker.get('process_exitcode')) is int and checker['process_exitcode'] == 0
                and checker.get('closure', {}).get('status') == 'closed'):
            native = 'success' if native_value else 'failure'
    endpoint = 'unissued' if unissued else 'missing'
    if checkpoint is not None:
        for key, expected in [('assignment_id', aid), ('checkpoint', 'full'),
                              ('task', row['instance_id']), ('factor', row['factor']),
                              ('level', row['level']), ('continue_full', True)]:
            need(checkpoint.get(key) == expected, 'full checkpoint ' + key)
        need(type(checkpoint.get('eligible_for_diagnostic')) is bool, 'endpoint eligibility is explicit')
        endpoint = 'eligible' if checkpoint['eligible_for_diagnostic'] else 'ineligible'
    keys = ('assignment_id', 'task', 'instance_id', 'condition', 'factor', 'level',
            'replicate', 'block_id', 'realization', 'qwen_replica', 'qwen_seed',
            'balanced_schedule_stratum', 'actor_slot_randomization_key')
    return {**{k: row[k] for k in keys if k in row},
            'actor_process_disposition': 'unissued' if unissued else 'started_closed',
            'actor_result_available': result is not None, 'actor_original_started': actor_started,
            'original_disposition_status': disposition.get('status'),
            'original_actor_status': result.get('status') if result else None,
            'original_completion_reason': completion.get('reason') if completion else None,
            'native_check': native, 'original_scoped_task_native_passed': native_value,
            'original_checker_status': checker.get('status') if isinstance(checker, dict) else None,
            'natural_completion': natural, 'actor_elapsed_seconds': elapsed,
            'preparation_and_actor_seconds': preparation_elapsed,
            'full_checkpoint_availability': endpoint}


def elapsed_summary(values):
    values = [finite_seconds(v) for v in values if v is not None]
    return {'available': len(values), 'mean': mean(values) if values else None,
            'median': median(values) if values else None,
            'min': min(values) if values else None, 'max': max(values) if values else None}


def native_binary(row):
    return {'success': 1, 'failure': 0, 'unknown': None, 'unissued': None}[row['native_check']]


def paired_description(pairs):
    """Assigned-frame identification bounds only; no test or sampling interval."""
    known = [high-low for low, high in pairs if low is not None and high is not None]
    lo = sum((0 if high is None else high)-(1 if low is None else low) for low, high in pairs)/len(pairs)
    hi = sum((1 if high is None else high)-(0 if low is None else low) for low, high in pairs)/len(pairs)
    return {'assigned_pairs': len(pairs), 'both_available': len(known),
            'observed_pair_mean_difference': mean(known) if known else None,
            'assigned_difference_identification_bounds': [lo, hi],
            'scope': 'Strong minus baseline; worst binary completions of missing positions, not a confidence interval.'}


def describe(model, selected, projected):
    need(model in ('Qwen', 'API'), 'separate model cohort')
    expected = {r['assignment_id']: r for r in selected}
    got = {r['assignment_id']: r for r in projected}
    need(len(selected) == len(expected) == len(projected) == len(got) == 24
         and set(expected) == set(got), 'all24 exactly once; no replacements')
    for aid, row in got.items():
        for key in ('task', 'instance_id', 'condition', 'factor', 'level', 'replicate', 'block_id', 'realization'):
            need(row[key] == expected[aid][key], 'frozen row mapping ' + key)
        for key in ('qwen_replica', 'qwen_seed', 'balanced_schedule_stratum', 'actor_slot_randomization_key'):
            need(row.get(key) == expected[aid].get(key), 'frozen scheduling mapping ' + key)
    tasks = sorted({r['task'] for r in selected})
    need(len(tasks) == 6, 'six fixed tasks')
    cells, pairs, factors = [], [], []
    for factor, high in [('F07', 4), ('F08', 3)]:
        fpairs = []
        for task in tasks:
            rows = sorted([r for r in got.values() if r['task'] == task and r['factor'] == factor], key=lambda r:r['level'])
            need(len(rows) == 2 and [r['level'] for r in rows] == [0, high], 'fixed endpoints')
            for key in ('replicate', 'block_id', 'realization'):
                need(rows[0][key] == rows[1][key], 'within-model shared ' + key)
            stratum = 'qwen_replica' if model == 'Qwen' else 'balanced_schedule_stratum'
            need(rows[0][stratum] == rows[1][stratum], 'within-model scheduling stratum')
            low, top = rows
            native_pair = [native_binary(low), native_binary(top)]
            fpairs.append(native_pair)
            seconds = [r['actor_elapsed_seconds'] for r in rows]
            pairs.append({'task': task, 'factor': factor, 'replicate': low['replicate'],
                          'baseline_id': low['assignment_id'], 'strong_id': top['assignment_id'],
                          'native_pair': native_pair,
                          'natural_completion_pair': [r['natural_completion'] for r in rows],
                          'actor_elapsed_difference_seconds': seconds[1]-seconds[0] if None not in seconds else None})
        factors.append({'factor': factor, **paired_description(fpairs)})
        for level in (0, high):
            rows = [r for r in got.values() if r['factor'] == factor and r['level'] == level]
            need(len(rows) == 6, 'six assigned per condition')
            counts = Counter(r['native_check'] for r in rows)
            n = {key: counts[key] for key in ('success', 'failure', 'unknown', 'unissued')}
            cells.append({'factor': factor, 'level': level, 'assigned': 6, 'native_check': n,
                          'native_success_fraction_bounds': [n['success']/6, (6-n['failure'])/6],
                          'natural_completion': {'yes': sum(r['natural_completion'] is True for r in rows),
                                                 'no': sum(r['natural_completion'] is False for r in rows),
                                                 'unknown': sum(r['natural_completion'] is None for r in rows)},
                          'full_checkpoint_availability': dict(Counter(r['full_checkpoint_availability'] for r in rows)),
                          'actor_elapsed_seconds': elapsed_summary([r['actor_elapsed_seconds'] for r in rows]),
                          'preparation_and_actor_seconds': elapsed_summary([r['preparation_and_actor_seconds'] for r in rows])})
    return {'model': model, 'assigned': 24, 'conditions': cells, 'pairs': pairs,
            'native_endpoint_descriptive': factors,
            'records': [got[r['assignment_id']] for r in selected],
            'limits': ['Six fixed task pairs per factor; F07/F08 only; descriptive, no powered behavioral claim.',
                       'Native-check success is neither artifact correctness nor full-request fulfillment.',
                       'Unknown and unissued stay in assigned denominators; elapsed summaries condition on observed clocks.',
                       'Elapsed is original actor completion time, excludes later checker/custody, and is not incremental post-prefix time.',
                       'No awareness values, cheating inference, mediation, cross-model pooling or new significance tests.']}
