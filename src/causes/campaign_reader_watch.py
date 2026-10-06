from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from causes import campaign_reader as reader

WATCH_SOURCE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fsync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    reader.save(path, value)
    for parent in (path.parent, path.parent.parent):
        fsync_dir(parent)


def jsonlines(path, strict=False):
    if not path.exists():
        return []
    data = path.read_bytes()
    if strict and data and not data.endswith(b'\n'):
        raise ValueError('torn_reader_journal_requires_review')
    # The synchronizer may be appending the final row concurrently. Only a
    # complete newline-terminated acceptance is eligible for consumption.
    return [json.loads(line) for line in data.split(b'\n')[:-1] if line]


def verified_json(path, expected_sha):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError('source_file_hash_mismatch:' + Path(path).name)
    return json.loads(raw)


def confined(path, base):
    path, base = Path(path).absolute(), Path(base).resolve()
    if ('..' in path.parts or path.resolve() != path or not path.is_relative_to(base)
            or any(p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(base))):
        raise ValueError('snapshot_path_not_canonical_regular_storage')
    return path


def model_roster(models):
    if (not isinstance(models, (list, tuple)) or not models
            or any(type(model) is not str or not model or model in ('.', '..')
                   or Path(model).name != model for model in models)
            or len(set(models)) != len(models)):
        raise ValueError('explicit_distinct_model_roster_required')
    return tuple(models)


def load_configuration(path):
    config = json.loads(Path(path).read_bytes())
    if config.get('schema') != 'causes.reader_watch_configuration.v1':
        raise ValueError('reader_watch_configuration_schema')
    config['models'] = model_roster(config['models'])
    for key in ('protocol', 'qualification', 'pin_base', 'reader_config', 'prompts',
                'accepted', 'output', 'pause_file', 'env_file'):
        value = config[key]
        if type(value) is not str or not value or not Path(value).is_absolute() or '..' in Path(value).parts:
            raise ValueError('explicit_absolute_configuration_path_required:' + key)
    if type(config['workers']) is not int or not 1 <= config['workers'] <= 4:
        raise ValueError('reader_context_worker_limit')
    return config


def load_frozen(protocol_path, milestone_path, *, models, pin_base, checker_config_path, prompts_path):
    protocol_path, milestone_path = Path(protocol_path), Path(milestone_path)
    models = model_roster(models)
    pin_base = Path(pin_base).resolve()
    protocol = json.loads(protocol_path.read_bytes())
    milestone = json.loads(milestone_path.read_bytes())
    if milestone['source_sha256'] != file_sha(reader.__file__):
        raise ValueError('reader_source_not_qualified')
    qualified = {}
    for pin in milestone['pins']:
        path = confined(pin_base / pin['path'], pin_base)
        if file_sha(path) != pin['sha256']:
            raise ValueError('reader_qualification_pin_changed')
        qualified[str(path)] = pin['sha256']
    checker_config_path = confined(checker_config_path, pin_base)
    prompts_path = confined(prompts_path, pin_base)
    for path in (checker_config_path, *(prompts_path / (name + '.txt')
                                      for name in ('rubric', *reader.INSTRUMENTS))):
        path = confined(path, pin_base)
        if qualified.get(str(path)) != file_sha(path):
            raise ValueError('configured_reader_input_not_qualified')
    pins = {Path(row['path']).name: row for row in protocol['artifacts']}
    selected = {}
    for model in models:
        config_pin = pins[model + '_config.json']
        config = verified_json(confined(pin_base / config_pin['path'], pin_base), config_pin['sha256'])
        job_pin = pins[model + '_jobs.json']
        jobs = verified_json(confined(pin_base / job_pin['path'], pin_base), job_pin['sha256'])
        for job in jobs:
            allocation = job['allocation']
            stages = allocation.get('reader_stages', [])
            if not stages:
                continue
            if set(stages) - {'early', 'end'} or allocation.get('reader_thresholds') != list(reader.INSTRUMENTS):
                raise ValueError('unqualified_reader_allocation')
            run_id = allocation['run_id']
            if run_id in selected or Path(run_id).name != run_id:
                raise ValueError('duplicate_or_unsafe_frozen_run_id')
            selected[run_id] = {'model': model, 'job': job, 'config': config,
                                'jobs_file_sha256': job_pin['sha256'], 'model_file_sha256': config_pin['sha256']}
    checker_config = json.loads(checker_config_path.read_bytes())
    pins = {'protocol_sha256': file_sha(protocol_path), 'milestone_sha256': file_sha(milestone_path),
            'reader_source_sha256': milestone['source_sha256'], 'watcher_source_sha256': WATCH_SOURCE_SHA,
            'reader_config_sha256': file_sha(checker_config_path),
            'prompts': {name: file_sha(prompts_path / (name + '.txt'))
                        for name in ('rubric', *reader.INSTRUMENTS)}}
    return selected, checker_config, prompts_path, pins


