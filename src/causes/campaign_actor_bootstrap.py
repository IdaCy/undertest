"""Finite prefix actor qualification behind an independently released barrier."""
from pathlib import Path
import argparse
import ast
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from urllib.parse import urlsplit

d = h = telemetry = None
CONTROL = OUT = GLOBAL_PAUSE = None
PAUSES = ()


def checked_bytes(reference):
    path = Path(reference['path'])
    require(path.is_absolute() and path.resolve() == path
            and not any(part.is_symlink() for part in (path, *path.parents)),
            'bootstrap_noncanonical_pin')
    raw = path.read_bytes()
    require(len(raw) == reference['bytes']
            and hashlib.sha256(raw).hexdigest() == reference['sha256'],
            'bootstrap_pin_changed')
    return raw


def checked(reference):
    return json.loads(checked_bytes(reference))


def native_modules(references):
    import importlib.util
    expected = {'__init__.py', 'campaign_dispatcher.py', 'campaign_handoff.py',
                'campaign_runner.py', 'campaign_sandbox.py', 'campaign_telemetry.py'}
    require(len(references) == len(expected)
            and {Path(ref['path']).name for ref in references} == expected,
            'bootstrap_exact_native_package_required')
    roots = {Path(ref['path']).parent for ref in references}
    require(len(roots) == 1 and next(iter(roots)).name == 'causes',
            'bootstrap_native_package_root_changed')
    for ref in references:
        checked_bytes(ref)
    require(not any(key == 'causes' or key.startswith('causes.') for key in sys.modules),
            'bootstrap_requires_fresh_native_namespace')
    modules = {}
    for name in ('__init__', 'campaign_handoff', 'campaign_sandbox', 'campaign_runner',
                 'campaign_telemetry', 'campaign_dispatcher'):
        ref = next(ref for ref in references if Path(ref['path']).stem == name)
        qualified = 'causes' if name == '__init__' else 'causes.' + name
        locations = [str(next(iter(roots)))] if name == '__init__' else None
        spec = importlib.util.spec_from_file_location(qualified, ref['path'],
                                                    submodule_search_locations=locations)
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = module
        exec(compile(checked_bytes(ref), ref['path'], 'exec'), module.__dict__)
        modules[name] = module
        if name != '__init__':
            setattr(modules['__init__'], name, module)
    for name, module in modules.items():
        ref = next(ref for ref in references if Path(ref['path']).stem == name)
        require(Path(module.__file__).resolve() == Path(ref['path']), 'bootstrap_native_import_changed')
        checked_bytes(ref)
    return modules


def require(value, message):
    if not value:
        raise ValueError(message)


def process():
    return {'pid': os.getpid(), 'process_starttime_ticks':
            Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19],
            'argv': [v.decode() for v in Path('/proc/self/cmdline').read_bytes().split(b'\0') if v]}


def peer(reference, flag, **fields):
    value = h.checked(reference)
    require(value.get(flag) is True and all(value.get(k) == v for k, v in fields.items()),
            'accepted_peer_required:' + flag)
    return value


def validate_fixed_scope(p, proposal, jobs, config):
    require(p['schema'] == 'causes.finite_actor_bootstrap_runtime.v1'
            and p['workers'] == proposal['workers'] == 4
            and p['max_full'] == proposal['max_full'] == 0
            and p['spares_enabled'] is proposal['spares_enabled'] is False,
            'bootstrap_fixed_prefix_scope_changed')
    for key in ('jobs', 'config', 'control_root', 'output_root'):
        require(p[key] == proposal[key], 'bootstrap_proposal_changed:' + key)
    require(p['native_sources'] == proposal['proposed_native_actor_sources']
            and p['automatic_retry'] is False and proposal['maximum_invocations'] == 1
            and proposal['outside_fixed_study'] is True and proposal['replacements'] is False,
            'bootstrap_source_or_retry_scope_changed')
    ids = [job['allocation']['run_id'] for job in jobs]
    require(len(ids) == len(set(ids)) == proposal['prefix_count'] == 8
            and len({job['spec']['task_id'] for job in jobs}) == 8
            and all(job['allocation']['continue_to_end'] is False for job in jobs)
            and config['dispatcher']['disk_floor_bytes'] >= 112 * 1024**3,
            'bootstrap_frozen_eight_prefixes_required')
    control, output = Path(p['control_root']), Path(p['output_root'])
    for path in (control, output, Path(p['global_pause'])):
        require(path.is_absolute() and str(path) == str(path.resolve()),
                'bootstrap_noncanonical_namespace')
    require(p['pause_paths'] == [p['global_pause'], str(control / 'PAUSE'),
                                str(control / 'USER_PAUSE')]
            and len(set(p['pause_paths'])) == 3
            and control not in output.parents and output not in control.parents,
            'bootstrap_pause_or_namespace_scope_changed')
    return ids


