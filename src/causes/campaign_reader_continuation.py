"""Bind and audit continuation-plan metadata without executing a reader."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from causes import campaign_reader_watch as watch
from causes import campaign_snapshot as snapshot

SCHEMA = 'causes.reader_continuation_descriptor.v1'
TERMINALS = {'finished', 'unresolved_current_reader_attempt',
             'unresolved_prior_reader_attempt', 'existing_reader_output_not_replayed'}
LATE_FIELDS = {
    'prepared', 'prepared_review', 'additive_ready', 'pending',
    'expected_unclaimed_contexts', 'previous_cohort_claimed_keys',
    'intervening_reader_cuts', 'route_predecessor', 'runtime_authorized', 'root_authorized',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def pin(path):
    path = snapshot.canonical_path(path)
    raw = path.read_bytes()
    return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def load(reference):
    return json.loads(watch.checked_pin(reference))


def unique_keys(keys, label):
    require(isinstance(keys, list) and all(type(k) is str and k for k in keys), label + '_keys')
    require(len(keys) == len(set(keys)), label + '_duplicate_keys')
    return set(keys)


def accepted(reference, flag, joins):
    review = load(reference)
    require(review.get(flag) is True and review.get('findings') == [], flag)
    require(all(review.get(key) == value for key, value in joins.items()), flag + '_joins')
    return review


def closed_cut(actual_ref, review_ref):
    actual = load(actual_ref)
    joins = {key: actual[key] for key in ('plan', 'reader_source', 'root', 'journal', 'runtime_result')}
    accepted(review_ref, 'accepted_finite_reader_route_closed_cut', dict(actual=actual_ref, **joins))
    keys = unique_keys(actual['claimed_keys'], 'closed')
    require(actual.get('all_claims_accounted') is True, 'closed_unaccounted')
    require(actual['active'] == actual['unclaimed'] == 0, 'closed_unfinished')
    require(actual['claimed'] == actual['finished_or_unresolved'] == len(keys), 'closed_counts')
    raw = watch.checked_pin(actual['journal'])
    require(bool(raw) and raw.endswith(b'\n'), 'closed_torn_journal')
    rows = [json.loads(line) for line in raw.splitlines()]
    claims = [row['key'] for row in rows if row['event'] == 'claimed']
    terminals = [row['key'] for row in rows if row['event'] in TERMINALS]
    unique_keys(claims, 'journal_claim'); unique_keys(terminals, 'journal_terminal')
    require(claims == actual['claimed_keys'] and set(terminals) == keys, 'closed_journal_membership')
    counter, quarantine = watch.route_state(rows)
    require(type(counter) is int and 0 <= counter < 3 and quarantine is None, 'closed_route_held')
    require(actual['final_consecutive_failed_contexts'] == counter and actual['quarantine'] is None,
            'closed_route_counter')
    # A continuation must retain the inherited counter, even when the last attempt failed.
    carried = rows[0]
    require(carried['event'] == 'reader_route_state_carried'
            and carried['consecutive_failed_contexts'] == actual['initial_consecutive_failed_contexts']
            and carried['carry'] == actual['route_state_carry'], 'closed_route_carry')
    carry = load(actual['route_state_carry'])
    require(carry['consecutive_failed_contexts'] == actual['initial_consecutive_failed_contexts']
            and carry['quarantine'] is None, 'closed_carry_counter')
    watch.checked_pin(actual['runtime_result'])
    return actual, keys, counter


def derive_plan(descriptor_ref):
    """Return a deterministic plan with authorization flags false and audit facts."""
    descriptor = load(descriptor_ref)
    require(descriptor.get('schema') == SCHEMA, 'continuation_descriptor_schema')
    pending = load(descriptor['pending_plan'])
    require(pending.get('schema') == 'causes.fresh_candidate_reader_plan.v1', 'pending_plan_schema')
    require(pending.get('root_authorized') is False and pending.get('runtime_authorized') is False,
            'pending_plan_authorized')
    for key in LATE_FIELDS - {'intervening_reader_cuts', 'root_authorized', 'runtime_authorized'}:
        require(pending.get(key) is None, 'pending_bound_field:' + key)
    require(type(pending.get('workers')) is int and 1 <= pending['workers'] <= 4, 'reader_worker_limit')
    require(pending.get('measurements_only') is True, 'reader_measurements_only')
    watch.checked_pin(pending['source'])
    preparation = load(descriptor['preparation'])
    joins = {key: preparation[key] for key in ('source', 'cohort', 'ready', 'self_jobs', 'unavailable', 'pending', 'reader_source')}
    input_review = accepted(descriptor['preparation_review'], 'accepted_fresh_candidate_reader_inputs',
                            dict(preparation=descriptor['preparation'], **joins))
    require(input_review['preceding_closed_actual'] == descriptor['predecessor_actual']
            and input_review['preceding_closed_review'] == descriptor['predecessor_review'], 'input_predecessor')
    require(preparation['source'] == pending['materializer'] and preparation['cohort'] == pending['cohort'],
            'input_sources_or_cohort')
    latest, claimed, counter = closed_cut(descriptor['predecessor_actual'], descriptor['predecessor_review'])
    previous = load(latest['plan'])
    accepted(latest['plan_review'], 'accepted_fresh_candidate_reader_plan',
             {'plan': latest['plan'], 'source': latest['reader_source']})
    require(previous['source'] == latest['reader_source'] == pending['source'], 'reader_source_changed')
    require(previous['cohort'] == pending['cohort'], 'reader_cohort_changed')
    prior_keys = unique_keys(previous['previous_cohort_claimed_keys'], 'previous')
    require(not prior_keys.intersection(claimed), 'previous_claim_replayed')
    all_prior = prior_keys | claimed
    require(latest['ready'] == latest['claimed'] == previous['expected_unclaimed_contexts'],
            'predecessor_selection_incomplete')
    old_cuts = previous['intervening_reader_cuts']
    require(isinstance(old_cuts, list) and old_cuts, 'predecessor_history_required')
    require(pending['intervening_reader_cuts'] == old_cuts + [{'actual': None, 'review': None}],
            'history_order_or_placeholder')
    # Existing accepted cuts remain byte-pinned and disjoint, including older non-fresh routes.
    historical_keys = set()
    for cut in old_cuts:
        actual = load(cut['actual'])
        accepted(cut['review'], 'accepted_finite_reader_route_closed_cut', {'actual': cut['actual']})
        keys = unique_keys(actual['claimed_keys'], 'historical')
        require(not historical_keys.intersection(keys), 'historical_claim_replayed')
        historical_keys.update(keys)
    require(actual['final_consecutive_failed_contexts'] == latest['initial_consecutive_failed_contexts'],
            'predecessor_counter_discontinuity')
    require(prior_keys <= historical_keys and not claimed.intersection(historical_keys),
            'predecessor_history_membership')
    ready = load(preparation['ready'])
    keys = [row['measurement_key'] for row in ready]
    ready_set = unique_keys(keys, 'ready')
    require(all_prior <= ready_set, 'ready_lost_previous_claim')
    new_keys = [key for key in keys if key not in all_prior]
    require(new_keys and not set(new_keys).intersection(historical_keys), 'no_new_or_replayed_selection')
    counts = preparation['counts']
    for key in ('reader_ready', 'unavailable', 'copied', 'pending', 'selected'):
        require(type(counts[key]) is int and counts[key] >= 0, 'input_count_type')
    require(counts['reader_ready'] == len(keys)
            and counts['reader_ready'] + counts['unavailable'] == counts['copied']
            and counts['copied'] + counts['pending'] == counts['selected'], 'input_count_partition')
    previous_preparation = load(previous['prepared'])
    require(counts['selected'] == previous_preparation['counts']['selected'], 'selected_roster_changed')
    require(preparation['reader_source'] == previous_preparation['reader_source'], 'prepared_reader_source_changed')
    watch.checked_pin(preparation['reader_source'])
    require(input_review['counts'] == counts, 'accepted_input_counts')
    cuts = copy.deepcopy(old_cuts) + [{'actual': descriptor['predecessor_actual'],
                                     'review': descriptor['predecessor_review']}]
    plan = copy.deepcopy(pending)
    plan.update(prepared=descriptor['preparation'], prepared_review=descriptor['preparation_review'],
                additive_ready=preparation['ready'], pending=preparation['pending'],
                expected_unclaimed_contexts=len(new_keys),
                previous_cohort_claimed_keys=[key for key in keys if key in all_prior],
                intervening_reader_cuts=cuts, route_predecessor=descriptor['predecessor_actual'])
    require({key for key in pending if pending[key] != plan[key]} <= LATE_FIELDS, 'unexpected_plan_change')
    facts = {'prior_claimed_unique': len(all_prior), 'new_contexts': len(new_keys),
             'new_keys': new_keys, 'history_count': len(cuts), 'counts': counts,
             'final_consecutive_failed_contexts': counter, 'quarantine': None}
    return descriptor, plan, facts


def reviewed_plan(descriptor_ref, review_ref):
    descriptor, plan, facts = derive_plan(descriptor_ref)
    review = accepted(review_ref, 'accepted_reader_continuation_descriptor',
                      {'descriptor': descriptor_ref, 'source': pin(Path(__file__).resolve())})
    require(review.get('root_authorized_plan_binding') is True, 'plan_binding_not_authorized')
    require(review.get('authorized_plan_flags') == {'runtime_authorized': True, 'root_authorized': True},
            'plan_flags_not_authorized')
    require(review.get('maximum_invocations') == 1 and review.get('automatic_retry') is False,
            'plan_binding_once_required')
    plan.update(review['authorized_plan_flags'])
    return descriptor, plan, facts


def output_paths(descriptor):
    outputs = descriptor['outputs']
    require(set(outputs) == {'plan', 'binding', 'review'}, 'output_fields')
    paths = {key: snapshot.canonical_path(value) for key, value in outputs.items()}
    require(len(set(paths.values())) == 3, 'output_alias')
    protected = set()
    def visit(value):
        if isinstance(value, dict):
            if {'path', 'bytes', 'sha256'} <= value.keys():
                protected.add(snapshot.canonical_path(value['path']))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(descriptor)
    require(not set(paths.values()).intersection(protected), 'output_overwrites_input')
    return paths


def save_exclusive(path, document):
    with path.open('x') as stream:
        stream.write(json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + '\n')
    return pin(path)


def bind(descriptor_ref, review_ref):
    descriptor, plan, facts = reviewed_plan(descriptor_ref, review_ref)
    paths = output_paths(descriptor)
    require(all(not path.exists() for path in paths.values()), 'output_already_exists')
    require(all(path.parent.is_dir() for path in paths.values()), 'output_parent_missing')
    runtime_output = snapshot.canonical_path(plan['output'])
    require(not runtime_output.exists(), 'runtime_output_already_exists')
    controls = set(plan['pause_paths']) | {plan['pause_file'], plan['revocation_file']}
    require(all(not snapshot.canonical_path(path).exists() for path in controls), 'reader_paused_or_revoked')
    require(not set(paths.values()).intersection(map(Path, controls)), 'metadata_control_path_collision')
    require(all(not path.is_relative_to(runtime_output) for path in paths.values()), 'metadata_in_runtime_output')
    # No runtime authority is minted here; native launch gates still apply to the resulting plan.
    plan_ref = save_exclusive(paths['plan'], plan)
    binding = dict(schema='causes.reader_continuation_binding.v1', descriptor=descriptor_ref,
                   descriptor_review=review_ref, plan=plan_ref, source=pin(Path(__file__).resolve()),
                   facts=facts, runtime_actions=0, model_calls=0, host_calls=0)
    binding_ref = save_exclusive(paths['binding'], binding)
    return {'plan': plan_ref, 'binding': binding_ref, 'facts': facts}


def validate_saved(descriptor_ref, review_ref, plan_ref):
    descriptor, expected, facts = reviewed_plan(descriptor_ref, review_ref)
    paths = output_paths(descriptor)
    require(plan_ref['path'] == str(paths['plan']) and load(plan_ref) == expected, 'saved_plan_changed')
    binding_ref = pin(paths['binding']); binding = load(binding_ref)
    require(binding == dict(schema='causes.reader_continuation_binding.v1', descriptor=descriptor_ref,
                            descriptor_review=review_ref, plan=plan_ref, source=pin(Path(__file__).resolve()),
                            facts=facts, runtime_actions=0, model_calls=0, host_calls=0), 'saved_binding_changed')
    review = dict(schema='causes.reader_continuation_saved_review.v1',
                  accepted_reader_continuation_plan_binding=True, descriptor=descriptor_ref,
                  descriptor_review=review_ref, plan=plan_ref, binding=binding_ref,
                  source=pin(Path(__file__).resolve()), facts=facts, findings=[],
                  native_load_invoked=False, native_launch_authority=False,
                  runtime_actions=0, model_calls=0, host_calls=0)
    return save_exclusive(paths['review'], review)


def parity(descriptor_ref, plan_ref, plan_review_ref):
    """Read-only qualification against an already accepted native plan."""
    _, expected, facts = derive_plan(descriptor_ref)
    actual = load(plan_ref)
    accepted(plan_review_ref, 'accepted_fresh_candidate_reader_plan',
             {'plan': plan_ref, 'source': actual['source']})
    require(actual.get('root_authorized') is True and actual.get('runtime_authorized') is True,
            'parity_plan_flags')
    expected.update(root_authorized=True, runtime_authorized=True)
    require(actual == expected, 'parity_plan_changed')
    return dict(exact_plan_parity=True, descriptor=descriptor_ref, plan=plan_ref,
                plan_review=plan_review_ref, facts=facts, writes=0, runtime_actions=0, model_calls=0, host_calls=0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('bind', 'validate', 'parity'))
    parser.add_argument('--descriptor', required=True, type=json.loads, help='Exact JSON file pin')
    parser.add_argument('--review', type=json.loads, help='Exact descriptor review pin')
    parser.add_argument('--plan', type=json.loads, help='Exact saved plan pin')
    parser.add_argument('--plan-review', type=json.loads, help='Exact native plan review pin for parity')
    args = parser.parse_args(argv)
    if args.operation == 'bind':
        require(args.review is not None, 'descriptor_review_required')
        result = bind(args.descriptor, args.review)
    elif args.operation == 'validate':
        require(args.review is not None and args.plan is not None, 'saved_review_arguments')
        result = validate_saved(args.descriptor, args.review, args.plan)
    else:
        require(args.plan is not None and args.plan_review is not None, 'parity_arguments')
        result = parity(args.descriptor, args.plan, args.plan_review)
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
