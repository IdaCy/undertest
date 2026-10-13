"""One-time continuous-owner admission using explicit reviewed policy and evidence.

This module does not collect remote evidence, qualify a model, compose an
allocation block, stage files, or publish a registration. Those inputs must
already be pinned and accepted. Failed actions retain their partial receipts
and require reconciliation; there is no recovery or retry entry point.
"""

import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

from causes import campaign_handoff as h
from causes.campaign_census import identity
from causes.campaign_monitor import history_blockers, history_relevance, listener_owned, replay_history, validate_registration
from causes.corpus_continuation import CorpusContinuation


START = 'start_continuous_owner_behind_barrier'
RELEASE = 'release_continuous_owned_barrier'
STATIC_GATES = ('runtime', 'host_transfer', 'native_host', 'images', 'source_publication',
                'capacity_inheritance', 'resource_decision')


def require(value, message):
    if not value:
        raise ValueError(message)


def recent(value, seconds=180):
    observed = dt.datetime.fromisoformat(value)
    require(observed.tzinfo is not None
            and 0 <= (dt.datetime.now(dt.timezone.utc)-observed).total_seconds() <= seconds,
            'admission_observation_stale')


def object_sha(value):
    return hashlib.sha256(h.encoded(value)).hexdigest()


def loaded_sources_match(expected):
    for name,wanted in expected.items():
        # ``python -m`` executes this file as __main__, without its qualified
        # sys.modules alias. The other imported modules retain their names.
        source=__file__ if name=='causes.campaign_admission' else sys.modules[name].__file__
        require(hashlib.sha256(Path(source).read_bytes()).hexdigest()==wanted,
                'loaded_operator_module_changed:'+name)


def accepted(pair, flag, key):
    value, review = h.checked(pair['evidence']), h.checked(pair['review'])
    require(review.get(flag) is True and review.get(key) == pair['evidence'], 'unaccepted_admission_input:'+flag)
    return value


def reviewed_policy(policy, template, review):
    require(template['schema']=='causes.continuous_admission_policy_template.v1', 'policy_template_schema')
    require(review.get('accepted_continuous_admission_source') is True
            and review.get('accepted_continuous_admission_policy_template') is True
            and review.get('policy_template')==policy['policy_template'], 'unaccepted_admission_policy_template')
    fixed, dynamic = template['fixed'], template['dynamic_keys']
    require(set(policy)==set(fixed)|set(dynamic)|{'policy_template'}
            and len(set(dynamic))==len(dynamic) and not set(dynamic)&set(fixed), 'policy_key_scope_changed')
    require(all(h.encoded(policy[k])==h.encoded(value) for k,value in fixed.items()), 'reviewed_policy_changed')
    require(review['source']['sha256']==policy['source_sha256'], 'unaccepted_admission_source')


def bind_policy(template_reference, control_reference, controller_template_reference, donor_histories):
    """Return policy data from already sealed inputs; create no files or blocks."""
    template=h.checked(template_reference)
    p=json.loads(h.encoded(template['fixed']))
    control=h.checked(control_reference);config=h.checked(control['config']);prototypes=h.checked(control['prototypes'])
    allocator=CorpusContinuation.from_control(control,prototypes,config)
    t=h.checked(controller_template_reference)['templates']
    require(len(t)==1, 'one_normalized_template_required');t=t[0]
    bundle=h.checked(t['bundle']);code={r['name']:r['sha256'] for r in bundle['files']}
    execution={'dispatcher_sha256':code['campaign_dispatcher.py']}
    execution.update({k+'_sha256':code[k] for k in ('campaign_runner.py','campaign_sandbox.py','corpus_allocation.py','corpus_continuation.py')})
    continuation={'control':control_reference,'binding':allocator.binding,'package':p['allocator_package'],
                  'source_review':p['allocator_review'],'allocator_sources':{k:code[k] for k in
                  ('campaign_dispatcher.py','corpus_allocation.py','corpus_continuation.py')}}
    queue={'jobs_sha256':object_sha([]),'config_sha256':object_sha(config),'prototypes_sha256':object_sha(prototypes),
           'execution_code':execution,'corpus_continuation':{'control':control_reference,'binding':allocator.binding}}
    controller={**queue,**execution,'workers':p['workers'],'max_full':0,'pause_file':p['dedicated_pause'],
                'global_pause_file':p['global_pause'],'start_barrier_file':p['barrier'],
                'disk_floor_bytes':config.get('dispatcher',{}).get('disk_floor_bytes',80*1024**3),
                'route_failure_threshold':config.get('dispatcher',{}).get('route_failure_threshold',3),
                'route_recovery':'explicit_qualification_receipt_required'}
    p.update(policy_template=template_reference,controller_template=controller_template_reference,normalized_template=t,
             argv=t['argv_template_at_proposed_cap'][2:],continuation=continuation,expected_queue=queue,
             expected_controller_fields=controller,donor_histories=json.loads(h.encoded(donor_histories)),
             runtime_input_pins=[t[k] for k in ('jobs','config','prototypes','bundle')]+[p['holds'],controller_template_reference,
                               control_reference]+[control[k] for k in ('catalog','original_jobs','reservation')])
    require(set(p)==set(template['fixed'])|set(template['dynamic_keys'])|{'policy_template'}, 'policy_binding_field_mismatch')
    return p


def _reservation_equal(original, current):
    require(original['schema'] == current['schema'] == 'causes.corpus_allocation_reservation.v1',
            'reservation_schema_changed')
    def rows(value):
        result = {}
        for raw in value['allocation_identities']:
            row = identity(raw)
            require(row['run_id'] not in result, 'duplicate_reservation_identity')
            result[row['run_id']] = row
        require(sorted(result) == value['run_ids'], 'reservation_ID_union_changed')
        seeds = sorted({r[k] for r in result.values() for k in ('seed','block_seed','task_seed','model_seed')
                        if type(r[k]) is int and r[k] > 0})
        require(seeds == value['allocation_seeds'], 'reservation_seed_union_changed')
        return result
    require(h.encoded(rows(original)) == h.encoded(rows(current)), 'reservation_changed_requires_new_review')
    require(original['frozen_input_pins'] == current['frozen_input_pins'], 'frozen_census_scope_changed')
    return True


def _reservation_recent(current):
    for cut in current['host_cuts']:
        recent(cut['started_at']); recent(cut['finished_at'])


def reservation_unchanged(original, current):
    _reservation_equal(original, current)
    _reservation_recent(current)
    return True


def native_reservation_augmentation(original, current, allocation, *, allocation_cache=None):
    """Allow only a regenerated native owner's exact initial JOBS addition.

    The allocator keeps its precreation reservation. The fresh census reserves
    the entire selected queue, including unclaimed jobs, or the explicit empty
    JOBS file for a continuous owner. This does not accept foreign growth.
    """
    from causes.campaign_corpus_succession import native_allocation
    control = h.checked(allocation['allocation_control'])
    require(h.encoded(h.checked(control['reservation'])) == h.encoded(original),
            'native_generation_reservation_changed')
    jobs, _, _, _ = native_allocation(allocation, cache=allocation_cache)
    require(original['schema'] == 'causes.corpus_allocation_reservation.v1', 'reservation_schema_changed')
    old = {}
    for raw in original['allocation_identities']:
        row = identity(raw)
        require(row['run_id'] not in old, 'duplicate_reservation_identity')
        old[row['run_id']] = row
    require(sorted(old) == original['run_ids'], 'reservation_ID_union_changed')
    seed_keys = ('seed', 'block_seed', 'task_seed', 'model_seed')
    seeds = lambda rows: sorted({row[key] for row in rows for key in seed_keys
                                 if type(row[key]) is int and row[key] > 0})
    require(h.encoded(seeds(old.values())) == h.encoded(original['allocation_seeds']),
            'reservation_seed_union_changed')
    own = [identity(job['allocation']) for job in jobs]
    own_ids = [row['run_id'] for row in own]
    require(len(set(own_ids)) == len(own_ids) and not set(own_ids) & set(old)
            and not set(seeds(own)) & set(original['allocation_seeds']), 'native_own_allocation_collision')
    require(all(ref['path'] != allocation['jobs']['path'] for ref in original['frozen_input_pins']),
            'native_initial_jobs_already_reserved')
    hosts = [cut['host'] for cut in original['host_cuts']]
    require(hosts and len(set(hosts)) == len(hosts)
            and hosts == [cut['host'] for cut in current['host_cuts']], 'native_reservation_hosts_changed')
    require(h.encoded(original['persisted_full_blocks']) == h.encoded(current['persisted_full_blocks']),
            'native_reservation_blocks_changed')
    rows = [*old.values(), *own]
    expected = {**original, 'allocation_identities': rows,
                'run_ids': sorted([*old, *own_ids]), 'allocation_seeds': seeds(rows),
                'frozen_input_pins': [*original['frozen_input_pins'], allocation['jobs']]}
    require(h.encoded(expected['allocation_seeds']) == h.encoded(current['allocation_seeds']),
            'reservation_seed_union_changed')
    return reservation_unchanged(expected, current)


