"""Derive cue-hidden measurement inputs without changing recorded actor text.

Callers must verify the original job/config/context custody before using this
pure adapter. Hiding the injected system suffix does not undo its effect on
generation: actor-generated repetitions and endorsements remain visible. It
also removes a reader's ability to attribute an unmarked echo to that suffix.
No files, model calls, or original measurement records are changed here.
"""

import copy

from causes import campaign_reader as reader


_VIEW_KEYS = {'messages', 'initial_context', 'prefix_context', 'phase'}
_MESSAGE_KEYS = {'role', 'content', 'reasoning', 'reasoning_content',
                 'tool_calls', 'tool_call_id'}


def _check_message_fields(messages):
    # The native runner's replay_message/execute_tool fields, without arbitrary
    # caller metadata. Actor-authored text and tool arguments are never scrubbed.
    if not isinstance(messages, list):
        raise ValueError('measurement_messages_required')
    for message in messages:
        if not isinstance(message, dict) or set(message) - _MESSAGE_KEYS:
            raise ValueError('measurement_message_metadata_not_allowed')
        for call in message.get('tool_calls') or []:
            if (not isinstance(call, dict) or set(call) - {'id', 'type', 'function'}
                    or not isinstance(call.get('function'), dict)
                    or set(call['function']) - {'name', 'arguments'}):
                raise ValueError('measurement_tool_metadata_not_allowed')


def build_view(job, config, baseline_system, initial_context, messages, *,
               phase='prefix', prefix_context=None):
    """Return (view, private_provenance) for one frozen early or cumulative end.

Only the exact builder-appended system suffix is removed. SP00-like controls
must carry the native unchanged_control marker and produce an exact no-op.
The separately returned provenance must never be included in model input.
"""
    arm = job['allocation']['system_prompt_intervention']['arm']
    text = arm['text']
    if not isinstance(baseline_system, str) or not isinstance(text, str):
        raise ValueError('measurement_system_text_invalid')
    control = 'unchanged_control' in arm
    if control:
        if arm['unchanged_control'] is not True or text != '':
            raise ValueError('measurement_control_not_unchanged')
        expected_system = baseline_system
    else:
        if not text or text.strip() != text or '\0' in text:
            raise ValueError('measurement_intervention_text_invalid')
        literal = text.replace('{', '{{').replace('}', '}}')
        expected_system = baseline_system + '\n\n' + literal
    if config['system'] != expected_system:
        raise ValueError('measurement_system_suffix_mismatch')
    workspace = job['variant'].get('workspace', job['spec']['workspace'])
    expected_initial = [
        {'role': 'system', 'content': config['system'].format(workspace=workspace)},
        *copy.deepcopy(job['variant'].get('history', [])),
        {'role': 'user', 'content': job['variant']['request']}]
    if initial_context != expected_initial:
        raise ValueError('measurement_initial_context_mismatch')
    if phase == 'full' and prefix_context is None:
        raise ValueError('measurement_full_requires_paired_prefix')
    if phase == 'prefix' and prefix_context is not None:
        raise ValueError('measurement_prefix_must_not_have_paired_prefix')
    for value in (initial_context, messages, prefix_context):
        if value is not None:
            _check_message_fields(value)
            if any(m['role'] == 'system' for m in value[1:]):
                raise ValueError('measurement_additional_system_message')
    # The original native boundaries are checked before any transformation.
    reader.prepare_context(messages, len(initial_context), phase,
                           initial_context, prefix_context)
    if prefix_context is not None:
        reader.prepare_context(prefix_context, len(initial_context), 'prefix',
                               initial_context)
    baseline_rendered = baseline_system.format(workspace=workspace)

    def transformed(value):
        if value is None:
            return None
        result = copy.deepcopy(value)
        result[0]['content'] = baseline_rendered
        inverse = copy.deepcopy(result)
        inverse[0]['content'] = value[0]['content']
        if reader.encoded(inverse) != reader.encoded(value):
            raise ValueError('measurement_transform_not_exact_inverse')
        if control and reader.encoded(result) != reader.encoded(value):
            raise ValueError('measurement_control_changed')
        return result

    view = {'messages': transformed(messages),
            'initial_context': transformed(initial_context),
            'prefix_context': transformed(prefix_context), 'phase': phase}
    # Recheck that the same early prefix remains structurally included at end.
    prepare_judge_context(view)
    provenance = {
        'schema': 'causes.measurement_view_derivation.v1',
        'job_sha256': reader.digest(job), 'config_sha256': reader.digest(config),
        'arm_id': arm['id'], 'unchanged_control': control,
        'baseline_system_sha256': reader.digest(baseline_system),
        'changed_message_index': None if control else 0,
        'changed_field': None if control else 'content',
        'original_context_sha256': reader.digest(messages),
        'measurement_context_sha256': reader.digest(view['messages']),
        'original_initial_sha256': reader.digest(initial_context),
        'measurement_initial_sha256': reader.digest(view['initial_context']),
        'original_prefix_sha256': reader.digest(prefix_context) if prefix_context is not None else None,
        'measurement_prefix_sha256': reader.digest(view['prefix_context']) if prefix_context is not None else None,
        'generated_actor_text_unchanged': True,
        'external_system_cue_removed': not control,
        'treatment_inference_from_generated_text_still_possible': True}
    return view, provenance


def prepare_judge_context(view):
    """Construct native reader data; arbitrary top-level metadata is refused."""
    if not isinstance(view, dict) or set(view) != _VIEW_KEYS:
        raise ValueError('measurement_view_metadata_not_allowed')
    for key in ('messages', 'initial_context', 'prefix_context'):
        if view[key] is not None:
            _check_message_fields(view[key])
    return reader.prepare_context(view['messages'], len(view['initial_context']),
                                  view['phase'], view['initial_context'],
                                  view['prefix_context'])


def prepare_probe_context(view):
    """Return a separate message list for the native probe's copied branch."""
    prepare_judge_context(view)
    return copy.deepcopy(view['messages'])


def make_judge_payload(view, config, prompt_directory, instrument):
    """Use the existing reader payload format without private condition data."""
    context, _ = prepare_judge_context(view)
    return reader.make_payload(config, prompt_directory, context, instrument)
