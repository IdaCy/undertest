"""Bind reviewed storage admission to current local control and history state."""

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

import causes
from causes import campaign_admission as admission
from causes import campaign_census as census
from causes import campaign_dispatcher as dispatcher
from causes import campaign_handoff as h
from causes import campaign_monitor as monitor
from causes import campaign_runner as runner
from causes import campaign_sandbox as sandbox
from causes import campaign_storage as storage
from causes import campaign_storage_observation as observation
from causes import campaign_storage_runtime as runtime
from causes import campaign_storage_identity as identity
from causes import corpus_allocation
from causes import corpus_continuation


_SOURCE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
RELEASE = 'release_storage_owned_barrier'
_REQUIRED_READINESS = ('source_adoption', 'history_scope', 'namespace_membership',
                       'scratch_qualification', 'consumer_registration', 'resource_plan')
_MODULES = (causes, admission, census, dispatcher, h, monitor, runner, sandbox, storage,
            observation, runtime, identity, corpus_allocation, corpus_continuation)
_IMPORTED_SOURCES = {module.__name__: h.pin(Path(module.__file__).resolve())
                     for module in _MODULES}
_IMPORTED_SOURCES['causes.campaign_storage_policy'] = h.pin(Path(__file__).resolve())


def source_inventory():
    return copy.deepcopy(_IMPORTED_SOURCES)


def validate_root_reserve(binding, owners=None):
    floor = binding.get('root_floor_bytes', binding['floor_bytes'])
    storage.require(type(floor) is int and floor in (20 * 1024**3, 80 * 1024**3),
                    'storage_root_reserve_scope_changed')
    if floor < binding['floor_bytes'] and owners is not None:
        storage.require(all(owners[role]['observation_state'] == 'retired'
                            for role in ('old_mi', 'old_texas')),
                        'reduced_root_reserve_requires_both_retired')


def _accepted(pair, flag, key):
    value, review = h.checked(pair['evidence']), h.checked(pair['review'])
    storage.require(review.get(flag) is True and review.get(key) == pair['evidence'],
                    'unaccepted_storage_input:' + flag)
    return value


def validate_route_scopes(config, jobs, scopes):
    model = 'model:' + dispatcher.sha([config.get('model'), config.get('endpoint')])
    tasks = {'task:' + dispatcher.sha([job['spec'].get('task_id'), job['spec'].get('image')])
             for job in jobs}
    storage.require(scopes and tasks, 'storage_admission_route_inventory_required')
    for scope in scopes:
        models, selected = scope['model_aliases'], scope['task_routes']
        storage.require(isinstance(models, list) and len(set(models)) == len(models)
                        and model in models and all(route.startswith('model:') for route in models)
                        and isinstance(selected, list) and len(set(selected)) == len(selected)
                        and tasks <= set(selected) and all(route.startswith('task:') for route in selected),
                        'storage_admission_route_scope_incomplete')


