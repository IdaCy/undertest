import copy
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import time

from causes import campaign_capacity_guard as guard
from causes import campaign_capacity_sample as sample_api
from causes import campaign_handoff as h


_SOURCE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def require(value, reason):
    if not value:
        raise ValueError(reason)


def own_storage_observation(declaration):
    """Retain admission health separately from independently checked physical health."""
    value = sample_api.guard_observation(declaration)
    owned_ref = declaration['guard']['binding']['capacity_guard']['owned_startup_barrier']
    owned = h.checked(owned_ref)
    value['owned_startup_barrier_present'] = os.path.lexists(owned['marker']['path'])
    if value['owned_startup_barrier_present']:
        h.verify_owned_barrier(owned_ref, owned['marker']['path'])
    value['admission_healthy'] = value['healthy']
    value['telemetry_healthy'] = value['healthy']
    capacity = value.get('capacity_guard', {})
    permitted = capacity.get('status') in ('endpoint_pending', 'expired')
    if permitted:
        require(capacity['guard'] == guard.self_process(), 'another_capacity_guard_wait')
        alarms = dict(value['alarms'])
        alarms.pop('capacity_guard_not_ready', None)
        if not any(alarms.values()):
            value['telemetry_healthy'] = True
            value['own_capacity_wait_retained'] = True
    return value


def metric_values(lines, series):
    found = {}
    for line in lines:
        key, number = line.rsplit(None, 1)
        require(key not in found, 'duplicate_metrics_series')
        value = float(number)
        require(math.isfinite(value) and value >= 0, 'invalid_metrics_value')
        found[key] = value
    result = {}
    for name, keys in series.items():
        require(type(keys) is list and keys and len(set(keys)) == len(keys),
                'exact_nonempty_metrics_series_required')
        values = [found[key] for key in keys]
        result[name] = max(values) if name == 'kv' else sum(values)
    require(set(result) == {'queue', 'kv', 'preemptions', 'prompt_tokens',
                            'completion_tokens', 'successful_requests'}, 'required_capacity_metrics')
    return result


