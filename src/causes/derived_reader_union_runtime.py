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


def verified_union(binding):
    extension = binding['history_extension']
    require(type(extension) is dict and set(extension) == {'schema', 'union', 'review'}
            and extension['schema'] == 'causes.derived_reader_closed_union_history.v1', 'union_extension')
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
    require(binding['previous_plan'] == tail['plan']
            and binding['previous_closed'] == union['continuation_actual']
            and binding['previous_closed_review'] == union['continuation_review'], 'union_latest_predecessor')
    return continuation, union, prior_binding, prior, original, tail


def validate_union_plan(binding, plan, known):
    _, union, _, prior, original, tail = verified_union(binding)
    require(plan['history_extension'] == binding['history_extension']
            and plan['intervening_reader_cuts'] == original['intervening_reader_cuts']
            and plan['route_predecessor'] == union['continuation_actual'], 'typed_union_plan_history')
    require(exact(binding['fixed_plan_fields'], known['plan_fields'])
            and all(exact(plan.get(k), v) and exact(prior.get(k), v) and exact(original.get(k), v)
                    for k, v in known['plan_fields'].items()), 'fixed_native_history')
    require(plan['operational_policy'] == prior['operational_policy']
            and plan['revocation_file'] == prior['revocation_file'], 'native_policy_changed')
    output = Path(plan['output'])
    require(output.is_absolute() and output.resolve() == output and not output.is_symlink()
            and str(output) not in {row['root'] for row in union['context_locations']}, 'new_union_successor_output')
    require(plan['pause_file'] == str(output / 'PAUSE')
            and plan['pause_paths'] == [plan['pause_file']] + [p for p in prior['pause_paths']
                                                              if p != prior['pause_file']], 'pause_scope_changed')
    require(tail['active'] == tail['unclaimed'] == 0 and tail['quarantine'] is None, 'completed_tail_required')
    return prior


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
    return {'completed_reader_chain': dict(consecutive_failed_contexts=checked_result['final_counter'],
        quarantine=None, final_cut=union['continuation_actual'], final_cut_review=union['continuation_review'],
        closed_union=binding['history_extension']['union'], closed_union_review=binding['history_extension']['review'],
        original_quarantine=union['original_quarantine'], stopped_route_history=stopped_states,
        retained_previous_history=retained)}, set(previous) | set(occupied)


def union_gates(binding, plan, modules, ordinary_gates):
    continuation, union, old_binding, _, original, tail = verified_union(binding)
    ordinary_gates(original, modules)
    continuation.stopped_gates(old_binding, modules)
    handoff = modules['campaign_handoff']
    for field, key in (('actor_process', 'identity'), ('supervisor_intent', 'supervisor')):
        who = checked(tail[field])[key]
        require(handoff.retirement_state(dict(pid=who['pid'], process_starttime_ticks=who['start_ticks'],
                                              argv=who['argv']))['absent'], 'tail_reader_process_present')
    lock = tail['after']['lock']
    path = Path(lock['path']); current = path.stat()
    require(current.st_dev == lock['device'] and current.st_ino == lock['inode'] and handoff.lock_free(path),
            'tail_reader_lock_changed_or_occupied')
    require(raw(tail['journal_original']) == raw(tail['journal']), 'tail_original_journal_changed')
    raw(tail['runtime_result'])
    require(plan['route_predecessor'] == union['continuation_actual'], 'latest_union_route')


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
    require(len(keys) == len(set(keys)) and not set(keys).intersection(occupied)
            and plan['route_predecessor'] == union['continuation_actual']
            and plan['history_extension'] == binding['history_extension'], 'union_audit_history')
    return rows + [tail], set(occupied) | set(keys)


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
