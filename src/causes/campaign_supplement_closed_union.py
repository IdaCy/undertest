"""Normalize explicitly reviewed closed supplement evidence, without re-verifying probes.

Rows are never rewritten. Every origin names the actual invocation, original
artifact/index and source identities. A caller must authenticate the supplied
review references; an arbitrary self-authored review is not an authority.
"""
import copy
import hashlib
import json
from pathlib import Path

from shared import evidence_bytes


def require(value, reason):
    if not value:
        raise ValueError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def same(a, b):
    return encoded(a) == encoded(b)


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def unique(rows, key, reason):
    result = {}
    for row in rows:
        mid = row[key]
        require(type(mid) is str and mid and mid not in result, reason)
        result[mid] = row
    return result


def payload(reference, get, read_bytes, *, maximum_bytes):
    """Decode only an explicit exact-gzip record; retain its wrapper identity."""
    evidence_bytes._pin_shape(reference, max(maximum_bytes, evidence_bytes.MANIFEST_LIMIT))
    value = get(reference)
    if type(value) is list:
        require(type(reference['bytes']) is int and 0 <= reference['bytes'] <= maximum_bytes,
                'closed_payload_limit')
        return value
    require(type(value) is dict and value.get('schema') == 'shared.exact_gzip_bytes.v1',
            'closed_payload_schema')
    require(set(value) == {'schema', 'stored', 'raw'}, 'closed_payload_fields')
    evidence_bytes._digest_shape(value['raw'], maximum_bytes)
    evidence_bytes._pin_shape(value['stored'], evidence_bytes.ABSOLUTE_LIMIT)
    # The caller's reader records the compressed file for its final integrity pass.
    packed = read_bytes(value['stored'])
    require(type(packed) is bytes, 'closed_payload_bytes')
    raw = evidence_bytes.read_gzip(value, limit=maximum_bytes)
    require(hashlib.sha256(packed).hexdigest() == value['stored']['sha256']
            and len(packed) == value['stored']['bytes'], 'closed_stored_bytes_changed')
    value = evidence_bytes._json(raw)
    require(type(value) is list, 'closed_payload_list')
    return value



