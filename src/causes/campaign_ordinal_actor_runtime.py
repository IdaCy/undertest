"""Run one reviewed ordinal slice with inherited route state and native guards."""
import argparse
import copy
import datetime as dt
import json
import os
import socket
from causes import campaign_dispatcher as d
from causes import campaign_handoff as h
from causes import campaign_monitor as m
from causes import campaign_storage as s
from causes import campaign_runner as runner
from causes import campaign_sandbox as sandbox
from causes import campaign_storage_observation as observation
from causes import campaign_telemetry as telemetry
from causes import campaign_storage_runtime as runtime


def require(value, reason):
    if not value:
        raise ValueError(reason)


def peer(reference, flag, **joins):
    value = h.checked(reference)
    require(value.get(flag) is True and value.get('findings', []) == []
            and all(exact(value.get(k), v) for k, v in joins.items()), 'unaccepted:' + flag)
    return value


def positive_int(value):
    return type(value) is int and value > 0


def exact(left, right):
    return d.encoded(left) == d.encoded(right)


def validate_ordinals(p, global_jobs, host_jobs, jobs, ordinals):
    """Select exactly the assigned slice; never refill around an occupied job."""
    bounds = p['ordinal_range']
    require(type(bounds) is list and len(bounds) == 2
            and all(type(v) is int for v in bounds), 'integer_ordinal_range_required')
    start, stop = bounds
    require(0 < start < stop <= len(host_jobs), 'continuation_range_required')
    require(positive_int(p['global_job_count']) and positive_int(p['host_job_count'])
            and len(global_jobs) == p['global_job_count']
            and len(host_jobs) == p['host_job_count'], 'fixed_assignment_count_changed')
    require(type(ordinals) is list and len(ordinals) == len(host_jobs)
            and all(type(i) is int and 0 <= i < len(global_jobs) for i in ordinals)
            and len(set(ordinals)) == len(ordinals)
            and exact(host_jobs, [global_jobs[i] for i in ordinals]), 'host_partition_changed')
    global_ids = [d.validate_job(j) for j in global_jobs]
    require(len(global_ids) == len(set(global_ids)), 'duplicate_global_assignment')
    require(exact(jobs, host_jobs[start:stop]), 'exact_ordinal_slice_required')
    require(p['instruments'] == ['evaluation_presence', 'evaluation_strongly_favors',
                                'evaluation_leans_towards_or_favors']
            and all(j['allocation']['continue_to_end'] is False
                    and j['allocation']['instruments'] == p['instruments'] for j in jobs),
            'prefix_instrument_scope')


def validate_history(p, global_jobs, host_jobs, config):
    """Replay every inherited event and prove the preceding prefix is terminal."""
    history = h.checked(p['route_carry'])
    peer(p['route_carry_review'], 'accepted_requested_actor_route_carry',
         actual=p['route_carry'], config=p['config'], role=p['role'])
    parent = h.checked(p['route_carry_parent'])
    peer(p['route_carry_parent_review'], 'accepted_requested_actor_route_carry',
         actual=p['route_carry_parent'], config=p['config'], role=p['role'])
    require(parent['config'] == history['config'] == p['config']
            and parent['role'] == history['role'] == p['role']
            and parent['native_replay_source'] == history['native_replay_source']
            == p['native_sources']['campaign_monitor']
            and history['journals'][:len(parent['journals'])] == parent['journals']
            and len(history['journals']) > len(parent['journals']), 'literal_route_history_prefix_required')
    chunks = [h.checked_bytes(ref) for ref in history['journals']]
    require(all(raw and raw.endswith(b'\n') for raw in chunks), 'complete_history_journals_required')
    require(exact(m.replay_history(b''.join(chunks[:len(parent['journals'])])),
                  parent['combined_route_history']), 'parent_route_history_changed')
    raw = b''.join(chunks)
    replay = m.replay_history(raw)
    require(exact(replay, history['combined_route_history']) and history['automatic_reset'] is False
            and exact(history['failures'], replay['nonzero_failures'])
            and exact(history['quarantines'], replay['quarantines']), 'route_history_changed')
    threshold = config.get('dispatcher', {}).get('route_failure_threshold', 3)
    require(positive_int(threshold) and not replay['unconsumed_tail_bytes']
            and not replay['unresolved_events'] and not replay['quarantines']
            and all(0 < count < threshold for count in replay['nonzero_failures'].values()),
            'unresolved_or_quarantined_history')
    require(all(isinstance(route, str) and route.startswith(('model:', 'task:', 'recorder:'))
                for route in replay['nonzero_failures']), 'unrecognized_history_route')
    claims, terminals = {}, set()
    for line in raw.splitlines():
        row = json.loads(line)
        if row['event'] == 'claimed':
            require(row['run_id'] not in claims, 'duplicate_history_claim')
            claims[row['run_id']] = row
        elif row['event'] == 'episode_finished':
            require(row['run_id'] in claims and row['run_id'] not in terminals,
                    'invalid_history_terminal')
            terminals.add(row['run_id'])
    expected = {d.validate_job(job): job for job in host_jobs[:p['ordinal_range'][0]]}
    all_ids = {d.validate_job(job) for job in global_jobs}
    require(set(claims) & all_ids == set(expected), 'claimed_assignment_or_prefix_gap')
    require(set(expected) <= terminals, 'unfinished_predecessor_assignment')
    for run_id, job in expected.items():
        require(claims[run_id]['job_sha256'] == d.sha(job)
                and claims[run_id]['config_sha256'] == d.sha(config), 'predecessor_claim_identity_changed')
    return history