def bind_snapshot(receipt, frozen, snapshots_root, reader_pins):
    root = confined(receipt['path'], snapshots_root)
    manifest_path = confined(receipt['manifest'], snapshots_root)
    manifest = verified_json(manifest_path, receipt['manifest_sha256'])
    matches = [row for row in manifest['runs'] if row['run'] == receipt['run']]
    if len(matches) != 1:
        raise ValueError('snapshot_manifest_run_ambiguous')
    manifest_run = matches[0]
    files = {row['path']: row for row in manifest_run['files']}
    if len(files) != len(manifest_run['files']):
        raise ValueError('duplicate_manifest_file')
    job, config = frozen['job'], frozen['config']
    loaded, verified = {}, {}
    for name in ('allocation.json', 'spec.json', 'variant.json', 'model.json', 'summary.json',
                 'initial_context.json', 'early_context.json', 'end_context.json'):
        if name not in files:
            continue
        path = confined(root / name, snapshots_root)
        loaded[name] = verified_json(path, files[name]['sha256'])
        verified[name] = {'sha256': files[name]['sha256'], 'bytes': path.stat().st_size}
        if verified[name]['bytes'] != files[name]['bytes']:
            raise ValueError('snapshot_file_size_mismatch')
    for name, expected in [('allocation.json', job['allocation']), ('spec.json', job['spec']),
                           ('variant.json', job['variant']), ('model.json', config)]:
        if loaded.get(name) != expected:
            raise ValueError('frozen_identity_mismatch:' + name)
    summary = loaded.get('summary.json', {})
    if summary.get('run_id') != job['allocation']['run_id']:
        raise ValueError('summary_run_identity_mismatch')
    if (verified.get('summary.json', {}).get('sha256') != receipt['summary_sha256']
            or receipt['summary_sha256'] != manifest_run['summary_sha256']):
        raise ValueError('summary_receipt_hash_mismatch')
    closure = summary.get('sandbox_closure', {})
    if closure.get('state') != 'closed' or closure.get('overlay_mounted') is not False:
        raise ValueError('snapshot_sandbox_closure_not_confirmed')
    workspace = job['variant'].get('workspace', job['spec']['workspace'])
    expected_initial = [{'role': 'system', 'content': config['system'].format(workspace=workspace)},
                        *job['variant'].get('history', []), {'role': 'user', 'content': job['variant']['request']}]
    if 'initial_context.json' in loaded and loaded['initial_context.json'] != expected_initial:
        raise ValueError('initial_context_does_not_match_frozen_request')
    binding = {'schema': 'caus.reader_snapshot_binding.v1', 'model': frozen['model'],
               'run_id': job['allocation']['run_id'], 'snapshot_receipt': receipt,
               'frozen_job_sha256': reader.digest(job), 'frozen_model_sha256': reader.digest(config),
               'jobs_file_sha256': frozen['jobs_file_sha256'], 'model_file_sha256': frozen['model_file_sha256'],
               'reader_pins': reader_pins, 'source_files': verified,
               'actor_end_status': summary.get('end_status'),
               'expected_initial_context_sha256': reader.digest(expected_initial)}
    return binding, loaded


def stage_context(stage, loaded):
    initial = loaded.get('initial_context.json')
    messages = loaded.get(stage + '_context.json')
    summary = loaded['summary.json']
    if initial is None or messages is None:
        raise ValueError('required_stage_or_initial_context_not_captured')
    expected_sha = summary.get('early_context_sha256' if stage == 'early' else 'actor_context_sha256')
    if reader.digest(messages) != expected_sha:
        raise ValueError('context_does_not_match_summary_hash')
    prefix = loaded.get('early_context.json') if stage == 'end' else None
    if stage == 'end' and (summary.get('end_status') != 'natural_completion' or prefix is None):
        raise ValueError('natural_end_or_paired_prefix_not_captured')
    phase = 'prefix' if stage == 'early' else 'full'
    reader.prepare_context(messages, len(initial), phase, initial, prefix)
    return messages, initial, prefix, phase