def original_bank_extension(saved, plan, prior, get):
    """Check an audited append of original donor jobs, never a replacement bank.

    The accepted closure establishes that the reviewed donor resolver checked
    saved donor custody. This helper joins that accepted transfer and its full
    native job digest; it does not rerun a collector, resolver or observation.
    """
    extension = saved.get('original_bank_extension')
    if extension is None:
        require('original_reservation_transfer' not in plan
                and same(saved['full_jobs'], prior['full_jobs']), 'completed_actor_same_bank')
        return None
    fields = {'schema', 'previous_closed', 'previous_full_jobs', 'transfer', 'source_family',
              'model', 'prior_count', 'appended_count', 'appended_jobs_sha256',
              'appended_ids_sha256', 'cumulative_count', 'cumulative_jobs_sha256',
              'full_jobs', 'prior_prefix_unchanged', 'native_families_preserved'}
    require(type(extension) is dict and set(extension) == fields
            and extension['schema'] == 'causes.original_supplement_owner_bank_extension.v1'
            and extension['prior_prefix_unchanged'] is True
            and extension['native_families_preserved'] is True
            and same(extension['previous_closed'], plan['predecessor_custody'])
            and same(plan['predecessor_custody'], dict(actual=saved['prior_closed'],
                                                     review=saved['prior_closed_review']))
            and same(extension['previous_full_jobs'], prior['full_jobs'])
            and same(extension['full_jobs'], saved['full_jobs'])
            and same(extension['transfer'], plan.get('original_reservation_transfer')),
            'original_bank_extension_binding')
    previous = get(prior['full_jobs']); full = get(saved['full_jobs'])
    previous_ids = list(unique(previous, 'measurement_id', 'original_bank_duplicate_prior'))
    full_ids = list(unique(full, 'measurement_id', 'original_bank_duplicate_job'))
    require(prior['unstarted_ids'] == [] and previous_ids == prior['complete_ids']
            and prior['terminal'] == len(previous) and prior.get('unresolved_claimed_ids', []) == []
            and prior['clean_native_ending'] is True
            and all(type(extension[k]) is int for k in ('prior_count', 'appended_count', 'cumulative_count'))
            and extension['prior_count'] == len(previous)
            and extension['appended_count'] > 0
            and extension['cumulative_count'] == len(full) == len(previous) + extension['appended_count']
            and same(full[:len(previous)], previous), 'original_bank_prefix')
    appended = full[len(previous):]
    require(type(plan['maximum_new_requests']) is int
            and plan['maximum_new_requests'] == len(appended)
            and extension['source_family'] == plan['family']
            and extension['model'] == plan['model']
            and plan['family'] in ('requested_five_historical_self_v1', 'candidate_saved_self_v1')
            and extension['appended_jobs_sha256'] == plan['jobs_sha256'] == digest(appended)
            and extension['appended_ids_sha256'] == digest(full_ids[len(previous):])
            and extension['cumulative_jobs_sha256'] == digest(full), 'original_bank_native_digest')
    physical = set()
    for job in full:
        require(job['measurement_id'] == '/'.join(job[k] for k in
                    ('family', 'model', 'run_id', 'stage', 'question_id'))
                and job['model'] == plan['model'] and job['stage'] == 'early'
                and job['question_id'] in ('evaluation_strongly_favors', 'evaluation_leans_towards_or_favors'),
                'original_bank_native_identity')
        key = (job['run_id'], job['stage'], job['question_id'])
        require(key not in physical, 'original_bank_physical_alias')
        physical.add(key)
    require(all(job['family'] == plan['family'] for job in appended), 'original_bank_native_family')
    transfer_ref = extension['transfer']
    require(type(transfer_ref) is dict and set(transfer_ref) == {'actual', 'review'},
            'original_bank_transfer_descriptor')
    transfer, peer = get(transfer_ref['actual']), get(transfer_ref['review'])
    require(peer.get('accepted_original_supplement_reservation_transfer') is True
            and peer.get('findings') == [] and same(peer.get('actual'), transfer_ref['actual'])
            and transfer['schema'] == 'causes.original_supplement_reservation_transfer.v1'
            and same(transfer['execution_predecessor'], extension['previous_closed'])
            and transfer['target_role'] == saved['role'] == 'mi'
            and transfer['target_output'] == saved['output']
            and transfer['family'] == plan['family'] and transfer['model'] == plan['model']
            and type(transfer['job_count']) is int and transfer['job_count'] == len(appended)
            and transfer['jobs_sha256'] == digest(appended), 'original_bank_accepted_transfer')
    invocations = saved['native_invocations']; before = prior['native_invocations']
    require(type(invocations) is list and type(before) is list
            and len(invocations) == len(before) + 1 and same(invocations[:-1], before),
            'original_bank_invocation_prefix')
    invocation = invocations[-1]
    require(same(invocation['plan'], saved['plan'])
            and same(invocation['source'], plan['sources']['driver'])
            and same(invocation['result'], saved['native_result'])
            and type(invocation['pid']) is int and invocation['pid'] > 0
            and invocation['process_absent'] is True
            and invocation['family'] == plan['family'] and invocation['model'] == plan['model']
            and invocation['jobs_sha256'] == plan['jobs_sha256']
            and same(invocation['original_reservation_transfer'], transfer_ref),
            'original_bank_invocation_identity')
    return copy.deepcopy(extension)

