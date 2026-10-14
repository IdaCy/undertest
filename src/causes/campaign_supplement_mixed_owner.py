"""Authenticate an ordered original/derived owner bank without executing probes.

Closed custody is not shortened for an original-only consumer.  This module
keeps the complete owner bank and derives separate scientific identity domains
from accepted append proofs and exact prepared derived rows, never family names.
The caller owns source acceptance and fresh runtime authority.
"""
import hashlib
import json
import math
from pathlib import PurePosixPath


SCHEMA = 'causes.mixed_supplement_owner_partition.v1'
ORIGINAL = 'causes.original_supplement_owner_bank_extension.v1'
DERIVED = 'causes.derived_supplement_owner_bank_extension.v1'
QUESTIONS = ('evaluation_presence', 'evaluation_strongly_favors',
             'evaluation_leans_towards_or_favors')


class MixedOwnerError(ValueError):
    pass


def require(ok, reason):
    if not ok:
        raise MixedOwnerError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _unique(items, reason):
    require(type(items) is list and all(type(x) is str and x for x in items)
            and len(items) == len(set(items)), reason)
    return items


def _ids(jobs):
    require(type(jobs) is list and all(type(j) is dict for j in jobs), 'mixed_job_list')
    return _unique([j['measurement_id'] for j in jobs], 'mixed_duplicate_job')


def original_key(job, model):
    require(job['model'] == model and job['stage'] == 'early'
            and job['question_id'] in QUESTIONS[1:]
            and job['measurement_id'] == '/'.join(job[k] for k in
                ('family', 'model', 'run_id', 'stage', 'question_id')),
            'mixed_original_identity')
    return job['run_id'], job['stage'], job['question_id']


def _reader(read, maximum_bank_bytes):
    refs, documents = {}, {}
    require(callable(read) and type(maximum_bank_bytes) is int
            and 0 < maximum_bank_bytes <= 256 * 1024**2, 'mixed_reader_bound')

    def duplicate_free(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'mixed_duplicate_json_key')
            result[key] = value
        return result

    def finite(text):
        result = float(text)
        require(math.isfinite(result), 'mixed_nonfinite_json')
        return result

    def value(ref, bank=False):
        require(type(ref) is dict and set(ref) == {'path', 'bytes', 'sha256'}, 'mixed_pin')
        path = ref['path']; limit = maximum_bank_bytes if bank else 8 * 1024**2
        require(type(path) is str and path.startswith('/') and '\x00' not in path
                and str(PurePosixPath(path)) == path and '..' not in PurePosixPath(path).parts
                and type(ref['bytes']) is int and 0 <= ref['bytes'] <= limit
                and type(ref['sha256']) is str and len(ref['sha256']) == 64,
                'mixed_pin_bound')
        require(path not in refs or refs[path] == ref, 'mixed_pin_conflict')
        if path not in documents:
            raw = read(ref)
            require(type(raw) is bytes and len(raw) == ref['bytes']
                    and hashlib.sha256(raw).hexdigest() == ref['sha256'], 'mixed_pin_bytes')
            documents[path] = json.loads(raw, object_pairs_hook=duplicate_free,
                parse_float=finite, parse_constant=lambda _: require(False, 'mixed_nonfinite_json'))
            refs[path] = dict(ref)
        return documents[path]
    return value, refs


def _accepted(descriptor, value, flag, field='actual'):
    require(type(descriptor) is dict and set(descriptor) == {'actual', 'review'},
            'mixed_descriptor')
    actual, peer = value(descriptor['actual']), value(descriptor['review'])
    require(peer.get(flag) is True and peer.get('findings') == []
            and peer.get(field) == descriptor['actual'], 'mixed_independent_peer')
    return actual, peer


