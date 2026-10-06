from __future__ import annotations

import argparse
import concurrent.futures
import copy
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import time
import traceback
from urllib.parse import urlsplit


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode()


def sha(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(encoded(value) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())


def exchange(endpoint, payload, destination, timeout=600):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    save(destination / 'request.json', payload)
    receipt = {'started_at': now(), 'request_sha256': sha(payload), 'retry': False}
    save(destination / 'attempt.json', receipt)
    url = urlsplit(endpoint)
    connection_type = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
    connection = connection_type(url.hostname, url.port, timeout=timeout)
    started = time.monotonic()
    raw = bytearray()
    try:
        connection.request('POST', url.path, body=encoded(payload), headers={
            'Content-Type': 'application/json', 'X-Client-Request-Id': sha(str(destination))[:32]})
        response = connection.getresponse()
        receipt['http_status'] = response.status
        with (destination / 'response.raw').open('xb') as stream:
            while chunk := response.read(65536):
                stream.write(chunk)
                raw.extend(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if response.status != 200:
            raise RuntimeError('model_http_status_' + str(response.status))
        result = json.loads(raw)
        receipt['response_sha256'] = hashlib.sha256(raw).hexdigest()
        receipt['usage'] = result.get('usage')
        receipt['model'] = result.get('model')
        receipt['system_fingerprint'] = result.get('system_fingerprint')
        receipt['status'] = 'complete'
        return result
    except BaseException as error:
        receipt['status'] = 'transport_or_response_failure'
        receipt['error'] = repr(error)
        raise
    finally:
        receipt['elapsed_seconds'] = time.monotonic() - started
        receipt['finished_at'] = now()
        save(destination / 'receipt.json', receipt)
        connection.close()


def request(config, messages, seed, tools=None):
    payload = copy.deepcopy(config['sampling'])
    payload.update(model=config['model'], messages=copy.deepcopy(messages), seed=seed, stream=False, n=1)
    if tools:
        payload['tools'] = copy.deepcopy(tools)
        payload['tool_choice'] = 'auto'
        payload['parallel_tool_calls'] = True
    return payload


def check_rendered_context(response, messages):
    rendered = response.get('prompt_text')
    if not isinstance(rendered, str):
        raise RuntimeError('rendered_prompt_missing')
    for message in messages:
        content = message.get('content')
        if isinstance(content, str) and content.strip() and content.strip() not in rendered:
            raise RuntimeError('prior_content_not_preserved_in_rendered_prompt')
        reasoning = message.get('reasoning') or message.get('reasoning_content')
        if reasoning and reasoning.strip() not in rendered:
            raise RuntimeError('prior_reasoning_not_preserved_in_rendered_prompt')
        for call in message.get('tool_calls') or []:
            function = call['function']
            if function['name'] not in rendered:
                raise RuntimeError('prior_tool_name_not_preserved')
            try:
                arguments = json.loads(function['arguments'])
            except (ValueError, TypeError):
                arguments = function['arguments']
            def check_value(value):
                if isinstance(value, dict):
                    for nested in value.values():
                        check_value(nested)
                elif isinstance(value, list):
                    for nested in value:
                        check_value(nested)
                elif isinstance(value, str) and value.strip():
                    escaped = json.dumps(value, ensure_ascii=False)[1:-1]
                    if value.strip() not in rendered and escaped not in rendered:
                        raise RuntimeError('prior_tool_argument_not_preserved')
            check_value(arguments)


def seed_for(seed, tag):
    return int(sha([seed, tag])[:8], 16) % (2**31 - 1)


def replay_message(raw):
    result = {key: copy.deepcopy(raw[key]) for key in ('role', 'content', 'tool_calls', 'reasoning', 'reasoning_content')
              if key in raw and raw[key] is not None}
    result.setdefault('content', None)
    return result


def render_tool(result, limit):
    text = 'exit status: ' + str(result['returncode']) + '\nstdout:\n' + result['stdout']
    text += '\nstderr:\n' + result['stderr']
    if result.get('timeout'):
        text += '\nCommand exceeded the time limit.'
    if len(text) > limit:
        text = text[:limit] + '\n[Tool output truncated; use a narrower command to inspect further.]'
    return text


def checkpoint_summary(record):
    return {key: value for key, value in record.items() if key != 'git_diff_before_pause'}


def execute_tool(sandbox, call, destination, limit):
    function = call.get('function', {})
    try:
        args = json.loads(function.get('arguments', '{}'))
        if function['name'] == 'read' and set(args) == {'path'} and isinstance(args['path'], str):
            command = ['cat', '--', args['path']]
        elif function['name'] == 'bash' and set(args) == {'command'} and isinstance(args['command'], str):
            command = ['bash', '-lc', args['command']]
        else:
            raise ValueError('unrecognized_tool_or_arguments')
    except (ValueError, KeyError, TypeError) as error:
        result = {'returncode': 2, 'stdout': '', 'stderr': 'Invalid tool call: ' + str(error), 'timeout': False}
    else:
        result = sandbox.run(command, timeout=90)
    save(destination, {'call': call, 'result': result})
    return {'role': 'tool', 'tool_call_id': call['id'], 'content': render_tool(result, limit)}


def probe(config, context, question, seed, destination):
    original_sha = sha(context)
    messages = copy.deepcopy(context)
    messages.append({'role': 'user', 'content': question['text']})
    payload = request(config, messages, seed, config['tools'])
    payload['tool_choice'] = 'none'
    if config.get('probe_response_format'):
        payload['response_format'] = copy.deepcopy(config['probe_response_format'])
    payload['max_tokens'] = config.get('probe_max_tokens', 8192)
    response = exchange(config['endpoint'], payload, destination, config.get('request_timeout_seconds', 600))
    check_rendered_context(response, messages)
    if sha(context) != original_sha:
        raise RuntimeError('probe_mutated_original_context')
    choice = response['choices'][0]
    result = {'instrument': question['id'], 'awareness_type': question['awareness_type'],
              'threshold': question['threshold'], 'context_sha256': original_sha,
              'question_sha256': sha(question), 'finish_reason': choice['finish_reason'],
              'message': choice['message'], 'technical_status': 'complete' if choice['finish_reason'] == 'stop' else 'truncated'}
    try:
        answer = json.loads(choice['message'].get('content') or '')
        allowed = config.get('answer_categories', [])
        if set(answer) != {'answer', 'reason'} or answer['answer'] not in allowed or not isinstance(answer['reason'], str):
            raise ValueError('invalid_answer_shape')
        result['answer'] = answer['answer']
        result['reason'] = answer['reason']
    except (ValueError, TypeError):
        result['technical_status'] = 'invalid_answer_schema'
        result['interpretation_status'] = 'unparsed'
    save(Path(destination) / 'result.json', result)
    return result


def run_episode(spec, variant, config, allocation, output):
    from causes.campaign_sandbox import Sandbox
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    save(output / 'allocation.json', allocation)
    save(output / 'spec.json', spec)
    save(output / 'variant.json', variant)
    save(output / 'model.json', config)
    workspace = variant.get('workspace', spec['workspace'])
    messages = [{'role': 'system', 'content': config['system'].format(workspace=workspace)}]
    messages.extend(copy.deepcopy(variant.get('history', [])))
    messages.append({'role': 'user', 'content': variant['request']})
    effective_spec = copy.deepcopy(spec)
    overrides = dict(entry.split('=', 1) for entry in variant.get('env', []))
    effective_spec['actor_env'] = [entry for entry in spec.get('actor_env', []) if entry.split('=', 1)[0] not in overrides]
    effective_spec['actor_env'].extend(key + '=' + value for key, value in overrides.items())
    sandbox = Sandbox(effective_spec, output / 'sandbox', allocation['run_id'], workspace_path=workspace, files=variant.get('files', []))
    summary = {'started_at': now(), 'run_id': allocation['run_id'], 'task_id': spec['task_id'],
               'variant_id': variant['id'], 'stage': allocation['stage'], 'early_status': 'missing',
               'end_status': 'not_selected', 'tool_results': 0, 'calls': 0, 'probes': []}
    started = time.monotonic()
    early = None
    natural = False
    try:
        sandbox.start()
        save(output / 'initial_context.json', messages)
        for turn in range(config.get('max_actor_turns', 128)):
            if time.monotonic() - started > config.get('max_episode_seconds', 7200):
                summary['end_status'] = 'administratively_censored_runtime'
                break
            payload = request(config, messages, seed_for(allocation['seed'], ['actor', turn]), config['tools'])
            response = exchange(config['endpoint'], payload, output / 'actor' / f'{turn:04d}', config.get('request_timeout_seconds', 600))
            summary['calls'] += 1
            check_rendered_context(response, messages)
            choice = response['choices'][0]
            message = replay_message(choice['message'])
            messages.append(message)
            calls = message.get('tool_calls') or []
            if choice['finish_reason'] not in ('stop', 'tool_calls'):
                summary['end_status'] = 'generation_' + str(choice['finish_reason'])
                break
            if calls:
                ids = [call.get('id') for call in calls]
                if not all(ids) or len(ids) != len(set(ids)):
                    raise RuntimeError('invalid_tool_call_identifiers')
                # Preserve declared order: shell commands within a batch can depend on each other.
                for index, call in enumerate(calls):
                    messages.append(execute_tool(sandbox, call, output / 'tools' / f'{turn:04d}_{index:03d}.json', config.get('tool_output_characters', 32000)))
                    summary['tool_results'] += 1
            else:
                natural = choice['finish_reason'] == 'stop'
            save(output / 'contexts' / f'{turn:04d}.json', messages)
            if early is None and (summary['tool_results'] >= 3 or natural):
                early = copy.deepcopy(messages)
                summary['early_status'] = 'earlier_natural_completion' if natural else 'completed_batch_at_least_three_results'
                summary['early_elapsed_seconds'] = time.monotonic() - started
                summary['early_context_sha256'] = sha(early)
                save(output / 'early_context.json', early)
                try:
                    summary['early_workspace_checkpoint'] = checkpoint_summary(sandbox.checkpoint('early'))
                    if not summary['early_workspace_checkpoint'].get('complete', False):
                        raise RuntimeError('early_workspace_snapshot_incomplete')
                    summary['early_workspace_status'] = 'captured'
                except BaseException as error:
                    summary['early_workspace_status'] = 'failed'
                    summary['early_workspace_error'] = repr(error)
                    raise
                finally:
                    save(output / 'early_capture.json', {key: value for key, value in summary.items() if key != 'probes'})
            if natural:
                summary['end_status'] = 'natural_completion'
                save(output / 'end_context.json', messages)
                if allocation.get('continue_to_end'):
                    summary['end_workspace_checkpoint'] = checkpoint_summary(sandbox.checkpoint('end'))
                    summary['end_workspace_status'] = 'captured' if summary['end_workspace_checkpoint'].get('complete') else 'failed'
                break
            if early is not None and not allocation.get('continue_to_end', False):
                break
        else:
            summary['end_status'] = 'administratively_censored_turns'
        summary['actor_context_sha256'] = sha(messages)
        save(output / 'actor_final_context.json', messages)
    except BaseException as error:
        summary['actor_error'] = repr(error)
        summary['traceback'] = traceback.format_exc()
        summary['end_status'] = 'technical_failure'
        if not (output / 'actor_final_context.json').exists():
            save(output / 'actor_final_context.json', messages)
    finally:
        try:
            closure = sandbox.close()
            if isinstance(closure, dict) and closure.get('state') == 'closure_requires_review':
                closure = sandbox.close()
            if isinstance(closure, dict):
                summary['sandbox_closure'] = {key: closure.get(key) for key in ('state', 'closure_errors', 'overlay_mounted', 'retained_container')}
        except BaseException as error:
            summary['sandbox_close_error'] = repr(error)
        save(output / 'actor_summary.json', summary)
    # No probe response can enter the original actor history or its workspace.
    for stage, context in [('early', early), ('end', messages if natural and allocation.get('continue_to_end') else None)]:
        if context is None:
            continue
        for question in config['questions']:
            if question['id'] not in allocation.get('instruments', []):
                continue
            try:
                result = probe(config, context, question, seed_for(allocation['seed'], ['probe', stage, question['id']]), output / 'probes' / stage / question['id'])
                summary['probes'].append({'stage': stage, **result})
            except BaseException as error:
                summary['probes'].append({'stage': stage, 'instrument': question['id'], 'technical_status': 'failure', 'error': repr(error)})
    summary['finished_at'] = now()
    summary['elapsed_seconds'] = time.monotonic() - started
    save(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--jobs', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    jobs = json.loads(Path(args.jobs).read_text())
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    def run(job):
        destination = output / job['allocation']['run_id']
        if destination.exists():
            return {'run_id': job['allocation']['run_id'], 'status': 'existing_evidence_not_replayed'}
        return run_episode(job['spec'], job['variant'], config, job['allocation'], destination)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run, job): job['allocation']['run_id'] for job in jobs}
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                print(json.dumps({key: result.get(key) for key in ('run_id', 'early_status', 'end_status', 'calls', 'elapsed_seconds', 'actor_error', 'status')}), flush=True)
            except BaseException as error:
                print(json.dumps({'run_id': futures[future], 'controller_error': repr(error)}), flush=True)


if __name__ == '__main__':
    main()
