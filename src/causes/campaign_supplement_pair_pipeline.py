"""Project complete supplement pairs from independently accepted native closures.

Original native verification rows are combined with an explicit origin/index map.
No native verifier, parser, queue, provider or original-output writer is invoked.
Study data and source locations enter through reviewed pinned inputs.
"""
from pathlib import Path
import argparse
import copy
import datetime
import hashlib
import importlib.util
import json
import sys

import ijson


def require(value, reason):
    if not value:
        raise ValueError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def same(left, right):
    return encoded(left) == encoded(right)


def pin(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path
            and not any(p.is_symlink() for p in (path, *path.parents)), 'canonical_path')
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return dict(path=str(path), bytes=path.stat().st_size, sha256=digest)


def checked(reference):
    require(same(pin(reference['path']), reference), 'pin_changed')
    return json.loads(Path(reference['path']).read_bytes())


def accepted(get, reference, flag, **joins):
    peer = get(reference)
    require(peer.get(flag) is True and peer.get('findings') == []
            and all(same(peer.get(key), value) for key, value in joins.items()), flag)
    return peer


def module(reference, name):
    require(same(pin(reference['path']), reference), 'source_changed')
    spec = importlib.util.spec_from_file_location(name, reference['path'])
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    require(same(pin(value.__file__), reference), 'loaded_source_changed')
    return value


def references(value):
    if type(value) is dict:
        if set(value) == {'path', 'bytes', 'sha256'}:
            yield value
        else:
            for item in value.values():
                yield from references(item)
    elif type(value) is list:
        for item in value:
            yield from references(item)


def unique(rows, key, reason):
    result = {}
    for row in rows:
        identity = row[key]
        require(type(identity) is str and identity and identity not in result, reason)
        result[identity] = row
    return result


def validate_membership(data, get):
    """Validate fixed jobs and complete saved terminal membership without labels."""
    jobs = get(data['global_jobs'])
    by_id = unique(jobs, 'measurement_id', 'duplicate_global_jobs')
    membership = get(data['membership'])
    rows = membership['rows']
    require(type(rows) is list and rows, 'empty_terminal_membership')
    members = unique(rows, 'measurement_id', 'duplicate_terminal_membership')
    require(same(membership['global_jobs'], data['global_jobs'])
            and same(membership['assignments'], data['assignments']), 'membership_inputs')
    require(membership['measurement_ids'] == [row['measurement_id'] for row in rows],
            'membership_order')
    require([job['measurement_id'] for job in jobs if job['measurement_id'] in members]
            == membership['measurement_ids'], 'global_order_changed')
    assignments = get(data['assignments'])
    accepted(get, data['assignment_review'], data['assignment_acceptance_flag'],
             assignments=data['assignments'])
    parts = unique(data['parts'], 'role', 'duplicate_parts')
    terminal_ids = set()
    for role, part in parts.items():
        plan = get(part['plan'])
        accepted(get, part['plan_review'], part['plan_acceptance_flag'],
                 source=plan['source'], plan=part['plan'])
        cut = get(part['cut'])
        accepted(get, part['cut_review'], part['cut_acceptance_flag'], actual=part['cut'])
        saved = cut['roles'][role]
        require(same(saved['plan'], part['plan']) and same(saved['jobs'], part['jobs'])
                and same(saved['journal_prefix'], part['journal'])
                and same(plan['jobs'], part['jobs']) and same(plan['constructor'], part['constructor'])
                and plan['output_root'] == part['output_root']
                and plan['endpoint'] == part['endpoint'], 'part_cut_plan_identity')
        local_jobs = get(part['jobs'])
        require(len(unique(local_jobs, 'measurement_id', 'duplicate_part_jobs')) == len(local_jobs)
                and all(same(by_id.get(job['measurement_id']), job) for job in local_jobs),
                'part_global_job_identity')
        for terminal in saved['terminal_events']:
            mid = terminal['measurement_id']
            require(mid not in terminal_ids and mid in members, 'terminal_partition')
            terminal_ids.add(mid)
            member = members[mid]
            job = by_id[mid]
            ordinal = member['global_job_ordinal']
            local_index = member['job_index']
            require(type(ordinal) is int and 0 <= ordinal < len(jobs)
                    and same(jobs[ordinal], job) and type(local_index) is int
                    and 0 <= local_index < len(local_jobs)
                    and same(local_jobs[local_index], job), 'job_ordinals')
            require(member['role'] == role and same(member['jobs'], part['jobs'])
                    and member['endpoint'] == part['endpoint']
                    and same(member['terminal'], terminal), 'terminal_membership_identity')
            for key in ('run_id', 'stage', 'question_id', 'context', 'frozen_config', 'original_refs'):
                require(same(member[key], job[key]), 'frozen_job_identity:' + key)
            require(member['global_job_sha256'] == hashlib.sha256(encoded(job)).hexdigest()
                    and same(get(member['input']), job)
                    and member['epoch'] == assignments['epochs'][job['run_id']]
                    and member['readout'] == 'self_' + job['question_id'], 'input_epoch_identity')
        require(type(part['terminal_count']) is int
                and len(saved['terminal_events']) == part['terminal_count'], 'terminal_count')
    require(terminal_ids == set(members), 'extra_or_missing_terminal')
    active_ids = {row['measurement_id'] for row in membership['active_rows']}
    require(not active_ids & terminal_ids, 'active_is_not_terminal')
    return jobs, membership, assignments, parts


