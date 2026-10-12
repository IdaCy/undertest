from __future__ import annotations

import argparse
from collections.abc import Mapping
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
import traceback
import uuid


_SOURCE = Path(__file__).read_bytes()
_SOURCE_SHA = hashlib.sha256(_SOURCE).hexdigest()
_SUCCESSION_PREDECESSOR = '5dbbe46249b6ab3e86ce09a55b66d86038ade23fcd4264bd716b890dea29bd3d'


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


def pinned_json(path, expected_sha256):
    path = Path(path)
    if (not path.is_absolute() or path.resolve() != path
            or any(part.is_symlink() for part in (path, *path.parents))):
        raise ValueError('noncanonical_admission_control_path')
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('admission_control_pin_mismatch')
    return json.loads(raw), {'path': str(path), 'bytes': len(raw), 'sha256': expected_sha256}


class AdmissionHolds:
    def __init__(self, path, expected_sha256):
        value, self.pin = pinned_json(path, expected_sha256)
        if value.get('schema') != 'caus.admission_holds.v1' or not isinstance(value.get('holds'), list):
            raise ValueError('invalid_admission_holds')
        self.holds = value['holds']
        ids = set()
        for row in self.holds:
            if (not isinstance(row.get('hold_id'), str) or not row['hold_id'] or row['hold_id'] in ids
                    or not isinstance(row.get('task_id'), str) or not row['task_id']
                    or any(not re.fullmatch('[a-f0-9]{64}', str(row.get(key, '')))
                           for key in ('stimulus_sha256', 'definition_sha256'))):
                raise ValueError('invalid_admission_hold_binding')
            ids.add(row['hold_id'])
        self.seen = set()

    def verify(self):
        _, current = pinned_json(self.pin['path'], self.pin['sha256'])
        if current != self.pin:
            raise ValueError('admission_holds_changed')

    def matches(self, job):
        task, variant = job['spec'].get('task_id'), job['variant']
        stimulus, definition = variant.get('stimulus_sha256'), variant.get('definition_sha256')
        result = []
        for row in self.holds:
            if row['task_id'] != task:
                continue
            if definition == row['definition_sha256'] and stimulus != row['stimulus_sha256']:
                raise ValueError('held_definition_stimulus_drift')
            # A canonical alias of the same physical stimulus is also held.
            if stimulus == row['stimulus_sha256']:
                result.append(row['hold_id'])
        return result


def validate_job(job):
    if not isinstance(job, dict) or not all(key in job for key in ('spec', 'variant', 'allocation')):
        raise ValueError('invalid_job_shape')
    run_id = job['allocation'].get('run_id')
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,179}', run_id):
        raise ValueError('unsafe_run_id')
    if not isinstance(job['allocation'].get('continue_to_end', False), bool):
        raise ValueError('invalid_full_allocation')
    return run_id


def extension_block(prototypes, config, block_index, *, namespace=None):
    base_seed = config.get('dispatcher', {}).get('seed', 61806)
    model = config.get('model', 'model')
    seed_scope = [] if namespace is None else ['prefix_extension', namespace]
    seed = lambda *parts: int(sha([base_seed, *seed_scope, *parts])[:16], 16) % (2**31 - 1)
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
        run_id = f'{namespace or "ext"}_b{block_index:08d}_{slug(model, 20)}_{slug(task, 36)}_{slug(variant, 24)}_{prototype_sha[:12]}'
        allocation = job['allocation']
        allocation.update(run_id=run_id, stage='corpus_extension', instruments=['evaluation_presence'],
                          seed=seed('episode', block_index, model, task, variant, prototype_sha),
                          block_index=block_index, block_seed=block_seed,
                          task_seed=seed('task', block_index, task), model_seed=seed('model', block_index, model),
                          prototype_sha256=prototype_sha, prototype_run_id=prototype['allocation'].get('run_id'))
        if namespace is not None:
            allocation.update(extension_namespace=namespace, fixed_study_inference=False)
        if 'pair_id' in allocation:
            allocation['pair_id'] = f'{namespace or "extension"}_b{block_index:08d}:' + str(allocation['pair_id'])
        validate_job(job)
        jobs.append(job)
    random.Random(block_seed).shuffle(jobs)
    return {'block_index': block_index, 'block_seed': block_seed, 'model': model,
            'prototype_set_sha256': sha(prototypes), 'balanced_complete_prototype_block': True, 'jobs': jobs}


