"""Distinguish completed citation-invalid annotations from route failures.

This optional scheduling classifier never changes judgments or missingness. Its
caller must retain the native results and evidence, and must leave the route
failure counter unchanged when a nonempty tuple is returned. Other failures
continue through the caller's ordinary failure/quarantine policy.
"""
from . import campaign_reader as reader


def citation_only_missing(result, evidence):
    """Return affected instruments only for the narrow completed-citation case.

    ``evidence`` maps each instrument to its native ``attempt`` and ``receipt``
    plus ``expected`` request/context/source and prompt/config/schema digests,
    ``actual_request_sha256`` (native canonical digest) and
    ``actual_response_sha256`` (whole raw-response bytes).
    The expected values must come from the accepted input, not the receipt.
    Evidence contradictions raise; an unrecognized failure returns ``()``.
    No judgment, quote, raw response or parsed answer is inspected here.
    """
    if result.get('integrity_failure'):
        return ()
    instruments = result.get('instruments', {})
    if set(instruments) != set(reader.INSTRUMENTS):
        raise ValueError('reader_instrument_membership_changed')
    affected = []
    for name in reader.INSTRUMENTS:
        row = instruments[name]
        if row.get('status') in ('validated', 'validated_partial_observation'):
            continue
        code = row.get('validation_error')
        if (row.get('status') != 'technical_missing'
                or row.get('technical_reason') != 'ValueError'
                or code not in {'evidence_not_exact_channel_substring:' + t for t in reader.TYPES}):
            return ()
        affected.append(name)
    if not affected:
        return ()
    for name in affected:
        row = instruments[name]
        item = evidence[name]
        attempt, receipt, expected = item['attempt'], item['receipt'], item['expected']
        keys = ('context_sha256', 'request_sha256', 'source_sha256', 'rubric_sha256',
                'threshold_sha256', 'schema_sha256', 'config_sha256')
        if (set(expected) != set(keys)
                or any(not isinstance(expected[k], str) or len(expected[k]) != 64
                       or any(c not in '0123456789abcdef' for c in expected[k]) for k in keys)):
            raise ValueError('reader_expected_evidence_invalid')
        if (row.get('instrument') != name or row.get('context_sha256') != expected['context_sha256']
                or result.get('context_sha256') != expected['context_sha256']
                or item.get('actual_request_sha256') != expected['request_sha256']
                or any(attempt.get(k) != expected[k] or receipt.get(k) != expected[k] for k in keys)
                or any(receipt.get(k) != v for k, v in attempt.items())
                or attempt.get('instrument') != name or receipt.get('instrument') != name
                or type(attempt.get('max_retries')) is not int or attempt['max_retries'] != 0
                or attempt.get('store') is not False
                or receipt.get('status') != 'technical_missing'):
            raise ValueError('reader_citation_evidence_mismatch')
        if (receipt.get('api_call_performed') is not True
                or type(receipt.get('http_status')) is not int or receipt['http_status'] != 200
                or receipt.get('response_status') != 'completed'):
            return ()
        response_hash = receipt.get('response_sha256')
        if (not isinstance(response_hash, str) or len(response_hash) != 64
                or any(c not in '0123456789abcdef' for c in response_hash)):
            raise ValueError('reader_completed_response_pin_missing')
        if item.get('actual_response_sha256') != response_hash:
            raise ValueError('reader_completed_response_hash_mismatch')
    return tuple(affected)