def _closed(descriptor, value):
    saved, peer = _accepted(descriptor, value, 'accepted_finite_supplement_closed_custody')
    require(saved['schema'] == 'causes.finite_supplement_tranche_closed.v1'
            and saved['role'] == 'mi' and saved['mode'] == 'completed_actor_direct_probe'
            and type(saved['active']) is int and saved['active'] == 0
            and saved['ambiguous'] == [] and saved['clean_native_ending'] is True
            and saved['unresolved_claimed_ids'] == []
            and peer.get('accepted_completed_actor_saved_closure') is True
            and peer.get('source') == saved['source']
            and peer.get('original_denominator_preserved') is True
            and peer.get('all_original_attempts_retained') is True
            and peer.get('qualification_findings') == saved['qualification_findings'],
            'mixed_closed_custody')
    jobs = value(saved['full_jobs'], True); ids = _ids(jobs)
    done = _unique(saved['complete_ids'], 'mixed_claimed_ids')
    todo = _unique(saved['unstarted_ids'], 'mixed_unstarted_ids')
    require(ids == done + todo and saved['terminal'] == saved['occupied'] == len(done)
            and all(type(saved[k]) is int for k in ('terminal','occupied'))
            and type(saved['consecutive_failures']) is int and saved['consecutive_failures'] >= 0,
            'mixed_closed_partition')
    require(not any(k.endswith('_bank_extension') and k not in
                    ('original_bank_extension','derived_bank_extension') for k in saved),
            'mixed_unknown_extension_field')
    return saved, peer, jobs


def _derived_jobs(bank, value):
    """Use accepted prepared rows, including literal shared-control equality."""
    jobs, seen, protocol = [], {}, None
    require(type(bank['inputs']) is list and bank['inputs'], 'mixed_derived_inputs')
    for descriptor in bank['inputs']:
        prepared, peer = _accepted(descriptor, value,
            'accepted_frozen_derived_probe_inputs', 'preparation')
        require(prepared['schema'] == 'causes.frozen_derived_probe_preparation.v1'
                and prepared['runtime_authorized'] is False
                and all(peer[k] == prepared[k] for k in ('rows','pairs','assignments','protocol','counts')),
                'mixed_derived_input_peer')
        rows, pairs, assignments, current_protocol = [value(prepared[k]) for k in
                                                      ('rows','pairs','assignments','protocol')]
        require(digest(rows) == prepared['rows_canonical_sha256'] and len(rows) == len(assignments)
                and current_protocol['family'] == bank['family']
                and current_protocol['model'] == bank['model'], 'mixed_derived_protocol')
        if protocol is None:
            protocol = current_protocol
        require(current_protocol == protocol, 'mixed_derived_protocol_changed')
        roles = {}
        for pair in pairs:
            require(set(pair['arms']) == {'baseline','treatment'}, 'mixed_derived_pair')
            for role, arm in pair['arms'].items():
                require(arm['measurement_key'] not in roles, 'mixed_derived_pair_alias')
                roles[arm['measurement_key']] = (role, arm)
        require(len(roles) == len(rows), 'mixed_derived_pair_count')
        local = set()
        for row, assignment in zip(rows, assignments):
            key = row['measurement_key']
            require(key not in local and key in roles and row['schema'] == 'causes.derived_probe_input.v1'
                    and row['runtime_authorized'] is False
                    and assignment == {k:row[k] for k in ('run_id','view_kind','stage')}
                    and roles[key][1] == {k:row[k] for k in ('run_id','measurement_key','disposition')},
                    'mixed_derived_row_identity')
            local.add(key)
            if row['disposition'] == 'unavailable':
                require(row['jobs'] == [] and row['view'] is None
                        and type(row['reason']) is str and row['reason'], 'mixed_derived_unavailable')
            else:
                require(row['disposition'] == 'prepared'
                        and [j['question_id'] for j in row['jobs']] == list(QUESTIONS),
                        'mixed_derived_questions')
                for job in row['jobs']:
                    require(job['family'] == bank['family'] and job['model'] == bank['model']
                            and job['run_id'] == key and job['original_run_id'] == row['run_id']
                            and job['stage'] == row['stage'] == 'early'
                            and job['derived_view_canonical_sha256'] == row['view_canonical_sha256']
                            and job['context_canonical_sha256'] == row['context_canonical_sha256']
                            and job['protocol_sha256'] == row['protocol_sha256']
                            and job['measurement_id'] == '/'.join(job[k] for k in
                                ('family','model','run_id','stage','question_id')),
                            'mixed_derived_job_identity')
            if key in seen:
                require(roles[key][0] == seen[key][0] == 'baseline' and row == seen[key][1],
                        'mixed_shared_control_changed')
            else:
                seen[key] = roles[key][0], row
                jobs.extend(row['jobs'])
    require(len(jobs) == bank['job_count'] and digest(jobs) == bank['jobs_sha256']
            and digest(_ids(jobs)) == bank['ids_sha256'], 'mixed_derived_exact_jobs')
    return jobs


