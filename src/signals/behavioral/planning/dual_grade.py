"""Pure saved-public adapter. Both imported checkers are unchanged pinned source.

No source repair, answer extraction, search, optimality or statistical inference.
Ordinary checker exceptions are retained as null; process/resource/I/O failures
propagate to the isolated controller and never authorize a replay.
"""
import json
import traceback


def canonical(value):
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


FAILURES = {'action_arity': 'wrong_action_arity',
            'action_argument_type': 'wrong_argument_type',
            'precondition_unsatisfied': 'unsatisfied_precondition',
            'missing_goals': 'missing_final_goals'}


def projection(result, accepted):
    if result['status'] == 'INVALID_STRICT_JSON':
        return {'parsed': False}
    if result['status'] != 'PARSED':
        raise ValueError('nonsemantic_checker_result')
    v = result['raw']['observations'] if accepted else result['raw']['replay']
    trace = v['trace'] if accepted else result['raw']['trace']
    events = []
    for event in trace:
        item = {k: event[k] for k in ('step', 'action', 'before', 'after',
                                    'missing_preconditions', 'deletes', 'adds') if k in event}
        if 'failure' in event:
            item['failure'] = FAILURES.get(event['failure'], event['failure'])
        events.append(item)
    reason = v['failure'] if accepted else v['reason']
    return canonical({'parsed': True, 'task_success': result['task_success'],
                      'reason': FAILURES.get(reason, reason),
                      'failed_step': v['failed_step'], 'assigned_actions': v['assigned_actions'],
                      'completed_actions': v['completed_actions'],
                      'missing_goals': v['missing_goals'],
                      'final': v['final'] if accepted else v['final_pin'], 'trace': events})


def accepted_call(module, domain, problem, public):
    model = module.parse_source(domain, problem)
    raw = module.grade(public, model, transport_complete=True)
    if raw.get('status') == 'INVALID_WHOLE_FINAL' and raw.get('task_success') is None:
        return {'status': 'INVALID_STRICT_JSON', 'task_success': None, 'raw': raw}
    if raw.get('status') != 'VALID_PARSED_ANSWER' or type(raw.get('task_success')) is not bool:
        raise ValueError('accepted_checker_unrecognized_result')
    return {'status': 'PARSED', 'task_success': raw['task_success'], 'raw': raw}


def independent_call(module, domain, problem, public):
    d = module.parse_domain(domain)
    p = module.parse_problem(problem, d)
    try:
        actions = module.parse_actions(public)
    except module.ActionDocumentError as exc:
        return {'status': 'INVALID_STRICT_JSON', 'task_success': None,
                'raw': {'error_type': type(exc).__name__, 'error': str(exc)}}
    trace = []
    replay, final = module.replay(d, p, actions, observer=trace.append)
    if type(replay.get('witness_valid')) is not bool:
        raise ValueError('independent_checker_unrecognized_result')
    return {'status': 'PARSED', 'task_success': replay['witness_valid'],
            'raw': {'replay': replay, 'trace': trace,
                    'returned_final_pin': module.state_digest(final)}}


def dual_grade(domain, problem, public, accepted, independent, emit=lambda value: None):
    results = {}
    for name, module, call in (('accepted', accepted, accepted_call),
                               ('independent', independent, independent_call)):
        emit({'event': 'CHECKER_STARTED', 'checker': name})
        result = None
        try:
            result = call(module, domain, problem, public)
            # Projection failures are also checker-interface errors, never False.
            result['comparison'] = projection(result, name == 'accepted')
            if name == 'independent' and result['status'] == 'PARSED':
                if result['raw']['returned_final_pin'] != result['raw']['replay']['final_pin']:
                    raise ValueError('independent_returned_state_pin_mismatch')
        except (MemoryError, OSError):
            raise
        except Exception as exc:
            returned = result
            result = {'status': 'CHECKER_EXCEPTION', 'task_success': None,
                      'error_type': type(exc).__name__, 'error': str(exc),
                      'traceback': traceback.format_exc(), 'returned_before_exception': returned}
        results[name] = result
        emit({'event': 'CHECKER_RETURNED', 'checker': name, 'result': result})
    a, b = results['accepted'], results['independent']
    success = None
    if any(v['status'] == 'CHECKER_EXCEPTION' for v in results.values()):
        status = 'CHECKER_EXCEPTION'
    elif a['comparison'] != b['comparison']:
        status = 'CHECKER_DISAGREEMENT'
    elif a['status'] == b['status'] == 'INVALID_STRICT_JSON':
        status = 'INVALID_STRICT_JSON'
    else:
        success = a['task_success']
        status = 'PASS' if success else 'FAIL'
    return {'status': status, 'task_success': success, 'checkers': results,
            'whole_public_unchanged': True, 'optimality_claim': False,
            'awareness_annotation_used': False}
