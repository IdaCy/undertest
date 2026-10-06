from __future__ import annotations

import argparse
import concurrent.futures
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import time
import uuid


_SOURCE = Path(__file__).read_bytes()
_SOURCE_SHA = hashlib.sha256(_SOURCE).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.writing.' + uuid.uuid4().hex)
    with temporary.open('xb') as stream:
        stream.write(encoded(value) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())
    # Exclusive publication leaves the staging file retained as crash evidence.
    os.link(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate_job(job):
    if not isinstance(job, dict) or not all(key in job for key in ('spec', 'variant', 'allocation')):
        raise ValueError('invalid_job_shape')
    run_id = job['allocation'].get('run_id')
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,179}', run_id):
        raise ValueError('unsafe_run_id')
    if not isinstance(job['allocation'].get('continue_to_end', False), bool):
        raise ValueError('invalid_full_allocation')
    return run_id


def extension_block(prototypes, config, block_index):
    base_seed = config.get('dispatcher', {}).get('seed', 61806)
    model = config.get('model', 'model')
    seed = lambda *parts: int(sha([base_seed, *parts])[:16], 16) % (2**31 - 1)
    block_seed = seed('block', block_index, model)
    jobs = []
    eligible = [job for job in prototypes if job.get('eligible', True)]
    ordered = sorted(eligible, key=sha)
    if len({sha(job) for job in ordered}) != len(ordered):
        raise ValueError('duplicate_extension_prototypes')
    for prototype in ordered:
        job = copy.deepcopy(prototype)
        prototype_sha = sha(prototype)
        task = job['spec']['task_id']
        variant = job['variant']['id']
        slug = lambda value, length: re.sub('[^A-Za-z0-9_.-]', '_', str(value))[:length]
        run_id = f'ext_b{block_index:08d}_{slug(model, 20)}_{slug(task, 36)}_{slug(variant, 24)}_{prototype_sha[:12]}'
        allocation = job['allocation']
        allocation.update(run_id=run_id, stage='corpus_extension', instruments=['evaluation_presence'],
                          seed=seed('episode', block_index, model, task, variant, prototype_sha),
                          block_index=block_index, block_seed=block_seed,
                          task_seed=seed('task', block_index, task), model_seed=seed('model', block_index, model),
                          prototype_sha256=prototype_sha, prototype_run_id=prototype['allocation'].get('run_id'))
        if 'pair_id' in allocation:
            allocation['pair_id'] = f'extension_b{block_index:08d}:' + str(allocation['pair_id'])
        validate_job(job)
        jobs.append(job)
    random.Random(block_seed).shuffle(jobs)
    return {'block_index': block_index, 'block_seed': block_seed, 'model': model,
            'prototype_set_sha256': sha(prototypes), 'balanced_complete_prototype_block': True, 'jobs': jobs}


