"""Reuse closed staging receipts without resending identical immutable files.

``reduce_inventory`` reads pinned local metadata and returns a new inventory and
its derivation. It performs no staging, writes, transport, or approval. The
caller supplies the receipt producer's field names and records that contract
alongside the returned proof. A returned proof is not an independent acceptance.

Every promoted file remains a verify-only input to ``campaign_staging``. That
stager must check its current destination bytes before any new file is written;
a past receipt does not establish that the destination is still present.
"""

import copy
from dataclasses import dataclass, fields
import json
from pathlib import Path
import shlex

from . import campaign_handoff as h


@dataclass(frozen=True)
class ReceiptContract:
    """Explicit names for one pinned wrapper's staging receipt protocol.

    These values describe the producer; they do not waive any pin, source,
    transport, closure, scope, or partition check. ``remote_python`` is the
    executable recorded in the expected SSH command, never executed here.
    """

    stage_step: str
    inventory_acceptance_field: str
    orchestration_acceptance_field: str
    configuration_host_field: str
    remote_python: str

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not str or not value:
                raise ValueError('invalid_receipt_contract:' + field.name)


def _require(value, message):
    if not value:
        raise ValueError(message)


def _equal(a, b):
    return h.encoded(a) == h.encoded(b)


def _accepted_inventory(pair, contract):
    value, review = h.checked(pair['evidence']), h.checked(pair['review'])
    _require(review.get(contract.inventory_acceptance_field) is True
             and review.get('inventory') == pair['evidence'], 'inventory_not_accepted')
    return value


def _indexed(rows):
    result = {}
    for ref in rows:
        _require(type(ref) is dict and set(ref) == {'path', 'bytes', 'sha256'}
                 and type(ref['bytes']) is int and ref['bytes'] >= 0
                 and type(ref['sha256']) is str and len(ref['sha256']) == 64,
                 'invalid_inventory_pin')
        h.canonical_path(ref['path'])
        _require(ref['path'] not in result, 'duplicate_inventory_path')
        result[ref['path']] = ref
    return result


def _successful_stage(reference, current, contract):
    """Verify retained transport metadata, without reading payload packets."""
    result = h.checked(reference)
    directory = Path(reference['path']).parent
    _require(Path(reference['path']).name == 'RESULT.json', 'stage_result_path')
    intentref = h.pin(directory / 'INTENT.json')
    intent = h.checked(intentref)
    _require(result['step'] == intent['step'] == contract.stage_step
             and result['inputs'] == intent['inputs']
             and result['source'] == intent['source']
             and result['no_following_step_executed'] is True
             and intent['no_automatic_retry'] is True, 'stage_wrapper_binding_changed')
    source_review = h.checked(intent['source_review'])
    config = h.checked(intent['configuration'])
    _require(source_review.get(contract.orchestration_acceptance_field) is True
             and source_review['source'] == intent['source']
             and source_review['configuration'] == intent['configuration'],
             'stage_orchestration_not_accepted')
    h.checked_bytes(intent['source'])
    stageinputs = h.checked(result['inputs'])
    prior = _accepted_inventory(stageinputs['inventory'], contract)
    for field in ('host', 'base', 'protected_paths', 'staging_source'):
        _require(_equal(prior[field], current[field]), 'stage_inventory_scope_changed:' + field)
    _require(config[contract.configuration_host_field] == current['host']
             and config['base'] == current['base']
             and _equal(config['protected_stage_paths'], current['protected_paths'])
             and config['references']['staging_source'] == current['staging_source'],
             'stage_configuration_scope_changed')
    staging_review = h.checked(config['references']['staging_review'])
    _require(staging_review.get('accepted_campaign_staging_source') is True
             and all(staging_review['source'][k] == current['staging_source'][k]
                     for k in ('bytes', 'sha256')), 'staging_source_not_accepted')
    h.checked_bytes(staging_review['source'])
    source = h.checked_bytes(current['staging_source']).decode()
    remote = [contract.remote_python, '-B', '-c', source, '--base', current['base']]
    for path in current['protected_paths']:
        remote += ['--protected-path', path]
    expected = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=10', current['host'], shlex.join(remote)]
    execution = h.checked(result['result']['execution'])
    _require(execution['argv'] == intent['plan']['argv'] == expected, 'stage_ssh_or_source_changed')
    _require(execution['returncode'] == 0 and execution['timed_out'] is False
             and execution['completion_unknown'] is False
             and h.checked_bytes(execution['stderr']) == b'', 'stage_not_successfully_closed')
    _require(execution['started_at'] <= execution['ended_at'] <= result['at'], 'stage_time_order')
    primitive = json.loads(h.checked_bytes(execution['stdout']))
    _require(_equal(primitive, result['result']['primitive_output'])
             and primitive['model_or_service_actions'] is False, 'stage_stdout_result_changed')
    _require(intent['plan']['kind'] == 'command'
             and intent['plan']['step'] == contract.stage_step
             and intent['plan']['files'] == [{'pin': intent['plan']['stdin']}],
             'stage_packet_binding_changed')
    copied = _indexed(prior['copy'])
    verified = _indexed(prior['verify_only'])
    _require(not set(copied) & set(verified), 'prior_copy_verify_overlap')
    returned = []
    for row in primitive['files']:
        _require(type(row) is dict and set(row) == {'path', 'bytes', 'sha256', 'existing_identical'}
                 and type(row['existing_identical']) is bool, 'stage_file_receipt_shape')
        returned.append({k: row[k] for k in ('path', 'bytes', 'sha256')})
    _require(_equal(returned, prior['copy']) and primitive['verify_only_count'] == len(verified),
             'stage_files_not_exact_inventory')
    return copied, dict(
        result=reference, intent=intentref, execution=result['result']['execution'],
        inventory=stageinputs['inventory'], source_review=intent['source_review'],
        configuration=intent['configuration'],
        packet_pin_retained_without_payload_rehash=intent['plan']['stdin'],
        stdout=execution['stdout'], stderr=execution['stderr'])


