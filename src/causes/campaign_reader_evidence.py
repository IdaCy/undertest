from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import stat

from causes import campaign_reader as reader


def _bytes(path):
    path = Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('reader_evidence_not_regular_file:' + path.name)
    return path.read_bytes()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_reader_evidence_json_key')
        result[key] = value
    return result


def _json(path):
    return json.loads(_bytes(path), object_pairs_hook=_pairs)


def _same(actual, expected, code):
    if reader.encoded(actual) != reader.encoded(expected):
        raise ValueError(code)


def _missing_base(context, instrument):
    return {'instrument': instrument, 'status': 'technical_missing', 'judgments': None,
            'context_sha256': context['context_sha256'], 'observation': context['observation']}


def _raw_result(folder, context, targets, instrument, receipt):
    result = _missing_base(context, instrument)
    raw_path = folder / 'response.raw'
    parsed_path = folder / 'parsed.json'
    if not raw_path.exists():
        if receipt.get('response_sha256') is not None or parsed_path.exists():
            raise ValueError('reader_raw_response_missing_with_derived_evidence')
        if not context['observation']['eligible_text_available']:
            _same(receipt.get('api_call_performed'), False, 'reader_empty_context_api_flag_mismatch')
            result['technical_reason'] = 'no_generated_actor_text_captured'
            return result, True
        # A transport exception before receiving a body cannot be reconstructed
        # from a response. It can establish missingness only, never a label.
        return result, False
    raw = _bytes(raw_path)
    _same(hashlib.sha256(raw).hexdigest(), receipt.get('response_sha256'), 'reader_raw_response_hash_mismatch')
    if receipt.get('api_call_performed') is not True:
        raise ValueError('reader_response_without_api_attempt')
    if not context['observation']['eligible_text_available']:
        raise ValueError('reader_response_for_ineligible_empty_context')
    http_status = receipt.get('http_status')
    if type(http_status) is not int:
        raise ValueError('reader_response_http_status_missing')
    if not 200 <= http_status < 300:
        if parsed_path.exists():
            raise ValueError('reader_parsed_success_from_http_error')
        return result, False
    try:
        response = json.loads(raw)
        for receipt_key, response_key in [('response_id', 'id'), ('model', 'model'),
                                          ('usage', 'usage'), ('response_status', 'status')]:
            _same(receipt.get(receipt_key), response.get(response_key), 'reader_response_metadata_mismatch:' + receipt_key)
        if response.get('status') != 'completed':
            if parsed_path.exists():
                raise ValueError('reader_parsed_output_from_incomplete_response')
            result['technical_reason'] = 'response_not_completed'
            return result, True
        texts = [part['text'] for item in response.get('output', []) if item.get('type') == 'message'
                 for part in item.get('content', []) if part.get('type') == 'output_text']
        if len(texts) != 1:
            if parsed_path.exists():
                raise ValueError('reader_parsed_output_from_invalid_text_cardinality')
            result['technical_reason'] = 'response_output_text_cardinality'
            return result, True
        parsed = json.loads(texts[0])
    except json.JSONDecodeError as error:
        if parsed_path.exists():
            raise ValueError('reader_parsed_output_without_valid_response_json') from error
        result['technical_reason'] = type(error).__name__
        return result, True
    _same(_json(parsed_path), parsed, 'reader_saved_parsed_response_mismatch')
    try:
        judgments = reader.validate_judgments(parsed, context, targets, instrument)
    except Exception as error:
        result['technical_reason'] = type(error).__name__
        if type(error) is ValueError:
            result['validation_error'] = str(error)[:300]
        return result, True
    result['judgments'] = judgments
    result['status'] = 'validated_partial_observation' if context['observation']['technical_missing_reasoning'] else 'validated'
    return result, True


def verify_reader_capture(destination, messages, initial_context, phase, config, prompts,
                          expected_source_sha256, prefix_context=None):
    """Verify a retained reader capture without writing files or calling a model.

    Transport failures without response bodies remain technical missingness;
    they cannot substantiate any semantic judgment. A binding inconsistency
    raises ValueError rather than downgrading an otherwise claimed result.
    """
    destination = Path(destination)
    if destination.is_symlink() or not destination.is_dir():
        raise ValueError('reader_capture_directory_invalid')
    if not isinstance(initial_context, list) or phase not in ('prefix', 'full'):
        raise ValueError('reader_initial_context_or_phase_invalid')
    if phase == 'full' and prefix_context is None:
        raise ValueError('reader_full_capture_requires_paired_prefix')
    source_sha = hashlib.sha256(Path(reader.__file__).read_bytes()).hexdigest()
    if source_sha != expected_source_sha256 or reader._SOURCE_SHA256 != expected_source_sha256:
        raise ValueError('reader_verification_source_not_expected_revision')
    context, targets = reader.prepare_context(messages, len(initial_context), phase, initial_context, prefix_context)
    _same(_json(destination / 'context.json'), context, 'reader_saved_context_mismatch')
    _same(_json(destination / 'config.json'), config, 'reader_saved_config_mismatch')
    instruments = {}
    for instrument in reader.INSTRUMENTS:
        folder = destination / instrument
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError('reader_instrument_directory_invalid')
        payload, pins = reader.make_payload(config, prompts, context, instrument)
        _same(_json(folder / 'request.json'), payload, 'reader_saved_request_mismatch:' + instrument)
        _same(_json(folder / 'schema.json'), reader.schema(instrument), 'reader_saved_schema_mismatch:' + instrument)
        attempt, receipt = _json(folder / 'attempt.json'), _json(folder / 'receipt.json')
        expected = {'instrument': instrument, 'context_sha256': context['context_sha256'],
                    'request_sha256': reader.digest(payload), 'source_sha256': expected_source_sha256,
                    'max_retries': 0, 'store': False, **pins}
        for key, value in expected.items():
            _same(attempt.get(key), value, 'reader_attempt_binding_mismatch:' + key)
            _same(receipt.get(key), value, 'reader_receipt_binding_mismatch:' + key)
        for key, value in attempt.items():
            _same(receipt.get(key), value, 'reader_attempt_receipt_disagreement:' + key)
        saved = _json(folder / 'result.json')
        _same(receipt.get('status'), saved.get('status'), 'reader_receipt_result_status_mismatch')
        reconstructed, fully_reconstructed = _raw_result(folder, context, targets, instrument, receipt)
        if fully_reconstructed:
            _same(saved, reconstructed, 'reader_saved_judgments_or_admission_mismatch:' + instrument)
        else:
            for key, value in reconstructed.items():
                _same(saved.get(key), value, 'reader_transport_missingness_binding_mismatch:' + key)
            if set(saved) - {*reconstructed, 'technical_reason', 'validation_error'}:
                raise ValueError('reader_transport_missingness_unexpected_fields')
            if not isinstance(saved.get('technical_reason'), str) or not saved['technical_reason']:
                raise ValueError('reader_transport_missingness_reason_missing')
        instruments[instrument] = saved
    expected_result = {'schema': 'caus.reader_context.v1', 'context_sha256': context['context_sha256'],
                       'observation': context['observation'], 'independent_instrument_calls': True,
                       'threshold_results_inferred_from_other_calls': False, 'instruments': instruments}
    result = _json(destination / 'RESULT.json')
    _same(result, expected_result, 'reader_aggregate_result_mismatch')
    return copy.deepcopy(result)