def closed_lineage_rows(role_ref, part, sources, identity, get, seen=None):
    """Expand accepted saved closure deltas without repeating native verification."""
    seen = set() if seen is None else seen
    require(role_ref['path'] not in seen and len(seen) < 128, 'closure_lineage_cycle_or_depth')
    seen.add(role_ref['path'])
    saved = get(role_ref)
    schema = saved['schema']
    require(schema in ('causes.finite_supplement_tranche_closed.v1',
                       'causes.finite_zero_claim_closed_extension.v1'), 'closure_lineage_schema')
    require(saved.get('full_tranche_qualified') is True and saved.get('qualification_findings') == []
            and saved['role'] == part['role'] and saved['output'] == part['output_root']
            and same(saved['identity'], identity) and type(saved['active']) is int
            and saved['active'] == 0 and same(saved['native_collector'], sources['collector'])
            and same(saved['native_runner'], sources['runner'])
            and same(saved['adapter'], sources['adapter']), 'closure_lineage_identity')
    plan = get(saved['plan'])
    require(same(plan['jobs'], part['jobs']) and plan['endpoint'] == part['endpoint']
            and plan['output_root'] == part['output_root'], 'closure_lineage_plan_identity')
    ids = saved['complete_ids']
    require(type(ids) is list and all(type(mid) is str and mid for mid in ids)
            and len(set(ids)) == len(ids) and type(saved['terminal']) is int
            and saved['terminal'] == len(ids), 'closure_lineage_terminal_ids')
    reference = saved['verified_finals']
    rows = get(reference)
    require(type(rows) is list, 'closure_lineage_rows')
    previous_rows, origins = [], []
    zero = schema == 'causes.finite_zero_claim_closed_extension.v1'
    prior_key = 'previous' if zero else 'prior_closed'
    review_key = 'previous_review' if zero else 'prior_closed_review'
    require(not (zero and any(key in saved for key in ('prior_closed', 'prior_closed_review',
                                                       'prior_verified_finals')))
            and not (not zero and any(key in saved for key in ('previous', 'previous_review'))),
            'closure_lineage_ambiguous_predecessor')
    if prior_key in saved:
        prior_ref, peer_ref = saved[prior_key], saved[review_key]
        accepted(get, peer_ref, 'accepted_bounded_finite_tranche_closed', actual=prior_ref)
        prior = get(prior_ref)
        previous_rows, origins = closed_lineage_rows(prior_ref, part, sources, identity, get, seen)
        require(ids[:len(previous_rows)] == prior['complete_ids'], 'closure_lineage_prefix')
        for origin in origins:
            origin.setdefault('closure_lineage', []).append(dict(actual=role_ref,
                previous=prior_ref, review=peer_ref, accepted_flag='accepted_bounded_finite_tranche_closed'))
        if zero:
            require(same(reference, prior['verified_finals']) and ids == prior['complete_ids']
                    and type(saved['new_claims']) is int and saved['new_claims'] == 0
                    and type(saved['new_finals_verified']) is int
                    and saved['new_finals_verified'] == 0, 'zero_claim_closure_changed')
            return previous_rows, origins
        require(same(saved['prior_verified_finals'], prior['verified_finals']),
                'closure_lineage_prior_artifact')
    else:
        require(not zero and not any(key in saved for key in
                ('prior_closed_review', 'prior_verified_finals')), 'closure_lineage_missing_predecessor')
    require(type(saved['new_finals_verified']) is int and saved['new_finals_verified'] == len(rows)
            and ids == [row['measurement_id'] for row in previous_rows + rows],
            'closure_lineage_delta_partition')
    for index, row in enumerate(rows):
        require('qualification_error' not in row, 'unqualified_lineage_row')
        origins.append(dict(role=part['role'], actual=role_ref, verified_finals=reference,
            verified_index=index, row_sha256=hashlib.sha256(encoded(row)).hexdigest()))
    return copy.deepcopy(previous_rows + rows), origins