def admission_review_partition(saved, peer, mids, prior_count):
    """Read an authenticated review without altering its saved representation."""
    fields = ('qualified_admission_ids', 'invalid_admission_ids', 'invalid_admission_custody')
    present = [name in peer for name in fields]
    if any(present):
        require(all(present) and all(type(peer[name]) is list for name in fields),
                'closed_admission_review_fields')
        return tuple(peer[name] for name in fields)

    # Some independently reviewed clean deltas record counts instead of repeating
    # every ID. The caller has already checked their exact actual/source/flags,
    # native rows, claims, terminals and ordered complete-ID partition.
    require(peer.get('schema') == 'causes.completed_actor_saved_closure_review.v1'
            and peer.get('current_tranche_qualified') is True
            and saved.get('current_tranche_qualified') is True
            and peer.get('new_invalid_ids') == saved['new_invalid_ids'] == []
            and peer.get('current_qualification_findings')
                == saved['current_qualification_findings'] == []
            and same(peer.get('inherited_qualification_findings'),
                     saved['inherited_qualification_findings'])
            and same(peer.get('cumulative_invalid_ids'), saved['cumulative_invalid_ids'])
            and saved['new_scientifically_qualified_ids'] == mids,
            'closed_clean_admission_review')
    partition = peer.get('partition')
    require(type(partition) is dict, 'closed_clean_admission_partition')
    qualified_fields = [name for name in ('new_qualified', 'new_admission_qualified')
                        if name in partition]
    require(len(qualified_fields) == 1, 'closed_clean_admission_count_field')
    expected = dict(active=0, new_invalid=0, occupied=saved['occupied'],
                    prior_occupied=prior_count)
    expected[qualified_fields[0]] = len(mids)
    require(all(type(partition.get(name)) is int and partition[name] == value
                for name, value in expected.items()), 'closed_clean_admission_counts')
    for name in ('scientifically_qualified_total', 'qualified_custody_total'):
        if name in partition:
            require(type(partition[name]) is int
                    and partition[name] == saved['scientifically_qualified_total'],
                    'closed_clean_admission_total')
    return saved['new_scientifically_qualified_ids'], [], []