class NativeReservationProofCache:
    """Reuse one immutable foreign-block proof, never its time validity.

    Every hit checks the full input value and every transitive file's bytes.
    Only canonical readers are supported. A changed scope or failed dependency
    recheck permanently invalidates this instance; it never refreshes evidence.
    The caller must still check independent acceptance and current freshness.
    """

    def __init__(self):
        self._scope = self._readers = self._loaded_source = None
        self._references = ()
        self._invalid = False

    def verify(self, original, current, allocation, growth, config_reference, *,
               waiting=False, allocation_cache=None, reader=h.checked, bytes_reader=h.checked_bytes):
        from causes import campaign_census as census
        require(not self._invalid, 'native_reservation_cache_invalid')
        if self._scope is not None:
            try:
                scope = object_sha([original, current, allocation, growth, config_reference, waiting])
                loaded_source = str(h.canonical_path(census.__file__))
                require(reader is h.checked and bytes_reader is h.checked_bytes
                        and (reader, bytes_reader) == self._readers, 'native_reservation_cache_reader_changed')
                require(scope == self._scope and loaded_source == self._loaded_source,
                        'native_reservation_cache_scope_changed')
                for ref in self._references:
                    bytes_reader(ref)
            except BaseException:
                self._invalid = True
                raise
            return True

        require(reader is h.checked and bytes_reader is h.checked_bytes,
                'native_reservation_cache_reader_changed')
        scope = object_sha([original, current, allocation, growth, config_reference, waiting])
        loaded_source = str(h.canonical_path(census.__file__))
        references = {}

        def record(ref):
            saved = copy.deepcopy(ref)
            require(references.setdefault(saved['path'], saved) == saved,
                    'native_reservation_cache_conflicting_pin')
            return saved

        def read(ref):
            return reader(record(ref))

        def read_bytes(ref):
            return bytes_reader(record(ref))

        # The nested allocation cache exports its checked transitive references.
        # A private cache on a direct call keeps that same exact dependency path.
        from causes.campaign_corpus_succession import NativeAllocationCache
        allocation_cache = allocation_cache if allocation_cache is not None else NativeAllocationCache()
        _native_reservation_foreign_blocks_proof(original, current, allocation, growth, config_reference,
            waiting=waiting, allocation_cache=allocation_cache, reader=read, bytes_reader=read_bytes,
            dependency_sink=record)
        try:
            require(object_sha([original, current, allocation, growth, config_reference, waiting]) == scope
                    and str(h.canonical_path(census.__file__)) == loaded_source,
                    'native_reservation_cache_scope_changed')
            for ref in references.values():
                bytes_reader(ref)
        except BaseException:
            self._invalid = True
            raise
        self._scope, self._readers, self._loaded_source = scope, (reader, bytes_reader), loaded_source
        self._references = tuple(references.values())
        return True


def native_reservation_foreign_blocks(original, current, allocation, growth, config_reference, *,
                                      waiting=False, allocation_cache=None, proof_cache=None):
    """Validate the immutable relation and check dated host cuts on every call."""
    if proof_cache is None:
        _native_reservation_foreign_blocks_proof(original, current, allocation, growth, config_reference,
                                                waiting=waiting, allocation_cache=allocation_cache)
    else:
        require(isinstance(proof_cache, NativeReservationProofCache), 'native_reservation_cache_scope_changed')
        proof_cache.verify(original, current, allocation, growth, config_reference,
                           waiting=waiting, allocation_cache=allocation_cache)
    _reservation_recent(current)
    return True


def _native_reservation_foreign_blocks_proof(original, current, allocation, growth, config_reference, *,
                                             waiting=False, allocation_cache=None, reader=h.checked,
                                             bytes_reader=h.checked_bytes, dependency_sink=None):
    """Preserve a generated queue while reviewed foreign roots append full blocks.

    The original allocator reservation is never rebased. Pinned census metadata
    proves every added identity, including unclaimed jobs. Only the declared
    foreign roots may grow; this is an explicit alternative to strict equality.
    """
    from causes import campaign_census as census
    from causes.campaign_corpus_succession import native_allocation_components
    control = reader(allocation['allocation_control'])
    require(h.encoded(reader(control['reservation'])) == h.encoded(original),
            'native_generation_reservation_changed')
    jobs, _, _, _ = native_allocation_components(allocation, cache=allocation_cache, dependency_sink=dependency_sink)
    require(set(growth) == {'baseline_census_config', 'foreign_roots'}, 'foreign_growth_scope_changed')
    baseline_config, config = reader(growth['baseline_census_config']), reader(config_reference)
    require(original['configuration'] == growth['baseline_census_config'] and current['configuration'] == config_reference,
            'foreign_reservation_configuration_changed')
    loaded_census = h.pin(census.__file__)
    bytes_reader(loaded_census)
    for value in (original, current):
        source = value['composition_source']; bytes_reader(source)
        require(all(source[key] == loaded_census[key] for key in ('bytes', 'sha256')),
                'foreign_census_source_changed')
    hosts = [row['host'] for row in baseline_config['hosts']]
    require(hosts and len(set(hosts)) == len(hosts), 'native_reservation_hosts_changed')
    own_host = next((row for row in baseline_config['hosts'] if row['host'] == allocation['controller_host']), None)
    require(own_host is not None, 'native_owner_host_missing')
    root = Path(allocation['output_root'])
    require(root.is_absolute() and root.is_relative_to(Path(own_host['base'])), 'native_owner_root_changed')
    own_root = str(root.relative_to(own_host['base']))
    require(own_root not in own_host['roots'], 'native_initial_root_already_reserved')
    expected_config = json.loads(h.encoded(baseline_config))
    own_spec = dict(pin=allocation['jobs'], selector=[], kind='jobs', expected_count=len(jobs))
    if waiting:
        own_spec['allowed_locations'] = [[allocation['controller_host'], own_root]]
        next(row for row in expected_config['hosts'] if row['host'] == allocation['controller_host'])['roots'].append(own_root)
    expected_config['frozen_inputs'].append(own_spec)
    require(h.encoded(config) == h.encoded(expected_config), 'foreign_growth_census_config_changed')
    require(original['frozen_input_pins'] == [row['pin'] for row in baseline_config['frozen_inputs']]
            and all(row['pin']['path'] != allocation['jobs']['path'] for row in baseline_config['frozen_inputs']),
            'foreign_growth_frozen_inputs_changed')
    allowed = set()
    for row in growth['foreign_roots']:
        require(set(row) == {'host', 'root'}, 'foreign_growth_root_changed')
        host = next((item for item in baseline_config['hosts'] if item['host'] == row['host']), None)
        require(host is not None and row['root'] in host['roots']
                and (row['host'], row['root']) != (allocation['controller_host'], own_root), 'foreign_growth_root_changed')
        key = (row['host'], str(Path(host['base'])/row['root']))
        require(key not in allowed, 'foreign_growth_duplicate_root'); allowed.add(key)
    require(allowed, 'foreign_growth_roots_required')

    def capture_projection(value, configuration):
        require(len(value['capture_pins']) == len(configuration['hosts']), 'foreign_capture_inventory_changed')
        require(reader(value['capture_map']) == {host['host']: ref for host, ref in
                zip(configuration['hosts'], value['capture_pins'])}, 'foreign_capture_map_changed')
        captures = {}; blocks = {}
        for host, reference in zip(configuration['hosts'], value['capture_pins']):
            require(0 < reference['bytes'] <= configuration['max_capture_bytes'], 'foreign_capture_bound')
            captured = census.strict_json(bytes_reader(reference))
            require(captured['host'] == host['host'] and host['host'] not in captures, 'foreign_capture_host_changed')
            require(captured['configuration'] == value['configuration']
                    and captured['composition_source'] == captured['collector_source']
                    and all(captured['collector_source'][key] == value['composition_source'][key]
                            for key in ('bytes', 'sha256')), 'foreign_capture_source_or_config_changed')
            captures[host['host']] = dict(pin=reference, value=captured)
            for item in captured['files']:
                if item['kind'] != 'persisted_extension_block':
                    continue
                key = (host['host'], item['path'])
                require(key not in blocks, 'foreign_duplicate_block')
                blocks[key] = item
        # This is a capture-only projection, not a substitute census or an
        # acceptance of new frozen inputs. The original frozen union is retained
        # below, and the actual full census still has its independent review.
        selected = json.loads(h.encoded(configuration)); selected['frozen_inputs'] = []
        projection = census.compose_reservation(selected, captures, [])
        require(all(h.encoded(projection[key]) == h.encoded(value[key]) for key in
                    ('host_cuts', 'capture_pins', 'persisted_full_blocks')), 'foreign_capture_projection_changed')
        return projection, blocks

    old_projection, old_blocks = capture_projection(original, baseline_config)
    new_projection, new_blocks = capture_projection(current, config)
    require(all(key in new_blocks and h.encoded(new_blocks[key]) == h.encoded(row)
                for key, row in old_blocks.items()), 'foreign_prior_block_changed')

    def identities(rows):
        result = {}
        for raw in rows:
            row = identity(raw)
            require(row['run_id'] not in result, 'duplicate_reservation_identity')
            result[row['run_id']] = row
        return result

    seeds = lambda rows: sorted({row[key] for row in rows for key in ('seed', 'block_seed', 'task_seed', 'model_seed')
                                 if type(row[key]) is int and row[key] > 0})
    old = identities(original['allocation_identities'])
    require(sorted(old) == original['run_ids'] and h.encoded(seeds(old.values())) == h.encoded(original['allocation_seeds']),
            'foreign_original_union_changed')
    require(all(old.get(row['run_id']) == row for row in old_projection['allocation_identities']),
            'foreign_original_capture_changed')
    own = identities([job['allocation'] for job in jobs])
    require(not set(own) & set(old) and not set(seeds(own.values())) & set(seeds(old.values())),
            'native_own_allocation_collision')
    added = {}; added_blocks = {key: row for key, row in new_blocks.items() if key not in old_blocks}
    for key, row in added_blocks.items():
        scope = (key[0], str(Path(key[1]).parent.parent.parent))
        require(scope in allowed, 'foreign_unreviewed_growth_root')
        rows = identities(row['allocations'])
        require(rows and not (set(rows) & (set(old) | set(own) | set(added))), 'foreign_added_identity_collision')
        added.update(rows)
    for host, output_root in allowed:
        def sequence(blocks):
            result = {}
            for (owner, path), item in blocks.items():
                if owner != host or str(Path(path).parent.parent.parent) != output_root:
                    continue
                stem = Path(path).stem
                require(stem and all('0' <= char <= '9' for char in stem), 'foreign_block_index_invalid')
                index = int(stem)
                require(index not in result, 'foreign_duplicate_block_index')
                result[index] = item
            return result
        before, after = sequence(old_blocks), sequence(new_blocks)
        require(before, 'foreign_baseline_blocks_required')
        first, last = min(before), max(before)
        require(sorted(before) == list(range(first, last+1))
                and sorted(after) == list(range(first, max(after)+1)), 'foreign_block_gap')
        counts = {len(row['allocations']) for row in before.values()}
        widths = {len(Path(row['path']).stem) for row in before.values()}
        require(len(counts) == len(widths) == 1 and next(iter(counts)) > 0, 'foreign_baseline_block_shape_changed')
        require(all(len(row['allocations']) in counts and len(Path(row['path']).stem) in widths
                    and len(identities(row['allocations'])) == len(row['allocations']) for row in after.values()),
                'foreign_partial_or_duplicate_block')
    rows = {**old, **own, **added}
    require(all(rows.get(row['run_id']) == row for row in new_projection['allocation_identities']),
            'foreign_unattributed_capture_identity')
    require(not set(seeds(own.values())) & set(seeds([*old.values(), *added.values()])), 'native_own_allocation_collision')
    expected = {**original, 'allocation_identities': list(rows.values()), 'run_ids': sorted(rows),
                'allocation_seeds': seeds(rows.values()), 'frozen_input_pins': [*original['frozen_input_pins'], allocation['jobs']]}
    require(h.encoded(expected['allocation_seeds']) == h.encoded(current['allocation_seeds']),
            'reservation_seed_union_changed')
    return _reservation_equal(expected, current)


