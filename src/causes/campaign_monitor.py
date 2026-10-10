import concurrent.futures
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import time
import urllib.request


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical_path(value):
    path = Path(value)
    require(path.is_absolute() and path.resolve() == path
            and not any(p.is_symlink() for p in (path, *path.parents)), 'noncanonical_monitor_path')
    return path


def checked(ref):
    raw = canonical_path(ref['path']).read_bytes()
    require(len(raw) == ref['bytes'] and digest(raw) == ref['sha256'], 'monitor_pin_changed')
    return raw


def checked_json(ref):
    return json.loads(checked(ref))


def replay_history(raw):
    prefix = raw[:raw.rfind(b'\n') + 1]
    failures, quarantines, counts, claims = {}, {}, {}, {}
    lifetime = unresolved = 0
    for line in prefix.splitlines():
        row = json.loads(line); event = row['event']
        counts[event] = counts.get(event, 0) + 1
        if event == 'claimed':
            if row['run_id'] in claims:
                raise ValueError('duplicate_or_conflicting_history_claim')
            claims[row['run_id']] = row['job_sha256']
        elif event == 'route_failure':
            if type(row['consecutive_failures']) is not int or row['consecutive_failures'] < 1:
                raise ValueError('invalid_route_failure_count')
            lifetime += 1; failures[row['route']] = row['consecutive_failures']
        elif event == 'route_success':
            for route in row['routes']: failures[route] = 0
        elif event == 'route_quarantined':
            quarantines[row['route']] = row['quarantine_id']
        elif event == 'route_requalified':
            if quarantines.get(row['route']) != row['quarantine_id']:
                raise ValueError('unbound_history_requalification')
            quarantines.pop(row['route']); failures[row['route']] = 0
        if event.startswith('unresolved_') or event == 'controller_error': unresolved += 1
    return {'bytes':len(prefix),'sha256':hashlib.sha256(prefix).hexdigest(),
            'unconsumed_tail_bytes':len(raw)-len(prefix),'event_counts':counts,
            'lifetime_route_failures':lifetime,'quarantines':quarantines,
            'nonzero_failures':{route:value for route,value in failures.items() if value},
            'unresolved_events':unresolved}


def history_blockers(journal, model_aliases, task_routes, recorder_relevant):
    routes = set(journal['nonzero_failures']) | set(journal['quarantines'])
    if any(not isinstance(route,str) or not route.startswith(('model:','task:','recorder:')) for route in routes):
        raise ValueError('unrecognized_history_route')
    return sorted(route for route in routes if route in model_aliases or route in task_routes
                  or (recorder_relevant and route.startswith('recorder:')))


def continuous_progress(identity, continuation, initial_jobs, events, persisted_blocks, barrier_present):
    if initial_jobs != [] or identity['jobs_sha256'] != hashlib.sha256(json.dumps([],sort_keys=True,ensure_ascii=False).encode()).hexdigest():
        raise ValueError('continuous_initial_queue_must_be_empty')
    if identity['corpus_continuation'] != {'control':continuation['control'],'binding':continuation['binding']}:
        raise ValueError('continuous_queue_binding_changed')
    claims = sum(row['event'] == 'claimed' for row in events)
    blocks = sum(row['event'] == 'extension_block_created' for row in events)
    if barrier_present and (claims or blocks or persisted_blocks):
        raise ValueError('waiting_continuation_issued_work')
    return {'waiting_behind_barrier':barrier_present,'initial_jobs':0,'claimed':claims,
            'published_blocks':blocks,'first_block_observed':blocks > 0}


STORAGE_PHASE_ROLES = {
    'texas_waiting': {'old_mi': 'draining', 'old_texas': 'retired', 'new_texas': 'waiting'},
    'texas_running_mi_draining': {'old_mi': 'draining', 'old_texas': 'retired', 'new_texas': 'running'},
    'texas_waiting_mi_retired': {'old_mi': 'retired', 'old_texas': 'retired', 'new_texas': 'waiting'},
    'texas_running_mi_retired': {'old_mi': 'retired', 'old_texas': 'retired', 'new_texas': 'running'},
    'mi_waiting': {'old_mi': 'retired', 'old_texas': 'retired', 'new_texas': 'running', 'new_mi': 'waiting'},
    'successors_running': {'old_mi': 'retired', 'old_texas': 'retired', 'new_texas': 'running', 'new_mi': 'running'},
}


def legacy_roles(policy):
    legacy = policy['legacy']
    transition = legacy.get('storage_transition')
    if transition is None:
        require(len(legacy['owners']) == 2 and len({r['model'] for r in legacy['owners']}) == 2,
                'exact_two_legacy_owners_required')
        return None
    require(transition['schema'] == 'causes.storage_legacy_monitor_policy.v1', 'storage_monitor_policy_schema')
    roles = STORAGE_PHASE_ROLES.get(transition['phase'])
    require(roles is not None and set(transition['role_models']) == set(roles),
            'exact_storage_monitor_roles_required')
    models = transition['role_models']
    require(all(type(value) is str and value for value in models.values())
            and models['old_mi'] != models['old_texas']
            and models['new_texas'] == models['old_texas']
            and ('new_mi' not in models or models['new_mi'] == models['old_mi']),
            'storage_monitor_role_models_changed')
    expected_models = sorted(models[role] for role, state in roles.items() if state != 'retired')
    require([row['model'] for row in legacy['owners']] == expected_models
            and len({row['pid'] for row in legacy['owners']}) == len(expected_models),
            'storage_monitor_live_owner_set_changed')
    require(legacy['identity_receipt'] == transition['registration'], 'storage_monitor_receipt_changed')
    return roles