def load(reference):
    global d, h, telemetry, CONTROL, OUT, GLOBAL_PAUSE, PAUSES
    p = checked(reference)
    require(p['source']['path'] == str(Path(__file__).resolve())
            and checked_bytes(p['source']) == Path(__file__).read_bytes(),
            'bootstrap_caller_source_changed')
    modules = native_modules(p['native_sources'])
    d, h, telemetry = (modules[name] for name in
                       ('campaign_dispatcher', 'campaign_handoff', 'campaign_telemetry'))
    proposal = h.checked(p['proposal'])
    peer(p['proposal_data_review'], 'accepted_finite_actor_bootstrap_data_and_ancestry',
         proposal=p['proposal'], jobs=p['jobs'])
    peer(p['proposal_protocol_review'], 'accepted_finite_actor_bootstrap_protocol_design',
         proposal=p['proposal'], limitation=p['prefix_observation_limitation'],
         jobs=p['jobs'], config=p['config'])
    limitation = h.checked(p['prefix_observation_limitation'])
    require(limitation['proposal'] == p['proposal']
            and limitation['no_added_actor_turn'] is True
            and limitation['no_forced_tool_choice'] is True
            and limitation['no_prompt_changes'] is True
            and limitation['no_continue_to_end_widening'] is True,
            'bootstrap_native_prefix_limitation_changed')
    jobs, config = h.checked(p['jobs']), h.checked(p['config'])
    ids = validate_fixed_scope(p, proposal, jobs, config)
    require([d.validate_job(job) for job in jobs] == ids, 'bootstrap_native_jobs_changed')
    CONTROL, OUT = Path(p['control_root']), Path(p['output_root'])
    GLOBAL_PAUSE, PAUSES = Path(p['global_pause']), tuple(map(Path, p['pause_paths']))
    data = h.checked(p['data_preparation'])
    peer(p['data_review'], 'accepted_finite_actor_bootstrap_runtime_data',
         preparation=p['data_preparation'], proposal=p['proposal'], jobs=p['jobs'], config=p['config'])
    require(data['schema'] == 'causes.finite_actor_bootstrap_runtime_data.v1'
            and all(data[key] == p[key] for key in
                    ('proposal', 'jobs', 'config', 'native_sources', 'output_root', 'control_root',
                     'global_pause', 'pause_paths', 'provider', 'provider_binding', 'boot_id', 'holds', 'host',
                     'current_owner_exclusion', 'whole_self_closure', 'asset_evidence',
                     'provider_read_source', 'provider_read_environment', 'provider_read_base_url')),
            'bootstrap_accepted_data_changed')
    validate_provider_binding(p)
    validate_provider_reader(p, config, h.checked_bytes(p['provider_read_source']))
    holds = d.AdmissionHolds(p['holds']['path'], p['holds']['sha256'])
    require(not any(holds.matches(job) for job in jobs), 'held_scientific_assignment')
    exclusion = h.checked(p['current_owner_exclusion']['evidence'])
    peer(p['current_owner_exclusion']['review'], 'accepted_native_current_owner_exclusion',
         exclusion=p['current_owner_exclusion']['evidence'])
    require(exclusion['host'] == p['host'] and exclusion['roster_complete'] is True
            and exclusion['future_owner_launch_excluded'] is True and exclusion['owners'],
            'bootstrap_current_owner_exclusion_incomplete')
    external = external_self_custody(exclusion, p['host'], p['provider_binding'], h.checked, h.checked_bytes,
        lambda pair, flag, key: (peer(pair['review'], flag, **{key: pair['evidence']}), h.checked(pair['evidence']))[1])
    closed = h.checked(p['whole_self_closure']['evidence'])
    peer(p['whole_self_closure']['review'], 'accepted_native_owner_closure_projection',
         closure=p['whole_self_closure']['evidence'])
    prior_plan = h.checked(proposal['current_reference_provider_plan'])
    prior_jobs = h.checked(prior_plan['jobs'])
    require(closed['kind'] == 'self' and closed['provider_host'] == prior_plan['host'] == p['host']
            and closed['provider'] == p['provider_binding'] and closed['self_plan'] == proposal['current_reference_provider_plan']
            and type(closed['assigned']) is int and closed['assigned'] > 0
            and closed['reservation_jobs'] == prior_plan['jobs']
            and closed['assigned'] == len(prior_jobs)
            and closed['whole_reservation_complete'] is True
            and closed['terminal'] == closed['assigned'] and closed['active_claims'] == 0
            and closed['owner_absent'] is True and closed['all_locks_free'] is True
            and any(row['closure'] == p['whole_self_closure'] for row in exclusion['owners'] + external['owners']),
            'bootstrap_whole_self_closure_required')
    for row in exclusion['owners']:
        closed = h.checked(row['closure']['evidence'])
        peer(row['closure']['review'], 'accepted_native_owner_closure_projection',
             closure=row['closure']['evidence'])
        validate_current_owner(p, row, closed)
    for key in ('source_assets', 'images', 'sandbox'):
        evidence = p['asset_evidence'][key]
        h.checked_bytes(evidence['evidence']); h.checked_bytes(evidence['review'])
        require(data['accepted_asset_checks'][key] is True, 'bootstrap_asset_check_missing:' + key)
    require(data['asset_provider'] == p['provider']
            and data['asset_native_sources'] == p['native_sources'], 'bootstrap_asset_stack_changed')
    for ref in p['required_pins'] + [proposal['retained_history_map'], proposal['retained_failed_route_state']]:
        h.checked_bytes(ref)
    require(p['runtime_authorized'] is True, 'bootstrap_runtime_plan_not_authorized')
    peer(p['source_review'], 'accepted_finite_actor_bootstrap_source', source=p['source'])
    adoption = h.checked(p['root_adoption'])
    require(adoption.get('adopted_finite_actor_bootstrap') is True
            and all(adoption[key] == p[key] for key in
                    ('source', 'proposal', 'jobs', 'config', 'provider', 'provider_binding', 'native_sources',
                     'workers', 'max_full', 'data_preparation', 'data_review'))
            and adoption['production_authorized'] is False, 'bootstrap_root_adoption_required')
    p['_reference'] = reference
    return p, jobs, config