def combine_verified(data, membership, get, actual_ref, review_ref, acceptance_flag,
                     *, allow_closed_lineage=False):
    """Reorder accepted native rows, retaining each original artifact and index."""
    closure = get(actual_ref)
    accepted(get, review_ref, acceptance_flag, actual=actual_ref)
    require(closure.get('all_qualified') is True
            and closure.get('qualification_findings') == []
            and type(closure.get('active')) is int and closure['active'] == 0,
            'combined_closure_not_qualified')
    parts = unique(data['parts'], 'role', 'duplicate_parts')
    require(set(closure['roles']) == set(parts), 'closure_role_partition')
    values, origins = {}, {}
    for role, part in parts.items():
        role_ref = closure['roles'][role]
        saved = get(role_ref)
        require(saved.get('full_tranche_qualified') is True
                and saved.get('qualification_findings') == []
                and saved['role'] == role and same(saved['plan'], part['plan'])
                and saved['output'] == part['output_root']
                and type(saved['active']) is int and saved['active'] == 0
                and same(saved['native_collector'], data['sources']['collector'])
                and same(saved['native_runner'], data['sources']['runner'])
                and same(saved['adapter'], data['sources']['adapter']), 'role_closure_identity')
        original_ref = saved['verified_finals']
        if allow_closed_lineage:
            rows, row_origins = closed_lineage_rows(role_ref, part, data['sources'],
                                                   saved['identity'], get)
        else:
            rows = get(original_ref)
            row_origins = [dict(role=role, actual=role_ref, verified_finals=original_ref,
                verified_index=index, row_sha256=hashlib.sha256(encoded(row)).hexdigest())
                for index, row in enumerate(rows)]
        expected_ids = [row['measurement_id'] for row in membership['rows'] if row['role'] == role]
        require(type(saved['terminal']) is int and saved['terminal'] == len(rows)
                and saved['complete_ids'] == [row['measurement_id'] for row in rows]
                and set(saved['complete_ids']) == set(expected_ids)
                and len(rows) == len(expected_ids), 'role_verification_partition')
        for index, row in enumerate(rows):
            mid = row['measurement_id']
            require(mid not in values and 'qualification_error' not in row, 'duplicate_or_unqualified_row')
            values[mid] = copy.deepcopy(row)
            origins[mid] = row_origins[index]
    require(set(values) == set(membership['measurement_ids'])
            and type(closure['terminal']) is int and closure['terminal'] == len(values),
            'combined_verification_partition')
    ordered, mapping = [], []
    for index, member in enumerate(membership['rows']):
        mid = member['measurement_id']
        row = values[mid]
        require(same(row['final'], member['final'])
                and same(row['capture_files'], member['capture_files']), 'native_member_custody')
        origin = origins[mid]
        require(same(get(origin['verified_finals'])[origin['verified_index']], row),
                'original_artifact_index_identity')
        ordered.append(row)
        mapping.append(dict(measurement_id=mid, combined_index=index, **origin))
    return ordered, mapping


def projected_evidence(data, membership, verified_reference, verified):
    """Bind every global index to the same checked combined verification artifact."""
    require(len(verified) == len(membership['rows']), 'combined_verification_count')
    parts = unique(data['parts'], 'role', 'duplicate_parts')
    evidence = {}
    for index, (member, value) in enumerate(zip(membership['rows'], verified)):
        mid = member['measurement_id']
        require(value['measurement_id'] == mid and mid not in evidence
                and same(value['final'], member['final']), 'combined_verification_row')
        part = parts[member['role']]
        evidence[mid] = dict(measurement_id=mid, final=value['final'],
            request_sha256=value['request_body_sha256'], verified_finals=copy.deepcopy(verified_reference),
            verified_index=index, native_collector=data['sources']['collector'],
            custody=dict(actual=part['cut'], review=part['cut_review'],
                accepted_flag=part['cut_acceptance_flag'], closed_run_id=member['run_id'],
                terminal=True, membership=data['membership']))
    return evidence