def qualified_sealed_model(scope, replay, blockers, events, config_reference, provider):
    pair = scope['sealed_model_qualification']
    allowed = _accepted(pair, 'accepted_storage_route_scope', 'scope')
    storage.require(config_reference is not None and provider is not None,
                    'storage_qualification_current_binding_required')
    config = h.checked(config_reference)
    route = 'model:' + dispatcher.sha([config.get('model'), config.get('endpoint')])
    storage.require(scope['mode'] == 'sealed'
                    and allowed['schema'] == 'causes.storage_sealed_model_qualification.v1'
                    and allowed['output_root'] == scope['output_root']
                    and allowed['baseline'] == scope['baseline']
                    and allowed['route'] == route and allowed['consecutive_failures'] == 1
                    and type(allowed['consecutive_failures']) is int
                    and blockers == [route] and not replay['quarantines']
                    and replay['nonzero_failures'] == {route: 1}
                    and type(replay['nonzero_failures'].get(route)) is int,
                    'storage_qualification_history_scope_changed')
    plan = h.checked(allowed['plan'])
    result = _accepted(allowed['qualification'],
                       'accepted_storage_route_qualification', 'qualification')
    times = [dt.datetime.fromisoformat(value) for value in
             (events[-1]['at'], plan['at'], result['started_at'])]
    storage.require(all(value.utcoffset() is not None for value in times)
                    and times[0] < times[1] <= times[2],
                    'storage_qualification_not_after_sealed_history')
    storage.require(allowed['config'] == config_reference == plan['config'] == result['config']
                    and allowed['provider'] == plan['provider_binding']
                    and h.checked(allowed['provider']) == provider
                    and result['plan'] == allowed['plan']
                    and result['source'] == allowed['source']
                    and result['runner'] == allowed['runner'] == plan['runner'],
                    'storage_qualification_identity_changed')
    for reference in (allowed['source'], allowed['runner']):
        h.checked_bytes(reference)
    current_runner = _IMPORTED_SOURCES['causes.campaign_runner']
    storage.require(all(allowed['runner'][k] == current_runner[k] for k in ('bytes', 'sha256')),
                    'storage_qualification_runner_changed')
    storage.require(result['schema'] == 'causes.storage_route_qualification_result.v1'
                    and result['accepted'] is True and result['technical_status'] == 'complete'
                    and result['finish_reason'] == 'stop'
                    and result['context_unchanged'] is True
                    and result['rendered_context_preserved'] is True
                    and result['model_calls'] == 1 and result['task_actor_calls'] == 0
                    and type(result['model_calls']) is int and type(result['task_actor_calls']) is int
                    and result['automatic_retry'] is False
                    and result['semantic_answer_selection'] is False
                    and result['study_observation'] is False
                    and result['stage'] == plan['stage'] == 'early'
                    and result['instrument'] == plan['question']['id'] == 'evaluation_presence'
                    and result['qualification_id'] == plan['qualification_id']
                    and result['seed'] == plan['seed']
                    and plan['one_attempt_only'] is True and plan['automatic_retry'] is False,
                    'storage_qualification_unsuccessful')
    directory = Path(allowed['qualification']['evidence']['path']).parent / 'probe'
    artifacts = result['artifacts']
    expected = {'attempt.json', 'receipt.json', 'request.json', 'response.raw', 'result.json'}
    storage.require(len(artifacts) == len(expected)
                    and {r['path'] for r in artifacts} == {str(directory / n) for n in expected},
                    'storage_qualification_artifacts_changed')
    by_name = {Path(r['path']).name: r for r in artifacts}
    for reference in artifacts:
        h.checked_bytes(reference)
    receipt, attempt = h.checked(by_name['receipt.json']), h.checked(by_name['attempt.json'])
    request = h.checked(by_name['request.json'])
    storage.require(receipt['status'] == 'complete' and receipt['http_status'] == 200
                    and receipt['retry'] is False and attempt['retry'] is False
                    and receipt['request_sha256'] == attempt['request_sha256'] == runner.sha(request)
                    and receipt['response_sha256'] == by_name['response.raw']['sha256']
                    and result['started_at'] <= attempt['started_at'] == receipt['started_at']
                    <= receipt['finished_at'] <= result['finished_at'],
                    'storage_qualification_receipt_changed')
    return pair


