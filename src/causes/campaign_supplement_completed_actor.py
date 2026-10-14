"""Completed-actor admission composition for the unchanged supplement queue.

This module supplies no authority, host adapter, ownership lock, job selection,
or capacity certification. An independently reviewed caller must bind accepted
completed-actor and predecessor custody, hold the exclusive executor lock, and
provide the live guard, provider capture, source and original request callbacks.
Every callback result is retained before it can authorize a claim. A cached
provider snapshot keeps its original clocks; local checks never renew them.
"""
import copy
import datetime as dt
import hashlib
import json

from causes import campaign_supplement as core


MODE = 'completed_actor_direct_probe'
CONTRACT_SCHEMA = 'causes.completed_actor_direct_probe_contract.v1'
PROVIDER_SCHEMA = 'causes.completed_actor_provider_observation.v1'
GUARD_SCHEMA = 'causes.completed_actor_claim_guard.v1'
ADMISSION_SCHEMA = 'causes.completed_actor_admission.v1'


def require(value, reason):
    if not value:
        raise core.IntegrityError(reason)


def instant(value):
    parsed = dt.datetime.fromisoformat(value)
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
            'completed_actor_naive_clock')
    return parsed


def read_raw(reference, read):
    require(type(reference) is dict and set(reference) == {'path', 'bytes', 'sha256'},
            'completed_actor_pin')
    core.canonical(reference['path'])
    raw = read(reference)
    require(type(raw) is bytes and type(reference['bytes']) is int and
            len(raw) == reference['bytes'] and
            hashlib.sha256(raw).hexdigest() == reference['sha256'], 'completed_actor_pin_bytes')
    return raw


def read_value(reference, read):
    return json.loads(read_raw(reference, read))


def verify_evidence(references, read):
    require(type(references) is list and references, 'completed_actor_evidence_required')
    for reference in references:
        read_raw(reference, read)


