"""Pure replay of sealed native supplement histories across physical batches.

Original identity, policy and journal bytes are embedded and pinned; no file is
opened. Declared custody, export, retirement, pause and authorization references
are retained requirements, not certifications. An external reviewed materializer
must qualify their contents, the prepared-input projection, complete physical
ancestry, client absence and exclusive ownership before any admission.

Remote dispatcher journals retain campaign_monitor.replay_history semantics.
Native bundles are sealed; this helper neither changes the launcher nor opts a
sealed scope into live lifetime progression. Importing it performs no I/O.
"""
import base64
import binascii
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import TypedDict

from causes.campaign_monitor import replay_history as replay_dispatcher_history

SCHEMA = 'causes.supplement_history_bundle.v1'


class Pin(TypedDict):
    path: str
    bytes: int
    sha256: str


class EmbeddedFile(TypedDict):
    pin: Pin
    base64: str


class Measurement(TypedDict):
    measurement_id: str
    input_sha256: str


class Invocation(TypedDict):
    started_line: int
    policy: EmbeddedFile
    adoption: Pin
    review: Pin


class Batch(TypedDict):
    output_root: str
    identity: EmbeddedFile
    prepared: Pin
    journal: EmbeddedFile
    measurements: list[Measurement]
    invocations: list[Invocation]
    evidence: dict[str, Pin]
    ambiguous_attempts: list[str]
    previous_batch_sha256: str | None


class Bundle(TypedDict):
    schema: str
    family: str
    model: str
    model_route: str
    config: Pin
    batches: list[Batch]


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def _load(raw):
    def invalid_constant(value):
        raise ValueError('nonfinite_json_number:' + value)
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=invalid_constant)


