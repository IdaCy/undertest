"""Validate the exact terminal/unstarted partition of a saved supplementary queue."""

import json


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def drained_partition(jobs, history, terminal_custody, unstarted_rows, *,
                      journal_index, digest):
    """Return native claims/finals and exact unstarted IDs without reading files.

    Callers must bind accepted immutable custody and source identities, supply
    their frozen journal-index and input-digest functions, and subsequently
    verify every consumed final through the native request/parser verifier.
    This partition check does not certify completed reservation, labels,
    retirement, transfer, resumption, or authority to issue requests.
    """
    _require(type(jobs) is list and jobs, 'original_jobs_required')
    by_id = {job['measurement_id']: job for job in jobs}
    _require(len(by_id) == len(jobs), 'duplicate_original_measurement')
    claims, finals = journal_index(history, by_id)
    _require(set(claims) == set(finals), 'unresolved_drained_claim')
    for key in ('claimed_ids', 'terminal_ids'):
        ids = terminal_custody[key]
        _require(type(ids) is list and len(ids) == len(set(ids))
                 and set(ids) == set(finals), 'custody_terminal_inventory')
    rows = terminal_custody['rows']
    _require(type(rows) is list, 'terminal_rows_required')
    saved = {row['measurement_id']: row for row in rows}
    _require(len(saved) == len(rows) and set(saved) == set(finals),
             'custody_terminal_rows')
    for mid, terminal in finals.items():
        _require(claims[mid]['input_sha256'] == digest(by_id[mid]),
                 'drained_claim_input_identity')
        _require(all(_encoded(terminal[key]) == _encoded(saved[mid][key])
                     for key in ('event', 'final', 'measurement_id', 'technical_status')),
                 'custody_terminal_identity')
    _require(type(unstarted_rows) is list, 'unstarted_rows_required')
    unstarted = {row['measurement_id'] for row in unstarted_rows}
    _require(len(unstarted) == len(unstarted_rows)
             and unstarted == set(by_id) - set(finals), 'unstarted_inventory')
    fields = ('run_id', 'seed', 'original_seed', 'stage', 'question_id',
              'request_body_sha256', 'request_file_sha256', 'request_body_bytes',
              'context_canonical_sha256', 'question_canonical_sha256',
              'native_job_sha256')
    for row in unstarted_rows:
        ordinal = row['ordinal']
        _require(type(ordinal) is int and 0 <= ordinal < len(jobs),
                 'unstarted_original_ordinal')
        job = jobs[ordinal]
        _require(row['measurement_id'] == job['measurement_id']
                 and row['input_sha256'] == digest(job)
                 and all(_encoded(row[key]) == _encoded(job[key]) for key in fields),
                 'unstarted_original_input_identity')
    return {'claims': claims, 'finals': finals, 'unstarted_ids': unstarted,
            'counts': {'assigned': len(jobs), 'terminal': len(finals),
                       'unstarted': len(unstarted),
                       'technical_failures': sum(row['event'] == 'route_failure'
                                                 for row in finals.values())}}