def donor_cuts_clear(history_policy, baseline, current, fault_applicability=None):
    keys=('host','output_root','mode','recorder_relevant')
    require([{k:r[k] for k in keys} for r in baseline]==history_policy['donor_scopes']
            ==[{k:r[k] for k in keys} for r in current], 'donor_history_scope_changed')
    for bound,row in zip(baseline,current):
        remote=row['journal'];raw=h.checked_bytes(row['capture']);ref=bound['journal']
        require(remote['path']==ref['path']==row['output_root']+'/.dispatcher/journal.jsonl'
                and len(raw)==remote['bytes'] and hashlib.sha256(raw).hexdigest()==remote['sha256'], 'donor_capture_binding_changed')
        prefix=raw[:ref['bytes']]
        require(len(prefix)==ref['bytes'] and hashlib.sha256(prefix).hexdigest()==ref['sha256']
                and prefix.endswith(b'\n') and raw.endswith(b'\n'), 'donor_history_prefix_changed')
        if row['mode']=='sealed':require(remote==ref, 'sealed_donor_changed')
        else:require(row['mode']=='live_prefix', 'unknown_donor_mode')
        recent(row['at'])
        replay=replay_history(raw)
        relevance = history_relevance(replay, history_policy['model_aliases'], history_policy['task_routes'],
                                      row['recorder_relevant'], bound, fault_applicability or [])
        require(not (row['recorder_relevant'] and replay['unresolved_events']) and not relevance['blocked_routes'],
                'donor_history_requires_reconciliation')
    return True


def finite_terminal_identity(jobs, config, raw, custody):
    require(raw and raw.endswith(b'\n'), 'finite_journal_missing_or_torn')
    expected = {j['allocation']['run_id']: object_sha(j) for j in jobs}
    require(len(expected) == len(jobs), 'duplicate_prior_assignment')
    claims, finals, last_claim, exhaustion = {}, {}, -1, []
    for position, line in enumerate(raw.splitlines()):
        row = json.loads(line); event = row['event']
        require(not event.startswith('unresolved_') and event != 'controller_error', 'prior_unresolved_event')
        if event == 'claimed':
            rid = row['run_id']
            require(rid not in claims and expected.get(rid) == row['job_sha256']
                    and row.get('config_sha256') == object_sha(config), 'prior_claim_identity_changed')
            claims[rid] = position; last_claim = position
        elif event == 'episode_finished':
            rid = row['run_id']
            require(rid in claims and rid not in finals, 'prior_final_without_unique_claim')
            finals[rid] = position
        elif event == 'existing_completed_summary':
            raise ValueError('unreviewed_prior_replay_receipt')
        elif event == 'extension_block_created':
            raise ValueError('finite_predecessor_created_block')
        elif event == 'waiting_for_operator_pause_no_eligible_extensions':
            exhaustion.append(position)
    require(set(claims) == set(finals) == set(expected), 'finite_terminal_membership_incomplete')
    require(any(position > last_claim for position in exhaustion), 'finite_exhaustion_not_after_last_claim')
    require(custody['completed_ids'] == [j['allocation']['run_id'] for j in jobs]
            and set(custody['summaries']) == set(expected), 'prior_copy_custody_membership_changed')
    for rid, reference in custody['summaries'].items():
        require(Path(reference['path']).parent.name == rid and Path(reference['path']).name == 'summary.json',
                'prior_summary_path_changed')
        h.checked_bytes(reference)
    return {'assigned':len(expected), 'claims':len(claims), 'finals':len(finals),
            'lifetime_route_failures':replay_history(raw)['lifetime_route_failures']}


def resources(base):
    mem = {line.split(':',1)[0]:int(line.split()[1])*1024 for line in Path('/proc/meminfo').read_text().splitlines()
           if line.startswith(('MemTotal:','MemAvailable:'))}
    cpu, limit, available = float(len(os.sched_getaffinity(0))), mem['MemTotal'], mem['MemAvailable']
    group_text = Path('/proc/self/cgroup').read_text().strip()
    require(group_text.startswith('0::'), 'unqualified_cgroup_layout')
    group = Path('/sys/fs/cgroup') / group_text.split('::',1)[1].lstrip('/')
    for path in (group, *[p for p in group.parents if p.is_relative_to('/sys/fs/cgroup')]):
        if (path/'cpu.max').exists():
            quota, period = (path/'cpu.max').read_text().split()
            if quota != 'max': cpu = min(cpu,int(quota)/int(period))
        if (path/'memory.max').exists() and (path/'memory.max').read_text().strip() != 'max':
            maximum = int((path/'memory.max').read_text())
            limit = min(limit,maximum); available = min(available,max(0,maximum-int((path/'memory.current').read_text())))
    disk = os.statvfs(h.canonical_path(base))
    return {'at':h.now(),'cpu_budget':cpu,'memory_limit_bytes':limit,'memory_available_bytes':available,
            'root_available_bytes':disk.f_bavail*disk.f_frsize,'cgroup':group_text}


def resource_check(decision, measured, prototypes, workers):
    require(prototypes and type(workers) is int and workers > 0, 'nonempty_capacity_prototypes_required')
    cpu = max(float(j['spec']['cpus']) for j in prototypes)
    ram = max(int(j['spec']['memory_bytes']) for j in prototypes)
    for key in ('os_provider_cpu_reserve','os_provider_ram_reserve_bytes','disk_growth_reserve_bytes','disk_floor_bytes'):
        require(type(decision[key]) in (int,float) and decision[key] > 0, 'positive_resource_reserve_required')
    require(decision['new_workers'] == workers and decision['existing_task_workers'] == 0, 'capacity_scope_changed')
    require(0 < decision['effective_cpu_budget'] <= measured['cpu_budget']
            and workers*cpu+decision['os_provider_cpu_reserve'] <= decision['effective_cpu_budget'], 'CPU_reservation_unavailable')
    need = workers*ram+decision['os_provider_ram_reserve_bytes']
    require(need <= measured['memory_limit_bytes'] and need <= measured['memory_available_bytes'], 'RAM_reservation_unavailable')
    require(measured['root_available_bytes'] >= decision['disk_floor_bytes']+decision['disk_growth_reserve_bytes'],
            'disk_reservation_unavailable')
    return True


def exact_children_absent(process):
    children = (Path('/proc')/str(process['pid'])/'task'/str(process['pid'])/'children').read_text().split()
    require(not children, 'owned_children_remain')


