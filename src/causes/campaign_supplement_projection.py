"""Project accepted saved supplementary finals without executing requests."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import PurePosixPath

from causes.campaign_analysis import SELF_LABELS


QUESTIONS = ('evaluation_strongly_favors', 'evaluation_leans_towards_or_favors')
PRESENCE = 'self_evaluation_presence'


def require(value, reason):
    if not value:
        raise ValueError(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode()).hexdigest()


def _same_json(left, right):
    return json.dumps(left, sort_keys=True, ensure_ascii=False, allow_nan=False) == json.dumps(
        right, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _reference(value):
    require(type(value) is dict and set(value) == {'path', 'bytes', 'sha256'},
            'exact_reference_required')
    require(type(value['path']) is str and value['path'].startswith('/')
            and '..' not in PurePosixPath(value['path']).parts,
            'absolute_reference_required')
    require(type(value['bytes']) is int and value['bytes'] >= 0
            and type(value['sha256']) is str and len(value['sha256']) == 64
            and all(c in '0123456789abcdef' for c in value['sha256']), 'invalid_reference')


def _unique(rows, key, reason):
    require(type(rows) is list, reason)
    result = {}
    for row in rows:
        require(type(row) is dict, reason)
        value = key(row)
        require(value not in result, reason)
        result[value] = row
    return result


def _validated_final(job, member, value, epochs):
    mid, run, question = job['measurement_id'], job['run_id'], job['question_id']
    require(type(mid) is str and mid and type(run) is str and run in epochs,
            'unknown_measurement_or_epoch')
    require(job['stage'] == 'early' and question in QUESTIONS, 'supplement_scope')
    require(type(job['seed']) is int and type(value['seed']) is int, 'integer_seed_required')
    for field in ('measurement_id', 'run_id', 'stage', 'question_id', 'context'):
        require(_same_json(member[field], job[field]), 'membership_job_identity')
    require(member['epoch'] == epochs[run] and member['readout'] == 'self_' + question,
            'membership_epoch_or_readout')
    require(member['global_job_sha256'] == digest(job) == value['input_canonical_sha256'],
            'canonical_job_identity')
    require(value['measurement_id'] == mid and value['disposition'] == 'final_verified'
            and value['integrity_failure'] is False, 'native_verification_required')
    for field in ('context', 'context_canonical_sha256', 'question_canonical_sha256',
                  'seed', 'request_body_sha256'):
        require(_same_json(value[field], job[field]), 'verified_request_identity')
    terminal = member['terminal']
    require(terminal['measurement_id'] == mid
            and _same_json(terminal['final'], member['final'])
            and _same_json(member['final'], value['final']), 'terminal_final_identity')
    _reference(value['final'])
    _reference(job['context'])
    status = value['technical_status']
    require(type(status) is str and bool(status) and terminal['technical_status'] == status,
            'terminal_status_identity')
    require(terminal['event'] == ('route_success' if status == 'complete' else 'route_failure'),
            'terminal_event_identity')
    require(_same_json(value['capture_files'], member['capture_files']), 'capture_identity')
    captures = {}
    require(type(value['capture_files']) is list, 'capture_list_required')
    for ref in value['capture_files']:
        _reference(ref)
        name = PurePosixPath(ref['path']).name
        require(name not in captures, 'duplicate_capture')
        captures[name] = ref
    if 'request.json' in captures:
        require(_same_json(member['request'], captures['request.json'])
                and captures['request.json']['sha256'] == job['request_file_sha256'],
                'actual_request_identity')
    else:
        require(member.get('request') is None, 'unexpected_request_reference')
    replay = value['offline_verification']
    label = value['admissible_label']
    require(value['label'] == label, 'verified_label_identity')
    if status == 'complete':
        require(replay == 'exact_frozen_request_render_and_parser_reconstructed'
                and {'request.json', 'attempt.json', 'receipt.json', 'response.raw', 'result.json'}
                <= captures.keys(), 'complete_native_reconstruction_required')
        require(label in SELF_LABELS and value['raw_parsed_label'] == label
                and type(value['raw_result']) is dict
                and value['raw_result'].get('answer') == label
                and value['raw_result'].get('technical_status') == 'complete',
                'complete_label_identity')
        return 'observed', label, None
    require(label is None and replay in ('technical_missing_no_parsed_result',
            'exact_frozen_request_render_and_parser_reconstructed'), 'missing_native_verification')
    if value['raw_result'] is None:
        require(replay == 'technical_missing_no_parsed_result'
                and value['raw_parsed_label'] is None, 'missing_parsed_identity')
    else:
        require(replay == 'exact_frozen_request_render_and_parser_reconstructed'
                and type(value['raw_result']) is dict
                and value['raw_result'].get('technical_status') == status
                and _same_json(value['raw_result'].get('answer'), value['raw_parsed_label']),
                'missing_parsed_identity')
    reason = value.get('reason') or value.get('failure')
    if not isinstance(reason, str) or not reason.strip():
        reason = 'native_technical_' + status
    return 'missing', None, reason


def project_verified_supplement_pairs(*, jobs, epochs, terminal_membership,
                                     verified_finals, occupied_rows,
                                     evidence_by_measurement, invalid_admissions=None):
    """Caller verifies file pins, accepted native reviews and fixed cut membership."""
    require(type(epochs) is dict and all(type(k) is str and k and type(v) is str and v
                                       for k, v in epochs.items()), 'epoch_mapping_required')
    job_index = _unique(jobs, lambda row: row['measurement_id'], 'duplicate_or_invalid_jobs')
    members = _unique(terminal_membership, lambda row: row['measurement_id'],
                      'duplicate_or_invalid_terminal_membership')
    verified = _unique(verified_finals, lambda row: row['measurement_id'],
                       'duplicate_or_invalid_verified_finals')
    verified_indices = {row['measurement_id']: i for i, row in enumerate(verified_finals)}
    require(set(verified) == set(members), 'verified_terminal_membership_changed')
    require(invalid_admissions is None or type(invalid_admissions) is dict
            and set(invalid_admissions) <= set(members), 'invalid_admission_membership')
    require(type(evidence_by_measurement) is dict
            and set(evidence_by_measurement) == set(members), 'evidence_membership_changed')
    occupied = _unique(occupied_rows, lambda row: (row['run_id'], row['readout']),
                       'duplicate_or_invalid_occupied_keys')
    by_run, statuses = {}, {}
    for mid, member in members.items():
        require(mid in job_index, 'terminal_job_unknown')
        job = job_index[mid]
        statuses[mid] = _validated_final(job, member, verified[mid], epochs)
        run, question = job['run_id'], job['question_id']
        require(question not in by_run.setdefault(run, {}), 'duplicate_terminal_instrument')
        by_run[run][question] = mid
        require((run, 'self_' + question) not in occupied, 'occupied_supplement_cannot_replay')
        evidence = evidence_by_measurement[mid]
        require(type(evidence) is dict and evidence, 'measurement_evidence_required')
        require(evidence['measurement_id'] == mid
                and _same_json(evidence['final'], verified[mid]['final'])
                and evidence['request_sha256'] == job['request_body_sha256']
                and type(evidence['verified_index']) is int
                and evidence['verified_index'] == verified_indices[mid], 'evidence_request_identity')
        _reference(evidence['verified_finals'])
        _reference(evidence['native_collector'])
        custody = evidence['custody']
        require(custody['closed_run_id'] == run and custody['terminal'] is True
                and type(custody['accepted_flag']) is str
                and custody['accepted_flag'].startswith('accepted_'), 'terminal_custody_required')
        for field in ('actual', 'review', 'membership'):
            _reference(custody[field])
        require(('invalid_admission' in evidence) ==
                (invalid_admissions is not None and mid in invalid_admissions),
                'explicit_invalid_admission_mapping_required')
        if invalid_admissions is not None and mid in invalid_admissions:
            proof = invalid_admissions[mid]
            require(type(proof) is dict and proof.get('measurement_id') == mid
                    and proof.get('qualified') is False
                    and proof.get('accepted_flag') == 'accepted_finite_supplement_closed_custody'
                    and proof.get('condition') == 'accepted_closed_native_admission_failure'
                    and proof.get('row_sha256') == digest(verified[mid])
                    and type(proof.get('verified_index')) is int and proof['verified_index'] >= 0
                    and _same_json(evidence.get('invalid_admission'), proof),
                    'invalid_admission_proof_identity')
            for field in ('actual', 'review', 'verified_finals', 'audit_source'):
                _reference(proof[field])
            finding = proof['finding']
            require(set(finding) == {'measurement_id', 'stage', 'error'}
                    and finding['measurement_id'] == mid and finding['stage'] == 'native_admission'
                    and type(finding['error']) is str and bool(finding['error'])
                    and proof['custody'].get('scientific_qualified') is False
                    and proof['custody'].get('request_replay_permitted') is False,
                    'invalid_admission_finding_required')
            statuses[mid] = ('missing', None, 'invalid_native_admission:' + finding['error'])
    selected, pending, slots, preserved = [], [], [], []
    # Frozen global job order controls ordering; response labels do not select groups.
    run_order = list(dict.fromkeys(job['run_id'] for job in jobs if job['measurement_id'] in members))
    for run in run_order:
        instruments = by_run[run]
        if set(instruments) != set(QUESTIONS):
            pending.extend(copy.deepcopy(members[instruments[q]]) for q in QUESTIONS if q in instruments)
            continue
        require((run, PRESENCE) in occupied, 'existing_presence_required')
        presence = occupied[(run, PRESENCE)]
        require(presence['status'] in ('observed', 'missing'), 'terminal_presence_required')
        require(presence.get('epoch', epochs[run]) == epochs[run], 'presence_epoch_changed')
        require(all((run, 'self_' + q) not in occupied for q in QUESTIONS),
                'occupied_supplement_cannot_replay')
        selected.append(run)
        preserved.append(copy.deepcopy(presence))
        for question in QUESTIONS:
            mid = instruments[question]
            status, label, reason = statuses[mid]
            slots.append(dict(run_id=run, readout='self_' + question, status=status,
                              label=label, reason=reason,
                              evidence=copy.deepcopy(evidence_by_measurement[mid]), epoch=epochs[run]))
    return dict(slots=slots, terminal_run_ids=selected,
                counts=dict(contexts=len(selected), occupied=len(slots),
                            observed=sum(row['status'] == 'observed' for row in slots),
                            missing=sum(row['status'] == 'missing' for row in slots)),
                existing_presence=preserved, pending_incomplete_membership=pending)