class TelemetryGuard:
    def __init__(self, series, expected_roles=None, owner=None):
        self.series, self.previous, self.anchor = series, None, None
        self.expected_roles = expected_roles
        self.owner, self.barrier_was_absent = owner, False
        self.streaks = dict.fromkeys(('ram', 'queue', 'kv_warning', 'kv_hold'), 0)

    def evaluate(self, row, logs):
        reasons, warnings = [], []
        try:
            require(sample_api.identity_valid(row), 'candidate_identity_invalid')
            require(row['process_identities'] and (self.expected_roles is None or
                    {p['role'] for p in row['process_identities']} == set(self.expected_roles)),
                    'candidate_identity_observation_missing')
            require(row['storage_guard'].get('telemetry_healthy', row['storage_guard']['healthy']) is True,
                    'candidate_storage_refusal')
            require(not any(row['controls_present'].values()), 'candidate_pause_present')
            barrier = row['storage_guard']['owned_startup_barrier_present']
            require(type(barrier) is bool, 'candidate_startup_barrier_observation_missing')
            claimed = self.owner is not None and any(
                event['event'] == 'claimed' and event.get('controller_id') == self.owner['controller_id']
                for log in logs if log['group'] == self.owner['group'] for event in log['events'])
            require(not barrier or not (self.barrier_was_absent or claimed),
                    'candidate_startup_barrier_reappeared')
            self.barrier_was_absent |= not barrier
            occupancy = row['current_owner']
            require(all(type(occupancy[key]) is int and occupancy[key] >= 0
                        for key in ('active_full', 'active_prefix'))
                    and occupancy['active_full'] <= 4
                    and occupancy['active_full'] + occupancy['active_prefix'] <= 8,
                    'candidate_occupancy_missing_or_invalid')
            require(all(log['incomplete_tail_bytes'] == 0 for log in logs), 'candidate_journal_torn')
            require('metrics_error' not in row, 'candidate_metrics_missing')
            values = metric_values(row['metrics'], self.series)
            require(0 <= values['kv'] <= 1, 'candidate_kv_fraction_invalid')
            mono, wall = row['monotonic_seconds'], sample_api.epoch(row['at'])
            if self.anchor is None:
                self.anchor = (mono, wall)
            require(0 <= row['lateness_seconds'] <= 10, 'candidate_sample_late')
            require(0 <= row['finished_monotonic_seconds'] - mono <= 30,
                    'candidate_sample_span')
            pairs = [(wall, mono), (sample_api.epoch(row['finished_at']), row['finished_monotonic_seconds'])]
            for key in ('metrics_capture_started', 'metrics_capture_finished',
                        'journal_capture_started', 'journal_capture_finished'):
                pairs.append((sample_api.epoch(row[key]['wall']), row[key]['monotonic_seconds']))
            require(all(abs((w - self.anchor[1]) - (m - self.anchor[0])) <= 1 for w, m in pairs),
                    'candidate_clock_drift')
            vm = {key: row['vmstat'][key] for key in ('oom_kill', 'pswpin', 'pswpout')}
            require(all(type(v) is int and v >= 0 for v in vm.values()), 'candidate_vm_counter_invalid')
            available = row['memory']['MemAvailable_KiB']
            require(type(available) is int and available >= 0, 'candidate_memory_invalid')
            if self.previous is not None:
                require(mono > self.previous['mono'] and wall > self.previous['wall'],
                        'candidate_sample_not_monotonic')
                require(row['index'] == self.previous['index'] + 1, 'candidate_sample_gap')
                for key in vm:
                    require(vm[key] == self.previous['vm'][key], 'candidate_vm_counter_changed:' + key)
                for key in ('preemptions', 'prompt_tokens', 'completion_tokens', 'successful_requests'):
                    require(values[key] >= self.previous['values'][key], 'candidate_counter_reset:' + key)
                require(values['preemptions'] == self.previous['values']['preemptions'],
                        'candidate_preemption_increment')
            conditions = {'ram': available < 16 * 1024**2, 'queue': values['queue'] > 0,
                          'kv_warning': values['kv'] >= .85, 'kv_hold': values['kv'] >= .95}
            for name, condition in conditions.items():
                self.streaks[name] = self.streaks[name] + 1 if condition else 0
            for name, limit in (('ram', 2), ('queue', 3), ('kv_hold', 2)):
                if self.streaks[name] >= limit:
                    reasons.append('candidate_' + name)
            if self.streaks['kv_warning'] >= 3:
                warnings.append('candidate_kv_warning')
            self.previous = {'mono': mono, 'wall': wall, 'index': row['index'], 'vm': vm, 'values': values}
        except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
            reasons.append(type(error).__name__ + ':' + str(error))
            self.streaks = dict.fromkeys(self.streaks, 0)
        return {'hold': bool(reasons), 'reasons': reasons, 'warnings': warnings,
                'evaluated_after_complete_sample': True}


