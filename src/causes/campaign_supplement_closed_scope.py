"""Pure original-bank scope and cumulative custody for saved supplement audits.

The caller independently validates the pinned plan, predecessor and original
transfer review, then verifies every current native final and admission. These
helpers perform no observation, lock operation, write or qualification decision.
"""
from causes import campaign_supplement as core


EXTENSION_SCHEMA = 'causes.original_supplement_owner_bank_extension.v1'
DERIVED_EXTENSION_SCHEMA = 'causes.derived_supplement_owner_bank_extension.v1'


def require(ok, reason):
    if not ok:
        raise core.IntegrityError(reason)


def value(reference, read):
    import hashlib
    import json
    require(type(reference) is dict and set(reference) == {'path', 'bytes', 'sha256'},
            'closed_scope_pin')
    core.canonical(reference['path'])
    data = read(reference)
    require(type(data) is bytes and type(reference['bytes']) is int and
            len(data) == reference['bytes'] and hashlib.sha256(data).hexdigest() == reference['sha256'],
            'closed_scope_pin_bytes')
    return json.loads(data)


def unique_ids(items):
    require(type(items) is list and all(type(mid) is str and mid for mid in items) and
            len(items) == len(set(items)), 'closed_scope_duplicate_identity')
    return items


def resolve(plan, previous, *, original_transfer_jobs=None, derived_bank_jobs=None,
            read=core.pinned):
    """Return exact current jobs and their ordered cumulative owner bank.

    A same-bank invocation takes only the next original untouched prefix. An
    explicit transfer calls the independently reviewed donor resolver and adds
    its unchanged jobs after a completely occupied predecessor bank.
    """
    require(previous['schema'] == 'causes.finite_supplement_tranche_closed.v1',
            'closed_scope_predecessor_schema')
    require('derived_bank' in plan or not plan['family'].startswith('derived_probe_'),
            'closed_scope_explicit_derived_bank_required')
    prior = unique_ids(previous['complete_ids'])
    remaining = unique_ids(previous['unstarted_ids'])
    previous_jobs = value(previous['full_jobs'], read)
    require(type(previous_jobs) is list and
            unique_ids([j['measurement_id'] for j in previous_jobs]) == prior + remaining and
            plan['occupied_ids'] == prior, 'closed_scope_prior_partition')
    require(type(plan['maximum_new_requests']) is int and plan['maximum_new_requests'] > 0,
            'closed_scope_request_bound')
    extension = None
    derived = None
    carried_derived_extension = None
    required = [previous['full_jobs']]
    if 'derived_bank' in plan:
        require('original_reservation_transfer' not in plan and callable(derived_bank_jobs),
                'closed_scope_derived_resolver_required')
        derived = derived_bank_jobs(plan, previous, read=read)
        jobs, full_jobs = derived['jobs'], derived['cumulative_jobs']
        required.extend(derived['required_pins'])
        bank = value(plan['derived_bank']['actual'], read)
        base = value(bank['execution_predecessor']['actual'], read)
        required.extend([plan['derived_bank']['actual'], bank['execution_predecessor']['actual']])
        owner_jobs, bank_jobs = derived['prior_owner_jobs'], derived['bank_jobs']
        expected = dict(schema=DERIVED_EXTENSION_SCHEMA, adoption=plan['derived_bank'],
            execution_predecessor=bank['execution_predecessor'],
            previous_closed=bank['execution_predecessor'], previous_full_jobs=base['full_jobs'],
            source_family=plan['family'], model=plan['model'],
            prior_count=len(owner_jobs), appended_count=len(bank_jobs),
            appended_jobs_sha256=core.digest(bank_jobs),
            appended_ids_sha256=core.digest([j['measurement_id'] for j in bank_jobs]),
            cumulative_count=len(full_jobs), cumulative_jobs_sha256=core.digest(full_jobs),
            comparisons=derived['comparisons'], source_counts=derived['source_counts'],
            unique_missing_slots=derived['unique_missing_slots'])
        if plan['continuation_scope'] == 'adopted_derived_bank':
            require(not remaining and previous_jobs == owner_jobs,
                    'closed_scope_derived_initial_history')
            extension = expected
        else:
            require(plan['continuation_scope'] == 'derived_bank_remaining' and
                    previous_jobs == full_jobs, 'closed_scope_derived_resume_history')
            carried_derived_extension = previous['derived_bank_extension']
            expected_scope = dict(extension=expected, full_jobs=full_jobs)
            require(carried_derived_extension == bind_extension(expected_scope, previous['full_jobs']),
                    'closed_scope_derived_extension_changed')
    elif 'original_reservation_transfer' in plan:
        require(callable(original_transfer_jobs), 'closed_scope_transfer_resolver_required')
        transfer = original_transfer_jobs(plan, previous, read=read)
        require(not remaining, 'closed_scope_unfinished_predecessor')
        jobs = transfer['jobs']
        full_jobs = previous_jobs + jobs
        required.extend(transfer['required_pins'])
        extension = dict(schema=EXTENSION_SCHEMA,
            previous_closed=plan['predecessor_custody'], previous_full_jobs=previous['full_jobs'],
            transfer=plan['original_reservation_transfer'],
            source_family=plan['family'], model=plan['model'],
            prior_count=len(prior), appended_count=len(jobs),
            appended_jobs_sha256=core.digest(jobs),
            appended_ids_sha256=core.digest([j['measurement_id'] for j in jobs]),
            cumulative_count=len(full_jobs), cumulative_jobs_sha256=core.digest(full_jobs))
    else:
        full_jobs = previous_jobs
        jobs = full_jobs[len(prior):len(prior)+plan['maximum_new_requests']]
    require(jobs and len(jobs) == plan['maximum_new_requests'] and
            core.digest(jobs) == plan['jobs_sha256'] and
            all(j['family'] == plan['family'] and j['model'] == plan['model'] for j in jobs),
            'closed_scope_current_jobs')
    full_ids = unique_ids([j['measurement_id'] for j in full_jobs])
    require(full_ids[:len(prior)] == prior and
            full_ids[len(prior):len(prior)+len(jobs)] == [j['measurement_id'] for j in jobs],
            'closed_scope_ordered_extension')
    for key in ('maximum_admission_attempts', 'maximum_captures', 'maximum_capture_uses'):
        require(type(plan[key]) is int and plan[key] > 0, 'closed_scope_operational_budget')
    require(plan['maximum_captures'] <= plan['maximum_admission_attempts'],
            'closed_scope_capture_budget')
    result = dict(jobs=jobs, full_jobs=full_jobs, prior_ids=prior,
                prior_terminal=previous['terminal'], full_jobs_reference=previous['full_jobs'],
                extension=extension, required_pins=required)
    if derived is not None:
        result.update(derived_bank=derived, carried_derived_extension=carried_derived_extension)
    return result


