"""Continue only untouched derived inputs after an explicitly qualified stopped cut."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def exact(left, right):
    return encoded(left) == encoded(right)


def pin(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and not path.is_symlink(), 'canonical_file')
    body = path.read_bytes()
    return dict(path=str(path), bytes=len(body), sha256=hashlib.sha256(body).hexdigest())


def raw(ref):
    require(type(ref) is dict and set(ref) == {'path', 'bytes', 'sha256'}
            and type(ref['bytes']) is int and ref['bytes'] >= 0, 'file_reference')
    path = Path(ref['path'])
    require(path.is_absolute() and path.resolve() == path and not path.is_symlink(), 'canonical_file')
    body = path.read_bytes()
    require(len(body) == ref['bytes'] and hashlib.sha256(body).hexdigest() == ref['sha256'], 'immutable_pin_changed')
    return body


def checked(ref):
    return json.loads(raw(ref))


def accepted(ref, flag, **joins):
    value = checked(ref)
    require(value.get(flag) is True and value.get('findings') == []
            and all(exact(value.get(key), expected) for key, expected in joins.items()), flag)
    return value


def module(ref):
    """Check executable bytes before import and the loaded file afterward."""
    raw(ref)
    name = 'derived_reader_bound_' + ref['sha256']
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ref['path'])
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[name] = loaded
        try:
            spec.loader.exec_module(loaded)
        except BaseException:
            del sys.modules[name]
            raise
    loaded = sys.modules[name]
    require(exact(pin(loaded.__file__), ref), 'wrong_imported_module')
    return loaded



def keys(rows):
    require(type(rows) is list and all(type(r) is dict and type(r.get('measurement_key')) is str
            for r in rows), 'measurement_rows')
    value = [r['measurement_key'] for r in rows]
    require(len(value) == len(set(value)), 'duplicate_measurement_key')
    return value


def stopped_partition(prior, cut, result, events, ready):
    """Keep the entire occupied prefix, including the unresolved attempt."""
    all_keys = keys(ready)
    claims = [r['key'] for r in events if r['event'] == 'claimed']
    terminals = [r['key'] for r in events if r['event'] in ('finished', 'unresolved_current_reader_attempt')]
    unresolved = [r['key'] for r in events if r['event'] == 'unresolved_current_reader_attempt']
    require(claims and len(claims) < len(all_keys) and len(claims) == len(set(claims))
            and len(terminals) == len(set(terminals)) == len(claims)
            and set(terminals) == set(claims) and claims == all_keys[:len(claims)], 'stopped_prefix')
    suffix = all_keys[len(claims):]
    require(all(type(cut[k]) is int for k in ('claimed', 'active', 'unclaimed', 'ready'))
            and cut['claimed'] == len(claims) and cut['ready'] == len(all_keys)
            and cut['active'] == 0 and cut['unclaimed'] == len(suffix)
            and cut['all_claims_accounted'] is True and cut['final_for_reader_analysis'] is False,
            'stopped_cut_counts')
    require(cut['root'] == prior['output'] and cut['reader_source'] == prior['source']
            and cut['prepared'] == prior['prepared'] and cut['route_predecessor'] == prior['route_predecessor'],
            'stopped_predecessor')
    require(claims == cut['claimed_keys'] == result['claimed']
            and suffix == cut['unclaimed_keys'] == result['unclaimed']
            and terminals == result['finished_or_unresolved']
            and result['ready'] == len(all_keys) and result['prepared'] == prior['prepared']
            and result['route_predecessor'] == prior['route_predecessor'], 'stopped_native_partition')
    quarantine = cut['quarantine']
    require(type(quarantine) is dict and quarantine.get('reason') == 'unresolved_current_reader_attempt'
            and type(quarantine.get('quarantine_id')) is str and quarantine['quarantine_id']
            and unresolved == [quarantine['key']] and exact(result['quarantine'], quarantine)
            and cut['stop_reason'] == result['stop_reason'] == 'reader_route_quarantined', 'original_quarantine')
    require(type(cut['final_consecutive_failed_contexts']) is int
            and type(result['consecutive_failed_contexts']) is int
            and 0 <= cut['final_consecutive_failed_contexts'] < 3
            and cut['final_consecutive_failed_contexts'] == result['consecutive_failed_contexts'],
            'original_failure_counter')
    require(events and events[0]['event'] == 'reader_route_state_carried'
            and events[-1]['event'] == 'watcher_paused'
            and events[-1]['new_admissions_stopped'] is True, 'closed_stopped_journal')
    return claims, suffix, quarantine


def validate_resolution(binding, plan, cut, qualification, resolution, suffix):
    """Qualify only a new route; the old quarantine is immutable evidence."""
    quarantine = cut['quarantine']
    require(qualification['schema'] == 'causes.derived_reader_context_fix_saved_qualification.v1'
            and qualification['source'] == binding['fixed_runtime']
            and qualification['parent_runtime'] == binding['parent_runtime']
            and qualification['derived_input_adapter'] == binding['input_adapter']
            and qualification['plan'] == binding['previous_plan']
            and qualification['native_result'] == cut['runtime_result']
            and qualification['journal'] == cut['journal_original']
            and qualification['key'] == quarantine['key'], 'qualification_custody')
    require(qualification['classification'] == 'completed_citation_invalid_annotation_missing'
            and qualification['affected_instruments']
            and len(qualification['affected_instruments']) == len(set(qualification['affected_instruments']))
            and set(qualification['affected_instruments']) <= {'presence', 'strongly_favors', 'leans_yes'}
            and qualification['expected_counter_action'] == 'preserve'
            and all(qualification[k] is True for k in ('all_original_records_unchanged',
                'original_row_and_result_unchanged', 'original_unresolved_and_quarantine_retained'))
            and qualification['scientific_missingness_changed'] is False
            and all(type(qualification[k]) is int and qualification[k] == 0
                    for k in ('model_calls', 'host_calls', 'parser_calls', 'route_resolutions_written')),
            'saved_citation_qualification')
    expected = dict(schema='causes.derived_reader_new_route_resolution.v1', status='qualified',
        root_authorized=True, new_route_only=True, original_route_mutation_authorized=False,
        model_calls_authorized=False, stopped_actual=binding['stopped_actual'],
        stopped_review=binding['stopped_review'], previous_plan=binding['previous_plan'],
        source=binding['source'], fixed_runtime=binding['fixed_runtime'],
        fixed_runtime_review=binding['fixed_runtime_review'], qualification=binding['qualification'],
        quarantine=quarantine, quarantine_id=quarantine['quarantine_id'], output=plan['output'],
        unclaimed_keys=suffix, consecutive_failed_contexts=cut['final_consecutive_failed_contexts'],
        counter_action='preserve', new_quarantine=None)
    require(set(resolution) == set(expected) | {'at', 'root_author'}
            and all(exact(resolution[k], v) for k, v in expected.items())
            and type(resolution['at']) is str and type(resolution['root_author']) is str,
            'new_route_resolution')


def history(binding, plan, native, modules, assigned, ready):
    prior = checked(binding['previous_plan'])
    original_binding = checked(prior['derived_binding'])
    accepted(prior['derived_binding_review'], 'accepted_derived_reader_runtime_binding',
             binding=prior['derived_binding'], source=prior['source'], inputs=binding['inputs'])
    accepted(binding['previous_plan_review'], 'accepted_derived_reader_plan',
             plan=binding['previous_plan'], source=prior['source'], binding=prior['derived_binding'],
             inputs=binding['inputs'])
    require(prior['schema'] == 'causes.derived_reader_plan.v1'
            and prior['prepared'] == binding['inputs'] and prior['prepared_review'] == binding['inputs_review']
            and original_binding['parent_runtime'] == binding['parent_runtime']
            and original_binding['input_adapter'] == binding['input_adapter'], 'original_derived_cohort')
    require(exact(binding['fixed_plan_fields'], native['KNOWN']['plan_fields'])
            and all(exact(plan.get(k), v) and exact(prior.get(k), v)
                    for k, v in binding['fixed_plan_fields'].items())
            and plan['operational_policy'] == prior['operational_policy']
            and plan['revocation_file'] == prior['revocation_file']
            and exact(plan['intervening_reader_cuts'], prior['intervening_reader_cuts']), 'complete_previous_history')
    output = Path(plan['output'])
    require(output.is_absolute() and output.resolve() == output and output != Path(prior['output'])
            and plan['pause_file'] == str(output / 'PAUSE')
            and plan['pause_paths'] == [plan['pause_file']] + prior['pause_paths'], 'continuation_pause_scope')
    states = native['arrival_context'](prior, ready, modules,
             native['additive_context'](prior, ready, modules,
             native['secondary_context'](prior, checked(prior['inputs_preparation']), modules,
             native['previous_readers'](prior, modules))))
    states, historical_claims = native['supplemental_history'](prior, states, modules)
    require(not set(historical_claims).intersection(keys(assigned)), 'historical_derived_replay')
    cut = checked(binding['stopped_actual'])
    accepted(binding['stopped_review'], 'accepted_stopped_derived_reader_custody',
             actual=binding['stopped_actual'], plan=binding['previous_plan'], reader_source=prior['source'],
             journal=cut['journal'], runtime_result=cut['runtime_result'])
    adoption = checked(binding['stopped_adoption'])
    require(adoption['schema'] == 'causes.root_stopped_derived_reader_custody_adoption.v1'
            and adoption['accepted'] is True and adoption['actual'] == binding['stopped_actual']
            and adoption['review'] == binding['stopped_review']
            and exact(adoption['quarantine'], cut['quarantine'])
            and adoption['full_cohort_complete'] is False and adoption['original_resolution_absent'] is True
            and cut['plan'] == binding['previous_plan'], 'stopped_plan_adoption')
    body = raw(cut['journal'])
    require(body.endswith(b'\n') and raw(cut['journal_original']) == body, 'stopped_journal_unchanged')
    events = [json.loads(line) for line in body.splitlines()]
    claims, suffix, quarantine = stopped_partition(prior, cut, checked(cut['runtime_result']), events, ready)
    counter, native_quarantine = modules['campaign_reader_watch'].route_state(events)
    require(type(counter) is int and counter == cut['final_consecutive_failed_contexts']
            and exact(native_quarantine, quarantine)
            and events[0]['consecutive_failed_contexts'] == max(v['consecutive_failed_contexts'] for v in states.values()),
            'native_stopped_route_state')
    qualification = checked(binding['qualification'])
    accepted(binding['fixed_runtime_review'], 'accepted_derived_reader_citation_context_fix',
             source=binding['fixed_runtime'], parent_runtime=binding['parent_runtime'],
             saved_qualification=binding['qualification'])
    require(qualification['ready'] == checked(binding['inputs'])['ready'], 'qualified_ready_pin')
    validate_resolution(binding, plan, cut, qualification, checked(binding['route_resolution']), suffix)
    require(plan['previous_cohort_claimed_keys'] == claims and plan['route_predecessor'] == binding['stopped_actual']
            and type(plan['expected_unclaimed_contexts']) is int
            and plan['expected_unclaimed_contexts'] == len(suffix), 'exact_remaining_suffix')
    state = dict(consecutive_failed_contexts=counter, quarantine=None,
        original_quarantine=quarantine, original_counter=counter, occupied_keys=claims,
        stopped_actual=binding['stopped_actual'], stopped_review=binding['stopped_review'],
        qualification=binding['qualification'], qualification_review=binding['fixed_runtime_review'],
        route_resolution=binding['route_resolution'], retained_previous_history=states,
        rule='reviewed_new_route_only_original_quarantine_retained')
    return ready[len(claims):], {'qualified_stopped_derived_route': state}


def stopped_gates(binding, modules):
    cut = checked(binding['stopped_actual'])
    raw(binding['stopped_review']); raw(binding['stopped_adoption']); raw(binding['route_resolution']); raw(binding['qualification'])
    raw(cut['journal_original']); raw(cut['runtime_result'])
    h = modules['campaign_handoff']
    for field, key in (('actor_process', 'identity'), ('supervisor_intent', 'supervisor')):
        identity = checked(cut[field])[key]
        native = dict(pid=identity['pid'], process_starttime_ticks=identity['start_ticks'], argv=identity['argv'])
        require(h.retirement_state(native)['absent'], 'stopped_reader_process_present')
    lock = cut['after']['lock']; path = Path(lock['path']); stat = path.stat()
    require(stat.st_dev == lock['device'] and stat.st_ino == lock['inode'] and h.lock_free(path),
            'stopped_reader_lock_changed_or_occupied')
    resolution_path = Path(cut['quarantine']['required_resolution'])
    require(not resolution_path.exists() and not resolution_path.is_symlink(), 'original_quarantine_resolution_changed')


def runtime_namespace(binding_ref, review_ref):
    binding = checked(binding_ref)
    require(binding['schema'] == 'causes.derived_reader_stopped_continuation_binding.v1'
            and binding['source'] == pin(__file__), 'continuation_binding_schema')
    accepted(review_ref, 'accepted_derived_reader_continuation_binding', binding=binding_ref,
             source=binding['source'], inputs=binding['inputs'], stopped_actual=binding['stopped_actual'],
             qualification=binding['qualification'])
    accepted(binding['source_review'], 'accepted_derived_reader_continuation_source', source=binding['source'],
             fixed_runtime=binding['fixed_runtime'], parent_runtime=binding['parent_runtime'])
    fixed = module(binding['fixed_runtime'])
    accepted(binding['fixed_runtime_review'], 'accepted_derived_reader_runtime_source',
             source=binding['fixed_runtime'], parent_runtime=binding['parent_runtime'])
    parent = module(binding['parent_runtime'])
    for ref in parent.KNOWN['modules'].values():
        raw(ref)
    namespace = dict(vars(parent))
    namespace['__file__'] = str(Path(__file__).resolve())
    namespace['KNOWN'] = copy.deepcopy(parent.KNOWN)
    namespace['KNOWN']['materializer'] = binding['input_adapter']
    for name, fn in vars(parent).items():
        if isinstance(fn, types.FunctionType) and fn.__globals__ is vars(parent):
            clone = types.FunctionType(fn.__code__, namespace, fn.__name__, fn.__defaults__, fn.__closure__)
            clone.__kwdefaults__ = fn.__kwdefaults__
            namespace[name] = clone
    native_gates = namespace['gates']
    native_classify = namespace['classify_secondary_result']

    def classify_secondary_result(key, result, row, output, modules):
        # The frozen row hashes the prepared context object. Native receipts
        # identify its original message sequence; neither digest replaces the
        # other in the saved input or journal.
        adapter = module(binding['input_adapter'])
        context, _ = modules['measurement_view'].prepare_judge_context(adapter.read_view(row['view']))
        require(modules['campaign_reader'].digest(context) == row['context_canonical_sha256'],
                'derived_classification_context_changed')
        native_row = dict(row, context_canonical_sha256=context['context_sha256'])
        return native_classify(key, result, native_row, output, modules)

    def load(plan_ref, plan_review_ref, modules):
        plan = checked(plan_ref)
        accepted(plan_review_ref, 'accepted_derived_reader_continuation_plan', plan=plan_ref,
                 source=binding['source'], binding=binding_ref, inputs=binding['inputs'])
        require(plan['schema'] == 'causes.derived_reader_stopped_continuation_plan.v1'
                and plan['runtime_authorized'] is True and plan['source'] == binding['source']
                and plan['continuation_binding'] == binding_ref and plan['continuation_binding_review'] == review_ref,
                'continuation_plan_join')
        require(type(plan['workers']) is int and plan['workers'] == 4
                and plan['materializer'] == binding['input_adapter']
                and plan['prepared'] == binding['inputs'] and plan['prepared_review'] == binding['inputs_review'],
                'frozen_protocol_plan')
        fixed.validate_storage(binding, plan)
        prep, assigned, ready, config = fixed.validate_inputs(binding, checked(binding['inputs']),
                                                              module(binding['input_adapter']), modules)
        require(plan['cohort'] == plan['additive_assigned'] == prep['assignments']
                and plan['additive_ready'] == prep['ready'], 'original_complete_membership')
        remaining, states = history(binding, plan, namespace, modules, assigned, ready)
        credential = checked(plan['credential_plan'])
        require(credential['env_file'] == plan['env_file'], 'credential_path_changed')
        return plan, prep, assigned, remaining, config, None, states

    def gates(plan, prep, modules):
        require(exact(checked(binding_ref), binding), 'continuation_binding_changed')
        for field in ('source_review', 'fixed_runtime', 'fixed_runtime_review', 'parent_runtime',
                      'inputs', 'inputs_review', 'input_adapter', 'input_adapter_review'):
            raw(binding[field])
        raw(review_ref)
        fixed.validate_storage(binding, plan)
        native_gates(plan, prep, modules)
        stopped_gates(binding, modules)

    namespace.update(load=load, gates=gates, classify_secondary_result=classify_secondary_result)
    return namespace


def validate_union(ready, stopped, continued, tail_result, qualification):
    """Validate the partition of independently accepted cuts; never grant acceptance."""
    all_keys = keys(ready)
    old, tail = stopped['claimed_keys'], continued['claimed_keys']
    require(old and tail and old + tail == all_keys and len(set(old + tail)) == len(all_keys),
            'complete_union_membership')
    require(type(stopped['active']) is int and stopped['active'] == 0 and stopped['all_claims_accounted'] is True
            and stopped['unclaimed_keys'] == tail and stopped['unclaimed'] == len(tail)
            and type(stopped['quarantine']) is dict, 'stopped_union_custody')
    require(all(type(continued[k]) is int for k in ('active', 'unclaimed', 'ready', 'claimed'))
            and continued['active'] == continued['unclaimed'] == 0 and continued['all_claims_accounted'] is True
            and continued['ready'] == continued['claimed'] == len(tail)
            and continued['quarantine'] is None and continued['stop_reason'] is None
            and continued['unclaimed_keys'] == [], 'continuation_not_complete')
    require(tail_result['claimed'] == tail and tail_result['unclaimed'] == []
            and len(tail_result['finished_or_unresolved']) == len(tail)
            and set(tail_result['finished_or_unresolved']) == set(tail)
            and tail_result['stop_reason'] is None and tail_result['quarantine'] is None,
            'continuation_native_completion')
    require(qualification['key'] == stopped['quarantine']['key']
            and qualification['key'] in old and qualification['classification'] == 'completed_citation_invalid_annotation_missing',
            'union_original_missingness')
    return dict(keys=all_keys, roots=[stopped['root']] * len(old) + [continued['root']] * len(tail),
                original_unresolved_key=qualification['key'],
                original_missing_instruments=qualification['affected_instruments'],
                original_quarantine=stopped['quarantine'])



def audit_previous_history(plan, original_history):
    """Extend the saved auditor with the accepted stopped cut, never a clean label."""
    require(plan['schema'] == 'causes.derived_reader_stopped_continuation_plan.v1'
            and plan['source'] == pin(__file__), 'audit_continuation_source')
    binding = checked(plan['continuation_binding'])
    runtime_namespace(plan['continuation_binding'], plan['continuation_binding_review'])
    prior = checked(binding['previous_plan'])
    history_rows, seen = original_history(prior)
    cut = checked(binding['stopped_actual'])
    accepted(binding['stopped_review'], 'accepted_stopped_derived_reader_custody',
             actual=binding['stopped_actual'], plan=binding['previous_plan'],
             reader_source=prior['source'], journal=cut['journal'], runtime_result=cut['runtime_result'])
    require(cut['plan'] == binding['previous_plan']
            and plan['route_predecessor'] == binding['stopped_actual']
            and exact(plan['intervening_reader_cuts'], prior['intervening_reader_cuts']), 'audit_route_ancestry')
    ready = checked(checked(binding['inputs'])['ready'])
    body = raw(cut['journal']); require(raw(cut['journal_original']) == body, 'audit_stopped_journal')
    claims, suffix, _ = stopped_partition(prior, cut, checked(cut['runtime_result']),
                                        [json.loads(line) for line in body.splitlines()], ready)
    require(not set(claims).intersection(seen) and plan['previous_cohort_claimed_keys'] == claims,
            'audit_occupied_membership')
    validate_resolution(binding, plan, cut, checked(binding['qualification']),
                        checked(binding['route_resolution']), suffix)
    return history_rows + [cut], set(seen) | set(claims)



def recheck_stopped_original(binding, stopped, ready):
    """Rehash the original closed journal and RESULT immediately before a union."""
    prior = checked(binding['previous_plan'])
    require(stopped['plan'] == binding['previous_plan'], 'union_original_plan')
    saved = raw(stopped['journal'])
    require(saved.endswith(b'\n') and raw(stopped['journal_original']) == saved,
            'union_original_journal_changed')
    return stopped_partition(prior, stopped, checked(stopped['runtime_result']),
                             [json.loads(line) for line in saved.splitlines()], ready)

def closed_union(binding_ref, binding_review_ref, continuation_ref, continuation_review_ref):
    """Build a read-only union only from accepted cuts and the exact saved repair."""
    runtime_namespace(binding_ref, binding_review_ref)
    binding = checked(binding_ref)
    stopped, continued = checked(binding['stopped_actual']), checked(continuation_ref)
    accepted(binding['stopped_review'], 'accepted_stopped_derived_reader_custody', actual=binding['stopped_actual'])
    accepted(continuation_review_ref, 'accepted_derived_reader_continuation_closed_cut',
             actual=continuation_ref, plan=continued['plan'], reader_source=binding['source'],
             journal=continued['journal'], runtime_result=continued['runtime_result'])
    plan = checked(continued['plan'])
    require(plan['schema'] == 'causes.derived_reader_stopped_continuation_plan.v1'
            and plan['continuation_binding'] == binding_ref and plan['continuation_binding_review'] == binding_review_ref
            and plan['source'] == binding['source'] == continued['reader_source']
            and plan['output'] == continued['root'] != stopped['root']
            and continued['route_predecessor'] == binding['stopped_actual']
            and continued['prepared'] == binding['inputs'], 'union_continuation_join')
    prep = checked(binding['inputs']); ready = checked(prep['ready'])
    recheck_stopped_original(binding, stopped, ready)
    qualification = checked(binding['qualification'])
    accepted(binding['fixed_runtime_review'], 'accepted_derived_reader_citation_context_fix',
             source=binding['fixed_runtime'], parent_runtime=binding['parent_runtime'],
             saved_qualification=binding['qualification'])
    validate_resolution(binding, plan, stopped, qualification, checked(binding['route_resolution']), stopped['unclaimed_keys'])
    union = validate_union(ready, stopped, continued, checked(continued['runtime_result']), qualification)
    return dict(schema='causes.derived_reader_closed_union.v1', source=pin(__file__),
        source_review=binding['source_review'], binding=binding_ref, binding_review=binding_review_ref,
        inputs=binding['inputs'], inputs_review=binding['inputs_review'],
        stopped_actual=binding['stopped_actual'], stopped_review=binding['stopped_review'],
        stopped_adoption=binding['stopped_adoption'], qualification=binding['qualification'],
        qualification_review=binding['fixed_runtime_review'], route_resolution=binding['route_resolution'],
        continuation_actual=continuation_ref, continuation_review=continuation_review_ref,
        assignments=prep['assignments'], unavailable=prep['unavailable'], pairs=prep['pairs'],
        counts=prep['counts'], context_locations=[dict(key=k, root=r) for k, r in zip(union['keys'], union['roots'])],
        original_quarantine=union['original_quarantine'], original_unresolved_key=union['original_unresolved_key'],
        original_missing_instruments=union['original_missing_instruments'],
        full_ready_membership_accounted=True, original_journal_unchanged=True,
        runtime_authorized=False, model_calls=0)

def bind_for_plan(plan_ref):
    plan = checked(plan_ref)
    return types.SimpleNamespace(**runtime_namespace(plan['continuation_binding'], plan['continuation_binding_review']))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binding', required=True)
    parser.add_argument('--binding-review', required=True)
    args, remaining = parser.parse_known_args()
    bound = runtime_namespace(pin(args.binding), pin(args.binding_review))
    sys.argv = [sys.argv[0], *remaining]
    bound['main']()


if __name__ == '__main__':
    main()
