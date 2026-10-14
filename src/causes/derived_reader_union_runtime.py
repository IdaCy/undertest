"""Use an accepted stopped-plus-completed reader union as immutable route history.

The fixed derived runtime supplies its unchanged finite loop and classifier.
The typed extension retains both native journals and qualifies no old replay.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types


def require(value, reason):
    if not value:
        raise ValueError(reason)


def pin(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and not path.is_symlink(), 'canonical_file')
    body = path.read_bytes()
    return dict(path=str(path), bytes=len(body), sha256=hashlib.sha256(body).hexdigest())


def raw(ref):
    require(type(ref) is dict and set(ref) == {'path', 'bytes', 'sha256'}
            and type(ref['bytes']) is int and ref['bytes'] >= 0, 'exact_pin')
    require(pin(ref['path']) == ref, 'immutable_pin_changed')
    return Path(ref['path']).read_bytes()


def checked(ref):
    return json.loads(raw(ref))


def exact(left, right):
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def accepted(ref, flag, **joins):
    value = checked(ref)
    require(value.get(flag) is True and value.get('findings') == []
            and all(exact(value.get(k), v) for k, v in joins.items()), flag)
    return value


def module(ref):
    raw(ref)
    name = 'derived_union_' + ref['sha256']
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
    require(pin(loaded.__file__) == ref, 'imported_source_changed')
    return loaded


def history_prefix(extension, count=0):
    """Return an exact prefix; an empty suffix retains the original contract."""
    ordinary = {'schema', 'union', 'review'}
    require(type(extension) is dict, 'union_extension')
    if extension.get('schema') == 'causes.derived_reader_closed_union_history.v1':
        require(set(extension) == ordinary and count == 0, 'union_extension')
        return copy.deepcopy(extension)
    require(extension.get('schema') == 'causes.derived_reader_closed_union_tails.v1'
            and set(extension) == ordinary | {'closed_tails'}
            and type(extension['closed_tails']) is list and extension['closed_tails']
            and type(count) is int and 0 <= count <= len(extension['closed_tails']), 'union_extension')
    result = {key: copy.deepcopy(extension[key]) for key in ordinary}
    if count:
        result['closed_tails'] = copy.deepcopy(extension['closed_tails'][:count])
    else:
        result['schema'] = 'causes.derived_reader_closed_union_history.v1'
    return result


def post_union_records(binding, union, base_plan, original, base_tail):
    """Join accepted completed cuts after the union, never a replacement bank."""
    extension = binding['history_extension']
    history_prefix(extension)
    entries = extension.get('closed_tails', [])
    records, seen = [], set()
    previous_ref, previous_review = union['continuation_actual'], union['continuation_review']
    previous, previous_plan = base_tail, base_plan
    roots = {row['root'] for row in union['context_locations']}
    for index, entry in enumerate(entries):
        require(type(entry) is dict and set(entry) == {'actual', 'review'}, 'closed_tail_descriptor')
        actual, peer = checked(entry['actual']), checked(entry['review'])
        require(entry['actual']['path'] not in seen, 'duplicate_closed_tail')
        seen.add(entry['actual']['path'])
        accepted(entry['review'], 'accepted_finite_reader_route_closed_cut', actual=entry['actual'],
                 **{key: actual[key] for key in ('source', 'plan', 'plan_review', 'reader_source',
                    'root', 'journal', 'runtime_result', 'prepared', 'route_predecessor')})
        require(actual['schema'] == 'causes.finite_reader_route_closed_cut.v1'
                and actual['all_claims_accounted'] is True
                and type(actual['active']) is type(actual['unclaimed']) is int
                and actual['active'] == actual['unclaimed'] == 0
                and actual['quarantine'] is None and actual['stop_reason'] is None
                and type(actual['final_consecutive_failed_contexts']) is int
                and 0 <= actual['final_consecutive_failed_contexts'] < 3, 'completed_post_union_tail')
        plan = checked(actual['plan'])
        old_binding = checked(plan['derived_binding'])
        require(plan['schema'] == 'causes.derived_reader_plan.v1'
                and plan['source'] == actual['reader_source'] == old_binding['source']
                and plan['prepared'] == actual['prepared'] == old_binding['inputs'], 'closed_tail_plan')
        accepted(actual['plan_review'], 'accepted_derived_reader_plan', plan=actual['plan'],
                 source=plan['source'], binding=plan['derived_binding'], inputs=plan['prepared'])
        accepted(plan['derived_binding_review'], 'accepted_derived_reader_runtime_binding',
                 binding=plan['derived_binding'], source=plan['source'],
                 **{key: old_binding[key] for key in ('parent_runtime', 'inputs', 'input_adapter')})
        accepted(old_binding['source_review'], 'accepted_derived_reader_union_runtime_source',
                 source=plan['source'], fixed_runtime=binding['fixed_runtime'],
                 parent_runtime=binding['parent_runtime'])
        accepted(peer['source_review'], 'accepted_finite_reader_priority_custody_preparation',
                 source=actual['source'])
        require(all(old_binding[key] == binding[key] for key in
                    ('fixed_runtime', 'parent_runtime', 'input_adapter', 'fixed_plan_fields')),
                'closed_tail_native_sources')
        require(exact(old_binding['history_extension'], history_prefix(extension, index))
                and exact(plan['history_extension'], old_binding['history_extension'])
                and old_binding['previous_closed'] == previous_ref
                and old_binding['previous_closed_review'] == previous_review
                and old_binding['previous_plan'] == previous['plan']
                and actual['route_predecessor'] == plan['route_predecessor'] == previous_ref
                and plan['intervening_reader_cuts'] == original['intervening_reader_cuts'],
                'ordered_post_union_history')
        require(all(exact(plan.get(key), value) for key, value in binding['fixed_plan_fields'].items())
                and plan['operational_policy'] == previous_plan['operational_policy']
                and plan['revocation_file'] == previous_plan['revocation_file'], 'closed_tail_policy')
        output = Path(plan['output'])
        require(output.is_absolute() and output.resolve() == output and not output.is_symlink()
                and str(output) == actual['root'] and str(output) not in roots, 'closed_tail_output')
        require(plan['pause_file'] == str(output / 'PAUSE')
                and plan['pause_paths'] == [plan['pause_file']] + [p for p in previous_plan['pause_paths']
                                                                  if p != previous_plan['pause_file']],
                'closed_tail_pause_scope')
        roots.add(str(output))
        records.append((entry, actual, plan))
        previous_ref, previous_review, previous, previous_plan = entry['actual'], entry['review'], actual, plan
    return records


def verified_union(binding):
    extension = binding['history_extension']
    history_prefix(extension)
    union = checked(extension['union'])
    require(union['schema'] == 'causes.derived_reader_closed_union.v1', 'closed_union_schema')
    accepted(extension['review'], 'accepted_derived_reader_closed_union', union=extension['union'],
             **{k: union[k] for k in ('source', 'binding', 'binding_review', 'continuation_actual', 'continuation_review')})
    source_peer = accepted(union['source_review'], 'accepted_derived_reader_continuation_source', source=union['source'])
    continuation = module(union['source'])
    prior_binding = checked(union['binding'])
    require(prior_binding['fixed_runtime'] == binding['fixed_runtime']
            and prior_binding['parent_runtime'] == binding['parent_runtime']
            and prior_binding['input_adapter'] == binding['input_adapter']
            and source_peer['fixed_runtime'] == binding['fixed_runtime'], 'union_native_source_chain')
    rebuilt = continuation.closed_union(union['binding'], union['binding_review'],
                                        union['continuation_actual'], union['continuation_review'])
    require(exact(rebuilt, union), 'whole_closed_union_changed')
    tail = checked(union['continuation_actual'])
    prior = checked(tail['plan'])
    original = checked(prior_binding['previous_plan'])
    records = post_union_records(binding, union, prior, original, tail)
    latest, actual = (records[-1][0], records[-1][1]) if records else (
        dict(actual=union['continuation_actual'], review=union['continuation_review']), tail)
    require(binding['previous_plan'] == actual['plan']
            and binding['previous_closed'] == latest['actual']
            and binding['previous_closed_review'] == latest['review'], 'union_latest_predecessor')
    return continuation, union, prior_binding, prior, original, tail


def validate_union_plan(binding, plan, known):
    _, union, _, prior, original, tail = verified_union(binding)
    records = post_union_records(binding, union, prior, original, tail)
    if records:
        prior, tail = records[-1][2], records[-1][1]
    require(plan['history_extension'] == binding['history_extension']
            and plan['intervening_reader_cuts'] == original['intervening_reader_cuts']
            and plan['route_predecessor'] == binding['previous_closed'], 'typed_union_plan_history')
    require(exact(binding['fixed_plan_fields'], known['plan_fields'])
            and all(exact(plan.get(k), v) and exact(prior.get(k), v) and exact(original.get(k), v)
                    for k, v in known['plan_fields'].items()), 'fixed_native_history')
    require(plan['operational_policy'] == prior['operational_policy']
            and plan['revocation_file'] == prior['revocation_file'], 'native_policy_changed')
    output = Path(plan['output'])
    require(output.is_absolute() and output.resolve() == output and not output.is_symlink()
            and str(output) not in ({row['root'] for row in union['context_locations']}
                                    | {actual['root'] for _, actual, _ in records}), 'new_union_successor_output')
    require(plan['pause_file'] == str(output / 'PAUSE')
            and plan['pause_paths'] == [plan['pause_file']] + [p for p in prior['pause_paths']
                                                              if p != prior['pause_file']], 'pause_scope_changed')
    require(tail['active'] == tail['unclaimed'] == 0 and tail['quarantine'] is None, 'completed_tail_required')
    return prior


def post_union_history(binding, records, states, occupied, modules):
    """Reuse each accepted capture's pure native journal validator."""
    occupied = set(occupied)
    fixed = module(binding['fixed_runtime'])
    for entry, actual, plan in records:
        prior_binding = checked(plan['derived_binding'])
        prep = checked(actual['prepared'])
        accepted(prior_binding['inputs_review'], 'accepted_frozen_derived_reader_inputs',
                 preparation=actual['prepared'], source=binding['input_adapter'],
                 **{key: prep[key] for key in ('ready', 'unavailable', 'assignments', 'pairs')})
        require(prep['source'] == binding['input_adapter'], 'closed_tail_input_source')
        ready, missing = checked(prep['ready']), checked(prep['unavailable'])
        assigned = fixed.ordered_rows(checked(prep['assignments']), ready, missing)
        body = raw(actual['journal'])
        require(body.endswith(b'\n') and raw(actual['journal_original']) == body, 'post_union_journal_changed')
        events = [json.loads(line) for line in body.splitlines()]
        result, carry = checked(actual['runtime_result']), checked(actual['route_state_carry'])
        validator = module(actual['source'])
        validation = validator.validate_history(events, result, plan, assigned, ready, carry, states, modules)
        keys = validation['claims']
        require(keys == actual['claimed_keys'] == [row['measurement_key'] for row in ready]
                and len(keys) == len(set(keys)) == actual['claimed']
                and not occupied.intersection(keys)
                and actual['assigned'] == len(assigned) and actual['ready'] == len(ready)
                and validation['unclaimed'] == [] and validation['quarantine'] is None
                and result['stop_reason'] is None
                and validation['inherited'] == actual['initial_consecutive_failed_contexts']
                and validation['final_counter'] == actual['final_consecutive_failed_contexts']
                and 0 <= validation['final_counter'] < 3, 'post_union_native_state')
        require(events[0]['carry'] == actual['route_state_carry'], 'post_union_carry_pin')
        occupied.update(keys)
        states = {'completed_reader_chain': dict(
            consecutive_failed_contexts=validation['final_counter'], quarantine=None,
            final_cut=entry['actual'], final_cut_review=entry['review'], retained_previous_history=states)}
    return states, occupied