def waiting_identity(policy, process):
    root = h.canonical_path(policy['output_root'])
    h.process_identity(process); exact_children_absent(process)
    require(h.lock_owned_by(root/'.dispatcher.lock',process['pid']), 'waiting_output_lock_changed')
    require(h.canonical_path(policy['barrier']).is_file(), 'waiting_barrier_missing')
    paths = {'queue_identity':root/'.dispatcher/queue_identity.json',
             'admission_control':root/'.dispatcher/admission_control.json'}
    require(h.checked(h.pin(paths['queue_identity'])) == policy['expected_queue'], 'waiting_queue_changed')
    require(h.checked(h.pin(paths['admission_control'])) == policy['expected_admission_control'], 'waiting_holds_changed')
    identities = list((root/'.dispatcher/controllers').glob('*/identity.json'))
    require(len(identities) == 1, 'one_controller_identity_required')
    value = h.checked(h.pin(identities[0]))
    require(h.encoded(value)==h.encoded(policy['expected_controller_fields']), 'waiting_controller_identity_changed')
    raw = (root/'.dispatcher/journal.jsonl').read_bytes()
    require(raw and raw.endswith(b'\n'), 'waiting_journal_missing_or_torn')
    rows = [json.loads(line) for line in raw.splitlines()]
    allowed = {'controller_started','admission_holds_bound','admission_barrier_waiting'}
    require(all(row['event'] in allowed for row in rows), 'waiting_owner_already_issued_or_faulted')
    pending = (len(h.checked(policy['initial_jobs']))
               if policy.get('schema') == 'causes.native_corpus_admission_policy.v2' else 0)
    require(sum(row['event']=='controller_started' for row in rows) == 1
            and any(row['event']=='admission_barrier_waiting' and row['active']==0
                    and row['pending']==pending for row in rows),
            'empty_waiting_owner_not_observed')
    require(not list((root/'.dispatcher/blocks').glob('*')), 'waiting_owner_created_block')
    require(not [p for p in root.iterdir() if p.is_dir() and p.name != '.dispatcher'], 'waiting_owner_created_attempt')
    return {**{key:h.pin(path) for key,path in paths.items()}, 'controller_identity':h.pin(identities[0]),
            'journal':h.pin(root/'.dispatcher/journal.jsonl',raw),'zero_initial_claims':True,'zero_persisted_blocks':True}


def observe_waiting(policy_reference, launch_reference):
    """Local read-only waiting-owner check for a separately reviewed publisher."""
    policy,launch=h.checked(policy_reference),h.checked(launch_reference)
    require(launch['policy']==policy_reference and launch_reference['path']==str(Path(policy['attempt_root'])/'LAUNCH.json'),
            'waiting_launch_scope_changed')
    h.verify_owned_barrier(launch['owned_barrier'],policy['barrier'])
    for path in policy['pause_paths']:require(not os.path.lexists(h.canonical_path(path)), 'waiting_pause_present')
    from causes.campaign_corpus_succession import native_stack_transition, native_local_owners, native_owner_locks
    transition = native_stack_transition(policy)
    for previous in native_local_owners(policy, transition) if transition else policy['predecessors']:
        state=h.retirement_state(previous['process'])
        require((state['absent'] or state.get('state') in ('Z','X','x'))
                and all(h.lock_free(Path(path)) for path in
                        (native_owner_locks(previous) if transition else [previous['output_root'] + '/.dispatcher.lock'])), 'prior_owner_not_retired')
        if policy.get('schema') == 'causes.native_corpus_admission_policy.v2' and 'closure' in previous and previous.get('kind') not in ('actor', 'self'):
            from causes.campaign_corpus_succession import live_closed_predecessor
            live_closed_predecessor(previous)
    value=waiting_identity(policy,launch['process'])
    for key in ('queue_identity','admission_control','controller_identity'):require(value[key]==launch[key], 'waiting_receipt_changed')
    return {'at':h.now(),'policy':policy_reference,'launch':launch_reference,'process':launch['process'],
            'owned_barrier':launch['owned_barrier'],'previous_owners_retired':True,'controls_clear':True,**value}


