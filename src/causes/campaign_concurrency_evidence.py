"""Describe concurrency from retained request intervals and telemetry counters."""

import datetime as dt
import copy
import hashlib
import json
import math
from pathlib import Path
import types

from causes import campaign_runner as runner
from causes.campaign_actor_evidence import _read
from causes.campaign_handoff import canonical_path


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _number(value, reason):
    _require(type(value) in (int, float) and math.isfinite(value) and value >= 0, reason)
    return value


def _time(value):
    parsed = dt.datetime.fromisoformat(value)
    _require(parsed.tzinfo is not None, 'timezone_required')
    return parsed.timestamp()


def _union(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([start, end])
    return result


def actor_request_exposure(episodes, *, maximum_clock_difference_seconds):
    """Project independently audited actor receipts, excluding self requests.

    Each episode has a unique run_id and ordered requests from the actor audit.
    This function checks timing, not request content or closed-payload custody.
    A clock discrepancy is retained and makes temporal evidence inconclusive.
    """
    tolerance = _number(maximum_clock_difference_seconds, 'invalid_clock_tolerance')
    identities, projected, by_run = set(), [], []
    for episode in episodes:
        rid = episode['run_id']
        _require(isinstance(rid, str) and rid and rid not in identities, 'duplicate_or_invalid_run')
        identities.add(rid)
        requests, rows, intervals = episode['requests'], [], []
        _require(isinstance(requests, list) and requests, 'actor_requests_required')
        previous_end = None
        for index, row in enumerate(requests):
            _require(row['turn'] == index, 'actor_turn_order')
            started, finished = _time(row['started_at']), _time(row['finished_at'])
            elapsed = _number(row['elapsed_seconds'], 'invalid_request_elapsed')
            _require(finished >= started and (previous_end is None or started >= previous_end),
                     'actor_request_chronology')
            difference = finished - started - elapsed
            rows.append({'turn': index, 'started_at': row['started_at'], 'finished_at': row['finished_at'],
                         'wall_seconds': finished - started, 'receipt_elapsed_seconds': elapsed,
                         'clock_difference_seconds': difference, 'timing_consistent': abs(difference) <= tolerance})
            intervals.append((started, finished))
            previous_end = finished
        by_run.append(intervals)
        projected.append({'run_id': rid, 'successful_requests': len(rows),
                          'actor_span_seconds': intervals[-1][1] - intervals[0][0],
                          'request_elapsed_seconds': sum(row['receipt_elapsed_seconds'] for row in rows),
                          'timing_consistent': all(row['timing_consistent'] for row in rows), 'requests': rows})
    _require(len(projected) >= 2, 'multiple_episodes_required')
    overlaps = []
    for index, first in enumerate(by_run):
        for second in by_run[index + 1:]:
            overlaps.extend((max(a, c), min(b, d)) for a, b in first for c, d in second if max(a, c) < min(b, d))
    merged = _union(overlaps)
    return {'episodes': projected, 'successful_requests': sum(row['successful_requests'] for row in projected),
            'sum_actor_span_seconds': sum(row['actor_span_seconds'] for row in projected),
            'cross_job_actor_overlap_seconds': sum(end - start for start, end in merged),
            'cross_job_actor_overlap_intervals': merged,
            'timing_consistent': all(row['timing_consistent'] for row in projected),
            'scope': 'Actor receipts only; no self-request time, capacity decision or statistical inference.'}


def aggregate_generation_windows(rows, *, concurrency, minimum_seconds, maximum_seconds):
    """Retain every adjacent telemetry bin and pool eligible counter deltas.

    The caller supplies verified counters and active/running/waiting counts.
    Eligibility uses both endpoints and unchanged claim/terminal counts. It
    measures a busy-window aggregate rate, not an end-to-end speedup estimate.
    """
    _require(type(concurrency) is int and concurrency > 0, 'invalid_concurrency')
    minimum_seconds = _number(minimum_seconds, 'invalid_minimum_seconds')
    maximum_seconds = _number(maximum_seconds, 'invalid_maximum_seconds')
    _require(0 < minimum_seconds <= maximum_seconds and len(rows) >= 2, 'invalid_window_bounds')
    checked = []
    for row in rows:
        timestamp = _time(row['at'])
        if checked:
            _require(timestamp > checked[-1][0], 'telemetry_order')
        for key in ('active', 'running', 'waiting', 'claims', 'terminals'):
            _require(type(row[key]) is int and row[key] >= 0, 'invalid_count:' + key)
        _require(type(row['healthy']) is bool, 'explicit_health_required')
        _number(row['generation_tokens'], 'invalid_generation_counter')
        checked.append((timestamp, row))
    windows, total_tokens, total_seconds = [], 0, 0
    for (ta, a), (tb, b) in zip(checked, checked[1:]):
        elapsed, delta = tb - ta, b['generation_tokens'] - a['generation_tokens']
        reasons = []
        if not (a['healthy'] and b['healthy']):
            reasons.append('unhealthy_endpoint')
        if any(row['active'] != concurrency or row['running'] != concurrency or row['waiting'] != 0 for row in (a, b)):
            reasons.append('concurrency_or_waiting')
        if a['claims'] != b['claims'] or a['terminals'] != b['terminals']:
            reasons.append('claim_or_terminal_transition')
        if not minimum_seconds <= elapsed <= maximum_seconds:
            reasons.append('interval_outside_bounds')
        if delta < 0:
            reasons.append('counter_decreased')
        eligible = not reasons
        if eligible:
            total_tokens += delta
            total_seconds += elapsed
        windows.append({'from': a['at'], 'to': b['at'], 'seconds': elapsed, 'tokens': delta,
                        'eligible': eligible, 'excluded_reasons': reasons})
    return {'windows': windows, 'eligible_bins': sum(row['eligible'] for row in windows),
            'generation_tokens': total_tokens, 'elapsed_seconds': total_seconds,
            'tokens_per_second': total_tokens / total_seconds if total_seconds else None,
            'counter_decreases': sum('counter_decreased' in row['excluded_reasons'] for row in windows),
            'scope': 'All eligible adjacent bins; pooled deltas, no best-window selection or fleet ETA.'}


def audit_self_probe_traffic(directory, job, config):
    """Replay saved self-probe parsing with isolated read-only exchange/save stubs.

    Native request, rendering, seed and answer parsing code is reused unchanged.
    The caller establishes the closed snapshot and runner-source identity first.
    Failed calls remain capacity concerns; completed but unusable measurements
    retain their native status without being classified as service failures.
    """
    root = canonical_path(directory)
    allocation = job['allocation']
    _require(_read(root, 'allocation.json') == allocation and _read(root, 'model.json') == config,
             'self_probe_identity_changed')
    summary = _read(root, 'summary.json')
    probes = summary.get('probes', [])
    selected = [q for q in config['questions'] if q['id'] in allocation['instruments']]
    _require(len(selected) == len(set(allocation['instruments'])), 'self_question_roster_changed')
    expected = [(stage, q['id']) for stage in ('early', 'end') for q in selected]
    _require([(p['stage'], p['instrument']) for p in probes] == expected, 'self_probe_stage_roster_changed')
    rows = []
    for probe, (stage, instrument) in zip(probes, expected):
        relative = Path('probes') / stage / instrument
        _require(relative.parts == ('probes', stage, instrument), 'unsafe_probe_identifier')
        status = probe['technical_status']
        if status == 'failure':
            rows.append({'stage': stage, 'instrument': instrument, 'technical_status': status,
                         'capacity_fault': True, 'error': probe.get('error'),
                         'scope': 'Native failed call retained; transport/protocol cause needs its saved evidence.'})
            continue
        _require(status in ('complete', 'invalid_answer_schema', 'truncated'), 'unknown_probe_status')
        context = _read(root, stage + '_context.json')
        question, = [q for q in selected if q['id'] == instrument]
        request = _read(root, str(relative / 'request.json'))
        attempt = _read(root, str(relative / 'attempt.json'))
        receipt = _read(root, str(relative / 'receipt.json'))
        response_path = canonical_path(root / relative / 'response.raw')
        _require(response_path.is_relative_to(root), 'probe_response_outside_snapshot')
        raw = response_path.read_bytes()
        response = json.loads(raw)
        _require(attempt['request_sha256'] == receipt['request_sha256'] == runner.sha(request)
                 and receipt['response_sha256'] == hashlib.sha256(raw).hexdigest()
                 and attempt['retry'] is False and receipt['retry'] is False
                 and attempt['started_at'] == receipt['started_at']
                 and receipt['status'] == 'complete' and receipt['http_status'] == 200,
                 'self_request_receipt_changed')
        _require(response['model'] == config['model']
                 and all(receipt.get(key) == response.get(key) for key in ('model', 'usage', 'system_fingerprint')),
                 'self_response_metadata_changed')
        _number(receipt['elapsed_seconds'], 'invalid_self_request_elapsed')
        _require(_time(receipt['finished_at']) >= _time(receipt['started_at']), 'self_request_chronology')
        exchanged, saved = [], []

        def exchange(endpoint, payload, destination, timeout):
            _require(endpoint == config['endpoint'] and payload == request and destination == root / relative
                     and timeout == config.get('request_timeout_seconds', 600), 'self_request_changed')
            exchanged.append(True)
            return copy.deepcopy(response)

        def save(path, result):
            _require(path == root / relative / 'result.json'
                     and result == _read(root, str(relative / 'result.json')), 'self_result_changed')
            saved.append(True)

        native = types.FunctionType(runner.probe.__code__, {**runner.probe.__globals__, 'exchange': exchange, 'save': save})
        reconstructed = native(config, context, question, runner.seed_for(allocation['seed'], ['probe', stage, instrument]), root / relative)
        _require(exchanged == saved == [True] and probe == {'stage': stage, **reconstructed}, 'self_summary_changed')
        rows.append({'stage': stage, 'instrument': instrument, 'technical_status': status, 'capacity_fault': False,
                     'request_sha256': receipt['request_sha256'], 'response_sha256': receipt['response_sha256'],
                     'context_sha256': reconstructed['context_sha256'], 'question_sha256': reconstructed['question_sha256'],
                     'elapsed_seconds': receipt['elapsed_seconds'], 'finish_reason': reconstructed['finish_reason'],
                     'usage': receipt.get('usage')})
    return {'run_id': allocation['run_id'], 'probes': rows, 'capacity_faults': sum(row['capacity_fault'] for row in rows),
            'completed_http_requests': sum(not row['capacity_fault'] for row in rows),
            'measurement_only_technical': sum(row['technical_status'] in ('invalid_answer_schema', 'truncated') for row in rows),
            'model_calls': 0, 'scope': 'Self traffic only; native statuses retained and no awareness-effect comparison.'}