class Watcher:
    def __init__(self, selected, checker_config, prompts, pins, accepted, output, pause_file,
                 client=None, context_runner=None, workers=4, poll_seconds=15):
        if not 1 <= workers <= 4:
            raise ValueError('reader_context_worker_limit')
        self.selected, self.config, self.prompts, self.pins = selected, checker_config, prompts, pins
        self.accepted, self.output = Path(accepted), Path(output)
        self.snapshots_root = self.accepted.parent
        self.pause_file = Path(pause_file)
        self.client, self.context_runner = client, context_runner or reader.run_context
        self.workers, self.poll = workers, min(30, max(0.005, poll_seconds))
        self.output.mkdir(parents=True, exist_ok=True)
        self.meta = self.output / '.watch'
        self.meta.mkdir(exist_ok=True)
        self.journal = self.meta / 'journal.jsonl'
        self.handled, self.completed = set(), set()
        self.failures, self.quarantine = 0, None

    def event(self, event, **fields):
        row = {'event': event, 'at_unix': time.time(), **fields}
        with self.journal.open('ab') as stream:
            stream.write(reader.encoded(row) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        fsync_dir(self.meta)
        return row

    def receipt(self, key, status, **fields):
        row = self.event(status, key=key, **fields)
        save(self.meta / 'receipts' / (uuid.uuid4().hex + '.json'), row)

    def quarantine_route(self, reason, key):
        if self.quarantine is None:
            identity = uuid.uuid4().hex
            self.quarantine = self.event('reader_route_quarantined', quarantine_id=identity, reason=reason, key=key,
                required_resolution=str(self.meta / 'route_resolutions' / (identity + '.json')),
                resolution_contract={'status': 'qualified', 'quarantine_id': identity,
                                     'qualification_evidence': 'retained repair and independent smoke evidence reference'})

    def route_blocked(self):
        if self.quarantine is None:
            return False
        path = self.meta / 'route_resolutions' / (self.quarantine['quarantine_id'] + '.json')
        if path.is_file():
            value = json.loads(path.read_bytes())
            if (value.get('status') == 'qualified' and value.get('quarantine_id') == self.quarantine['quarantine_id']
                    and value.get('qualification_evidence')):
                self.event('reader_route_requalified', quarantine_id=self.quarantine['quarantine_id'], resolution_sha256=file_sha(path))
                self.quarantine, self.failures = None, 0
                return False
        return True

    def observe_result(self, key, result):
        if result.get('integrity_failure'):
            self.quarantine_route('context_integrity:' + result['reason'], key)
            return
        instruments = result.get('instruments', {})
        failed = [name for name, row in instruments.items() if row.get('status') == 'technical_missing'
                  and row.get('technical_reason') != 'no_generated_actor_text_captured']
        if failed:
            self.failures += 1
            self.event('reader_route_failure', key=key, consecutive_failed_contexts=self.failures,
                       instruments=failed, reasons={name: instruments[name].get('technical_reason') for name in failed})
            if self.failures >= 3:
                self.quarantine_route('three_consecutive_unusable_checker_contexts', key)
        elif instruments and all(row.get('status') in ('validated', 'validated_partial_observation') for row in instruments.values()):
            self.failures = 0
            self.event('reader_route_success', key=key)

    def execute(self, frozen, stage, destination, binding, loaded, binding_error):
        if binding_error is None:
            try:
                messages, initial, prefix, phase = stage_context(stage, loaded)
                return self.context_runner(self.client, self.config, self.prompts, messages, len(initial),
                    destination, phase=phase, initial_context=initial, prefix_context=prefix)
            except Exception as error:
                reason = str(error) if type(error) is ValueError else type(error).__name__
        else:
            reason = binding_error
        # A partial reader output is retained; no call or stage is retried.
        destination.mkdir(parents=True, exist_ok=True)
        missing = {'schema': 'caus.reader_technical_missing.v1', 'status': 'technical_missing',
                   'reason': reason, 'stage': stage, 'run_id': frozen['job']['allocation']['run_id'],
                   'binding_sha256': reader.digest(binding) if binding else None,
                   'admissible_labels': None, 'replayed': False,
                   'integrity_failure': reason not in ('required_stage_or_initial_context_not_captured',
                                                       'natural_end_or_paired_prefix_not_captured')}
        save(destination / 'TECHNICAL_MISSING.json', missing)
        return missing

    def run(self):
        lock = (self.output / '.watch.lock').open('a+')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            identity = {'pins': self.pins, 'selected_sha256': reader.digest(self.selected),
                        'accepted_index': str(self.accepted), 'workers': self.workers}
            identity_path = self.meta / 'IDENTITY.json'
            if identity_path.exists():
                if json.loads(identity_path.read_bytes()) != identity:
                    raise ValueError('reader_queue_identity_changed')
            else:
                save(identity_path, identity)
            for row in jsonlines(self.journal, strict=True):
                if row['event'] == 'claimed':
                    self.handled.add(row['key'])
                elif row['event'] in ('finished', 'unresolved_prior_reader_attempt', 'existing_reader_output_not_replayed'):
                    self.completed.add(row['key'])
                    self.handled.add(row['key'])
                elif row['event'] == 'reader_route_failure':
                    self.failures = row['consecutive_failed_contexts']
                elif row['event'] == 'reader_route_success':
                    self.failures = 0
                elif row['event'] == 'reader_route_quarantined':
                    self.quarantine = row
                elif row['event'] == 'reader_route_requalified':
                    self.quarantine, self.failures = None, 0
            for key in self.handled - self.completed:
                self.receipt(key, 'unresolved_prior_reader_attempt', replayed=False, counted_semantic_negative=False)
            active = {}
            bindings = {}
            self.event('watcher_started', source_sha256=WATCH_SOURCE_SHA, selected_runs=len(self.selected),
                       selected_stages=sum(len(row['job']['allocation']['reader_stages']) for row in self.selected.values()),
                       context_workers=self.workers, maximum_threshold_calls=self.workers * 3)
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
                while True:
                    for future in list(active):
                        if future.done():
                            key = active.pop(future)
                            try:
                                result = future.result()
                                status = result.get('status', 'reader_context_returned')
                                instruments = {k: v['status'] for k, v in result.get('instruments', {}).items()}
                                self.receipt(key, 'finished', reader_status=status, instrument_statuses=instruments)
                                self.observe_result(key, result)
                            except BaseException as error:
                                self.receipt(key, 'unresolved_current_reader_attempt', error_type=type(error).__name__, replayed=False)
                    if self.pause_file.exists():
                        if not active:
                            self.event('watcher_paused', new_admissions_stopped=True)
                            return
                    elif not self.route_blocked():
                        for receipt in jsonlines(self.accepted):
                            run_id = Path(receipt['run']).name
                            frozen = self.selected.get(run_id)
                            if frozen is None:
                                continue
                            for stage in frozen['job']['allocation']['reader_stages']:
                                key = frozen['model'] + '/' + run_id + '/' + stage
                                if key in self.handled or len(active) >= self.workers or self.quarantine is not None:
                                    continue
                                destination = self.output / key
                                if destination.exists():
                                    self.receipt(key, 'existing_reader_output_not_replayed', replayed=False,
                                                 final_result_present=(destination / 'RESULT.json').is_file())
                                    self.handled.add(key)
                                    continue
                                if run_id not in bindings:
                                    try:
                                        binding, loaded = bind_snapshot(receipt, frozen, self.snapshots_root, self.pins)
                                        binding_path = destination.parent / 'BINDING.json'
                                        if binding_path.exists():
                                            if json.loads(binding_path.read_bytes()) != binding:
                                                raise ValueError('existing_binding_identity_mismatch')
                                        else:
                                            save(binding_path, binding)
                                        bindings[run_id] = (binding, loaded, None)
                                    except Exception as error:
                                        reason = str(error) if type(error) is ValueError else type(error).__name__
                                        bindings[run_id] = (None, {}, reason)
                                        self.quarantine_route('source_identity_or_evidence_integrity:' + reason, key)
                                binding, loaded, binding_error = bindings[run_id]
                                if self.pause_file.exists():
                                    break
                                self.event('claimed', key=key, snapshot_manifest_sha256=receipt['manifest_sha256'],
                                           binding_sha256=reader.digest(binding) if binding else None,
                                           binding_error=binding_error, retries=0)
                                self.handled.add(key)
                                future = pool.submit(self.execute, frozen, stage, destination, binding, loaded, binding_error)
                                active[future] = key
                    if active:
                        concurrent.futures.wait(active, timeout=self.poll, return_when=concurrent.futures.FIRST_COMPLETED)
                    else:
                        time.sleep(self.poll)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--configuration', required=True)
    args = parser.parse_args()
    options = load_configuration(args.configuration)
    selected, config, prompts, pins = load_frozen(options['protocol'], options['qualification'],
        models=options['models'], pin_base=options['pin_base'], checker_config_path=options['reader_config'],
        prompts_path=options['prompts'])
    from dotenv import dotenv_values
    from openai import OpenAI
    key = dotenv_values(options['env_file']).get('OPENAI_API_KEY')
    if not key:
        raise RuntimeError('API credential unavailable')
    with OpenAI(api_key=key, base_url='https://api.openai.com/v1', max_retries=0,
                timeout=config.get('timeout_seconds', 180)) as client:
        Watcher(selected, config, prompts, pins, options['accepted'], options['output'], options['pause_file'],
                client=client, workers=options['workers']).run()


if __name__ == '__main__':
    main()
