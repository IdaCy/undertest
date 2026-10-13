"""Import completed reader batches through their frozen evidence verifier."""
import argparse
import collections
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

def pin(path):
    path = Path(path)
    assert path.is_absolute() and path.resolve() == path and not path.is_symlink()
    raw = path.read_bytes()
    return dict(path=str(path), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def ck(ref):
    assert pin(ref['path']) == ref
    return json.loads(Path(ref['path']).read_bytes())


def save(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write('\n')
    return pin(path)


def load(ref, name):
    assert pin(ref['path']) == ref
    spec = importlib.util.spec_from_file_location(name, ref['path'])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    assert pin(module.__file__) == ref
    return module


def select_closed_rows(preparation, ready, plan, closed, events, kind):
    """Return only this batch's terminal rows, preserving unresolved attempts."""
    def require(value, reason):
        if not value:
            raise ValueError(reason)

    require(kind in ('fixed', 'additive'), 'input_kind')
    by = {row['measurement_key']: row for row in ready}
    require(len(ready) == len(by) == len({r['run_id'] for r in ready}),
            'duplicate_prepared_identity')
    require(all(row['stage'] == 'early' for row in ready), 'unsupported_stage')
    counts = preparation['counts']
    require(all(type(value) is int and value >= 0 for value in counts.values()),
            'invalid_prepared_count')
    require(counts['reader_ready'] == len(ready), 'prepared_reader_count')
    if kind == 'fixed':
        require(counts['selected'] == counts['context_ready'] == len(ready)
                and counts['unavailable'] == 0
                and closed['assigned'] == len(ready), 'fixed_prepared_count')
        prior = []
    else:
        require(counts['copied'] == len(ready) + counts['unavailable']
                and closed['assigned'] == counts['copied']
                and counts['selected'] == counts['copied'] + counts['pending']
                and counts['reader_requests'] == 3 * len(ready)
                and counts['self_requests'] == 0, 'additive_prepared_count')
        prior = plan['previous_cohort_claimed_keys']
        require(len(prior) == len(set(prior)) and set(prior) <= set(by),
                'prior_claims_not_in_preparation')
    n = closed['claimed']
    require(type(n) is int and n > 0 and n == closed['ready']
            == closed['finished_or_unresolved'], 'closed_count')
    require(closed['active'] == closed['unclaimed'] == 0
            and closed['unclaimed_keys'] == []
            and closed['all_claims_accounted'] is True, 'incomplete_batch')
    require(all(closed[k] is None for k in ('owned_pause', 'stop_reason', 'quarantine')),
            'abnormal_batch_closure')
    claims = [r['key'] for r in events if r['event'] == 'claimed']
    terminals = [r for r in events if r['event'] in
                 ('finished', 'unresolved_current_reader_attempt')]
    keys = [r['key'] for r in terminals]
    require(len(claims) == len(set(claims)) == len(keys) == len(set(keys)) == n
            and set(claims) == set(keys)
            and closed['claimed_keys'] == claims, 'terminal_accounting')
    require(set(claims).isdisjoint(prior) and set(claims) | set(prior) == set(by),
            'prepared_claim_partition')
    require(events and events[-1]['event'] == 'finite_selection_complete'
            and events[-1]['new_admissions_stopped'] is True
            and events[-1]['reason'] is None,
            'natural_completion_required')
    if kind == 'additive':
        require(plan['expected_unclaimed_contexts'] == n, 'planned_batch_count')
    return {key: by[key] for key in claims}, terminals


def run(request_ref):
    request = ck(request_ref)
    assert request['source'] == pin(__file__) and request['root_authorized'] is True
    assert request['model_calls_authorized'] is request['analysis_authorized'] is False
    peer = ck(request['source_review'])
    assert peer['accepted_closed_reader_import_source'] is True
    assert peer['source'] == request['source'] and peer['findings'] == []
    bindings = ck(request['bindings'])
    binding_peer = ck(request['bindings_review'])
    assert binding_peer['accepted_closed_reader_import_bindings'] is True
    assert binding_peer['source'] == request['source']
    assert binding_peer['source_review'] == request['source_review']
    assert binding_peer['bindings'] == request['bindings']
    assert binding_peer['findings'] == []
    prep = ck(bindings['prepared'])
    prep_peer = ck(bindings['prepared_review'])
    kind = bindings['input_kind']
    flags = {'fixed': 'accepted_saved_corpus_measurement_inputs',
             'additive': 'accepted_fresh_candidate_reader_inputs'}
    assert kind in flags and prep_peer[flags[kind]] is True
    assert prep_peer['preparation'] == bindings['prepared'] and prep_peer['findings'] == []
    ready = ck(prep['ready'])
    if kind == 'fixed':
        cohort = ck(prep['cohort'])
        cohort_peer = ck(prep['cohort_review'])
        assert cohort_peer['accepted_original_candidate_measurement_cohort'] is True
        assert cohort_peer['cohort'] == prep['cohort'] and cohort_peer['findings'] == []
        assert [row['run_id'] for row in ready] == cohort['selected_run_ids']
    closed = ck(request['closure'])
    closed_peer = ck(request['closure_review'])
    assert closed_peer['accepted_finite_reader_route_closed_cut'] is True
    assert closed_peer['actual'] == request['closure'] and closed_peer['findings'] == []
    for name in ('plan', 'plan_review', 'prepared', 'reader_source', 'journal',
                 'runtime_result', 'complete', 'execution', 'native_failure_events'):
        assert closed_peer[name] == closed[name]
    assert closed['plan'] == bindings['plan'] and closed['prepared'] == bindings['prepared']
    plan = ck(bindings['plan'])
    plan_peer = ck(closed['plan_review'])
    assert plan_peer['plan'] == bindings['plan'] and plan_peer['findings'] == []
    assert plan['prepared'] == bindings['prepared']
    assert plan['prepared_review'] == bindings['prepared_review']
    raw = Path(closed['journal']['path']).read_bytes()
    assert pin(closed['journal']['path']) == closed['journal'] and raw.endswith(b'\n')
    events = [json.loads(line) for line in raw.splitlines()]
    by, terminals = select_closed_rows(prep, ready, plan, closed, events, kind)
    count = len(terminals)
    assignments = ck(bindings['assignments'])
    assignment_peer = ck(bindings['assignment_review'])
    assert assignment_peer['accepted_requested_39_contrast_assignments'] is True
    assert assignment_peer['assignments'] == bindings['assignments'] and assignment_peer['findings'] == []
    assert set(row['run_id'] for row in by.values()) <= set(assignments['epochs'])
    sources = ck(bindings['native_preparation'])
    desc_ref = sources['descriptors']['mi']['readout']
    desc = ck(desc_ref)
    entry = load(desc['entrypoint'], 'historical_reader_import_entry')
    settings, loader, constructor_binding = entry.configuration(desc_ref)
    read = entry.module(settings['source'], 'historical_reader_import_native')
    known = ck(sources['known'])
    with loader.bind_constructor(read, settings['source'], constructor_binding) as resolved:
        bundle = read.native(known)
    api = bundle['api']
    assert api.module_pin(bundle['modules']['campaign_reader']) == prep['reader_source']
    config = ck(prep['checker_config'])
    output = Path(request['output'])
    assert output.is_absolute() and output.resolve() == output and not output.exists()
    output.mkdir()
    intent = save(output / 'INTENT.v1.json', dict(request=request_ref, started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), model_calls=0, host_calls=0, original_writes=0))
    membership = save(output / 'MEMBERSHIP.v1.json', dict(actual=request['closure'], review=request['closure_review'], terminal_run_ids=[by[row['key']]['run_id'] for row in terminals], terminal_events=terminals))
    refs = {}

    def add(ref):
        assert refs.setdefault(ref['path'], ref) == ref

    for ref in [request['closure'], request['closure_review'], membership, prep['ready'], prep['checker_config'], *prep['prompts'].values()]:
        add(ref)
    rows = []
    for terminal in terminals:
        prepared = by[terminal['key']]
        for ref in prepared['original_refs'].values():
            add(ref)
        folder = Path(closed['root']) / terminal['key']
        row = dict(run_id=prepared['run_id'], source_kind=('historical_main_reader' if kind == 'fixed' else 'fresh_candidate_reader'), stage='early', instruments=list(api.READER), measurement_key=terminal['key'], root=str(folder), overlay_manifest=prep['ready'], prepared_row_sha256=api.digest(prepared), custody=dict(actual=request['closure'], review=request['closure_review'], accepted_flag='accepted_finite_reader_route_closed_cut', closed_run_id=prepared['run_id'], terminal=True, membership=membership))
        if terminal['event'] == 'finished':
            names = ['context.json', 'config.json', 'RESULT.json']
            for instrument in api.READER:
                names += [instrument + '/' + name for name in ('request.json', 'schema.json', 'attempt.json', 'receipt.json', 'result.json')]
                names += [instrument + '/' + name for name in ('response.raw', 'parsed.json') if (folder / instrument / name).exists()]
            for name in names:
                add(pin(folder / name))
            row['result'] = pin(folder / 'RESULT.json')
        rows.append((row, prepared, terminal))
    native_known = ck(known['api_known'])
    source_refs = [settings['source'], known['api'], native_known['reader_verifier'], native_known['labels'], prep['reader_source'], desc['loader_source'], desc['binding'], *(item['physical'] for item in resolved)]
    destination = output / 'verified_slots_v1'
    guard = read.ReadGuard(list(refs.values()), destination, source_refs, [])
    sys.addaudithook(guard.audit)
    destination.mkdir()
    slots = []
    try:
        for row, prepared, terminal in rows:
            if terminal['event'] == 'unresolved_current_reader_attempt':
                api.custody(row, guard)
                values = [api.slot(row['run_id'], 'reader', instrument, 'missing', None, 'unresolved_current_reader_attempt', dict(custody=row['custody'], terminal=terminal)) for instrument in api.READER]
            else:
                values = api.bind_reader(row, prepared=prepared, preparation=prep, config=config, modules=bundle['modules'], materializer=bundle['materializer'], verifier=bundle['reader'], labels=bundle['labels'], record=guard)
                result = read.read_json(guard, row['result'])
                assert terminal['instrument_statuses'] == {name: result['instruments'][name]['status'] for name in api.READER}
            assert len(values) == 3
            slots.extend(dict(value, epoch=assignments['epochs'][value['run_id']]) for value in values)
        assert len(slots) == len({(row['run_id'], row['readout']) for row in slots}) == 3 * count
        guard.finish()
        slots_ref = save(destination / 'SLOTS.v1.json', slots)
        guard.finish()
    finally:
        guard.active = False
    counts = collections.Counter((row['readout'], row['status'], row['reason']) for row in slots)
    result = dict(schema='causes.closed_reader_verified_slots.v1', finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), source=pin(__file__), bindings=request['bindings'], intent=intent, closure=request['closure'], closure_review=request['closure_review'], assignments=bindings['assignments'], membership=membership, native_source_descriptor=desc_ref, source_relocations=resolved, slots=slots_ref, counts=[dict(readout=readout, status=status, reason=reason, count=count) for (readout, status, reason), count in sorted(counts.items(), key=lambda item: str(item[0]))], occupied=3 * count, named_files_verified=len(guard.identities), model_calls=0, host_calls=0, original_writes=0, claimed_contexts=count, retained_prepared_contexts=len(ready)-count, analysis_performed=False, native_parser_reconstructed=True, failures_preserved=True)
    return save(output / 'RESULT.v1.json', result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    print(json.dumps(run(pin(args.request)), sort_keys=True))
