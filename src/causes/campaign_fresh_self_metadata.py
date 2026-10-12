"""Plan saved fresh-self index imports without reading per-run payloads."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from causes import campaign_reader_watch as watch
from causes import campaign_snapshot as snapshot

SELF = ('evaluation_presence', 'evaluation_strongly_favors', 'evaluation_leans_towards_or_favors')
FORBIDDEN = {'MANIFEST.json', 'VERIFIED.json', 'summary.json', 'actor_summary.json', 'context.json',
             'initial_context.json', 'early_context.json', 'end_context.json', 'response.raw',
             'parsed.json', 'result.json', 'request.json', 'receipt.json', 'attempt.json'}
PRODUCER_FLAGS = ('accepted_individual_variant_mi_runtime_plan', 'accepted_individual_variant_ny_runtime_plan',
                  'accepted_individual_variant_texas_runtime_plan', 'accepted_future_texas_ny_subset_runtime_plan')


def require(value, message):
    if not value:
        raise ValueError(message)


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def saved_bytes(ref):
    path = snapshot.canonical_path(ref['path'])
    require(path.name not in FORBIDDEN and 'probes' not in path.parts, 'per_run_read_forbidden')
    return watch.checked_pin(ref)


def load(ref):
    return json.loads(saved_bytes(ref))


def accepted(ref, flag, actual):
    peer = load(ref)
    require(peer.get(flag) is True and peer.get('actual') == actual and peer.get('findings', []) == [],
            'accepted_metadata_join:' + flag)
    return peer


def slots(run):
    return {(run, 'self_' + name) for name in SELF}


def feed(descriptor, profile, logical_role, execution_host):
    """Normalize one independently accepted native cut; no new startup is asserted."""
    actual = load(descriptor['actual']); peer = load(descriptor['review'])
    require(peer.get('actual') == descriptor['actual'] and peer.get('findings', []) == [], 'feed_peer_join')
    startup_flag = ('accepted_individual_variant_reader_feed_startup' if profile == 'reader'
                    else 'accepted_individual_variant_self_probe_feed_startup_actual')
    entry = descriptor.get('entry')
    if entry is not None:
        require(entry == logical_role+'_'+profile and execution_host == logical_role, 'aggregate_role_identity')
        require(peer.get('accepted_local_copy_progress_cut') is True and actual['errors'] == [], 'aggregate_cut')
        native = actual['feeds'][entry]
        require(native['errors'] == [] and native['trailing_partial_bytes'] == 0
                and native['prior_prefix_retained'] is True and native['startup_prefix_retained'] is True,
                'aggregate_feed_custody')
        startup = load(native['startup']); accepted(native['startup_review'], startup_flag, native['startup'])
        require(startup['plan'] == native['plan'], 'aggregate_startup_plan')
        index = native['index']; plan_ref = native['plan']; count = native['index_rows']
        observed = native['observed_at']
    else:
        flags = (startup_flag, 'accepted_individual_variant_reader_feed_index_cut') if profile == 'reader' else (startup_flag, 'accepted_individual_variant_self_probe_feed_index_cut')
        require(any(peer.get(flag) is True for flag in flags), 'feed_cut_not_accepted')
        native = actual; index = actual['index']['saved']; plan_ref = actual['plan']; count = actual['accepted']
        observed = actual.get('observed_at', actual.get('at', actual.get('finished_at')))
    require(type(observed) is str and bool(observed), 'feed_observed_time_required')
    plan = load(plan_ref); registration_ref = plan['registration']; registration = load(registration_ref)
    if 'registration' in native:
        require(native['registration'] == registration_ref, 'feed_registration_join')
    if 'logical_role' in registration or 'execution_host' in registration:
        require(registration['logical_role'] == logical_role and registration['execution_host'] == execution_host,
                'registered_role_identity')
    else:
        require(entry is not None, 'standalone_role_registration_required')
    raw = saved_bytes(index)
    require(raw and raw.endswith(b'\n'), 'incomplete_index')
    rows = [json.loads(line) for line in raw.splitlines()]
    by = {Path(row['run']).name: row for row in rows}
    require(len(by) == len(rows) == count and set(by) <= set(registration['selected_run_ids']), 'index_membership')
    view = dict(schema='causes.individual_variant_saved_feed_cut_projection.v1', profile=profile,
                capture_kind='dated_saved_cut', source_actual=descriptor['actual'], source_review=descriptor['review'],
                source_entry=entry, observed_at=observed, plan=plan_ref, registration=registration_ref,
                logical_role=logical_role, execution_host=execution_host,
                source=plan['source'], index=dict(saved=index, bytes=index['bytes'], sha256=index['sha256']),
                accepted=count, remaining=len(registration['selected_run_ids'])-count,
                current_liveness_claimed=False, new_startup_claimed=False, scientific_payloads_read=False,
                new_observations=0, model_calls=0, errors=[])
    return view, by, registration


def historical_groups(accounting, occupied):
    """Reuse an accepted supplement proof, retaining exact identities instead of a count exemption."""
    result_ref = accounting['accepted_supplement_result']; result = load(result_ref)
    peer = accepted(accounting['accepted_supplement_review'], 'accepted_historical_extra_self_measurement_readout', result_ref)
    require(peer['source'] == result['source'] and peer['manifest'] == result['manifest'], 'supplement_result_join')
    manifest = load(result['manifest']); member_ref = accounting['supplement_membership']; member = load(member_ref)
    require(len(manifest['parts']) == 1 and manifest['parts'][0]['membership'] == member_ref, 'supplement_membership_join')
    part = manifest['parts'][0]
    require(member['kind'] == 'supplement_phase' and member['actual'] == part['actual'] == accounting['phase']
            and member['review'] == part['review'] == accounting['phase_review'], 'supplement_phase_join')
    accepted(accounting['phase_review'], 'accepted_saved_corpus_self_phase_cut', accounting['phase'])
    runs = set(member['terminal_run_ids']); ids = set(member['terminal_measurement_ids'])
    require(len(runs) == len(member['terminal_run_ids']) == part['count']
            and len(ids) == len(member['terminal_measurement_ids']) == 2*len(runs), 'supplement_unique_membership')
    require(len(manifest['rows']) == len(ids) and {row['measurement_id'] for row in manifest['rows']} == ids
            and {row['run_id'] for row in manifest['rows']} == runs, 'supplement_row_membership')
    require(all(row['source_kind'] == 'historical_extra_self' and row['custody']['membership'] == member_ref
                and row['custody']['actual'] == part['actual'] and row['custody']['review'] == part['review']
                for row in manifest['rows']), 'supplement_row_custody')
    extra_slots = {(run, 'self_'+question) for run in runs for question in SELF[1:]}
    require({tuple(key) for key in manifest['new_slot_keys']} == extra_slots <= occupied, 'supplement_occupied_slots')
    require(result['new_physical_measurements'] == peer['counts']['new_physical_measurements'] == len(extra_slots),
            'supplement_import_count')
    require(set(accounting['exact_supplement_run_ids']) == runs
            and {tuple(key) for key in accounting['exact_supplement_slots']} == extra_slots, 'supplement_accounting_ids')
    unavailable = set(accounting['historical_unavailable_run_ids'])
    require(len(unavailable) == len(accounting['historical_unavailable_run_ids']) and not runs & unavailable,
            'historical_group_overlap')
    return runs, unavailable


def plan(descriptor_ref):
    """Return deterministic artifacts for an ordered list of native producer pairs."""
    descriptor = load(descriptor_ref)
    require(descriptor['schema'] == 'causes.fresh_self_metadata_descriptor.v1', 'descriptor_schema')
    output = snapshot.canonical_path(descriptor['output'])
    base = load(descriptor['base_result'])
    accepted(descriptor['base_review'], 'accepted_incremental_measurement_readout', descriptor['base_result'])
    manifest = load(base['manifest']); raw_slots = manifest['occupied_slot_keys']
    require(isinstance(raw_slots, list) and all(isinstance(key, list) and len(key) == 2
            and all(type(value) is str and value for value in key) for key in raw_slots)
            and type(base['physical_measurements']) is int, 'base_slot_schema')
    occupied = {tuple(key) for key in raw_slots}
    require(raw_slots == [list(key) for key in sorted(occupied)] and len(occupied) == base['physical_measurements'],
            'base_slot_identity')
    parent = load(descriptor['previous_preparation']); pp = load(descriptor['previous_review'])
    require(pp.get('accepted_fresh_self_incremental_metadata') is True
            and pp['preparation'] == descriptor['previous_preparation'] and pp.get('findings', []) == [], 'previous_metadata_join')
    previous = {key: load(ref) for key, ref in parent['binding_proofs'].items()}
    prior_fresh = set()
    for proof in previous.values():
        ids = proof['ordered_intersection']
        require(len(ids) == len(set(ids)) and not prior_fresh.intersection(ids), 'previous_group_duplicate')
        prior_fresh.update(ids)
    accounting = parent['historical_supplement_accounting']
    supplement, unavailable = historical_groups(accounting, occupied)
    complete = {run for run, name in occupied if name == 'self_'+SELF[0] and slots(run) <= occupied}
    require(not prior_fresh.intersection(supplement | unavailable)
            and complete == prior_fresh | supplement | unavailable, 'complete_prior_group_union')
    materialize_template = load(descriptor['materialize_template']); readout_template = load(descriptor['readout_template'])
    require(materialize_template['source'] == parent['builder'] and readout_template['source'] == parent['importer'],
            'native_sources_changed')
    require(materialize_template['source_review'] == parent['builder_review']
            and readout_template['source_review'] == parent['importer_review'], 'native_source_reviews_changed')
    require(materialize_template['known'] == readout_template['known'] == parent['known'], 'native_known_changed')
    require(len(materialize_template['parts']) == 1 and materialize_template['parts'][0]['source_kind'] == 'fresh',
            'one_fresh_part_required')
    require(materialize_template['root_materialization_authorized'] is False
            and readout_template['root_readout_authorized'] is False
            and readout_template['manifest'] is None, 'pending_native_authority_required')
    require(all(q[key] is None for q in (materialize_template, readout_template)
                for key in ('base_result', 'base_review', 'base_source')), 'pending_base_required')
    runner = materialize_template['parts'][0]['runner_source']; watch.checked_pin(runner)
    artifacts = {}; proofs = {}; views = {}; requests = {}; counts = {}; all_old = set(); all_new = set(); seen = set()
    native_outputs = set(); active = []; zero = []; expected_total = len(occupied)
    def add(name, value):
        require(name not in artifacts, 'artifact_collision')
        data = encoded(value); artifacts[name] = value
        return dict(path=str(output/name), bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    pairs = descriptor['pairs']
    require(isinstance(pairs, list) and pairs, 'producer_pairs_required')
    names = [pair['id'] for pair in pairs]
    require(len(names) == len(set(names)) and all(type(name) is str and name and Path(name).name == name
                                                and name not in ('.', '..') for name in names), 'pair_names')
    require(set(previous) <= set(names), 'previous_pair_missing')
    for pair in pairs:
        name = pair['id']; role = pair['logical_role']; host = pair['execution_host']
        require(role in ('mi', 'ny', 'texas') and host in ('mi', 'ny', 'texas'), 'producer_role')
        sv, self_rows, sr = feed(pair['self'], 'self', role, host)
        rv, reader_rows, rr = feed(pair['reader'], 'reader', role, host)
        require(sr['producer_plan'] == rr['producer_plan'] and sr['producer_plan_review'] == rr['producer_plan_review']
                and sr['selected_run_ids'] == rr['selected_run_ids'], 'producer_pair_identity')
        producer = load(sr['producer_plan']); producer_peer = load(sr['producer_plan_review'])
        require(any(producer_peer.get(flag) is True for flag in PRODUCER_FLAGS)
                and producer_peer['plan'] == sr['producer_plan'] and producer_peer['source'] == producer['source'],
                'producer_plan_peer')
        if name in previous:
            prior = previous[name]
            require(prior['producer_plan'] == sr['producer_plan']
                    and prior['producer_plan_review'] == sr['producer_plan_review']
                    and prior['jobs'] == producer['jobs'] and prior['config'] == producer['config']
                    and prior['runner_source'] == runner, 'previous_producer_identity')
            for profile, view in (('self', sv), ('reader', rv)):
                old_index = saved_bytes(prior[profile+'_index'])
                require((not old_index or old_index.endswith(b'\n'))
                        and saved_bytes(view['index']['saved']).startswith(old_index), 'previous_index_prefix')
        ordered = [run for run in self_rows if run in reader_rows]
        require(not seen.intersection(ordered), 'duplicate_producer_group'); seen.update(ordered)
        old = []; new = []
        for run in ordered:
            present = slots(run) & occupied
            require(not present or present == slots(run), 'partly_occupied_group')
            require(run not in supplement | unavailable, 'fresh_historical_overlap')
            (old if present else new).append(run)
        require(old == previous.get(name, {}).get('ordered_intersection', []), 'previous_pair_order_or_membership')
        all_old.update(old); all_new.update(new)
        counts[name] = dict(self_runs=len(self_rows), reader_runs=len(reader_rows), intersection_runs=len(ordered),
                            old_occupied_runs=len(old), new_runs=len(new), new_measurement_slots=3*len(new),
                            deferred_missing_reader=len(set(self_rows)-set(reader_rows)),
                            reader_only=len(set(reader_rows)-set(self_rows)))
        refs = {profile: add(name+'/'+profile+'.ACTUAL.v1.json', value) for profile,value in [('self',sv),('reader',rv)]}
        views[name] = refs
        proof = dict(schema='causes.fresh_self_saved_index_membership_preparation.v1', producer_id=name,
                     logical_role=role, execution_host=host,
                     producer_plan=sr['producer_plan'], producer_plan_review=sr['producer_plan_review'],
                     jobs=producer['jobs'], config=producer['config'], runner_source=runner,
                     reader_index=rv['index']['saved'], self_index=sv['index']['saved'],
                     ordered_intersection=ordered, old_occupied_run_ids=old, new_run_ids=new, counts=counts[name],
                     deferred_missing_reader_run_ids=[run for run in self_rows if run not in reader_rows],
                     reader_only_run_ids=[run for run in reader_rows if run not in self_rows],
                     new_index_receipts=[dict(run_id=run,self_receipt=self_rows[run],reader_receipt=reader_rows[run]) for run in new],
                     mask_base_result=descriptor['base_result'], mask_base_review=descriptor['base_review'],
                     mask_base_manifest=base['manifest'], per_run_identity_proof_prepared=False,
                     accepted_previous_binding=parent['binding_proofs'].get(name), projections=refs)
        proofs[name] = add(name+'/INDEX_BINDING.v1.json', proof)
        q = copy.deepcopy(materialize_template); r = copy.deepcopy(readout_template)
        for document, key in [(q, 'manifest_output'), (r, 'readout_output')]:
            path = snapshot.canonical_path(pair[key]); require(path not in native_outputs and not path.exists(), 'native_output_collision')
            require(not path.is_relative_to(output) and not output.is_relative_to(path), 'native_metadata_output_overlap')
            native_outputs.add(path); document['output'] = str(path)
        q['parts'][0].update(actual=refs['self'], review=None, reader_actual=refs['reader'], reader_review=None)
        requests[name] = dict(materialize=add(name+'/REQUEST.materialize.pending.v1.json',q),
                              readout=add(name+'/REQUEST.readout.pending.v1.json',r),
                              previous_readout_required=(descriptor['base_result']['path'] if not active else
                                  requests[active[-1]]['readout_output']+'/RESULT.v1.json'),
                              readout_output=r['output'], prospective_base_measurements=expected_total)
        expected_total += counts[name]['new_measurement_slots']
        requests[name]['prospective_total_measurements'] = expected_total
        (active if new else zero).append(name)
    require(all_old == prior_fresh and all_new.isdisjoint(all_old), 'fresh_group_union')
    skipped = add('ZERO_DELTA_SKIPS.v1.json', dict(base_result=descriptor['base_result'], base_review=descriptor['base_review'],
                   producers=zero, bindings={name:proofs[name] for name in zero},
                   counts={name:counts[name] for name in zero}, native_imports=0))
    preparation = dict(schema='causes.fresh_self_generic_metadata_preparation.v1', descriptor=descriptor_ref,
                       previous_preparation=descriptor['previous_preparation'], previous_review=descriptor['previous_review'],
                       mask_base_result=descriptor['base_result'], mask_base_review=descriptor['base_review'],
                       mask_base_manifest=base['manifest'], historical_supplement_accounting=accounting,
                       builder=parent['builder'], builder_review=parent['builder_review'], importer=parent['importer'],
                       importer_review=parent['importer_review'], known=parent['known'], binding_proofs=proofs,
                       projections=views, requests=requests, producers=counts, active_import_order=active,
                       required_projection_reviews={name:{profile:dict(
                           accepted_flag=('accepted_individual_variant_self_probe_feed_startup_actual' if profile == 'self'
                                          else 'accepted_individual_variant_reader_feed_startup'),
                           actual=views[name][profile], source_actual=pair[profile]['actual'],
                           source_review=pair[profile]['review'], plan=artifacts[name+'/'+profile+'.ACTUAL.v1.json']['plan'])
                           for profile in ('self','reader')} for name,pair in zip(names,pairs)},
                       zero_delta_skips=skipped, old_fresh_groups=len(all_old), new_fresh_groups=len(all_new),
                       new_measurement_slots=3*len(all_new), prospective_total_measurements=expected_total,
                       root_authorized=False, model_calls=0, host_calls=0, native_invocations=0, per_run_payload_reads=0)
    add('PREPARATION.v1.json', preparation)
    return artifacts


def write(descriptor_ref):
    artifacts = plan(descriptor_ref); descriptor = load(descriptor_ref)
    output = snapshot.canonical_path(descriptor['output'])
    require(not output.exists(), 'metadata_output_exists')
    output.mkdir()
    for name, value in artifacts.items():
        path = output/name; path.parent.mkdir(exist_ok=True)
        with path.open('xb') as stream:
            stream.write(encoded(value))
    path = output/'PREPARATION.v1.json'; raw = path.read_bytes()
    return dict(path=str(path),bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--descriptor', type=json.loads, required=True)
    parser.add_argument('--write', action='store_true', help='Exclusively emit metadata, never native imports')
    args = parser.parse_args(argv)
    result = write(args.descriptor) if args.write else plan(args.descriptor)['PREPARATION.v1.json']
    print(json.dumps(result,sort_keys=True,allow_nan=False))


if __name__ == '__main__':
    main()