def validate_resources(p, jobs, actual=None):
    """Bound both routes together using unchanged sandbox caps and fresh capacity."""
    binding = p['resource_binding']
    reserved = binding['workers_reserved']
    require(type(reserved) is dict and reserved and all(isinstance(k, str) and positive_int(v)
            for k, v in reserved.items()) and positive_int(p['workers'])
            and reserved.get(p['role']) == p['workers'], 'joint_worker_reservation_required')
    keys = ('cpus_per_worker', 'memory_bytes_per_worker', 'cpu_headroom', 'memory_headroom_bytes')
    require(all(positive_int(binding[k]) for k in keys), 'positive_resource_budget_required')
    for job in jobs:
        spec = job['spec']
        require(type(spec['cpus']) in (int, float) and 0 < spec['cpus'] <= binding['cpus_per_worker']
                and positive_int(spec['memory_bytes'])
                and spec['memory_bytes'] <= binding['memory_bytes_per_worker'], 'sandbox_resource_budget_exceeded')
    if actual is not None:
        cpus = actual['cpu_affinity']
        require(type(cpus) is list and cpus and all(type(v) is int and v >= 0 for v in cpus)
                and len(cpus) == len(set(cpus)) and positive_int(actual['memory_available_bytes']),
                'current_resource_observation_required')
        workers = sum(reserved.values())
        require(len(cpus) >= workers * binding['cpus_per_worker'] + binding['cpu_headroom']
                and actual['memory_available_bytes'] >= workers * binding['memory_bytes_per_worker']
                    + binding['memory_headroom_bytes'], 'joint_resource_budget_exceeded')


def load(reference, review, adoption_reference):
    p = h.checked(reference)
    peer(review, 'accepted_ordinal_actor_plan', plan=reference, source=p['source'])
    require(p['schema'] == 'causes.ordinal_actor_plan.v1'
            and p['runtime_authorized'] is True and p['automatic_retry'] is False
            and p['automatic_resume'] is False
            and socket.gethostname() == p['execution_hostname'], 'exact_host_scope')
    require(h.pin(__file__) == p['source'], 'source_changed')
    modules = {'campaign_dispatcher': d, 'campaign_handoff': h, 'campaign_monitor': m,
               'campaign_storage': s, 'campaign_storage_runtime': runtime,
               'campaign_runner': runner, 'campaign_sandbox': sandbox,
               'campaign_storage_observation': observation, 'campaign_telemetry': telemetry}
    require(set(p['native_sources']) == set(modules), 'exact_native_package_required')
    for name, module in modules.items():
        require(h.pin(module.__file__) == p['native_sources'][name], 'native_module_changed:' + name)
    for ref in p['native_sources'].values():
        h.checked_bytes(ref)
    adoption = peer(adoption_reference, 'accepted_ordinal_actor_runtime_adoption',
                    plan=reference, source=p['source'], plan_review=review)
    require(adoption['no_replay'] is True and adoption['no_extra_draws'] is True,
            'fixed_attempt_adoption_required')
    global_jobs = h.checked(p['global_jobs'])
    host_jobs = h.checked(p['host_jobs'])
    jobs = h.checked(p['jobs'])
    ordinals = h.checked(p['host_ordinals'])
    validate_ordinals(p, global_jobs, host_jobs, jobs, ordinals)
    config = h.checked(p['config'])
    require(p['config'] == p['parent_config'], 'original_actor_config_required')
    holds = d.AdmissionHolds(p['holds']['path'], p['holds']['sha256'])
    require(not any(holds.matches(j) for j in host_jobs), 'held_assignment')
    reservation = peer(p['collision_review'], 'accepted_requested_actor_named_history_collision_census',
                       jobs=p['global_jobs'])
    require(reservation['run_id_collisions'] == [] and reservation['seed_collisions'] == [],
            'prior_allocation_collision')
    history = validate_history(p, global_jobs, host_jobs, config)
    peer(p['qualification_review'], 'accepted_ordinal_actor_qualification',
         global_jobs=p['global_jobs'], host_jobs=p['host_jobs'], jobs=p['jobs'],
         ordinal_range=p['ordinal_range'], config=p['config'], workers=p['workers'],
         route_carry=p['route_carry'], predecessor_closures=p['predecessor_closures'],
         role=p['role'], execution_host=p['execution_host'], provider_host=p['provider_host'],
         provider_process=p['provider_process'], local_endpoint_process=p['local_endpoint_process'],
         native_sources=p['native_sources'])
    require(p['predecessor_closures'], 'accepted_predecessor_closure_required')
    for ref in p['predecessor_closures']:
        h.checked_bytes(ref)
    validate_resources(p, jobs)
    storage = p['storage_binding']
    root = h.canonical_path(p['output'])
    require(storage['output'] == str(root) and storage['floor_bytes'] == 80 * 1024**3
            and storage['root_floor_bytes'] == 20 * 1024**3
            and config['dispatcher']['disk_floor_bytes'] == storage['floor_bytes'], 'qualified_storage_floors')
    require(root.parent == h.canonical_path(storage['mount_target'])
            and p['max_full'] == 0 and type(p['max_full']) is int
            and p['extension_prototypes'] == [],
            'finite_qualification_only')
    options = dict(workers=p['workers'], max_full=0, pause_file=p['pause_file'],
        global_pause_file=p['global_pause_file'], start_barrier_file=p['barrier_path'],
        admission_holds=p['holds']['path'], admission_holds_sha256=p['holds']['sha256'],
        extension_prototypes=[])
    require(set([p['pause_file'], p['global_pause_file']]) <= set(p['pause_paths'])
            and len(set(p['pause_paths'])) == len(p['pause_paths']), 'control_scope')
    p.update(_reference=reference, _review=review, _adoption=adoption_reference,
             _history=history, _options=options, _jobs=jobs)
    return p, jobs, config