def validate_storage_legacy(identity, policy, at):
    roles = legacy_roles(policy)
    transition = policy['legacy']['storage_transition']
    registration = checked_json(transition['registration'])
    accepted = checked_json(transition['independent_review'])
    adoption = checked_json(transition['adoption'])
    require(accepted.get('accepted_storage_phase_registration') is True
            and accepted.get('registration') == transition['registration'], 'storage_phase_not_reviewed')
    require(adoption.get('root_accepted') is True
            and adoption.get('registration') == transition['registration']
            and adoption.get('independent_review') == transition['independent_review']
            and adoption.get('manifest') == registration['identity_manifest'], 'storage_phase_not_adopted')
    require(registration['schema'] == 'causes.storage_controller_phase.v1'
            and registration['phase'] == transition['phase']
            and set(registration['owners']) == set(roles), 'storage_phase_registration_changed')
    overlay = identity['overlay']
    require(overlay['phase'] == transition['phase']
            and overlay['manifest_sha256'] == registration['identity_manifest']['sha256'],
            'storage_identity_phase_changed')
    observations = overlay['succession_observations']
    require(type(observations) is list and len(observations) == 1, 'one_fresh_storage_observation_required')
    observed = observations[0]
    require(observed['schema'] == 'causes.storage_phase_observation.v1'
            and observed['source_sha256'] == transition['observer_source']['sha256']
            and digest(checked(transition['observer_source'])) == observed['source_sha256'],
            'storage_observation_source_changed')
    started = dt.datetime.fromisoformat(observed['observed_from'])
    finished = dt.datetime.fromisoformat(observed['observed_until'])
    require(started.tzinfo is not None and finished.tzinfo is not None
            and 0 <= (finished - started).total_seconds() <= 180
            and 0 <= (at - finished).total_seconds() <= policy['max_observation_age_seconds'],
            'stale_storage_phase_observation')
    require(observed['decision'] == {'phase': transition['phase'], 'roles': roles,
                                    'processes_restarted': False, 'runtime_actions': False}
            and observed['runtime_actions'] is False
            and set(observed['observations']) == set(observed['final_ownership']) == set(roles),
            'storage_phase_observation_scope_changed')
    expected_live = []
    outputs, processes = set(), set()
    for role, state in roles.items():
        owner = registration['owners'][role]
        row = observed['observations'][role]
        process = owner['process']
        require(type(process['pid']) is int and process['pid'] > 0
                and str(process['process_starttime_ticks']).isdigit()
                and int(process['process_starttime_ticks']) > 0
                and type(process['argv']) is list and process['argv']
                and all(type(value) is str for value in process['argv']), 'invalid_storage_monitor_process')
        require(process['pid'] not in processes and owner['output_root'] not in outputs,
                'duplicate_storage_monitor_owner')
        processes.add(process['pid']); outputs.add(owner['output_root'])
        require(owner['observation_state'] == state and row['process'] == process
                and row['output_root'] == owner['output_root'], 'storage_observed_owner_changed')
        require(type(row['active_claims']) is int and row['active_claims'] >= 0,
                'invalid_storage_monitor_claims')
        owner_from = dt.datetime.fromisoformat(row['observed_from'])
        owner_until = dt.datetime.fromisoformat(row['observed_until'])
        require(started <= owner_from <= owner_until <= finished, 'storage_owner_observation_interval_changed')
        require(row['metadata_pins']['queue_identity_pin'] == owner['queue_identity_pin']
                and row['metadata_pins']['controller_identity_pin'] == owner['controller_identity_pin']
                and row['metadata_pins']['journal']['path'] == owner['journal_prefix']['path'],
                'storage_owner_metadata_binding_changed')
        evidence = [row['process_before'], row['process_after'], observed['final_ownership'][role]]
        if state == 'retired':
            require(row['live'] is False and row['lock_owner'] is None and row['active_claims'] == 0,
                    'storage_previous_owner_still_active')
            for value in evidence:
                require(value['pid'] == process['pid'] and
                        (value['absent'] is True or (value['absent'] is False
                         and value.get('state') in ('Z', 'X', 'x')
                         and value.get('start_ticks') == str(process['process_starttime_ticks']))),
                        'storage_retirement_identity_changed')
        else:
            expected = {**process, 'process_starttime_ticks': str(process['process_starttime_ticks'])}
            require(all(value == expected for value in evidence)
                    and row['live'] is True and row['lock_owner'] == process['pid']
                    and row['queue_identity'] == owner['queue_identity'], 'storage_live_identity_changed')
            expected_live.append({'pid': process['pid'], 'model': transition['role_models'][role]})
            if role.startswith('new_'):
                expected_alarms = {'owner_identity_invalid', 'reporting_lock_invalid',
                                   'guard_record_invalid', 'guard_not_ready',
                                   'storage_binding_invalid', 'volume_below_floor'}
                registered_guard = owner.get('guard', {}).get('binding', {})
                if 'capacity_guard' in registered_guard:
                    capacity_binding = registered_guard['capacity_guard']
                    require(type(capacity_binding) is dict and capacity_binding,
                            'invalid_registered_capacity_guard')
                    capacity = row['guard']['capacity_guard']
                    require(capacity['status'] == 'healthy' and capacity['admission_allowed'] is True
                            and capacity['candidate'] == process
                            and capacity['guard']['argv'] == capacity_binding['guard_argv']
                            and row['guard']['fresh_storage']['capacity_guard'] == capacity,
                            'storage_capacity_guard_invalid')
                    expected_alarms.add('capacity_guard_not_ready')
                require(row['barrier_present'] is (state == 'waiting')
                        and row['guard']['healthy'] is True
                        and set(row['guard']['alarms']) == expected_alarms
                        and all(value is False for value in row['guard']['alarms'].values()),
                        'storage_successor_guard_or_barrier_invalid')
                guard_at = dt.datetime.fromisoformat(row['guard']['at'])
                require(owner_from <= guard_at <= owner_until, 'storage_guard_observation_stale')
                if state == 'waiting':
                    require(type(row['claims']) is type(row['persisted_blocks']) is int
                            and row['claims'] == row['active_claims'] == row['persisted_blocks'] == 0,
                            'waiting_storage_successor_issued_work')
    require(sorted(expected_live, key=lambda row: row['model']) == policy['legacy']['owners'],
            'storage_registered_live_projection_changed')


def remote_validation_budget(policy, side):
    require(side in ('mi', 'jp'), 'invalid_monitor_side')
    budgets = policy.get('remote_validation_budgets', {})
    require(isinstance(budgets, dict) and set(budgets) <= {'mi', 'jp'}, 'invalid_remote_validation_budgets')
    require(all(type(value) is int and 28 <= value <= 60 for value in budgets.values()),
            'invalid_remote_validation_seconds')
    if side not in budgets:
        return None
    seconds = budgets[side]
    return {'remote_seconds':seconds, 'kill_grace_seconds':5, 'ssh_seconds':seconds + 5 + 8}



