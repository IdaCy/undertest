"""Bind a reviewed derived cohort to an unchanged finite reader implementation.

The binding and its independent review are data, not execution authority. The
parent's main still requires its separate once authority, native live gates,
credentials and a new output directory before making a request.
"""
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


def ordered_rows(assignments, ready, missing):
    """Retain every assigned arm in its original order, including missing arms."""
    require(all(type(rows) is list for rows in (assignments, ready, missing)), 'row_lists')
    def key(row):
        require(type(row) is dict and all(type(row.get(k)) is str and row[k]
                for k in ('run_id', 'view_kind', 'stage')), 'assignment_identity')
        return tuple(row[k] for k in ('run_id', 'view_kind', 'stage'))
    wanted = [key(row) for row in assignments]
    require(len(wanted) == len(set(wanted)), 'duplicate_assignment')
    require(all(set(row) == {'run_id', 'view_kind', 'stage'} for row in assignments), 'assignment_fields')
    rows = ready + missing
    indexed = {key(row): row for row in rows}
    require(len(indexed) == len(rows) and set(indexed) == set(wanted), 'complete_membership')
    require(all(row['disposition'] == 'prepared' for row in ready)
            and all(row['disposition'] == 'unavailable' for row in missing), 'input_disposition')
    result = [indexed[k] for k in wanted]
    require(exact([r for r in result if r['disposition'] == 'prepared'], ready)
            and exact([r for r in result if r['disposition'] == 'unavailable'], missing), 'input_order')
    keys = [row['measurement_key'] for row in result]
    require(all(type(k) is str and k.startswith('derived_reader_') for k in keys)
            and len(keys) == len(set(keys)), 'derived_measurement_identity')
    return result


def validate_history(binding, plan, parent_known):
    prior = checked(binding['previous_plan'])
    closed = checked(binding['previous_closed'])
    accepted(binding['previous_closed_review'], 'accepted_finite_reader_route_closed_cut',
             actual=binding['previous_closed'], plan=binding['previous_plan'],
             reader_source=prior['source'], root=closed['root'], journal=closed['journal'],
             runtime_result=closed['runtime_result'])
    require(closed['plan'] == binding['previous_plan'] and closed['all_claims_accounted'] is True
            and type(closed['active']) is int and type(closed['unclaimed']) is int
            and closed['active'] == closed['unclaimed'] == 0 and closed['quarantine'] is None,
            'predecessor_not_closed')
    expected = prior['intervening_reader_cuts'] + [dict(actual=binding['previous_closed'],
                                                      review=binding['previous_closed_review'])]
    require(exact(plan['intervening_reader_cuts'], expected)
            and plan['route_predecessor'] == binding['previous_closed'], 'complete_route_history')
    require(exact(binding['fixed_plan_fields'], parent_known['plan_fields'])
            and all(exact(plan.get(k), v) and exact(prior.get(k), v)
                    for k, v in parent_known['plan_fields'].items()), 'fixed_native_history')
    require(plan['operational_policy'] == prior['operational_policy']
            and plan['revocation_file'] == prior['revocation_file'], 'native_policy_changed')
    output = Path(plan['output'])
    require(output.is_absolute() and output.resolve() == output and output != Path(prior['output']),
            'new_canonical_output')
    require(plan['pause_file'] == str(output / 'PAUSE')
            and plan['pause_paths'] == [plan['pause_file']] + [p for p in prior['pause_paths']
                                                            if p != prior['pause_file']], 'pause_scope_changed')
    return prior


def validate_storage(binding, plan):
    grant = checked(binding['storage_authority'])
    require(plan['storage_authority'] == binding['storage_authority']
            and grant.get('root_authorized') is True and grant['runtime_root'] == plan['output'],
            'runtime_storage_authority')
    require(type(grant['runtime_operational_reserve_bytes']) is int and grant['runtime_operational_reserve_bytes'] > 0
            and type(grant['category_cap_bytes']) is int and grant['category_cap_bytes'] > 0
            and type(grant['category_outstanding_reserved_bytes']) is int
            and grant['category_outstanding_reserved_bytes'] >= 0, 'storage_bounds')
    require(type(grant['category_roots']) is list and grant['category_roots']
            and any(Path(plan['output']).is_relative_to(Path(root)) for root in grant['category_roots']),
            'runtime_storage_placement')
    require(all(type(grant[k]) is int and grant[k] >= 0
                for k in ('minimum_free_bytes', 'maximum_contexts', 'maximum_reader_requests', 'workers'))
            and grant['minimum_free_bytes'] == plan['free_space_floor_bytes']
            and grant['maximum_contexts'] == plan['expected_unclaimed_contexts']
            and grant['maximum_reader_requests'] == 3 * plan['expected_unclaimed_contexts']
            and type(grant['workers']) is int and grant['workers'] == plan['workers'], 'storage_scope')
    return grant