def partition(scope, claimed_ids, terminal_ids):
    """Preserve all claims, including unresolved ones, without inventing finals."""
    claimed_ids = unique_ids(claimed_ids)
    terminal_ids = unique_ids(terminal_ids)
    current = [j['measurement_id'] for j in scope['jobs']]
    require(claimed_ids == current[:len(claimed_ids)] and
            set(terminal_ids) <= set(claimed_ids), 'closed_scope_current_partition')
    prior = scope['prior_ids']
    full = [j['measurement_id'] for j in scope['full_jobs']]
    require(type(scope['prior_terminal']) is int and
            0 <= scope['prior_terminal'] <= len(prior), 'closed_scope_prior_terminal')
    return dict(complete_ids=prior+claimed_ids,
                unstarted_ids=full[len(prior)+len(claimed_ids):],
                terminal=scope['prior_terminal']+len(terminal_ids),
                occupied=len(prior)+len(claimed_ids),
                unresolved_claimed_ids=[mid for mid in claimed_ids if mid not in set(terminal_ids)])


def bind_extension(scope, full_jobs_reference):
    """Bind the exact ordinary native JSON file written by the saved audit."""
    if scope['extension'] is None:
        require(full_jobs_reference == scope['full_jobs_reference'], 'closed_scope_same_bank_reference')
        return None
    data = core.encoded(scope['full_jobs'])
    import hashlib
    require(type(full_jobs_reference) is dict and
            set(full_jobs_reference) == {'path', 'bytes', 'sha256'} and
            full_jobs_reference['bytes'] == len(data) and
            full_jobs_reference['sha256'] == hashlib.sha256(data).hexdigest(),
            'closed_scope_extended_bank_bytes')
    core.canonical(full_jobs_reference['path'])
    return dict(scope['extension'], full_jobs=full_jobs_reference,
                prior_prefix_unchanged=True, native_families_preserved=True)