def _resolve(reference, *, predecessor, source, read, require_complete=True,
            maximum_bank_bytes=128 * 1024**2):
    """Authenticate an explicit partition through one adopted derived bank.

    The original base is genuine accepted custody, not a synthetic shortened
    head. Every later invocation is retained. Same-bank partial closures are
    allowed in the chain; a runtime caller requires a completely occupied head.
    Metadata pins retain an 8 MiB ceiling independently of full-bank file size.
    """
    value, refs = _reader(read, maximum_bank_bytes)
    proof, partition_peer = _accepted(reference, value, 'accepted_mixed_supplement_owner_partition')
    require(partition_peer.get('source') == source and partition_peer.get('head') == predecessor,
            'mixed_partition_source_and_head')
    require(set(proof) == {'schema','head','original_base','links','model','counts',
                           'owner_ids_sha256','original_ids_sha256','derived_ids_sha256'}
            and proof['schema'] == SCHEMA and proof['head'] == predecessor
            and type(proof['links']) is list and 0 < len(proof['links']) <= 64
            and proof['links'][-1] == predecessor, 'mixed_partition_descriptor')
    base, _, previous_jobs = _closed(proof['original_base'], value)
    require(base['unstarted_ids'] == [] and not base.get('derived_bank_extension'),
            'mixed_original_base')
    original = list(previous_jobs); derived = []; links = []; visited = {proof['original_base']['actual']['path']}
    original_keys = [original_key(j, proof['model']) for j in original]
    require(len(original_keys) == len(set(original_keys)), 'mixed_original_alias')
    prior, prior_ref, adoption, derived_extension = base, proof['original_base'], None, None
    for descriptor in proof['links']:
        require(descriptor['actual']['path'] not in visited, 'mixed_history_cycle')
        visited.add(descriptor['actual']['path'])
        saved, peer, full = _closed(descriptor, value); plan = value(saved['plan'])
        require(plan['schema'] == 'causes.completed_actor_driver_plan.v1'
                and plan['role'] == 'mi' and plan['model'] == proof['model']
                and plan['predecessor_custody'] == prior_ref
                and saved['prior_closed'] == prior_ref['actual']
                and saved['prior_closed_review'] == prior_ref['review']
                and plan['occupied_ids'] == prior['complete_ids']
                and plan['initial_failures'] == prior['consecutive_failures']
                and saved['inherited_qualification_findings'] == prior['qualification_findings']
                and saved['qualification_findings'] == prior['qualification_findings'] + saved['current_qualification_findings']
                and saved['prior_invalid_ids'] == prior['cumulative_invalid_ids']
                and saved['cumulative_invalid_ids'] == prior['cumulative_invalid_ids'] + saved['new_invalid_ids'],
                'mixed_preserved_history')
        require(full[:len(previous_jobs)] == previous_jobs and len(full) >= len(previous_jobs),
                'mixed_changed_owner_prefix')
        appended = full[len(previous_jobs):]
        oe, de = saved.get('original_bank_extension'), saved.get('derived_bank_extension')
        require(not (oe and de), 'mixed_dual_extension')
        if 'derived_bank' in plan:
            require(not oe and 'original_reservation_transfer' not in plan and type(de) is dict,
                    'mixed_derived_extension_required')
            bank, _ = _accepted(plan['derived_bank'], value, 'accepted_derived_supplement_bank')
            require(bank['schema'] == 'causes.derived_supplement_bank_adoption.v1'
                    and bank['root_authorized'] is True and bank['target_role'] == 'mi'
                    and bank['model'] == proof['model'] and bank['family'] == plan['family'],
                    'mixed_derived_adoption')
            if adoption is None:
                require(appended and prior['unstarted_ids'] == []
                        and bank['execution_predecessor'] == prior_ref
                        and plan['continuation_scope'] == 'adopted_derived_bank', 'mixed_derived_base')
                expected = _derived_jobs(bank, value)
                require(appended == expected, 'mixed_derived_append_changed')
                derived = list(appended); adoption = plan['derived_bank']; derived_extension = de
            else:
                require(not appended and plan['derived_bank'] == adoption and de == derived_extension
                        and plan['continuation_scope'] == 'derived_bank_remaining',
                        'mixed_second_derived_bank')
            require(de['schema'] == DERIVED and de['adoption'] == adoption
                    and de['execution_predecessor'] == de['previous_closed'] == bank['execution_predecessor']
                    and de['previous_full_jobs'] == base['full_jobs']
                    and de['prior_count'] == len(base['complete_ids'])
                    and de['appended_count'] == len(derived)
                    and de['appended_jobs_sha256'] == digest(derived) == bank['jobs_sha256']
                    and de['appended_ids_sha256'] == digest(_ids(derived))
                    and de['full_jobs'] == saved['full_jobs']
                    and de['cumulative_count'] == len(full) and de['cumulative_jobs_sha256'] == digest(full)
                    and de['prior_prefix_unchanged'] is True and de['native_families_preserved'] is True,
                    'mixed_derived_extension_binding')
            kind = 'derived'
        else:
            require(adoption is not None and not de, 'mixed_original_after_derived_only')
            if appended:
                require(prior['unstarted_ids'] == [] and type(oe) is dict
                        and oe['schema'] == ORIGINAL and oe['previous_closed'] == prior_ref
                        and oe['previous_full_jobs'] == prior['full_jobs']
                        and oe['transfer'] == plan['original_reservation_transfer']
                        and oe['source_family'] == plan['family'] and oe['model'] == plan['model']
                        and oe['prior_count'] == len(previous_jobs) and oe['appended_count'] == len(appended)
                        and oe['appended_jobs_sha256'] == digest(appended) == plan['jobs_sha256']
                        and oe['appended_ids_sha256'] == digest(_ids(appended))
                        and oe['cumulative_count'] == len(full) and oe['cumulative_jobs_sha256'] == digest(full)
                        and oe['full_jobs'] == saved['full_jobs'] and oe['prior_prefix_unchanged'] is True
                        and oe['native_families_preserved'] is True, 'mixed_original_extension_binding')
                transfer, _ = _accepted(oe['transfer'], value,
                                       'accepted_original_supplement_reservation_transfer')
                require(transfer['schema'] == 'causes.original_supplement_reservation_transfer.v1'
                        and transfer['execution_predecessor'] == prior_ref
                        and transfer['target_role'] == 'mi' and transfer['target_output'] == saved['output']
                        and transfer['family'] == plan['family'] and transfer['model'] == proof['model']
                        and transfer['job_count'] == len(appended)
                        and transfer['jobs_sha256'] == digest(appended), 'mixed_original_transfer')
                keys = [original_key(j, proof['model']) for j in appended]
                require(all(j['family'] == plan['family'] for j in appended)
                        and len(keys) == len(set(keys)) and not set(keys).intersection(original_keys),
                        'mixed_original_append_alias')
                original_keys.extend(keys); original.extend(appended)
            else:
                require(not oe and 'original_reservation_transfer' not in plan, 'mixed_unknown_extension')
            kind = 'original'
        old_done = prior['complete_ids']; new = saved['complete_ids'][len(old_done):]
        selected = full[len(old_done):len(old_done)+plan['maximum_new_requests']]
        require(saved['complete_ids'] == old_done + new
                and type(plan['maximum_new_requests']) is int and plan['maximum_new_requests'] > 0
                and len(selected) == plan['maximum_new_requests'] and digest(selected) == plan['jobs_sha256']
                and new == _ids(selected)[:len(new)], 'mixed_current_request_partition')
        domain = set(_ids(derived if kind == 'derived' else original))
        require(all(j['measurement_id'] in domain and j['family'] == plan['family']
                    and j['model'] == plan['model'] for j in selected), 'mixed_current_scientific_domain')
        inv = saved['native_invocations']; before = prior['native_invocations']
        require(len(inv) == len(before) + 1 and inv[:-1] == before
                and inv[-1]['plan'] == saved['plan'] and inv[-1]['source'] == plan['sources']['driver']
                and inv[-1]['result'] == saved['native_result'] and inv[-1]['process_absent'] is True
                and type(inv[-1]['pid']) is int and inv[-1]['pid'] > 0
                and inv[-1]['family'] == plan['family'] and inv[-1]['model'] == plan['model']
                and inv[-1]['jobs_sha256'] == plan['jobs_sha256'], 'mixed_native_invocation_prefix')
        require((kind != 'derived' or inv[-1].get('derived_bank') == plan['derived_bank'])
                and (not appended or kind != 'original'
                     or inv[-1].get('original_reservation_transfer') == plan['original_reservation_transfer']),
                'mixed_invocation_extension')
        result = value(saved['native_result'])['result']
        require(result['active'] == 0 and result['consecutive_failures'] == saved['consecutive_failures']
                and result['new_final'] == len(new) and set(result['completed_ids']) == set(new)
                and len(result['completed_ids']) == len(new), 'mixed_native_result')
        links.append(dict(custody=descriptor, kind=kind, plan=saved['plan'],
                          original_extension=oe, derived_extension=de))
        prior, prior_ref, previous_jobs = saved, descriptor, full
    require(adoption is not None and (not require_complete or prior['unstarted_ids'] == []),
            'mixed_incomplete_execution_predecessor')
    original_ids, derived_ids, owner_ids = _ids(original), _ids(derived), _ids(previous_jobs)
    require(not set(original_ids).intersection(derived_ids)
            and set(owner_ids) == set(original_ids) | set(derived_ids)
            and proof['counts'] == dict(owner=len(owner_ids), original=len(original_ids), derived=len(derived_ids))
            and proof['owner_ids_sha256'] == digest(owner_ids)
            and proof['original_ids_sha256'] == digest(original_ids)
            and proof['derived_ids_sha256'] == digest(derived_ids), 'mixed_exact_partition')
    return dict(owner_jobs=previous_jobs, original_jobs=original, derived_jobs=derived,
                original_ids=original_ids, derived_ids=derived_ids, original_base=proof['original_base'],
                derived_adoption=adoption, links=links, head=predecessor,
                required_pins=list(refs.values()))


