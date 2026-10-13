"""Authorize a finite reader-input copy using exact frozen actor allocations."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import types


def require(value, message):
    if not value:
        raise ValueError(message)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def pin(path):
    path = Path(path)
    data = path.read_bytes()
    return dict(path=str(path), bytes=len(data), sha256=hashlib.sha256(data).hexdigest())


def checked(reference):
    require(pin(reference['path']) == reference, 'changed_reference')
    return json.loads(Path(reference['path']).read_bytes())


def check_local_budget(roots, limit):
    """Count both retained payloads and control files before another transfer."""
    require(type(limit) is int and 2 * 1024**3 < limit <= 45_000_000_000, 'invalid_copy_budget')
    total = 0
    pending = [Path(root) for root in roots]
    while pending:
        path = pending.pop()
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode), 'copy_budget_symlink')
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                pending.extend(Path(entry.path) for entry in entries)
        else:
            require(stat.S_ISREG(info.st_mode), 'copy_budget_nonregular')
            total += info.st_size
    # Native reader passes retain at most 256 MiB of payload, plus their
    # manifests and finite selection metadata. Keep headroom for that pass.
    require(total + 2 * 1024**3 <= limit, 'local_copy_budget_hold')
    return total


def validate_registration(registration, read=checked):
    """Verify the selected physical runs and their actual producer plan bindings."""
    r = registration
    require(r['schema'] == 'causes.selected_actor_reader_feed_registration.v1', 'registration_schema')
    selection, peer, config = (read(r[k]) for k in ('selection', 'selection_review', 'configuration'))
    require(peer.get('accepted_requested_actor_reader_selection_preparation') is True
            and not peer['findings'] and peer['selection'] == r['selection']
            and peer['configuration'] == r['configuration']
            and peer['global_jobs'] == selection['global_jobs']
            and peer['assignments'] == selection['assignments'], 'selection_review_join')
    require(selection['stage'] == 'early' and selection['reader_instruments'] ==
            ['presence', 'strongly_favors', 'leans_towards_or_favors'], 'reader_scope')
    jobs = read(selection['global_jobs'])
    ids = [j['allocation']['run_id'] for j in jobs]
    require(len(ids) == len(set(ids)) == len(selection['rows']) == selection['contexts'], 'global_membership')
    stages = {(s['stage'], s['role']): s for s in selection['stages']}
    producers = {(p['stage'], p['role']): p for p in r['producers']}
    require(len(stages) == len(selection['stages']) == len(producers) == len(r['producers'])
            and set(stages) == set(producers), 'producer_partition')
    hosts = {}
    for role, refs in selection['host_partitions'].items():
        host_jobs, ordinals = read(refs['jobs']), read(refs['ordinals'])
        require(all(type(x) is int and 0 <= x < len(jobs) for x in ordinals)
                and len(ordinals) == len(set(ordinals)) == len(host_jobs)
                and encoded(host_jobs) == encoded([jobs[x] for x in ordinals]), 'host_membership')
        hosts[role] = (host_jobs, ordinals)
    require(sorted(x for _, ordinals in hosts.values() for x in ordinals) == list(range(len(jobs))), 'host_partition_union')
    stage_jobs = {}
    for key, stage in stages.items():
        binding = producers[key]
        plan, review = read(binding['plan']), read(binding['review'])
        flag = 'accepted_requested_fixed8_actor_plan' if stage['stage'] == 'first8' else 'accepted_ordinal_actor_plan'
        require(stage['stage'] in {'first8', 'next64', 'remaining'} and review.get(flag) is True
                and not review['findings'] and review['plan'] == binding['plan']
                and review['source'] == plan['source'], 'producer_plan_review')
        require(plan['runtime_authorized'] is True and plan['role'] == stage['role']
                and plan['execution_host'] == config['host'] == binding['execution_host']
                and plan['output'] == stage['output_root'] == binding['output_root']
                and plan['jobs'] == binding['jobs'] and plan['config'] == binding['config']
                and plan['global_jobs'] == selection['global_jobs']
                and plan['host_jobs'] == selection['host_partitions'][stage['role']]['jobs']
                and plan['host_ordinals'] == selection['host_partitions'][stage['role']]['ordinals'], 'producer_identity')
        read(plan['config'])
        start, stop = stage['host_ordinal_range']
        host_jobs, _ = hosts[stage['role']]
        require(type(start) is int and type(stop) is int and 0 <= start < stop <= len(host_jobs)
                and stop - start == stage['contexts'], 'stage_range')
        actual_jobs = read(plan['jobs'])
        require(encoded(actual_jobs) == encoded(host_jobs[start:stop]), 'stage_jobs')
        require(plan['max_full'] == 0 and plan.get('prefix_policy') is None and plan['extension_prototypes'] == []
                and plan['workers'] == (4 if stage['stage'] == 'first8' else 8), 'producer_protocol')
        if stage['stage'] == 'first8':
            require(start == 0, 'first_stage_prefix')
        else:
            require(plan['ordinal_range'] == [start, stop], 'producer_ordinal_range')
        require(str(Path(plan['output']).relative_to(config['remote_base'])) == stage['relative_root'], 'producer_root')
        stage_jobs[key] = {j['allocation']['run_id'] for j in actual_jobs}
    seen = set()
    for ordinal, (row, job) in enumerate(zip(selection['rows'], jobs)):
        allocation = job['allocation'];key = (row['stage'], row['provider_role'])
        require(type(row['global_ordinal']) is int and row['global_ordinal'] == ordinal
                and row['run_id'] == allocation['run_id'] and row['run_id'] not in seen
                and row['job_sha256'] == hashlib.sha256(encoded(job)).hexdigest(), 'selected_job_identity')
        seen.add(row['run_id'])
        host_jobs, ordinals = hosts[row['provider_role']]
        pos = row['host_ordinal'];require(type(pos) is int and 0 <= pos < len(host_jobs)
                and ordinals[pos] == ordinal and encoded(host_jobs[pos]) == encoded(job), 'selected_host_ordinal')
        stage = stages[key]
        require(row['run_id'] in stage_jobs[key] and stage['host_ordinal_range'][0] <= pos < stage['host_ordinal_range'][1]
                and row['execution_host'] == config['host']
                and row['relative_run'] == stage['relative_root'] + '/' + row['run_id'], 'selected_stage')
        require(allocation['continue_to_end'] is False and allocation['instruments'] ==
                ['evaluation_presence', 'evaluation_strongly_favors', 'evaluation_leans_towards_or_favors']
                and allocation['reader_stages'] == ['early'] and allocation['reader_thresholds'] == selection['reader_instruments'], 'selected_protocol')
    require(selection['selected_runs'] == [row['relative_run'] for row in selection['rows']]
            and config == dict(host=r['execution_host'], remote_base=r['remote_base'],
                roots=[s['relative_root'] for s in selection['stages']], destination=r['destination'],
                free_space_floor_bytes=80*1024**3, limit=8, selected_runs=selection['selected_runs'], reader_inputs_only=True), 'copy_configuration')
    require(r['self_probe_custody'] == dict(originals_retained_on_producer=True, local_copy_included=False), 'self_custody_scope')
    return config


def load(plan_ref, review_ref, adoption_ref):
    captured = {}
    def read(reference):
        value = checked(reference)
        previous = captured.setdefault(reference['path'], dict(reference))
        require(previous == reference, 'conflicting_reference')
        return value
    p, review, adoption = map(read, (plan_ref, review_ref, adoption_ref))
    require(p['schema'] == 'causes.selected_actor_reader_feed_plan.v1'
            and p['runtime_authorized'] is True and p['producer_plan_binding_complete'] is True,
            'pending_or_untyped_copy_plan')
    require(type(p['maximum_local_copy_bytes']) is int
            and 2 * 1024**3 < p['maximum_local_copy_bytes'] <= 45_000_000_000,
            'invalid_copy_budget')
    require(p['source'] == pin(__file__) and review.get('accepted_selected_actor_reader_feed_preparation') is True
            and review['plan'] == plan_ref and review['source'] == p['source'] and not review['findings'], 'feed_source_review')
    require(adoption['root_authorized'] is True and adoption['no_user_revocation'] is True
            and adoption['plan'] == plan_ref and adoption['review'] == review_ref
            and adoption['maximum_invocations'] == 1 and adoption['automatic_retry'] is False, 'copy_authority')
    registration = read(p['registration']);native = validate_registration(registration, read)
    require(p['configuration'] == registration['configuration'] and p['remote_base'] == native['remote_base']
            and p['destination'] == native['destination'], 'feed_configuration_join')
    parent_ref = p['transport_parent'];require(pin(parent_ref['path']) == parent_ref, 'transport_parent_changed')
    spec = importlib.util.spec_from_file_location('selected_reader_feed_native_parent', parent_ref['path'])
    parent = importlib.util.module_from_spec(spec);sys.modules[spec.name] = parent;spec.loader.exec_module(parent)
    h, snapshot = parent.h, parent.snapshot
    for ref in p['sources'].values():h.checked_bytes(ref)
    require(p['sources']['handoff'] == h.pin(h.__file__) and p['sources']['snapshot'] == h.pin(snapshot.__file__), 'native_source_identity')
    spec = importlib.util.spec_from_file_location('selected_reader_feed_native_watch', p['sources']['watch']['path'])
    watch = importlib.util.module_from_spec(spec);spec.loader.exec_module(watch)
    require(watch.snapshot is snapshot and p['interval_seconds'] == 180, 'native_watch_binding')
    config = snapshot.SnapshotConfig(**dict(native, destination=Path(native['destination']), roots=tuple(native['roots']), selected_runs=tuple(native['selected_runs'])))
    config.validate()
    for ref in [p['source'], parent_ref, p['ssh_config'], *p['sources'].values()]:
        previous = captured.setdefault(ref['path'], dict(ref))
        require(previous == ref, 'conflicting_reference')
    refs = list(captured.values())
    def authorize(actual):
        require(actual == config, 'native_configuration_changed')
        check_local_budget([config.destination, p['watch_directory']], p['maximum_local_copy_bytes'])
        for ref in refs:h.checked_bytes(ref)
        return dict(host=config.host, remote_base=config.remote_base, roots=list(config.roots), selected_runs=list(config.selected_runs), evidence=refs,
                    custody_kind='actor_reader_inputs', scope='Finite selected actor identity and reader contexts only; original self-probe evidence stays on the producer. No actor, model or reader action is authorized.')
    snapshot.checked_authorization(config, authorize)
    return p, config, watch, authorize, parent


def main(plan_ref, review_ref, adoption_ref):
    p, config, watch, authorize, parent = load(plan_ref, review_ref, adoption_ref)
    require(not config.destination.exists() and not Path(p['watch_directory']).exists(), 'fresh_snapshot_namespace_required')
    snapshot = parent.snapshot;native_subprocess = snapshot.subprocess
    def run(argv, **kwargs):
        parent.h.checked_bytes(p['ssh_config'])
        command = parent.strict_transport(argv, p)
        check_local_budget([config.destination, p['watch_directory']], p['maximum_local_copy_bytes'])
        return subprocess.run(command, **kwargs)
    snapshot.subprocess = types.SimpleNamespace(run=run)
    try:
        result = watch.watch_completed(config, authorize, Path(p['watch_directory']), interval_seconds=p['interval_seconds'])
        print(json.dumps(result, sort_keys=True))
    finally:
        snapshot.subprocess = native_subprocess


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('plan', 'review', 'adoption'):
        parser.add_argument('--'+name, required=True, type=json.loads)
    args = parser.parse_args();main(args.plan, args.review, args.adoption)