def external_self_custody(exclusion, provider_host, provider, read, read_bytes, accept):
    rows = exclusion.get('external_self_owners', [])
    require(isinstance(rows, list) and len({row['id'] for row in rows}) == len(rows)
            and len({(row['host'], row['output_root']) for row in rows}) == len(rows),
            'external_self_owner_inventory_changed')
    require(all(row.get('host') == provider_host for row in exclusion['owners'])
            and not ({row['id'] for row in rows} & {row['id'] for row in exclusion['owners']}),
            'external_self_local_partition_changed')
    pins = []
    for row in rows:
        require(isinstance(row['host'], str) and row['host'] and row['host'] != provider_host
                and row['kind'] == 'self' and row['lock_paths']
                and len(set(row['lock_paths'])) == len(row['lock_paths']),
                'external_self_execution_host_required')
        closed = accept(row['closure'], 'accepted_native_owner_closure_projection', 'closure')
        plan = read(row['self_plan'])
        jobs = read(plan['jobs'])
        require(closed['schema'] == 'causes.native_owner_closure_projection.v1'
                and all(closed[key] == row[key] for key in
                        ('id', 'host', 'kind', 'process', 'output_root', 'lock_paths', 'self_plan'))
                and closed['provider_host'] == plan['host'] == provider_host
                and closed['provider'] == provider
                and closed['output_root'] == plan['output_root']
                and closed['reservation_jobs'] == plan['jobs']
                and type(closed['assigned']) is int and closed['assigned'] == len(jobs) > 0
                and closed['terminal'] == closed['assigned']
                and closed['whole_reservation_complete'] is True
                and closed['owner_absent'] is True and closed['all_locks_free'] is True
                and closed['active_claims'] == 0 and closed['attempts_preserved'] is True
                and closed['dispositions_preserved'] is True,
                'external_self_closure_changed')
        custody = accept(row['execution_custody'], 'accepted_native_external_self_execution_custody', 'custody')
        require(custody['schema'] == 'causes.native_external_self_execution_custody.v1'
                and custody['execution_host'] == row['host']
                and custody['provider_host'] == provider_host and custody['provider'] == provider
                and custody['closure'] == row['closure'] and custody['self_plan'] == row['self_plan']
                and custody['reservation_jobs'] == plan['jobs']
                and all(custody[key] == row[key] for key in ('process', 'output_root', 'lock_paths'))
                and custody['closed_on_execution_host'] is True
                and custody['future_self_launch_excluded'] is True
                and custody['original_execution_evidence'] and custody['original_execution_reviews'],
                'external_self_execution_custody_changed')
        refs = [*row['closure'].values(), *row['execution_custody'].values(), row['self_plan'], plan['jobs'],
                *custody['original_execution_evidence'], *custody['original_execution_reviews']]
        for ref in refs:
            read_bytes(ref)
        pins.extend(refs)
    return {'owners': rows, 'pins': pins}