def terminal_metadata(directory, episode, terminal):
    """Project actor/recorder metadata only, leaving probe answers and actor text unread."""
    path = directory / 'actor_summary.json'
    classification, pins, fields = 'unknown', [], {}
    if path.exists():
        value, ref = sample_api.metadata(path)
        require(value['run_id'] == episode['run_id'], 'candidate_actor_metadata_ID_changed')
        require('actor_summary.json' not in episode
                or episode['actor_summary.json']['pin'] == ref,
                'candidate_actor_metadata_changed_during_capture')
        pins.append(ref)
        closure = value.get('sandbox_closure')
        closure = closure if type(closure) is dict else {}
        errors = closure.get('closure_errors')
        clean_closure = (closure.get('state') == 'closed' and type(errors) is list and not errors
                         and closure.get('overlay_mounted') is False
                         and closure.get('retained_container') is False)
        fields = {'end_status': value.get('end_status'),
                  'actor_error_present': bool(value.get('actor_error')),
                  'sandbox_close_error_present': bool(value.get('sandbox_close_error')),
                  'closure_state': closure.get('state'),
                  'closure_errors_present': bool(errors) if type(errors) is list else None,
                  'overlay_mounted': closure.get('overlay_mounted'),
                  'retained_container': closure.get('retained_container'),
                  'explicit_clean_closure': clean_closure,
                  'early_workspace_status': value.get('early_workspace_status'),
                  'end_workspace_status': value.get('end_workspace_status')}
        technical = (fields['actor_error_present'] or fields['sandbox_close_error_present']
                     or fields['closure_state'] == 'closure_requires_review'
                     or fields['closure_errors_present'] is True or fields['overlay_mounted'] is True
                     or fields['retained_container'] is True
                     or any(fields[key] == 'failed' for key in ('early_workspace_status', 'end_workspace_status'))
                     or fields['end_status'] == 'technical_failure'
                     or str(fields['end_status']).startswith('generation_'))
        if technical:
            classification = 'technical'
        elif clean_closure and fields['end_status'] == 'natural_completion':
            classification = 'natural'
        elif clean_closure and fields['end_status'] == 'administratively_censored_runtime':
            classification = 'censored'
    if terminal.get('controller_error_type') or terminal['event'] == 'unresolved_current_attempt':
        classification = 'technical'
    return {'classification': classification,
            'runtime_censored': fields.get('end_status') == 'administratively_censored_runtime',
            'pins': pins, 'technical_fields': fields,
            'terminal_event_sha256': hashlib.sha256(h.encoded(terminal)).hexdigest()}


def capture_candidate_boundary(directory, logs, cohort_start, declaration):
    """Keep fixed endpoint inputs and both success/failure capture intervals."""
    directory = h.new_record_directory(directory)
    before = {'wall': h.now(), 'monotonic_seconds': time.monotonic()}
    failure = None
    try:
        sample_api.save(directory / 'journals.json', logs)
        projection = sample_api.requests(Path(declaration['campaign_root']), declaration['model_key'],
                                         logs, cohort_start)
        require(not projection['metadata_errors'], 'candidate_boundary_metadata_errors')
        current = next(log for log in logs if log['group'] == declaration['owner']['group'])
        terminals = {event['run_id']: event for event in current['events']
                     if event.get('controller_id') == declaration['owner']['controller_id']
                     and event['event'] in sample_api.TERMINAL_EVENTS}
        for episode in projection['episodes']:
            if (episode['group'] == declaration['owner']['group']
                    and episode['claim'].get('controller_id') == declaration['owner']['controller_id']
                    and episode['run_id'] in terminals):
                run = Path(declaration['campaign_root']) / 'runs' / episode['group'] / declaration['model_key'] / episode['run_id']
                episode['terminal_metadata'] = terminal_metadata(run, episode, terminals[episode['run_id']])
        sample_api.save(directory / 'requests.json', projection)
    except BaseException as error:
        failure = {'error_class': type(error).__name__, 'error': str(error)}
        raise
    finally:
        capture = {'started': before, 'finished': {'wall': h.now(), 'monotonic_seconds': time.monotonic()},
                   'request_projection_is_boundary_only': True, 'failure': failure,
                   'journal_prefixes': [{key: log[key] for key in ('group', 'path', 'bytes', 'sha256', 'incomplete_tail_bytes')}
                                        for log in logs]}
        sample_api.save(directory / 'capture.json', capture)
    require(capture['finished']['monotonic_seconds'] - before['monotonic_seconds'] <= 60,
            'candidate_boundary_capture_span')
    return {'requests': projection, 'journals': logs, 'capture': capture}


