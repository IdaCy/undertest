"""Audit saved closed actor episodes without executing tools or model calls."""

import copy
import datetime as dt
import hashlib
import json
import math
import re
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


def audit_prefix_episode(directory, job, config, *, original_directory):
    """Audit a nontechnical native prefix without extending its termination rule."""
    return _audit_episode(directory, job, config, original_directory=original_directory,
                          allow_censored=False, prefix=True)


def _prefix_stages(root, summary, allocation):
    require(allocation.get('continue_to_end') is False, 'prefix_episode_required')
    require(summary.get('end_status') in ('not_selected', 'natural_completion')
            and not summary.get('actor_error') and not summary.get('sandbox_close_error')
            and summary['sandbox_closure']['state'] == 'closed'
            and summary['sandbox_closure']['overlay_mounted'] is False
            and summary['sandbox_closure']['closure_errors'] == [], 'nontechnical_closed_prefix_required')
    require(summary.get('early_status') in ('earlier_natural_completion',
                                          'completed_batch_at_least_three_results'), 'prefix_capture_required')
    require(not any(key in summary for key in ('end_workspace_status', 'end_workspace_checkpoint',
                                               'end_workspace_error')), 'prefix_end_checkpoint_metadata')
    require(not any((root / name).exists() or (root / name).is_symlink()
                    for name in ('checkpoints/end', 'sandbox/sandbox/checkpoints/end', 'probes/end')),
            'prefix_has_unassigned_end_evidence')
    if summary['end_status'] != 'natural_completion':
        require(not (root / 'end_context.json').exists() and not (root / 'end_context.json').is_symlink(),
                'prefix_has_unassigned_end_context')
    return ['early']


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


def _audit_episode(directory, job, config, *, original_directory, allow_censored, prefix=False):
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
    captured = (_prefix_stages(root, summary, allocation) if prefix else
                _closed_stages(root, summary, allocation) if allow_censored else ['early', 'end'])
    natural_ending = summary['end_status'] == 'natural_completion'
    require(summary['run_id'] == allocation['run_id'] and summary['task_id'] == spec['task_id']
            and summary['variant_id'] == variant['id']
            and (natural_ending or allow_censored or prefix) and not summary.get('actor_error')
            and summary['sandbox_closure']['state'] == 'closed'
            and summary['sandbox_closure']['overlay_mounted'] is False
            and summary['sandbox_closure']['closure_errors'] == [], 'natural_closed_actor_required')
    require(allocation['continue_to_end'] is (not prefix), 'prefix_episode_required' if prefix else 'full_episode_required')
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
        if prefix and early is not None:
            require(turn == count - 1, 'prefix_continued_after_native_capture')
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
    if prefix:
        result.update(prefix_only=True, end_status=summary['end_status'], captured_stages=captured)
    return result


def _native_tool_command(call):
    try:
        function = call['function']
        arguments = json.loads(function['arguments'])
        if call.get('type') != 'function':
            return None
        if function['name'] == 'read' and set(arguments) == {'path'} and isinstance(arguments['path'], str):
            return ['cat', '--', arguments['path']]
        if function['name'] == 'bash' and set(arguments) == {'command'} and isinstance(arguments['command'], str):
            return ['bash', '-lc', arguments['command']]
    except (KeyError, ValueError, TypeError):
        pass
    return None