def union_history(binding, plan, states, modules, native, ordinary_history):
    continuation, union, old_binding, prior, original, tail = verified_union(binding)
    retained, previous = ordinary_history(original, states, modules)
    old_inputs = checked(old_binding['inputs'])
    fixed = module(binding['fixed_runtime'])
    old_ready, old_missing = checked(old_inputs['ready']), checked(old_inputs['unavailable'])
    old_assigned = fixed.ordered_rows(checked(old_inputs['assignments']), old_ready, old_missing)
    tail_ready, stopped_states = continuation.history(old_binding, prior,
        dict(native, supplemental_history=ordinary_history), modules, old_assigned, old_ready)
    body = raw(tail['journal'])
    require(body.endswith(b'\n') and raw(tail['journal_original']) == body, 'tail_journal_changed')
    events = [json.loads(line) for line in body.splitlines()]
    result, carry = checked(tail['runtime_result']), checked(tail['route_state_carry'])
    validator = module(tail['source'])
    checked_result = validator.validate_history(events, result, prior, old_assigned,
                                                tail_ready, carry, stopped_states, modules)
    require(checked_result['unclaimed'] == [] and checked_result['quarantine'] is None
            and result['stop_reason'] is None and checked_result['claims'] == tail['claimed_keys']
            and checked_result['final_counter'] == tail['final_consecutive_failed_contexts']
            and 0 <= checked_result['final_counter'] < 3, 'completed_tail_native_state')
    occupied = [row['key'] for row in union['context_locations']]
    require(len(occupied) == len(set(occupied)) and not set(occupied).intersection(previous), 'union_history_overlap')
    states = {'completed_reader_chain': dict(consecutive_failed_contexts=checked_result['final_counter'],
        quarantine=None, final_cut=union['continuation_actual'], final_cut_review=union['continuation_review'],
        closed_union=binding['history_extension']['union'], closed_union_review=binding['history_extension']['review'],
        original_quarantine=union['original_quarantine'], stopped_route_history=stopped_states,
        retained_previous_history=retained)}
    records = post_union_records(binding, union, prior, original, tail)
    if not records:
        return states, set(previous) | set(occupied)
    return post_union_history(binding, records, states, set(previous) | set(occupied), modules)