class ContinuousAdmission:
    start_action = START
    release_action = RELEASE

    def __init__(self, policy, inputs, authority):
        self.policy_ref, self.inputs_ref, self.authority_ref = policy, inputs, authority
        self.policy, self.inputs = h.checked(policy), h.checked(inputs)
        self.retirement = None
        self.owned_barrier = None

    def static(self):
        p = self.policy
        require(p['schema']=='causes.continuous_admission_policy.v1' and self.inputs['policy']==self.policy_ref,
                'admission_policy_changed')
        h.checked_bytes(self.policy_ref); h.checked_bytes(self.inputs_ref)
        reviewed_policy(p,h.checked(p['policy_template']),h.checked(self.inputs['source_review']))
        for ref in p['source_pins']+p['static_pins']: h.checked_bytes(ref)
        loaded_sources_match(p['operator_module_hashes'])
        require(p['source_sha256'] == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'admission_source_changed')
        require(type(p['workers']) is int and p['workers'] > 0 and p['max_full']==0, 'prefix_capacity_required')
        template = h.checked(p['controller_template'])
        require(template['templates'] == [p['normalized_template']], 'normalized_template_changed')
        t = p['normalized_template']; bundle = h.checked(t['bundle'])
        original=h.checked(p['prospective_controller_template'])
        original_argv=original['argv_template'].copy();original_argv[-1]=p['continuation']['control']['sha256']
        require(t==dict(phase=p['phase'],output_root=original['output_root'],runtime_cohort=original['runtime_cohort'],
                proposed_workers=original['workers'],argv_template_at_proposed_cap=['env','PYTHONPATH='+original['pythonpath'],*original_argv],
                **{k:original[k] for k in ('jobs','config','prototypes','bundle')}), 'prospective_controller_scope_changed')
        require([{k:r[k] for k in ('path','bytes','sha256')} for r in bundle['files']]==p['runtime_source_pins'],
                'runtime_bundle_source_set_changed')
        for ref in p['runtime_source_pins']+p['runtime_input_pins']:h.checked_bytes(ref)
        h.checked_bytes(p['exec_wrapper'])
        require(p['exec_wrapper']['sha256']==hashlib.sha256(Path(h.__file__).read_bytes()).hexdigest(),
                'exec_wrapper_not_qualified_handoff')
        require(h.checked(t['jobs']) == [] and t['jobs']==p['initial_jobs'], 'empty_initial_queue_required')
        config, prototypes, control = h.checked(t['config']), h.checked(t['prototypes']), h.checked(p['continuation']['control'])
        expected_control=h.checked(p['control_template'])
        require(expected_control['reservation'] is None, 'prospective_control_template_changed')
        expected_control['reservation']=self.inputs['precreation_reservation']['evidence']
        require(h.encoded(control)==h.encoded(expected_control) and p['continuation']['control']['path']==p['control_path'],
                'frozen_control_scope_changed')
        require(p['continuation']['package']==p['allocator_package'] and p['continuation']['source_review']==p['allocator_review'],
                'allocator_qualification_changed')
        allocator_review=h.checked(p['allocator_review'])
        require(allocator_review.get('accepted_JP_continuation_source') is True
                and allocator_review['package']==p['allocator_package'], 'allocator_source_not_accepted')
        allocator = CorpusContinuation.from_control(control,prototypes,config)
        require(allocator.binding == p['continuation']['binding'], 'complete_continuation_binding_changed')
        require(control['family']==p['family']==t['runtime_cohort'] and control['task_host']==p['controller_host']
                and control['provider_host']==p['controller_host'] and config['model']==p['model'], 'continuous_runtime_scope_changed')
        require(control['config']==t['config'] and control['prototypes']==t['prototypes'], 'runtime_control_input_changed')
        require(control['reservation'] == self.inputs['precreation_reservation']['evidence'], 'precreation_reservation_changed')
        require(t['argv_template_at_proposed_cap']==['env','PYTHONPATH='+p['pythonpath'],*p['argv']]
                and bundle['path']==p['pythonpath'], 'exact_argv_or_source_path_changed')
        require(t['proposed_workers']==p['workers'] and t['output_root']==p['output_root'], 'template_capacity_or_root_changed')
        flags = dict(zip(p['argv'][4::2],p['argv'][5::2]))
        expected = {'--jobs':t['jobs']['path'],'--config':t['config']['path'],'--output':p['output_root'],
                    '--workers':str(p['workers']),'--max-full':'0','--extension-prototypes':t['prototypes']['path'],
                    '--pause-file':p['dedicated_pause'],'--global-pause-file':p['global_pause'],
                    '--start-barrier-file':p['barrier'],'--admission-holds':p['holds']['path'],
                    '--admission-holds-sha256':p['holds']['sha256'],'--corpus-continuation':p['continuation']['control']['path'],
                    '--corpus-continuation-sha256':p['continuation']['control']['sha256']}
        require(p['argv'][:4]==[p['python'],'-B','-m','causes.campaign_dispatcher'] and flags==expected
                and len(p['argv'])==4+2*len(expected), 'continuous_command_scope_changed')
        code = {Path(row['path']).name:row['sha256'] for row in bundle['files']}
        require(p['continuation']['allocator_sources']=={k:code[k] for k in
                ('campaign_dispatcher.py','corpus_allocation.py','corpus_continuation.py')}, 'allocator_source_set_changed')
        required_inputs=[t[k] for k in ('jobs','config','prototypes','bundle')]+[p['holds'],p['controller_template'],
                         p['continuation']['control']]+[control[k] for k in ('catalog','original_jobs','reservation')]
        require(sorted(p['runtime_input_pins'],key=lambda r:r['path'])==sorted(required_inputs,key=lambda r:r['path']),
                'whole_runtime_inputs_changed')
        execution = {'dispatcher_sha256':code['campaign_dispatcher.py']}
        execution.update({name+'_sha256':code[name] for name in ('campaign_runner.py','campaign_sandbox.py','corpus_allocation.py','corpus_continuation.py')})
        queue = dict(jobs_sha256=object_sha([]),config_sha256=object_sha(config),prototypes_sha256=object_sha(prototypes),
                     execution_code=execution,corpus_continuation={'control':p['continuation']['control'],'binding':allocator.binding})
        require(queue==p['expected_queue'], 'whole_queue_identity_changed')
        controller={**queue,**execution,'workers':p['workers'],'max_full':0,'pause_file':p['dedicated_pause'],
                    'global_pause_file':p['global_pause'],'start_barrier_file':p['barrier'],
                    'disk_floor_bytes':config.get('dispatcher',{}).get('disk_floor_bytes',80*1024**3),
                    'route_failure_threshold':config.get('dispatcher',{}).get('route_failure_threshold',3),
                    'route_recovery':'explicit_qualification_receipt_required'}
        require(h.encoded(controller)==h.encoded(p['expected_controller_fields']), 'whole_controller_identity_changed')
        require(p['resource_decision']['disk_floor_bytes']>=controller['disk_floor_bytes'], 'resource_floor_weakened')
        require({p['global_pause'],p['dedicated_pause']}<=set(p['pause_paths']) and p['barrier'] not in p['pause_paths'],
                'pause_scope_changed')
        require(not any(r['path']==p['barrier'] for r in p['runtime_input_pins']), 'removable_barrier_is_not_input_pin')
        aliases=sorted({'model:'+object_sha([v.get('model'),v.get('endpoint')]) for v in
                        [h.checked(ref) for ref in p['history']['model_configs']]})
        routes=sorted({'task:'+object_sha([j['spec'].get('task_id'),j['spec'].get('image')]) for j in prototypes})
        require(aliases==p['history']['model_aliases'] and routes==p['history']['task_routes'], 'prototype_history_scope_changed')
        require(p['expected_admission_control']=={'schema':'caus.dispatcher_admission_control.v1','admission_holds':p['holds']},
                'admission_hold_identity_changed')
        require(p['host_transfer']['target_output_roots']==[row['output_root'] for row in p['predecessors']]
                and p['output_root'] not in p['host_transfer']['target_output_roots'], 'original_transfer_scope_changed')
        for key in STATIC_GATES:
            rule = p['acceptance_rules'][key]
            wanted=p['static_acceptances'][key]
            require(self.inputs[key]['evidence']==wanted['evidence']
                    and self.inputs[key]['review']==wanted.get('review',self.inputs['source_review']), 'static_qualification_changed:'+key)
            accepted(self.inputs[key],rule['flag'],rule['binding_key'])
        cap_review = h.checked(self.inputs['capacity_inheritance']['review'])
        require(cap_review['next_phase']==p['phase'] and cap_review['next_worker_cap']==p['workers'], 'capacity_inheritance_scope_changed')
        return config, prototypes

    def evidence(self, action):
        p, x = self.policy, self.inputs
        for key in ('prior_custody','precreation_reservation','fresh_reservation','foundation'):
            rule=p['acceptance_rules'][key]
            accepted(x[key],rule['flag'],rule['binding_key'])
        complete=h.checked(x['prior_custody']['evidence'])
        require(complete['host_transfer']==p['host_transfer'] and set(complete['roots'])=={r['output_root'] for r in p['predecessors']},
                'finite_custody_scope_changed')
        for key in ('all_expected_terminal_custody','all_closed_verified','technical_missingness_preserved','no_unresolved_terminal_records'):
            require(complete[key] is True, 'prior_custody_not_complete:'+key)
        old, fresh = h.checked(x['precreation_reservation']['evidence']), h.checked(x['fresh_reservation']['evidence'])
        reservation_unchanged(old,fresh)
        require([c['host'] for c in fresh['host_cuts']]==p['census_hosts'], 'both_host_census_required')
        scope=h.checked(x['fresh_reservation']['review'])
        require(scope['census_config']==p['census_configs']['before_start' if action==START else 'waiting_release'], 'census_root_scope_changed')
        foundation=h.checked(x['foundation']['evidence']);recent(foundation['at'])
        require(foundation['host_transfer']==p['host_transfer'] and foundation['provider']==p['provider'], 'foundation_binding_changed')
        for key in ('legacy_two_owners_healthy','provider_tunnels_healthy','original_transfer_present','unused_MI_output_absent',
                    'donor_histories_clear','native_no_active_owned_work'):
            require(foundation[key] is True, 'foundation_not_clear:'+key)
        require(foundation['legacy_owners']==p['legacy_owners'], 'legacy_owner_binding_changed')
        donor_cuts_clear(p['history'],p['donor_histories'],foundation['donor_cuts'])
        for ref in foundation['evidence_pins']:h.checked_bytes(ref)
        if action==RELEASE:
            ready=accepted(x['startup'], 'accepted_continuous_startup_watch','readiness');recent(ready['at'])
            launch=h.checked(x['launch'])
            require(ready['launch']==x['launch'] and ready['owned_barrier']==launch['owned_barrier'], 'startup_owner_binding_changed')
            for key in ('zero_initial_claims','zero_persisted_blocks','legacy_MI_Tex_healthy','mandatory_JP_companion_valid','all_13_MI_roots_requested','all_4_JP_roots_requested','same_owned_watcher_successor'):
                require(ready[key] is True, 'startup_gate_missing:'+key)
            for key in ('registration','authorization_pointer','monitor_policy','watcher_cycle','MI_sync_roots','JP_sync_roots'):
                h.checked_bytes(ready[key])
            registration=h.checked(ready['registration']);pointer=h.checked(ready['authorization_pointer'])
            require(pointer['registration']==ready['registration'] and registration['controller']['process']==launch['process']
                    and registration['controller']['launch_receipt']==x['launch'], 'startup_registration_changed')
            validate_registration(registration,h.checked(ready['monitor_policy']),reader=h.checked)
            require(registration['donor_histories']==p['donor_histories'], 'startup_donor_binding_changed')
        return complete

    def live(self, action):
        p=self.policy;_,prototypes=self.static();complete=self.evidence(action)
        for index, old in enumerate(p['predecessors']):
            root=h.canonical_path(old['output_root']);saved=complete['roots'][str(root)]
            require(saved['launch']==old['launch'], 'prior_launch_changed')
            launch=h.checked(old['launch'])
            require(launch['process']==old['process'], 'prior_process_binding_changed')
            for key in ('queue_identity','admission_control'):h.checked_bytes(launch[key])
            raw=h.checked_bytes(saved['journal'])
            require(saved['journal']['path']==str(root/'.dispatcher/journal.jsonl'), 'prior_journal_path_changed')
            require(all(ref['path']==str(root/rid/'summary.json') for rid,ref in saved['summaries'].items()),
                    'current_summary_scope_changed')
            finite_terminal_identity(h.checked(old['jobs']),h.checked(old['config']),raw,saved)
            history=replay_history(raw)
            require(not history_blockers(history,p['history']['model_aliases'],p['history']['task_routes'],True)
                    and not history['unresolved_events'], 'prior_route_requires_reconciliation')
            process=launch['process']
            if index==len(p['predecessors'])-1 and action==START and self.retirement is None:
                h.process_identity(process);exact_children_absent(process)
                require(h.lock_owned_by(root/'.dispatcher.lock',process['pid']), 'prior_idle_lock_changed')
            else:
                state=h.retirement_state(process)
                require((state['absent'] or state.get('state') in ('Z','X','x')) and h.lock_free(root/'.dispatcher.lock'), 'prior_owner_not_retired')
        provider=p['provider'];h.process_identity(provider['process'])
        for ref in provider['source_pins']+[provider['ready_receipt'],provider['serving_plan']]:h.checked_bytes(ref)
        require((Path('/proc')/str(provider['process']['pid'])/'cgroup').read_text()==provider['process']['cgroup'],
                'provider_cgroup_changed')
        listener_owned(provider['process']['pid'],p['provider_port'])
        with urllib.request.urlopen(p['model_list_url'],timeout=4) as response:
            body=response.read(1024*1024);require(response.status==200,'provider_HTTP_failure')
        models=json.loads(body)['data']
        require(len(models)==1 and models[0]['id']==p['model'] and models[0]['max_model_len']==p['max_model_len'], 'provider_model_changed')
        measured=resources(p['base']);resource_check(p['resource_decision'],measured,prototypes,p['workers'])
        return {'at':h.now(),'resources':measured,'prior_custody':self.inputs['prior_custody']['evidence'],
                'fresh_reservation':self.inputs['fresh_reservation']['evidence'],'foundation':self.inputs['foundation']['evidence']}

    def authority(self, action):
        def validate(value):
            require(value['policy']==self.policy_ref and value['inputs']==self.inputs_ref
                    and value['phase_requests_authorized'] is (action==self.release_action), 'authority_scope_changed')
            self.live(action)
            return True
        return h.Authority(self.authority_ref,action,tuple(Path(p) for p in self.policy['pause_paths']),validate)

    def retire_predecessor(self, attempt, authority):
        p = self.policy
        old=p['predecessors'][-1];complete=h.checked(self.inputs['prior_custody']['evidence']);previous=h.checked(old['launch'])
        return h.retire_exact_idle_owner(previous['process'],old['output_root'],complete['roots'][old['output_root']]['journal'],attempt/'retirement',authority)

    def local_owner_projection(self):
        if self.policy.get('stack_transition') is not None:
            from causes.campaign_corpus_succession import native_local_owners
            return native_local_owners(self.policy)
        return [{k: row[k] for k in ('output_root', 'process', 'launch')} for row in self.policy['predecessors']]

    def start(self, execute=False):
        p=self.policy;authority=self.authority(self.start_action);authority.check()
        require(not any(os.path.lexists(p[key]) for key in ('output_root','attempt_root','barrier')), 'start_namespace_exists_no_retry')
        if not execute:return {'validated':True,'executed':False,'action':self.start_action}
        control=h.canonical_path(p['control_root']);control.mkdir(exist_ok=True);h.sync_directory(control.parent)
        with h.canonical_path(p['handoff_lock']).open('a+b') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);authority.check()
            require(not any(os.path.lexists(p[key]) for key in ('output_root','attempt_root','barrier')), 'start_namespace_changed')
            attempt=h.new_record_directory(p['attempt_root']);spawn=None
            try:
                h.save_new(attempt,'ADMISSION.json',{'authority':self.authority_ref,'policy':self.policy_ref,'inputs':self.inputs_ref})
                self.retirement=self.retire_predecessor(attempt,authority)
                self.owned_barrier=h.create_owned_barrier(p['barrier'],p['output_root'],p['family'],attempt/'barrier',authority)
                intent=h.save_new(attempt,'INTENT.json',{'at':h.now(),'authority':self.authority_ref,'policy':self.policy_ref,
                    'inputs':self.inputs_ref,'argv':p['argv'],'output_root':p['output_root'],'retirement':self.retirement,'owned_barrier':self.owned_barrier})
                authority.check();h.verify_owned_barrier(self.owned_barrier,p['barrier'])
                env=os.environ.copy();env['PYTHONPATH']=p['pythonpath']
                wrapper=[p['python'],p['exec_wrapper']['path'],str(attempt/'PROCESS.json'),json.dumps(p['argv']),intent['path'],intent['sha256']]
                with (attempt/'stdout.log').open('xb') as out,(attempt/'stderr.log').open('xb') as err:
                    child=subprocess.Popen(wrapper,cwd=p['working_directory'],env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True)
                    spawn=child.pid
                h.save_new(attempt,'SPAWN_RETURN.json',{'at':h.now(),'pid':spawn,'intent':intent})
                deadline=time.monotonic()+8
                while time.monotonic()<deadline:
                    require(child.poll() is None,'waiting_owner_exited_no_retry')
                    if not (attempt/'PROCESS.json').exists():time.sleep(.05);continue
                    receipt=h.checked(h.pin(attempt/'PROCESS.json'))
                    require(receipt['pid']==spawn and receipt['argv']==p['argv'] and receipt['intent_sha256']==intent['sha256'],'process_receipt_changed')
                    process={'pid':spawn,'process_starttime_ticks':str(receipt['process_starttime_ticks']),'argv':p['argv']}
                    actual=[s.decode() for s in (Path('/proc')/str(spawn)/'cmdline').read_bytes().split(b'\0') if s]
                    if actual==wrapper:time.sleep(.05);continue
                    h.process_identity(process)
                    if not (Path(p['output_root'])/'.dispatcher/journal.jsonl').exists():time.sleep(.05);continue
                    raw=(Path(p['output_root'])/'.dispatcher/journal.jsonl').read_bytes()
                    if not raw.endswith(b'\n') or not any(json.loads(line)['event']=='admission_barrier_waiting' for line in raw.splitlines()):
                        time.sleep(.05);continue
                    wait=waiting_identity(p,process);break
                else:raise ValueError('waiting_owner_not_confirmed_no_retry')
                h.verify_owned_barrier(self.owned_barrier,p['barrier'])
                launch={'at':h.now(),'policy':self.policy_ref,'authority':self.authority_ref,'runtime_package':p['runtime_package'],
                    'phase':p['phase'],'runtime_cohort':p['family'],'controller_host':p['controller_host'],'process':process,
                    'process_receipt':h.pin(attempt/'PROCESS.json'),'intent':intent,'output_root':p['output_root'],'pythonpath':p['pythonpath'],
                    'workers':p['workers'],'max_full':p['max_full'],'initial_jobs':p['initial_jobs'],'continuation':p['continuation'],
                    'source_pins':p['runtime_source_pins'],'input_pins':p['runtime_input_pins'],'owned_barrier':self.owned_barrier,
                    'retirement':self.retirement,'prior_local_owners':self.local_owner_projection(),
                    'controller_templates':p['controller_template'],'controller_jobs':p['initial_jobs'],
                    'controller_config':p['normalized_template']['config'],'capacity_qualification':{
                        'evidence':self.inputs['capacity_inheritance']['evidence'],'independent_review':self.inputs['capacity_inheritance']['review']},
                    'donor_histories':p['donor_histories'],
                    'host_transfer':p['host_transfer'],
                    **({'stack_transition':p['stack_transition']} if p.get('stack_transition') is not None else {}), **wait}
                return h.save_new(attempt,'LAUNCH.json',launch)
            except BaseException as error:
                h.save_new(attempt,'FAILED.json',{'at':h.now(),'error_type':type(error).__name__,'error':str(error)[:500],
                    'spawn_returned_pid':spawn,'manual_reconciliation_required':True,'no_automatic_retry_or_termination':True})
                raise

    def release(self, execute=False):
        p=self.policy;authority=self.authority(self.release_action);authority.check();launch=h.checked(self.inputs['launch'])
        require(self.inputs['launch']['path']==str(Path(p['attempt_root'])/'LAUNCH.json') and launch['policy']==self.policy_ref,
                'release_launch_scope_changed')
        owned=launch['owned_barrier']
        def validate_waiting(value):
            wait=waiting_identity(p,launch['process'])
            for key in ('queue_identity','admission_control','controller_identity'):
                require(wait[key]==launch[key], 'waiting_receipt_changed')
            require(value['output_root']==p['output_root'],'owned_marker_output_changed')
            return True
        h.verify_owned_barrier(owned,p['barrier']);validate_waiting(h.checked(owned))
        if not execute:return {'validated':True,'executed':False,'action':self.release_action}
        with h.canonical_path(p['handoff_lock']).open('a+b') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return h.release_owned_barrier(owned,p['barrier'],Path(p['attempt_root'])/'release',authority,validate_waiting)


