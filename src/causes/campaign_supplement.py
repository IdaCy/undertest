"""Durable supplementary measurements with explicit model and family namespaces.

The caller owns authorization, an exclusive family lock, immutable input/source pins, and fresh host/route admission checks. This module supplies no runtime authority or route selection. Admission and source callbacks are checked before each claim; the worker must also check frozen sources immediately before a request. Partial or terminal attempts are never automatically replayed, including after a process restart.

Each output directory and its .control journal belong to exactly one immutable family/model binding. The journal is not multiplexed: a different model or family needs a separate output and control journal, with any applicable prior histories qualified explicitly by the caller.

The queue retains the two-client ceiling and 80 GiB local-storage floor. Callers may choose a stricter single-client bound; any host-specific resource or scientific scope must be qualified separately. Importing this module performs no I/O.
"""
import concurrent.futures
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading


def namespace_component(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value):
        raise IntegrityError('invalid_namespace_component')
    return value


class IntegrityError(RuntimeError):
    pass


class AdmissionHold(RuntimeError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def canonical(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or path.resolve() != path:
        raise IntegrityError('noncanonical_path')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise IntegrityError('symlink_path')
    return path


def fsync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def mkdir(path):
    path = canonical(path)
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for item in reversed(missing):
        item.mkdir()
        fsync_dir(item.parent)
    return path


def save(path, value):
    path = canonical(path)
    with path.open('xb') as stream:
        stream.write(encoded(value) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())
    fsync_dir(path.parent)


def pin(path):
    path = canonical(path)
    if not path.is_file():
        raise IntegrityError('missing_regular_file')
    raw = path.read_bytes()
    return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def pinned(item):
    path = canonical(item['path'])
    if not path.is_file():
        raise IntegrityError('missing_regular_file')
    raw = path.read_bytes()
    actual = {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    if actual != item:
        raise IntegrityError('file_pin_mismatch:' + item['path'])
    return raw


def load(path):
    return json.loads(canonical(path).read_bytes())


def journal(path, value):
    path = canonical(path)
    with path.open('ab') as stream:
        stream.write(encoded({'at': now(), **value}) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())
    fsync_dir(path.parent)


def measurement_path(output, job, *, family, model):
    family = namespace_component(family)
    model = namespace_component(model)
    output = canonical(output)
    pieces = job['measurement_id'].split('/')
    if len(pieces) != 5 or pieces[0] != family or pieces[1] != model:
        raise IntegrityError('measurement_identity_scope')
    if pieces[2:] != [job['run_id'], job['stage'], job['question_id']]:
        raise IntegrityError('measurement_identity_fields')
    if job.get('family') != family or job.get('model') != model:
        raise IntegrityError('measurement_family_or_model')
    for value in pieces:
        namespace_component(value)
    if job['stage'] not in ('early', 'end'):
        raise IntegrityError('measurement_stage')
    path = canonical(output.joinpath(*pieces[1:]))
    if not path.is_relative_to(output):
        raise IntegrityError('measurement_escape')
    return path


def read_history(path):
    raw = canonical(path).read_bytes() if path.exists() else b''
    if raw and not raw.endswith(b'\n'):
        raise IntegrityError('unterminated_journal_requires_manual_reconciliation')
    return [json.loads(line) for line in raw.splitlines()]


def attempt_directories(output, *, model):
    """Inspect only the fixed three-level measurement namespace, without symlinks."""
    model = canonical(canonical(output) / namespace_component(model))
    if not model.exists():
        return []
    found = []
    for run in sorted(model.iterdir()):
        canonical(run)
        if not run.is_dir():
            raise IntegrityError('unexpected_run_namespace_file')
        for stage in sorted(run.iterdir()):
            canonical(stage)
            if not stage.is_dir() or stage.name not in ('early', 'end'):
                raise IntegrityError('unexpected_stage_namespace')
            for question in sorted(stage.iterdir()):
                canonical(question)
                if not question.is_dir():
                    raise IntegrityError('unexpected_question_namespace_file')
                found.append(question)
    return found


def reconcile(output, jobs, history, *, family, model):
    pending, complete, ambiguous = [], [], []
    by_id = {job['measurement_id']: job for job in jobs}
    if len(by_id) != len(jobs):
        raise IntegrityError('duplicate_job_identity')
    claims, terminals = {}, {}
    counter = 0
    for row in history:
        event = row['event']
        if event == 'controller_started':
            inherited = row['inherited_failures']
            if not isinstance(inherited, int) or not 0 <= inherited < 3 or inherited < counter:
                raise IntegrityError('journal_failure_history_lost')
            counter = inherited
        if event not in ('claimed', 'route_success', 'route_failure'):
            continue
        mid = row['measurement_id']
        if mid not in by_id:
            raise IntegrityError('journal_unknown_measurement')
        if event == 'claimed':
            if mid in claims or mid in terminals or row['input_sha256'] != digest(by_id[mid]):
                raise IntegrityError('journal_duplicate_or_conflicting_claim')
            claims[mid] = row
        else:
            if mid not in claims or mid in terminals:
                raise IntegrityError('journal_unclaimed_or_duplicate_final')
            counter = 0 if event == 'route_success' else counter + 1
            if row['consecutive_failures'] != counter:
                raise IntegrityError('journal_route_counter_disagrees')
            terminals[mid] = row
    known_paths = {measurement_path(output, job, family=family, model=model): job['measurement_id'] for job in jobs}
    for directory in attempt_directories(output, model=model):
        if directory not in known_paths:
            raise IntegrityError('unregistered_attempt_directory')
    for job in jobs:
        mid = job['measurement_id']
        destination = measurement_path(output, job, family=family, model=model)
        if not destination.exists():
            if mid in claims or mid in terminals:
                ambiguous.append(mid)
            else:
                pending.append(job)
            continue
        final_path = destination / 'FINAL.json'
        if mid not in claims or mid not in terminals or not final_path.is_file() or not (destination / 'INPUT.json').is_file():
            ambiguous.append(mid)
            continue
        terminal = terminals[mid]
        final = json.loads(pinned(terminal['final']))
        if terminal['final']['path'] != str(final_path):
            raise IntegrityError('journal_final_path_conflict')
        if final['measurement_id'] != mid or final['input_sha256'] != digest(job):
            raise IntegrityError('existing_final_identity_conflict')
        if encoded(load(destination / 'INPUT.json')) != encoded(job):
            raise IntegrityError('existing_input_conflict')
        expected_event = 'route_success' if final['technical_status'] == 'complete' else 'route_failure'
        if terminal['event'] != expected_event or terminal['technical_status'] != final['technical_status']:
            raise IntegrityError('journal_final_outcome_conflict')
        if final['integrity_failure']:
            raise AdmissionHold('prior_integrity_failure_requires_manual_reconciliation')
        for item in final['capture_files']:
            if not canonical(item['path']).is_relative_to(destination / 'capture'):
                raise IntegrityError('capture_pin_escape')
            pinned(item)
        if final['technical_status'] == 'complete':
            names = {Path(item['path']).name for item in final['capture_files']}
            if not {'request.json', 'attempt.json', 'receipt.json', 'response.raw', 'result.json'}.issubset(names):
                raise IntegrityError('incomplete_success_capture')
            if load(destination / 'capture/result.json') != final['result']:
                raise IntegrityError('existing_result_conflict')
            if pin(destination / 'capture/request.json')['sha256'] != job['request_file_sha256']:
                raise IntegrityError('existing_request_conflict')
        complete.append(mid)
    return pending, complete, ambiguous


def prepare_request(job, contexts, config, runner):
    context_row = contexts[(job['run_id'], job['stage'])]
    for key, item in context_row['original_bindings'].items():
        # The large complete job ledger is pinned once at invocation admission;
        # every specific saved allocation/config/context is rehashed per call.
        if key == 'original_jobs':
            continue
        if isinstance(item, dict) and set(item) == {'path', 'bytes', 'sha256'}:
            pinned(item)
    context = json.loads(pinned(job['context']))
    if runner.sha(context) != job['context_canonical_sha256']:
        raise IntegrityError('context_canonical_hash')
    questions = [q for q in config['questions'] if q['id'] == job['question_id']]
    if len(questions) != 1 or runner.sha(questions[0]) != job['question_canonical_sha256']:
        raise IntegrityError('question_identity')
    question = questions[0]
    if job['question_id'].startswith('evaluation_') or question['threshold'] not in ('strongly_favors', 'leans_towards_or_favors'):
        raise IntegrityError('already_covered_question')
    seed = runner.seed_for(job['original_seed'], ['probe', job['stage'], job['question_id']])
    if seed != job['seed']:
        raise IntegrityError('seed_identity')
    messages = copy.deepcopy(context) + [{'role': 'user', 'content': question['text']}]
    payload = runner.request(config, messages, seed, config['tools'])
    payload['tool_choice'] = 'none'
    if config.get('probe_response_format'):
        payload['response_format'] = copy.deepcopy(config['probe_response_format'])
    payload['max_tokens'] = config.get('probe_max_tokens', 8192)
    raw = runner.encoded(payload)
    if hashlib.sha256(raw).hexdigest() != job['request_body_sha256'] or len(raw) != job['request_body_bytes'] or hashlib.sha256(raw + b'\n').hexdigest() != job['request_file_sha256']:
        raise IntegrityError('request_reconstruction_identity')
    return context, question


def run_queue(output, jobs, worker, admission_check, source_check, pause_paths, *, family, model, initial_failures=0, phase='pilot', max_clients=2):
    """Caller holds the unique family lock and binds this output/journal to one family/model. A partial attempt is never replayed."""
    if type(max_clients) is not int or max_clients not in (1, 2):
        raise IntegrityError('client_cap')
    if type(initial_failures) is not int or initial_failures < 0:
        raise IntegrityError('initial_failure_count')
    output = canonical(output)
    namespace_component(family)
    namespace_component(model)
    for job in jobs:
        measurement_path(output, job, family=family, model=model)
    pause_paths = [canonical(path) for path in pause_paths]
    control = mkdir(output / '.control')
    log = control / 'journal.jsonl'
    history = read_history(log)
    if any(row['event'] in ('route_quarantined', 'unresolved_prior_attempt', 'unresolved_current_attempt') for row in history):
        raise AdmissionHold('manual_reconciliation_required')
    retained_failures = 0
    for row in history:
        if row['event'] == 'controller_started':
            retained_failures = row['inherited_failures']
        elif row['event'] in ('route_success', 'route_failure'):
            retained_failures = row['consecutive_failures']
    failures = max(initial_failures, retained_failures)
    if failures >= 3:
        raise AdmissionHold('inherited_donor_failure_threshold')
    pending, existing, ambiguous = reconcile(output, jobs, history, family=family, model=model)
    if ambiguous:
        journal(log, {'event': 'unresolved_prior_attempt', 'measurement_ids': ambiguous})
        raise AdmissionHold('partial_attempt_requires_manual_reconciliation')
    journal(log, {'event': 'controller_started', 'phase': phase, 'existing_finals': len(existing), 'pending': len(pending), 'inherited_failures': failures})
    stopped = threading.Event()
    lock = threading.Lock()
    stop_reason = None
    completed = []

    def task(job, destination):
        try:
            result = worker(job, destination / 'capture')
            status = result.get('technical_status', 'missing_status')
            failure = None
            integrity = False
        except BaseException as error:
            status = 'failure'
            failure = repr(error)
            integrity = isinstance(error, IntegrityError) or any(code in str(error) for code in ('not_preserved', 'rendered_prompt_missing', 'probe_mutated_original_context'))
            if integrity:
                with lock:
                    stopped.set()
            result = None
        try:
            files = [pin(p) for p in sorted((destination / 'capture').iterdir()) if p.is_file()] if (destination / 'capture').exists() else []
            final = {'measurement_id': job['measurement_id'], 'input_sha256': digest(job), 'finished_at': now(), 'technical_status': status, 'admissible': status == 'complete', 'failure': failure, 'integrity_failure': integrity, 'capture_files': files, 'result': result, 'retry': False}
            save(destination / 'FINAL.json', final)
            return final
        except BaseException:
            with lock:
                stopped.set()
            raise

    def harvest(active, done):
        nonlocal failures, stop_reason
        for future in sorted(done, key=lambda item: active[item]['measurement_id']):
            job = active.pop(future)
            try:
                final = future.result()
            except BaseException as error:
                stopped.set()
                stop_reason = 'recorder_or_unresolved_current_attempt'
                journal(log, {'event': 'unresolved_current_attempt', 'measurement_id': job['measurement_id'], 'error': repr(error)})
                continue
            completed.append(job['measurement_id'])
            failures = 0 if final['technical_status'] == 'complete' else failures + 1
            journal(log, {'event': 'route_success' if final['technical_status'] == 'complete' else 'route_failure', 'measurement_id': job['measurement_id'], 'consecutive_failures': failures, 'technical_status': final['technical_status'], 'final': pin(measurement_path(output, job, family=family, model=model) / 'FINAL.json')})
            if final['integrity_failure'] or failures >= 3:
                stopped.set()
                stop_reason = 'integrity_failure' if final['integrity_failure'] else 'three_consecutive_infrastructure_failures'
                journal(log, {'event': 'route_quarantined', 'reason': stop_reason, 'measurement_id': job['measurement_id']})

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_clients) as pool:
        active = {}
        while pending or active:
            harvest(active, [future for future in active if future.done()])
            while pending and len(active) < max_clients and not stopped.is_set():
                try:
                    if any(path.exists() for path in pause_paths):
                        raise AdmissionHold('operator_pause')
                    if shutil.disk_usage(output).free < 80 * 1024**3:
                        raise AdmissionHold('local_disk_below80GiB')
                    observation = admission_check()
                    job = pending[0]
                    source_check(job)
                    if any(future.done() for future in active):
                        break  # Account completed outcomes before any replacement claim.
                    with lock:
                        if any(future.done() for future in active):
                            break
                        if stopped.is_set() or any(path.exists() for path in pause_paths):
                            stopped.set()
                            stop_reason = 'concurrent_fault_or_operator_pause'
                            break
                        destination = measurement_path(output, job, family=family, model=model)
                        if destination.exists():
                            raise IntegrityError('claim_collision')
                        mkdir(destination)
                        save(destination / 'INPUT.json', job)
                        save(destination / 'ADMISSION_OBSERVATION.json', observation)
                        journal(log, {'event': 'claimed', 'measurement_id': job['measurement_id'], 'input_sha256': digest(job), 'phase': phase})
                        active[pool.submit(task, job, destination)] = job
                        pending.pop(0)
                except (AdmissionHold, IntegrityError) as error:
                    stopped.set()
                    stop_reason = repr(error)
                    journal(log, {'event': 'admission_held', 'reason': stop_reason})
            if not active:
                break
            done, _ = concurrent.futures.wait(active, timeout=.5, return_when=concurrent.futures.FIRST_COMPLETED)
            harvest(active, done)
    result = {'at': now(), 'phase': phase, 'existing_final': len(existing), 'new_final': len(completed), 'remaining_unclaimed': len(pending), 'stop_reason': stop_reason, 'consecutive_failures': failures, 'active': 0, 'completed_ids': completed}
    journal(log, {'event': 'controller_finished', **result})
    return result


def probe_measurement(job, destination, source_check, config, runner):
    context, question = source_check(job)
    result = runner.probe(config, context, question, job['seed'], destination)
    if pin(destination / 'request.json')['sha256'] != job['request_file_sha256']:
        raise IntegrityError('actual_request_changed')
    if result['instrument'] != job['question_id'] or result['question_sha256'] != job['question_canonical_sha256'] or result['context_sha256'] != job['context_canonical_sha256']:
        raise IntegrityError('actual_result_binding_changed')
    return result