def occupied_rows(previous, manifest, selected_runs, record, *, retain_supplements=False):
    """Keep a full unique-key index and literal selected presence rows in memory."""
    seen, rows = set(), []
    for segment in manifest['segments']:
        reference = segment['payload']
        record(reference)
        count = 0
        with open(reference['path'], 'rb') as stream:
            for row in ijson.items(stream, 'item', use_float=True):
                key = (row['run_id'], row['readout'])
                require(key not in seen, 'duplicate_previous_slot')
                seen.add(key)
                count += 1
                rows.append(row if row['run_id'] in selected_runs
                            and (row['readout'] == 'self_evaluation_presence'
                                 or retain_supplements and row['readout'] in (
                                     'self_evaluation_strongly_favors',
                                     'self_evaluation_leans_towards_or_favors'))
                            else dict(run_id=row['run_id'], readout=row['readout']))
        require(count == segment['physical_measurements'], 'previous_segment_count')
    require(len(rows) == previous['terminal_slots'] == manifest['physical_measurements'],
            'previous_total_count')
    return rows


def retained_pair_ledger(entries, manifest, assignments, members, verified,
                         occupied, get):
    """Prove old pairs against accepted imports and the cumulative native rows."""
    require(type(entries) is list and entries, 'prior_projection_ledger_required')
    by_key = {(row['run_id'], row['readout']): row for row in occupied}
    require(len(by_key) == len(occupied), 'duplicate_previous_slot')
    member_keys = {(row['run_id'], row['readout']): row for row in members}
    require(len(member_keys) == len(members), 'duplicate_terminal_slot')
    current = unique(verified, 'measurement_id', 'duplicate_native_row')
    segment_refs = {row['payload']['path']: row['payload'] for row in manifest['segments']}
    retained, seen_results = {}, set()
    for entry in entries:
        result = get(entry['result'])
        require(entry['result']['path'] not in seen_results, 'duplicate_prior_projection')
        seen_results.add(entry['result']['path'])
        accepted(get, entry['review'], 'accepted_supplement_pair_verified_slots',
                 actual=entry['result'], slots=result['slots'], assignments=assignments)
        require(result['schema'] == 'causes.supplement_pair_pipeline_result.v1'
                and result['mode'] in ('project', 'continue')
                and same(result['assignments'], assignments)
                and same(segment_refs.get(result['slots']['path']), result['slots']),
                'prior_projection_not_in_current_manifest')
        slots = get(result['slots'])
        require(type(slots) is list and slots and type(result['occupied']) is int
                and result['occupied'] == len(slots), 'prior_projection_count')
        run_instruments = {}
        for row in slots:
            key = (row['run_id'], row['readout'])
            require(key not in retained and key in member_keys
                    and same(by_key.get(key), row), 'prior_projection_slot_changed_or_removed')
            member = member_keys[key]
            evidence = row['evidence']
            value = current[member['measurement_id']]
            index = evidence['verified_index']
            originals = get(evidence['verified_finals'])
            require(type(originals) is list and type(index) is int
                    and 0 <= index < len(originals) and same(originals[index], value),
                    'prior_native_row_changed')
            require(evidence['measurement_id'] == member['measurement_id']
                    and same(evidence['final'], value['final'])
                    and evidence['request_sha256'] == value['request_body_sha256'],
                    'prior_measurement_identity_changed')
            run_instruments.setdefault(key[0], set()).add(key[1])
            retained[key] = dict(slot=row, prior_result=entry['result'],
                                 prior_review=entry['review'], prior_slots=result['slots'])
        require(all(readouts == {'self_evaluation_strongly_favors',
                                 'self_evaluation_leans_towards_or_favors'}
                    for readouts in run_instruments.values()), 'partial_prior_pair')
    require(set(retained) == set(member_keys) & set(by_key), 'unproved_occupied_supplement')
    return retained


