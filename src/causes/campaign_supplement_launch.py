"""Reviewed invocation boundaries around the saved-context supplement executor.

A host adapter supplies a fresh read-only observation and the already qualified history replay function. This module performs no host capture, route discovery, automatic refresh, retry, or capacity decision. The adapter and its complete source closure must be independently reviewed and pinned by the caller. A Boolean observation alone is insufficient: exact identities, controls, resources, complete journal captures, and retained evidence are required.

Each output belongs to one immutable family/model and prepared input set. Pilot jobs are the first two original ready IDs; continuation reuses their terminal records. A separate adoption is required for each invocation. Transport changes may alter only the endpoint; original config and request identities remain authoritative.
"""
import copy
import datetime as dt
import fcntl
import json
import os
from pathlib import Path

from causes import campaign_supplement as core

CHECKS = ('process_identity', 'listener_identity', 'model_template_revision',
          'storage_guard', 'pause_routing', 'capacity_separation', 'resource_capacity')
HISTORY_FIELDS = ('lifetime_route_failures', 'nonzero_failures', 'quarantines',
                  'unresolved_events', 'unconsumed_tail_bytes')


def require(value, reason):
    if not value:
        raise core.IntegrityError(reason)


def recent(timestamp, maximum, *, at=None):
    observed = dt.datetime.fromisoformat(timestamp)
    current = dt.datetime.fromisoformat(at or core.now())
    require(observed.tzinfo is not None and current.tzinfo is not None, 'naive_observation_time')
    age = (current - observed).total_seconds()
    if not 0 <= age <= maximum:
        raise core.AdmissionHold('supplement_observation_stale')


def transport_config(original, endpoint):
    require(isinstance(endpoint, str) and endpoint.startswith(('http://', 'https://')),
            'transport_endpoint_required')
    result = copy.deepcopy(original)
    result['endpoint'] = endpoint
    return result


def request_source_check(job, contexts, original, transport, runner):
    require(core.encoded(json.loads(core.pinned(job['frozen_config']))) == core.encoded(original),
            'frozen_original_config_changed')
    restored = copy.deepcopy(transport)
    restored['endpoint'] = original['endpoint']
    require(core.encoded(restored) == core.encoded(original), 'transport_changed_request_settings')
    context, question = core.prepare_request(job, contexts, original, runner)
    translated = core.prepare_request(job, contexts, transport, runner)
    require(core.encoded(translated) == core.encoded((context, question)),
            'transport_changed_context_or_question')
    return context, question


def shared_histories(scopes, current, replay):
    require(scopes and len({s['id'] for s in scopes}) == len(scopes), 'unique_donor_scopes_required')
    require([r['id'] for r in current] == [s['id'] for s in scopes], 'donor_scope_changed')
    inherited = 0
    for scope, row in zip(scopes, current):
        require(core.encoded(row['identity']) == core.encoded(scope['identity']), 'donor_route_identity_changed')
        baseline = core.pinned(scope['baseline'])
        raw = core.pinned(row['capture'])
        require(raw and raw.endswith(b'\n') and baseline and baseline.endswith(b'\n')
                and raw.startswith(baseline), 'donor_journal_prefix_changed')
        require(scope['mode'] in ('sealed', 'live_prefix'), 'donor_mode')
        require(scope['mode'] != 'sealed' or raw == baseline, 'sealed_donor_changed')
        before, after = replay(baseline), replay(raw)
        require(not after['unconsumed_tail_bytes'] and not after['unresolved_events'],
                'unresolved_donor_history')
        before_state = {k: before[k] for k in HISTORY_FIELDS}
        after_state = {k: after[k] for k in HISTORY_FIELDS}
        require(core.encoded(before_state) == core.encoded(scope['accepted_state']),
                'accepted_donor_baseline_changed')
        if core.encoded(before_state) != core.encoded(after_state):
            raise core.AdmissionHold('shared_route_history_changed')
        aliases = scope['model_aliases']
        require(aliases and len(set(aliases)) == len(aliases), 'explicit_model_route_aliases_required')
        if any(route in after['quarantines'] for route in aliases):
            raise core.AdmissionHold('shared_model_route_quarantined')
        for route in aliases:
            value = after['nonzero_failures'].get(route, 0)
            require(type(value) is int and value >= 0, 'invalid_shared_failure_count')
            inherited = max(inherited, value)
    return inherited