def validate_policy(policy):
    require(policy['schema'] in ('causes.campaign_monitor_policy.v1', 'causes.campaign_monitor_policy.v2'), 'monitor_policy_schema')
    require(policy['required'] is True, 'mandatory_companion_required')
    for side in ('mi', 'jp'):
        remote_validation_budget(policy, side)
    base = PurePosixPath(policy['remote_base'])
    require(base.is_absolute() and str(base) == policy['remote_base'] and '..' not in base.parts, 'invalid_remote_base')
    for key in ('controller_host', 'transfer_host'):
        require(isinstance(policy[key], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', policy[key]), 'invalid_monitor_host')
    phases = policy['phases']
    roots = policy['sync_roots']
    require(isinstance(roots, list) and roots and len(set(roots)) == len(roots), 'distinct_sync_roots_required')
    for root in roots:
        p = PurePosixPath(root)
        require(not p.is_absolute() and str(p) == root and '..' not in p.parts and p.parts, 'invalid_sync_root')
    require([p['output_root'] for p in phases] == [str(base / r) for r in roots], 'phase_root_order_changed')
    require(len({p['phase'] for p in phases}) == len(phases), 'duplicate_phase')
    transferred = policy['host_transfer']['target_output_roots']
    native = policy['schema'] == 'causes.campaign_monitor_policy.v2'
    base_count = policy['native_successions']['base_phase_count'] if native else len(phases)
    require(type(base_count) is int and 0 < base_count <= len(phases)
            and (not native or base_count < len(phases)), 'native_base_phase_count')
    base_phases = phases[:base_count]
    require(transferred and [p['output_root'] for p in base_phases if p['mode'] == 'finite'] == transferred,
            'transfer_scope_changed')
    continuous = [p for p in base_phases if p['mode'] == 'continuous']
    require(len(continuous) <= 1 and (not continuous or base_phases[-1] == continuous[0]), 'continuous_phase_must_be_last')
    if not native:
        require('native_successions' not in policy, 'native_lineage_requires_v2')
    for phase in phases:
        require(phase['mode'] in ('finite', 'continuous') and type(phase['workers']) is int and phase['workers'] > 0,
                'invalid_phase_policy')
    legacy_roles(policy)
    require(type(policy['max_observation_age_seconds']) is int and policy['max_observation_age_seconds'] > 0,
            'invalid_observation_age')
    require(len(policy['tunnels']) == 2, 'exact_two_tunnels_required')
    for key in ('provider_port','relay_port'):
        require(type(policy[key]) is int and 0 < policy[key] < 65536, 'invalid_monitor_port')
    require(type(policy['max_model_len']) is int and policy['max_model_len'] > 0
            and type(policy['minimum_gpu_free_mib']) is int and policy['minimum_gpu_free_mib'] >= 0, 'invalid_provider_policy')


class MonitorConfig:
    def __init__(self, policy_pin, authorization_path):
        self.policy_pin = json.loads(encoded(policy_pin))
        self.authorization_path = canonical_path(authorization_path)
        validate_policy(self.policy)

    @property
    def policy(self):
        return checked_json(self.policy_pin)


def validate_registration(reg, policy, reader=checked_json):
    validate_policy(policy)
    require(reg['schema'] == 'caus.jp_companion_registration.v1' and reg['status'] == 'active'
            and reg['root_authorized'] is True, 'inactive_registration')
    phases = policy['phases']
    matches = [p for p in phases if p['phase'] == reg['phase']]
    require(len(matches) == 1, 'unregistered_phase')
    phase = matches[0]
    index = phases.index(phase)
    if policy['schema'] == 'causes.campaign_monitor_policy.v2':
        from causes.campaign_corpus_succession import native_lineage
        native_lineage(policy, reg, reader)
    controller = reg['controller']
    require(reg['controller_host'] == policy['controller_host']
            and controller['model'] == policy['model_key']
            and controller['output_root'] == phase['output_root']
            and type(controller['workers']) is int
            and controller['workers'] == phase['workers'], 'controller_scope_changed')
    require(phase['process'] is not None and controller['process'] == phase['process'], 'exact_owner_required')
    require([r['output_root'] for r in reg['prior_local_owners']] == [p['output_root'] for p in phases[:index]],
            'prior_owner_root_order_changed')
    require([r['process'] for r in reg['prior_local_owners']] == [p['process'] for p in phases[:index]],
            'prior_owner_identity_changed')
    for key in ('controller_templates', 'controller_jobs', 'controller_config'):
        require(reg[key] == phase[key], 'controller_input_policy_changed')
    require(reg['runtime_cohort'] == phase['runtime_cohort'], 'runtime_cohort_changed')
    if phase['capacity_required']:
        capacity = reg['capacity_qualification']
        accepted = reader(capacity['independent_review']); reader(capacity['evidence'])
        flag = ('accepted_native_capacity_admission' if policy['schema'] == 'causes.campaign_monitor_policy.v2'
                else 'accepted_JP_local_capacity')
        require(accepted.get(flag) is True
                and accepted.get('evidence') == capacity['evidence']
                and accepted.get('next_phase') == reg['phase']
                and accepted.get('next_worker_cap') == controller['workers'], 'capacity_not_accepted')
        if policy['schema'] == 'causes.campaign_monitor_policy.v2':
            allocation = reader(policy['native_successions']['links'][-1]['allocation']['evidence'])
            require(accepted['max_full'] == allocation['max_full']
                    and accepted['workload'] == allocation['workload']
                    and accepted['resident_config'] == allocation['config'], 'native_capacity_evidence_changed')
    else:
        require(reg['capacity_qualification'] is None, 'unexpected_capacity_binding')
    transfer = reg['host_transfer']
    require(transfer == policy['host_transfer'], 'original_transfer_changed')
    manifest = reader(transfer['manifest']); accepted = reader(transfer['independent_review'])
    require(accepted.get('accepted_JP_phase02_host_transfer') is True and accepted.get('transfer') == transfer['manifest'],
            'transfer_not_accepted')
    require(manifest['mi_reservation'] == transfer['mi_reservation']
            and manifest['jobs'] == transfer['original_phase02_jobs']
            and manifest['target_output_roots'] == transfer['target_output_roots']
            and manifest['target_host'] == policy['controller_host']
            and manifest.get('all_target_ids_unissued_on_both_hosts') is True
            and manifest.get('prior_phase01_retired') is True, 'transfer_manifest_changed')
    templates = reader(reg['controller_templates'])
    matches = [t for t in templates['templates'] if t['phase'] == reg['phase']]
    require(len(matches) == 1, 'exact_controller_template_required')
    template = matches[0]
    require(template['output_root'] == controller['output_root'] and template['runtime_cohort'] == reg['runtime_cohort'],
            'controller_template_scope_changed')
    wanted = template['argv_template_at_proposed_cap'][2:].copy()
    require(1 <= controller['workers'] <= template['proposed_workers'], 'worker_cap_exceeded')
    wanted[wanted.index('--workers') + 1] = str(controller['workers'])
    actual = controller['process']['argv']
    require(Path(actual[0]).name == 'python3' and actual[1:] == wanted[1:], 'controller_argv_changed')
    bundle = reader(template['bundle'])
    expected_sources = [{k: row[k] for k in ('path', 'bytes', 'sha256')} for row in bundle['files']]
    require(controller['source_pins'] == expected_sources and controller['pythonpath'] == bundle['path'], 'controller_source_changed')
    extension_key = 'empty_extensions' if phase['mode'] == 'finite' else 'prototypes'
    required_inputs = [template[k] for k in ('jobs', 'config', extension_key, 'bundle')]
    required_inputs += [reg['controller_templates'], policy['holds']]
    require(all(ref in controller['input_pins'] for ref in required_inputs), 'required_controller_input_missing')
    for flag in ('--pause-file', '--global-pause-file', '--start-barrier-file'):
        require(actual[actual.index(flag) + 1] == phase['controls'][flag], 'controller_control_changed')
    require(not any(ref['path'] in phase['controls'].values() for ref in controller['input_pins']), 'removable_control_is_input_pin')
    for key, name in [('queue_identity','queue_identity.json'), ('admission_control','admission_control.json')]:
        require(controller[key]['path'] == controller['output_root'] + '/.dispatcher/' + name, 'controller_receipt_path_changed')
    require(reg['provider'] == policy['provider'] and reg['tunnels'] == policy['tunnels']
            and reg['reverse_listener_process'] == policy['reverse_listener_process'], 'provider_or_tunnel_identity_changed')
    if phase['mode'] == 'continuous':
        continuation = reg['continuation']
        require(phase['continuation'] is not None and continuation == phase['continuation'], 'continuation_binding_required')
        accepted = reader(continuation['source_review']); package = reader(continuation['package'])
        require(accepted.get('accepted_JP_continuation_source') is True
                and accepted.get('package') == continuation['package'], 'continuation_source_not_accepted')
        control = reader(continuation['control'])
        from causes.corpus_continuation import control_full_selection
        full_selection = control_full_selection(control)
        require(reader(template['jobs']) == [], 'continuous_initial_jobs_not_empty')
        require(control['family'] == reg['runtime_cohort'] and control['model_key'] == policy['model_key']
                and control['task_host'] == control['provider_host'] == policy['controller_host']
                and control['config'] == reg['controller_config'] and control['prototypes'] == template['prototypes'],
                'continuation_control_scope_changed')
        fields = {'catalog':'catalog_sha256','prototypes':'prototype_set_sha256','original_jobs':'original_jobs_sha256',
                  'config':'config_object_sha256','reservation':'reservation_sha256'}
        binding = {k:control[k] for k in ('family','model_key','task_host','provider_host','repetition_start')}
        binding.update(schema='causes.corpus_continuation_binding.v1', model_config_sha256=control['config']['sha256'])
        if full_selection is not None:
            require(0 < int(actual[actual.index('--max-full') + 1]) <= controller['workers'],
                    'full_continuation_requires_positive_full_limit')
            binding.update(schema='causes.corpus_continuation_binding.v2',
                           full_selection=full_selection)
        for key, name in fields.items():
            require(control[key] in controller['input_pins'], 'whole_continuation_input_missing')
            binding[name] = digest(encoded(reader(control[key])))
        require(binding == continuation['binding'] and continuation['control'] in controller['input_pins'], 'continuation_object_binding_changed')
        require(actual[actual.index('--corpus-continuation') + 1] == continuation['control']['path'], 'continuation_argv_changed')
        require(actual[actual.index('--corpus-continuation-sha256') + 1] == continuation['control']['sha256'], 'continuation_argv_hash_changed')
        require(set(continuation['allocator_sources']) == {'campaign_dispatcher.py','corpus_allocation.py','corpus_continuation.py'},
                'whole_allocator_sources_required')
        reviewed_sources = {Path(row['snapshot']['path']).name:row['snapshot']['sha256']
                            for row in package['tracked_sources_and_exact_snapshots']}
        for name, sha in continuation['allocator_sources'].items():
            require(reviewed_sources.get(name) == sha, 'allocator_not_bound_to_reviewed_package')
            require(any(Path(ref['path']).name == name and ref['sha256'] == sha for ref in expected_sources), 'allocator_source_changed')
        require(reg['donor_histories'] == phase['donor_histories'] and reg['donor_histories'], 'donor_history_binding_required')
        require([row['output_root'] for row in reg['donor_histories'] if row['host'] == policy['controller_host']]
                == [p['output_root'] for p in phases[:index]], 'prior_local_history_missing')
        history = policy['history']
        projected = [{k:row[k] for k in ('host','output_root','mode','recorder_relevant')} for row in reg['donor_histories']]
        require(projected == history['donor_scopes'], 'required_donor_scope_changed')
        require(all(row['host'] in (policy['transfer_host'],policy['controller_host']) and row['mode'] in ('sealed','live_prefix')
                    and type(row['recorder_relevant']) is bool for row in projected), 'invalid_donor_scope')
        models = [reader(ref) for ref in history['model_configs']]
        require(models and all(cfg['model'] == policy['model'] for cfg in models), 'same_provider_alias_model_changed')
        aliases = sorted({'model:'+digest(encoded([cfg.get('model'),cfg.get('endpoint')])) for cfg in models})
        tasks = sorted({'task:'+digest(encoded([job['spec'].get('task_id'),job['spec'].get('image')]))
                        for job in reader(control['prototypes'])})
        require(aliases == history['model_aliases'] and tasks == history['task_routes'], 'history_route_scope_changed')
    if policy['schema'] == 'causes.campaign_monitor_policy.v2':
        history = policy['history']
        models = [reader(ref) for ref in history['model_configs']]
        require(models and all(cfg['model'] == policy['model'] for cfg in models), 'native_history_model_changed')
        aliases = sorted({'model:'+digest(encoded([cfg.get('model'),cfg.get('endpoint')])) for cfg in models})
        allocation = reader(policy['native_successions']['links'][-1]['allocation']['evidence'])
        control = reader(allocation['allocation_control'])
        jobs = reader(reg['controller_jobs']) if phase['mode'] == 'finite' else reader(control['prototypes'])
        routes = sorted({'task:'+digest(encoded([job['spec'].get('task_id'),job['spec'].get('image')])) for job in jobs})
        require(aliases == history['model_aliases'] and routes == history['task_routes'], 'native_history_route_scope_changed')
    return phase


def registration_custody_reader(auth, policy_pin, reg):
    """Resolve only explicitly reviewed immutable registration metadata aliases."""
    pair = auth['registration_custody']
    require(type(pair) is dict and set(pair) == {'evidence', 'review'},
            'registration_custody_pair_required')

    def strict_json(raw):
        def pairs(items):
            result = {}
            for key, value in items:
                require(key not in result, 'registration_custody_duplicate_json_key')
                result[key] = value
            return result
        def constant(value):
            raise ValueError('registration_custody_nonfinite_json')
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)

    def reference(ref):
        require(type(ref) is dict and set(ref) == {'path', 'bytes', 'sha256'}
                and type(ref['bytes']) is int and ref['bytes'] >= 0
                and type(ref['sha256']) is str and len(ref['sha256']) == 64
                and all(c in '0123456789abcdef' for c in ref['sha256']),
                'registration_custody_invalid_pin')
        return canonical_path(ref['path'])

    def stable(ref):
        path = reference(ref)
        with path.open('rb') as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode), 'registration_custody_not_regular')
            raw = stream.read()
            after = os.fstat(stream.fileno())
        current = canonical_path(path).stat()
        require(stat.S_ISREG(current.st_mode)
                and all(getattr(before, k) == getattr(after, k) == getattr(current, k)
                        for k in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))
                and len(raw) == ref['bytes'] == before.st_size
                and digest(raw) == ref['sha256'], 'registration_custody_bytes_changed')
        return raw

    value, review = strict_json(stable(pair['evidence'])), strict_json(stable(pair['review']))
    require(value['schema'] == 'causes.native_monitor_registration_custody.v1'
            and review.get('accepted_native_monitor_registration_custody') is True
            and review['custody'] == pair['evidence'], 'registration_custody_not_accepted')
    require(value['policy'] == review['policy'] == policy_pin
            and value['registration'] == review['registration'] == auth['registration'],
            'registration_custody_scope_changed')
    for key in ('registered_companion_module', 'local_companion_module'):
        require(review[key] == value[key], 'registration_custody_module_binding_changed')
    require(value['local_companion_module'] == auth['companion_module']
            and stable(value['local_companion_module']) == Path(__file__).read_bytes(),
            'registration_custody_local_module_changed')
    require(digest(stable(value['registered_companion_module'])) == reg['companion_source_sha256'],
            'registered_monitor_source_changed')
    require(type(value['provenance']) is list and value['provenance'],
            'registration_custody_provenance_required')
    for ref in value['provenance']:
        stable(ref)
    rows = value['aliases']
    require(type(rows) is list and rows, 'registration_custody_aliases_required')
    aliases = {}
    for row in rows:
        require(type(row) is dict and set(row) == {'source', 'local'},
                'registration_custody_alias_shape')
        source, local = row['source'], row['local']
        reference(source); reference(local)
        require(source['path'] not in aliases and source['path'] != local['path']
                and all(source[k] == local[k] for k in ('bytes', 'sha256')),
                'registration_custody_alias_conflict')
        stable(local)
        aliases[source['path']] = (copy.deepcopy(source), copy.deepcopy(local))

    def reader(ref):
        if ref['path'] not in aliases:
            return checked_json(ref)
        source, local = aliases[ref['path']]
        require(ref == source, 'registration_custody_source_pin_changed')
        return strict_json(stable(local))
    return reader