def normalize_closed_union(descriptor, get, read_bytes, legacy_rows):
    """Return native rows and explicit origins for a disjoint reviewed role union.

legacy_rows resolves the unchanged legacy lineage with the explicit full-bank
ending option. No collector, parser, queue, process check or model is invoked.
"""
    require(type(descriptor) is dict and set(descriptor) == {'schema', 'roots', 'maximum_payload_bytes'}
            and descriptor['schema'] == 'causes.supplement_closed_union.v1', 'closed_union_schema')
    limit = descriptor['maximum_payload_bytes']
    require(type(limit) is int and 0 < limit <= evidence_bytes.ABSOLUTE_LIMIT,
            'closed_union_payload_limit')
    roots = descriptor['roots']
    require(type(roots) is list and roots, 'closed_union_roots')
    roles = unique(roots, 'role', 'closed_union_duplicate_role')
    cache, journals, ancestry, extensions = {}, {}, set(), []

    def document(ref):
        key = encoded(ref)
        if key not in cache:
            cache[key] = get(ref)
        return cache[key]

    def review(actual_ref, peer_ref, direct=False):
        actual, peer = document(actual_ref), document(peer_ref)
        flag = ('accepted_finite_supplement_closed_custody' if
                actual.get('full_tranche_qualified') is False or direct else
                'accepted_bounded_finite_tranche_closed')
        require(peer.get(flag) is True and peer.get('findings') == []
                and same(peer.get('actual'), actual_ref), 'closed_union_review')
        if direct:
            require(peer.get('accepted_completed_actor_saved_closure') is True
                    and same(peer.get('source'), actual['source'])
                    and peer.get('original_denominator_preserved') is True
                    and peer.get('all_original_attempts_retained') is True
                    and same(peer.get('qualification_findings'), actual['qualification_findings']),
                    'completed_actor_review')
        return actual, peer, flag

    def terminals(saved):
        ref = saved['journal']; key = encoded(ref)
        if key not in journals:
            raw = read_bytes(ref)
            require(type(raw) is bytes, 'closed_journal_bytes')
            events = [evidence_bytes._json(line) for line in raw.splitlines()]
            terms = unique([e for e in events if e.get('event') in ('route_success', 'route_failure')],
                           'measurement_id', 'closed_duplicate_terminal')
            journals[key] = (events, terms)
        return journals[key]

    def enrich(row, origin, review_ref, flag, sources, invocation):
        saved = document(origin['actual']); events, terms = terminals(saved)
        mid = row['measurement_id']; require(mid in terms, 'closed_missing_terminal')
        terminal = terms[mid]
        require(same(terminal['final'], row['final'])
                and terminal['technical_status'] == row['technical_status'], 'closed_terminal_row')
        out = Path(saved['output'])
        require(out.is_absolute() and Path(row['final']['path']).is_relative_to(out)
                and all(Path(r['path']).is_relative_to(out) for r in row['capture_files']),
                'closed_invocation_output')
        origin.update(review=review_ref, accepted_flag=flag, plan=saved['plan'],
                      output_root=saved['output'], journal=saved['journal'],
                      terminal=copy.deepcopy(terminal), sources=copy.deepcopy(sources),
                      invocation=copy.deepcopy(invocation))

    def walk(actual_ref, peer_ref, role):
        key = actual_ref['path']
        require(key not in ancestry and len(ancestry) < 128, 'closed_union_cycle_or_depth')
        ancestry.add(key)
        saved = document(actual_ref)
        require(saved['schema'] in ('causes.finite_supplement_tranche_closed.v1',
                                    'causes.finite_zero_claim_closed_extension.v1')
                and saved['role'] == role and type(saved['active']) is int
                and saved['active'] == 0 and saved.get('ambiguous') == [], 'closed_union_identity')
        direct = saved.get('mode') == 'completed_actor_direct_probe'
        saved, peer, flag = review(actual_ref, peer_ref, direct)
        plan = document(saved['plan'])
        if not direct:
            require('mode' not in saved, 'unknown_closed_mode')
            sources = dict(collector=saved['native_collector'], runner=saved['native_runner'],
                           adapter=saved['adapter'])
            part = dict(role=role, jobs=plan['jobs'], endpoint=plan['endpoint'],
                        output_root=saved['output'])
            reviews = {actual_ref['path']: peer_ref}; cursor = saved; seen = set()
            while 'prior_closed' in cursor or 'previous' in cursor:
                prior_key = 'prior_closed' if 'prior_closed' in cursor else 'previous'
                require(not ('prior_closed' in cursor and 'previous' in cursor), 'closed_parent_ambiguous')
                ref, pref = cursor[prior_key], cursor[prior_key + '_review']
                require(ref['path'] not in seen and len(seen) < 128, 'closed_legacy_cycle')
                seen.add(ref['path']); reviews[ref['path']] = pref; cursor = document(ref)
            rows, origins = legacy_rows(actual_ref, part, sources, saved['identity'], document,
                                        qualification_reviews=reviews, allow_full_bank=True)
            for row, origin in zip(rows, origins):
                old = document(origin['actual']); pref = reviews[origin['actual']['path']]
                _, _, oldflag = review(origin['actual'], pref)
                enrich(row, origin, pref, oldflag, dict(collector=old['native_collector'],
                    runner=old['native_runner'], adapter=old['adapter']),
                    dict(kind='legacy_executor', identity=old['identity']))
            return rows, origins
        require(saved.get('clean_native_ending') is True and saved.get('unresolved_claimed_ids') == []
                and saved.get('original_denominator_preserved') is True
                and saved.get('all_original_attempts_retained') is True,
                'completed_actor_closed_custody')
        prior_ref, prior_peer = saved['prior_closed'], saved['prior_closed_review']
        prior = document(prior_ref)
        require(same(plan['predecessor_custody'], dict(actual=prior_ref, review=prior_peer))
                and plan['schema'] == 'causes.completed_actor_driver_plan.v1'
                and plan['mode'] == saved['mode'] and plan['output_root'] == saved['output']
                and plan['occupied_ids'] == prior['complete_ids']
                and type(plan['initial_failures']) is int
                and plan['initial_failures'] >= 0
                and plan['initial_failures'] == prior['consecutive_failures']
                and same(saved['inherited_qualification_findings'], prior['qualification_findings'])
                and same(saved['qualification_findings'], saved['inherited_qualification_findings']
                         + saved['current_qualification_findings']), 'completed_actor_predecessor')
        extension = original_bank_extension(saved, plan, prior, document)
        previous_rows, origins = walk(prior_ref, prior_peer, role)
        if extension is not None:
            extensions.append(dict(actual=actual_ref, review=peer_ref, extension=extension))
        full = document(saved['full_jobs']); by_id = unique(full, 'measurement_id', 'closed_duplicate_job')
        old_ids = [r['measurement_id'] for r in previous_rows]
        require(old_ids == prior['complete_ids'], 'completed_actor_prior_rows')
        available = [mid for mid in by_id if mid not in set(old_ids)]
        maximum = plan['maximum_new_requests']
        require(type(maximum) is int and 0 < maximum <= len(available), 'completed_actor_request_bound')
        selected = available[:maximum]
        selected_jobs = [by_id[mid] for mid in selected]
        require(plan['jobs_sha256'] == digest(selected_jobs), 'completed_actor_selected_jobs')
        recipe = document(saved['recipe'])
        require(same(recipe['source'], saved['source']) and same(recipe['plan'], saved['plan'])
                and same(recipe['driver_result'], saved['native_result']), 'completed_actor_recipe')
        constructor = document(plan['constructor'])['bindings']
        sources = dict(collector=recipe['native_collector'], runner=constructor['runner'],
                       adapter=constructor['adapter'])
        # This collector is the native verifier; plan.sources.collector is transport.
        source_roster = payload(saved['source_pins'], document, read_bytes, maximum_bytes=limit)
        for ref in [saved['source'], constructor['runner'], constructor['adapter']]:
            require(ref in source_roster, 'completed_actor_source_roster')
            read_bytes(ref)
        # The audit loads the verifier from its recipe before collecting its
        # transitive source roster; retain that genuine, separately pinned join.
        read_bytes(recipe['native_collector'])
        rows = payload(saved['verified_finals'], document, read_bytes, maximum_bytes=limit)
        mids = list(unique(rows, 'measurement_id', 'completed_actor_duplicate_row'))
        require(saved['complete_ids'] == old_ids + mids
                and mids == selected[:len(mids)]
                and saved['terminal'] == saved['occupied'] == len(old_ids) + len(rows)
                and saved['unstarted_ids'] == [mid for mid in by_id if mid not in set(saved['complete_ids'])],
                'completed_actor_complete_partition')
        result = document(saved['native_result'])['result']
        require(all(type(result[k]) is int and result[k] >= 0 for k in
                    ('active', 'existing_final', 'new_final', 'remaining_unclaimed', 'consecutive_failures'))
                and all(type(saved[k]) is int and saved[k] >= 0 for k in
                    ('active', 'terminal', 'occupied', 'consecutive_failures', 'scientifically_qualified_total')),
                'completed_actor_count_types')
        require(result['phase'] == saved['mode'] and result['active'] == 0
                and result['existing_final'] == 0 and result['new_final'] == len(rows)
                and result['remaining_unclaimed'] == maximum - len(rows)
                and result['stop_reason'] == saved['stop_reason']
                and result['consecutive_failures'] == saved['consecutive_failures']
                and len(result['completed_ids']) == len(set(result['completed_ids'])) == len(rows)
                and set(result['completed_ids']) == set(mids), 'completed_actor_native_result')
        events, terms = terminals(saved)
        claims = unique([e for e in events if e.get('event') == 'claimed'],
                        'measurement_id', 'completed_actor_duplicate_claim')
        require(set(claims) == set(terms) == set(mids)
                and list(terms) == result['completed_ids'], 'completed_actor_journal_partition')
        counter = plan['initial_failures']
        for terminal in terms.values():
            counter = 0 if terminal['event'] == 'route_success' else counter + 1
            require(type(terminal['consecutive_failures']) is int
                    and terminal['consecutive_failures'] == counter, 'completed_actor_failure_history')
        require(counter == saved['consecutive_failures'], 'completed_actor_ending_counter')
        findings = unique(saved['current_qualification_findings'], 'measurement_id', 'closed_duplicate_finding')
        valid, invalid, invalid_custody = admission_review_partition(saved, peer, mids, len(old_ids))
        require(len(set(valid)) == len(valid) and len(set(invalid)) == len(invalid)
                and not set(valid) & set(invalid) and set(valid) | set(invalid) == set(mids)
                and set(invalid) == set(findings)
                and saved['new_scientifically_qualified_ids'] == valid and saved['new_invalid_ids'] == invalid
                and saved['prior_invalid_ids'] == prior.get('cumulative_invalid_ids', peer.get('inherited_invalid_admission_ids')),
                'completed_actor_admission_partition')
        require(saved['cumulative_invalid_ids'] == saved['prior_invalid_ids'] + invalid
                and len(set(saved['cumulative_invalid_ids'])) == len(saved['cumulative_invalid_ids'])
                and set(saved['cumulative_invalid_ids']) <= set(saved['complete_ids'])
                and saved['scientifically_qualified_total'] == len(saved['complete_ids']) - len(saved['cumulative_invalid_ids']),
                'completed_actor_cumulative_qualification')
        custody = unique(invalid_custody, 'measurement_id', 'closed_duplicate_invalid_custody')
        require(set(custody) == set(invalid), 'closed_invalid_custody_partition')
        for index, row in enumerate(rows):
            require('qualification_error' not in row, 'closed_row_changed')
            mid = row['measurement_id']
            origin = dict(role=role, actual=actual_ref, verified_finals=saved['verified_finals'],
                          verified_index=index, row_sha256=digest(row))
            enrich(row, origin, peer_ref, flag, sources,
                   dict(kind='completed_actor', constructor=plan['constructor'],
                        recipe=saved['recipe'], driver=plan['sources']['driver'],
                        native_queue=plan['sources']['native_queue'],
                        transport=plan['sources']['collector']))
            if extension is not None:
                origin['invocation']['original_bank_extension'] = copy.deepcopy(extension)
            if mid in findings:
                finding, evidence = findings[mid], custody[mid]
                require(set(finding) == {'measurement_id','stage','error'}
                        and finding['stage'] == 'native_admission' and type(finding['error']) is str
                        and finding['error']
                        and evidence.get('scientific_qualified') is False
                        and evidence.get('request_replay_permitted') is False, 'closed_invalid_admission')
                require(same(document(evidence['observation']), document(evidence['native_saved_observation']))
                        and evidence['observation']['bytes'] == evidence['native_saved_observation']['bytes']
                        and evidence['observation']['sha256'] == evidence['native_saved_observation']['sha256'],
                        'closed_invalid_observation_identity')
                origin['admission_qualification'] = dict(measurement_id=mid, qualified=False,
                    actual=actual_ref, review=peer_ref, accepted_flag=flag, audit_source=saved['source'],
                    condition='accepted_closed_native_admission_failure', verified_finals=saved['verified_finals'],
                    verified_index=index, row_sha256=digest(row), finding=copy.deepcopy(finding), custody=copy.deepcopy(evidence))
            origins.append(origin)
        return previous_rows + copy.deepcopy(rows), origins

    rows, origins = [], []
    for role, root in roles.items():
        require(set(root) == {'role','actual','review'}, 'closed_union_root_fields')
        values, mapping = walk(root['actual'], root['review'], role)
        rows.extend(values); origins.extend(mapping)
    require(len(unique(rows, 'measurement_id', 'closed_union_overlap')) == len(origins),
            'closed_union_origin_count')
    result = dict(schema='causes.normalized_supplement_closed_union.v1', rows=rows, origins=origins,
                  roots=copy.deepcopy(roots), original_rows_unchanged=True, native_verifier_invocations=0)
    if extensions:
        result['original_bank_extensions'] = extensions
    return result



