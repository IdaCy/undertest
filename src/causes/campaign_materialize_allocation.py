"""Late-bind one native allocation to an accepted closed-history reservation."""
import copy
import hashlib
import json
from pathlib import Path

from causes import campaign_handoff as h
from causes import campaign_corpus_succession as succession
from causes.corpus_continuation import CorpusContinuation


def require(value, reason):
    if not value:
        raise ValueError(reason)


def materialize(inputs, closure_reader=h.checked):
    templates = h.checked(inputs['templates'])
    approval = h.checked(inputs['preparation_review'])
    preparation = h.checked(approval['preparation'])
    require(approval.get('accepted_native_allocation_materializer') is True
            and preparation['source'] == h.pin(__file__)
            and preparation['templates'] == inputs['templates'],
            'unaccepted_allocation_materializer')
    choice = inputs['choice']
    require(choice in ('qualification', 'prefix_fallback', 'mixed_successor'),
            'unknown_allocation_choice')
    template = templates['templates'][choice]
    authority = h.checked(inputs['delegated_adoption'])
    require(authority.get('root_authorized') is True
            and authority['preparation_review'] == inputs['preparation_review']
            and authority['choice'] == choice and authority['inputs'] == inputs['binding'],
            'unadopted_allocation_binding')
    binding = h.checked(inputs['binding'])
    require(binding == {key: inputs[key] for key in
                       ('templates', 'choice', 'reservation', 'reservation_review',
                        'closed_predecessor', 'output_directory', 'roster')},
            'allocation_binding_changed')
    closed = succession.closed_predecessor(inputs['closed_predecessor'], reader=closure_reader)
    review = h.checked(inputs['reservation_review'])
    reservation = h.checked(inputs['reservation'])
    require(review.get('accepted_native_allocation_reservation') is True
            and review['reservation'] == inputs['reservation']
            and review['predecessor_closure'] == inputs['closed_predecessor']['closure']
            and review['final_journal'] == closed['journal']
            and review['all_persisted_assignment_ids_and_numeric_seeds_included'] is True
            and review['no_unknown_or_duplicate_assignments'] is True,
            'reservation_not_bound_to_final_closed_history')
    # The public allocator validates the reservation structure and collision
    # domain. No early active-owner reservation is silently refreshed here.
    control = copy.deepcopy(template['control'])
    require(control['reservation'] is None, 'template_already_bound')
    control['reservation'] = inputs['reservation']
    prototypes, config = h.checked(control['prototypes']), h.checked(control['config'])
    allocator = CorpusContinuation.from_control(control, prototypes, config)
    if choice == 'qualification':
        roster = h.checked(inputs['roster'])
        require(roster['family'] == control['family']
                and [row['run_id'] for row in roster['rows']] == template['selected_run_ids'],
                'physical_roster_order_changed')
        block = allocator.block(0)
        by_id = {job['allocation']['run_id']: job for job in block['jobs']}
        jobs = [by_id[row['run_id']] for row in roster['rows']]
        require([bool(job['allocation']['continue_to_end']) for job in jobs]
                == [row['continue_to_end'] for row in roster['rows']],
                'physical_roster_mode_changed')
        extensions = []
    else:
        require(inputs['roster'] is None and template['selected_run_ids'] is None,
                'continuous_finite_roster_present')
        jobs, extensions = [], prototypes
    output = h.new_record_directory(inputs['output_directory'])
    intent = h.save_new(output, 'INTENT.json', {
        'at': h.now(), 'inputs': inputs, 'source': h.pin(__file__),
        'host_calls': 0, 'model_calls': 0, 'runtime_action': False})
    control_ref = h.save_new(output, 'CONTROL.json', control)
    jobs_ref = h.save_new(output, 'JOBS.json', jobs)
    extensions_ref = (h.save_new(output, 'EMPTY_EXTENSIONS.json', [])
                      if choice == 'qualification' else control['prototypes'])
    allocation = dict(schema='causes.native_corpus_allocation.v1', origin='native_successor',
                      no_fixed_study_pooling=True, target_mode=template['target_mode'],
                      workload=template['workload'], workers=template['workers'], max_full=template['max_full'],
                      family=control['family'], model_key=control['model_key'], controller_host=control['task_host'],
                      output_root=template['output_root'], config=control['config'], jobs=jobs_ref,
                      extensions=extensions_ref, allocation_control=control_ref,
                      control=None if choice == 'qualification' else control_ref,
                      selection=dict(block_index=0, run_ids=template['selected_run_ids'])
                      if choice == 'qualification' else None)
    succession.native_allocation(allocation)
    allocation_ref = h.save_new(output, 'ALLOCATION.json', allocation)
    assignment_metadata = h.save_new(output, 'ASSIGNMENT_METADATA.json', {
        'schema': 'causes.native_assignment_metadata.v1', 'allocation': allocation_ref,
        'config': control['config'], 'rows': [
            {'run_id': job['allocation']['run_id'], 'task_id': job['spec']['task_id'],
             'continue_to_end': job['allocation']['continue_to_end'],
             'job_sha256': hashlib.sha256(h.encoded(job)).hexdigest(),
             'config_sha256': hashlib.sha256(h.encoded(config)).hexdigest(),
             'instruments': job['allocation']['instruments']} for job in jobs]})
    return h.save_new(output, 'RESULT.json', {
        'at': h.now(), 'intent': intent, 'allocation': allocation_ref,
        'assignment_metadata': assignment_metadata,
        'late_fields': ['reservation', 'numeric_seeds', 'exact_job_and_dependent_pins'],
        'physical_roster_changed': False, 'original_configuration_changed': False,
        'initial_jobs': len(jobs), 'full_jobs': sum(bool(j['allocation']['continue_to_end']) for j in jobs),
        'runtime_authorized': False, 'automatic_retry': False})


if __name__ == '__main__':
    import sys
    print(json.dumps(materialize(h.checked(json.loads(sys.argv[1])))))