def validate_authorization(config):
    policy = config.policy
    raw = canonical_path(config.authorization_path).read_bytes(); auth = json.loads(raw)
    require(auth['schema'] == 'caus.jp_companion_authorization.v1' and auth['policy'] == config.policy_pin, 'authorization_policy_changed')
    require(digest(checked(auth['companion_module'])) == digest(Path(__file__).read_bytes()), 'monitor_source_changed')
    reg = checked_json(auth['registration']); accepted = checked_json(auth['independent_review'])
    require(accepted.get('accepted_companion_registration') is True and accepted.get('registration') == auth['registration'],
            'registration_not_independently_accepted')
    reader = None
    if 'registration_custody' in auth:
        require(policy['schema'] == 'causes.campaign_monitor_policy.v2',
                'registration_custody_requires_native_policy')
        reader = registration_custody_reader(auth, config.policy_pin, reg)
    else:
        require(reg['companion_source_sha256'] == digest(Path(__file__).read_bytes()), 'registered_monitor_source_changed')
    if policy['schema'] == 'causes.campaign_monitor_policy.v2':
        from causes import campaign_corpus_succession as succession, corpus_allocation, corpus_continuation
        modules = {'campaign_corpus_succession': succession, 'corpus_allocation': corpus_allocation, 'corpus_continuation': corpus_continuation}
        require(set(policy['native_successions']['validator_sources']) == set(modules), 'native_validator_source_set_changed')
        for name, module in modules.items():
            require(checked(policy['native_successions']['validator_sources'][name]) == Path(module.__file__).read_bytes(),
                    'native_lineage_validator_source_changed:' + name)
    if reader is None:
        validate_registration(reg, policy)
    else:
        validate_registration(reg, policy, reader)
    return reg, {'path':str(config.authorization_path),'bytes':len(raw),'sha256':digest(raw)}, auth


