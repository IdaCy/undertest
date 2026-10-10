import argparse
import datetime
import hashlib
import json
import math
import statistics
from pathlib import Path


def epoch(value):
    return datetime.datetime.fromisoformat(value).timestamp()

def stats(values):
    values = sorted(values)
    if not values:
        return {'count': 0}
    return {'count': len(values), 'minimum': values[0], 'maximum': values[-1],
            'mean': statistics.mean(values), 'median': statistics.median(values),
            'p90_nearest_rank': values[max(0, math.ceil(.9 * len(values)) - 1)],
            'p95_nearest_rank': values[max(0, math.ceil(.95 * len(values)) - 1)]}

def metrics(row):
    return {key: float(value) for key, value in (line.rsplit(' ', 1) for line in row['metrics'])}

def metric(values, name):
    matches = [value for key, value in values.items() if key == 'vllm:' + name or key.startswith('vllm:' + name + '{')]
    assert matches, 'required_server_metric_missing:' + name
    assert all(math.isfinite(value) and value >= 0 for value in matches), 'invalid_server_metric:' + name
    return sum(matches)


TERMINALS = {'episode_finished', 'unresolved_current_attempt', 'existing_completed_summary',
             'unresolved_prior_attempt', 'unresolved_completed_evidence', 'unresolved_identity_conflict'}
TOKEN_BANDS = [(0, 8192), (8192, 32768), (32768, 65536), (65536, 131072), (131072, 262145)]


def require(value, reason):
    if not value:
        raise ValueError(reason)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def token_band(row):
    value = ((row.get('receipt') or {}).get('usage') or {}).get('prompt_tokens')
    if type(value) is not int or value < 0:
        return 'missing_or_invalid_usage'
    return next((str(a) + '<=prompt<' + str(b) for a, b in TOKEN_BANDS if a <= value < b),
                'outside_declared_context')


def _pair(wall, mono, w0, m0, limit, issues, label):
    if not finite(mono) or abs(epoch(wall) - w0 - (mono - m0)) > limit:
        issues.append('clock_drift:' + label)


def _span(value, w0, m0, maximum, drift, issues, label):
    start, end = value['started'], value['finished']
    for name, point in [('start', start), ('end', end)]:
        _pair(point['wall'], point['monotonic_seconds'], w0, m0, drift, issues, label + ':' + name)
    length = end['monotonic_seconds'] - start['monotonic_seconds']
    if not finite(length) or not 0 <= length <= maximum or epoch(end['wall']) < epoch(start['wall']):
        issues.append('capture_span:' + label)


