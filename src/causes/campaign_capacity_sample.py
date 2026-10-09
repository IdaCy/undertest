from pathlib import Path
import argparse
import datetime
import hashlib
import importlib
import json
import os
import shutil
import sys
import time
import urllib.parse
import urllib.request


_SOURCE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
TERMINAL_EVENTS = {'episode_finished', 'unresolved_current_attempt', 'existing_completed_summary',
                   'unresolved_prior_attempt', 'unresolved_completed_evidence',
                   'unresolved_identity_conflict'}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def epoch(value):
    return datetime.datetime.fromisoformat(value).timestamp()


def save(path, value):
    with path.open('x') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def metadata(path):
    limit = 4*1024**2 if path.name in {'summary.json','actor_summary.json'} else 262144
    assert not path.is_symlink() and path.stat().st_size <= limit, 'metadata_bound'
    for retry in range(3):
        raw = path.read_bytes()
        try:
            return json.loads(raw), {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        except json.JSONDecodeError:
            if retry == 2:
                raise
            time.sleep(.05)


def journal(base, model_key, group):
    path = base / 'runs' / group / model_key / '.dispatcher/journal.jsonl'
    assert path.stat().st_size < 64 * 1024**2
    raw = path.read_bytes()
    complete = raw[:raw.rfind(b'\n') + 1]
    return {'group': group, 'path': str(path), 'bytes': len(complete), 'sha256': hashlib.sha256(complete).hexdigest(),
            'incomplete_tail_bytes': len(raw) - len(complete), 'events': [json.loads(line) for line in complete.splitlines()]}


def requests(base, model_key, journals, lo):
    rows, episodes, errors = [], [], []
    for log in journals:
        claims = {row['run_id']: row for row in log['events'] if row['event'] == 'claimed'}
        finished = {row['run_id']: row for row in log['events'] if row['event'] == 'episode_finished'}
        for run_id, claim in claims.items():
            if run_id in finished and epoch(finished[run_id]['at']) < lo:
                continue
            directory = base / 'runs' / log['group'] / model_key / run_id
            episode = {'group': log['group'], 'run_id': run_id, 'full': bool(claim.get('full')),
                       'claim': claim, 'final': finished.get(run_id)}
            early_calls = None
            for filename in ['early_capture.json', 'actor_summary.json', 'summary.json']:
                path = directory / filename
                if path.exists():
                    value, pin = metadata(path)
                    episode[filename] = {'pin': pin, 'fields': {key: value.get(key) for key in [
                        'started_at', 'finished_at', 'early_elapsed_seconds', 'elapsed_seconds',
                        'calls', 'tool_results', 'early_status', 'end_status']}}
                    if filename == 'early_capture.json':
                        early_calls = value.get('calls')
            episodes.append(episode)
            attempts = sorted(directory.glob('actor/*/attempt.json')) + sorted(directory.glob('probes/*/*/attempt.json'))
            for path in attempts:
                try:
                    attempt, attempt_pin = metadata(path)
                    receipt_path = path.with_name('receipt.json')
                    receipt, receipt_pin = metadata(receipt_path) if receipt_path.exists() else (None, None)
                    if receipt and epoch(receipt['finished_at']) < lo:
                        continue
                    relative = path.parent.relative_to(directory)
                    if relative.parts[0] == 'actor':
                        turn = int(relative.parts[1])
                        phase = 'actor_prefix' if early_calls is None or turn < early_calls else 'actor_continuation'
                    else:
                        phase = 'probe_' + relative.parts[1]
                    rows.append({'group': log['group'], 'run_id': run_id, 'full': bool(claim.get('full')),
                                 'relative_request_directory': str(relative), 'phase': phase,
                                 'attempt': request_fields(attempt), 'attempt_pin': attempt_pin, 'receipt': request_fields(receipt) if receipt is not None else None, 'receipt_pin': receipt_pin})
                except Exception as exc:
                    errors.append({'path': str(path), 'error': repr(exc)})
    return {'observed_at': now(), 'requests': rows, 'episodes': episodes, 'metadata_errors': errors,
            'large_request_response_context_or_actor_text_read': False}


def request_fields(value):
    keys = ['started_at','finished_at','elapsed_seconds','status','http_status','retry','request_sha256','response_sha256','usage']
    result = {key: value[key] for key in keys if key in value}
    if 'usage' in result and isinstance(result['usage'], dict):
        result['usage'] = {key: result['usage'][key] for key in ['prompt_tokens','completion_tokens','total_tokens'] if key in result['usage']}
    error = str(value.get('error') or '')
    if error:
        result['error_class'] = next((kind for kind in ['TimeoutError','HTTPError','URLError','ConnectionError','ValueError'] if error.startswith(kind)), 'other')
    return result


def require(value, reason):
    if not value:
        raise ValueError(reason)


def checked(ref):
    path = Path(ref['path'])
    require(path.is_absolute() and not path.is_symlink(), 'pinned_regular_path_required')
    require(path.stat().st_size == ref['bytes'], 'pinned_size_changed')
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == ref['sha256'], 'pinned_bytes_changed')
    return raw