class Dispatcher:
    def __init__(self, jobs, config, output, workers=8, max_full=2, extension_prototypes=None,
                 pause_file=None, runner=None, free_bytes=None, poll_seconds=2):
        if type(workers) is not int or workers < 1 or type(max_full) is not int or not 0 <= max_full <= workers:
            raise ValueError('invalid_worker_limits')
        self.jobs = copy.deepcopy(jobs)
        self.config = copy.deepcopy(config)
        self.prototypes = copy.deepcopy(extension_prototypes or [])
        ids = [validate_job(job) for job in self.jobs]
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate_fixed_run_id')
        for job in self.prototypes:
            if job.get('eligible', True):
                validate_job(job)
        if max_full == 0 and any(job['allocation'].get('continue_to_end', False) for job in self.jobs + self.prototypes if job.get('eligible', True)):
            raise ValueError('full_jobs_require_positive_full_limit')
        self.output = Path(output).absolute()
        self.output.mkdir(parents=True, exist_ok=True)
        self.meta = self.output / '.dispatcher'
        self.pause_file = Path(pause_file) if pause_file else self.output / 'PAUSE'
        self.workers, self.max_full = workers, max_full
        self.policy = self.config.get('dispatcher', {})
        self.disk_floor = self.policy.get('disk_floor_bytes', 80 * 1024**3)
        self.failure_threshold = self.policy.get('route_failure_threshold', 3)
        if self.disk_floor < 0 or self.failure_threshold < 1:
            raise ValueError('invalid_dispatch_policy')
        self.poll_seconds = min(30, max(0.001, poll_seconds))
        self.runner = runner
        self.free_bytes = free_bytes or (lambda: shutil.disk_usage(self.output).free)
        self.claimed, self.done, self.failures, self.quarantines = {}, set(), {}, {}
        self.disk_wait = False
        self.controller_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:12]

    def event(self, kind, **fields):
        value = {'at': now(), 'controller_id': self.controller_id, 'event': kind, **fields}
        new_journal = not (self.meta / 'journal.jsonl').exists()
        with (self.meta / 'journal.jsonl').open('ab') as stream:
            stream.write(encoded(value) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        if new_journal:
            fsync_directory(self.meta)
        return value

    def receipt(self, run_id, status, **fields):
        value = self.event(status, run_id=run_id, **fields)
        save_new(self.meta / 'receipts' / (run_id[:120] + '_' + uuid.uuid4().hex + '.json'), value)
        self.done.add(run_id)

    def load_history(self):
        path = self.meta / 'journal.jsonl'
        if not path.exists():
            return
        for line in path.read_bytes().splitlines():
            # A corrupt/torn journal cannot establish safe admission identities.
            row = json.loads(line)
            if row['event'] == 'claimed':
                prior = self.claimed.get(row['run_id'])
                if prior and prior != row['job_sha256']:
                    raise RuntimeError('conflicting_durable_claim_identity')
                self.claimed[row['run_id']] = row['job_sha256']
            elif row['event'] == 'route_quarantined':
                self.quarantines[row['route']] = row
            elif row['event'] == 'route_requalified':
                self.quarantines.pop(row['route'], None)
                self.failures[row['route']] = 0
            elif row['event'] == 'route_failure':
                self.failures[row['route']] = row['consecutive_failures']
            elif row['event'] == 'route_success':
                for route in row['routes']:
                    self.failures[route] = 0

    def reconcile(self, job):
        run_id = validate_job(job)
        if run_id in self.done:
            return True
        path = self.output / run_id
        prior = self.claimed.get(run_id)
        if prior is not None and prior != sha(job):
            self.receipt(run_id, 'unresolved_identity_conflict', job_sha256=sha(job), claimed_job_sha256=prior)
            return True
        summary = path / 'summary.json'
        if summary.is_file():
            try:
                value = json.loads(summary.read_bytes())
                if value.get('run_id') != run_id:
                    raise ValueError('summary_run_identity_mismatch')
                for filename, expected in (('allocation.json', job['allocation']), ('spec.json', job['spec']),
                                           ('variant.json', job['variant']), ('model.json', self.config)):
                    if not (path / filename).is_file() or json.loads((path / filename).read_bytes()) != expected:
                        raise ValueError('completed_identity_mismatch')
                self.receipt(run_id, 'existing_completed_summary', job_sha256=sha(job),
                             summary_sha256=hashlib.sha256(summary.read_bytes()).hexdigest())
            except (ValueError, OSError):
                self.receipt(run_id, 'unresolved_completed_evidence', job_sha256=sha(job))
            return True
        if path.exists() or prior is not None:
            self.receipt(run_id, 'unresolved_prior_attempt', job_sha256=sha(job),
                         destination_exists=path.exists(), prior_claim_exists=prior is not None,
                         replayed=False, counted_completed=False)
            return True
        return False

    def routes(self, job):
        return ('model:' + sha([self.config.get('model'), self.config.get('endpoint')]),
                'task:' + sha([job['spec'].get('task_id'), job['spec'].get('image')]),
                'recorder:' + sha(str(self.output)))

    def route_ready(self, job):
        for route in self.routes(job):
            quarantine = self.quarantines.get(route)
            if not quarantine:
                continue
            resolution = self.meta / 'route_resolutions' / (quarantine['quarantine_id'] + '.json')
            if resolution.is_file():
                try:
                    value = json.loads(resolution.read_bytes())
                    if (value.get('status') == 'qualified' and value.get('route') == route
                            and value.get('quarantine_id') == quarantine['quarantine_id']
                            and value.get('qualification_evidence')):
                        self.event('route_requalified', route=route, quarantine_id=quarantine['quarantine_id'],
                                   resolution_sha256=hashlib.sha256(resolution.read_bytes()).hexdigest())
                        self.quarantines.pop(route)
                        self.failures[route] = 0
                        continue
                except (ValueError, OSError):
                    pass
            return False
        return True

    def complete(self, future, job):
        run_id = job['allocation']['run_id']
        try:
            result = future.result()
        except BaseException as error:
            result = {'end_status': 'technical_failure', 'controller_error_type': type(error).__name__}
        if not isinstance(result, dict):
            result = {'end_status': 'technical_failure', 'controller_error_type': 'invalid_runner_return'}
        summary = self.output / run_id / 'summary.json'
        model_route, task_route, recorder_route = self.routes(job)
        route_faults = {}
        protocol_markers = ('not_preserved', 'rendered_prompt_missing', 'mutated_original_context', 'reasoning_missing')
        if result.get('end_status') == 'technical_failure' or not summary.is_file():
            error = str(result.get('actor_error', ''))
            is_protocol = any(marker in error for marker in protocol_markers)
            route = model_route if is_protocol or any(x in error for x in ('model_http_status_', 'Connection', 'connection', 'timed out', 'Timeout')) else task_route
            route_faults[route] = {'immediate': is_protocol, 'fault_kind': 'actor_or_missing_summary'}
        for probe in result.get('probes', []):
            # A valid semantic no/unknown answer never counts as a fault.
            # Repeated unusable instrument outputs require separate qualification.
            if probe.get('technical_status') in ('failure', 'invalid_answer_schema', 'truncated'):
                is_protocol = any(marker in str(probe.get('error', '')) for marker in protocol_markers)
                prior = route_faults.get(model_route, {})
                route_faults[model_route] = {'immediate': is_protocol or prior.get('immediate', False), 'fault_kind': 'probe_infrastructure_or_unusable_instrument'}
        closure = result.get('sandbox_closure', {})
        if (result.get('sandbox_close_error') or closure.get('state') == 'closure_requires_review'
                or closure.get('overlay_mounted') or closure.get('retained_container')
                or any(result.get(stage + '_workspace_status') == 'failed' for stage in ('early', 'end'))):
            route_faults[recorder_route] = {'immediate': True, 'fault_kind': 'snapshot_or_closure_integrity'}
        failed = bool(route_faults)
        if not summary.is_file():
            status = 'unresolved_current_attempt'
        else:
            status = 'episode_finished'
        self.receipt(run_id, status, job_sha256=sha(job), technical_failure=failed,
                     summary_sha256=hashlib.sha256(summary.read_bytes()).hexdigest() if summary.is_file() else None,
                     early_status=result.get('early_status'), end_status=result.get('end_status'),
                     controller_error_type=result.get('controller_error_type'), route_faults=route_faults, replayed=False)
        for route, fault in route_faults.items():
            self.failures[route] = self.failures.get(route, 0) + 1
            self.event('route_failure', route=route, run_id=run_id, consecutive_failures=self.failures[route], **fault)
            if fault['immediate'] or self.failures[route] >= self.failure_threshold:
                quarantine_id = uuid.uuid4().hex
                self.quarantines[route] = self.event('route_quarantined', route=route, quarantine_id=quarantine_id,
                    failures=self.failures[route], resolution_path=str(self.meta / 'route_resolutions' / (quarantine_id + '.json')),
                    resolution_contract={'status': 'qualified', 'route': route, 'quarantine_id': quarantine_id,
                                         'qualification_evidence': 'required retained repair and smoke evidence reference'},
                    policy='no_time_based_readmission_no_fresh_scientific_jobs_as_health_probes')
                self.failures[route] = 0
        if not failed:
            routes = self.routes(job)
            self.event('route_success', routes=routes, run_id=run_id)
            for route in routes:
                self.failures[route] = 0

    def run(self):
        lock = (self.output / '.dispatcher.lock').open('a+')
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError('another_dispatcher_owns_output')
        try:
            self.meta.mkdir(exist_ok=True)
            fsync_directory(self.output)
            code = {'dispatcher_sha256': _SOURCE_SHA}
            for name in ('campaign_runner.py', 'campaign_sandbox.py'):
                path = Path(__file__).with_name(name)
                if path.exists():
                    code[name + '_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            identity = {'jobs_sha256': sha(self.jobs), 'config_sha256': sha(self.config),
                        'prototypes_sha256': sha(self.prototypes), 'execution_code': code}
            identity_path = self.meta / 'queue_identity.json'
            if identity_path.exists():
                if json.loads(identity_path.read_bytes()) != identity:
                    raise RuntimeError('queue_identity_changed_use_new_output')
            else:
                save_new(identity_path, identity)
            self.load_history()
            controller = self.meta / 'controllers' / self.controller_id
            controller.mkdir(parents=True)
            with (controller / 'dispatcher_source.py').open('xb') as stream:
                stream.write(_SOURCE)
            if self.runner is None:
                from causes.campaign_runner import run_episode
                self.runner = run_episode
            save_new(controller / 'identity.json', {**identity, **code, 'workers': self.workers, 'max_full': self.max_full,
                     'pause_file': str(self.pause_file), 'disk_floor_bytes': self.disk_floor,
                     'route_failure_threshold': self.failure_threshold, 'route_recovery': 'explicit_qualification_receipt_required'})
            self.event('controller_started', **identity, **code)
            pending = [job for job in self.jobs if not self.reconcile(job)]
            blocks = sorted((self.meta / 'blocks').glob('[0-9]*.json')) if (self.meta / 'blocks').exists() else []
            next_block = 0
            if blocks:
                for block_path in blocks:
                    last = json.loads(block_path.read_bytes())
                    if last['block_index'] != next_block or last != extension_block(self.prototypes, self.config, next_block):
                        raise RuntimeError('extension_block_identity_mismatch')
                    pending.extend(job for job in last['jobs'] if not self.reconcile(job))
                    next_block += 1
            active = {}
            pause_seen = False
            idle_seen = False
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
                while True:
                    for future in list(active):
                        if future.done():
                            self.complete(future, active.pop(future))
                    paused = self.pause_file.exists()
                    if paused and not pause_seen:
                        self.event('pause_seen', active=len(active), pending=len(pending), admission_stopped=True)
                        pause_seen = True
                    if paused and not active:
                        self.event('controller_paused', pending=len(pending), next_extension_block=next_block)
                        return {'status': 'paused', 'pending': len(pending), 'next_extension_block': next_block}
                    full_active = sum(job['allocation'].get('continue_to_end', False) for job in active.values())
                    def admissible(job):
                        return (not job['allocation'].get('continue_to_end', False) or full_active < self.max_full) and self.route_ready(job)
                    blocked_backlog = (pending and not any(admissible(job) for job in pending)
                        and any(job.get('eligible', True) and admissible(job) for job in self.prototypes))
                    if not paused and len(active) < self.workers and (not pending or blocked_backlog):
                        block = extension_block(self.prototypes, self.config, next_block)
                        if block['jobs']:
                            save_new(self.meta / 'blocks' / f'{next_block:012d}.json', block)
                            self.event('extension_block_created', block_index=next_block, block_sha256=sha(block), jobs=len(block['jobs']))
                            next_block += 1
                            pending.extend(job for job in block['jobs'] if not self.reconcile(job))
                            idle_seen = False
                        elif not idle_seen:
                            self.event('waiting_for_operator_pause_no_eligible_extensions')
                            idle_seen = True
                    while not paused and len(active) < self.workers and pending:
                        full_active = sum(job['allocation'].get('continue_to_end', False) for job in active.values())
                        candidate = next((i for i, job in enumerate(pending)
                                          if self.route_ready(job) and (not job['allocation'].get('continue_to_end', False) or full_active < self.max_full)), None)
                        if candidate is None:
                            break
                        free = self.free_bytes()
                        if free < self.disk_floor:
                            if not self.disk_wait:
                                self.event('disk_floor_wait', free_bytes=free, required_bytes=self.disk_floor,
                                           candidate_run_id=pending[candidate]['allocation']['run_id'])
                                self.disk_wait = True
                            break
                        if self.disk_wait:
                            self.event('disk_floor_recovered', free_bytes=free)
                            self.disk_wait = False
                        if self.pause_file.exists():
                            paused = True
                            break
                        job = pending.pop(candidate)
                        if self.reconcile(job):
                            continue
                        run_id = job['allocation']['run_id']
                        self.event('claimed', run_id=run_id, job_sha256=sha(job), config_sha256=sha(self.config),
                                   full=job['allocation'].get('continue_to_end', False), stage=job['allocation'].get('stage'),
                                   free_bytes_before_admission=free)
                        self.claimed[run_id] = sha(job)
                        future = pool.submit(self.runner, job['spec'], job['variant'], self.config, job['allocation'], self.output / run_id)
                        active[future] = job
                    if not self.pause_file.exists() and not pending and len(active) < self.workers and any(job.get('eligible', True) for job in self.prototypes):
                        continue
                    if active:
                        concurrent.futures.wait(active, timeout=self.poll_seconds, return_when=concurrent.futures.FIRST_COMPLETED)
                    else:
                        time.sleep(self.poll_seconds)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--jobs', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--max-full', type=int, default=2)
    parser.add_argument('--extension-prototypes', required=True)
    parser.add_argument('--pause-file', required=True)
    args = parser.parse_args()
    jobs = json.loads(Path(args.jobs).read_bytes())
    config = json.loads(Path(args.config).read_bytes())
    prototypes = json.loads(Path(args.extension_prototypes).read_bytes())
    result = Dispatcher(jobs, config, args.output, args.workers, args.max_full, prototypes, args.pause_file).run()
    print(json.dumps(result))


if __name__ == '__main__':
    main()