def timing_support(samples, boundaries, window, declaration, preceding_sample=None):
    issues = []
    lo, hi = epoch(window['start_wall']), epoch(window['end_wall'])
    m0, m1 = window['start_monotonic_seconds'], window['end_monotonic_seconds']
    seconds = window['scheduled_duration_seconds']
    require(type(seconds) is int and seconds > 0 and seconds % 30 == 0, 'fixed_window_duration')
    require(hi - lo == seconds and m1 - m0 == seconds
            and window['half_open_receipt_finish_interval'] is True, 'fixed_window_anchor_mismatch')
    require(len(samples) == seconds // 30 + 1, 'missing_or_extra_samples')
    base_index = samples[0]['index']
    require(type(base_index) is int and base_index >= 0
            and [s['index'] for s in samples] == list(range(base_index, base_index + len(samples))), 'sample_indices')
    timing = declaration['timing']
    late = timing['max_lateness_seconds']
    drift = timing['max_clock_drift_seconds']
    span = timing['max_sample_observation_span_seconds']
    capture = timing['max_boundary_receipt_capture_span_seconds']
    if base_index == 0:
        require(samples[0]['at'] == window['start_wall']
                and samples[0]['monotonic_seconds'] == m0, 't0_sample_changed')
    else:
        require(epoch(window['global_start_wall']) + base_index * 30 == lo
                and window['global_start_monotonic_seconds'] + base_index * 30 == m0,
                'shared_window_nominal_anchor_changed')
    if samples[0]['monotonic_seconds'] > m0:
        require(preceding_sample is not None and preceding_sample['index'] == base_index-1
                and preceding_sample['monotonic_seconds'] <= m0
                and 0 < samples[0]['monotonic_seconds']-preceding_sample['monotonic_seconds'] <= 40,
                'late_boundary_requires_adjacent_preceding_sample')
    if preceding_sample is not None:
        require(preceding_sample['index'] == base_index-1 and preceding_sample['monotonic_seconds'] < samples[0]['monotonic_seconds']
                and epoch(preceding_sample['at']) < epoch(samples[0]['at']), 'preceding_sample_identity_or_order')
        nominal = m0-30
        if not 0 <= preceding_sample['monotonic_seconds']-nominal <= late:
            issues.append('preceding_sample_lateness')
        require(abs(preceding_sample['scheduled_monotonic_seconds']-nominal) < 1e-6,
                'preceding_sample_schedule_changed')
        _pair(preceding_sample['at'], preceding_sample['monotonic_seconds'],lo,m0,drift,issues,'preceding_sample')
        _span({'started':{'wall':preceding_sample['at'],'monotonic_seconds':preceding_sample['monotonic_seconds']},
               'finished':{'wall':preceding_sample['finished_at'],'monotonic_seconds':preceding_sample['finished_monotonic_seconds']}},
              lo,m0,span,drift,issues,'preceding_sample')
    require(all(b['monotonic_seconds'] > a['monotonic_seconds'] and epoch(b['at']) > epoch(a['at'])
                for a, b in zip(samples, samples[1:])), 'nonmonotone_sample_times')
    for i, row in enumerate(samples):
        actual_lateness = row['monotonic_seconds'] - (m0 + 30 * i)
        # The reused eligible point may retain its original prelude scheduling anchor.
        if (not 0 <= actual_lateness <= late or row['lateness_seconds'] > late
                or ((i or base_index) and abs(row['scheduled_monotonic_seconds'] - (m0 + 30 * i)) > 1e-6)):
            issues.append('sample_lateness:' + str(i))
        _pair(row['at'], row['monotonic_seconds'], lo, m0, drift, issues, str(i))
        _span({'started': {'wall': row['at'], 'monotonic_seconds': row['monotonic_seconds']},
               'finished': {'wall': row['finished_at'], 'monotonic_seconds': row['finished_monotonic_seconds']}},
              lo, m0, span, drift, issues, 'sample:' + str(i))
        for name in ['metrics_capture', 'journal_capture']:
            _span({'started': row[name + '_started'], 'finished': row[name + '_finished']},
                  lo, m0, span, drift, issues, name + ':' + str(i))
    for i, boundary in enumerate(boundaries):
        expected = m0 if i == 0 else m1
        if boundary['capture']['started']['monotonic_seconds'] < expected:
            issues.append('boundary_before_fixed_endpoint:' + str(i))
        _span(boundary['capture'], lo, m0, capture, drift, issues, 'boundary:' + str(i))
    return issues


def _journal_prefixes(samples, start, end, owner):
    initial = {r['group']: r for r in start}
    final = {r['group']: r for r in end}
    require(len(initial) == len(start) and len(final) == len(end) and initial.keys() == final.keys(),
            'journal_group_set_changed')
    result, claims, terminals = {}, {}, {}
    for group, log in final.items():
        require(log['events'][:len(initial[group]['events'])] == initial[group]['events'], 'journal_prefix_changed')
        for bound in [initial[group], log]:
            raw = b''.join(json.dumps(e, sort_keys=True, ensure_ascii=False, allow_nan=False).encode() + b'\n'
                           for e in bound['events'])
            require(len(raw) == bound['bytes'] and hashlib.sha256(raw).hexdigest() == bound['sha256'],
                    'journal_serialization_pin_mismatch')
            require(bound['incomplete_tail_bytes'] == 0, 'incomplete_journal_tail')
        wanted = {next(c['journal_sha256'] for c in s['controllers'] if c['group'] == group) for s in samples}
        digest, all_claims, terminal_keys = hashlib.sha256(), {}, set()
        def retain():
            value = digest.hexdigest()
            if value in wanted:
                active = {k: v for k, v in all_claims.items() if k not in terminal_keys
                          and group == owner['group'] and k[0] == owner['controller_id']}
                result[group, value] = {'full': sum(bool(v.get('full')) for v in active.values()),
                                        'prefix': sum(not v.get('full') for v in active.values()),
                                        'run_ids': sorted(k[1] for k in active)}
        retain()
        for event in log['events']:
            digest.update(json.dumps(event, sort_keys=True, ensure_ascii=False, allow_nan=False).encode() + b'\n')
            key = event.get('controller_id'), event.get('run_id')
            if event['event'] == 'claimed':
                require(key not in all_claims and key not in terminal_keys, 'duplicate_or_post_terminal_claim')
                all_claims[key] = event
                if group == owner['group'] and key[0] == owner['controller_id']:
                    claims[key[1]] = event
            elif event['event'] in TERMINALS:
                terminal_keys.add(key)
                if group == owner['group'] and key[0] == owner['controller_id']:
                    terminals.setdefault(key[1], event)
            retain()
        require(all((group, h) in result for h in wanted), 'unresolved_sample_journal_prefix')
    return result, claims, terminals


def _identity_issues(samples, declaration, allowed_wait_samples=()):
    issues = []
    expected = {r['role']: r for r in declaration['processes']}
    for i, sample in enumerate(samples):
        actual = {r['role']: r for r in sample['process_identities']}
        if sample.get('pin_errors') or len(actual) != len(sample['process_identities']) or actual.keys() != expected.keys():
            issues.append('identity_or_pin_error:' + str(i))
            continue
        for role, target in expected.items():
            row = actual[role]
            argv_hash = hashlib.sha256(json.dumps(target['argv']).encode()).hexdigest()
            if (row.get('pid') != target['pid'] or row.get('start_ticks') != target['process_starttime_ticks']
                    or row.get('argv_sha256') != argv_hash or not row.get('start_matches')
                    or not row.get('argv_matches') or row.get('identity_error')):
                issues.append('process_identity_changed:' + role + ':' + str(i))
            if role == declaration['owner']['role'] and (
                    row.get('qualified_cap_matches') is not True
                    or row.get('workers') != declaration['owner']['workers']
                    or row.get('max_full') != declaration['owner']['max_full']):
                issues.append('capacity_changed:' + str(i))
        if ((sample['index'] not in allowed_wait_samples and (not sample.get('storage_guard', {}).get('healthy')
                or any(sample.get('storage_guard', {}).get('alarms', {}).values())))
                or set(sample.get('controls_present', {})) != set(declaration['controls_absent'])
                or any(sample.get('controls_present', {}).values())):
            issues.append('storage_or_control_refusal:' + str(i))
    return issues


def population(rows, seconds):
    completed = [r for r in rows if r['completed_in_window']]
    success = [r for r in completed if r['receipt'].get('status') == 'complete']
    failure = [r for r in completed if r['receipt'].get('status') != 'complete']
    valid = [r for r in success if type((r['receipt'].get('usage') or {}).get('completion_tokens')) is int
             and r['receipt']['usage']['completion_tokens'] >= 0]
    elapsed = [r['receipt'].get('elapsed_seconds') for r in success]
    elapsed_valid = all(finite(v) and v > 0 for v in elapsed)
    total_time = sum(elapsed) if elapsed_valid else None
    tokens = sum(r['receipt']['usage']['completion_tokens'] for r in valid)
    return {'completed': len(completed), 'successful': len(success), 'failed': len(failure),
            'successful_actors': len({r['run_id'] for r in success}),
            'failed_actors': len({r['run_id'] for r in failure}),
            'returned_tokens': tokens, 'tokens_per_second': tokens / seconds,
            'elapsed_seconds': total_time, 'elapsed_supported': elapsed_valid,
            'service_productivity': tokens / total_time if total_time else None,
            'usage_coverage': len(valid) / len(success) if success else None,
            'failure_fraction': len(failure) / len(completed) if completed else None,
            'timeouts': sum(r['receipt'].get('error_class') == 'TimeoutError' for r in failure),
            'actor_failures': sum(r['phase'].startswith('actor_') for r in failure),
            'probe_failures': sum(r['phase'].startswith('probe_') for r in failure),
            'successful_elapsed_seconds': stats(elapsed) if elapsed_valid else None,
            'left_straddling_completions': sum(not r['fully_contained'] for r in completed),
            'fully_contained_returned_tokens': sum(r['receipt']['usage']['completion_tokens'] for r in valid if r['fully_contained'])}


def summarize_window(samples, start_boundary, end_boundary, window, declaration, *, preceding_sample=None, capacity_wait_evidence=None):
    """Project already collected technical metadata; this function has no I/O or controls."""
    lo, hi = epoch(window['start_wall']), epoch(window['end_wall'])
    seconds = window['scheduled_duration_seconds']
    issues = timing_support(samples, [start_boundary, end_boundary], window, declaration, preceding_sample)
    occupancy_samples = ([preceding_sample] if preceding_sample is not None else []) + samples
    owner = declaration['owner']
    control_events = [e for log in end_boundary['journals'] if log['group'] == owner['group']
                      for e in log['events'] if e.get('controller_id') == owner['controller_id']
                      and lo <= epoch(e['at']) < hi
                      and e['event'] in {'pause_seen','controller_paused','controller_started','disk_floor_wait','route_quarantined'}]
    wait_support = {'sample_indices': [], 'event_sha256': []}
    if capacity_wait_evidence is not None:
        from causes.campaign_capacity_wait import qualify_capacity_waits
        wait_support = qualify_capacity_waits(occupancy_samples, control_events, declaration, capacity_wait_evidence)
    issues += _identity_issues(occupancy_samples, declaration, wait_support['sample_indices'])
    prefixes, claims, terminals = _journal_prefixes(occupancy_samples, start_boundary['journals'], end_boundary['journals'], owner)
    requests, episodes = {}, {}
    for boundary in [start_boundary, end_boundary]:
        require(not boundary['requests']['metadata_errors'], 'request_projection_errors')
        seen_episodes, seen_requests = set(), set()
        for row in boundary['requests']['episodes']:
            key = row['group'], row['run_id']
            require(key not in seen_episodes, 'duplicate_boundary_episode')
            seen_episodes.add(key)
            previous = episodes.get(key)
            if previous is not None:
                require(all(previous[k] == row[k] for k in ('group','run_id','full','claim')),
                        'immutable_episode_metadata_changed')
                if previous['final'] is not None:
                    require(previous['final'] == row['final'], 'existing_terminal_changed')
                for name in ('early_capture.json','actor_summary.json','summary.json','terminal_metadata'):
                    if name in previous:
                        require(previous[name] == row.get(name), 'existing_episode_capture_changed')
            episodes[key] = row
        for row in boundary['requests']['requests']:
            key = row['group'], row['run_id'], row['relative_request_directory']
            require(key not in seen_requests, 'duplicate_boundary_request')
            seen_requests.add(key)
            previous = requests.get(key)
            if previous is not None:
                require(all(previous[k] == row[k] for k in ('group','run_id','full','relative_request_directory','attempt','attempt_pin')),
                        'immutable_request_metadata_changed')
                if previous['receipt'] is not None:
                    require(previous['receipt'] == row['receipt'] and previous['receipt_pin'] == row['receipt_pin'],
                            'existing_request_receipt_changed')
                else:
                    require(previous['receipt_pin'] is None, 'receipt_pin_without_receipt')
                if previous['phase'] != row['phase']:
                    capture = episodes[(row['group'],row['run_id'])].get('early_capture.json',{}).get('fields',{})
                    parts = row['relative_request_directory'].split('/')
                    require(previous['phase'] == 'actor_prefix' and row['phase'] == 'actor_continuation'
                            and len(parts)==2 and parts[0]=='actor' and parts[1].isdigit()
                            and type(capture.get('calls')) is int and int(parts[1]) >= capture['calls'],
                            'unjustified_request_phase_change')
            requests[key] = row
    current = {}
    actors = []
    for key, row in episodes.items():
        if row['group'] != owner['group'] or row['claim'].get('controller_id') != owner['controller_id']:
            continue
        require(claims.get(row['run_id']) == row['claim'], 'claim_boundary_mismatch')
        require(bool(row['full']) == bool(row['claim'].get('full')), 'episode_full_mismatch')
        terminal = terminals.get(row['run_id'])
        if terminal:
            require(epoch(terminal['at']) >= epoch(row['claim']['at']), 'terminal_precedes_claim')
        if row['final']:
            require(row['final']['controller_id'] == owner['controller_id'] and terminal is not None
                    and epoch(terminal['at']) <= epoch(row['final']['at']), 'terminal_boundary_mismatch')
        current[key] = dict(row, final=terminal)
        start = epoch(row['claim']['at']); end = epoch(terminal['at']) if terminal else None
        metadata = row.get('terminal_metadata')
        classification = metadata.get('classification') if metadata else 'unprojected'
        require(classification in {'natural', 'censored', 'technical', 'unknown', 'unprojected'}, 'invalid_terminal_class')
        actors.append({'run_id': row['run_id'], 'full': bool(row['full']), 'claim': row['claim'],
                       'terminal': terminal, 'terminal_in_window': end is not None and lo <= end < hi,
                       'classification': classification, 'terminal_metadata': metadata,
                       'occupied_seconds': max(0, min(end if end is not None else hi, hi) - max(start, lo))})
    for event in control_events:
        event_digest = hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        if event['event'] != 'disk_floor_wait' or event_digest not in wait_support['event_sha256']:
            issues.append('current_owner_pause_restart_floor_or_quarantine')
    request_rows = []
    for row in requests.values():
        episode = current.get((row['group'], row['run_id']))
        if episode is None:
            continue
        require(bool(row['full']) == bool(episode['full']), 'request_full_mismatch')
        start = epoch(row['attempt']['started_at'])
        receipt = row['receipt']; end = epoch(receipt['finished_at']) if receipt else None
        require(start >= epoch(episode['claim']['at']), 'request_precedes_claim')
        require(end is None or end >= start, 'receipt_precedes_attempt')
        if start >= hi or end is not None and end < lo:
            continue
        unsupported = receipt is None and episode['final'] is not None and epoch(episode['final']['at']) <= hi
        if unsupported:
            issues.append('terminal_request_missing_receipt:' + row['run_id'] + ':' + row['relative_request_directory'])
        request_rows.append(dict({k: row[k] for k in ('group','run_id','full','relative_request_directory','phase','attempt','attempt_pin','receipt','receipt_pin')}, completed_in_window=end is not None and lo <= end < hi,
            fully_contained=end is not None and lo <= start <= end < hi,
            occupancy_supported=not unsupported,
            occupancy_measurement='unsupported_terminal_episode_missing_receipt' if unsupported else
                                  'receipt_interval' if receipt else 'pending_current_owner_estimate',
            started_epoch=start, finished_epoch=end,
            occupied_seconds=None if unsupported else max(0, min(end if end is not None else hi, hi)-max(start, lo))))
    sampled = []
    for sample in occupancy_samples:
        require({r['group'] for r in sample['controllers']} == set(declaration['groups']), 'sample_group_set')
        observed = next(c for c in sample['controllers'] if c['group'] == owner['group'])
        projected = prefixes[owner['group'], observed['journal_sha256']]
        declared = sample['current_owner']
        require(declared['controller_id'] == owner['controller_id'] and declared['group'] == owner['group']
                and declared['active_full'] == projected['full'] and declared['active_prefix'] == projected['prefix']
                and sorted(declared['active_run_ids']) == projected['run_ids'], 'sample_occupancy_disagrees')
        if projected['full'] > owner['max_full'] or projected['full'] + projected['prefix'] > owner['workers']:
            issues.append('capacity_occupancy_exceeded:' + str(sample['index']))
        sampled.append(projected)
    m0, m1 = window['start_monotonic_seconds'], window['end_monotonic_seconds']
    weights = [max(0, min(b['monotonic_seconds'], m1) - max(a['monotonic_seconds'], m0))
               for a, b in zip(occupancy_samples, occupancy_samples[1:])]
    require(abs(sum(weights)-seconds) < 1e-6, 'incomplete_fixed_occupancy_interval')
    occupancy = sum(row['full'] * weight for row, weight in zip(sampled, weights)) / seconds
    result_metrics = {}
    for name, rows in [('overall', request_rows), ('full', [r for r in request_rows if r['full']]),
                       ('continuation', [r for r in request_rows if r['full'] and r['phase'] == 'actor_continuation']),
                       ('prefix', [r for r in request_rows if not r['full']])]:
        result_metrics[name] = population(rows, seconds)
    result_metrics.update(mean_active_full=occupancy,
                          pending_requests=sum(r['started_epoch'] < hi and (r['finished_epoch'] is None or r['finished_epoch'] >= hi)
                                               and r['occupancy_supported'] for r in request_rows))
    counter_info = {}
    first, last = metrics(samples[0]), metrics(samples[-1])
    metric_seconds = (samples[-1]['metrics_capture_finished']['monotonic_seconds']
                      - samples[0]['metrics_capture_finished']['monotonic_seconds'])
    require(metric_seconds > 0, 'metric_interval_nonpositive')
    for name in ['generation_tokens_total', 'prompt_tokens_total', 'request_success_total', 'num_preemptions_total']:
        values = [metric(metrics(s), name) for s in samples]
        if any(b < a for a, b in zip(values, values[1:])):
            issues.append('counter_reset:' + name)
        counter_info[name] = {'delta': values[-1] - values[0], 'actual_interval_seconds': metric_seconds,
                              'per_actual_metric_second': (values[-1] - values[0]) / metric_seconds}
    for key in ['oom_kill', 'pswpin', 'pswpout']:
        values = [s['vmstat'][key] for s in samples]
        require(all(type(v) is int and v >= 0 for v in values), 'invalid_vm_counter')
        if any(a != b for a, b in zip(values, values[1:])):
            issues.append('vm_counter_changed:' + key)
    if counter_info['num_preemptions_total']['delta'] != 0:
        issues.append('preemption_increment')
    prompt_bands = []
    for full, phase, band in sorted({(r['full'], r['phase'], token_band(r)) for r in request_rows if r['completed_in_window']}):
        rows = [r for r in request_rows if (r['full'],r['phase'],token_band(r)) == (full,phase,band)]
        prompt_bands.append({'full': full, 'phase': phase, 'prompt_band': band, **population(rows, seconds)})
    return {'schema': 'causes.capacity_window_summary.v1', 'capacity_wait_support': wait_support, 'window': window,
            'support': {'supported': not issues, 'issues': sorted(set(issues))},
            'requests': request_rows, 'actors': actors, 'metrics': result_metrics,
            'descriptive': {'counter_intervals': counter_info, 'owner_projection': owner,
                            'sample_occupancy': sampled, 'prompt_bands': prompt_bands,
                            'historical_boundary_episodes_excluded': len(episodes)-len(current),
                            'terminal_counts_are_dispositions_not_useful_completions': True,
                            'completion_tokens_include_left_straddlers': True,
                            'actual_metric_interval_distinct_from_fixed_request_window': True,
                            'pending_endpoint_semantics':'Requests started before fixed hi with absent receipt or receipt finish >=hi; exact-hi completion belongs to the next half-open block.'},
            'runtime_actions': False}


def _supports(metrics, minima):
    reasons = []
    for population_name, prefix in [('full','full'),('continuation','continuation'),('prefix','prefix')]:
        m = metrics[population_name]
        if m['successful'] < minima[prefix + '_successful_requests_min']:
            reasons.append(prefix + '_successful_request_support')
        if m['successful_actors'] < minima[prefix + '_unique_actors_min']:
            reasons.append(prefix + '_actor_support')
        if m['usage_coverage'] != 1 or not m['elapsed_supported']:
            reasons.append(prefix + '_usage_or_elapsed_support')
    for name in ['continuation','prefix']:
        if not metrics[name]['service_productivity'] or metrics[name]['service_productivity'] <= 0:
            reasons.append(name + '_zero_service_reference')
    return reasons


def evaluate_reference(summary, criteria):
    m, required = summary['metrics'], criteria['reference_support']
    reasons = list(summary['support']['issues']) + _supports(m, required)
    if summary['window']['scheduled_duration_seconds'] != criteria['reference_selection']['duration_seconds']:
        reasons.append('reference_duration')
    if m['mean_active_full'] < required['mean_full_occupied_workers_min']:
        reasons.append('full_exposure')
    for name in ['full','continuation']:
        if m[name]['tokens_per_second'] <= 0:
            reasons.append(name + '_zero_progress_reference')
    for name in ['overall','full']:
        value = m[name]['failure_fraction']
        if value is None or value > required[name + '_failure_fraction_max']:
            reasons.append(name + '_failure_fraction')
    return {'supported': not reasons, 'decision': 'eligible_reference' if not reasons else 'unsupported_reference',
            'reasons': sorted(set(reasons)), 'measurements': m, 'runtime_actions': False,
            'capacity_change_authorized': False}


def actor_terminal_guard(actors, criteria, lo, hi, *, cohort_start):
    require(len({r['run_id'] for r in actors}) == len(actors), 'duplicate_actor_identity')
    for row in actors:
        require(row['classification'] in {'natural','censored','technical','unknown','unprojected'},
                'invalid_cumulative_terminal_class')
        if row['terminal'] is not None:
            require(epoch(row['terminal']['at']) >= epoch(row['claim']['at']), 'cumulative_terminal_precedes_claim')
            for event in [row['claim'],row['terminal']]:
                require(event.get('run_id',row['run_id']) == row['run_id'], 'cumulative_actor_identity_mismatch')
            require(row['claim'].get('controller_id') == row['terminal'].get('controller_id'),
                    'cumulative_terminal_owner_mismatch')
    selected = [r for r in actors if r['full'] and epoch(r['claim']['at']) >= cohort_start
                and r['terminal'] and lo <= epoch(r['terminal']['at']) < hi]
    unknown = [r for r in selected if r['classification'] in ('unknown','unprojected')]
    count = len(selected)-len(unknown)
    failed = sum(r['classification'] == 'technical' for r in selected)
    gate = criteria['guards']['full_actor_technical_terminals']
    hold = (count >= gate['minimum_terminal_dispositions'] and failed >= gate['minimum_technical_failures']
            and failed / count >= gate['technical_fraction_hold_at_or_above'])
    return {'supported': not unknown, 'hold': hold or bool(unknown), 'terminals': count,
            'technical': failed, 'unknown': len(unknown), 'selected_run_ids': [r['run_id'] for r in selected]}


def request_failure_guard(measurements, reference_measurements):
    """The adopted dense/sparse full and overall predicates, shared by endpoints."""
    m, b, reasons = measurements, reference_measurements, []
    for name, key, add, floor, minimum, failures, sparse_actors in [
            ('full','full_timeout_and_technical_failure',.04,.08,20,3,2),
            ('overall','overall_timeout_and_technical_failure',.03,.05,30,4,3)]:
        n, f = m[name]['completed'], m[name]['failed']
        threshold = max(floor, b[name]['failure_fraction'] + add)
        if ((n >= minimum and f >= failures and m[name]['failure_fraction'] > threshold)
                or (n < minimum and f >= failures and m[name]['failed_actors'] >= sparse_actors)):
            reasons.append(name + '_request_failure_guard')
    return reasons


def evaluate_candidate(summary, reference, criteria, *, prior_blocks=(), cohort_start=None,
                       cumulative_actors=None, confirmation=False):
    """Evaluate fixed endpoints only; callers separately apply reviewed control actions."""
    require(reference['decision'] == 'eligible_reference' and reference['supported'], 'unqualified_reference')
    m, b = summary['metrics'], reference['measurements']
    reasons = list(summary['support']['issues'])
    hold = bool(reasons)
    for name in ['overall','full','continuation','prefix']:
        if m[name]['successful'] and (m[name]['usage_coverage'] != 1 or not m[name]['elapsed_supported']):
            reasons.append(name+'_request_metadata_integrity');hold=True
    expected_seconds = 7200 if confirmation else 1800
    require(summary['window']['scheduled_duration_seconds'] == expected_seconds, 'candidate_window_duration')
    if m['mean_active_full'] < 3.5:
        reasons.append('candidate_full_exposure');hold = True
    failure_reasons = request_failure_guard(m, b)
    reasons += failure_reasons; hold = hold or bool(failure_reasons)
    lo, hi = epoch(summary['window']['start_wall']), epoch(summary['window']['end_wall'])
    require(cohort_start is not None and cumulative_actors is not None, 'candidate_actor_cohort_required')
    actor_checks = [actor_terminal_guard(summary['actors'],criteria,lo,hi,cohort_start=cohort_start),
                    actor_terminal_guard(cumulative_actors,criteria,cohort_start,hi,cohort_start=cohort_start)]
    if any(r['hold'] for r in actor_checks):
        reasons.append('full_actor_terminal_guard');hold = True
    completed_success = [r for r in summary['requests'] if r['completed_in_window'] and r['receipt'].get('status')=='complete']
    if m['pending_requests'] and all(not any(a <= r['finished_epoch'] < z for r in completed_success)
                                    for a,z in [(hi-1200,hi-600),(hi-600,hi)]):
        reasons.append('endpoint_progress_stall');hold = True
    ratios = {}
    for name in ['continuation','prefix']:
        value, base = m[name]['service_productivity'], b[name]['service_productivity']
        ratios[name + '_service'] = value / base if value is not None and base and m[name]['usage_coverage']==1 else None
    if prior_blocks and not confirmation:
        previous = prior_blocks[-1]
        require(previous['window']['end_wall']==summary['window']['start_wall'], 'nonadjacent_prior_block')
        for name, n, actors in [('continuation',10,2),('prefix',20,5)]:
            def loss(block):
                p = block['metrics'][name]
                return (block['support']['supported'] and p['successful']>=n and p['successful_actors']>=actors
                        and p['usage_coverage']==1 and p['elapsed_supported'] and p['service_productivity'] is not None
                        and p['service_productivity']/b[name]['service_productivity'] < .8)
            if loss(previous) and loss(summary):
                reasons.append(name+'_adjacent_service_loss');hold=True
    if confirmation:
        reasons += _supports(m,criteria['candidate_windows']['confirmation_support'])
        for name, key in [('full','full_progress_ratio_min'),('continuation','continuation_progress_ratio_min')]:
            ratios[name+'_progress']=m[name]['tokens_per_second']/b[name]['tokens_per_second']
            if ratios[name+'_progress'] < criteria['adoption_gates'][key]:reasons.append(name+'_progress')
        for name in ['continuation','prefix']:
            if ratios[name+'_service'] is None or ratios[name+'_service'] < criteria['adoption_gates']['service_productivity_ratio_min']:
                reasons.append(name+'_service')
    return {'supported': summary['support']['supported'] and not any('support' in r for r in reasons),
            'decision':'hold' if hold else 'inconclusive' if reasons else 'eligible_candidate' if confirmation else 'continue_confirmation',
            'reasons':sorted(set(reasons)),'measurements':m,'ratios':ratios,'actor_guards':actor_checks,
            'runtime_actions':False,'capacity_change_authorized':False}


def evaluate_prelude(boundary, declaration, reference, criteria, *, cohort_start, end_wall):
    """Check [first candidate claim,t0) technical health without a progress gate.

    Malformed metadata raises and must hold the caller. Empty exposure is allowed;
    this is a health check, never fixed-window performance qualification.
    """
    require(reference['decision'] == 'eligible_reference' and reference['supported'], 'unqualified_reference')
    require(finite(cohort_start), 'prelude_cohort_start_required')
    lo, hi = cohort_start, epoch(end_wall)
    require(hi >= lo, 'prelude_end_precedes_cohort')
    owner = declaration['owner']; logs = boundary['journals']
    require(owner['workers'] == 8 and owner['max_full'] == 4, 'candidate_prelude_capacity_required')
    require({row['group'] for row in logs} == set(declaration['groups']), 'prelude_journal_group_set')
    _, claims, terminals = _journal_prefixes([], logs, logs, owner)
    require(claims and min(epoch(row['at']) for row in claims.values()) == lo, 'prelude_first_claim_changed')
    projection = boundary['requests']
    require(not projection['metadata_errors'], 'prelude_request_projection_errors')
    episodes, seen, actors, issues = {}, set(), [], []
    for row in projection['episodes']:
        key = row['group'], row['run_id']
        require(key not in seen, 'prelude_duplicate_episode'); seen.add(key)
        if row['group'] != owner['group'] or row['claim'].get('controller_id') != owner['controller_id']:
            continue
        require(claims.get(row['run_id']) == row['claim'], 'prelude_claim_mismatch')
        require(bool(row['full']) == bool(row['claim']['full']), 'prelude_episode_full_mismatch')
        terminal = terminals.get(row['run_id'])
        if terminal:
            require(epoch(terminal['at']) >= epoch(row['claim']['at']), 'prelude_terminal_precedes_claim')
        if row['final']:
            require(terminal is not None and row['final']['controller_id'] == owner['controller_id']
                    and epoch(terminal['at']) <= epoch(row['final']['at']), 'prelude_terminal_mismatch')
        metadata = row.get('terminal_metadata')
        classification = metadata.get('classification') if metadata else 'unprojected'
        require(classification in {'natural','censored','technical','unknown','unprojected'}, 'prelude_terminal_class_invalid')
        if row['full'] and terminal and lo <= epoch(terminal['at']) < hi and classification in ('unknown','unprojected'):
            issues.append('prelude_terminal_metadata_unsupported:' + row['run_id'])
        episodes[key] = dict(row, final=terminal)
        actors.append({'run_id':row['run_id'],'full':bool(row['full']),'claim':row['claim'],
                       'terminal':terminal,'classification':classification,'terminal_metadata':metadata})
    require(set(claims) <= {run for _,run in episodes}, 'prelude_current_episode_missing')
    seen, requests = set(), []
    for row in projection['requests']:
        key = row['group'], row['run_id'], row['relative_request_directory']
        require(key not in seen, 'prelude_duplicate_request'); seen.add(key)
        episode = episodes.get((row['group'],row['run_id']))
        if episode is None:
            require(row['group'] != owner['group'], 'prelude_request_episode_missing')
            continue
        require(bool(row['full']) == bool(episode['full']), 'prelude_request_full_mismatch')
        start = epoch(row['attempt']['started_at']); receipt = row['receipt']
        finish = epoch(receipt['finished_at']) if receipt else None
        require(start >= epoch(episode['claim']['at']), 'prelude_request_precedes_claim')
        require(finish is None or finish >= start, 'prelude_receipt_precedes_attempt')
        if receipt is None:
            require(row['receipt_pin'] is None, 'prelude_receipt_pin_without_receipt')
        if start < hi and receipt is None and episode['final'] is not None and epoch(episode['final']['at']) < hi:
            issues.append('prelude_terminal_request_missing_receipt:' + row['run_id'])
        if finish is not None and lo <= finish < hi:
            requests.append(row)
    measurements = {}
    for name, rows in [('overall',requests),('full',[r for r in requests if r['full']])]:
        success = [r for r in rows if r['receipt'].get('status') == 'complete']
        failure = [r for r in rows if r['receipt'].get('status') != 'complete']
        metadata_ok = all(type((r['receipt'].get('usage') or {}).get('completion_tokens')) is int
                          and r['receipt']['usage']['completion_tokens'] >= 0
                          and finite(r['receipt'].get('elapsed_seconds')) and r['receipt']['elapsed_seconds'] > 0
                          for r in success)
        if not metadata_ok: issues.append('prelude_' + name + '_request_metadata_integrity')
        measurements[name] = {'completed':len(rows),'successful':len(success),'failed':len(failure),
            'successful_actors':len({r['run_id'] for r in success}),
            'failed_actors':len({r['run_id'] for r in failure}),
            'failure_fraction':len(failure)/len(rows) if rows else None,
            'request_metadata_supported':metadata_ok}
    for log in logs:
        if log['group'] == owner['group'] and any(e.get('controller_id') == owner['controller_id']
                and lo <= epoch(e['at']) < hi and e['event'] in
                {'pause_seen','controller_paused','controller_started','disk_floor_wait','route_quarantined'}
                for e in log['events']):
            issues.append('prelude_owner_pause_restart_floor_or_quarantine')
    reasons = issues + request_failure_guard(measurements, reference['measurements'])
    actor = actor_terminal_guard(actors,criteria,lo,hi,cohort_start=lo)
    if actor['hold']: reasons.append('full_actor_terminal_guard')
    return {'schema':'causes.capacity_prelude_evaluation.v1','supported':not issues and actor['supported'],
            'decision':'hold' if reasons else 'continue_confirmation','reasons':sorted(set(reasons)),
            'interval':{'first_claim_epoch':lo,'end_wall':end_wall,'half_open':True},
            'measurements':measurements,'actor_guard':actor,
            'progress_duration_and_occupancy_qualification_performed':False,
            'runtime_actions':False,'capacity_change_authorized':False}


def passive_health_issues(samples):
    """Retrospective reference support only; no admission control is performed."""
    issues = []
    series = {'ram': [], 'queue': [], 'kv': []}
    for sample in samples:
        values = metrics(sample)
        series['ram'].append(sample['memory']['MemAvailable_KiB'] < 16 * 1024**2)
        series['queue'].append(metric(values, 'num_requests_waiting') > 0)
        series['kv'].append(metric(values, 'kv_cache_usage_perc') >= .95)
        if not sample.get('storage_guard', {}).get('healthy') or any(sample.get('controls_present', {}).values()):
            issues.append('storage_or_control_refusal')
    for name, count in [('ram',2),('queue',3),('kv',2)]:
        if any(all(series[name][i:i+count]) for i in range(len(samples)-count+1)):
            issues.append(name+'_threshold')
    for name in ['oom_kill','pswpin','pswpout']:
        values=[s['vmstat'][name] for s in samples]
        if any(a != b for a,b in zip(values,values[1:])):issues.append('vm_counter_changed:'+name)
    for name in ['generation_tokens_total','prompt_tokens_total','request_success_total','num_preemptions_total']:
        values=[metric(metrics(s),name) for s in samples]
        if any(b<a for a,b in zip(values,values[1:])):issues.append('counter_reset:'+name)
        if name=='num_preemptions_total' and values[-1]!=values[0]:issues.append('preemption_increment')
    return sorted(set(issues))


def prelude_support(prelude, first, declaration):
    require(prelude and len(prelude) <= declaration['eligibility']['max_wait_seconds']//30+1,
            'eligibility_prelude_missing_or_unbounded')
    require([r['index'] for r in prelude]==list(range(len(prelude))), 'prelude_indices')
    require(all(not r['eligible'] for r in prelude[:-1]) and prelude[-1]['eligible'], 'first_eligible_selection_changed')
    last=dict(prelude[-1]);last['index']=0;last['wall_minus_monotonic_delta_seconds']=0.0
    require(last==first, 'eligible_sample_not_reused')
    require(all(b['monotonic_seconds']>a['monotonic_seconds'] and epoch(b['at'])>epoch(a['at'])
                for a,b in zip(prelude,prelude[1:])), 'nonmonotone_prelude')
    issues=_identity_issues(prelude,declaration)+passive_health_issues(prelude)
    w0,m0=epoch(prelude[0]['at']),prelude[0]['monotonic_seconds']
    timing=declaration['timing']
    schedule=prelude[0]['scheduled_monotonic_seconds']
    for i,row in enumerate(prelude):
        require(abs(row['scheduled_monotonic_seconds']-(schedule+i*30))<1e-6,'prelude_schedule_changed')
        if not 0 <= row['monotonic_seconds']-row['scheduled_monotonic_seconds'] <= timing['max_lateness_seconds']:
            issues.append('prelude_lateness:'+str(i))
        _pair(row['at'],row['monotonic_seconds'],w0,m0,timing['max_clock_drift_seconds'],issues,'prelude:'+str(i))
        _span({'started':{'wall':row['at'],'monotonic_seconds':row['monotonic_seconds']},
               'finished':{'wall':row['finished_at'],'monotonic_seconds':row['finished_monotonic_seconds']}},
              w0,m0,timing['max_sample_observation_span_seconds'],timing['max_clock_drift_seconds'],issues,'prelude:'+str(i))
        for name in ['metrics_capture','journal_capture']:
            _span({'started':row[name+'_started'],'finished':row[name+'_finished']},w0,m0,
                  timing['max_sample_observation_span_seconds'],timing['max_clock_drift_seconds'],issues,'prelude_'+name+':'+str(i))
    owner=first['current_owner'];require(owner['active_full']==declaration['eligibility']['active_full']
         and owner['active_full']+owner['active_prefix']==declaration['eligibility']['active_total'],'eligibility_occupancy_changed')
    return issues


def summarize_directory(directory, declaration, criteria):
    """Read only the sampler's retained metadata files, never referenced actor files."""
    directory=Path(directory)
    input_pins=[]
    def read(name):
        path=directory/name;raw=path.read_bytes()
        input_pins.append({'path':str(path.resolve()),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
        return json.loads(raw)
    receipt=read('RECEIPT.json')
    require(receipt['source_sha256']==declaration['sampler_sha256'],'sampler_source_receipt_changed')
    require(receipt['seconds']==1800 and receipt['interval']==30 and receipt['new_actor_or_model_calls']==0
            and receipt['concurrency_changes'] is False,'passive_receipt_changed')
    for name in ['FAILED.json','INELIGIBLE.json','PRELUDE_INVALID.json']:
        if (directory/name).exists():
            disposition=read(name)
            return {'schema':'causes.capacity_reference_report.v1','evaluation':{'supported':False,
                    'decision':'unsupported_reference','reasons':[name.removesuffix('.json').lower()]},
                    'collection_disposition':disposition,'input_pins':input_pins,'runtime_actions':False}
    complete=read('COMPLETE.json')
    require(complete['samples']==61,'sample_count')
    sample_paths=sorted(directory.glob('sample_*.json'))
    require([p.name for p in sample_paths]==['sample_%02d.json'%i for i in range(61)],'sample_files')
    samples=[read(p.name) for p in sample_paths]
    boundaries=[]
    for index in [0,60]:
        boundaries.append({'requests':read('requests_%02d.json'%index),
                           'journals':read('journals_%02d.json'%index),
                           'capture':read('boundary_%02d.json'%index)})
    summary=summarize_window(samples,*boundaries,complete['window'],declaration)
    prelude=[read(p.name) for p in sorted(directory.glob('eligibility_*.json'))]
    issues=summary['support']['issues']+prelude_support(prelude,samples[0],declaration)+passive_health_issues(samples)
    summary['support']={'supported':not issues,'issues':sorted(set(issues))}
    evaluation=evaluate_reference(summary,criteria)
    for ref in input_pins:
        raw=Path(ref['path']).read_bytes()
        require(len(raw)==ref['bytes'] and hashlib.sha256(raw).hexdigest()==ref['sha256'],'retained_input_changed')
    return {'schema':'causes.capacity_reference_report.v1','summary':summary,'evaluation':evaluation,
            'input_pins':input_pins,'runtime_actions':False,'capacity_change_authorized':False}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',required=True)
    parser.add_argument('--declaration',required=True)
    parser.add_argument('--declaration-sha256',required=True)
    parser.add_argument('--criteria',required=True)
    parser.add_argument('--criteria-sha256',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    def pinned(path,digest):
        raw=Path(path).read_bytes();require(hashlib.sha256(raw).hexdigest()==digest,'input_pin_changed')
        return json.loads(raw),{'path':str(Path(path).resolve()),'bytes':len(raw),'sha256':digest}
    declaration,declaration_pin=pinned(args.declaration,args.declaration_sha256)
    criteria,criteria_pin=pinned(args.criteria,args.criteria_sha256)
    require(hashlib.sha256(Path(args.declaration).read_bytes()).hexdigest()==args.declaration_sha256,'declaration_changed')
    output=Path(args.output);output.mkdir(exist_ok=False)
    receipt={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'declaration':declaration_pin,
             'criteria':criteria_pin,'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'directory':str(Path(args.directory).resolve()),'runtime_actions':False}
    (output/'INVOCATION.json').open('x').write(json.dumps(receipt,indent=2)+'\n')
    try:
        collection_receipt=json.loads((Path(args.directory)/'RECEIPT.json').read_bytes())
        require(collection_receipt['plan_sha256']==args.declaration_sha256,'collection_declaration_mismatch')
        report=summarize_directory(args.directory,declaration,criteria)
        report.update(declaration=declaration_pin,criteria=criteria_pin)
        path=output/'REPORT.json';path.open('x').write(json.dumps(report,indent=2,allow_nan=False)+'\n')
        raw=path.read_bytes();print(json.dumps({'path':str(path.resolve()),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest(),
                                               'decision':report['evaluation']['decision']}))
    except BaseException as exc:
        (output/'FAILED.json').open('x').write(json.dumps({'error_class':type(exc).__name__,'error':str(exc),
            'partial_outputs_preserved':True,'runtime_actions':False},indent=2)+'\n')
        raise


if __name__=='__main__':
    main()