def validate_legacy(status, policy, at=None):
    at = at or dt.datetime.now(dt.timezone.utc)
    observed = dt.datetime.fromisoformat(status['at'])
    require(0 <= (at - observed).total_seconds() <= policy['max_observation_age_seconds'], 'stale_legacy_observation')
    identity = status['source_identity']
    require(identity['all_expected_identities_match'] is True and identity['kernel_profile_name_loaded'] is True
            and identity['overlay']['authorized'] is True and identity['overlay']['valid'] is True, 'legacy_identity_invalid')
    require(identity['overlay']['receipt'] == policy['legacy']['identity_receipt'], 'legacy_identity_receipt_changed')
    actual = identity['controllers']
    roles = legacy_roles(policy)
    require([{k:r[k] for k in ('pid','model')} for r in actual] == policy['legacy']['owners'], 'legacy_owner_set_changed')
    if roles is not None:
        validate_storage_legacy(identity, policy, at)
    require(all(r['live'] is True and r['argv_matches'] is True and r['starttime_matches'] is True for r in actual), 'legacy_owner_not_live')
    require(identity['files'] and all(r['matches'] is True and r['expected_sha256'] == r['actual_sha256'] for r in identity['files']), 'legacy_source_changed')
    require([{k:r[k] for k in ('path','expected_sha256')} for r in identity['files']] == policy['legacy']['files'], 'legacy_source_inventory_changed')
    for alarm in policy['legacy']['alarm_keys']:
        require(status['alarms'][alarm] is False, 'legacy_alarm_active')


def process(expected):
    path = Path('/proc') / str(expected['pid'])
    before = (path/'stat').read_text().rsplit(')',1)[1].split()
    raw = (path/'cmdline').read_bytes()
    after = (path/'stat').read_text().rsplit(')',1)[1].split()
    argv = [v.decode() for v in raw.split(b'\0') if v]
    require(before[19] == after[19] == expected['process_starttime_ticks']
            and after[0] not in ('Z','X','x') and argv == expected['argv'], 'process_identity_changed')
    return {'pid':expected['pid'],'process_starttime_ticks':after[19],'argv':argv,'cmdline_sha256':digest(raw)}


def listener_owned(pid, port):
    rows = [line.split() for line in Path('/proc/net/tcp').read_text().splitlines()[1:]]
    rows = [r for r in rows if r[1] == '0100007F:' + format(port,'04X') and r[3] == '0A']
    require(len(rows) == 1, 'one_loopback_listener_required')
    links = []
    for path in (Path('/proc')/str(pid)/'fd').iterdir():
        try: links.append(os.readlink(path))
        except FileNotFoundError: pass
    require('socket:[' + rows[0][9] + ']' in links, 'listener_owner_changed')
    return {'port':port,'pid':pid,'socket_inode':rows[0][9]}


def models(port, policy):
    with urllib.request.urlopen('http://127.0.0.1:' + str(port) + '/v1/models', timeout=4) as response:
        raw = response.read(1024*1024); require(response.status == 200, 'endpoint_http_failure')
    data = json.loads(raw)['data']
    require(len(data) == 1 and data[0]['id'] == policy['model'] and data[0]['max_model_len'] == policy['max_model_len'], 'endpoint_model_changed')
    return {'port':port,'http_status':200,'bytes':len(raw),'sha256':digest(raw),'model':policy['model'],'max_model_len':policy['max_model_len']}