def validate_plan(plan):
    require(plan['schema'] == 'causes.passive_capacity_sample.v1', 'plan_schema')
    require(plan['sampler_sha256'] == _SOURCE_SHA
            == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'sampler_source_changed')
    require(type(plan['duration_seconds']) is int and plan['duration_seconds'] == 1800
            and type(plan['interval_seconds']) is int and plan['interval_seconds'] == 30,
            'fixed_sampling_window_required')
    for key in ['campaign_root', 'output']:
        path = Path(plan[key])
        require(path.is_absolute() and '..' not in path.parts, 'absolute_plan_path_required')
    leaves = [plan['model_key'], *plan['groups']]
    require(plan['groups'] and len(set(plan['groups'])) == len(plan['groups'])
            and all(type(v) is str and v not in ('', '.', '..') and Path(v).name == v
                    for v in leaves), 'unique_root_names_required')
    url = urllib.parse.urlsplit(plan['metrics_endpoint'])
    require(url.scheme == 'http' and url.hostname in ('127.0.0.1', '::1')
            and url.path == '/metrics' and not url.query and not url.fragment
            and url.username is None and url.password is None, 'loopback_metrics_only')
    owner = plan['owner']
    require(owner['group'] in plan['groups'] and owner['controller_id'], 'current_owner_required')
    require(type(owner['workers']) is int and type(owner['max_full']) is int
            and 0 < owner['max_full'] <= owner['workers'], 'valid_capacity_required')
    require(len({p['role'] for p in plan['processes']}) == len(plan['processes'])
            and owner['role'] in {p['role'] for p in plan['processes']}, 'unique_process_roles_required')
    for process in plan['processes']:
        require(type(process['pid']) is int and process['pid'] > 0
                and str(process['process_starttime_ticks']).isdigit()
                and process['argv'] and all(type(v) is str for v in process['argv']),
                'exact_process_identity_required')
    eligibility = plan['eligibility']
    require(type(eligibility['max_wait_seconds']) is int
            and eligibility['max_wait_seconds'] >= 0
            and eligibility['max_wait_seconds'] % 30 == 0,
            'bounded_eligibility_wait_required')
    require(eligibility['active_full'] == owner['max_full']
            and eligibility['active_total'] == owner['workers'], 'capacity_trigger_mismatch')
    require(plan['timing']['max_lateness_seconds'] >= 0
            and plan['timing']['max_clock_drift_seconds'] >= 0, 'timing_limits_required')


def identity_pins(plan):
    refs = [*plan['required_pins'], plan['owner']['policy'], plan['owner']['identity'],
            plan['guard']['source'], plan['qualification']['pin']]
    by_path = {}
    for ref in refs:
        require(ref['path'] not in by_path or by_path[ref['path']] == ref,
                'conflicting_identity_pins')
        by_path[ref['path']] = ref
    return list(by_path.values())