def validate_global_jobs_extension(data, normalized, get, jobs):
    """Reconstruct an explicit global-bank append without changing assignments."""
    extensions = normalized.get('original_bank_extensions', [])
    if not extensions:
        require('global_jobs_extension' not in data, 'global_extension_without_closed_bank')
        return
    require('global_jobs_extension' in data, 'global_jobs_extension_required')
    descriptor = get(data['global_jobs_extension'])
    require(type(descriptor) is dict and set(descriptor) == {
                'schema', 'previous_global_jobs', 'previous_assignments', 'global_jobs',
                'assignments', 'closed_bank_extensions'}
            and descriptor['schema'] == 'causes.supplement_global_jobs_extension.v1'
            and same(descriptor['global_jobs'], data['global_jobs'])
            and same(descriptor['assignments'], data['assignments'])
            and same(descriptor['previous_assignments'], data['assignments'])
            and same(descriptor['closed_bank_extensions'],
                     [dict(actual=e['actual'], review=e['review']) for e in extensions]),
            'global_jobs_extension_binding')
    previous = get(descriptor['previous_global_jobs'])
    existing = unique(previous, 'measurement_id', 'global_extension_duplicate_prior')
    expected = copy.deepcopy(previous)
    require(type(jobs) is list and len(jobs) >= len(previous)
            and same(jobs[:len(previous)], previous), 'global_jobs_prefix_changed')
    physical = {(j['run_id'], j['stage'], j['question_id']): j['measurement_id'] for j in previous}
    require(len(physical) == len(previous), 'global_prior_physical_alias')
    for proof in extensions:
        actual, peer = get(proof['actual']), get(proof['review'])
        extension = proof['extension']
        require(peer.get('accepted_completed_actor_saved_closure') is True
                and peer.get('accepted_finite_supplement_closed_custody') is True
                and peer.get('original_denominator_preserved') is True
                and peer.get('all_original_attempts_retained') is True
                and peer.get('findings') == [] and same(peer.get('actual'), proof['actual'])
                and same(peer.get('source'), actual['source'])
                and same(peer.get('qualification_findings'), actual['qualification_findings'])
                and same(actual.get('original_bank_extension'), extension),
                'global_extension_closed_peer')
        # Reuse the typed accepted-transfer join, never infer membership from a
        # marker or from a run merely appearing in the assignment roster.
        checked = original_bank_extension(actual, get(actual['plan']),
                                          get(actual['prior_closed']), get)
        require(same(checked, extension), 'global_extension_actual_identity')
        full = get(extension['full_jobs'])
        for job in full[extension['prior_count']:]:
            mid = job['measurement_id']; key = (job['run_id'], job['stage'], job['question_id'])
            if mid in existing:
                require(same(existing[mid], job), 'global_existing_job_changed')
            else:
                require(key not in physical, 'global_extension_physical_alias')
                physical[key] = mid; existing[mid] = job; expected.append(copy.deepcopy(job))
    require(same(jobs, expected), 'global_jobs_exact_append')

