"""Separately reviewed release-only composition over an unchanged native runtime.

Allocation reconstructions in registration validation use per-instance caches.
Every original predicate and freshness/live check still executes; the source
pair and full input bytes are rechecked each pass.
"""
import argparse
import copy
import json
from pathlib import Path

from causes import campaign_admission as admission
from causes import campaign_monitor as monitor
from causes import campaign_corpus_succession as succession
from causes import campaign_handoff as h
from causes.campaign_admission import (accepted, donor_cuts_clear, native_reservation_augmentation,
                                      native_reservation_foreign_blocks, recent, require,
                                      reservation_unchanged)
from causes.campaign_monitor import digest, encoded, validate_policy
from causes.campaign_corpus_succession import closed_predecessor, validate_native_allocation
from causes.corpus_continuation import control_full_selection

CONTRACT = 'causes.native_release_operator.v1'
BASE_MODULES = (admission, monitor, succession, h)

def release_native_lineage(policy, registration, reader=h.checked, *, allocation_ref, allocation_cache,
                           lineage_caches=None):
    """Validate an appended native lineage against its unchanged v1 base."""
    from causes.campaign_corpus_succession import accepted
    require(reader is h.checked and isinstance(allocation_cache, succession.NativeAllocationCache),
            'release_allocation_cache_scope_changed')
    require(lineage_caches is None or type(lineage_caches) is dict,
            'release_lineage_cache_scope_changed')
    lineage = policy['native_successions']
    base = reader(lineage['base_policy'])
    base_registration = reader(lineage['base_registration'])
    review = reader(lineage['base_review'])
    require(base['schema'] == 'causes.campaign_monitor_policy.v1'
            and review.get('accepted_native_succession_base') is True
            and review.get('policy') == lineage['base_policy']
            and review.get('registration') == lineage['base_registration'],
            'native_base_not_accepted')
    count = lineage['base_phase_count']
    require(count == len(base['phases']) and policy['phases'][:count] == base['phases']
            and policy['sync_roots'][:count] == base['sync_roots']
            and base_registration['phase'] == base['phases'][-1]['phase'],
            'native_base_root_order_changed')
    for key in ('controller_host', 'transfer_host', 'host_transfer', 'provider',
                'tunnels', 'reverse_listener_process', 'provider_port', 'relay_port',
                'model', 'model_key', 'max_model_len', 'holds', 'remote_base'):
        require(policy[key] == base[key], 'native_base_binding_changed:' + key)
    require(policy['minimum_gpu_free_mib'] >= base['minimum_gpu_free_mib']
            and policy['max_observation_age_seconds'] <= base['max_observation_age_seconds'],
            'native_observation_limits_weakened')
    links, phases = lineage['links'], policy['phases']
    require(len(links) == len(phases) - count and links
            and registration['phase'] == phases[-1]['phase'], 'native_active_tail_changed')
    for index, link in enumerate(links, count):
        phase, previous = phases[index], phases[index - 1]
        require(link['phase'] == phase['phase']
                and link['predecessor']['phase'] == previous['phase']
                and link['predecessor']['output_root'] == previous['output_root']
                and link['predecessor']['process'] == previous['process'],
                'native_predecessor_order_changed')
        closed_predecessor(link['predecessor'], reader)
        allocation = accepted(link['allocation'], 'accepted_native_corpus_allocation',
                              'allocation', reader)
        if index == len(phases) - 1:
            require(link['allocation']['evidence'] == allocation_ref, 'release_final_allocation_changed')
            validate_native_allocation(allocation, reader, cache=allocation_cache)
        else:
            cache = None
            if lineage_caches is not None:
                key = h.encoded(link['allocation']['evidence'])
                if key not in lineage_caches:
                    lineage_caches[key] = succession.NativeAllocationCache()
                cache = lineage_caches[key]
            validate_native_allocation(allocation, reader, cache=cache)
        adoption = reader(link['root_adoption'])
        require(adoption.get('root_accepted') is True
                and adoption.get('allocation') == link['allocation']['evidence']
                and adoption.get('allocation_review') == link['allocation']['review'],
                'native_allocation_not_adopted')
        mode = {'finite_qualification': 'finite', 'continuous_corpus': 'continuous'}
        require(allocation['family'] not in [p['runtime_cohort'] for p in phases[:index]], 'native_family_reused')
        require(allocation['origin'] == 'native_successor'
                and mode.get(allocation['target_mode']) == phase['mode']
                and allocation['output_root'] == phase['output_root']
                and allocation['family'] == phase['runtime_cohort']
                and allocation['jobs'] == phase['controller_jobs']
                and allocation['config'] == phase['controller_config'] == base['phases'][-1]['controller_config']
                and allocation['workers'] == phase['workers']
                and allocation['no_fixed_study_pooling'] is True
                and phase['capacity_required'] is True,
                'native_allocation_scope_changed')
        require(type(allocation['max_full']) is int
                and 0 <= allocation['max_full'] <= allocation['workers'],
                'native_full_capacity_invalid')
        if phase['mode'] == 'continuous':
            require(allocation['control'] == phase['continuation']['control'],
                    'native_continuation_control_changed')
            full_selection = control_full_selection(reader(allocation['control']))
            require(allocation['workload'] == ('mixed' if full_selection else 'prefix_only')
                    and ((allocation['max_full'] > 0) == bool(full_selection)),
                    'native_continuation_workload_changed')
        else:
            require(allocation['workload'] == 'mixed' and allocation['max_full'] > 0,
                    'native_qualification_workload_changed')
        if index == len(phases) - 1:
            argv = registration['controller']['process']['argv']
            require(registration['controller']['workers'] == allocation['workers']
                    and int(argv[argv.index('--max-full') + 1]) == allocation['max_full'],
                    'native_full_capacity_changed')
    scopes = policy['history']['donor_scopes']
    old_scopes = base['history']['donor_scopes']
    require(scopes[:len(old_scopes)] == old_scopes, 'native_base_donor_history_changed')
    local = [row for row in scopes if row['host'] == policy['controller_host']]
    require([row['output_root'] for row in local] == [p['output_root'] for p in phases[:-1]]
            and all(row['mode'] == 'sealed' and row['recorder_relevant'] is True for row in local),
            'native_closed_donor_scope_changed')
    require(registration['donor_histories'] == phases[-1]['donor_histories'],
            'native_donor_registration_changed')
    projection = [{key: row[key] for key in ('host', 'output_root', 'mode', 'recorder_relevant')}
                  for row in registration['donor_histories']]
    require(projection == scopes, 'native_donor_history_missing')
    for link in links:
        predecessor = link['predecessor']
        closure = closed_predecessor(predecessor, reader)
        donors = [row for row in registration['donor_histories'] if row['host'] == policy['controller_host'] and row['output_root'] == predecessor['output_root']]
        require(len(donors) == 1 and donors[0]['journal'] == closure['journal'], 'native_closed_history_pin_changed')
    return links[-1]