def remove_verified_retained_pairs(projection, retained, current_evidence):
    """Keep old slots literal; only return newly derived, wholly unoccupied pairs."""
    canonical = {(row['run_id'], row['readout']): row for row in projection['slots']}
    require(len(canonical) == len(projection['slots']) and set(retained) <= set(canonical),
            'retained_pair_not_complete')
    mappings = []
    for key, proof in retained.items():
        old, new = proof['slot'], canonical[key]
        require(same({k: v for k, v in old.items() if k != 'evidence'},
                     {k: v for k, v in new.items() if k != 'evidence'}),
                'retained_slot_value_changed')
        mid = new['evidence']['measurement_id']
        require(same(old['evidence']['native_collector'],
                     current_evidence[mid]['native_collector']), 'retained_collector_changed')
        mappings.append(dict(run_id=key[0], readout=key[1], measurement_id=mid,
            original_slot_sha256=hashlib.sha256(encoded(old)).hexdigest(),
            prior_result=proof['prior_result'], prior_review=proof['prior_review'],
            prior_slots=proof['prior_slots'], original_evidence=copy.deepcopy(old['evidence']),
            cumulative_evidence=copy.deepcopy(current_evidence[mid])))
    old_runs = {key[0] for key in retained}
    slots = [row for row in projection['slots'] if (row['run_id'], row['readout']) not in retained]
    require(not {row['run_id'] for row in slots} & old_runs, 'partly_retained_pair')
    result = copy.deepcopy(projection)
    result.update(slots=slots,
        terminal_run_ids=[run for run in projection['terminal_run_ids'] if run not in old_runs],
        existing_presence=[row for row in projection['existing_presence'] if row['run_id'] not in old_runs],
        counts=dict(contexts=len({row['run_id'] for row in slots}), occupied=len(slots),
                    observed=sum(row['status'] == 'observed' for row in slots),
                    missing=sum(row['status'] == 'missing' for row in slots)))
    return result, mappings


