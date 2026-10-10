"""Partition a frozen intervention queue without splitting paired prompt arms."""

import copy
import json


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def partition_prompt_queue(jobs, system_prompts, host_by_pair, *, expected_arm_ids):
    """Apply an explicit pair-to-host assignment, preserving jobs and queue order.

    This function assigns no capacity, changes no configuration, and performs no
    I/O. Callers must qualify the hosts and freeze the assignment before running.
    """
    if (not isinstance(expected_arm_ids, (list, tuple)) or not expected_arm_ids
            or any(not isinstance(arm, str) or not arm for arm in expected_arm_ids)
            or len(set(expected_arm_ids)) != len(expected_arm_ids)):
        raise ValueError('invalid_expected_arms')
    expected = set(expected_arm_ids)
    if not isinstance(jobs, list) or not jobs:
        raise ValueError('jobs_required')
    if not isinstance(system_prompts, dict) or not isinstance(host_by_pair, dict):
        raise ValueError('mappings_required')
    if any(not isinstance(pair, str) or not pair or not isinstance(host, str) or not host
           for pair, host in host_by_pair.items()):
        raise ValueError('invalid_pair_host_mapping')

    pairs, arm_definitions, seen_runs, identities = {}, {}, set(), []
    namespace = None
    for job in jobs:
        if not isinstance(job, dict) or not isinstance(job.get('allocation'), dict):
            raise ValueError('invalid_job')
        allocation = job['allocation']
        run_id = allocation.get('run_id')
        intervention = allocation.get('system_prompt_intervention')
        if not isinstance(run_id, str) or not run_id or run_id in seen_runs:
            raise ValueError('duplicate_or_invalid_run')
        seen_runs.add(run_id)
        if not isinstance(intervention, dict) or not isinstance(intervention.get('arm'), dict):
            raise ValueError('invalid_intervention')
        pair_id, arm = intervention.get('pair_id'), intervention['arm']
        arm_id, this_namespace = arm.get('id'), intervention.get('namespace')
        if (not isinstance(pair_id, str) or not pair_id
                or not isinstance(arm_id, str) or arm_id not in expected
                or not isinstance(this_namespace, str) or not this_namespace):
            raise ValueError('invalid_pair_or_arm')
        if namespace is None:
            namespace = this_namespace
        elif namespace != this_namespace:
            raise ValueError('mixed_namespaces')
        if type(allocation.get('continue_to_end')) is not bool:
            raise ValueError('invalid_full_flag')
        if not isinstance(allocation.get('paired_baseline'), dict):
            raise ValueError('missing_paired_baseline')
        if not isinstance(system_prompts.get(run_id), str) or not system_prompts[run_id]:
            raise ValueError('missing_or_invalid_system_prompt')
        arm_identity = _canonical(arm)
        if arm_id in arm_definitions and arm_definitions[arm_id] != arm_identity:
            raise ValueError('inconsistent_arm_definition')
        arm_definitions[arm_id] = arm_identity

        # Every other job and intervention field is a paired invariant.
        invariant = copy.deepcopy(job)
        del invariant['allocation']['run_id']
        del invariant['allocation']['system_prompt_intervention']['arm']
        invariant = _canonical(invariant)
        group = pairs.setdefault(pair_id, {'invariant': invariant, 'arms': set()})
        if group['invariant'] != invariant:
            raise ValueError('paired_job_mismatch')
        if arm_id in group['arms']:
            raise ValueError('duplicate_pair_arm')
        group['arms'].add(arm_id)
        identities.append((pair_id, run_id))

    if set(system_prompts) != seen_runs:
        raise ValueError('system_prompt_membership_mismatch')
    if set(host_by_pair) != set(pairs):
        raise ValueError('pair_host_membership_mismatch')
    if any(group['arms'] != expected for group in pairs.values()):
        raise ValueError('incomplete_pair')

    partitions, placed_pairs = {}, set()
    for job, (pair_id, run_id) in zip(jobs, identities):
        host = host_by_pair[pair_id]
        shard = partitions.setdefault(host, {'jobs': [], 'system_prompts': {}, 'pair_ids': []})
        shard['jobs'].append(copy.deepcopy(job))
        shard['system_prompts'][run_id] = system_prompts[run_id]
        if pair_id not in placed_pairs:
            shard['pair_ids'].append(pair_id)
            placed_pairs.add(pair_id)
    return partitions