def read_history(scope, *, config_reference=None, provider=None):
    """Read one complete current journal while preserving its accepted prefix."""
    root = h.canonical_path(scope['output_root'])
    path = root / '.dispatcher/journal.jsonl'
    baseline = scope['baseline']
    storage.require(baseline['path'] == str(path), 'storage_history_path_changed')
    with path.open('rb') as stream:
        raw = stream.read(256 * 1024**2 + 1)
    storage.require(raw and len(raw) <= 256 * 1024**2 and raw.endswith(b'\n'),
                    'storage_history_missing_torn_or_too_large')
    count = baseline['bytes']
    storage.require(type(count) is int and 0 < count <= len(raw)
                    and raw[:count].endswith(b'\n')
                    and hashlib.sha256(raw[:count]).hexdigest() == baseline['sha256'],
                    'storage_history_prefix_changed')
    storage.require(scope['mode'] in ('sealed', 'live_prefix'), 'storage_history_mode')
    storage.require(scope['mode'] != 'sealed' or len(raw) == count,
                    'sealed_storage_history_changed')
    replay = monitor.replay_history(raw)
    storage.require(replay['unconsumed_tail_bytes'] == 0 and not replay['unresolved_events'],
                    'storage_history_unresolved')
    storage.require(type(scope['recorder_relevant']) is bool,
                    'explicit_storage_recorder_scope_required')
    if not scope['recorder_relevant']:
        exception = scope['historical_recorder_scope']
        allowed = _accepted(exception, 'accepted_storage_history_scope', 'scope')
        storage.require(allowed['output_root'] == str(root)
                        and allowed['baseline'] == baseline
                        and allowed['retained_quarantines'] == replay['quarantines']
                        and allowed['root_remains_excluded'] is True
                        and allowed['requalification_performed'] is False,
                        'historical_storage_recorder_scope_changed')
        storage.require(all(route.startswith('recorder:') for route in replay['quarantines']),
                        'nonrecorder_historical_quarantine')
    blockers = monitor.history_blockers(replay, scope['model_aliases'],
                                         scope['task_routes'], scope['recorder_relevant'])
    events = [json.loads(line) for line in raw.splitlines()]
    qualification = None
    if 'sealed_model_qualification' in scope:
        qualification = qualified_sealed_model(scope, replay, blockers, events,
                                                 config_reference, provider)
        blockers = []
    storage.require(not blockers, 'storage_history_requires_requalification')
    result = {'output_root': str(root), 'journal': h.pin(path, raw),
              'events': events, 'replay': replay}
    if qualification is not None:
        result['sealed_model_qualification'] = qualification
    return result


def fixed_membership(policy, histories):
    """Recompute the exact never-claimed queue from preserved original roots."""
    frozen = policy['fixed_membership']
    jobs, config = h.checked(frozen['jobs']), h.checked(frozen['config'])
    quality = h.checked(frozen['quality'])
    unavailable = [r['run_id'] for r in quality['dispositions']
                   if r['model'] == policy['model']]
    storage.require(all(r['status'] == 'final_unavailable_quality_hold'
                        for r in quality['dispositions'] if r['model'] == policy['model']),
                    'storage_unavailable_disposition_changed')
    custody = h.checked(frozen['custody'])
    roster = {row['allocation']['run_id'] for row in jobs}
    expected_roots = [row['output_root'] for row in frozen['roots']]
    storage.require(expected_roots and len(set(expected_roots)) == len(expected_roots),
                    'complete_fixed_predecessor_inventory_required')
    by_root = {}
    seen = set()
    for row in custody[policy['model']]:
        run = Path(row['run'])
        storage.require(not run.is_absolute() and '..' not in run.parts
                        and str(run) == row['run'] and run.name in roster,
                        'invalid_storage_fixed_custody_row')
        root = str(h.canonical_path(policy['campaign_root']) / run.parent)
        storage.require(root in expected_roots and run.name not in seen,
                        'storage_fixed_custody_scope_changed')
        seen.add(run.name)
        by_root.setdefault(root, []).append(run.name)
    by_history = {r['output_root']: r for r in histories}
    rows = []
    for previous in frozen['roots']:
        root = h.canonical_path(previous['output_root'])
        state = h.retirement_state(previous['process'])
        storage.require((state['absent'] or state.get('state') in ('Z', 'X', 'x'))
                        and h.lock_free(root / '.dispatcher.lock'),
                        'fixed_storage_predecessor_not_retired')
        history = by_history[str(root)]
        events = history['events']
        claims = {r['run_id'] for r in events if r['event'] == 'claimed'}
        terminals = {r['run_id'] for r in events if r['event'] in storage.TERMINAL_EVENTS}
        rows.append({'root': str(root), 'retired': True, 'active_claims': len(claims - terminals),
                     'output_lock_free': True, 'journal': events,
                     'run_directories': sorted(p.name for p in root.iterdir()
                                               if p.is_dir() and p.name != '.dispatcher'),
                     'generated_ids': by_root.get(str(root), [])})
    result = storage.partition_fixed_jobs(jobs, config,
        [r['output_root'] for r in frozen['roots']], rows, unavailable)
    storage.require(result['jobs'] == h.checked(policy['launch']['jobs'])
                    and config == h.checked(policy['launch']['config']),
                    'storage_successor_fixed_membership_changed')
    return result