def passive_identity_and_memory(plan, proc_root=Path('/proc')):
    value = {'vmstat': {}, 'memory': {}, 'loadavg': list(os.getloadavg()), 'cpu_count': os.cpu_count()}
    for line in (proc_root / 'vmstat').read_text().splitlines():
        key, number = line.split()
        if key in {'oom_kill', 'pswpin', 'pswpout'}:
            value['vmstat'][key] = int(number)
    for line in (proc_root / 'meminfo').read_text().splitlines():
        key, rest = line.split(':', 1)
        if key in {'MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'}:
            value['memory'][key + '_KiB'] = int(rest.split()[0])
    value['pin_errors'] = []
    for ref in identity_pins(plan):
        try:
            checked(ref)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            value['pin_errors'].append({'path': ref['path'], 'error_class': type(exc).__name__})
    identities = []
    for expected in plan['processes']:
        proc = proc_root / str(expected['pid'])
        item = {'role': expected['role'], 'pid': expected['pid'],
                'expected_start_ticks': expected['process_starttime_ticks']}
        try:
            ticks = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
            args = (proc / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')
            item.update(start_ticks=ticks, start_matches=ticks == expected['process_starttime_ticks'],
                        argv_sha256=hashlib.sha256(json.dumps(args).encode()).hexdigest(),
                        argv_matches=args == expected['argv'])
            if expected['role'] == plan['owner']['role']:
                owner = plan['owner']
                policy = json.loads(checked(owner['policy']))
                actual = json.loads(checked(owner['identity']))
                item['capacity_source'] = 'pinned_policy_and_controller_identity'
                item['workers'], item['max_full'] = actual['workers'], actual['max_full']
                item['qualified_cap_matches'] = (
                    actual == owner['identity_fields']
                    and actual['workers'] == policy['workers'] == policy['launch']['options']['workers'] == owner['workers']
                    and actual['max_full'] == policy['max_full'] == policy['launch']['options']['max_full'] == owner['max_full'])
        except (OSError, ValueError, IndexError, KeyError, TypeError) as exc:
            item['identity_error'] = type(exc).__name__
        identities.append(item)
    value['process_identities'] = identities
    return value


def current_owner_occupancy(logs, owner):
    log = next(row for row in logs if row['group'] == owner['group'])
    claims, terminals = {}, set()
    for event in log['events']:
        if event.get('controller_id') != owner['controller_id']:
            continue
        rid = event.get('run_id')
        if event['event'] == 'claimed':
            require(rid not in claims and rid not in terminals, 'duplicate_or_post_terminal_claim')
            claims[rid] = event
        elif event['event'] in TERMINAL_EVENTS:
            if rid in claims:
                require(epoch(event['at']) >= epoch(claims[rid]['at']), 'terminal_precedes_claim')
            terminals.add(rid)
    active = [v for k, v in claims.items() if k not in terminals]
    return {'group': owner['group'], 'controller_id': owner['controller_id'],
            'active_full': sum(bool(v.get('full')) for v in active),
            'active_prefix': sum(not v.get('full') for v in active),
            'active_run_ids': [v['run_id'] for v in active]}


def guard_observation(plan):
    ref = plan['guard']['source']
    checked(ref)
    module = importlib.import_module('causes.campaign_storage')
    require(Path(module.__file__).resolve() == Path(ref['path']).resolve(), 'guard_import_identity_changed')
    owner = next(p for p in plan['processes'] if p['role'] == plan['owner']['role'])
    owner = {k: owner[k] for k in ['pid', 'process_starttime_ticks', 'argv']}
    guard = plan['guard']
    return module.observe_guard(guard['directory'], guard['binding'], owner, guard['reporting_lock'])


def sample(plan, index, scheduled, observe_guard=guard_observation):
    row = {'index': index, 'at': now(), 'host': plan['host'],
           'monotonic_seconds': time.monotonic(), 'scheduled_monotonic_seconds': scheduled}
    row['lateness_seconds'] = row['monotonic_seconds'] - scheduled
    row.update(passive_identity_and_memory(plan))
    row['metrics_capture_started'] = {'wall': now(), 'monotonic_seconds': time.monotonic()}
    try:
        with urllib.request.urlopen(plan['metrics_endpoint'], timeout=8) as response:
            body = response.read(4000000).decode()
        row['metrics'] = [line for line in body.splitlines() if line and not line.startswith('#') and '_bucket{' not in line]
        row['metrics_finished_at'] = now()
    except Exception as exc:
        row['metrics_error'] = repr(exc)
    row['metrics_capture_finished'] = {'wall': now(), 'monotonic_seconds': time.monotonic()}
    row['gpu'] = []
    for device in Path('/sys/class/drm').glob('card[0-9]*/device'):
        item = {'card': device.parent.name}
        for field in ['gpu_busy_percent', 'mem_info_vram_total', 'mem_info_vram_used']:
            path = device / field
            if path.is_file():
                item[field] = path.read_text().strip()
        if len(item) > 1:
            row['gpu'].append(item)
    row['journal_capture_started'] = {'wall': now(), 'monotonic_seconds': time.monotonic()}
    logs = [journal(Path(plan['campaign_root']), plan['model_key'], group) for group in plan['groups']]
    row['journal_capture_finished'] = {'wall': now(), 'monotonic_seconds': time.monotonic()}
    row['controllers'] = []
    for log in logs:
        claims = {entry['run_id']: entry for entry in log['events'] if entry['event'] == 'claimed'}
        final = {entry['run_id'] for entry in log['events'] if entry['event'] == 'episode_finished'}
        active = [entry for run, entry in claims.items() if run not in final]
        row['controllers'].append({'group': log['group'], 'claims': len(claims), 'final': len(final),
                                   'active_prefix': sum(not entry.get('full') for entry in active),
                                   'active_full': sum(bool(entry.get('full')) for entry in active),
                                   'active_run_ids': [entry['run_id'] for entry in active],
                                   'journal_sha256': log['sha256']})
    row['storage'] = {str(path): shutil.disk_usage(path)._asdict() for path in map(Path, plan['storage_paths'])}
    try:
        row['current_owner'] = current_owner_occupancy(logs, plan['owner'])
    except Exception as exc:
        row['current_owner_error'] = type(exc).__name__ + ':' + str(exc)
    try:
        row['storage_guard'] = observe_guard(plan)
    except Exception as exc:
        row['storage_guard_error'] = type(exc).__name__ + ':' + str(exc)
    row['controls_present'] = {path: os.path.lexists(path) for path in plan['controls_absent']}
    row['finished_at'], row['finished_monotonic_seconds'] = now(), time.monotonic()
    return row, logs


def eligible(row, plan):
    # Occupancy selects the window; faults are retained, never used to pick a better one.
    owner = row.get('current_owner', {})
    trigger = plan['eligibility']
    return (owner.get('active_full') == trigger['active_full']
            and owner.get('active_full', 0) + owner.get('active_prefix', 0) == trigger['active_total'])


def identity_valid(row):
    return (not row.get('pin_errors') and 'current_owner_error' not in row
            and all(item.get('start_matches') is True and item.get('argv_matches') is True
                    and 'identity_error' not in item and item.get('qualified_cap_matches', True) is True
                    for item in row['process_identities']))


def capture_boundary(out, index, logs, lo, plan):
    before = {'wall': now(), 'monotonic_seconds': time.monotonic()}
    save(out / ('journals_%02d.json' % index), logs)
    projection = requests(Path(plan['campaign_root']), plan['model_key'], logs, lo)
    save(out / ('requests_%02d.json' % index), projection)
    save(out / ('boundary_%02d.json' % index), {
        'started': before, 'finished': {'wall': now(), 'monotonic_seconds': time.monotonic()},
        'request_projection_is_boundary_only': True,
        'journal_prefixes': [{k: log[k] for k in ['group', 'path', 'bytes', 'sha256', 'incomplete_tail_bytes']}
                             for log in logs]})


def collect(plan, out, sample_fn=sample):
    interval, duration = plan['interval_seconds'], plan['duration_seconds']
    prelude_start = time.monotonic()
    chosen = None
    for index in range(plan['eligibility']['max_wait_seconds'] // interval + 1):
        scheduled = prelude_start + index * interval
        remaining = scheduled - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        row, logs = sample_fn(plan, index, scheduled)
        row['eligible'] = eligible(row, plan)
        save(out / ('eligibility_%03d.json' % index), row)
        if not identity_valid(row):
            save(out / 'PRELUDE_INVALID.json', {'at': now(), 'reason': 'current_identity_invalid',
                                              'samples': index + 1, 'no_measurement_window_selected': True})
            return
        if row['eligible']:
            chosen = row, logs
            break
    if chosen is None:
        save(out / 'INELIGIBLE.json', {'at': now(), 'reason': 'occupancy_trigger_not_reached',
                                     'samples': index + 1, 'no_measurement_window_selected': True})
        return
    row, logs = chosen
    row['index'] = 0
    start, wall_start = row['monotonic_seconds'], row['at']
    window_lo = epoch(wall_start)
    count = duration // interval + 1
    timing_errors = []
    wall_end = (datetime.datetime.fromisoformat(wall_start)
                + datetime.timedelta(seconds=duration)).isoformat()
    for index in range(count):
        if index:
            scheduled = start + index * interval
            remaining = scheduled - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            row, logs = sample_fn(plan, index, scheduled)
        row['wall_minus_monotonic_delta_seconds'] = epoch(row['at']) - window_lo - (row['monotonic_seconds'] - start)
        if (row['lateness_seconds'] > plan['timing']['max_lateness_seconds']
                or abs(row['wall_minus_monotonic_delta_seconds']) > plan['timing']['max_clock_drift_seconds']):
            timing_errors.append(index)
        if index in (0, count - 1):
            capture_boundary(out, index, logs, window_lo, plan)
        save(out / ('sample_%02d.json' % index), row)
        print(json.dumps({'index': index, 'at': row['at'], 'controllers': row['controllers']}), flush=True)
    save(out / 'COMPLETE.json', {
        'at': now(), 'samples': count, 'elapsed_seconds': time.monotonic() - start,
        'window': {'start_wall': wall_start, 'end_wall': wall_end,
                   'start_monotonic_seconds': start, 'end_monotonic_seconds': start + duration,
                   'actual_last_sample_wall': row['at'], 'actual_last_sample_monotonic_seconds': row['monotonic_seconds'],
                   'scheduled_duration_seconds': duration, 'half_open_receipt_finish_interval': True},
        'timing_error_sample_indices': timing_errors,
        'qualification_not_inferred_from_collection_completion': True})


def run(plan_path, plan_sha256):
    raw = Path(plan_path).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == plan_sha256, 'plan_hash_changed')
    plan = json.loads(raw)
    validate_plan(plan)
    require(plan['execution_authorized'] is True, 'inactive_sampling_plan')
    for ref in identity_pins(plan):
        checked(ref)
    qualification = json.loads(checked(plan['qualification']['pin']))
    require(qualification.get(plan['qualification']['accepted_field']) is True,
            'required_prior_qualification_missing')
    out = Path(plan['output'])
    out.mkdir(exist_ok=False)
    receipt = {'at': now(), 'pid': os.getpid(), 'mode': 'read_only_fixed_study_baseline',
               'seconds': plan['duration_seconds'], 'interval': plan['interval_seconds'],
               'process_start_ticks': Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19],
               'plan_path': str(plan_path), 'plan_sha256': plan_sha256, 'source_sha256': _SOURCE_SHA,
               'concurrency_changes': False, 'new_actor_or_model_calls': 0}
    save(out / 'RECEIPT.json', receipt)
    try:
        collect(plan, out)
    except BaseException as exc:
        save(out / 'FAILED.json', {'at': now(), 'error_class': type(exc).__name__,
                                  'error': str(exc), 'partial_outputs_preserved': True})
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', required=True)
    parser.add_argument('--plan-sha256', required=True)
    args = parser.parse_args()
    run(args.plan, args.plan_sha256)


if __name__ == '__main__':
    main()