def qualify_observation(value, policy, replay, *, initial=False, at=None):
    require(value['schema'] == 'causes.supplement_admission_observation.v1', 'observation_schema')
    maximum = 180 if initial else 240
    recent(value['observed_from'], maximum, at=at)
    recent(value['observed_until'], maximum, at=at)
    require(dt.datetime.fromisoformat(value['observed_until']) >=
            dt.datetime.fromisoformat(value['observed_from']), 'observation_interval_reversed')
    require(core.encoded(value['identity']) == core.encoded(policy['runtime_identity']),
            'runtime_identity_changed')
    require(set(value['checks']) == set(CHECKS)
            and all(value['checks'][key] is True for key in CHECKS), 'unqualified_live_checks')
    require(set(value['controls']) == set(policy['remote_pause_paths'])
            and all(state is False for state in value['controls'].values()), 'remote_pause_present_or_unknown')
    resources = value['resources']
    for key, minimum in (('local_free_bytes', 80 * 1024**3),
                         ('remote_root_free_bytes', 20 * 1024**3),
                         ('remote_scratch_free_bytes', 80 * 1024**3)):
        if type(resources[key]) is not int or resources[key] < minimum:
            raise core.AdmissionHold('supplement_resource_floor:' + key)
    require(value['evidence'] and len({r['path'] for r in value['evidence']}) == len(value['evidence']),
            'retained_observation_evidence_required')
    for reference in value['evidence']:
        core.pinned(reference)
    inherited = shared_histories(policy['donors'], value['donors'], replay)
    require(inherited == policy['initial_failures'], 'inherited_failure_baseline_changed')
    return value


