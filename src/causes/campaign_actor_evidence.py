"""Audit saved closed actor episodes without executing tools or model calls."""

import copy
import datetime as dt
import hashlib
import json
import math
from pathlib import Path

from causes import campaign_handoff as handoff
from causes import campaign_runner as runner
from causes.campaign_capacity_summary import stats


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _read(root, name):
    path = handoff.canonical_path(root / name)
    require(path.is_relative_to(root) and path.is_file(), 'missing_actor_evidence:' + name)
    return json.loads(path.read_bytes())


def _file_hash(path):
    path = handoff.canonical_path(path)
    with path.open('rb') as stream:
        value = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'bytes': path.stat().st_size, 'sha256': value}


def _time(value):
    parsed = dt.datetime.fromisoformat(value)
    require(parsed.tzinfo is not None, 'request_timestamp_requires_timezone')
    return parsed.timestamp()


def audit_natural_episode(directory, job, config, *, original_directory):
    """Reconstruct every saved turn and verify referenced checkpoint archives.

    The caller must establish a closed, immutable snapshot and qualify the
    runner version, provider, telemetry and experiment membership separately.
    A successful audit reports evidence; it does not authorize another run.
    """
    return _audit_episode(directory, job, config, original_directory=original_directory, allow_censored=False)


def audit_closed_episode(directory, job, config, *, original_directory):
    """Audit a natural or administratively censored, nontechnical full episode.

    Captured stages are reconstructed; uncaptured stages are explicit absences,
    not failed probes. Runtime censoring is a retained native disposition, not
    an independent reconstruction of the runner's monotonic stopping clock.
    Snapshot custody and runner-source qualification remain caller obligations.
    """
    return _audit_episode(directory, job, config, original_directory=original_directory, allow_censored=True)


def _closed_stages(root, summary, allocation):
    require(allocation.get('continue_to_end') is True, 'full_episode_required')
    require(summary.get('end_status') in ('natural_completion', 'administratively_censored_turns',
                                         'administratively_censored_runtime')
            and not summary.get('actor_error') and not summary.get('sandbox_close_error')
            and summary['sandbox_closure']['state'] == 'closed'
            and summary['sandbox_closure']['overlay_mounted'] is False
            and summary['sandbox_closure']['closure_errors'] == [], 'nontechnical_closed_actor_required')
    require(summary.get('early_status') in ('missing', 'earlier_natural_completion',
                                          'completed_batch_at_least_three_results'), 'early_status_changed')
    captured = []
    for phase, present in [('early', summary['early_status'] != 'missing'),
                           ('end', summary['end_status'] == 'natural_completion')]:
        if present:
            checkpoint = summary[phase + '_workspace_checkpoint']
            require(summary[phase + '_workspace_status'] == 'captured' and checkpoint['complete'] is True
                    and checkpoint['errors'] == [] and checkpoint['name'] == phase
                    and checkpoint['archives'], 'checkpoint_not_complete:' + phase)
            context = _read(root, phase + '_context.json')
            require(runner.sha(context) == summary['early_context_sha256' if phase == 'early'
                                                   else 'actor_context_sha256'], 'stage_context_changed:' + phase)
            captured.append(phase)
        else:
            fields = [phase + '_workspace_status', phase + '_workspace_checkpoint', phase + '_workspace_error']
            names = [phase + '_context.json', 'checkpoints/' + phase, 'probes/' + phase]
            if phase == 'early':
                fields.extend(['early_context_sha256', 'early_elapsed_seconds'])
                names.append('early_capture.json')
            require(not any(key in summary for key in fields), 'uncaptured_stage_metadata:' + phase)
            require(not any((root / name).exists() or (root / name).is_symlink() for name in names),
                    'uncaptured_stage_has_evidence:' + phase)
    return captured