def original_transfer_jobs(plan, previous, *, read=core.pinned):
    """Resolve a reviewed transfer of closed original reservations, without I/O effects.

    Execution ownership remains with ``predecessor_custody``. Donors supply only
    original job objects and closed attempt history; their wrappers never resume.
    The caller must hold the returned donor locks alongside its predecessor lock.
    """
    refs = {}

    def pinned(ref):
        data = read_raw(ref, read)
        require(ref['path'] not in refs or refs[ref['path']] == ref,
                'original_transfer_conflicting_pin')
        refs[ref['path']] = ref
        return data

    def value(ref):
        return json.loads(pinned(ref))

    def accepted(descriptor, flag):
        require(type(descriptor) is dict and set(descriptor) == {'actual', 'review'},
                'original_transfer_descriptor')
        actual = value(descriptor['actual'])
        peer = value(descriptor['review'])
        require(peer.get(flag) is True and peer.get('actual') == descriptor['actual'] and
                peer.get('findings') == [], 'original_transfer_independent_peer')
        return actual

    def identifiers(items):
        require(type(items) is list and all(type(mid) is str and mid for mid in items) and
                len(items) == len(set(items)), 'original_transfer_duplicate_identity')
        return set(items)

    def physical(job):
        require(job['model'] == plan['model'] and job['stage'] == 'early' and
                job['question_id'] in ('evaluation_strongly_favors',
                                       'evaluation_leans_towards_or_favors') and
                job['measurement_id'] == '/'.join(job[k] for k in
                    ('family', 'model', 'run_id', 'stage', 'question_id')),
                'original_transfer_scientific_identity')
        return job['run_id'], job['stage'], job['question_id']

    transfer = accepted(plan['original_reservation_transfer'],
                        'accepted_original_supplement_reservation_transfer')
    require(transfer['schema'] == 'causes.original_supplement_reservation_transfer.v1' and
            transfer['execution_predecessor'] == plan['predecessor_custody'] and
            transfer['target_role'] == 'mi' and transfer['target_output'] == plan['output_root'] and
            transfer['family'] == plan['family'] and transfer['model'] == plan['model'] and
            transfer['family'] in ('candidate_saved_self_v1', 'requested_five_historical_self_v1'),
            'original_transfer_scope')
    require(previous['unstarted_ids'] == [] and previous['active'] == 0 and
            previous['ambiguous'] == [] and previous['clean_native_ending'] is True and
            previous['consecutive_failures'] == plan['initial_failures'],
            'original_transfer_priority_predecessor_not_complete')
    owner_jobs = value(previous['full_jobs'])
    require(identifiers([job['measurement_id'] for job in owner_jobs]) ==
            identifiers(previous['complete_ids']), 'original_transfer_owner_partition')
    owner_physical = {physical(job) for job in owner_jobs}
    require(len(owner_physical) == len(owner_jobs), 'original_transfer_owner_alias')
    donors = transfer['source_reservations']
    require(type(donors) is list and donors and
            len({row['role'] for row in donors}) == len(donors), 'original_transfer_donors')
    source_rows, source_jobs, donor_locks, counters, occupied = {}, {}, [], {}, set()
    all_ids, all_physical = set(), set()
    historical = {
        'mi': ('causes.requested_priority_mi_self_drain.v1',
               'accepted_user_requested_mi_self_drained_custody'),
        'texas': ('causes.user_requested_texas_self_drain.v1',
                  'accepted_user_requested_texas_self_drained_custody'),
        'ny': ('causes.user_requested_ny_self_drain.v1',
               'accepted_user_requested_ny_self_drained_custody'),
    }
    for donor in donors:
        require(set(donor) == {'kind', 'role', 'custody'} and donor['role'] in historical,
                'original_transfer_donor_fields')
        role = donor['role']
        if donor['kind'] == 'historical_drained':
            actual = accepted(donor['custody'], historical[role][1])
            require(actual['schema'] == historical[role][0] and
                    actual['all_claimed_terminal'] is True and actual['counts']['active'] == 0,
                    'original_transfer_historical_ending')
            jobs = value(actual['original_jobs'])
            unstarted = value(actual['unstarted'])
            terminal = value(actual['terminal_custody'])
            complete_ids = terminal['claimed_ids']
            require(identifiers(complete_ids) == identifiers(terminal['terminal_ids']),
                    'original_transfer_unresolved_attempt')
            require(unstarted['schema'] == 'causes.unstarted_fixed_self_reservation.v1' and
                    unstarted['jobs'] == actual['original_jobs'] and
                    unstarted['assignment_or_seed_changed'] is False and
                    unstarted['original_count'] == len(jobs) and
                    unstarted['unstarted_count'] == len(unstarted['rows']),
                    'original_transfer_unstarted_binding')
            rows = unstarted['rows']
            ordinals = [row['ordinal'] for row in rows]
            require(all(type(i) is int and 0 <= i < len(jobs) for i in ordinals) and
                    ordinals == sorted(set(ordinals)), 'original_transfer_original_order')
            remaining = []
            for row in rows:
                job = jobs[row['ordinal']]
                require(row['input_sha256'] == core.digest(job) and
                        all(row[key] == job[key] for key in (
                            'measurement_id', 'native_job_sha256', 'original_seed', 'seed',
                            'context_canonical_sha256', 'question_canonical_sha256',
                            'request_body_sha256', 'request_file_sha256')),
                        'original_transfer_original_job_changed')
                remaining.append(job)
            require(actual['counts']['assigned'] == len(jobs) and
                    actual['counts']['claimed'] == actual['counts']['terminal'] == len(complete_ids) and
                    actual['counts']['unstarted'] == len(remaining),
                    'original_transfer_historical_counts')
            counters[role] = actual['counts']['consecutive_failures']
            # The accepted snapshot establishes absence; the caller still takes the
            # exact saved lock and keeps the saved journal and pause bytes pinned.
            require(actual['original_process_absence'] and
                    all(row['absent'] is True for row in actual['original_process_absence']) and
                    actual['executor_lock']['free'] is True,
                    'original_transfer_historical_owner')
            pinned(actual['pause'])
            journal = actual['journal_original']
            source_rows[role] = (actual['unstarted'], {r['measurement_id']: r for r in rows})
        elif donor['kind'] == 'finite_closed':
            require(role == 'texas' and transfer['family'] == 'requested_five_historical_self_v1',
                    'original_transfer_finite_scope')
            actual = accepted(donor['custody'], 'accepted_bounded_finite_tranche_closed')
            require(actual['schema'] == 'causes.finite_supplement_tranche_closed.v1' and
                    actual['role'] == role and actual['active'] == 0 and actual['ambiguous'] == [] and
                    actual['clean_native_ending'] is True and actual['full_tranche_qualified'] is True and
                    actual['qualification_findings'] == [] and actual['all_technical_complete'] is True,
                    'original_transfer_finite_ending')
            donor_plan = value(actual['plan'])
            jobs = value(donor_plan['jobs'])
            complete_ids = actual['complete_ids']
            ids = identifiers(actual['unstarted_ids'])
            remaining = [job for job in jobs if job['measurement_id'] in ids]
            require([job['measurement_id'] for job in remaining] == actual['unstarted_ids'] and
                    actual['terminal'] == len(complete_ids), 'original_transfer_finite_order')
            counters[role] = actual['consecutive_failures']
            require(type(actual['all_invocation_pids_absent']) is list and
                    actual['all_invocation_pids_absent'] and actual['process_absence'] and
                    actual['all_invocation_pids_absent'] ==
                        [row['pid'] for row in actual['process_absence']] and
                    all(type(row['pid']) is int and row['pid'] > 0 and
                        row['expected_start_ticks'] and row['argv']
                        for row in actual['process_absence']) and
                    actual['executor_lock']['acquired_nonblocking'] is True and
                    actual['executor_lock']['released'] is True,
                    'original_transfer_finite_owner')
            journal = actual['original_journal']
        else:
            raise core.IntegrityError('original_transfer_donor_kind')
        require(type(counters[role]) is int and counters[role] == 0,
                'original_transfer_nonzero_donor_counter')
        pinned(journal)
        full_ids = identifiers([job['measurement_id'] for job in jobs])
        done = identifiers(complete_ids)
        todo = identifiers([job['measurement_id'] for job in remaining])
        require(not done.intersection(todo) and full_ids == done | todo,
                'original_transfer_donor_partition')
        physical_ids = {physical(job) for job in jobs}
        require(len(physical_ids) == len(jobs) and not full_ids.intersection(all_ids) and
                not physical_ids.intersection(all_physical) and
                all(job['family'] == transfer['family'] for job in jobs),
                'original_transfer_duplicate_donor')
        all_ids.update(full_ids); all_physical.update(physical_ids); occupied.update(done)
        source_jobs[role] = remaining
        lock = actual['executor_lock']
        require(type(lock['device']) is int and type(lock['inode']) is int and
                lock['device'] > 0 and lock['inode'] > 0, 'original_transfer_lock_identity')
        core.canonical(lock['path'])
        donor_locks.append({key: lock[key] for key in ('path', 'device', 'inode')})
    selection = transfer['selection']
    if selection['mode'] == 'entire_unstarted':
        require(len(donors) == 1 and set(selection) == {'mode'} and
                donors[0]['kind'] == 'finite_closed', 'original_transfer_entire_scope')
        selected = source_jobs[donors[0]['role']]
    elif selection['mode'] == 'saved_priority':
        require(set(selection) == {'mode', 'metadata'} and
                set(source_rows) == {'mi', 'texas', 'ny'} and
                transfer['family'] == 'candidate_saved_self_v1', 'original_transfer_priority_scope')
        metadata = value(selection['metadata'])
        require(metadata['schema'] == 'causes.saved_unstarted_priority_subset.v1' and
                metadata['runtime_authorized'] is False, 'original_transfer_priority_metadata')
        value(metadata['assignments']); value(metadata['selection'])
        selected = []
        indexed = {role: {job['measurement_id']: job for job in jobs}
                   for role, jobs in source_jobs.items()}
        previous_ordinal = {}
        for row in metadata['rows']:
            role = row['original_scope']
            require(role in source_rows and row['unstarted_ref'] == source_rows[role][0] and
                    row['measurement_id'] in indexed[role], 'original_transfer_priority_membership')
            saved = source_rows[role][1][row['measurement_id']]
            job = indexed[role][row['measurement_id']]
            require(row['original_ordinal'] == saved['ordinal'] and
                    row['original_ordinal'] > previous_ordinal.get(role, -1) and
                    row['input_sha256'] == core.digest(job) and
                    all(row[key] == job[key] for key in
                        ('measurement_id', 'run_id', 'question_id', 'seed', 'request_body_sha256')),
                    'original_transfer_priority_identity')
            previous_ordinal[role] = row['original_ordinal']; selected.append(job)
    else:
        raise core.IntegrityError('original_transfer_selection_mode')
    selected_ids = identifiers([job['measurement_id'] for job in selected])
    require(selected and not selected_ids.intersection(occupied) and
            not selected_ids.intersection(previous['complete_ids']) and
            not {physical(job) for job in selected}.intersection(owner_physical) and
            type(transfer['job_count']) is int and transfer['job_count'] == len(selected) and
            transfer['jobs_sha256'] == core.digest(selected) == plan['jobs_sha256'] and
            transfer['donor_counters'] == counters and
            transfer['donor_occupied_ids_sha256'] == core.digest(sorted(occupied)),
            'original_transfer_complete_selection')
    require(len({row['path'] for row in donor_locks}) == len(donor_locks),
            'original_transfer_duplicate_lock')
    return dict(jobs=selected, donor_locks=donor_locks, required_pins=list(refs.values()),
                donor_counters=counters, donor_occupied_ids=sorted(occupied))