def validate_inputs(binding, prep, adapter, modules):
    require(prep['schema'] == 'causes.frozen_derived_reader_preparation.v1'
            and prep['source'] == binding['input_adapter'], 'derived_input_schema')
    accepted(binding['input_adapter_review'], 'accepted_derived_reader_inputs_source',
             source=binding['input_adapter'])
    require(prep['source_review'] == binding['input_adapter_review'], 'input_source_review')
    accepted(binding['inputs_review'], 'accepted_frozen_derived_reader_inputs',
             source=binding['input_adapter'], preparation=binding['inputs'],
             **{key: prep[key] for key in ('ready', 'unavailable', 'assignments', 'pairs')})
    require(exact(prep['counts'], binding['counts']) and all(type(n) is int and n >= 0
            for n in prep['counts'].values()), 'frozen_counts')
    ready, missing, assignments = (checked(prep[k]) for k in ('ready', 'unavailable', 'assignments'))
    rows = ordered_rows(assignments, ready, missing)
    require(prep['counts'] == dict(assigned=len(rows), ready=len(ready), unavailable=len(missing),
            requests=3 * len(ready)) and type(prep['missing_slots']) is int
            and prep['missing_slots'] == 3 * len(missing), 'exact_three_instruments')
    checked(prep['pairs'])
    adapter.validate_membership(assignments, rows, prep['protocol'])
    require(adapter.reader is modules['campaign_reader'], 'native_reader_object_changed')
    protocol = prep['protocol']
    require(protocol['prompts_directory'] == prep['prompts_directory'], 'prompts_directory_changed')
    reader_ref = protocol['reader_source']
    raw(reader_ref)
    require(reader_ref['sha256'] == hashlib.sha256(Path(modules['campaign_reader'].__file__).read_bytes()).hexdigest(),
            'native_reader_bytes_changed')
    # Only the native interface projection is added; original frozen fields stay literal.
    projected = dict(prep, checker_config=protocol['checker_config'], prompts=protocol['prompts'],
                     cohort=prep['assignments'])
    return projected, rows, ready, checked(protocol['checker_config'])


def runtime_namespace(binding_ref, review_ref):
    binding = checked(binding_ref)
    require(binding['schema'] == 'causes.derived_reader_runtime_binding.v1'
            and binding['source'] == pin(__file__), 'runtime_binding_schema')
    accepted(review_ref, 'accepted_derived_reader_runtime_binding', binding=binding_ref,
             source=binding['source'], parent_runtime=binding['parent_runtime'],
             inputs=binding['inputs'], input_adapter=binding['input_adapter'])
    accepted(binding['source_review'], 'accepted_derived_reader_runtime_source',
             source=binding['source'], parent_runtime=binding['parent_runtime'])
    parent = module(binding['parent_runtime'])
    require(exact(parent.KNOWN['plan_fields'], binding['fixed_plan_fields']), 'parent_native_binding')
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
        accepted(plan_review_ref, 'accepted_derived_reader_plan', plan=plan_ref,
                 source=binding['source'], binding=binding_ref, inputs=binding['inputs'])
        require(plan['schema'] == 'causes.derived_reader_plan.v1' and plan['runtime_authorized'] is True
                and plan['source'] == binding['source'] and plan['derived_binding'] == binding_ref
                and plan['derived_binding_review'] == review_ref, 'derived_plan_join')
        require(type(plan['workers']) is int and plan['workers'] == 4
                and plan['materializer'] == binding['input_adapter'], 'native_reader_limits')
        validate_history(binding, plan, parent.KNOWN)
        validate_storage(binding, plan)
        require(plan['prepared'] == binding['inputs'] and plan['prepared_review'] == binding['inputs_review'],
                'frozen_input_plan_join')
        adapter = module(binding['input_adapter'])  # Native package was bootstrapped by parent's main first.
        prep, assigned, ready, config = validate_inputs(binding, checked(binding['inputs']), adapter, modules)
        require(plan['cohort'] == prep['assignments'] and plan['additive_assigned'] == prep['assignments']
                and plan['additive_ready'] == prep['ready'] and plan['previous_cohort_claimed_keys'] == []
                and type(plan['expected_unclaimed_contexts']) is int
                and plan['expected_unclaimed_contexts'] == len(ready), 'whole_derived_cohort')
        states = namespace['arrival_context'](plan, ready, modules,
                 namespace['additive_context'](plan, ready, modules,
                 namespace['secondary_context'](plan, checked(plan['inputs_preparation']), modules,
                 namespace['previous_readers'](plan, modules))))
        states, prior_claims = namespace['supplemental_history'](plan, states, modules)
        require(not set(prior_claims).intersection(row['measurement_key'] for row in assigned), 'derived_replay')
        credential = checked(plan['credential_plan'])
        require(credential['env_file'] == plan['env_file'], 'credential_path_changed')
        return plan, prep, assigned, ready, config, None, states

    def gates(plan, prep, modules):
        require(exact(checked(binding_ref), binding), 'runtime_binding_changed')
        raw(review_ref)
        raw(binding['source_review'])
        raw(binding['parent_runtime'])
        raw(binding['inputs'])
        raw(binding['inputs_review'])
        validate_storage(binding, plan)
        native_gates(plan, prep, modules)

    namespace['load'] = load
    namespace['gates'] = gates
    namespace['classify_secondary_result'] = classify_secondary_result
    return namespace


def bind_for_plan(plan_ref):
    plan = checked(plan_ref)
    return types.SimpleNamespace(**runtime_namespace(plan['derived_binding'], plan['derived_binding_review']))


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