def resolve(reference, *, predecessor, source, read, require_complete=True,
            maximum_bank_bytes=128 * 1024**2):
    try:
        return _resolve(reference, predecessor=predecessor, source=source, read=read,
                        require_complete=require_complete, maximum_bank_bytes=maximum_bank_bytes)
    except (KeyError, TypeError, IndexError) as error:
        raise MixedOwnerError('mixed_shape') from error


def validate_complement(metadata, *, value):
    """Reconstruct the saved complement in original pool order, without jobs I/O."""
    try:
        require(metadata['schema'] == 'causes.saved_unstarted_complement_subset.v1'
                and metadata['runtime_authorized'] is False, 'complement_schema')
        pool, excluded = value(metadata['pool']), value(metadata['excluded'])
        require(type(pool) is list and excluded['schema'] == 'causes.saved_unstarted_priority_subset.v1'
                and excluded['runtime_authorized'] is False
                and metadata['assignments'] == excluded['assignments']
                and metadata['selection'] == excluded['selection'], 'complement_fixed_inputs')
        pool_ids = _unique([r['measurement_id'] for r in pool], 'complement_pool_alias')
        excluded_ids = _unique([r['measurement_id'] for r in excluded['rows']], 'complement_excluded_alias')
        by_id = {r['measurement_id']:r for r in pool}
        excluded_set = set(excluded_ids)
        require(excluded_set <= set(pool_ids), 'complement_excluded_outside_pool')
        unstarted = metadata['source_unstarted']
        role_map = metadata['pool_role_to_original_scope']
        require(role_map == {'mi':'mi', 'ny_saved_only':'ny', 'texas':'texas'},
                'complement_saved_role_labels')
        expected, prior = [], {}
        for row in pool:
            role = role_map[row['role']]; ordinal = row['ordinal']
            require(role in unstarted and type(ordinal) is int
                    and ordinal > prior.get(role, -1), 'complement_original_order')
            prior[role] = ordinal
            if row['measurement_id'] not in excluded_set:
                expected.append(dict(original_scope=role, original_ordinal=ordinal,
                    unstarted_ref=unstarted[role], **{k:row[k] for k in
                    ('measurement_id','run_id','question_id','seed','request_body_sha256','input_sha256')}))
        for row in excluded['rows']:
            original = by_id[row['measurement_id']]
            require(row['original_scope'] == role_map[original['role']]
                    and row['original_ordinal'] == original['ordinal']
                    and row['unstarted_ref'] == unstarted[row['original_scope']]
                    and all(row[k] == original[k] for k in
                        ('run_id','question_id','seed','request_body_sha256','input_sha256')),
                    'complement_excluded_identity')
        ids = [row['measurement_id'] for row in expected]
        require(metadata['rows'] == expected and metadata['pool_count'] == len(pool)
                and metadata['excluded_count'] == len(excluded_ids)
                and metadata['selected_count'] == len(expected)
                and metadata['selected_sorted_ids_sha256'] == digest(sorted(ids))
                and metadata['selected_ordered_ids_sha256'] == digest(ids), 'complement_exact_rows')
        return expected
    except (KeyError, TypeError, IndexError) as error:
        raise MixedOwnerError('complement_shape') from error