def validate_contract(plan, contract, jobs, *, initial_failures, max_clients, read=core.pinned):
    """Bind independently accepted custody and the next original untouched jobs."""
    require(plan['mode'] == MODE and contract['schema'] == CONTRACT_SCHEMA,
            'completed_actor_explicit_mode_required')
    require(contract['active_actor_roles'] == [] and
            plan['completed_actor_custody'] == contract['completed_actor_custody'] and
            plan['predecessor_custody'] == contract['predecessor_custody'] and
            bool(plan['completed_actor_custody']) and bool(plan['predecessor_custody']),
            'completed_actor_custody_required')
    require(type(max_clients) is int and max_clients in (1, 2, 4, 8, 12, 16) and
            type(plan['max_clients']) is int and type(contract['max_clients']) is int and
            plan['max_clients'] == contract['max_clients'] == max_clients and
            type(contract['server_max_sequences']) is int and
            max_clients <= contract['server_max_sequences'], 'completed_actor_clients')
    require(type(initial_failures) is int and 0 <= initial_failures < 3 and
            type(plan['initial_failures']) is int and
            initial_failures == plan['initial_failures'], 'completed_actor_failure_carry')
    require(type(contract['maximum_observation_age_seconds']) is int and
            0 < contract['maximum_observation_age_seconds'] <= 30 and
            type(contract['claim_ready_age_seconds']) is int and
            0 < contract['claim_ready_age_seconds'] < contract['maximum_observation_age_seconds'],
            'completed_actor_freshness')
    require(type(plan['maximum_new_requests']) is int and
            0 < len(jobs) <= plan['maximum_new_requests'] and
            plan['jobs_sha256'] == core.digest(jobs), 'completed_actor_fixed_jobs')
    ids = [job['measurement_id'] for job in jobs]
    occupied = plan['occupied_ids']
    require(type(occupied) is list and all(type(mid) is str for mid in occupied) and
            len(occupied) == len(set(occupied)) and len(ids) == len(set(ids)) and
            not set(ids).intersection(occupied), 'completed_actor_no_replay')
    require(plan['provider_identity'] == contract['provider_identity'] and
            type(contract['provider_identity']) is dict and bool(contract['provider_identity']) and
            type(contract['provider_process']) is dict and bool(contract['provider_process']) and
            type(plan['owner_identity']) is dict and bool(plan['owner_identity']) and
            type(plan['source_epoch']) is str and bool(plan['source_epoch']),
            'completed_actor_identity')
    for key in ('maximum_admission_attempts', 'maximum_captures', 'maximum_capture_uses'):
        require(type(plan[key]) is int and plan[key] > 0, 'completed_actor_finite_budget')
    require(plan['maximum_captures'] <= plan['maximum_admission_attempts'] and
            type(contract['baseline_preemptions']) is int and contract['baseline_preemptions'] >= 0,
            'completed_actor_budget_and_pressure')
    def custody(descriptor, flag):
        require(type(descriptor) is dict and set(descriptor) == {'actual', 'review'},
                'completed_actor_custody_descriptor')
        actual = read_value(descriptor['actual'], read)
        peer = read_value(descriptor['review'], read)
        require(peer.get(flag) is True and peer.get('actual') == descriptor['actual'] and
                peer.get('findings') == [], 'completed_actor_custody_peer')
        return actual, peer
    completed, completed_peer = custody(plan['completed_actor_custody'],
                                        'accepted_completed_actor_direct_probe_custody')
    require(completed['schema'] == 'causes.completed_actor_direct_probe_custody.v1' and
            completed['active_actor_roles'] == [] and completed['old_actor_owners_absent'] is True and
            completed['no_unresolved_actors'] is True and completed['completed_actor_records'] and
            completed['provider_identity'] == contract['provider_identity'] and
            completed['provider_process'] == contract['provider_process'],
            'completed_actor_completion_proof')
    for ref in completed['completed_actor_records']:
        read_value(ref, read)
    previous, peer = custody(plan['predecessor_custody'],
                             'accepted_finite_supplement_closed_custody')
    require(previous['schema'] == 'causes.finite_supplement_tranche_closed.v1' and
            type(previous['active']) is int and previous['active'] == 0 and previous['ambiguous'] == [] and
            previous['clean_native_ending'] is True and
            previous['complete_ids'] == occupied and
            type(previous['consecutive_failures']) is int and
            previous['consecutive_failures'] == initial_failures and
            peer['original_denominator_preserved'] is True and
            peer['all_original_attempts_retained'] is True and
            peer['qualification_findings'] == previous['qualification_findings'],
            'completed_actor_predecessor_changed')
    if 'derived_bank' in plan:
        from causes import campaign_supplement_derived_bank
        require('original_reservation_transfer' not in plan and
                contract.get('derived_bank') == plan['derived_bank'],
                'completed_actor_derived_bank_contract')
        resolved = campaign_supplement_derived_bank.resolve(plan, previous, read=read)
        require(jobs == resolved['jobs'], 'completed_actor_derived_jobs_changed')
    elif 'original_reservation_transfer' in plan:
        require(contract.get('original_reservation_transfer') == plan['original_reservation_transfer'],
                'completed_actor_transfer_contract')
        transfer = original_transfer_jobs(plan, previous, read=read)
        require(jobs == transfer['jobs'], 'completed_actor_transferred_jobs_changed')
    else:
        full_jobs = read_value(previous['full_jobs'], read)
        full_ids = [job['measurement_id'] for job in full_jobs]
        require(full_ids == occupied + previous['unstarted_ids'] and
                len(full_ids) == len(set(full_ids)) and
                jobs == full_jobs[len(occupied):len(occupied) + len(jobs)],
                'completed_actor_original_untouched_prefix')