def remote(side, registration, policy):
    budget = remote_validation_budget(policy, side)
    host = policy['transfer_host'] if side == 'mi' else policy['controller_host']
    raw = encoded({'side':side,'registration':registration,'policy':policy})
    command = ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8',host]
    if budget is not None:
        command += ['timeout','--signal=TERM','--kill-after=5s',str(budget['remote_seconds'])+'s']
    command += ['python3','-c',shlex.quote(REMOTE)]
    started = time.monotonic()
    result = subprocess.run(command, input=raw, capture_output=True,
                            timeout=28 if budget is None else budget['ssh_seconds'])
    elapsed = time.monotonic() - started
    require(result.returncode == 0, 'remote_monitor_failed:' + host)
    observation = json.loads(result.stdout); require(observation['valid'] is True, 'invalid_remote_observation')
    report = {'host':host,'source_sha256':digest(REMOTE.encode()),'request_bytes':len(raw),'request_sha256':digest(raw),
            'returncode':result.returncode,'stdout_sha256':digest(result.stdout),'stderr_sha256':digest(result.stderr),'observation':observation}
    if budget is not None:
        report.update(execution_budget=budget, elapsed_seconds=elapsed)
    return report


def observe(config, legacy_observer):
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        policy = config.policy
        for ref in policy['legacy']['local_pins']: checked(ref)
        roots = checked_json(policy['legacy']['sync_roots'])
        require(roots == policy['legacy']['sync_roots_value'], 'legacy_sync_roots_changed')
        legacy = legacy_observer(); validate_legacy(legacy, policy)
        reg, authpin, auth = validate_authorization(config)
        tunnels = [process(row['process']) for row in reg['tunnels']]
        listener = listener_owned(reg['tunnels'][0]['process']['pid'], policy['relay_port'])
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda side: remote(side,reg,policy), ('mi','jp')))
        endpoint = models(policy['relay_port'],policy)
        require(digest(canonical_path(config.authorization_path).read_bytes()) == authpin['sha256'], 'authorization_changed_during_observation')
        checked(config.policy_pin)
        histories = [row['observation']['history'] for row in rows]
        blocked = any(value['quarantines'] or value['nonzero_failures'] or value['unresolved_events'] for value in histories)
        return {'required':True,'valid':True,'started_at':started,'at':dt.datetime.now(dt.timezone.utc).isoformat(),
                'authorization':authpin,'policy':config.policy_pin,'registration':auth['registration'],'review':auth['independent_review'],
                'legacy':legacy,'tunnels':tunnels,'listener':listener,'endpoint':endpoint,'hosts':rows,
                'alarms':{'companion_missing_or_invalid':False,'route_history_blocked':blocked}}
    except Exception as error:
        return {'required':True,'valid':False,'started_at':started,'at':dt.datetime.now(dt.timezone.utc).isoformat(),
                'error_type':type(error).__name__,'error':str(error)[:500],
                'alarms':{'companion_missing_or_invalid':True,'route_history_blocked':False}}


def merge_monitor(legacy, companion):
    result = copy.deepcopy(legacy)
    require(companion.get('required') is True, 'mandatory_companion_missing')
    result['companion'] = copy.deepcopy(companion)
    result['alarms']['companion_missing_or_invalid'] = companion.get('valid') is not True
    result['alarms']['route_history_blocked'] = companion.get('alarms',{}).get('route_history_blocked',False)
    return result


def snapshot_authorizer(monitor_config, legacy_observer):
    def authorize(snapshot_config):
        policy = monitor_config.policy
        require(snapshot_config.host == policy['controller_host'] and snapshot_config.remote_base == policy['remote_base']
                and list(snapshot_config.roots) == policy['sync_roots'], 'snapshot_monitor_scope_changed')
        result = observe(monitor_config, legacy_observer)
        require(result['valid'] is True and result['required'] is True
                and result['alarms']['companion_missing_or_invalid'] is False, 'snapshot_monitor_refused')
        evidence = {key:result[key] for key in ('authorization','policy','registration','review')}
        evidence['legacy_identity_receipt'] = policy['legacy']['identity_receipt']
        return json.loads(encoded({'host':snapshot_config.host,'remote_base':snapshot_config.remote_base,
                                  'roots':list(snapshot_config.roots),'evidence':evidence}))
    return authorize