def launch(policy_reference, adoption_reference, *, runner, observation, replay, source_check):
    """Run one adopted invocation; callbacks must be the reviewed host adapter."""
    policy = json.loads(core.pinned(policy_reference))
    adoption = json.loads(core.pinned(adoption_reference))
    require(adoption.get('authorized_supplement_invocation') is True
            and adoption['policy'] == policy_reference, 'explicit_supplement_adoption_required')
    require(policy['phase'] in ('pilot', 'continue') and policy['max_clients'] == 1
            and type(policy['max_clients']) is int, 'single_client_scope')
    require(adoption['phase'] == policy['phase'] and type(adoption['max_clients']) is int
            and adoption['max_clients'] == 1,
            'adoption_scope_changed')
    require(type(policy['initial_failures']) is int and 0 <= policy['initial_failures'] < 3,
            'qualified_failure_baseline_required')
    recent(adoption['at'], 180)
    require(isinstance(policy['runtime_identity'], dict) and policy['runtime_identity']
            and policy['remote_pause_paths'] and policy['required_pins'], 'concrete_runtime_binding_required')
    prepared = json.loads(core.pinned(policy['prepared']))
    family, model = prepared['family'], prepared['model']
    output = core.canonical(prepared['output_root_proposed'])
    require(policy['output_root'] == str(output)
            and prepared['control_journal_proposed'] == str(output / '.control/journal.jsonl'),
            'prepared_output_scope_changed')
    jobs = prepared['ready_jobs']
    order = [job['measurement_id'] for job in jobs]
    require(order == prepared['ready_order'] and len(order) == len(set(order)) and len(order) >= 2,
            'prepared_order_changed')
    require(adoption['pilot_measurement_ids'] == policy['pilot_measurement_ids'] == order[:2],
            'original_pilot_order_changed')
    for job in jobs:
        core.measurement_path(output, job, family=family, model=model)
    require(policy['local_pause_paths'] == [str(output / 'PAUSE')], 'dedicated_local_pause_required')
    contexts = {(row['run_id'], row['stage']): row for row in prepared['contexts']}
    require(len(contexts) == len(prepared['contexts']), 'duplicate_context')
    original = json.loads(core.pinned(prepared['frozen_config']))
    require(policy['original_endpoint'] == original['endpoint'], 'original_endpoint_changed')
    transport = transport_config(original, policy['transport_endpoint'])

    def sources():
        core.pinned(policy_reference); core.pinned(adoption_reference)
        require(core.pin(Path(__file__).resolve()) == policy['launcher_source'], 'loaded_launcher_changed')
        for module, selected, original_ref in ((core, policy['core_source'], prepared['public_source']),
                                               (runner, policy['runner_source'], prepared['frozen_runner'])):
            require(core.pin(Path(module.__file__).resolve()) == selected, 'loaded_dependency_changed')
            require(core.pinned(selected) == core.pinned(original_ref), 'dependency_copy_changed')
        for reference in policy['required_pins']:
            core.pinned(reference)
        for callback, reference in ((observation, policy['host_adapter_source']),
                                    (source_check, policy['host_adapter_source']),
                                    (replay, policy['history_replay_source'])):
            require(core.pin(Path(callback.__code__.co_filename).resolve()) == reference,
                    'loaded_callback_source_changed')
        source_check()

    def request_check(job):
        sources()
        return request_source_check(job, contexts, original, transport, runner)

    def admit(initial=False):
        sources()
        return qualify_observation(observation(), policy, replay, initial=initial)

    sources()
    if policy['phase'] == 'continue':
        review = json.loads(core.pinned(policy['pilot_acceptance']))
        require(review.get('accepted_for_continuation') is True
                and review['prepared'] == policy['prepared']
                and review['pilot_measurement_ids'] == order[:2] and len(review['finals']) == 2,
                'pilot_acceptance_required')
        for job, reference in zip(jobs[:2], review['finals']):
            require(reference['path'] == str(core.measurement_path(output, job, family=family, model=model) / 'FINAL.json'),
                    'pilot_final_path_changed')
            final = json.loads(core.pinned(reference))
            require(final['measurement_id'] == job['measurement_id']
                    and final['input_sha256'] == core.digest(job) and final['technical_status'] == 'complete'
                    and final['integrity_failure'] is False, 'pilot_technical_validity_required')
    else:
        jobs = jobs[:2]
    initial = admit(initial=True)
    core.mkdir(output)
    require({p.name for p in output.iterdir()} <= {'.control', 'PAUSE', model}, 'foreign_output_namespace')
    control = core.mkdir(output / '.control')
    with core.canonical(control / 'executor.lock').open('ab') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if policy['phase'] == 'pilot':
            require(not (control / 'journal.jsonl').exists()
                    and not core.attempt_directories(output, model=model), 'pilot_already_attempted')
        identity = {'family': family, 'model': model, 'prepared': policy['prepared'],
                    'core': prepared['public_source'], 'runner': prepared['frozen_runner'],
                    'launcher': policy['launcher_source']}
        identity_path = control / 'IDENTITY.json'
        if identity_path.exists():
            require(core.encoded(core.load(identity_path)) == core.encoded(identity), 'output_identity_changed')
        else:
            require(not list(control.glob('invocation_*')) and not (control / 'journal.jsonl').exists()
                    and not core.attempt_directories(output, model=model), 'unbound_existing_output')
            core.save(identity_path, identity)
        invocation = core.canonical(control / ('invocation_' + adoption_reference['sha256']))
        require(not invocation.exists(), 'invocation_already_attempted')
        core.mkdir(invocation)
        core.save(invocation / 'INTENT.json', {'at': core.now(), 'policy': policy_reference,
                   'adoption': adoption_reference, 'phase': policy['phase'], 'pid': os.getpid(),
                   'measurement_ids': [j['measurement_id'] for j in jobs], 'initial': initial,
                   'original_endpoint': original['endpoint'], 'transport_endpoint': transport['endpoint']})
        try:
            result = core.run_queue(output, jobs,
                lambda job, destination: core.probe_measurement(job, destination, request_check, transport, runner),
                admit, request_check, policy['local_pause_paths'], family=family, model=model,
                initial_failures=policy['initial_failures'], phase=policy['phase'], max_clients=1)
            core.save(invocation / 'RESULT.json', result)
            return result
        except BaseException as error:
            core.save(invocation / 'STOPPED.json', {'at': core.now(), 'error': repr(error),
                       'requires_review': True, 'no_replay': True})
            raise