class CandidateSupervisor:
    """Control only a bound role-local pause and a non-sliding health lease."""
    def __init__(self, writer, telemetry, binding, record_directory):
        self.writer, self.telemetry, self.binding = writer, telemetry, binding
        self.directory = Path(record_directory)
        self.pending, self.last_sample, self.held = None, None, False

    def hold(self, reasons):
        if self.held:
            return
        self.held = True
        try:
            if self.last_sample is not None:
                self.writer.publish('held', self.last_sample, list(reasons))
        finally:
            # Losing the lock revokes even a previously durable healthy lease.
            self.writer.close()
            guard.hold_admission(self.binding, self.binding['pause_file'], self.directory, reasons)

    def sample_completed(self, row, logs, sample_ref):
        require(not self.held, 'candidate_already_held')
        self.last_sample = sample_ref
        decision = self.telemetry.evaluate(row, logs)
        if decision['hold']:
            self.hold(decision['reasons'])
        elif self.pending is None:
            self.writer.publish('healthy', sample_ref)
        elif time.monotonic() >= self.pending['deadline']:
            self.hold(['candidate_endpoint_deadline'])
        return decision

    def begin_endpoint(self, endpoint_id):
        require(not self.held and self.pending is None and self.last_sample is not None,
                'candidate_endpoint_state_invalid')
        sample = h.checked(self.last_sample)
        self.pending = {'id': endpoint_id,
                        'deadline': sample['finished_monotonic_seconds'] + 60,
                        'sample': self.last_sample}
        self.writer.publish('endpoint_pending', self.last_sample, {'endpoint_id': endpoint_id})
        return copy.deepcopy(self.pending)

    def finish_endpoint(self, endpoint_id, evaluation, *, final=False):
        require(not self.held and self.pending is not None and self.pending['id'] == endpoint_id,
                'candidate_endpoint_identity_changed')
        if time.monotonic() >= self.pending['deadline']:
            self.hold(['candidate_endpoint_deadline'])
        elif (evaluation.get('supported') is not True or evaluation['decision'] == 'hold'
              or final and evaluation['decision'] != 'eligible_candidate'):
            self.hold(['candidate_endpoint_rejected', *evaluation.get('reasons', [])])
        else:
            self.pending = None
            # The latest fully checked sample determines expiry; no blind renewal.
            self.writer.publish('healthy', self.last_sample)


def fixed_window(start, seconds, offset=0):
    wall = dt.datetime.fromisoformat(start['at']) + dt.timedelta(seconds=offset)
    return {'start_wall': wall.isoformat(), 'end_wall': (wall + dt.timedelta(seconds=seconds)).isoformat(),
            'start_monotonic_seconds': start['monotonic_seconds'] + offset,
            'end_monotonic_seconds': start['monotonic_seconds'] + offset + seconds,
            'global_start_wall': start['at'],
            'global_start_monotonic_seconds': start['monotonic_seconds'],
            'scheduled_duration_seconds': seconds, 'half_open_receipt_finish_interval': True}


def guard_transition_evidence(declaration):
    records = []
    directory = h.canonical_path(declaration['guard']['directory'])
    for path in sorted(directory.glob('[0-9]*.json')):
        ref = h.pin(path)
        value = h.checked(ref)
        row = {'pin': ref, 'value': value}
        capacity = (value.get('observation') or {}).get('capacity_guard', {})
        if capacity.get('lease'):
            row['lease'] = {'pin': capacity['lease'], 'value': h.checked(capacity['lease'])}
        records.append(row)
    return records


def collect_endpoint(directory, logs, cohort_start, declaration, sample_rows):
    boundary = capture_candidate_boundary(directory, logs, cohort_start, declaration)
    before = {'wall': h.now(), 'monotonic_seconds': time.monotonic()}
    transitions = guard_transition_evidence(declaration)
    references = {row['lease']['pin']['path']: row['lease']['pin'] for row in transitions if row.get('lease')}
    for row in sample_rows:
        lease = row.get('storage_guard', {}).get('capacity_guard', {}).get('lease')
        if lease is not None:
            require(lease['path'] not in references or references[lease['path']] == lease,
                    'candidate_lease_pin_conflict')
            references[lease['path']] = lease
    boundary['capacity_wait_evidence'] = {
        'transitions': transitions,
        'leases': [{'pin': ref, 'value': h.checked(ref)} for ref in references.values()],
        'capture': {'started': before, 'finished': {'wall': h.now(), 'monotonic_seconds': time.monotonic()}}}
    sample_api.save(Path(directory) / 'capacity_wait_evidence.json', boundary['capacity_wait_evidence'])
    return boundary