REMOTE = r'''


from pathlib import Path
import datetime, fcntl, hashlib, json, os, subprocess, sys, urllib.request
req = json.load(sys.stdin)
side = req['side']
x = req['registration']
policy = req['policy']
out = {'at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'side': side}

def pincheck(row):
    p = Path(row['path'])
    assert p.resolve() == p and (not any((v.is_symlink() for v in (p, *p.parents))))
    b = p.read_bytes()
    assert len(b) == row['bytes'] and hashlib.sha256(b).hexdigest() == row['sha256']
    return b

def proc(e):
    p = Path('/proc') / str(e['pid'])
    s = (p / 'stat').read_text().rsplit(')', 1)[1].split()
    raw = (p / 'cmdline').read_bytes()
    t = (p / 'stat').read_text().rsplit(')', 1)[1].split()
    a = [v.decode() for v in raw.split(bytes([0])) if v]
    assert s[19] == t[19] == e['process_starttime_ticks'] and t[0] not in ['Z', 'X', 'x'] and (a == e['argv'])
    return {'pid': e['pid'], 'process_starttime_ticks': t[19], 'argv': a, 'cmdline_sha256': hashlib.sha256(raw).hexdigest()}

def listener(pid, port):
    rs = [l.split() for l in Path('/proc/net/tcp').read_text().splitlines()[1:]]
    rs = [r for r in rs if r[1] == '0100007F:' + format(port, '04X') and r[3] == '0A']
    assert len(rs) == 1
    inode = rs[0][9]
    links = []
    for f in (Path('/proc') / str(pid) / 'fd').iterdir():
        try:
            links.append(os.readlink(f))
        except FileNotFoundError:
            pass
    assert 'socket:[' + inode + ']' in links
    return {'pid': pid, 'port': port, 'socket_inode': inode}
def replay_history(raw):
    prefix = raw[:raw.rfind(b'\n') + 1]
    failures, quarantines, counts, claims = ({}, {}, {}, {})
    lifetime = unresolved = 0
    for line in prefix.splitlines():
        row = json.loads(line)
        event = row['event']
        counts[event] = counts.get(event, 0) + 1
        if event == 'claimed':
            if row['run_id'] in claims:
                raise ValueError('duplicate_or_conflicting_history_claim')
            claims[row['run_id']] = row['job_sha256']
        elif event == 'route_failure':
            if type(row['consecutive_failures']) is not int or row['consecutive_failures'] < 1:
                raise ValueError('invalid_route_failure_count')
            lifetime += 1
            failures[row['route']] = row['consecutive_failures']
        elif event == 'route_success':
            for route in row['routes']:
                failures[route] = 0
        elif event == 'route_quarantined':
            quarantines[row['route']] = row['quarantine_id']
        elif event == 'route_requalified':
            if quarantines.get(row['route']) != row['quarantine_id']:
                raise ValueError('unbound_history_requalification')
            quarantines.pop(row['route'])
            failures[row['route']] = 0
        if event.startswith('unresolved_') or event == 'controller_error':
            unresolved += 1
    return {'bytes': len(prefix), 'sha256': hashlib.sha256(prefix).hexdigest(), 'unconsumed_tail_bytes': len(raw) - len(prefix), 'event_counts': counts, 'lifetime_route_failures': lifetime, 'quarantines': quarantines, 'nonzero_failures': {route: value for route, value in failures.items() if value}, 'unresolved_events': unresolved}
def history_blockers(journal, model_aliases, task_routes, recorder_relevant):
    routes = set(journal['nonzero_failures']) | set(journal['quarantines'])
    if any((not isinstance(route, str) or not route.startswith(('model:', 'task:', 'recorder:')) for route in routes)):
        raise ValueError('unrecognized_history_route')
    return sorted((route for route in routes if route in model_aliases or route in task_routes or (recorder_relevant and route.startswith('recorder:'))))
def continuous_progress(identity, continuation, initial_jobs, events, persisted_blocks, barrier_present):
    if initial_jobs != [] or identity['jobs_sha256'] != hashlib.sha256(json.dumps([], sort_keys=True, ensure_ascii=False).encode()).hexdigest():
        raise ValueError('continuous_initial_queue_must_be_empty')
    if identity['corpus_continuation'] != {'control': continuation['control'], 'binding': continuation['binding']}:
        raise ValueError('continuous_queue_binding_changed')
    claims = sum((row['event'] == 'claimed' for row in events))
    blocks = sum((row['event'] == 'extension_block_created' for row in events))
    if barrier_present and (claims or blocks or persisted_blocks):
        raise ValueError('waiting_continuation_issued_work')
    return {'waiting_behind_barrier': barrier_present, 'initial_jobs': 0, 'claimed': claims, 'published_blocks': blocks, 'first_block_observed': blocks > 0}
def native_tail_output():
    if policy['schema'] != 'causes.campaign_monitor_policy.v2':
        return False
    phase = policy['phases'][-1]
    link = policy['native_successions']['links'][-1]
    allocation = json.loads(pincheck(link['allocation']['evidence']))
    return (phase['phase'] == x['phase'] == link['phase']
            and phase['mode'] == 'finite'
            and allocation['schema'] == 'causes.native_corpus_allocation.v1'
            and allocation['origin'] == 'native_successor'
            and allocation['target_mode'] == 'finite_qualification'
            and allocation['output_root'] == phase['output_root'] == x['controller']['output_root']
            and allocation['family'] == phase['runtime_cohort'] == x['runtime_cohort']
            and allocation['jobs'] == phase['controller_jobs'] == x['controller_jobs']
            and allocation['config'] == phase['controller_config'] == x['controller_config'])
if side == 'mi':
    out['reverse_listener_process'] = proc(x['reverse_listener_process'])
    out['reverse_listener'] = listener(x['reverse_listener_process']['pid'], policy['relay_port'])
    tr = x['host_transfer']
    reservation = json.loads(pincheck(tr['mi_reservation']))
    assert reservation['target_host'] == policy['controller_host'] and reservation['target_output_roots'] == x['host_transfer']['target_output_roots'] and (x['controller']['output_root'] in reservation['target_output_roots'] or any((p['phase'] == x['phase'] and p['mode'] == 'continuous' and (p['output_root'] == x['controller']['output_root']) for p in policy['phases'])) or native_tail_output()) and (reservation['jobs'] == x['host_transfer']['original_phase02_jobs'])
    assert not os.path.lexists(tr['mi_unused_output_root'])
    out['host_transfer_reservation'] = tr['mi_reservation']
    out['unused_MI_output_absent'] = True
    port = policy['relay_port']
elif side == 'jp':
    e = x['provider']
    out['provider'] = proc(e['process'])
    ready = json.loads(pincheck(e['ready_receipt']))
    assert all((ready['process'][k] == e['process'][k] for k in ['pid', 'process_starttime_ticks', 'argv']))
    plan = json.loads(pincheck(e['serving_plan']))
    assert plan['unit'] == e['unit'] and plan['service_argv'] == e['process']['argv'] and (plan['port'] == policy['provider_port'])
    for p in plan['fresh_small_file_pins'] + plan['runtime_source_pins'] + e['source_pins'] + [e['ready_receipt']]:
        pincheck(p)
    p = subprocess.run(['systemctl', 'show', '--all', e['unit'], '--no-pager', '--property=MainPID,ActiveState,ControlGroup,Restart'], capture_output=True, text=True, timeout=5)
    assert p.returncode == 0
    u = dict((v.split('=', 1) for v in p.stdout.splitlines() if '=' in v))
    assert u['MainPID'] == str(e['process']['pid']) and u['ActiveState'] == 'active' and (u['Restart'] == 'no')
    assert (Path('/proc') / str(e['process']['pid']) / 'cgroup').read_text() == '0::' + u['ControlGroup'] + '\n'
    out['unit'] = u
    out['listener'] = listener(e['process']['pid'], policy['provider_port'])
    with urllib.request.urlopen('http://127.0.0.1:' + str(policy['provider_port']) + '/metrics', timeout=4) as r:
        metrics = r.read(3000000).decode()
    keys = ['num_requests_running', 'num_requests_waiting', 'generation_tokens_total', 'prompt_tokens_total', 'request_success_total', 'kv_cache_usage_perc', 'gpu_cache_usage_perc']
    out['metrics'] = [v for v in metrics.splitlines() if not v.startswith('#') and any((v.startswith('vllm:' + k + '{') for k in keys)) and ('model_name=' + json.dumps(policy['model']) in v)]
    assert any((v.startswith('vllm:num_requests_running{') for v in out['metrics']))
    env = os.environ.copy()
    env['LD_LIBRARY_PATH'] = e['driver_library_path']
    p = subprocess.run(['nvidia-smi', '--query-gpu=uuid,memory.total,memory.used,memory.free,utilization.gpu', '--format=csv,noheader,nounits'], capture_output=True, text=True, env=env, timeout=8)
    assert p.returncode == 0 and p.stdout.strip().split(',')[0].strip() == e['gpu_uuid']
    assert float(p.stdout.strip().split(',')[3]) >= policy['minimum_gpu_free_mib']
    out['gpu'] = {'returncode': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}
    port = policy['provider_port']
    e = x['controller']
    out['controller'] = proc(e['process'])
    root = Path(e['output_root'])
    assert root.resolve() == root
    values = (Path('/proc') / str(e['process']['pid']) / 'environ').read_bytes().split(bytes([0]))
    selected = [v.split(b'=', 1)[1].decode() for v in values if v.startswith(b'PYTHONPATH=')]
    assert selected == [e['pythonpath']]
    out['pythonpath_matches'] = True
    for p in e['source_pins'] + e['input_pins'] + [e['queue_identity'], e['admission_control'], e['launch_receipt']]:
        pincheck(p)
    lock = root / '.dispatcher.lock'
    st = lock.stat()
    owners = []
    for line in Path('/proc/locks').read_text().splitlines():
        r = line.split()
        if len(r) >= 8 and r[1] == 'FLOCK' and (r[3] == 'WRITE'):
            dev = r[5].split(':')
            if len(dev) == 3 and int(dev[0], 16) == os.major(st.st_dev) and (int(dev[1], 16) == os.minor(st.st_dev)) and (int(dev[2]) == st.st_ino):
                owners.append(int(r[4]))
    assert owners == [e['process']['pid']], 'controller must own output lock'
    out['lock_owner'] = owners
    for old in x['prior_local_owners']:
        q = Path('/proc') / str(old['process']['pid'])
        try:
            state = (q / 'stat').read_text().rsplit(')', 1)[1].split()
        except FileNotFoundError:
            state = None
        assert state is None or (state[19] == old['process']['process_starttime_ticks'] and state[0] in ['Z', 'X', 'x'])
        with (Path(old['output_root']) / '.dispatcher.lock').open('rb') as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if policy['schema'] == 'causes.campaign_monitor_policy.v2':
        for link in policy['native_successions']['links']:
            old = link['predecessor']
            assert not (Path('/proc') / str(old['process']['pid'])).exists(), 'native predecessor not absent'
            closure = json.loads(pincheck(old['closure']['evidence']))
            for key in ('journal', 'never_claimed', 'owned_pause', 'pause_ownership'):
                pincheck(closure[key])
            marker = Path(closure['owned_pause']['path']); st = marker.stat()
            saved = closure['owned_pause_stat']
            assert all(getattr(st, key) == value for key, value in saved.items()), 'native predecessor pause replaced'
    out['prior_local_owners_closed'] = True
    raw = (root / '.dispatcher/journal.jsonl').read_bytes()
    out['journal'] = replay_history(raw)
    phase = next(p for p in policy['phases'] if p['phase'] == x['phase'])
    if phase['mode'] == 'continuous':
        continuation = x['continuation']
        identity = json.loads(pincheck(e['queue_identity']))
        assert identity['corpus_continuation'] == {'control': continuation['control'], 'binding': continuation['binding']}
        control = json.loads(pincheck(continuation['control']))
        sys.path.insert(0, e['pythonpath'])
        from causes.corpus_continuation import CorpusContinuation
        allocator = CorpusContinuation.from_control(control, json.loads(pincheck(control['prototypes'])), json.loads(pincheck(control['config'])))
        prefix = raw[:raw.rfind(bytes([10])) + 1]
        events = [json.loads(line) for line in prefix.splitlines()]
        persisted = list((root / '.dispatcher/blocks').glob('[0-9]*.json'))
        progress = continuous_progress(identity,continuation,json.loads(pincheck(x['controller_jobs'])),events,len(persisted),os.path.lexists(phase['controls']['--start-barrier-file']))
        published = [row['block_index'] for row in events if row['event'] == 'extension_block_created']
        first_block = None
        for index in range(max(published, default=-1) + 1):
            block_path = root / '.dispatcher/blocks' / (str(index).zfill(12) + '.json')
            assert block_path.resolve() == block_path and not block_path.is_symlink()
            block_raw = block_path.read_bytes(); block = json.loads(block_raw)
            allocator.accept(block)
            if index == 0:first_block = {'path':str(block_path),'bytes':len(block_raw),'sha256':hashlib.sha256(block_raw).hexdigest(),'jobs':len(block['jobs'])}
        assert allocator.check_history(prefix) == []
        out['continuation'] = {'control':continuation['control'], 'binding':allocator.binding, 'progress':progress, 'first_published_block':first_block, 'verified_journal_sha256':hashlib.sha256(prefix).hexdigest()}

else:
    raise ValueError('unknown_registered_host')
histories = []
current_host = policy['transfer_host'] if side == 'mi' else policy['controller_host']
for donor in x.get('donor_histories', []):
    if donor['host'] != current_host:
        continue
    assert donor['journal']['path'] == donor['output_root'] + '/.dispatcher/journal.jsonl'
    path = Path(donor['journal']['path'])
    assert path.resolve() == path and not any(part.is_symlink() for part in (path,*path.parents))
    raw = path.read_bytes(); ref = donor['journal']
    assert len(raw) >= ref['bytes'] and hashlib.sha256(raw[:ref['bytes']]).hexdigest() == ref['sha256']
    assert raw[:ref['bytes']].endswith(bytes([10]))
    if donor['mode'] == 'sealed':assert len(raw) == ref['bytes']
    elif donor['mode'] != 'live_prefix':raise ValueError('unknown_history_mode')
    summary = replay_history(raw)
    blocked = history_blockers(summary,policy['history']['model_aliases'],policy['history']['task_routes'],donor['recorder_relevant'])
    histories.append({'root':donor['output_root'], 'journal':summary, 'blocked_routes':blocked, 'recorder_relevant':donor['recorder_relevant']})
if side == 'jp':
    histories.append({'root':x['controller']['output_root'], 'journal':out['journal'], 'blocked_routes':sorted(set(out['journal']['quarantines']) | set(out['journal']['nonzero_failures']))})
out['history'] = {'roots':histories, 'quarantines':[], 'nonzero_failures':[], 'unresolved_events':0}
for item in histories:
    journal = item['journal']
    if any(route in journal['quarantines'] for route in item['blocked_routes']):out['history']['quarantines'].append({'root':item['root'], 'routes':journal['quarantines']})
    if any(route in journal['nonzero_failures'] for route in item['blocked_routes']):out['history']['nonzero_failures'].append({'root':item['root'], 'routes':journal['nonzero_failures']})
    out['history']['unresolved_events'] += journal['unresolved_events'] if item.get('recorder_relevant',True) else 0
with urllib.request.urlopen('http://127.0.0.1:' + str(port) + '/v1/models', timeout=4) as response:
    raw = response.read(1024 * 1024)
    assert response.status == 200
m = json.loads(raw)
assert len(m['data']) == 1 and m['data'][0]['id'] == policy['model'] and (m['data'][0]['max_model_len'] == policy['max_model_len'])
out['endpoint'] = {'port': port, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'model': policy['model'], 'max_model_len': policy['max_model_len']}
out['valid'] = True
print(json.dumps(out))
'''