def _audit_episode(directory, job, config, *, original_directory, allow_censored):
    root = handoff.canonical_path(directory)
    original = Path(original_directory)
    require(original.is_absolute() and '..' not in original.parts, 'invalid_original_directory')
    allocation, spec, variant = (job[key] for key in ('allocation', 'spec', 'variant'))
    for name, expected in [('allocation', allocation), ('spec', spec), ('variant', variant), ('model', config)]:
        require(_read(root, name + '.json') == expected, 'actor_identity_changed:' + name)
    summary = _read(root, 'summary.json')
    actor_summary = _read(root, 'actor_summary.json')
    require(all(summary.get(key) == value for key, value in actor_summary.items() if key != 'probes'),
            'actor_summary_changed')
    captured = _closed_stages(root, summary, allocation) if allow_censored else ['early', 'end']
    natural_ending = summary['end_status'] == 'natural_completion'
    require(summary['run_id'] == allocation['run_id'] and summary['task_id'] == spec['task_id']
            and summary['variant_id'] == variant['id']
            and (natural_ending or allow_censored) and not summary.get('actor_error')
            and summary['sandbox_closure']['state'] == 'closed'
            and summary['sandbox_closure']['overlay_mounted'] is False
            and summary['sandbox_closure']['closure_errors'] == [], 'natural_closed_actor_required')
    require(allocation['continue_to_end'] is True, 'full_episode_required')
    count = summary['calls']
    maximum = config.get('max_actor_turns', 128)
    require(type(maximum) is int and maximum > 0 and type(count) is int
            and (0 if allow_censored else 1) <= count <= maximum, 'invalid_actor_call_count')
    if summary['end_status'] == 'administratively_censored_turns':
        require(count == maximum, 'turn_censor_before_limit')
    elif summary['end_status'] == 'administratively_censored_runtime':
        limit = config.get('max_episode_seconds', 7200)
        require(type(limit) in (int, float) and math.isfinite(limit) and limit >= 0
                and count < maximum and summary['elapsed_seconds'] > limit, 'runtime_censor_inconsistent')
    else:
        require(count > 0, 'invalid_actor_call_count')
    expected_turns = [f'{index:04d}' for index in range(count)]
    require((sorted(path.name for path in (root / 'actor').iterdir()) if (root / 'actor').exists() else []) == expected_turns, 'actor_turn_roster_changed')
    require((sorted(path.name for path in (root / 'contexts').iterdir()) if (root / 'contexts').exists() else [])
            == [name + '.json' for name in expected_turns], 'context_turn_roster_changed')
    workspace = variant.get('workspace', spec['workspace'])
    messages = [{'role': 'system', 'content': config['system'].format(workspace=workspace)}]
    messages.extend(copy.deepcopy(variant.get('history', [])))
    messages.append({'role': 'user', 'content': variant['request']})
    require(_read(root, 'initial_context.json') == messages, 'initial_context_changed')
    rows, tool_names, early = [], [], None
    for turn, name in enumerate(expected_turns):
        actor = root / 'actor' / name
        require(sorted(path.name for path in actor.iterdir())
                == ['attempt.json', 'receipt.json', 'request.json', 'response.raw'], 'actor_request_file_roster_changed')
        request = _read(root, f'actor/{name}/request.json')
        expected = runner.request(config, messages, runner.seed_for(allocation['seed'], ['actor', turn]), config['tools'])
        require(request == expected, 'actor_request_changed:' + name)
        attempt = _read(root, f'actor/{name}/attempt.json')
        receipt = _read(root, f'actor/{name}/receipt.json')
        response_path = handoff.canonical_path(actor / 'response.raw')
        raw = response_path.read_bytes()
        response = json.loads(raw)
        require(attempt['request_sha256'] == receipt['request_sha256'] == runner.sha(request)
                and receipt['response_sha256'] == hashlib.sha256(raw).hexdigest()
                and attempt['retry'] is False and receipt['retry'] is False
                and receipt['started_at'] == attempt['started_at']
                and receipt['status'] == 'complete' and receipt['http_status'] == 200,
                'actor_request_receipt_changed:' + name)
        require(all(receipt.get(key) == response.get(key) for key in ('usage', 'model', 'system_fingerprint'))
                and response['model'] == config['model'], 'actor_response_metadata_changed:' + name)
        elapsed = receipt['elapsed_seconds']
        require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0,
                'invalid_request_elapsed:' + name)
        require(_time(receipt['finished_at']) >= _time(receipt['started_at'])
                and (not rows or _time(receipt['started_at']) >= _time(rows[-1]['finished_at'])),
                'request_chronology_changed:' + name)
        runner.check_rendered_context(response, messages)
        require(len(response['choices']) == 1, 'single_actor_choice_required')
        choice = response['choices'][0]
        require(choice['finish_reason'] in ('stop', 'tool_calls'), 'unclean_actor_finish:' + name)
        message = runner.replay_message(choice['message'])
        require(message['role'] == 'assistant', 'actor_response_role_changed')
        messages.append(message)
        calls = message.get('tool_calls') or []
        ids = [call.get('id') for call in calls]
        require(all(ids) and len(ids) == len(set(ids)), 'invalid_tool_call_ids')
        for index, call in enumerate(calls):
            tool_name = f'{name}_{index:03d}.json'
            saved = _read(root, 'tools/' + tool_name)
            require(saved['call'] == call, 'saved_tool_call_changed:' + tool_name)
            messages.append({'role': 'tool', 'tool_call_id': call['id'],
                             'content': runner.render_tool(saved['result'], config.get('tool_output_characters', 32000))})
            tool_names.append(tool_name)
        natural = not calls and choice['finish_reason'] == 'stop'
        require(natural == (natural_ending and turn == count - 1), 'natural_completion_position_changed')
        require(_read(root, f'contexts/{name}.json') == messages, 'saved_actor_context_changed:' + name)
        if early is None and (len(tool_names) >= 3 or natural):
            early = copy.deepcopy(messages)
            status = 'earlier_natural_completion' if natural else 'completed_batch_at_least_three_results'
            require(summary['early_status'] == status, 'early_status_changed')
        usage = response.get('usage') or {}
        rows.append({'turn': turn, 'request_sha256': runner.sha(request),
                     'response_sha256': receipt['response_sha256'], 'started_at': receipt['started_at'],
                     'finished_at': receipt['finished_at'], 'elapsed_seconds': elapsed,
                     'usage': usage, 'model': response['model'],
                     'system_fingerprint': response.get('system_fingerprint'), 'finish_reason': choice['finish_reason']})
    actual_tools = sorted(path.name for path in (root / 'tools').iterdir()) if (root / 'tools').exists() else []
    require(actual_tools == sorted(tool_names) and summary['tool_results'] == len(tool_names), 'tool_result_roster_changed')
    require((early is not None) == ('early' in captured), 'early_capture_presence_changed')
    if early is not None:
        require(_read(root, 'early_context.json') == early and summary['early_context_sha256'] == runner.sha(early),
                'early_context_changed')
        early_capture = _read(root, 'early_capture.json')
        require(early_capture['early_context_sha256'] == summary['early_context_sha256']
                and early_capture['early_workspace_checkpoint'] == summary['early_workspace_checkpoint'],
                'early_capture_changed')
    require(_read(root, 'actor_final_context.json') == messages
            and summary['actor_context_sha256'] == runner.sha(messages), 'final_actor_context_changed')
    if natural_ending:
        require(_read(root, 'end_context.json') == messages, 'final_actor_context_changed')
    archives = []
    for phase in captured:
        checkpoint = summary[phase + '_workspace_checkpoint']
        require(summary[phase + '_workspace_status'] == 'captured' and checkpoint['complete'] is True
                and checkpoint['errors'] == [] and checkpoint['name'] == phase
                and checkpoint['archives'], 'checkpoint_not_complete:' + phase)
        for archive in checkpoint['archives']:
            source = Path(archive['path'])
            require(source.is_relative_to(original) and '..' not in source.parts, 'checkpoint_archive_outside_run')
            relative = source.relative_to(original)
            actual = _file_hash(root / relative)
            require(actual == {key: archive[key] for key in ('bytes', 'sha256')}, 'checkpoint_archive_changed:' + str(relative))
            archives.append({'phase': phase, 'path': str(relative), 'role': archive['role'], **actual})
    missing_usage = {key: [row['turn'] for row in rows
                          if type(row['usage'].get(key)) is not int or row['usage'][key] < 0]
                     for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
    episode_elapsed = summary['elapsed_seconds']
    require(type(episode_elapsed) in (int, float) and math.isfinite(episode_elapsed)
            and episode_elapsed >= sum(row['elapsed_seconds'] for row in rows) - 0.001,
            'episode_elapsed_shorter_than_requests')
    result = {'run_id': allocation['run_id'], 'successful_requests': len(rows),
            'tool_results': len(tool_names), 'episode_elapsed_seconds': episode_elapsed,
            'request_latency_seconds': stats([row['elapsed_seconds'] for row in rows]),
            'requests': rows, 'missing_or_invalid_token_usage': missing_usage,
            'checkpoint_archives': archives, 'natural_completion': natural_ending,
            'contexts_reconstructed': True, 'sandbox_closed': True,
            'model_calls': 0, 'tool_executions': 0}
    if allow_censored:
        result.update(end_status=summary['end_status'], captured_stages=captured,
                      uncaptured_stages=[phase for phase in ('early', 'end') if phase not in captured])
    return result