def union_gates(binding, plan, modules, ordinary_gates):
    continuation, union, old_binding, prior, original, tail = verified_union(binding)
    ordinary_gates(original, modules)
    continuation.stopped_gates(old_binding, modules)
    handoff = modules['campaign_handoff']
    records = post_union_records(binding, union, prior, original, tail)
    for closed in [tail] + [actual for _, actual, _ in records]:
        for field, key in (('actor_process', 'identity'), ('supervisor_intent', 'supervisor')):
            who = checked(closed[field])[key]
            require(handoff.retirement_state(dict(pid=who['pid'], process_starttime_ticks=who['start_ticks'],
                                                  argv=who['argv']))['absent'], 'tail_reader_process_present')
        lock = closed['after']['lock']
        path = Path(lock['path']); current = path.stat()
        require(current.st_dev == lock['device'] and current.st_ino == lock['inode'] and handoff.lock_free(path),
                'tail_reader_lock_changed_or_occupied')
        require(raw(closed['journal_original']) == raw(closed['journal']), 'tail_original_journal_changed')
        raw(closed['runtime_result'])
    require(plan['route_predecessor'] == binding['previous_closed'], 'latest_union_route')


def runtime_namespace(binding_ref, review_ref):
    binding = checked(binding_ref)
    require(binding['schema'] == 'causes.derived_reader_runtime_binding.v1'
            and binding['source'] == pin(__file__), 'union_runtime_binding')
    accepted(binding['source_review'], 'accepted_derived_reader_union_runtime_source',
             source=binding['source'], fixed_runtime=binding['fixed_runtime'], parent_runtime=binding['parent_runtime'])
    accepted(binding['fixed_runtime_review'], 'accepted_derived_reader_runtime_source',
             source=binding['fixed_runtime'], parent_runtime=binding['parent_runtime'])
    fixed = module(binding['fixed_runtime'])
    # Clone only the factory's globals. Its pinned module and all native code stay untouched.
    factory_globals = dict(vars(fixed), __file__=str(Path(__file__).resolve()), validate_history=validate_union_plan)
    factory = types.FunctionType(fixed.runtime_namespace.__code__, factory_globals,
                                 fixed.runtime_namespace.__name__, fixed.runtime_namespace.__defaults__)
    namespace = factory(binding_ref, review_ref)
    ordinary_history, ordinary_gates = namespace['supplemental_history'], namespace['supplemental_gates']
    namespace['supplemental_history'] = lambda plan, states, mods: union_history(
        binding, plan, states, mods, namespace, ordinary_history)
    namespace['supplemental_gates'] = lambda plan, mods: union_gates(binding, plan, mods, ordinary_gates)
    return namespace