def carry_dispatcher_class(p):
    class CarryDispatcher(d.Dispatcher):
        def load_history(self):
            super().load_history()
            require(not self.claimed and not self.done and not self.failures and not self.quarantines
                    and not (self.meta / 'journal.jsonl').exists(), 'fresh_owner_route_state_required')
            history = p['_history']
            h.save_new(self.meta, 'PREDECESSOR_ROUTE_STATE.json', dict(at=h.now(), plan=p['_reference'],
                source=p['source'], route_carry=p['route_carry'], route_carry_review=p['route_carry_review'],
                retained_route_history=history['combined_route_history'], failures=history['failures'],
                quarantines=history['quarantines'], automatic_reset=False))
            self.failures = copy.deepcopy(history['failures'])
            self.quarantines = copy.deepcopy(history['quarantines'])

        def admission_paused(self):
            return super().admission_paused() or any(os.path.lexists(v) for v in p['pause_paths'])
    return CarryDispatcher


def authority(p, reference):
    def validate(value):
        require(value['plan'] == p['_reference'] and value['plan_review'] == p['_review']
                and value['source'] == p['source'] and value['root_adoption'] == p['_adoption'],
                'authority_binding_changed')
        actual = h.checked(value['admission'])
        peer(value['admission_review'], 'accepted_ordinal_actor_current_admission',
             actual=value['admission'], plan=p['_reference'], source=p['source'])
        elapsed = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(actual['finished_at'])).total_seconds()
        require(0 <= elapsed <= 180 and actual['errors'] == [] and actual['provider_idle'] is True
                and actual['all_prior_actor_owners_retired'] is True
                and actual['supplementary_active_count'] == 0
                and actual['supplementary_unstarted_preserved'] is True
                and actual['resource_workers_reserved'] == p['resource_binding']['workers_reserved']
                and actual['model_calls'] == 0, 'fresh_idle_and_resource_admission_required')
        require(actual['execution_host'] == p['execution_host']
                and actual['provider_host'] == p['provider_host']
                and actual['provider_process'] == p['provider_process']
                and actual['local_endpoint_process'] == p['local_endpoint_process'], 'provider_identity_join')
        validate_resources(p, p['_jobs'], actual)
        h.process_identity(p['local_endpoint_process'])
        return True
    return h.Authority(reference, runtime.START,
                       tuple(h.canonical_path(v) for v in p['pause_paths']), validate)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=json.loads, required=True)
    parser.add_argument('--authority', type=json.loads, required=True)
    args = parser.parse_args()
    request = h.checked(args.request)
    require(request['action'] == runtime.START, 'only_finite_held_start')
    p, jobs, config = load(request['plan'], request['plan_review'], request['root_adoption'])
    require(request['source'] == p['source'], 'request_source_changed')
    cls = carry_dispatcher_class(p)
    result = runtime.run_recorded_dispatcher(jobs=jobs, config=config, output=p['output'],
        storage_binding=p['storage_binding'], record_directory=p['record_directory'],
        owned_barrier=request['owned_barrier'], options=p['_options'],
        authority=authority(p, args.authority), dispatcher_class=cls, dispatcher_source=p['source'])
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