def validate_membership(data, normalized, get):
    """Join an explicitly pinned fixed membership to its invocation-local custody."""
    # Avoid reloading large immutable banks/plans for each row. The caller owns
    # authenticated reads and its final integrity pass over all referenced files.
    reader, documents = get, {}
    def get(reference):
        key = encoded(reference)
        if key not in documents:
            documents[key] = reader(reference)
        return documents[key]
    jobs = get(data['global_jobs']); by_id = unique(jobs, 'measurement_id', 'duplicate_global_jobs')
    validate_global_jobs_extension(data, normalized, get, jobs)
    membership = get(data['membership']); members = unique(membership['rows'], 'measurement_id', 'duplicate_members')
    assignments = get(data['assignments']); peer = get(data['assignment_review'])
    require(peer.get(data['assignment_acceptance_flag']) is True and peer.get('findings') == []
            and same(peer.get('assignments'), data['assignments']), 'normalized_assignments')
    require(same(membership['global_jobs'], data['global_jobs'])
            and same(membership['assignments'], data['assignments'])
            and membership['active_rows'] == [], 'normalized_membership_inputs')
    parts = unique(data['parts'], 'role', 'normalized_duplicate_part')
    require(set(parts) == {root['role'] for root in normalized['roots']}, 'normalized_role_partition')
    for root in normalized['roots']:
        part = parts[root['role']]; actual = get(root['actual']); plan = get(actual['plan'])
        bank_ref = actual.get('full_jobs', plan.get('jobs'))
        bank = get(bank_ref)
        require(same(part['actual'], root['actual']) and same(part['review'], root['review'])
                and same(part['plan'], actual['plan']) and same(part['jobs'], bank_ref)
                and part['output_root'] == actual['output'] == plan['output_root']
                and part['endpoint'] == plan['endpoint']
                and type(part['terminal_count']) is int and part['terminal_count'] == actual['terminal']
                and all(same(by_id.get(job['measurement_id']), job) for job in bank),
                'normalized_part_identity')
    values = unique(normalized['rows'], 'measurement_id', 'normalized_duplicate_row')
    origins = {r['measurement_id']: o for r,o in zip(normalized['rows'],normalized['origins'])}
    ordered = [mid for mid in by_id if mid in members]
    require(set(values) == set(members) and membership['measurement_ids'] == ordered
            and [m['measurement_id'] for m in membership['rows']] == ordered,
            'normalized_membership_partition')
    for ordinal, job in enumerate(jobs):
        mid = job['measurement_id']
        if mid not in members: continue
        member, row, origin = members[mid], values[mid], origins[mid]
        plan = get(origin['plan']); bank_ref = get(origin['actual']).get('full_jobs', plan.get('jobs'))
        bank = get(bank_ref); local_index = member['job_index']
        require(type(local_index) is int and 0 <= local_index < len(bank)
                and same(bank[local_index], job) and same(member['jobs'], bank_ref)
                and type(member['global_job_ordinal']) is int and member['global_job_ordinal'] == ordinal
                and member['global_job_sha256'] == digest(job)
                and Path(member['input']['path']) == Path(row['final']['path']).parent / 'INPUT.json'
                and same(get(member['input']), job), 'normalized_job_identity')
        require(member['role'] == origin['role'] and member['endpoint'] == plan['endpoint']
                and same(member['terminal'], origin['terminal'])
                and same(member['final'], row['final']) and same(member['capture_files'], row['capture_files']),
                'normalized_native_custody')
        for key in ('run_id','stage','question_id','context','frozen_config','original_refs'):
            require(same(member[key],job[key]), 'normalized_job_field:' + key)
        require(member['epoch'] == assignments['epochs'][job['run_id']]
                and member['readout'] == 'self_' + job['question_id'], 'normalized_epoch')
    return jobs, membership, assignments, [values[mid] for mid in ordered], [origins[mid] for mid in ordered]


def projected_evidence(membership_ref, membership, verified_ref, verified, origins):
    require(len(verified) == len(origins) == len(membership['rows'])
            and len(unique(verified, 'measurement_id', 'projected_duplicate_row')) == len(verified)
            and all(row['measurement_id'] == member['measurement_id']
                    and same(row['final'], member['final'])
                    and same(row['capture_files'], member['capture_files'])
                    and origin['row_sha256'] == digest(row)
                    for row, origin, member in zip(verified, origins, membership['rows'])),
            'projected_origin_membership')
    return {row['measurement_id']: dict(measurement_id=row['measurement_id'], final=row['final'],
        request_sha256=row['request_body_sha256'], verified_finals=copy.deepcopy(verified_ref),
        verified_index=i, native_collector=origin['sources']['collector'],
        custody=dict(actual=origin['actual'], review=origin['review'], accepted_flag=origin['accepted_flag'],
                     closed_run_id=member['run_id'], terminal=True, membership=membership_ref,
                     invocation=copy.deepcopy(origin['invocation']), plan=origin['plan'],
                     output_root=origin['output_root'], journal=origin['journal']))
        for i,(row,origin,member) in enumerate(zip(verified,origins,membership['rows']))}
