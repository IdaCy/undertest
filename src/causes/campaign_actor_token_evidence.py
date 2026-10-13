"""Compare saved actor requests with independently bound native CPU renders."""

import hashlib
import json

from causes import campaign_runner as runner


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _pin(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def _tokens(value, reason):
    _require(isinstance(value, list) and bool(value)
             and all(type(token) is int and token >= 0 for token in value), reason)


def _normalized_sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def compare_saved_actor_prompt(request_file, attempt_file, receipt_file, response_file, *,
                               config, native_render, maximum_model_len,
                               expected_request_sha256, expected_response_sha256):
    """Check one saved call; native renderer and snapshot provenance are caller gates.

    Expected hashes come from the independently audited prefix request roster.
    The caller binds the render producer, implementation, assets, provider and
    complete call roster. This function never invokes a renderer or an endpoint.
    Equality does not establish tool coverage or authorize another actor call.
    """
    _require(all(type(raw) is bytes for raw in
                 (request_file, attempt_file, receipt_file, response_file)),
             'saved_actor_bytes_required')
    request, attempt, receipt, response = map(json.loads,
        (request_file, attempt_file, receipt_file, response_file))
    _require(all(isinstance(value, dict) for value in (request, attempt, receipt, response)),
             'saved_actor_objects_required')
    body = runner.encoded(request)
    request_sha = hashlib.sha256(body).hexdigest()
    response_sha = hashlib.sha256(response_file).hexdigest()
    _require(request_file == body + b'\n', 'native_actor_request_file_changed')
    _require(request_sha == expected_request_sha256 == attempt.get('request_sha256')
             == receipt.get('request_sha256')
             and response_sha == expected_response_sha256 == receipt.get('response_sha256'),
             'saved_actor_request_response_custody_changed')
    _require(attempt.get('retry') is False and receipt.get('retry') is False
             and receipt.get('status') == 'complete' and receipt.get('http_status') == 200
             and isinstance(attempt.get('started_at'), str)
             and receipt.get('started_at') == attempt['started_at'],
             'successful_nonretry_actor_call_required')
    _require(type(request.get('seed')) is int and request['seed'] >= 0
             and isinstance(request.get('messages'), list) and request['messages']
             and config.get('tools') and body == runner.encoded(runner.request(
                 config, request['messages'], request['seed'], config['tools'])),
             'frozen_actor_request_changed')
    _require(request.get('return_prompt_text') is True and request.get('return_token_ids') is True
             and response.get('model') == config['model']
             and all(receipt.get(key) == response.get(key)
                     for key in ('usage', 'model', 'system_fingerprint')),
             'actor_response_metadata_changed')
    _require(isinstance(native_render, dict)
             and native_render.get('request_body_sha256') == request_sha,
             'native_render_request_binding_changed')
    before, after = native_render.get('request_before'), native_render.get('request_after')
    _require(isinstance(before, dict) and isinstance(after, dict),
             'native_request_adjustment_evidence_required')
    actual_text, native_text = response.get('prompt_text'), native_render.get('prompt_text')
    _require(isinstance(actual_text, str) and isinstance(native_text, str),
             'complete_prompt_text_required')
    actual_tokens = response.get('prompt_token_ids')
    native_tokens = native_render.get('prompt_token_ids')
    _tokens(actual_tokens, 'complete_response_token_ids_required')
    _tokens(native_tokens, 'complete_native_token_ids_required')
    _require(native_text == actual_text, 'native_actor_prompt_text_mismatch')
    _require(native_tokens == actual_tokens, 'native_actor_prompt_token_mismatch')
    usage = response.get('usage')
    _require(usage is None or isinstance(usage, dict), 'actor_usage_object_required')
    prompt_usage_status = 'missing'
    if usage is not None and 'prompt_tokens' in usage:
        _require(type(usage['prompt_tokens']) is int
                 and usage['prompt_tokens'] == len(actual_tokens), 'actor_prompt_usage_mismatch')
        prompt_usage_status = 'present_equal'
    allowance = request.get('max_completion_tokens', request.get('max_tokens'))
    _require(type(allowance) is int and allowance > 0
             and type(maximum_model_len) is int and maximum_model_len > 0
             and len(actual_tokens) + allowance <= maximum_model_len,
             'actor_prompt_context_limit')
    return {
        'schema': 'causes.saved_actor_prompt_token_comparison.v1',
        'request_file': _pin(request_file), 'attempt_file': _pin(attempt_file),
        'receipt_file': _pin(receipt_file), 'response_file': _pin(response_file),
        'request_body_sha256': request_sha,
        'request_before_sha256': _normalized_sha(before), 'request_after_sha256': _normalized_sha(after),
        'adjusted_request_fields': sorted(key for key in set(before) | set(after)
                                          if key not in before or key not in after
                                          or runner.encoded(before[key]) != runner.encoded(after[key])),
        'prompt_tokens': len(actual_tokens), 'prompt_usage_status': prompt_usage_status,
        'output_allowance': allowance,
        'maximum_model_len': maximum_model_len,
        'rendered_utf8_bytes': len(native_text.encode()),
        'rendered_sha256': hashlib.sha256(native_text.encode()).hexdigest(),
        'token_ids_sha256': hashlib.sha256(json.dumps(native_tokens, separators=(',', ':')).encode()).hexdigest(),
        'complete_prompt_text_equal': True, 'complete_prompt_token_ids_equal': True,
        'model_calls': 0, 'endpoint_calls': 0, 'tool_calls': 0,
    }