def native_runtime(allocation, template, common, *, allocation_cache=None):
    """Reconstruct one native queue and its exact command, without publishing it."""
    from causes.campaign_corpus_succession import native_allocation_components
    jobs, config, prototypes, allocator_binding = native_allocation_components(allocation, cache=allocation_cache)
    t = template
    continuous = allocation['target_mode'] == 'continuous_corpus'
    extension_key = 'prototypes' if continuous else 'empty_extensions'
    require(t['jobs'] == allocation['jobs'] and t['config'] == allocation['config']
            and t[extension_key] == allocation['extensions']
            and t['runtime_cohort'] == allocation['family']
            and t['output_root'] == allocation['output_root']
            and t['proposed_workers'] == allocation['workers'], 'native_template_allocation_changed')
    bundle = h.checked(t['bundle'])
    code = {row['name']: row['sha256'] for row in bundle['files']}
    require(len(code) == len(bundle['files']), 'native_duplicate_source_name')
    names = ['campaign_runner.py', 'campaign_sandbox.py']
    if continuous: names += ['corpus_allocation.py', 'corpus_continuation.py']
    execution = {'dispatcher_sha256': code['campaign_dispatcher.py']}
    execution.update({name+'_sha256': code[name] for name in names})
    queue = dict(jobs_sha256=object_sha(jobs), config_sha256=object_sha(config),
                 prototypes_sha256=object_sha(prototypes), execution_code=execution)
    continuation = None
    flags = {'--jobs': t['jobs']['path'], '--config': t['config']['path'],
             '--output': allocation['output_root'], '--workers': str(allocation['workers']),
             '--max-full': str(allocation['max_full']), '--extension-prototypes': t[extension_key]['path'],
             '--pause-file': common['dedicated_pause'], '--global-pause-file': common['global_pause'],
             '--start-barrier-file': common['barrier'], '--admission-holds': common['holds']['path'],
             '--admission-holds-sha256': common['holds']['sha256']}
    if continuous:
        continuation = {'control': allocation['control'], 'binding': allocator_binding,
                        'package': common['allocator_package'], 'source_review': common['allocator_review'],
                        'allocator_sources': {name: code[name] for name in
                            ('campaign_dispatcher.py', 'corpus_allocation.py', 'corpus_continuation.py')}}
        queue['corpus_continuation'] = {'control': allocation['control'], 'binding': allocator_binding}
        flags.update({'--corpus-continuation': allocation['control']['path'],
                      '--corpus-continuation-sha256': allocation['control']['sha256']})
    argv = t['argv_template_at_proposed_cap'][2:]
    actual = dict(zip(argv[4::2], argv[5::2]))
    require(argv[:4] == [common['python'], '-B', '-m', 'causes.campaign_dispatcher']
            and len(argv) == 4+2*len(flags) and actual == flags
            and t['argv_template_at_proposed_cap'][:2] == ['env', 'PYTHONPATH='+common['pythonpath']]
            and bundle['path'] == common['pythonpath'], 'native_command_scope_changed')
    controller = {**queue, **execution, 'workers': allocation['workers'], 'max_full': allocation['max_full'],
                  'pause_file': common['dedicated_pause'], 'global_pause_file': common['global_pause'],
                  'start_barrier_file': common['barrier'],
                  'disk_floor_bytes': config.get('dispatcher', {}).get('disk_floor_bytes', 80*1024**3),
                  'route_failure_threshold': config.get('dispatcher', {}).get('route_failure_threshold', 3),
                  'route_recovery': 'explicit_qualification_receipt_required'}
    control = h.checked(allocation['allocation_control'])
    required_inputs = [t[k] for k in ('jobs', 'config', extension_key, 'bundle')]
    required_inputs += [common['holds'], common['controller_template'], allocation['allocation_control']]
    required_inputs += [control[k] for k in ('catalog', 'prototypes', 'original_jobs', 'reservation')]
    unique = {ref['path']: ref for ref in required_inputs}
    require(all(unique[ref['path']] == ref for ref in required_inputs), 'native_conflicting_input_pins')
    return dict(argv=argv, continuation=continuation, expected_queue=queue,
                expected_controller_fields=controller,
                runtime_input_pins=sorted(unique.values(), key=lambda ref: ref['path']))