def reduce_inventory(inputs, contract):
    """Return ``(inventory, proof)`` after checking exact closed receipts.

    ``inputs`` contains ``inventory={evidence: pin, review: pin}`` and a nonempty
    ``prior_successful_stages`` list of wrapper RESULT pins. Inventories contain
    ``host``, ``base``, ``protected_paths``, ``staging_source``, ``copy`` and
    ``verify_only``. Additional inventory metadata is preserved unchanged.

    Only exact pins returned as copied by a successfully closed, same-scope
    staging command move from ``copy`` to ``verify_only``. Ordering is retained:
    remaining copies, original verification pins, then promoted copies. Inputs
    are not mutated. Payload packet pins are bound but their bodies are not read.
    No freshness or runtime authority is granted or altered.
    """
    full = _accepted_inventory(inputs['inventory'], contract)
    _require(inputs['prior_successful_stages'] and type(inputs['prior_successful_stages']) is list,
             'successful_stage_required')
    copies, verify = _indexed(full['copy']), _indexed(full['verify_only'])
    _require(not set(copies) & set(verify), 'copy_verify_overlap')
    known, proofs = {}, {}
    for reference in inputs['prior_successful_stages']:
        _require(reference['path'] not in proofs, 'duplicate_stage_receipt')
        staged, proof = _successful_stage(reference, full, contract)
        proofs[reference['path']] = proof
        for path, ref in staged.items():
            _require(path not in known or _equal(known[path]['pin'], ref),
                     'conflicting_prior_staging_receipts')
            known[path] = {'pin': ref, 'stage_result': reference}
    moved, remaining = [], []
    for ref in full['copy']:
        previous = known.get(ref['path'])
        if previous is None:
            remaining.append(ref)
            continue
        _require(_equal(previous['pin'], ref), 'previous_staging_pin_differs_from_current_inventory')
        moved.append({'pin': ref, 'prior_result': previous['stage_result']})
    reduced = copy.deepcopy(full)
    reduced['copy'] = remaining
    reduced['verify_only'] = full['verify_only'] + [row['pin'] for row in moved]
    after = {**_indexed(reduced['copy']), **_indexed(reduced['verify_only'])}
    _require(_equal(after, {**copies, **verify}) and len(after) == len(copies) + len(verify),
             'inventory_partition_not_conserved')
    _require(_equal({k: v for k, v in reduced.items() if k not in ('copy', 'verify_only')},
                    {k: v for k, v in full.items() if k not in ('copy', 'verify_only')}),
             'inventory_metadata_changed')
    proof = dict(
        original_inventory=inputs['inventory'], prior_successful_stages=list(proofs.values()), moved=moved,
        original_copy_count=len(copies), remaining_copy_count=len(remaining), promoted_verify_count=len(moved),
        original_copy_bytes=sum(x['bytes'] for x in copies.values()),
        remaining_copy_bytes=sum(x['bytes'] for x in remaining),
        exact_pin_partition_conserved=True, all_other_inventory_fields_preserved=True,
        limits=['Prior stage success is used only to avoid resending identical immutable bytes.',
                'The unchanged stager must rehash every verify-only destination and refuse absent/changed/nonregular files.',
                'No capture, stage, authority, process, model, freshness relaxation or runtime action by this helper.',
                'Prior encoded payload packet pins are retained but payload bodies are not reread; exact returned file pins and closed stdout are independently bound.'])
    return reduced, proof