def _saved_tool_execution(root, original, state, saved, command):
    result = saved['result']
    stdout = Path(result['stdout_path'])
    command_root = original / 'sandbox/sandbox/commands'
    require(stdout.parent == command_root and re.fullmatch(r'\d{5,}_tool\.stdout', stdout.name)
            and result['stderr_path'] == str(stdout.with_suffix('.stderr')), 'tool_execution_path_changed')
    relative = stdout.relative_to(original)
    raw = _read(root, str(relative.with_suffix('.json')))
    expected = dict(raw, timeout=raw['timeout'] or raw['returncode'] in (124, 137))
    argv = ['docker', 'exec', state['container'], 'timeout', '-s', 'TERM', '-k', '2', '90', *command]
    require(all(isinstance(value, str) and '\0' not in value for value in argv),
            'tool_execution_invalid_argv')
    require(result == expected and raw['argv'] == argv,
            'tool_execution_command_changed')
    require(type(raw['returncode']) is int and type(raw['timeout']) is bool
            and type(raw['elapsed_seconds']) in (int, float)
            and math.isfinite(raw['elapsed_seconds']) and raw['elapsed_seconds'] >= 0,
            'tool_execution_status_changed')
    metadata = {key: value for key, value in raw.items() if key not in ('stdout', 'stderr')}
    require(state['operations'].count(metadata) == 1, 'tool_execution_missing_from_sandbox_state')
    streams = {}
    for label in ('stdout', 'stderr'):
        path = handoff.canonical_path(root / relative.with_suffix('.' + label))
        require(path.is_relative_to(root), 'tool_execution_stream_outside_run')
        actual = _file_hash(path)
        with path.open('rb') as stream:
            text = stream.read(1048576).decode('utf-8', errors='replace')
        require(type(raw[label + '_bytes']) is int and raw[label + '_bytes'] == actual['bytes']
                and raw[label] == text and raw[label + '_truncated'] is (actual['bytes'] > 1048576),
                'tool_execution_stream_changed:' + label)
        streams[label] = actual
    return {'command_record': str(relative.with_suffix('.json')), 'streams': streams,
            'returncode': raw['returncode'], 'timeout': expected['timeout']}


def audit_prefix_tool_exchanges(directory, job, config, *, original_directory):
    """Join prefix requests to retained sandbox execution; never run a tool.

    Zero qualifying exchanges is an explicit result, not actor qualification.
    Closed snapshot custody and the exact native runner/sandbox version must be
    established separately. Nonzero command exits can be valid tool exchanges.
    """
    result = audit_prefix_episode(directory, job, config, original_directory=original_directory)
    root, original = handoff.canonical_path(directory), Path(original_directory)
    state = _read(root, 'sandbox/sandbox/STATE.json')
    summary = _read(root, 'summary.json')
    require(state['schema'] == 'caus.sandbox.v1' and state['run_id'] == job['allocation']['run_id']
            and isinstance(state['container'], str) and bool(state['container'])
            and isinstance(state['operations'], list)
            and all(state.get(key) == value for key, value in summary['sandbox_closure'].items()),
            'tool_sandbox_identity_or_closure_changed')
    coverage = {name: {'observed_calls': 0, 'verified_executions': 0, 'complete_exchanges': 0}
                for name in ('read', 'bash')}
    exchanges, invalid, used_commands = [], [], set()
    for turn in range(result['successful_requests']):
        response = _read(root, f'actor/{turn:04d}/response.raw')
        calls = response['choices'][0]['message'].get('tool_calls') or []
        for index, call in enumerate(calls):
            tool_file = f'tools/{turn:04d}_{index:03d}.json'
            name = call.get('function', {}).get('name')
            if name in coverage:
                coverage[name]['observed_calls'] += 1
            command = _native_tool_command(call)
            if command is None:
                invalid.append({'turn': turn, 'index': index, 'tool_call_id': call['id']})
                continue
            execution = _saved_tool_execution(root, original, state, _read(root, tool_file), command)
            require(execution['command_record'] not in used_commands, 'tool_execution_reused_for_multiple_calls')
            used_commands.add(execution['command_record'])
            complete = turn + 1 < result['successful_requests']
            coverage[name]['verified_executions'] += 1
            coverage[name]['complete_exchanges'] += int(complete)
            exchanges.append({'turn': turn, 'index': index, 'tool_name': name, 'tool_call_id': call['id'],
                              'subsequent_request_verified': complete, **execution})
    result.update(native_tool_executions=exchanges, invalid_or_unsupported_tool_calls=invalid,
                  tool_coverage=coverage,
                  complete_native_tool_exchanges=sum(row['subsequent_request_verified'] for row in exchanges))
    return result