def bind_native_policy(template_reference, allocation_reference, controller_template_reference, donor_histories):
    """Bind a reviewed native allocation; retain the original transfer as history."""
    template = h.checked(template_reference)
    from causes.campaign_corpus_succession import TEMPLATE_SCHEMA
    require(template['schema'] == TEMPLATE_SCHEMA, 'native_policy_template_schema')
    p = json.loads(h.encoded(template['fixed']))
    allocation = h.checked(allocation_reference)
    templates = h.checked(controller_template_reference)['templates']
    require(len(templates) == 1, 'one_native_template_required')
    p.update(policy_template=template_reference, allocation=allocation_reference,
             controller_template=controller_template_reference, normalized_template=templates[0],
             donor_histories=json.loads(h.encoded(donor_histories)))
    p.update(native_runtime(allocation, templates[0], p))
    require(set(p) == set(template['fixed']) | set(template['dynamic_keys']) | {'policy_template'},
            'native_binding_field_scope_changed')
    return p


class NativeAdmission(ContinuousAdmission):
    """A new owner after a proven natural closure; never restart its predecessor."""
    start_action = 'start_native_corpus_owner_behind_barrier'
    release_action = 'release_native_corpus_owned_barrier'

    def _allocation_cache(self):
        from causes.campaign_corpus_succession import NativeAllocationCache
        if not hasattr(self, '_allocation_validation'):
            self._allocation_validation = NativeAllocationCache()
        return self._allocation_validation

    def _reservation_cache(self, action):
        if not hasattr(self, '_reservation_validation'):
            self._reservation_validation = {}
        if action not in self._reservation_validation:
            self._reservation_validation[action] = NativeReservationProofCache()
        return self._reservation_validation[action]

    def static(self):
        from causes import campaign_corpus_succession as s
        p, x = self.policy, self.inputs
        require(p['schema'] == s.ADMISSION_SCHEMA and p['origin'] == 'native_successor'
                and x['policy'] == self.policy_ref, 'native_admission_policy_changed')
        h.checked_bytes(self.policy_ref); h.checked_bytes(self.inputs_ref)
        template, review = h.checked(p['policy_template']), h.checked(x['source_review'])
        require(template['schema'] == s.TEMPLATE_SCHEMA
                and review.get('accepted_native_corpus_admission_source') is True
                and review.get('accepted_native_corpus_policy_template') is True
                and review['policy_template'] == p['policy_template']
                and review['source']['sha256'] == p['source_sha256'], 'native_source_not_accepted')
        fixed, dynamic = template['fixed'], template['dynamic_keys']
        require(set(p) == set(fixed) | set(dynamic) | {'policy_template'}
                and len(set(dynamic)) == len(dynamic) and not set(fixed) & set(dynamic)
                and all(h.encoded(p[k]) == h.encoded(v) for k, v in fixed.items()), 'native_reviewed_policy_changed')
        required_modules = {'causes.campaign_admission', 'causes.campaign_corpus_succession',
                            'causes.campaign_monitor', 'causes.campaign_handoff',
                            'causes.corpus_allocation', 'causes.corpus_continuation'}
        require(required_modules <= set(p['operator_module_hashes']), 'native_operator_source_set_incomplete')
        loaded_sources_match(p['operator_module_hashes'])
        require(p['source_sha256'] == h.pin(__file__)['sha256'], 'native_admission_source_changed')
        for ref in p['source_pins'] + p['static_pins'] + p['runtime_source_pins'] + p['runtime_input_pins']:
            h.checked_bytes(ref)
        allocation = accepted(x['allocation'], 'accepted_native_corpus_allocation', 'allocation')
        require(x['allocation']['evidence'] == p['allocation'], 'native_allocation_changed')
        adoption = h.checked(p['allocation_adoption'])
        require(adoption.get('root_accepted') is True and adoption['allocation'] == p['allocation']
                and adoption['allocation_review'] == x['allocation']['review'], 'native_allocation_not_adopted')
        for key in ('family', 'output_root', 'workers', 'max_full', 'target_mode', 'workload'):
            require(p[key] == allocation[key], 'native_policy_allocation_changed:' + key)
        require(p['initial_jobs'] == allocation['jobs'], 'native_initial_jobs_changed')
        normalized = h.checked(p['controller_template'])['templates']
        require(normalized == [p['normalized_template']], 'native_normalized_template_changed')
        reconstructed = native_runtime(allocation, normalized[0], p,
                                       allocation_cache=self._allocation_cache())
        require(all(h.encoded(p[k]) == h.encoded(v) for k, v in reconstructed.items()), 'native_runtime_binding_changed')
        bundle = h.checked(normalized[0]['bundle'])
        require(p['runtime_source_pins'] == [{k: row[k] for k in ('path', 'bytes', 'sha256')}
                                            for row in bundle['files']], 'native_runtime_sources_changed')
        require(h.checked_bytes(p['exec_wrapper']) == Path(h.__file__).read_bytes(), 'native_wrapper_changed')
        config = h.checked(allocation['config'])
        require(config['model'] == p['model'] and allocation['config'] == p['resident_config'], 'native_resident_model_changed')
        base = accepted(x['predecessor_monitor'], 'accepted_native_predecessor_monitor', 'policy')
        require(x['predecessor_monitor'] == p['predecessor_monitor'], 'native_predecessor_monitor_changed')
        transition = s.native_stack_transition(p)
        binding = transition['stack']['bindings'] if transition else base
        custody_reader = s.native_custody_reader(transition)
        for key in ('provider', 'model', 'model_key', 'controller_host', 'provider_port', 'max_model_len', 'host_transfer', 'holds'):
            require(p[key] == binding[key], 'native_base_runtime_changed:' + key)
        require([{k: row[k] for k in ('output_root', 'process')} for row in p['predecessors']]
                == [{k: row[k] for k in ('output_root', 'process')} for row in base['phases']], 'native_prior_owner_order_changed')
        require(p['predecessors'] and 'closure' in p['predecessors'][-1], 'native_immediate_closure_required')
        require(p['family'] not in [phase['runtime_cohort'] for phase in base['phases']]
                and p['output_root'] not in [phase['output_root'] for phase in base['phases']], 'native_namespace_reused')
        s.closed_predecessor(p['predecessors'][-1], custody_reader)
        require(p['resident_config'] == (transition['value']['current_config'] if transition else base['phases'][-1]['controller_config']), 'native_resident_config_changed')
        projected = [{k: row[k] for k in ('host', 'output_root', 'mode', 'recorder_relevant')} for row in p['donor_histories']]
        require(projected == p['history']['donor_scopes'], 'native_donor_history_binding_changed')
        for old in p['predecessors']:
            if 'closure' in old:
                closed = s.closed_predecessor(old, custody_reader)
                host = s.native_predecessor_host(p, old, transition)
                matches = [row for row in p['donor_histories'] if row['host'] == host and row['output_root'] == old['output_root']]
                require(len(matches) == 1 and matches[0]['journal'] == closed['journal'], 'native_closed_journal_binding_changed')
        local = [row for row in p['history']['donor_scopes'] if row['host'] == p['controller_host']]
        expected_local = (transition['exclusion']['donor_histories'] + p['predecessors'][len(transition['base']['phases']):]
                          if transition else p['predecessors'])
        if transition is not None:
            require([row for row in p['donor_histories'] if row['host'] == transition['value']['source_host']]
                    == transition['archive']['donor_histories'], 'native_archived_donors_changed')
            require([row for row in p['donor_histories'] if row['host'] == p['controller_host']][:len(transition['exclusion']['donor_histories'])]
                    == transition['exclusion']['donor_histories'], 'native_current_donors_changed')
        require([row['output_root'] for row in local] == [row['output_root'] for row in expected_local]
                and all(row['mode'] == 'sealed' and row['recorder_relevant'] is True for row in local),
                'native_full_local_history_required')
        require(p['history']['donor_scopes'][:len(base['history']['donor_scopes'])] == base['history']['donor_scopes'],
                'native_base_history_changed')
        jobs, _, prototypes, _ = s.native_allocation_components(allocation, cache=self._allocation_cache())
        routes = sorted({'task:'+object_sha([job['spec'].get('task_id'), job['spec'].get('image')])
                         for job in jobs + prototypes})
        aliases = sorted({'model:'+object_sha([cfg.get('model'), cfg.get('endpoint')])
                          for cfg in [h.checked(ref) for ref in p['history']['model_configs']]})
        require(routes == p['history']['task_routes'] and aliases == p['history']['model_aliases']
                and set(base['history']['model_aliases']) <= set(aliases), 'native_history_routes_changed')
        require(p['resource_decision']['disk_floor_bytes'] >= p['expected_controller_fields']['disk_floor_bytes'],
                'native_resource_floor_weakened')
        require({p['global_pause'], p['dedicated_pause']} <= set(p['pause_paths'])
                and p['barrier'] not in p['pause_paths']
                and not any(old.get('pause_path') in p['pause_paths'] for old in p['predecessors']),
                'native_new_and_retained_pause_scope_changed')
        require(p['expected_admission_control'] == {'schema': 'caus.dispatcher_admission_control.v1', 'admission_holds': p['holds']},
                'native_holds_changed')
        for key in ('runtime', 'native_host', 'images', 'source_publication', 'capacity_inheritance', 'resource_decision'):
            require(x[key] == p['static_acceptances'][key], 'native_static_evidence_changed:' + key)
            rule = p['acceptance_rules'][key]
            accepted(x[key], rule['flag'], rule['binding_key'])
        cap = h.checked(x['capacity_inheritance']['review'])
        require(cap.get('accepted_native_capacity_admission') is True
                and cap['next_phase'] == p['phase'] and cap['next_worker_cap'] == p['workers']
                and cap['max_full'] == p['max_full'] and cap['workload'] == p['workload']
                and cap['resident_config'] == p['resident_config'], 'native_capacity_scope_changed')
        return config, jobs + prototypes

    def evidence(self, action):
        p, x = self.policy, self.inputs
        values = {}
        for key in ('precreation_reservation', 'fresh_reservation', 'foundation'):
            rule = p['acceptance_rules'][key]
            values[key] = accepted(x[key], rule['flag'], rule['binding_key'])
        allocation = h.checked(p['allocation'])
        control = h.checked(allocation['allocation_control'])
        require(control['reservation'] == x['precreation_reservation']['evidence'], 'native_reservation_changed')
        for key in ('precreation_reservation', 'fresh_reservation'):
            h.checked_bytes(x[key]['evidence'])
        old, fresh = (values[k] for k in ('precreation_reservation', 'fresh_reservation'))
        if 'reservation_transition' in p:
            if p['reservation_transition'] == 'exact_own_and_foreign_blocks_v1':
                proof = h.checked(x['fresh_reservation']['review'])
                require(proof.get('accepted_dual_host_capture_custody') is True
                        and proof.get('accepted_native_foreign_block_extension') is True
                        and proof['generation_reservation'] == x['precreation_reservation']['evidence']
                        and proof['reservation_growth'] == p['reservation_growth']
                        and proof['capture_pins'] == fresh['capture_pins'], 'foreign_capture_proof_not_accepted')
                native_reservation_foreign_blocks(old, fresh, allocation, p['reservation_growth'],
                    p['census_configs']['before_start' if action == self.start_action else 'waiting_release'],
                    waiting=action == self.release_action, allocation_cache=self._allocation_cache(),
                    proof_cache=self._reservation_cache(action))
            else:
                require(p['reservation_transition'] == 'exact_own_allocation_v1', 'native_reservation_transition_unknown')
                native_reservation_augmentation(old, fresh, allocation,
                                                allocation_cache=self._allocation_cache())
        else:
            reservation_unchanged(old, fresh)
        require([row['host'] for row in fresh['host_cuts']] == p['census_hosts'], 'native_reservation_hosts_changed')
        scope = h.checked(x['fresh_reservation']['review'])
        require(scope['census_config'] == p['census_configs']['before_start' if action == self.start_action else 'waiting_release'],
                'native_reservation_roots_changed')
        h.checked_bytes(x['foundation']['evidence'])
        foundation = values['foundation']; recent(foundation['at'])
        for key in ('provider', 'host_transfer', 'legacy_owners'):
            require(foundation[key] == p[key], 'native_foundation_changed:' + key)
        require(foundation['predecessors'] == p['predecessors'], 'native_foundation_predecessors_changed')
        from causes.campaign_corpus_succession import native_transition_foundation
        transition = native_transition_foundation(p, foundation)
        flags = ('legacy_owners_healthy', 'provider_tunnels_healthy', 'original_transfer_present',
                 'donor_histories_clear', 'native_no_active_owned_work')
        if transition is None:
            flags += ('all_predecessors_absent_and_unlocked',)
        for key in flags:
            require(foundation[key] is True, 'native_foundation_not_clear:' + key)
        donor_cuts_clear(p['history'], p['donor_histories'], foundation['donor_cuts'],
                         transition['fault_applicability'] if transition else None)
        for ref in foundation['evidence_pins']: h.checked_bytes(ref)
        if action == self.release_action:
            ready = accepted(x['startup'], 'accepted_native_startup_consumers', 'readiness'); recent(ready['at'])
            launch = h.checked(x['launch'])
            require(ready['policy'] == self.policy_ref and ready['launch'] == x['launch']
                    and ready['owned_barrier'] == launch['owned_barrier'], 'native_startup_owner_changed')
            for key in ('zero_initial_claims', 'zero_persisted_blocks', 'legacy_owners_healthy',
                        'mandatory_companion_valid', 'snapshot_ownership_valid'):
                require(ready[key] is True, 'native_startup_gate_missing:' + key)
            require(ready['required_sync_roots'] == p['required_sync_roots'], 'native_startup_root_scope_changed')
            for ref in ready['evidence_pins']: h.checked_bytes(ref)
            registration = h.checked(ready['registration']); pointer = h.checked(ready['authorization_pointer'])
            require(pointer['registration'] == ready['registration']
                    and registration['controller']['process'] == launch['process']
                    and registration['controller']['launch_receipt'] == x['launch'], 'native_startup_registration_changed')
            validate_registration(registration, h.checked(ready['monitor_policy']), reader=h.checked)
            require(registration['donor_histories'] == p['donor_histories'], 'native_startup_history_changed')
        return foundation

    def live(self, action):
        from causes.campaign_corpus_succession import recheck_native_owners
        p = self.policy; _, jobs = self.static(); foundation = self.evidence(action)
        recheck_native_owners(p)
        provider = p['provider']; h.process_identity(provider['process'])
        for ref in provider['source_pins'] + [provider['ready_receipt'], provider['serving_plan']]: h.checked_bytes(ref)
        require((Path('/proc')/str(provider['process']['pid'])/'cgroup').read_text() == provider['process']['cgroup'],
                'native_provider_cgroup_changed')
        listener_owned(provider['process']['pid'], p['provider_port'])
        with urllib.request.urlopen(p['model_list_url'], timeout=4) as response:
            body = response.read(1024*1024); require(response.status == 200, 'native_provider_HTTP_failure')
        models = json.loads(body)['data']
        require(len(models) == 1 and models[0]['id'] == p['model'] and models[0]['max_model_len'] == p['max_model_len'],
                'native_provider_model_changed')
        measured = resources(p['base']); resource_check(p['resource_decision'], measured, jobs, p['workers'])
        return {'at': h.now(), 'resources': measured, 'foundation': self.inputs['foundation']['evidence'],
                'fresh_reservation': self.inputs['fresh_reservation']['evidence']}

    def retire_predecessor(self, attempt, authority):
        from causes.campaign_corpus_succession import recheck_native_owners, native_custody_reader, closed_predecessor
        authority.check()
        transition = recheck_native_owners(self.policy)
        value = closed_predecessor(self.policy['predecessors'][-1], native_custody_reader(transition))
        return h.save_new(attempt, 'PREDECESSOR_CLOSURE.json',
                          {'at': h.now(), 'closure': self.policy['predecessors'][-1]['closure'],
                           'owner_absent': value['owner_absent'], 'no_signal_or_restart': True})

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('start','release'))
    for name in ('policy','inputs','authority'):
        parser.add_argument('--'+name,required=True,help='Exact JSON pin, not an unpinned path')
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    refs=[json.loads(getattr(args,k)) for k in ('policy','inputs','authority')]
    cls=NativeAdmission if h.checked(refs[0]).get('schema')=='causes.native_corpus_admission_policy.v2' else ContinuousAdmission
    operator=cls(*refs)
    print(json.dumps(getattr(operator,args.action)(execute=args.execute),sort_keys=True))


if __name__=='__main__':main()