class StorageAdmission:
    def __init__(self, policy_reference, readiness_pair, authority_reference):
        self.policy_ref = policy_reference
        self.readiness_pair = readiness_pair
        self.authority_ref = authority_reference
        self.policy = h.checked(policy_reference)

    def static(self):
        p = self.policy
        storage.require(h.checked(self.policy_ref) == p
                        and p['schema'] == 'causes.storage_admission_policy.v1',
                        'storage_admission_policy_changed')
        storage.require(hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == _SOURCE_SHA,
                        'loaded_storage_policy_source_changed')
        storage.require(p['sources'] == source_inventory(), 'storage_policy_sources_changed')
        for reference in _IMPORTED_SOURCES.values():
            h.checked_bytes(reference)
        for module in (dispatcher, storage, observation, runtime):
            storage.require(hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                            == module._SOURCE_SHA, 'loaded_storage_dependency_changed')
        ready = _accepted(self.readiness_pair, 'accepted_storage_admission_readiness', 'readiness')
        storage.require(ready['schema'] == 'causes.storage_admission_readiness.v1'
                        and ready['policy'] == self.policy_ref
                        and set(ready['gates']) == set(_REQUIRED_READINESS),
                        'storage_readiness_policy_changed')
        for name in _REQUIRED_READINESS:
            gate = ready['gates'][name]
            storage.require(gate['accepted'] is True and gate['evidence'],
                            'storage_readiness_gate_missing:' + name)
            for reference in gate['evidence']:
                h.checked_bytes(reference)
        storage.require(p['role'] in ('mi', 'texas')
                        and p['model'] == {'mi': 'mi27', 'texas': 'tex9'}[p['role']]
                        and p['workers'] == 8
                        and p['max_full'] == (2 if p['role'] == 'mi' else 0),
                        'storage_capacity_scope_changed')
        launch = p['launch']
        config = h.checked(launch['config'])
        storage.require(config == h.checked(p['original_config']), 'storage_request_config_changed')
        options = launch['options']
        storage.require(options['workers'] == p['workers'] and options['max_full'] == p['max_full']
                        and options['admission_holds'] == p['holds']['path']
                        and options['admission_holds_sha256'] == p['holds']['sha256'],
                        'storage_launch_options_changed')
        h.checked_bytes(p['holds'])
        storage.require(launch['storage_binding']['output'] == p['output_root']
                        and launch['storage_binding']['floor_bytes'] == 80 * 1024**3,
                        'storage_launch_binding_changed')
        validate_root_reserve(launch['storage_binding'])
        storage.require(launch['storage_binding'].get('admission_control') == {
            'schema': 'causes.storage_admission_controls.v1', 'role': p['role'],
            'pause_routing': p['pause_routing'], 'drain_marker': p['drain_marker']},
            'continuous_storage_pause_coverage_required')
        jobs = h.checked(launch['jobs'])
        if p['role'] == 'mi':
            storage.require(jobs and not options.get('extension_prototypes')
                            and not options.get('corpus_continuation'),
                            'fixed_storage_successor_has_extensions')
        else:
            storage.require(jobs == [] and options.get('extension_prototypes')
                            and options.get('corpus_continuation'),
                            'texas_storage_disjoint_continuation_required')
            reference = p['continuation_control']
            storage.require(options['corpus_continuation'] == reference['path']
                            and options['corpus_continuation_sha256'] == reference['sha256'],
                            'texas_storage_continuation_control_changed')
            control = h.checked(reference)
            allocator = corpus_continuation.CorpusContinuation.from_control(
                control, options['extension_prototypes'], config)
            storage.require(allocator.binding == p['continuation_binding'],
                            'texas_storage_namespace_binding_changed')
        validate_route_scopes(config, jobs if p['role'] == 'mi'
                              else options['extension_prototypes'], p['history_scopes'])
        storage.require(all(type(v) in (int, float) and v > 0
                            for v in p['resource_floor'].values())
                        and set(p['resource_floor']) == {'memory_available_bytes', 'cpu_budget'},
                        'positive_storage_resource_floors_required')
        return ready

    def live(self):
        self.static()
        p = self.policy
        routing = h.checked(p['pause_routing'])
        marker = h.checked(p['drain_marker'])
        h.checked_bytes(marker['pin'])
        storage.require(h.stat_identity(marker['pin']['path']) == marker['stat'],
                        'owned_storage_drain_marker_changed')
        present = {path: os.path.lexists(path) for path in routing['user_pause_fanout']}
        storage.validate_pause_routing(routing, marker, present, admitting_role=p['role'])
        options = p['launch']['options']
        storage.require(options['pause_file'] == routing['successor_local_pauses'][p['role']]
                        and options['global_pause_file'] == routing['successor_global_pause'],
                        'storage_runtime_pause_routing_changed')
        owners = p['predecessor_observations']
        validate_root_reserve(p['launch']['storage_binding'], owners)
        wanted = {'old_mi', 'old_texas'} | ({'new_texas'} if p['role'] == 'mi' else set())
        storage.require(set(owners) == wanted and owners['old_texas']['observation_state'] == 'retired'
                        and owners['old_mi']['observation_state'] in
                        (('retired',) if p['role'] == 'mi' else ('retired', 'draining'))
                        and (p['role'] != 'mi' or owners['new_texas']['observation_state'] == 'running'),
                        'storage_predecessor_phase_changed')
        observed = {role: observation.observe_owner(owner) for role, owner in owners.items()}
        roots = [row['output_root'] for row in p['history_scopes']]
        storage.require(len(roots) == len(set(roots)) and roots, 'exact_storage_history_roots_required')
        histories = [read_history(scope, config_reference=p['launch']['config'],
                                  provider=p['provider']) for scope in p['history_scopes']]
        partition = fixed_membership(p, histories) if p['role'] == 'mi' else None
        provider = p['provider']
        h.process_identity(provider['process'])
        monitor.listener_owned(provider['process']['pid'], provider['port'])
        monitor.models(provider['port'], provider)
        for reference in provider['source_pins']:
            h.checked_bytes(reference)
        guard = storage.StorageGuard(p['launch']['storage_binding'])
        storage.require(guard.check() >= guard.binding['floor_bytes'], 'storage_admission_floor')
        resources = admission.resources(p['campaign_root'])
        storage.require(resources['memory_available_bytes'] >= p['resource_floor']['memory_available_bytes']
                        and resources['cpu_budget'] >= p['resource_floor']['cpu_budget'],
                        'storage_resource_floor')
        for owner in owners.values():
            observation._ownership(owner)
        return {'at': h.now(), 'policy': self.policy_ref,
                'history_cuts': [{k:v for k,v in row.items() if k != 'events'} for row in histories],
                'fixed_partition': None if partition is None else partition['partition'],
                'owners': {role: {k:v for k,v in row.items() if k != 'journal'}
                           for role,row in observed.items()}, 'resources': resources,
                'storage': guard.last_observation}

    def authority(self, action):
        p = self.policy
        def validate(value):
            storage.require(value['policy'] == self.policy_ref
                            and value['readiness'] == self.readiness_pair,
                            'storage_authority_scope_changed')
            self.live()
            return True
        options = p['launch']['options']
        paths = tuple(h.canonical_path(options[k]) for k in ('pause_file', 'global_pause_file'))
        return h.Authority(self.authority_ref, action, paths, validate)

    def run(self):
        p = self.policy
        launch = p['launch']
        return runtime.run_recorded_dispatcher(jobs=h.checked(launch['jobs']),
            config=h.checked(launch['config']), output=p['output_root'],
            storage_binding=launch['storage_binding'], record_directory=launch['record_directory'],
            owned_barrier=launch['owned_barrier'], options=launch['options'],
            authority=self.authority(runtime.START))

    def release(self, phase_pair, *, execute=False):
        p = self.policy
        launch = p['launch']
        registration = _accepted(phase_pair, 'accepted_storage_phase_registration', 'registration')
        role = 'new_mi' if p['role'] == 'mi' else 'new_texas'
        expected_phases = ('mi_waiting',) if p['role'] == 'mi' else (
            'texas_waiting', 'texas_waiting_mi_retired')
        storage.require(registration['phase'] in expected_phases,
                        'storage_release_requires_waiting_phase')
        owner = registration['owners'][role]
        receipt = h.checked(owner['launch_owner_receipt'])
        expected_binding = runtime.launch_binding(h.checked(launch['jobs']),
            h.checked(launch['config']), p['output_root'], launch['storage_binding'],
            launch['record_directory'], launch['owned_barrier'], launch['options'])
        storage.require(receipt['binding'] == expected_binding
                        and receipt['process'] == owner['process']
                        and receipt['controller_id'] == owner['controller_id']
                        and owner['output_root'] == p['output_root'],
                        'storage_release_owner_binding_changed')
        ordinary = self.authority(RELEASE)
        def validate(value):
            storage.require(value['storage_phase_registration'] == phase_pair,
                            'storage_release_phase_authority_changed')
            auth = h.checked(p['identity_authorization'])
            manifest = identity.validate_authorization_value(auth)
            pointer, current = identity.current_registration(auth, manifest)
            storage.require(pointer['registration'] == phase_pair['evidence']
                            and pointer['independent_review'] == phase_pair['review']
                            and current == registration, 'storage_release_phase_not_published')
            return ordinary.validate_policy(value)
        authority = h.Authority(self.authority_ref, RELEASE, ordinary.pause_paths, validate)
        def waiting(marker):
            storage.require(marker['output_root'] == p['output_root'],
                            'storage_release_barrier_output_changed')
            observation.observe_phase(registration)
            return True
        authority.check()
        waiting(h.verify_owned_barrier(launch['owned_barrier'], launch['options']['start_barrier_file']))
        if not execute:
            return {'validated': True, 'executed': False, 'action': RELEASE}
        return h.release_owned_barrier(launch['owned_barrier'],
            launch['options']['start_barrier_file'],
            Path(launch['record_directory']) / 'release', authority, waiting)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('check', 'run', 'release'))
    for name in ('policy', 'readiness', 'authority'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--phase')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    operator = StorageAdmission(*(json.loads(getattr(args, name))
                                  for name in ('policy', 'readiness', 'authority')))
    if args.action == 'check':
        result = operator.live()
    elif args.action == 'run':
        storage.require(args.execute, 'explicit_storage_run_execute_required')
        result = operator.run()
    else:
        storage.require(args.phase is not None, 'storage_release_phase_required')
        result = operator.release(json.loads(args.phase), execute=args.execute)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
