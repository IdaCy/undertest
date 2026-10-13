"""Extend an accepted finite reader cohort without changing its native reader."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types


READERS = ['presence', 'strongly_favors', 'leans_towards_or_favors']
SELF = ['evaluation_presence', 'evaluation_strongly_favors',
        'evaluation_leans_towards_or_favors']


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8')


def exact(a, b):
    return encoded(a) == encoded(b)


def pin(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and not path.is_symlink(), 'canonical_file')
    body = path.read_bytes()
    return dict(path=str(path), bytes=len(body), sha256=hashlib.sha256(body).hexdigest())


def raw(ref):
    require(exact(pin(ref['path']), ref), 'immutable_pin_changed')
    return Path(ref['path']).read_bytes()


def checked(ref):
    return json.loads(raw(ref))


def save(path, value):
    with Path(path).open('x') as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + '\n')
    return pin(path)


def accepted(ref, flag, **joins):
    value = checked(ref)
    require(value.get(flag) is True and value.get('findings') == []
            and all(exact(value.get(k), v) for k, v in joins.items()), flag)
    return value


def module(ref):
    raw(ref)
    name = 'reader_extension_' + ref['sha256']
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ref['path'])
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[name] = loaded
        spec.loader.exec_module(loaded)
    loaded = sys.modules[name]
    require(exact(pin(loaded.__file__), ref), 'wrong_bound_module')
    return loaded


def measurement_key(run):
    return 'fresh_candidate_' + hashlib.sha256((run + '/early').encode()).hexdigest()


def unique(rows, field, reason):
    require(type(rows) is list and all(type(row) is dict and type(row.get(field)) is str
            and row[field] for row in rows), reason)
    by = {row[field]: row for row in rows}
    require(len(by) == len(rows), reason)
    return by


def cohort(binding):
    """Validate immutable old membership and the exact ordered added assignments."""
    old = checked(binding['base_preparation'])
    accepted(binding['base_review'], 'accepted_fresh_candidate_reader_inputs',
             preparation=binding['base_preparation'], source=old['source'], cohort=old['cohort'],
             ready=old['ready'], self_jobs=old['self_jobs'], unavailable=old['unavailable'],
             pending=old['pending'], reader_source=old['reader_source'])
    require(checked(old['pending']) == [] and checked(old['self_jobs']) == [], 'base_not_closed_inputs')
    selection = checked(binding['selection'])
    accepted(binding['selection_review'], 'accepted_requested_actor_reader_selection_preparation',
             selection=binding['selection'], global_jobs=selection['global_jobs'])
    jobs = checked(selection['global_jobs'])
    by = unique([dict(run_id=j['allocation']['run_id'], job=j) for j in jobs],
                'run_id', 'duplicate_new_job')
    rows = selection['rows']
    require([r['run_id'] for r in rows] == list(by) and type(selection['contexts']) is int
            and selection['contexts'] == len(by)
            and selection['reader_instruments'] == READERS, 'ordered_selection_changed')
    for row in rows:
        job = by[row['run_id']]['job']; a = job['allocation']
        require(row['job_sha256'] == hashlib.sha256(encoded(job)).hexdigest()
                and a['continue_to_end'] is False and a['instruments'] == SELF
                and a['reader_stages'] == ['early'] and a['reader_thresholds'] == READERS,
                'native_job_or_instrument_changed')
    old_ready, old_missing = checked(old['ready']), checked(old['unavailable'])
    old_ids = set(unique(old_ready + old_missing, 'run_id', 'duplicate_old_input'))
    require(not old_ids.intersection(by), 'new_old_assignment_overlap')
    value = checked(binding['cohort'])
    require(value['schema'] == 'causes.extended_reader_cohort.v1'
            and value['base_preparation'] == binding['base_preparation']
            and value['base_cohort'] == old['cohort'] and value['selection'] == binding['selection']
            and value['new_jobs'] == selection['global_jobs']
            and value['new_run_ids'] == list(by)
            and value['count'] == old['counts']['selected'] + len(by), 'cohort_extension_changed')
    return old, old_ready, old_missing, selection, by


def validate_cumulative(binding, prep, previous=None):
    """Every occupied or unavailable row remains literal, including failed slots."""
    old, base_ready, base_missing, selection, jobs = cohort(binding)
    ready, missing, pending = (checked(prep[k]) for k in ('ready', 'unavailable', 'pending'))
    require(exact(ready[:len(base_ready)], base_ready)
            and exact(missing[:len(base_missing)], base_missing), 'base_rows_removed_or_relabelled')
    all_rows = ready + missing + pending
    by = unique(all_rows, 'run_id', 'duplicate_cumulative_run')
    unique(all_rows, 'measurement_key', 'duplicate_measurement_key')
    require(set(by) == {r['run_id'] for r in base_ready + base_missing} | set(jobs),
            'cumulative_membership_changed')
    require(all(r['disposition'] == 'prepared' for r in ready)
            and all(r['disposition'] == 'unavailable' for r in missing)
            and all(r['disposition'] == 'pending' for r in pending), 'input_disposition_changed')
    for run in jobs:
        row = by[run]
        require(row['stage'] == 'early' and row['measurement_key'] == measurement_key(run),
                'new_measurement_identity_changed')
        if row['disposition'] != 'pending':
            require(row['native_job_sha256'] == hashlib.sha256(encoded(jobs[run]['job'])).hexdigest(),
                    'new_input_job_changed')
    if previous is not None:
        for field in ('ready', 'unavailable'):
            for row in checked(previous[field]):
                require(row['run_id'] in by and exact(by[row['run_id']], row),
                        'previous_input_removed_or_relabelled')
    require(checked(prep['self_jobs']) == [] and prep['known'] == binding['known']
            and prep['source'] == pin(__file__) and prep['row_materializer'] == binding['row_materializer']
            and prep['cohort'] == binding['cohort'], 'input_source_or_scope_changed')
    for field in ('reader_source', 'checker_config', 'prompts', 'prompts_directory'):
        require(exact(prep[field], old[field]), 'reader_protocol_changed:' + field)
    require(exact(prep['counts'], dict(selected=len(by), copied=len(ready) + len(missing),
                reader_ready=len(ready), reader_requests=3 * len(ready), unavailable=len(missing),
                pending=len(pending), self_requests=0)), 'input_counts_changed')
    return ready, missing, pending


def validate_history(binding, plan):
    """A new cohort inherits the entire preceding route, not only new-cohort claims."""
    prior = checked(binding['previous_plan'])
    closure = checked(binding['previous_closed'])
    accepted(binding['previous_closed_review'], 'accepted_finite_reader_route_closed_cut',
             actual=binding['previous_closed'], plan=binding['previous_plan'],
             reader_source=prior['source'], root=closure['root'], journal=closure['journal'],
             runtime_result=closure['runtime_result'])
    require(closure['plan'] == binding['previous_plan'] and closure['all_claims_accounted'] is True
            and closure['active'] == closure['unclaimed'] == 0 and closure['quarantine'] is None,
            'predecessor_not_closed')
    expected = prior['intervening_reader_cuts'] + [dict(actual=binding['previous_closed'],
                                                      review=binding['previous_closed_review'])]
    require(exact(plan['intervening_reader_cuts'], expected)
            and plan['route_predecessor'] == binding['previous_closed'], 'prior_history_changed')
    require(all(exact(plan[k], v) for k, v in binding['fixed_plan_fields'].items()),
            'fixed_native_history_changed')
    return prior


def runtime_namespace(binding_ref, review_ref):
    """Bind explicit cohort data to unchanged native function code in a fresh namespace.

    Only KNOWN's materializer/cohort references and the caller source identity vary.
    The parent module and all of its function globals remain untouched. Its complete
    load, gates, request loop, parsers and history replay code objects are retained.
    """
    binding = checked(binding_ref)
    require(binding['schema'] == 'causes.reader_cohort_extension_binding.v1'
            and binding['source'] == pin(__file__), 'extension_binding_schema')
    accepted(review_ref, 'accepted_reader_cohort_extension_binding', binding=binding_ref,
             source=binding['source'], parent_runtime=binding['parent_runtime'],
             row_materializer=binding['row_materializer'], known=binding['known'])
    parent = module(binding['parent_runtime'])
    require(parent.KNOWN['materializer'] == binding['row_materializer']
            and parent.KNOWN['fresh_known'] == binding['parent_known']
            and exact(parent.KNOWN['plan_fields'], binding['fixed_plan_fields']), 'parent_native_binding_changed')
    known = checked(binding['known']); original = checked(binding['parent_known'])
    require(known['schema'] == 'causes.extended_reader_known.v1'
            and known['parent'] == binding['parent_known'] and known['allocation'] == binding['cohort'],
            'explicit_extended_known_required')
    changed = {'schema', 'allocation', 'allocation_review', 'placement', 'placement_review', 'global_count'}
    require(set(known) == set(original) | {'parent'}, 'known_field_set_changed')
    require(all(exact(known.get(k), v) for k, v in original.items() if k not in changed),
            'native_protocol_binding_changed')
    require(type(known['global_count']) is int
            and known['global_count'] == checked(binding['cohort'])['count'], 'extended_count_changed')
    namespace = dict(vars(parent))
    namespace['__file__'] = str(Path(__file__).resolve())
    namespace['KNOWN'] = copy.deepcopy(parent.KNOWN)
    namespace['KNOWN'].update(materializer=pin(__file__), fresh_known=binding['known'])
    for name, fn in vars(parent).items():
        if isinstance(fn, types.FunctionType) and fn.__globals__ is vars(parent):
            clone = types.FunctionType(fn.__code__, namespace, fn.__name__, fn.__defaults__, fn.__closure__)
            clone.__kwdefaults__ = fn.__kwdefaults__
            namespace[name] = clone
    native_load = namespace['load']

    def load(plan_ref, plan_review_ref, modules):
        plan = checked(plan_ref)
        require(plan['cohort_extension'] == binding_ref and plan['cohort_extension_review'] == review_ref,
                'plan_extension_join')
        prior = validate_history(binding, plan)
        prep = checked(plan['prepared'])
        prior_inputs = checked(prior['prepared'])
        if prior_inputs['schema'] == 'causes.saved_corpus_measurement_inputs.v1':
            require(all(row['measurement_key'].startswith('saved_corpus_')
                        for row in checked(prior_inputs['ready'])), 'saved_predecessor_key_scope')
            prior_inputs = None
        else:
            require(prior_inputs['cohort'] in (binding['cohort'], checked(binding['base_preparation'])['cohort']),
                    'unrelated_fresh_predecessor')
        validate_cumulative(binding, prep, prior_inputs)
        return native_load(plan_ref, plan_review_ref, modules)

    namespace['load'] = load
    return namespace


def prepare_reader(row, prep, config, modules):
    """Delegate exact native snapshot/context/payload reconstruction unchanged."""
    return module(prep['row_materializer']).prepare_reader(row, prep, config, modules)


def bind_for_plan(plan):
    """Explicit entry point for spec-loaded collectors and saved-only auditors."""
    require(plan.get('source') == pin(__file__) and type(plan.get('cohort_extension')) is dict
            and type(plan.get('cohort_extension_review')) is dict, 'plan_extension_required')
    bound = runtime_namespace(plan['cohort_extension'], plan['cohort_extension_review'])
    binding = checked(plan['cohort_extension'])
    require(bound['KNOWN']['fresh_known'] == binding['known'] and plan.get('cohort') == binding['cohort'],
            'consumer_extension_join')
    return types.SimpleNamespace(**bound)


def copied_rows(binding, request, selection, jobs):
    """Resolve a finite accepted copy cut to its exact frozen producer jobs."""
    cut = checked(request['feed_cut'])
    accepted(request['feed_cut_review'], 'accepted_requested_actor_reader_input_cut',
             actual=request['feed_cut'], selection=binding['selection'],
             configuration=cut['configuration'], index=cut['index']['saved'], accepted=cut['accepted'])
    require(cut['selection'] == binding['selection'], 'copy_selection_changed')
    index = cut['index']; body = raw(index['saved'])
    require((not body or body.endswith(b'\n')) and index['bytes'] == len(body)
            and index['sha256'] == hashlib.sha256(body).hexdigest()
            and index['original_path'] == str(Path(cut['destination']) / 'reader_inputs.accepted.jsonl'),
            'copy_index_changed')
    receipts = [json.loads(line) for line in body.splitlines()]
    require(len(receipts) == cut['accepted'], 'copy_count_changed')
    selected = {row['relative_run']: row for row in selection['rows']}
    stages = {(row['role'], row['stage']): row for row in selection['stages']}
    producers = {}
    for item in request['producers']:
        key = (item['role'], item['stage'])
        require(key in stages and key not in producers, 'duplicate_or_foreign_producer')
        p = checked(item['plan']); stage = stages[key]
        flag = ('accepted_requested_fixed8_actor_plan' if item['stage'] == 'first8'
                else 'accepted_ordinal_actor_plan')
        accepted(item['review'], flag, plan=item['plan'], source=p['source'])
        expected = [jobs[r['run_id']]['job'] for r in selection['rows']
                    if (r['provider_role'], r['stage']) == key]
        require(p['runtime_authorized'] is True and p['role'] == item['role']
                and p['output'] == stage['output_root'] and exact(checked(p['jobs']), expected),
                'producer_fixed_jobs_changed')
        producers[key] = (item, p)
    result = {}
    for receipt in receipts:
        relative = receipt['run']
        require(relative in selected and not Path(relative).is_absolute()
                and '..' not in Path(relative).parts and receipt['schema'] == 'caus.reader_input_receipt.v1'
                and receipt['custody_kind'] == 'actor_reader_inputs', 'foreign_copy_receipt')
        row = selected[relative]; run = row['run_id']; key = (row['provider_role'], row['stage'])
        require(run not in result and key in producers, 'copy_duplicate_or_unbound_producer')
        item, p = producers[key]
        require(p['execution_host'] == row['execution_host'], 'producer_execution_host_changed')
        result[run] = dict(job=jobs[run]['job'], role=p['role'], receipt=receipt,
                snapshots_root=cut['destination'], config=checked(p['config']),
                config_reference=p['config'], jobs_reference=p['jobs'],
                feed_cut=request['feed_cut'], feed_cut_review=request['feed_cut_review'],
                feed_plan=item['plan'])
    return result


def materialize(binding_ref, binding_review, request, output, modules):
    binding = checked(binding_ref)
    runtime_namespace(binding_ref, binding_review)
    old, base_ready, base_missing, selection, jobs = cohort(binding)
    previous = checked(request['previous_preparation'])
    accepted(request['previous_review'], 'accepted_fresh_candidate_reader_inputs',
             preparation=request['previous_preparation'], source=previous['source'],
             cohort=previous['cohort'], ready=previous['ready'], self_jobs=previous['self_jobs'],
             unavailable=previous['unavailable'], pending=previous['pending'],
             reader_source=previous['reader_source'])
    if request['previous_preparation'] != binding['base_preparation']:
        validate_cumulative(binding, previous)
    copied = copied_rows(binding, request, selection, jobs)
    old_rows = unique(checked(previous['ready']) + checked(previous['unavailable']),
                      'run_id', 'duplicate_previous_input')
    ready, unavailable, pending = list(base_ready), list(base_missing), []
    native = module(binding['row_materializer']); known = checked(binding['known'])
    for run in jobs:
        if run in old_rows:
            require(run in copied, 'previous_copy_removed')
            row = old_rows[run]
            receipt = copied[run]['receipt']
            require(exact(row['original_binding']['snapshot_receipt'], receipt),
                    'previous_copy_replaced')
        elif run in copied:
            row = native.bind_row(copied[run], known, modules)
        else:
            pending.append(dict(run_id=run, stage='early', measurement_key=measurement_key(run),
                disposition='pending', reason='no_accepted_reader_input_copy_at_cut'))
            continue
        (ready if row['disposition'] == 'prepared' else unavailable).append(row)
    refs = {name: save(output / (name.upper() + '.v1.json'), rows)
            for name, rows in [('ready', ready), ('unavailable', unavailable),
                               ('pending', pending), ('self_jobs', [])]}
    prep = dict(schema='causes.fresh_candidate_reader_inputs.v1', source=pin(__file__),
        known=binding['known'], cohort=binding['cohort'], cohort_extension=binding_ref,
        cohort_extension_review=binding_review, row_materializer=binding['row_materializer'],
        previous_preparation=request['previous_preparation'], previous_review=request['previous_review'],
        feed_cut=request['feed_cut'], feed_cut_review=request['feed_cut_review'], **refs,
        **{k: old[k] for k in ('reader_source', 'checker_config', 'prompts', 'prompts_directory')},
        counts=dict(selected=len(base_ready) + len(base_missing) + len(jobs),
            copied=len(ready) + len(unavailable), reader_ready=len(ready), reader_requests=3 * len(ready),
            unavailable=len(unavailable), pending=len(pending), self_requests=0),
        original_allocations_unchanged=True, treatments_preserved=True,
        payloads_contain_no_analysis_labels=True, pending_is_not_missing=True,
        cumulative_ready_requires_closed_history_filter=True, runtime_authorized=False, model_calls=0)
    validate_cumulative(binding, prep, previous)
    return save(output / 'PREPARATION.v1.json', prep)


def prepare_main(request_path, authority_path):
    reference = pin(request_path); request = checked(reference)
    require(request['source'] == pin(__file__) and request['root_authorized'] is True,
            'materialization_not_authorized')
    bound = runtime_namespace(request['binding'], request['binding_review'])
    modules = bound['native_modules']()
    authority = modules['campaign_handoff'].Authority(pin(authority_path),
        'prepare_extended_reader_inputs', tuple(Path(x) for x in request['pause_paths']),
        lambda v: v.get('request') == reference and v.get('source') == request['source']
        and v.get('output') == request['output'])
    authority.check(); output = Path(request['output'])
    require(output.is_absolute() and output.resolve() == output and not output.exists(),
            'fresh_output_required')
    output.mkdir()
    save(output / 'INTENT.v1.json', dict(request=reference, authority=pin(authority_path)))
    print(json.dumps(materialize(request['binding'], request['binding_review'], request, output, modules)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare-request')
    parser.add_argument('--prepare-authority')
    parser.add_argument('--binding')
    parser.add_argument('--binding-review')
    args, native_args = parser.parse_known_args()
    if args.prepare_request:
        require(args.prepare_authority and not native_args and not args.binding and not args.binding_review,
                'materialization_arguments')
        prepare_main(args.prepare_request, args.prepare_authority)
        return
    require(args.binding and args.binding_review and not args.prepare_authority, 'runtime_binding_required')
    bound = runtime_namespace(pin(args.binding), pin(args.binding_review))
    sys.argv = [sys.argv[0], *native_args]
    bound['main']()


if __name__ == '__main__':
    main()