def audit_previous_history(plan, original_history):
    require(plan['schema'] == 'causes.derived_reader_plan.v1' and plan['source'] == pin(__file__), 'union_audit_source')
    binding = checked(plan['derived_binding'])
    runtime_namespace(plan['derived_binding'], plan['derived_binding_review'])
    continuation, union, _, prior, _, tail = verified_union(binding)
    rows, occupied = continuation.audit_previous_history(prior, original_history)
    keys = tail['claimed_keys']
    records = post_union_records(binding, union, prior, checked(
        checked(union['binding'])['previous_plan']), tail)
    require(len(keys) == len(set(keys)) and not set(keys).intersection(occupied)
            and plan['route_predecessor'] == binding['previous_closed']
            and plan['history_extension'] == binding['history_extension'], 'union_audit_history')
    rows, occupied = rows + [tail], set(occupied) | set(keys)
    for _, actual, _ in records:
        keys = actual['claimed_keys']
        require(len(keys) == len(set(keys)) and not occupied.intersection(keys), 'post_union_audit_overlap')
        rows.append(actual)
        occupied.update(keys)
    return rows, occupied


def history_result_refs(plan):
    """Expose exact saved results for custody consumers without reading labels."""
    require(plan['schema'] == 'causes.derived_reader_plan.v1' and plan['source'] == pin(__file__),
            'union_audit_source')
    binding = checked(plan['derived_binding'])
    _, union, old_binding, prior, original, tail = verified_union(binding)
    require(plan['history_extension'] == binding['history_extension']
            and plan['route_predecessor'] == binding['previous_closed'], 'union_result_history')
    stopped = checked(old_binding['stopped_actual'])
    refs = [stopped['runtime_result'], tail['runtime_result']]
    refs += [actual['runtime_result'] for _, actual, _ in
             post_union_records(binding, union, prior, original, tail)]
    for ref in refs:
        raw(ref)
    return refs


def bind_for_plan(plan_ref):
    plan = checked(plan_ref)
    return types.SimpleNamespace(**runtime_namespace(plan['derived_binding'], plan['derived_binding_review']))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binding', required=True)
    parser.add_argument('--binding-review', required=True)
    args, remaining = parser.parse_known_args()
    namespace = runtime_namespace(pin(args.binding), pin(args.binding_review))
    sys.argv = [sys.argv[0], *remaining]
    namespace['main']()


if __name__ == '__main__':
    main()