def deny_effects(event, args):
    if event.startswith(('socket.', 'subprocess.', 'os.exec', 'os.spawn', 'os.posix_spawn')) \
            or event in ('os.system', 'os.kill', 'os.killpg'):
        raise ValueError('offline_only:' + event)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    parser.add_argument('--authority', required=True)
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    sys.addaudithook(deny_effects)
    request_ref, authority_ref = pin(args.request), pin(args.authority)
    request, authority = checked(request_ref), checked(authority_ref)
    require(request['schema'] == 'causes.supplement_pair_pipeline_request.v1'
            and request['mode'] in ('project', 'continue') and same(request['source'], pin(__file__)),
            'request_schema_or_source')
    mode = request['mode']
    accepted(checked, request['source_review'], 'accepted_supplement_pair_pipeline_source',
             source=request['source'])
    accepted(checked, request['bindings_review'], 'accepted_supplement_pair_pipeline_bindings',
             source=request['source'], source_review=request['source_review'],
             bindings=request['bindings'], mode=mode)
    require(authority['root_authorized'] is True and same(authority['request'], request_ref)
            and authority['mode'] == mode and same(authority['source'], request['source'])
            and type(authority['maximum_invocations']) is int
            and authority['maximum_invocations'] == 1 and authority['automatic_retry'] is False,
            'once_authority')
    limit = request['max_output_bytes']
    require(type(limit) is int and limit > 0, 'output_byte_limit')
    binding = checked(request['bindings'])
    require(binding['schema'] == 'causes.supplement_pair_pipeline_bindings.v1', 'bindings_schema')
    data = checked(binding['data'])
    output = Path(request['output'])
    require(output.is_absolute() and output.resolve() == output and not output.exists()
            and not any(p.is_symlink() for p in output.parents)
            and not any(output.is_relative_to(Path(p['output_root']))
                        or Path(p['output_root']).is_relative_to(output) for p in data['parts']),
            'fresh_separate_output')
    reader = module(data['sources']['read_guard'], 'supplement_pair_read_guard')
    accepted(checked, binding['projector_review'], 'accepted_terminal_pair_projection_source',
             source=binding['projector'])
    for name in list(sys.modules):
        if name == 'causes' or name.startswith('causes.'):
            del sys.modules[name]
    sys.path.insert(0, str(Path(binding['projector']['path']).parents[1]))
    projector = module(binding['projector'], 'supplement_pair_projector')
    require(same(pin(sys.modules['causes.campaign_analysis'].__file__), binding['labels_source']),
            'projector_labels_source')
    allowlist = checked(binding['allowlist'])
    guard = reader.ReadGuard(allowlist['files'] + list(references([request, authority, binding, data]))
        + [request_ref, authority_ref], output, allowlist['source_files'], [],
        json_cache_bytes=64 * 1024 * 1024, json_cache_entries=64)
    sys.addaudithook(guard.audit)
    get = guard.checked_json
    for reference in (request_ref, authority_ref, request['bindings'], binding['data']):
        guard.record(reference)
    jobs, membership, assignments, _ = validate_membership(data, get)
    verified, origins = combine_verified(data, membership, get, binding['verification'],
        binding['verification_review'], binding['verification_acceptance_flag'],
        allow_closed_lineage=mode == 'continue')
    previous = get(binding['previous'])
    accepted(get, binding['previous_review'], 'accepted_measurement_segments',
             actual=binding['previous'], assignments=data['assignments'], manifest=previous['manifest'])
    manifest = get(previous['manifest'])
    require(same(manifest['assignments'], data['assignments']), 'previous_assignments')
    occupied = occupied_rows(previous, manifest,
        {row['run_id'] for row in membership['rows']}, guard, retain_supplements=mode == 'continue')
    retained = {}
    if mode == 'continue':
        retained = retained_pair_ledger(binding['prior_projections'], manifest,
            data['assignments'], membership['rows'], verified, occupied, get)
    else:
        require(not binding.get('prior_projections'), 'continuation_mode_required')
    output.mkdir()
    writer = LimitedWriter(output, limit)
    intent = writer.save('INTENT.v1.json', dict(request=request_ref, authority=authority_ref,
        source=request['source'], mode=mode, at=datetime.datetime.now(datetime.timezone.utc).isoformat()))
    verified_ref = writer.save('VERIFIED_FINALS.derived.v1.json', verified)
    derivation = writer.save('VERIFICATION_ORIGINS.v1.json', dict(
        actual=binding['verification'], review=binding['verification_review'],
        accepted_flag=binding['verification_acceptance_flag'], combined=verified_ref,
        membership=data['membership'], rows=origins, rows_unchanged=True,
        native_verifier_invocations=0))
    evidence = projected_evidence(data, membership, verified_ref, verified)
    require(all(same(value['verified_finals'], verified_ref)
                and type(value['verified_index']) is int
                and same(verified[value['verified_index']], row)
                for value, row in zip(evidence.values(), verified)), 'combined_artifact_index_identity')
    projection = projector.project_verified_supplement_pairs(jobs=jobs,
        epochs=assignments['epochs'], terminal_membership=membership['rows'],
        verified_finals=verified,
        occupied_rows=[row for row in occupied if (row['run_id'], row['readout']) not in retained],
        evidence_by_measurement=evidence)
    retained_ref = None
    if mode == 'continue':
        projection, mappings = remove_verified_retained_pairs(projection, retained, evidence)
        retained_ref = writer.save('RETAINED_PAIRS.v1.json', dict(rows=mappings,
            original_slots_preserved=True, emitted_again=0,
            previous=binding['previous'], previous_review=binding['previous_review']))
    slots = writer.save('SLOTS.v1.json', projection['slots'])
    membership_ref = writer.save('MEMBERSHIP.v1.json', dict(
        terminal_run_ids=projection['terminal_run_ids'],
        pending_incomplete_membership=projection['pending_incomplete_membership'],
        source_membership=data['membership'], verification=binding['verification']))
    presence = writer.save('EXISTING_PRESENCE.v1.json', projection['existing_presence'])
    guard.finish()
    result = dict(schema='causes.supplement_pair_pipeline_result.v1', mode=mode,
        slots=slots, membership=membership_ref, existing_presence=presence,
        verified_finals=verified_ref, verification_origins=derivation,
        counts=projection['counts'], occupied=projection['counts']['occupied'],
        previous=binding['previous'], previous_review=binding['previous_review'],
        native_verifier_invocations=0, parser_invocations=0, projector_invocations=1,
        source=request['source'], source_review=request['source_review'], request=request_ref,
        bindings=request['bindings'], bindings_review=request['bindings_review'], data=binding['data'],
        assignments=data['assignments'], source_membership=data['membership'], intent=intent,
        finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        verified_named_files=len(guard.identities), host_calls=0, model_calls=0,
        queue_invocations=0, original_writes=0, analysis_performed=False,
        max_output_bytes=limit, prior_output_bytes=writer.written)
    if mode == 'continue':
        result.update(retained_pairs=retained_ref, retained_slots=len(retained),
                      prior_projections=binding['prior_projections'])
    print(json.dumps(writer.save('RESULT.v1.json', result)), flush=True)


class LimitedWriter:
    """Refuse a whole next artifact before writing beyond the request's envelope."""
    def __init__(self, output, limit):
        self.output, self.limit, self.written = Path(output), limit, 0

    def save(self, name, value):
        require(Path(name).name == name, 'output_basename')
        raw = encoded(value) + b'\n'
        require(self.written + len(raw) <= self.limit, 'output_byte_limit')
        path = self.output / name
        with path.open('xb') as stream:
            stream.write(raw)
        self.written += len(raw)
        return pin(path)


if __name__ == '__main__':
    main()