class _Expired(Exception):
    """Only explicit original-clock age checks may request an unclaimed renewal."""


class Admission:
    """Synchronous native admission/prepare callbacks; no scientific retries.

    ``guard`` performs fresh pause/revocation, exclusive ownership and actor
    exclusion, provider/forward identity and pressure, local storage/window,
    source and visible-failure checks. Its evidence must contain those facts;
    its actual implementation and retained proof require independent review.
    ``capture`` retains the provider's original full observation and returns its
    typed normalized value. ``retain`` writes each supplied value exclusively
    and returns an exact pin; ``read`` returns its original bytes.
    """
    def __init__(self, plan, contract, *, capture, guard, retain,
                 source_check, prepare_request, read=core.pinned, clock=core.now):
        self.plan = copy.deepcopy(plan)
        self.contract = copy.deepcopy(contract)
        self.capture = capture
        self.guard = guard
        self.retain = retain
        self.source_check = source_check
        self.prepare_request = prepare_request
        self.read = read
        self.clock = clock
        self.attempts = self.captures = self.uses = 0
        self.cached = self.cached_ref = self.current = self.epoch = None
        self.last_time = None
        self.last_clock_read = None
        self.seen_captures = set()

    def _now(self):
        self.last_clock_read = None
        value = self.clock()
        self.last_clock_read = value
        stamp = instant(value)
        require(self.last_time is None or stamp >= self.last_time,
                'completed_actor_clock_rollback')
        self.last_time = stamp
        return value

    def _refusal(self, error, **scope):
        # A broken/rolled-back clock must not be read again or relabeled fresh.
        return self._save('refusal', dict(scope, error=repr(error), claimed=False,
            last_clock_read=self.last_clock_read,
            last_accepted_clock=None if self.last_time is None else self.last_time.isoformat(),
            refusal_clock_is_not_new_observation=True))

    def _save(self, kind, value):
        frozen = copy.deepcopy(value)
        ref = self.retain(kind, frozen)
        require(read_value(ref, self.read) == frozen, 'completed_actor_retention_changed')
        return ref

    def _age(self, value, at, *, ready=True):
        bound = self.contract['claim_ready_age_seconds'] if ready else self.contract['maximum_observation_age_seconds']
        for key in ('observed_from', 'observed_until'):
            seconds = (instant(at) - instant(value[key])).total_seconds()
            require(seconds >= 0, 'completed_actor_future_observation')
            if seconds > bound:
                raise _Expired(key)

    def _pressure(self, value):
        require(value['identity'] == self.contract['provider_identity'] and
                value['process'] == self.contract['provider_process'],
                'completed_actor_provider_changed')
        require(all(type(value[k]) is int for k in
                    ('running_requests', 'waiting_requests', 'preemptions')) and
                0 <= value['running_requests'] <= self.contract['server_max_sequences'] and
                value['waiting_requests'] == 0 and
                value['preemptions'] == self.contract['baseline_preemptions'],
                'completed_actor_pressure')

    def _guard(self, phase):
        begin = self._now()
        value = copy.deepcopy(self.guard(phase))
        try:
            end = self._now()
        except BaseException as error:
            self._save('guard', dict(phase=phase, callback_from=begin,
                callback_until=self.last_clock_read, value=value, clock_error=repr(error)))
            raise
        ref = self._save('guard', dict(phase=phase, callback_from=begin,
                                     callback_until=end, value=value))
        verify_evidence(value['evidence'], self.read)
        require(value['schema'] == GUARD_SCHEMA and
                instant(begin) <= instant(value['observed_from']) <=
                instant(value['observed_until']) <= instant(end),
                'completed_actor_guard_clock')
        require(value['owner_identity'] == self.plan['owner_identity'] and
                value['source_epoch'] == self.plan['source_epoch'],
                'completed_actor_owner_or_source_changed')
        require(value['evidence'] and all(value[k] is True for k in
                ('pause_clear', 'ownership_valid', 'actor_owners_absent',
                 'provider_exclusive', 'local_storage_valid')) and
                value['quarantined'] is False and
                type(value['consecutive_failures']) is int and
                0 <= value['consecutive_failures'] < 3,
                'completed_actor_live_guard')
        self._pressure(value)
        self._age(value, end)
        epoch = core.digest(dict(owner=value['owner_identity'], source=value['source_epoch'],
                                 identity=value['identity'], process=value['process'],
                                 failures=value['consecutive_failures']))
        return value, ref, epoch

    def _capture(self, epoch):
        if self.captures >= self.plan['maximum_captures']:
            raise core.AdmissionHold('completed_actor_capture_budget_exhausted')
        self.captures += 1
        begin = self._now()
        self._save('capture_intent', dict(number=self.captures, at=begin))
        value = copy.deepcopy(self.capture())
        try:
            end = self._now()
        except BaseException as error:
            self._save('capture', dict(number=self.captures, callback_from=begin,
                callback_until=self.last_clock_read, value=value, clock_error=repr(error)))
            raise
        ref = self._save('capture', dict(number=self.captures, callback_from=begin,
                                       callback_until=end, value=value))
        verify_evidence(value['evidence'], self.read)
        require(value['schema'] == PROVIDER_SCHEMA and value['healthy'] is True and
                value['evidence'] and value['kernel_error_records'] == [] and
                instant(begin) <= instant(value['observed_from']) <=
                instant(value['observed_until']) <= instant(end),
                'completed_actor_provider_capture')
        self._pressure(value)
        novelty = core.digest([value['identity'], value['process'],
                               value['observed_from'], value['observed_until']])
        require(novelty not in self.seen_captures, 'completed_actor_retimed_capture')
        self.seen_captures.add(novelty)
        self._age(value, end)
        self.cached, self.cached_ref = copy.deepcopy(value), ref
        self.epoch, self.uses = epoch, 0

    def _candidate(self):
        if self.attempts >= self.plan['maximum_admission_attempts']:
            raise core.AdmissionHold('completed_actor_admission_budget_exhausted')
        self.attempts += 1
        number = self.attempts
        self._save('reservation', dict(number=number, at=self._now()))
        state, state_ref, epoch = self._guard('admission')
        refresh = self.cached is None or self.epoch != epoch or self.uses >= self.plan['maximum_capture_uses']
        if not refresh:
            try:
                self._age(self.cached, self._now())
            except _Expired:
                refresh = True
        if refresh:
            self._capture(epoch)
        require(read_value(self.cached_ref, self.read)['value'] == self.cached,
                'completed_actor_cached_capture_changed')
        verify_evidence(self.cached['evidence'], self.read)
        self.uses += 1
        self._age(self.cached, self._now())
        value = dict(schema=ADMISSION_SCHEMA, identity=self.cached['identity'],
                     observed_from=self.cached['observed_from'],
                     observed_until=self.cached['observed_until'], provider=self.cached_ref,
                     guard=state_ref, admission_attempt=number, capture_number=self.captures,
                     capture_use=self.uses, epoch=epoch)
        value['candidate'] = self._save('candidate', value)
        return value

    def _next(self):
        while True:
            try:
                return self._candidate()
            except _Expired as error:
                self._save('expiry', dict(admission_attempt=self.attempts,
                    at=self.last_clock_read, reason=str(error), claimed=False,
                    expiry_clock_is_not_new_observation=True))
                self.cached = None
            except BaseException as error:
                self.cached = None
                self._refusal(error, admission_attempt=self.attempts)
                raise

    def check(self):
        self.current = self._next()
        return self.current

    def _verify_candidate(self, candidate):
        expected = {key: value for key, value in candidate.items()
                    if key not in ('candidate', 'claim_ready')}
        require(read_value(candidate['candidate'], self.read) == expected,
                'completed_actor_candidate_changed')
        original_guard = read_value(candidate['guard'], self.read)
        verify_evidence(original_guard['value']['evidence'], self.read)
        require(read_value(candidate['provider'], self.read)['value'] == self.cached,
                'completed_actor_cached_capture_changed')
        verify_evidence(self.cached['evidence'], self.read)

    def prepare(self, job):
        require(self.current is not None, 'completed_actor_admission_missing')
        original_job = core.digest(job)
        while True:
            candidate = self.current
            try:
                self.source_check(job)
                self.prepare_request(job)
                require(core.digest(job) == original_job, 'completed_actor_request_mutated')
                state, state_ref, epoch = self._guard('claim_ready')
                require(epoch == candidate['epoch'], 'completed_actor_state_changed')
                self._verify_candidate(candidate)
                self._age(candidate, self._now())
                ready = self._save('ready', dict(candidate=candidate['candidate'],
                    measurement_id=job['measurement_id'], input_sha256=original_job,
                    at=self._now(), guard=state_ref))
                self._age(candidate, self._now())
                candidate['claim_ready'] = ready
                return
            except _Expired as error:
                self._save('expiry', dict(admission_attempt=candidate['admission_attempt'],
                    candidate=candidate['candidate'], measurement_id=job['measurement_id'],
                    input_sha256=original_job, at=self.last_clock_read, reason=str(error),
                    claimed=False, expiry_clock_is_not_new_observation=True))
                self.cached = None
                replacement = self._next()
                # The native queue keeps this exact object until its real claim.
                candidate.clear()
                candidate.update(replacement)
            except BaseException as error:
                self.cached = None
                self._refusal(error, admission_attempt=candidate['admission_attempt'])
                raise