class Dispatcher:
    def __init__(self, jobs, config, output, workers=8, max_full=2, extension_prototypes=None,
                 pause_file=None, runner=None, free_bytes=None, poll_seconds=2,
                 admission_holds=None, admission_holds_sha256=None,
                 code_succession=None, code_succession_sha256=None, global_pause_file=None, start_barrier_file=None,
                 corpus_continuation=None, corpus_continuation_sha256=None,
                 system_prompts_by_run_id=None, prefix_extension=None):
        if type(workers) is not int or workers < 1 or type(max_full) is not int or not 0 <= max_full <= workers:
            raise ValueError('invalid_worker_limits')
        self.jobs = copy.deepcopy(jobs)
        self.config = copy.deepcopy(config)
        self.prototypes = copy.deepcopy(extension_prototypes or [])
        ids = [validate_job(job) for job in self.jobs]
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate_fixed_run_id')
        self.system_prompts = None
        self.prefix_extension = None
        if prefix_extension is not None:
            if (not isinstance(prefix_extension, Mapping)
                    or set(prefix_extension) != {'namespace', 'system_prompt'}
                    or not isinstance(prefix_extension['namespace'], str)
                    or re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,47}', prefix_extension['namespace']) is None
                    or not isinstance(prefix_extension['system_prompt'], str)
                    or system_prompts_by_run_id is None or not self.prototypes
                    or any(job['allocation'].get('continue_to_end') is not False
                           or 'system_prompt_intervention' in job['allocation'] for job in self.prototypes)
                    or any(run_id.startswith(prefix_extension['namespace'] + '_b') for run_id in ids)):
                raise ValueError('invalid_prefix_extension_scope')
            self.prefix_extension = copy.deepcopy(dict(prefix_extension))
        if system_prompts_by_run_id is not None:
            if (not isinstance(system_prompts_by_run_id, Mapping)
                    or set(system_prompts_by_run_id) != set(ids)
                    or not ids or (self.prototypes and self.prefix_extension is None) or corpus_continuation is not None
                    or any(not isinstance(value, str) for value in system_prompts_by_run_id.values())):
                raise ValueError('system_prompts_require_exact_fixed_queue')
            self.system_prompts = copy.deepcopy(dict(system_prompts_by_run_id))
        for job in self.prototypes:
            if job.get('eligible', True):
                validate_job(job)
        if max_full == 0 and any(job['allocation'].get('continue_to_end', False) for job in self.jobs + self.prototypes if job.get('eligible', True)):
            raise ValueError('full_jobs_require_positive_full_limit')
        self.output = Path(output).absolute()
        self.output.mkdir(parents=True, exist_ok=True)
        self.meta = self.output / '.dispatcher'
        self.pause_file = Path(pause_file) if pause_file else self.output / 'PAUSE'
        self.global_pause_file = Path(global_pause_file) if global_pause_file is not None else None
        self.start_barrier_file = Path(start_barrier_file) if start_barrier_file is not None else None
        if self.start_barrier_file is not None and not self.start_barrier_file.is_file():
            raise ValueError('startup_barrier_required_on_owner_start')
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
        if bool(admission_holds) != bool(admission_holds_sha256) or bool(code_succession) != bool(code_succession_sha256):
            raise ValueError('admission_control_pin_required')
        self.admission_holds = AdmissionHolds(admission_holds, admission_holds_sha256) if admission_holds else None
        self.code_succession = pinned_json(code_succession, code_succession_sha256) if code_succession else None
        self.continuation = None
        self.continuation_control = None
        if bool(corpus_continuation) != bool(corpus_continuation_sha256):
            raise ValueError('continuation_control_pin_required')
        if corpus_continuation is not None:
            if self.jobs:
                raise ValueError('continuation_requires_empty_initial_queue')
            from causes.corpus_continuation import CorpusContinuation
            control, self.continuation_control = pinned_json(corpus_continuation, corpus_continuation_sha256)
            self.continuation = CorpusContinuation.from_control(control, self.prototypes, self.config)
            if self.continuation.full_selection is not None and self.max_full == 0:
                raise ValueError('full_continuation_requires_positive_full_limit')

    def config_for(self, job):
        if self.system_prompts is None:
            return self.config
        if (self.prefix_extension is not None
                and job['allocation'].get('extension_namespace') == self.prefix_extension['namespace']
                and job['allocation'].get('stage') == 'corpus_extension'
                and job['allocation']['run_id'].startswith(self.prefix_extension['namespace'] + '_b')):
            return {**self.config, 'system': self.prefix_extension['system_prompt']}
        return {**self.config, 'system': self.system_prompts[job['allocation']['run_id']]}

    def make_extension_block(self, index):
        if self.continuation is not None:
            return self.continuation.block(index)
        return extension_block(self.prototypes, self.config, index,
                               namespace=self.prefix_extension['namespace'] if self.prefix_extension else None)

    def admission_paused(self):
        return self.pause_file.exists() or (self.global_pause_file is not None and self.global_pause_file.exists())

    def admission_barred(self):
        return self.start_barrier_file is not None and self.start_barrier_file.exists()

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
                                           ('variant.json', job['variant']), ('model.json', self.config_for(job))):
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
        if self.admission_held(job):
            return False
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

    def admission_held(self, job):
        if self.admission_holds is None:
            return False
        holds = self.admission_holds.matches(job)
        run_id = job['allocation']['run_id']
        if holds and run_id not in self.admission_holds.seen:
            self.event('admission_held', run_id=run_id, job_sha256=sha(job), hold_ids=holds,
                       admission_holds=self.admission_holds.pin, consumed_assignment=False)
            self.admission_holds.seen.add(run_id)
        return bool(holds)

    def authorize_succession(self, identity_path, predecessor, identity):
        if self.code_succession is None or self.admission_holds is None:
            raise RuntimeError('queue_identity_changed_use_new_output')
        receipt, receipt_pin = self.code_succession
        raw = identity_path.read_bytes()
        predecessor_pin = {'path': str(identity_path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        old_code, new_code = predecessor.get('execution_code', {}), identity['execution_code']
        if (receipt.get('schema') != 'caus.dispatcher_code_succession.v1'
                or receipt.get('output_root') != str(self.output)
                or receipt.get('predecessor_queue_identity') != predecessor_pin
                or receipt.get('successor_execution_code') != new_code
                or receipt.get('admission_holds') != self.admission_holds.pin
                or not receipt.get('independent_review')
                or old_code.get('dispatcher_sha256') != _SUCCESSION_PREDECESSOR
                or {k: v for k, v in old_code.items() if k != 'dispatcher_sha256'} != {k: v for k, v in new_code.items() if k != 'dispatcher_sha256'}
                or {k: v for k, v in predecessor.items() if k != 'execution_code'} != {k: v for k, v in identity.items() if k != 'execution_code'}):
            raise RuntimeError('unapproved_queue_code_succession')
        review = receipt['independent_review']
        _, observed_review = pinned_json(review['path'], review['sha256'])
        if observed_review != review:
            raise RuntimeError('code_succession_review_pin_mismatch')
        self.admission_holds.verify()
        current, current_pin = pinned_json(receipt_pin['path'], receipt_pin['sha256'])
        if current != receipt or current_pin != receipt_pin:
            raise RuntimeError('code_succession_changed')
        saved = self.meta / 'code_successions' / (receipt_pin['sha256'] + '.json')
        if saved.exists():
            if json.loads(saved.read_bytes()) != receipt:
                raise RuntimeError('saved_code_succession_changed')
        else:
            save_new(saved, receipt)
        return receipt_pin

    def bind_admission_control(self):
        path = self.meta / 'admission_control.json'
        expected = {'schema': 'caus.dispatcher_admission_control.v1',
                    'admission_holds': self.admission_holds.pin if self.admission_holds else None}
        if path.exists():
            if json.loads(path.read_bytes()) != expected:
                raise RuntimeError('persisted_admission_control_changed')
        elif self.admission_holds is not None:
            # Publish before the first queue identity: a crash cannot leave a
            # fresh held queue restartable without its admission control.
            save_new(path, expected)

    def complete(self, future, job):
        run_id = job['allocation']['run_id']
        failure_details = {}
        try:
            result = future.result()
        except BaseException as error:
            result = {'end_status': 'technical_failure', 'controller_error_type': type(error).__name__}
            failure_details['controller_traceback'] = traceback.format_exc()
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
                or closure.get('overlay_mounted') or closure.get('closure_errors')
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
                     controller_error_type=result.get('controller_error_type'), route_faults=route_faults,
                     replayed=False, **failure_details)
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
            self.bind_admission_control()
            code = {'dispatcher_sha256': _SOURCE_SHA}
            for name in ('campaign_runner.py', 'campaign_sandbox.py'):
                path = Path(__file__).with_name(name)
                if path.exists():
                    code[name + '_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            identity = {'jobs_sha256': sha(self.jobs), 'config_sha256': sha(self.config),
                        'prototypes_sha256': sha(self.prototypes), 'execution_code': code}
            if self.system_prompts is not None:
                identity['system_prompts_sha256'] = sha(self.system_prompts)
            if self.prefix_extension is not None:
                identity['prefix_extension_sha256'] = sha(self.prefix_extension)
            if self.continuation is not None:
                for name in ('corpus_allocation.py', 'corpus_continuation.py'):
                    code[name + '_sha256'] = hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                identity['corpus_continuation'] = {'control': self.continuation_control,
                                                   'binding': self.continuation.binding}
            identity_path = self.meta / 'queue_identity.json'
            succession = None
            if identity_path.exists():
                predecessor = json.loads(identity_path.read_bytes())
                if predecessor != identity:
                    succession = self.authorize_succession(identity_path, predecessor, identity)
                elif self.code_succession is not None:
                    raise RuntimeError('code_succession_not_applicable')
            else:
                if self.code_succession is not None:
                    raise RuntimeError('code_succession_requires_existing_queue')
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
                     'pause_file': str(self.pause_file), 'global_pause_file': str(self.global_pause_file) if self.global_pause_file is not None else None,
                     'start_barrier_file': str(self.start_barrier_file) if self.start_barrier_file is not None else None,
                     'disk_floor_bytes': self.disk_floor,
                     'route_failure_threshold': self.failure_threshold, 'route_recovery': 'explicit_qualification_receipt_required'})
            self.event('controller_started', **identity, **code)
            if self.admission_holds is not None:
                self.event('admission_holds_bound', admission_holds=self.admission_holds.pin,
                           code_succession=succession, predecessor_queue_identity_retained=True)
            pending = [job for job in self.jobs if not self.reconcile(job)]
            blocks = sorted((self.meta / 'blocks').glob('[0-9]*.json')) if (self.meta / 'blocks').exists() else []
            next_block = 0
            if blocks:
                for block_path in blocks:
                    last = json.loads(block_path.read_bytes())
                    if last['block_index'] != next_block or last != self.make_extension_block(next_block):
                        raise RuntimeError('extension_block_identity_mismatch')
                    if self.continuation is not None:
                        self.continuation.accept(last)
                    pending.extend(job for job in last['jobs'] if not self.reconcile(job))
                    next_block += 1
            if self.continuation is not None:
                journal = (self.meta / 'journal.jsonl').read_bytes()
                for index in self.continuation.check_history(journal):
                    self.event('extension_block_created', block_index=index,
                               block_sha256=self.continuation.block_hashes[index],
                               jobs=len(self.prototypes), recovered_persisted_unclaimed_block=True)
            active = {}
            pause_seen = False
            barrier_seen = False
            idle_seen = False
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
                while True:
                    if self.admission_holds is not None:
                        self.admission_holds.verify()
                    for future in list(active):
                        if future.done():
                            self.complete(future, active.pop(future))
                    paused = self.admission_paused()
                    barrier = self.admission_barred()
                    if barrier and not barrier_seen:
                        self.event('admission_barrier_waiting', barrier_file=str(self.start_barrier_file), active=len(active), pending=len(pending))
                        barrier_seen = True
                    elif not barrier and barrier_seen:
                        self.event('admission_barrier_released', barrier_file=str(self.start_barrier_file))
                        barrier_seen = False
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
                    if not paused and not barrier and len(active) < self.workers and (not pending or blocked_backlog):
                        block = self.make_extension_block(next_block)
                        if block['jobs']:
                            save_new(self.meta / 'blocks' / f'{next_block:012d}.json', block)
                            self.event('extension_block_created', block_index=next_block, block_sha256=sha(block), jobs=len(block['jobs']))
                            if self.continuation is not None:
                                self.continuation.accept(block)
                            next_block += 1
                            pending.extend(job for job in block['jobs'] if not self.reconcile(job))
                            idle_seen = False
                        elif not idle_seen:
                            self.event('waiting_for_operator_pause_no_eligible_extensions')
                            idle_seen = True
                    while not paused and not barrier and len(active) < self.workers and pending:
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
                        if self.admission_paused():
                            paused = True
                            break
                        if self.admission_barred():
                            barrier = True
                            break
                        job = pending.pop(candidate)
                        if self.reconcile(job):
                            continue
                        if self.admission_holds is not None:
                            self.admission_holds.verify()
                            if self.admission_held(job):
                                pending.insert(candidate, job)
                                continue
                        if self.admission_barred():
                            pending.insert(candidate, job)
                            barrier = True
                            break
                        run_id = job['allocation']['run_id']
                        job_config = self.config_for(job)
                        self.event('claimed', run_id=run_id, job_sha256=sha(job), config_sha256=sha(job_config),
                                   full=job['allocation'].get('continue_to_end', False), stage=job['allocation'].get('stage'),
                                   free_bytes_before_admission=free)
                        self.claimed[run_id] = sha(job)
                        future = pool.submit(self.runner, job['spec'], job['variant'], job_config, job['allocation'], self.output / run_id)
                        active[future] = job
                    if not self.admission_paused() and not self.admission_barred() and not pending and len(active) < self.workers and any(job.get('eligible', True) for job in self.prototypes):
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
    parser.add_argument('--global-pause-file')
    parser.add_argument('--start-barrier-file')
    parser.add_argument('--corpus-continuation')
    parser.add_argument('--corpus-continuation-sha256')
    parser.add_argument('--admission-holds')
    parser.add_argument('--admission-holds-sha256')
    parser.add_argument('--code-succession')
    parser.add_argument('--code-succession-sha256')
    parser.add_argument('--system-prompts')
    parser.add_argument('--system-prompts-sha256')
    parser.add_argument('--prefix-extension')
    parser.add_argument('--prefix-extension-sha256')
    args = parser.parse_args()
    jobs = json.loads(Path(args.jobs).read_bytes())
    config = json.loads(Path(args.config).read_bytes())
    prototypes = json.loads(Path(args.extension_prototypes).read_bytes())
    if bool(args.system_prompts) != bool(args.system_prompts_sha256):
        parser.error('--system-prompts and --system-prompts-sha256 must be supplied together')
    system_prompts = (pinned_json(args.system_prompts, args.system_prompts_sha256)[0]
                      if args.system_prompts is not None else None)
    if bool(args.prefix_extension) != bool(args.prefix_extension_sha256):
        parser.error('--prefix-extension and --prefix-extension-sha256 must be supplied together')
    prefix_extension = (pinned_json(args.prefix_extension, args.prefix_extension_sha256)[0]
                        if args.prefix_extension is not None else None)
    result = Dispatcher(jobs, config, args.output, args.workers, args.max_full, prototypes, args.pause_file,
                        admission_holds=args.admission_holds, admission_holds_sha256=args.admission_holds_sha256,
                        code_succession=args.code_succession, code_succession_sha256=args.code_succession_sha256,
                        global_pause_file=args.global_pause_file, start_barrier_file=args.start_barrier_file,
                        corpus_continuation=args.corpus_continuation,
                        corpus_continuation_sha256=args.corpus_continuation_sha256,
                        system_prompts_by_run_id=system_prompts,
                        prefix_extension=prefix_extension).run()
    print(json.dumps(result))


if __name__ == '__main__':
    main()