def validate_operational_bindings(plan, policy, binding, owned):
    refs = plan['operational_bindings']
    require(refs == policy['capacity_trial']['operational_bindings']
            and policy['capacity_trial']['mode'] == 'candidate',
            'candidate_operational_policy_binding_changed')
    value, review, adoption = [h.checked(refs[key]) for key in ('evidence', 'review', 'adoption')]
    require(value['schema'] == 'causes.capacity_operational_binding_addendum.v1'
            and review.get('accepted_capacity_operational_binding_addendum') is True
            and review['addendum'] == refs['evidence']
            and adoption.get('root_adopted_capacity_operational_binding_addendum') is True
            and adoption['addendum'] == refs['evidence']
            and adoption['independent_review'] == refs['review'],
            'candidate_operational_bindings_unaccepted')
    require(all(row['criteria'] == binding['criteria']['evidence']
                and row['criteria_adoption'] == binding['criteria_adoption']
                for row in (value, review, adoption)), 'candidate_operational_criteria_changed')
    candidate = value['candidate']
    require(candidate['output_namespace'] == binding['output_root']
            and candidate['sole_hold_path'] == binding['pause_file']
            and candidate['startup_barrier'] == owned['marker']['path']
            and type(candidate['workers']) is int and candidate['workers'] == 8
            and type(candidate['max_full']) is int and candidate['max_full'] == 4,
            'candidate_operational_control_or_capacity_changed')


def load_trial(plan_ref):
    plan = h.checked(plan_ref)
    require(plan['schema'] == 'causes.capacity_trial_runner.v1'
            and plan['execution_authorized'] is True, 'inactive_capacity_trial')
    require(plan['source']['sha256'] == _SOURCE_SHA
            == hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'capacity_trial_source_changed')
    h.checked_bytes(plan['source'])
    binding = plan['capacity_guard']
    guard.validate_binding(binding)
    require(plan_ref['path'] == binding['plan_path'], 'capacity_trial_plan_path_changed')
    require(all(plan[key] == binding[key] for key in ('criteria', 'criteria_adoption', 'reference')),
            'capacity_trial_evidence_changed')
    declaration = plan['sampling']
    sample_api.validate_plan(declaration)
    require(declaration['owner']['workers'] == 8 and declaration['owner']['max_full'] == 4
            and declaration['eligibility']['max_wait_seconds'] == 600,
            'candidate_capacity_or_prelude_changed')
    require(declaration['guard']['binding']['capacity_guard'] == binding,
            'candidate_sampling_guard_changed')
    owner = next(p for p in declaration['processes'] if p['role'] == declaration['owner']['role'])
    require({key: owner[key] for key in ('pid', 'process_starttime_ticks', 'argv')} == plan['owner_process'],
            'candidate_sampling_owner_changed')
    owned = h.checked(binding['owned_startup_barrier'])
    policy = h.checked(declaration['owner']['policy'])
    validate_operational_bindings(plan, policy, binding, owned)
    controls = declaration['guard']['binding']['admission_control']
    routing = h.checked(controls['pause_routing'])
    require(policy['launch']['storage_binding'] == declaration['guard']['binding']
            and policy['launch']['owned_barrier'] == binding['owned_startup_barrier']
            and policy['launch']['options']['pause_file'] == binding['pause_file']
            and controls['role'] == 'mi'
            and routing['successor_local_pauses']['mi'] == binding['pause_file']
            and routing['successor_global_pause'] == policy['launch']['options']['global_pause_file']
            and set(declaration['controls_absent']) == {
                routing['successor_global_pause'], routing['successor_local_pauses']['mi']}
            and owned['marker']['path'] not in declaration['controls_absent'], 'candidate_control_scope_changed')
    require(plan['release_wait_seconds'] == 600 and type(plan['release_wait_seconds']) is int,
            'bounded_candidate_release_wait_required')
    require(plan['provisional_continuation_authorized'] is True,
            'explicit_guarded_pending_review_continuation_required')
    for ref in sample_api.identity_pins(declaration):
        sample_api.checked(ref)
    evaluator = importlib.import_module('causes.campaign_capacity_summary')
    wait_adapter = importlib.import_module('causes.campaign_capacity_wait')
    require(h.pin(Path(evaluator.__file__)) == plan['evaluator_source'], 'candidate_evaluator_source_changed')
    require(h.pin(Path(wait_adapter.__file__)) == plan['wait_adapter_source'], 'candidate_wait_adapter_source_changed')
    require(h.pin(Path(sample_api.__file__)) == plan['sampler_source'], 'candidate_sampler_source_changed')
    require(all(ref in binding['required_sources'] for ref in
                (plan['source'], plan['evaluator_source'], plan['wait_adapter_source'],
                 plan['sampler_source'], declaration['guard']['source'])),
            'candidate_guard_dependency_inventory_incomplete')
    criteria = h.checked(binding['criteria']['evidence'])
    reference_report = h.checked(binding['reference']['evidence'])
    reference = reference_report['evaluation']
    require(reference['supported'] is True and reference['decision'] == 'eligible_reference',
            'candidate_reference_not_qualified')
    return plan, evaluator, criteria, reference


