from __future__ import annotations

import math
import random


def _quantile(values, probability):
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    index = math.floor(position)
    fraction = position - index
    return ordered[index] + fraction * (ordered[min(index + 1, len(ordered) - 1)] - ordered[index])


def summarize_task_bounds(bounds, *, alpha=.05, bootstrap_draws=20000, seed=20261011):
    if not bounds or not 0 < alpha < 1:
        raise ValueError('nonempty_task_bounds_and_valid_alpha_required')
    if type(bootstrap_draws) is not int or bootstrap_draws < 1 or type(seed) is not int:
        raise ValueError('invalid_bootstrap_settings')
    if any(not isinstance(task, str) or not task for task in bounds):
        raise ValueError('invalid_task_identity')
    tasks = sorted(bounds)
    endpoints = []
    for task in tasks:
        if not isinstance(task, str) or not task:
            raise ValueError('invalid_task_identity')
        pair = bounds[task]
        if len(pair) != 2 or any(type(x) not in (int, float) or not math.isfinite(x) for x in pair):
            raise ValueError('invalid_task_interval')
        lower, upper = pair
        if not -1 <= lower <= upper <= 1:
            raise ValueError('task_interval_out_of_range')
        endpoints.append((lower, upper))
    count = len(tasks)
    lower, upper = (math.fsum(row[k] for row in endpoints) / count for k in (0, 1))
    distance = max(0., lower, -upper)
    radius = math.sqrt(2 * math.log(2 / alpha) / count)
    pvalue = min(1., 2 * math.exp(-count * distance * distance / 2))
    generator = random.Random(seed)
    lower_draws, upper_draws = [], []
    for _ in range(bootstrap_draws):
        sampled = [endpoints[generator.randrange(count)] for _ in range(count)]
        lower_draws.append(math.fsum(row[0] for row in sampled) / count)
        upper_draws.append(math.fsum(row[1] for row in sampled) / count)
    return {
        'task_ids': tasks,
        'task_count': count,
        'task_weight': 1 / count,
        'assigned_completion_bounds': [lower, upper],
        'hoeffding_p': pvalue,
        'hoeffding_radius': radius,
        'hoeffding_interval': [max(-1., lower - radius), min(1., upper + radius)],
        'alpha': alpha,
        'reject_zero_mean': pvalue <= alpha,
        'assumption': 'independent task-level contrasts in [-1,1]; arbitrary within-task dependence',
        'bootstrap_sensitivity_interval': [
            _quantile(lower_draws, alpha / 2), _quantile(upper_draws, 1 - alpha / 2)],
        'bootstrap_draws': bootstrap_draws,
        'bootstrap_seed': seed,
        'bootstrap_degenerate': len(set(lower_draws)) == len(set(upper_draws)) == 1,
        'bootstrap_scope': 'approximate paired task-block resampling; not the primary test',
    }