def run_queue(output, jobs, worker, admission, pause_paths, *, family, model,
              initial_failures, max_clients, capacity_limit,
              local_free_space_floor_bytes=core.DEFAULT_LOCAL_FREE_SPACE_FLOOR_BYTES):
    """Preserve native claim, harvest, failure, no-replay and capacity behavior."""
    validate_contract(admission.plan, admission.contract, jobs,
                      initial_failures=initial_failures, max_clients=max_clients,
                      read=admission.read)
    require(str(output) == admission.plan['output_root'] and family == admission.plan['family']
            and model == admission.plan['model'], 'completed_actor_native_namespace')
    require(capacity_limit == admission.plan['capacity_limit'], 'completed_actor_capacity_binding')
    require([str(path) for path in pause_paths] == admission.plan['pause_paths'] and
            local_free_space_floor_bytes == admission.plan['local_free_space_floor_bytes'],
            'completed_actor_native_controls')
    require(not (core.canonical(output) / '.control' / 'journal.jsonl').exists(),
            'completed_actor_fresh_handoff_root_required')
    def execute(job, destination):
        before = core.digest(job)
        admission.source_check(job)
        admission.prepare_request(job)
        require(core.digest(job) == before, 'completed_actor_worker_request_mutated')
        try:
            return worker(job, destination)
        finally:
            admission.source_check(job)
            require(core.digest(job) == before, 'completed_actor_worker_request_mutated')
    return core.run_queue(output, jobs, execute, admission.check, admission.prepare,
                          pause_paths, family=family, model=model,
                          initial_failures=initial_failures, phase=MODE,
                          max_clients=max_clients, capacity_limit=capacity_limit,
                          local_free_space_floor_bytes=local_free_space_floor_bytes)