def run_trial(plan_ref):
    """One same-host supervisor; endpoint collection never stops scheduled telemetry."""
    plan, evaluator, criteria, reference = load_trial(plan_ref)
    declaration, binding = plan['sampling'], plan['capacity_guard']
    out = h.new_record_directory(plan['output'])
    h.save_new(out, 'RECEIPT.json', {'at': h.now(), 'plan': plan_ref, 'process': guard.self_process(),
        'new_model_calls': 0, 'automatic_adoption': False, 'automatic_resume': False})
    writer = guard.LeaseWriter(binding, plan_ref, plan['owner_process'])
    supervisor = CandidateSupervisor(writer, TelemetryGuard(plan['metric_series'],
        [p['role'] for p in declaration['processes']], declaration['owner']), binding, out)
    executor = ThreadPoolExecutor(max_workers=1)
    samples, boundaries, blocks = [], {}, []
    pending, cohort_start, first_admission_mono, chosen = None, None, None, None
    started, index = time.monotonic(), 0
    next_sample = started
    try:
        while not supervisor.held:
            current_time = time.monotonic()
            if pending is not None and pending['future'].done():
                boundary = pending['future'].result()
                endpoint = pending['endpoint']
                boundaries[endpoint] = boundary
                if endpoint == 0:
                    evaluation = evaluator.evaluate_prelude(boundary, declaration, reference, criteria,
                        cohort_start=cohort_start, end_wall=chosen['at'])
                    h.save_new(out, 'PRELUDE_EVALUATION.json', evaluation)
                else:
                    first = endpoint - 60
                    selected = samples[first:endpoint + 1]
                    window = fixed_window(chosen, 1800, first * 30)
                    summary = evaluator.summarize_window(selected, boundaries[first], boundary,
                        window, declaration, preceding_sample=samples[first - 1] if first else None,
                        capacity_wait_evidence=boundary['capacity_wait_evidence'])
                    evaluation = evaluator.evaluate_candidate(summary, reference, criteria,
                        prior_blocks=blocks, cohort_start=cohort_start, cumulative_actors=summary['actors'])
                    h.save_new(out, f'BLOCK_{endpoint:04d}.json', {'summary': summary, 'evaluation': evaluation})
                    blocks.append(summary)
                    if endpoint == 300 and evaluation['decision'] != 'hold':
                        confirmation = evaluator.summarize_window(samples[60:301], boundaries[60], boundary,
                            fixed_window(chosen, 7200, 1800), declaration, preceding_sample=samples[59],
                            capacity_wait_evidence=boundary['capacity_wait_evidence'])
                        evaluation = evaluator.evaluate_candidate(confirmation, reference, criteria,
                            cohort_start=cohort_start, cumulative_actors=summary['actors'], confirmation=True)
                        h.save_new(out, 'QUALIFICATION.json', {'summary': confirmation, 'evaluation': evaluation,
                            'automatic_adoption': False, 'monitoring_continues': True})
                supervisor.finish_endpoint(endpoint, evaluation, final=endpoint == 300)
                pending = None
                if supervisor.held:
                    break
            if pending is not None and current_time >= supervisor.pending['deadline']:
                supervisor.hold(['candidate_endpoint_deadline'])
                break
            if current_time < next_sample:
                time.sleep(min(next_sample - current_time, .25))
                continue
            row, logs = sample_api.sample(declaration, index, next_sample, observe_guard=own_storage_observation)
            row['candidate_capacity_monitor'] = True
            sample_ref = h.save_new(out, f'sample_{index:05d}.json', row)
            decision = supervisor.sample_completed(row, logs, sample_ref)
            h.save_new(out, f'telemetry_{index:05d}.json', decision)
            if supervisor.held:
                break
            current = next(log for log in logs if log['group'] == declaration['owner']['group'])
            claims = [event for event in current['events'] if event['event'] == 'claimed'
                      and event.get('controller_id') == declaration['owner']['controller_id']]
            if cohort_start is None and claims:
                cohort_start = min(sample_api.epoch(event['at']) for event in claims)
                first_admission_mono = row['monotonic_seconds'] - (sample_api.epoch(row['at']) - cohort_start)
                require(started <= first_admission_mono <= started + plan['release_wait_seconds'],
                        'candidate_first_admission_outside_declared_wait')
                h.save_new(out, 'FIRST_ADMISSION.json', {'at': h.now(), 'first_claim_wall_epoch': cohort_start,
                    'observed_in': sample_ref, 'estimated_monotonic_seconds': first_admission_mono,
                    'conversion': 'paired current sample wall/monotonic; drift separately checked'})
            if cohort_start is None:
                if row['monotonic_seconds'] - started >= plan['release_wait_seconds']:
                    supervisor.hold(['candidate_first_admission_not_observed'])
            elif chosen is None:
                elapsed = row['monotonic_seconds'] - first_admission_mono
                if elapsed > 600:
                    supervisor.hold(['candidate_full_exposure_not_reached'])
                elif sample_api.eligible(row, declaration):
                    chosen = copy.deepcopy(row)
                    chosen['index'] = 0
                    h.save_new(out, 'WINDOW.json', fixed_window(row, 9000))
                elif elapsed >= 600:
                    supervisor.hold(['candidate_full_exposure_not_reached'])
            if chosen is not None and not supervisor.held:
                measurement = copy.deepcopy(row)
                measurement['observation_sequence'] = row['index']
                measurement['observation_pin'] = sample_ref
                measurement['index'] = len(samples)
                samples.append(measurement)
                h.save_new(out, f'measurement_sample_{measurement["index"]:05d}.json', measurement)
                endpoint = len(samples) - 1
                if endpoint % 60 == 0:
                    require(pending is None, 'overlapping_candidate_endpoints')
                    supervisor.begin_endpoint(endpoint)
                    pending = {'endpoint': endpoint, 'future': executor.submit(collect_endpoint,
                        out / f'boundary_{endpoint:04d}', copy.deepcopy(logs), cohort_start, declaration,
                        copy.deepcopy(samples))}
                # Once t0 is chosen, all boundaries use its fixed monotonic cadence.
                next_sample = chosen['monotonic_seconds'] + len(samples) * 30
            else:
                next_sample = started + (index + 1) * 30
            index += 1
        h.save_new(out, 'STOPPED.json', {'at': h.now(), 'admission_held': supervisor.held,
            'pending_endpoint': None if pending is None else pending['endpoint'], 'automatic_retry': False})
    except BaseException as error:
        try:
            supervisor.hold(['candidate_supervisor_exception', type(error).__name__])
        finally:
            h.save_new(out, 'FAILED.json', {'at': h.now(), 'error_class': type(error).__name__,
                'error': str(error), 'partial_outputs_preserved': True, 'automatic_retry': False})
        raise
    finally:
        writer.close()
        executor.shutdown(wait=False, cancel_futures=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', required=True)
    args = parser.parse_args()
    run_trial(h.pin(Path(args.plan)))


if __name__ == '__main__':
    main()