def validate_release_registration(reg, policy, reader=h.checked, *, allocation_ref, allocation_cache,
                                  lineage_caches=None):
    validate_policy(policy)
    require(reg['schema'] == 'caus.jp_companion_registration.v1' and reg['status'] == 'active'
            and reg['root_authorized'] is True, 'inactive_registration')
    phases = policy['phases']
    matches = [p for p in phases if p['phase'] == reg['phase']]
    require(len(matches) == 1, 'unregistered_phase')
    phase = matches[0]
    index = phases.index(phase)
    if policy['schema'] == 'causes.campaign_monitor_policy.v2':
        release_native_lineage(policy, reg, reader, allocation_ref=allocation_ref,
                               allocation_cache=allocation_cache, lineage_caches=lineage_caches)
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

class ReleaseOnlyNativeAdmission(admission.NativeAdmission):
    """Reuse allocation proof work without changing the held owner's runtime."""

    def __init__(self, policy, inputs, authority):
        super().__init__(policy, inputs, authority)
        self._release_policy_ref = copy.deepcopy(self.policy_ref)
        self._release_inputs_ref = copy.deepcopy(self.inputs_ref)
        self._release_policy = h.encoded(self.policy)
        self._release_inputs = h.encoded(self.inputs)
        self._release_binding_invalid = False
        self._release_lineage_caches = {}
        self._operator_binding(self.release_action)

    def _operator_binding(self, action):
        require(not self._release_binding_invalid, 'release_operator_binding_invalid')
        try:
            require(action == self.release_action, 'release_operator_action_forbidden')
            require(self.policy_ref == self._release_policy_ref and self.inputs_ref == self._release_inputs_ref,
                    'release_operator_reference_changed')
            require(h.encoded(h.checked(self.inputs_ref)) == self._release_inputs == h.encoded(self.inputs)
                    and h.encoded(h.checked(self.policy_ref)) == self._release_policy == h.encoded(self.policy),
                    'release_operator_input_changed')
            pair = self.inputs.get('release_operator')
            require(type(pair) is dict and set(pair) == {'source', 'review'}, 'release_operator_pair_required')
            require(pair['source'] == h.pin(__file__), 'release_operator_source_changed')
            h.checked_bytes(pair['source'])
            review = h.checked(pair['review'])
            require(review.get('accepted_native_release_operator_sources') is True
                    and review['source'] == pair['source'] and review['contract'] == CONTRACT,
                    'release_operator_not_accepted')
            bases = {module.__name__: h.pin(module.__file__) for module in BASE_MODULES}
            require(review['base_sources'] == bases, 'release_operator_base_changed')
            require(all(self.policy['operator_module_hashes'].get(name) == ref['sha256']
                        for name, ref in bases.items()), 'release_operator_policy_source_changed')
            for ref in bases.values():
                h.checked_bytes(ref)
            proof = h.checked(review['inverse_proof'])
            require(proof['contract'] == CONTRACT and proof['source'] == pair['source']
                    and proof['base_sources'] == bases
                    and proof['whole_function_inverse'] == {'evidence': True, 'registration': True, 'lineage': True},
                    'release_operator_inverse_not_accepted')
        except BaseException:
            self._release_binding_invalid = True
            raise

    def start(self, execute=False):
        raise ValueError('release_operator_start_forbidden')

    def evidence(self, action):
        self._operator_binding(action)
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
        for key in ('legacy_owners_healthy', 'provider_tunnels_healthy', 'original_transfer_present',
                    'donor_histories_clear', 'native_no_active_owned_work', 'all_predecessors_absent_and_unlocked'):
            require(foundation[key] is True, 'native_foundation_not_clear:' + key)
        donor_cuts_clear(p['history'], p['donor_histories'], foundation['donor_cuts'])
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
            validate_release_registration(registration, h.checked(ready['monitor_policy']), reader=h.checked,
                                          allocation_ref=p['allocation'], allocation_cache=self._allocation_cache(),
                                          lineage_caches=self._release_lineage_caches)
            require(registration['donor_histories'] == p['donor_histories'], 'native_startup_history_changed')
        return foundation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('release',))
    for name in ('policy', 'inputs', 'authority'):
        parser.add_argument('--' + name, required=True, help='Exact JSON pin, not an unpinned path')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    refs = [json.loads(getattr(args, key)) for key in ('policy', 'inputs', 'authority')]
    operator = ReleaseOnlyNativeAdmission(*refs)
    print(json.dumps(operator.release(execute=args.execute), sort_keys=True))


if __name__ == '__main__':
    main()