def validate_current_owner(p, row, closed):
    require(row['host'] == closed['host'] == p['host']
            and all(closed[key] == row[key] for key in ('id', 'kind', 'process', 'output_root', 'lock_paths'))
            and closed['owner_absent'] is True and closed['all_locks_free'] is True
            and closed['active_claims'] == 0 and closed['attempts_preserved'] is True
            and closed['dispositions_preserved'] is True, 'bootstrap_prior_owner_not_closed')


def current_admission(p, pair, action):
    validate_provider_binding(p)
    value = h.checked(pair['evidence'])
    peer(pair['review'], 'accepted_finite_actor_bootstrap_admission', admission=pair['evidence'],
         plan=p['_reference'], action=action)
    age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(value['at'])).total_seconds()
    require(value['schema'] == 'causes.finite_actor_bootstrap_admission.v1'
            and -1 <= age < 600 and value['plan'] == p['_reference']
            and value['action'] == action and value['provider'] == p['provider']
            and value['provider_binding'] == p['provider_binding']
            and value['boot_id'] == p['boot_id']
            and value['current_owner_exclusion'] == p['current_owner_exclusion']
            and value['source_assets_sandbox_current'] is True
            and value['bounded_resource_admission'] is True,
            'bootstrap_fresh_admission_required')
    exclusion = h.checked(p['current_owner_exclusion']['evidence'])
    external = external_self_custody(exclusion, p['host'], p['provider_binding'], h.checked, h.checked_bytes,
        lambda pair, flag, key: (peer(pair['review'], flag, **{key: pair['evidence']}), h.checked(pair['evidence']))[1])
    if external['owners']:
        require(value['external_self_owners'] == external['owners']
                and value['external_execution_exclusions_current'] is True,
                'bootstrap_fresh_external_self_exclusion_required')
    reservation = h.checked(value['reservation'])
    require(value['reservation_complete'] is True and value['census_pins'],
            'bootstrap_complete_current_reservation_required')
    for ref in value['census_pins']:
        h.checked_bytes(ref)
    jobs = h.checked(p['jobs'])
    ids = {job['allocation']['run_id'] for job in jobs}
    seeds = set()
    for job in jobs:
        for key in ('block_seed', 'model_seed', 'task_seed', 'seed'):
            seed = job['allocation'][key]
            require(type(seed) is int and seed >= 0, 'bootstrap_invalid_allocation_seed')
            seeds.add(seed)
    require(value['frozen_run_ids'] == sorted(ids)
            and value['frozen_allocation_seeds'] == sorted(seeds)
            and ids.isdisjoint(reservation['external_run_ids'])
            and seeds.isdisjoint(reservation['external_allocation_seeds'])
            and reservation['excluded_owned_output'] == (p['output_root'] if action == 'release_finite_actor_held_owner' else None),
            'bootstrap_current_id_or_seed_collision')
    return value