def _keys(value, expected, reason):
    _require(type(value) is dict and set(value) == set(expected.split()), reason)


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _hash(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _path(value):
    _require(type(value) is str and '\x00' not in value, 'invalid_history_path')
    path = PurePosixPath(value)
    _require(path.is_absolute() and not value.startswith('//') and str(path) == value and '..' not in path.parts
             and len(path.parts) > 1, 'noncanonical_history_path')
    return path


def _pin(value):
    _keys(value, 'path bytes sha256', 'history_pin_fields')
    _path(value['path'])
    _require(_integer(value['bytes']) and _hash(value['sha256']), 'invalid_history_pin')
    return value


def _embedded(value):
    _keys(value, 'pin base64', 'embedded_history_fields')
    ref = _pin(value['pin'])
    _require(type(value['base64']) is str, 'embedded_history_base64')
    try:
        raw = base64.b64decode(value['base64'], validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError('embedded_history_base64') from error
    _require(len(raw) == ref['bytes'] and _digest(raw) == ref['sha256'], 'embedded_history_pin_changed')
    return raw


def _bundle(raw):
    _require(type(raw) is bytes and raw.endswith(b'\n'), 'native_bundle_requires_complete_bytes')
    value = _load(raw)
    _keys(value, 'schema family model model_route config batches', 'native_bundle_fields')
    _require(value['schema'] == SCHEMA, 'native_bundle_schema')
    for key in ('family', 'model'):
        _require(type(value[key]) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value[key]),
                 'native_namespace')
    _require(type(value['model_route']) is str and value['model_route'].startswith('model:')
             and len(value['model_route']) > 6, 'native_model_route')
    _pin(value['config'])
    _require(type(value['batches']) is list and value['batches'], 'native_batches_required')
    return value


def replay_native_history(raw: bytes) -> dict:
    """Validate declared bytes/order and replay state; never certify physical custody.

    Unresolved_events counts retained blocker indicators, not unique failures:
    explicit unresolved/error rows, unmatched claims, reported ambiguity and an
    unclosed final controller or missing mandatory quarantine at a prefix end.
    Only actual route_failure rows increment lifetime.
    Quarantine evidence stays sticky even if a later already-active task succeeds.
    """
    bundle = _bundle(raw)
    route = bundle['model_route']
    roots, prepared_paths, all_ids = [], set(), set()
    counts, quarantines, quarantine_evidence, summaries = {}, {}, [], []
    lifetime = residual = unresolved = tails = 0
    previous_digest = None
    for batch in bundle['batches']:
        _keys(batch, 'output_root identity prepared journal measurements invocations evidence '
                     'ambiguous_attempts previous_batch_sha256', 'native_batch_fields')
        _require(batch['previous_batch_sha256'] == previous_digest, 'native_batch_ancestry_changed')
        previous_digest = _digest(_encoded(batch))
        root = _path(batch['output_root'])
        _require(all(not root.is_relative_to(p) and not p.is_relative_to(root) for p in roots),
                 'overlapping_physical_batches')
        roots.append(root)
        prepared = _pin(batch['prepared'])
        _require(prepared['path'] not in prepared_paths, 'duplicate_physical_prepared')
        prepared_paths.add(prepared['path'])
        identity = _load(_embedded(batch['identity']))
        _keys(identity, 'family model prepared core runner launcher', 'native_identity_fields')
        _require(batch['identity']['pin']['path'] == str(root / '.control/IDENTITY.json')
                 and identity['family'] == bundle['family'] and identity['model'] == bundle['model']
                 and identity['prepared'] == prepared, 'native_identity_binding_changed')
        for key in ('core', 'runner', 'launcher'):
            _pin(identity[key])
        # A later invocation may use an independently reviewed launcher succession.
        # The original IDENTITY is retained; source/custody authority stays external.
        _keys(batch['evidence'], 'custody export retirement pause', 'native_external_evidence_required')
        for ref in batch['evidence'].values():
            _pin(ref)
        _require(type(batch['measurements']) is list, 'native_measurements_required')
        inputs, order = {}, []
        for row in batch['measurements']:
            _keys(row, 'measurement_id input_sha256', 'native_measurement_fields')
            mid = row['measurement_id']
            _require(type(mid) is str, 'native_measurement_identity')
            parts = mid.split('/')
            _require(len(parts) == 5 and parts[:2] == [bundle['family'], bundle['model']]
                     and parts[3] in ('early', 'end')
                     and all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', p) for p in parts),
                     'native_measurement_identity')
            _require(mid not in all_ids and _hash(row['input_sha256']), 'overlapping_or_invalid_measurement')
            all_ids.add(mid); inputs[mid] = row['input_sha256']; order.append(mid)
        _require(type(batch['ambiguous_attempts']) is list
                 and all(type(mid) is str for mid in batch['ambiguous_attempts'])
                 and len(set(batch['ambiguous_attempts'])) == len(batch['ambiguous_attempts'])
                 and all(mid in inputs for mid in batch['ambiguous_attempts']), 'native_ambiguous_metadata')
        unresolved += len(batch['ambiguous_attempts'])
        _require(type(batch['invocations']) is list, 'native_invocations_required')
        invocations = {}
        for item in batch['invocations']:
            _keys(item, 'started_line policy adoption review', 'native_invocation_fields')
            line = item['started_line']
            _require(_integer(line, 1) and line not in invocations, 'native_invocation_line')
            policy = _load(_embedded(item['policy']))
            _pin(item['adoption']); _pin(item['review'])
            _require(policy['prepared'] == prepared and policy['output_root'] == str(root),
                     'native_invocation_binding_changed')
            _require(_integer(policy['initial_failures']) and policy['initial_failures'] < 3
                     and type(policy['max_clients']) is int and policy['max_clients'] in (1, 2)
                     and policy['phase'] in ('pilot', 'continue'), 'native_invocation_scope')
            _require(policy['pilot_measurement_ids'] == order[:2], 'native_pilot_order_changed')
            _pin(policy['launcher_source'])
            if policy['launcher_source'] != identity['launcher']:
                _pin(policy['launcher_succession'])  # Its acceptance/content is an external gate.
            invocations[line] = policy
        journal = _embedded(batch['journal'])
        _require(batch['journal']['pin']['path'] == str(root / '.control/journal.jsonl'), 'native_journal_path')
        prefix = journal[:journal.rfind(b'\n') + 1]
        tail = len(journal) - len(prefix); tails += tail
        claims, terminals, used_invocations = {}, {}, set()
        controller = None
        previous_row = None
        required_quarantine = None
        first_residual, first_lifetime = residual, lifetime
        for line, raw_row in enumerate(prefix.splitlines(), 1):
            row = _load(raw_row)
            _require(type(row) is dict and type(row.get('event')) is str, 'native_event_required')
            event = row['event']; counts[event] = counts.get(event, 0) + 1
            _require(required_quarantine is None or event == 'route_quarantined',
                     'native_required_quarantine_missing')
            if event == 'controller_started':
                _require(line in invocations and not (set(claims) - set(terminals)), 'native_start_binding')
                if controller is not None:
                    # An interrupted controller is retained as a blocker, never a failure.
                    unresolved += 1
                policy = invocations[line]; used_invocations.add(line)
                scope = order[:2] if policy['phase'] == 'pilot' else order
                existing = sum(mid in terminals for mid in scope)
                pending = sum(mid not in claims for mid in scope)
                inherited = row['inherited_failures']
                _require(_integer(inherited) and inherited < 3
                         and inherited == max(policy['initial_failures'], residual), 'native_inherited_failure_changed')
                _require(row['phase'] == policy['phase'] and type(row['existing_finals']) is int
                         and row['existing_finals'] == existing and type(row['pending']) is int
                         and row['pending'] == pending, 'native_start_counts_changed')
                residual = inherited
                controller = {'policy': policy, 'scope': scope, 'existing': existing,
                              'completed': [], 'held': False}
            elif event == 'claimed':
                _require(controller is not None and not controller['held'] and not quarantines,
                         'native_claim_outside_admission')
                mid = row['measurement_id']
                pending = [m for m in controller['scope'] if m not in claims]
                _require(pending and mid == pending[0] and mid not in terminals
                         and row['input_sha256'] == inputs[mid]
                         and row['phase'] == controller['policy']['phase'], 'native_claim_order_or_input_changed')
                _require(len(set(claims) - set(terminals)) < controller['policy']['max_clients'],
                         'native_client_cap_exceeded')
                claims[mid] = line
            elif event in ('route_success', 'route_failure'):
                mid = row['measurement_id']
                _require(controller is not None and mid in claims and mid not in terminals,
                         'native_unclaimed_or_duplicate_terminal')
                status = row['technical_status']
                _require(type(status) is str and status and (status == 'complete') == (event == 'route_success'),
                         'native_terminal_status_changed')
                final = _pin(row['final'])
                _require(final['path'] == str(root.joinpath(*mid.split('/')[1:]) / 'FINAL.json'),
                         'native_final_path_changed')
                residual = 0 if event == 'route_success' else residual + 1
                lifetime += event == 'route_failure'
                _require(type(row['consecutive_failures']) is int and row['consecutive_failures'] == residual,
                         'native_terminal_counter_changed')
                terminals[mid] = line; controller['completed'].append(mid)
                if event == 'route_failure' and residual >= 3:
                    required_quarantine = mid
            elif event == 'route_quarantined':
                _require(previous_row is not None and previous_row['event'] == 'route_failure'
                         and row['measurement_id'] == previous_row['measurement_id'],
                         'native_quarantine_link_changed')
                _require(row['reason'] == 'integrity_failure'
                         or (row['reason'] == 'three_consecutive_infrastructure_failures' and residual >= 3),
                         'native_quarantine_reason_changed')
                # Integrity is declared by this row; its FINAL/custody proof is external.
                required_quarantine = None
                evidence = {'journal': batch['journal']['pin'], 'line': line, 'reason': row['reason'],
                            'measurement_id': row['measurement_id']}
                quarantine_evidence.append(evidence)
                quarantines.setdefault(route, _digest(_encoded(evidence)))
                if controller is not None:
                    controller['held'] = True
            elif event.startswith('unresolved_') or event == 'controller_error':
                unresolved += 1
            elif event == 'admission_held':
                _require(controller is not None and type(row.get('reason')) is str, 'native_hold_outside_controller')
                controller['held'] = True
            elif event == 'controller_finished':
                _require(controller is not None, 'native_finish_without_start')
                expected = {'phase': controller['policy']['phase'], 'existing_final': controller['existing'],
                            'new_final': len(controller['completed']),
                            'remaining_unclaimed': sum(mid not in claims for mid in controller['scope']),
                            'consecutive_failures': residual, 'active': 0,
                            'completed_ids': controller['completed']}
                for key, value in expected.items():
                    _require(type(row.get(key)) is type(value) and row[key] == value, 'native_finish_changed:' + key)
                controller = None
            else:
                raise ValueError('unsupported_native_history_event:' + event)
            previous_row = row
        _require(used_invocations == set(invocations), 'unused_native_invocation_binding')
        unmatched = [mid for mid in order if mid in claims and mid not in terminals]
        unresolved += len(unmatched) + (controller is not None) + (required_quarantine is not None)
        summaries.append({'output_root': str(root), 'journal': batch['journal']['pin'],
                          'declared_measurements': len(order), 'claimed': len(claims), 'terminal': len(terminals),
                          'pending': [mid for mid in order if mid not in claims], 'unmatched_claims': unmatched,
                          'ambiguous_attempts': batch['ambiguous_attempts'], 'unconsumed_tail_bytes': tail,
                          'missing_required_quarantine': required_quarantine,
                          'inherited_residual_before_batch': first_residual, 'residual_after_batch': residual,
                          'actual_new_failures': lifetime - first_lifetime, 'external_evidence': batch['evidence']})
    return {'bytes': len(raw), 'sha256': _digest(raw), 'unconsumed_tail_bytes': tails,
            'event_counts': counts, 'lifetime_route_failures': lifetime, 'quarantines': quarantines,
            'nonzero_failures': {route: residual} if residual else {}, 'unresolved_events': unresolved,
            'batches': summaries, 'quarantine_evidence': quarantine_evidence,
            'physical_custody_certified': False, 'runtime_authorized': False}


def replay_history(raw: bytes) -> dict:
    """Dispatch an explicit native bundle; pass dispatcher journals unchanged.

    Unknown/malformed envelope objects cannot fall through as empty history.
    A dispatcher's original complete JSONL prefix and incomplete tail retain the
    existing monitor semantics; no remote route events are translated.
    """
    _require(type(raw) is bytes, 'history_bytes_required')
    if not raw:
        return replay_dispatcher_history(raw)
    try:
        value = _load(raw)
    except (ValueError, UnicodeError):
        # Valid dispatcher JSONL starts with an event, including when later rows
        # contain an incomplete tail. An envelope prefix is not such an event.
        first = _load(raw.splitlines()[0])
        _require(type(first) is dict and 'event' in first and 'schema' not in first
                 and 'batches' not in first, 'malformed_history_envelope')
        return replay_dispatcher_history(raw)
    if type(value) is dict and ('schema' in value or 'batches' in value):
        return replay_native_history(raw)
    _require(type(value) is dict and 'event' in value, 'untyped_history_object')
    return replay_dispatcher_history(raw)


def validate_extension(previous_raw: bytes, current_raw: bytes) -> dict:
    """Require an exact complete predecessor plus one disjoint declared batch.

    The caller must independently bind the predecessor bytes/pin to the accepted
    ancestry; this function cannot discover a batch omitted from both arguments.
    """
    previous, current = _bundle(previous_raw), _bundle(current_raw)
    _require({k: v for k, v in previous.items() if k != 'batches'}
             == {k: v for k, v in current.items() if k != 'batches'}, 'native_chain_binding_changed')
    _require(len(current['batches']) == len(previous['batches']) + 1
             and current['batches'][:-1] == previous['batches'], 'native_chain_prefix_changed')
    replay_native_history(previous_raw)
    return replay_native_history(current_raw)