def validate_provider_binding(p):
    require(isinstance(p['provider_binding'], dict)
            and p['provider_binding'].get('process') == p['provider'],
            'bootstrap_provider_process_binding_changed')


def validate_provider_reader(p, config, raw):
    endpoint = urlsplit(config['endpoint'])
    require(endpoint.scheme in ('http', 'https') and endpoint.hostname is not None
            and endpoint.username is None and endpoint.password is None
            and not endpoint.query and not endpoint.fragment
            and p['provider_read_base_url'] == endpoint.scheme + '://' + endpoint.netloc,
            'bootstrap_provider_reader_endpoint_changed')
    functions = [node for node in ast.parse(raw).body if isinstance(node, ast.FunctionDef)
                 and node.name == 'http']
    require(len(functions) == 1, 'bootstrap_native_http_reader_required')
    assignments = [node for node in functions[0].body if isinstance(node, ast.Assign)
                   and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                   and node.targets[0].id == 'url']
    require(len(assignments) == 1, 'bootstrap_native_http_base_required')
    value = assignments[0].value
    require(isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add)
            and isinstance(value.left, ast.Constant) and value.left.value == p['provider_read_base_url']
            and isinstance(value.right, ast.Name) and value.right.id == 'suffix',
            'bootstrap_native_http_base_changed')
    environment = p['provider_read_environment']
    require(set(environment) <= {'PATH', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH'}
            and {'PATH', 'LANG', 'LC_ALL'} <= set(environment)
            and environment['LANG'] == environment['LC_ALL'] == 'C'
            and all(isinstance(value, str) and value for value in environment.values()),
            'bootstrap_provider_environment_changed')


def provider_module(p):
    raw = h.checked_bytes(p['provider_read_source'])
    validate_provider_reader(p, h.checked(p['config']), raw)
    ns = {'__name__': 'bootstrap_native_provider_read', '__file__': p['provider_read_source']['path']}
    exec(compile(raw, ns['__file__'], 'exec'), ns)
    ns['ENV'] = dict(p['provider_read_environment'])
    return ns


def resources(p):
    validate_provider_binding(p)
    require(Path('/proc/sys/kernel/random/boot_id').read_text().strip() == p['boot_id'], 'bootstrap_boot_changed')
    provider = h.process_identity(p['provider'])
    exclusion = h.checked(p['current_owner_exclusion']['evidence'])
    external_self_custody(exclusion, p['host'], p['provider_binding'], h.checked, h.checked_bytes,
        lambda pair, flag, key: (peer(pair['review'], flag, **{key: pair['evidence']}), h.checked(pair['evidence']))[1])
    for row in exclusion['owners']:
        validate_current_owner(p, row, h.checked(row['closure']['evidence']))
        require(h.retirement_state(row['process'])['absent']
                and all(h.lock_free(Path(path)) for path in row['lock_paths']),
                'bootstrap_previous_owner_live_or_locked')
    memory = {line.split(':')[0]: int(line.split()[1])*1024
              for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemAvailable:', 'MemTotal:'))}
    result = {'provider': provider, 'affinity': sorted(os.sched_getaffinity(0)),
              'memory': memory, 'root_free_bytes': shutil.disk_usage('/').free}
    require(len(result['affinity']) >= 11 and memory['MemAvailable'] >= 48*1024**3
            and result['root_free_bytes'] >= 112*1024**3, 'bootstrap_resource_envelope_insufficient')
    return result


def health_counters(metrics, model):
    selected = telemetry.select_health_series(metrics,
        name_pattern='(?i)preempt|swap|error|failure',
        histogram_families=('vllm:request_num_preemptions',),
        required_names=('vllm:num_preemptions_total', 'vllm:request_num_preemptions_sum',
                        'vllm:num_preemptions_created', 'vllm:request_num_preemptions_created',
                        'vllm:request_success_created'),
        required_label_series=tuple(('vllm:request_success_total',
            r'(?:^|[,{])\s*(?:finish|finished_reason)="' + reason + r'"\s*(?=[,}])')
            for reason in ('error', 'abort')))
    base = '{engine="0",model_name=' + json.dumps(model) + '}'
    labels = lambda reason: '{engine="0",finished_reason="' + reason + '",model_name=' + json.dumps(model) + '}'
    expected = {(name, base) for name in ('vllm:num_preemptions_total',
        'vllm:request_num_preemptions_sum', 'vllm:num_preemptions_created',
        'vllm:request_num_preemptions_created')}
    expected.update(('vllm:request_success_total', labels(reason)) for reason in ('abort', 'error'))
    expected.update(('vllm:request_success_created', labels(reason))
                    for reason in ('stop', 'length', 'abort', 'error', 'repetition'))
    require(set(selected) == expected, 'exact_four_fault_totals_seven_epochs_required')
    require(all(value > 0 for key, value in selected.items() if key[0].endswith('_created')),
            'positive_creation_epochs_required')
    return selected


def pause(p, reason):
    try:
        return h.save_new(CONTROL, 'PAUSE', {'schema': 'causes.finite_actor_bootstrap_owned_pause.v1',
            'at': h.now(), 'plan': p['_reference'], 'reason': reason, 'automatic_resume': False})
    except FileExistsError:
        return {'existing_marker_preserved': True, 'path': str(CONTROL / 'PAUSE')}


class Guard:
    """Observe every20s; a failed observation stops later admission and drains active jobs.

One observation can spend up to15s in the three bounded native HTTP/kernel reads.
Claims additionally require a successful observation less than45s old. This is
not an interrupt of an in-flight model request or immediate one-fault quarantine.
"""
    def __init__(self, p):
        self.p, self.native = p, provider_module(p)
        self.sequence, self.last_good, self.failed = 0, None, False
        self.since = h.now()
        self.stop = threading.Event()
        self.dispatcher = None
        self.baseline_counters = None
        self.thread = None

    def sample(self):
        p = self.p
        value = {'schema': 'causes.finite_actor_bootstrap_guard_observation.v1', 'at_start': h.now(),
                 'sequence': self.sequence, 'plan': p['_reference'], 'errors': []}
        try:
            value['resources_before'] = resources(p)
            value['metrics'] = self.native['metrics']()
            values = value['metrics']['values']
            kv = [x['value'] for x in values if x['name'] == 'vllm:kv_cache_usage_perc']
            require(len(kv) == 1 and type(kv[0]) in (int, float) and 0 <= kv[0] < .8,
                    'finite_single_kv_fraction_below_point8_required')
            counters = health_counters(value['metrics'], h.checked(p['config'])['model'])
            if self.baseline_counters is None:
                require(all(v == 0 for key, v in counters.items() if not key[0].endswith('_created')),
                        'initial_service_fault_totals_must_be_zero')
            value['fault_counters'] = [{'name': k[0], 'labels': k[1], 'value': v} for k, v in sorted(counters.items())]
            if self.baseline_counters is not None:
                require(counters == self.baseline_counters, 'service_fault_counter_or_epoch_changed')
            value['kernel'] = self.native['kernel']({'boot_id': p['boot_id'], 'kernel_since': self.since})
            require(value['kernel']['error_records'] == [], 'kernel_driver_or_oom_fault')
            value['resources_after'] = resources(p)
            if self.dispatcher is not None:
                value['native_quarantines'] = self.dispatcher.quarantines.copy()
                require(not value['native_quarantines'], 'native_route_quarantine_preserved')
            self.baseline_counters = counters
            self.since = value['kernel']['until']
        except Exception as error:
            value['errors'].append({'type': type(error).__name__, 'error': str(error)})
        value.update(at_end=h.now(), healthy=not value['errors'])
        ref = h.save_new(CONTROL / 'observations', '%08d.json' % self.sequence, value)
        self.sequence += 1
        if value['errors']:
            self.failed = True
            pause(p, {'guard_observation': ref, 'errors': value['errors']})
        else:
            self.last_good = time.monotonic()
            if self.sequence == 1:
                h.save_new(CONTROL, 'READY.json', {'at': h.now(), 'plan': p['_reference'], 'observation': ref})
        return ref

    def available(self):
        try:
            require(not self.failed and self.last_good is not None
                    and 0 <= time.monotonic() - self.last_good < 45, 'guard_observation_not_fresh')
            require(not any(os.path.lexists(x) for x in PAUSES), 'pause_blocks_claim')
            require(self.dispatcher is None or not self.dispatcher.quarantines.copy(),
                    'native_route_quarantine_preserved')
            return resources(self.p)['root_free_bytes']
        except Exception as error:
            self.failed = True
            pause(self.p, 'admission_guard:' + type(error).__name__ + ':' + str(error))
            return 0

    def loop(self):
        while not self.stop.wait(20) and not self.failed:
            try:
                if any(os.path.lexists(x) for x in PAUSES):
                    pause(self.p, 'user_or_owned_pause_observed')
                    return
                self.sample()
            except BaseException as error:
                self.failed = True
                pause(self.p, 'guard_recorder_failure:' + type(error).__name__)


def authority(p, ref, action):
    def policy(value):
        peer(value['final_review'], 'accepted_finite_actor_bootstrap_runtime_plan', plan=p['_reference'], source=p['source'])
        return (value.get('plan') == p['_reference'] and value.get('source') == p['source']
                and value.get('automatic_retry') is False
                and bool(current_admission(p, value['admission'], action)) and bool(resources(p)))
    return h.Authority(ref, action, PAUSES, policy)


def start(p, jobs, config, authority_ref):
    auth = authority(p, authority_ref, 'start_finite_actor_held_owner')
    auth.check()
    require(not os.path.lexists(CONTROL) and not os.path.lexists(OUT.parent), 'fresh_bootstrap_namespace_required')
    CONTROL.mkdir(mode=0o700)
    (CONTROL / 'observations').mkdir(mode=0o700)
    h.save_new(CONTROL, 'START_INTENT.json', {'at': h.now(), 'plan': p['_reference'],
               'authority': authority_ref, 'process': process(), 'automatic_restart': False})
    OUT.parent.mkdir(mode=0o700)
    OUT.mkdir(mode=0o700)
    owned = h.create_owned_barrier(CONTROL / 'ADMISSION_BARRIER', OUT, 'finite_actor_bootstrap', CONTROL / 'barrier', auth)
    guard = Guard(p)
    try:
        guard.sample()
        require(not guard.failed, 'initial_guard_refused')
        dispatcher = d.Dispatcher(jobs, config, OUT, workers=4, max_full=0, extension_prototypes=[],
            pause_file=CONTROL / 'PAUSE', global_pause_file=GLOBAL_PAUSE,
            start_barrier_file=CONTROL / 'ADMISSION_BARRIER', free_bytes=guard.available,
            admission_holds=p['holds']['path'], admission_holds_sha256=p['holds']['sha256'])
        guard.dispatcher = dispatcher
        owner = {'at': h.now(), 'plan': p['_reference'], 'process': process(),
                 'controller_id': dispatcher.controller_id, 'owned_barrier': owned,
                 'output_root': str(OUT), 'jobs': p['jobs'], 'config': p['config'],
                 'workers': 4, 'max_full': 0, 'caller_source': p['source']}
        h.save_new(CONTROL, 'OWNER.json', owner)
        auth.check()
        guard.thread = threading.Thread(target=guard.loop, name='finite_actor_bootstrap_guard', daemon=True)
        guard.thread.start()
        result = dispatcher.run()
        h.save_new(CONTROL, 'RESULT.json', {'at': h.now(), 'owner': owner, 'result': result,
                   'automatic_restart': False, 'native_quarantines': dispatcher.quarantines})
    except BaseException as error:
        pause(p, 'owner_exception:' + type(error).__name__)
        h.save_new(CONTROL, 'FAILED.json', {'at': h.now(), 'error_type': type(error).__name__,
                   'error': str(error), 'automatic_restart': False})
        raise
    finally:
        guard.stop.set()
        if guard.thread is not None:
            guard.thread.join(timeout=20)


def waiting(p):
    owner = h.checked(h.pin(CONTROL / 'OWNER.json'))
    require(owner['plan'] == p['_reference'] and owner['caller_source'] == p['source'], 'owner_binding_changed')
    h.process_identity(owner['process'])
    require(h.lock_owned_by(OUT / '.dispatcher.lock', owner['process']['pid']), 'owned_output_lock_required')
    h.verify_owned_barrier(owner['owned_barrier'], CONTROL / 'ADMISSION_BARRIER')
    raw = (OUT / '.dispatcher/journal.jsonl').read_bytes()
    require(0 < len(raw) <= 1024**2 and raw.endswith(b'\n'), 'bounded_whole_held_journal_required')
    rows = [json.loads(line) for line in raw.splitlines()]
    require(all(x['controller_id'] == owner['controller_id'] for x in rows)
            and any(x['event'] == 'admission_barrier_waiting' for x in rows)
            and not any(x['event'] in ('claimed', 'extension_block_created') for x in rows),
            'unclaimed_held_owner_required')
    identity = h.checked(h.pin(OUT / '.dispatcher/queue_identity.json'))
    require(identity['jobs_sha256'] == d.sha(h.checked(p['jobs']))
            and identity['config_sha256'] == d.sha(h.checked(p['config']))
            and identity['prototypes_sha256'] == d.sha([]), 'held_queue_identity_changed')
    return owner, h.pin(OUT / '.dispatcher/journal.jsonl', raw)


def release(p, authority_ref):
    auth = authority(p, authority_ref, 'release_finite_actor_held_owner')
    value = auth.check()
    owner, journal = waiting(p)
    peer(value['held_review'], 'accepted_finite_actor_bootstrap_held_owner', owner=h.pin(CONTROL / 'OWNER.json'))
    def validate(_):
        waiting(p)
        latest = sorted((CONTROL / 'observations').glob('[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].json'))[-1]
        sample = h.checked(h.pin(latest))
        finished = dt.datetime.fromisoformat(sample['at_end'])
        require(sample['plan'] == p['_reference'] and sample['healthy'] is True and sample['errors'] == []
                and -1 <= (dt.datetime.now(dt.timezone.utc) - finished).total_seconds() < 45,
                'fresh_healthy_bootstrap_guard_required')
        resources(p)
        return True
    return h.release_owned_barrier(owner['owned_barrier'], CONTROL / 'ADMISSION_BARRIER', CONTROL / 'release', auth, validate)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=('check', 'start', 'observe', 'release'))
    ap.add_argument('--plan', required=True, type=Path)
    ap.add_argument('--plan-sha256', required=True)
    ap.add_argument('--authority', type=Path)
    ap.add_argument('--authority-sha256')
    args = ap.parse_args()
    raw = args.plan.resolve().read_bytes()
    ref = {'path': str(args.plan.resolve()), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    require(ref['sha256'] == args.plan_sha256, 'plan_pin_changed')
    p, jobs, config = load(ref)
    if args.action == 'check':
        print(json.dumps({'checked': True, 'jobs': len(jobs), 'runtime_invoked': False}))
    elif args.action == 'observe':
        owner, journal = waiting(p)
        print(json.dumps({'owner': owner, 'owner_reference': h.pin(CONTROL / 'OWNER.json'),
                          'journal': journal, 'held': True, 'claimed': 0, 'unclaimed': 8}))
    else:
        authority_ref = h.pin(args.authority.resolve())
        require(authority_ref['sha256'] == args.authority_sha256, 'authority_pin_changed')
        result = start(p, jobs, config, authority_ref) if args.action == 'start' else release(p, authority_ref)
        print(json.dumps(result))


if __name__ == '__main__':
    main()
