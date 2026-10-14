"""Pure, pending qualifications for injected banners in frozen early tool calls.

The caller authenticates the saved inputs and independently reviewed proof banks.
Review flags here check joins; they do not establish the authenticity of arbitrary
caller supplied evidence. This module neither executes commands nor edits views.
"""

import copy
import json
from pathlib import PurePosixPath

from causes import campaign_reader as reader, campaign_runner as runner
from causes.neutral_startup_banner_view import _BANNER, _raw, _startup_tuple
from causes.neutral_summary_view import _require, _same


def _pins(rows):
    _require(isinstance(rows, list) and rows, 'qualification_proof_pins_required')
    for row in rows:
        _require(isinstance(row, dict) and set(row) == {'path', 'bytes', 'sha256'}
                 and isinstance(row['path'], str) and row['path']
                 and type(row['bytes']) is int and row['bytes'] >= 0
                 and isinstance(row['sha256'], str) and len(row['sha256']) == 64
                 and all(c in '0123456789abcdef' for c in row['sha256']),
                 'qualification_proof_pin_invalid')


def _bank(bank, schema, flag):
    _require(isinstance(bank, dict) and set(bank) == {'definition', 'review'},
             'qualification_bank_invalid')
    definition, review = bank['definition'], bank['review']
    _require(isinstance(definition, dict) and set(definition) == {'schema', 'entries'}
             and definition['schema'] == schema and isinstance(definition['entries'], list),
             'qualification_bank_definition_invalid')
    _require(isinstance(review, dict) and review.get(flag) is True
             and review.get('definition_sha256') == reader.digest(definition)
             and review.get('findings') == [], 'qualification_bank_review_mismatch')
    return definition['entries']


def _dependency_entry(bank, actual):
    entries = _bank(bank, 'causes.grouped_startup_dependency_bank.v1',
                    'accepted_grouped_startup_dependency_bank')
    matches = []
    for entry in entries:
        _require(isinstance(entry, dict)
                 and set(entry) == {'startup_tuple', 'proofs', 'assumptions'},
                 'qualification_dependency_entry_invalid')
        _pins(entry['proofs'])
        _require(isinstance(entry['assumptions'], list) and entry['assumptions']
                 and all(isinstance(x, str) and x for x in entry['assumptions']),
                 'qualification_assumptions_required')
        if _same(entry['startup_tuple'], actual):
            matches.append(entry)
    _require(len(matches) == 1, 'qualification_exact_native_tuple_not_uniquely_reviewed')
    return matches[0]


def state_scope(episode, evidence, prior_calls, operation_prefix, call, actual_tuple):
    """Exact inputs a separate reviewer must assess for a later startup.

    This computes a scope, not a finding that previous commands were harmless.
    In particular a matching stdout banner is never evidence of unchanged state.
    """
    return dict(run_id=episode['job']['allocation']['run_id'],
                episode_sha256=reader.digest(episode), evidence_sha256=reader.digest(evidence),
                prior_calls_sha256=reader.digest(prior_calls),
                operation_prefix_sha256=reader.digest(operation_prefix),
                call_sha256=reader.digest(call), startup_tuple_sha256=reader.digest(actual_tuple))


def _later_state(bank, scope):
    if bank is None:
        return None
    entries = _bank(bank, 'causes.grouped_startup_state_bank.v1',
                    'accepted_grouped_startup_state_bank')
    matches = []
    for entry in entries:
        _require(isinstance(entry, dict) and set(entry) == {
            'scope', 'startup_state_unchanged', 'home_startup_files_absent', 'proofs'},
            'qualification_state_entry_invalid')
        _pins(entry['proofs'])
        _require(entry['startup_state_unchanged'] is True
                 and entry['home_startup_files_absent'] is True,
                 'qualification_state_not_established')
        if _same(entry['scope'], scope):
            matches.append(entry)
    _require(len(matches) <= 1, 'qualification_duplicate_state_proof')
    return matches[0] if matches else None


def _early_calls(episode):
    messages, initial = episode['messages'], episode['initial_context']
    _require(episode['phase'] == 'prefix' and episode['prefix_context'] is None
             and _same(messages[:len(initial)], initial), 'qualification_frozen_early_episode_required')
    calls, results, ids = [], {}, set()
    for index, message in enumerate(messages[len(initial):], len(initial)):
        if message.get('tool_calls'):
            _require(message.get('role') == 'assistant', 'qualification_non_actor_call')
            for call in message['tool_calls']:
                cid = call.get('id')
                _require(isinstance(cid, str) and cid and cid not in ids,
                         'qualification_duplicate_or_invalid_call_id')
                ids.add(cid)
                calls.append((index, call))
        if message.get('role') == 'tool':
            cid = message.get('tool_call_id')
            _require(cid in ids and cid not in results, 'qualification_orphan_or_duplicate_result')
            results[cid] = index
    _require(calls and set(results) == ids, 'qualification_unresolved_early_call')
    _require([results[c['id']] for _, c in calls] == sorted(results.values()),
             'qualification_tool_order_mismatch')
    return calls, results


def _receipt(evidence, capture, ordinal):
    receipt = capture['result']
    _require(type(ordinal) is int and ordinal >= 6
             and type(capture['operation_ordinal']) is int
             and capture['operation_ordinal'] == ordinal, 'qualification_native_operation_order')
    _require(type(receipt.get('returncode')) is int and receipt.get('timeout') is False,
             'qualification_incomplete_native_execution')
    streams = capture['streams']
    _require(isinstance(streams, list) and len(streams) == 2,
             'qualification_two_raw_streams_required')
    payloads = {s['path']: _raw(s) for s in streams}
    _require(len(payloads) == 2, 'qualification_duplicate_raw_stream')
    root = PurePosixPath(evidence['command_receipts'][0]['stdout_path']).parent.parent
    for name in ('stdout', 'stderr'):
        path = str(root / 'commands' / f'{ordinal:05d}_tool.{name}')
        _require(receipt.get(name + '_path') == path and path in payloads
                 and type(receipt.get(name + '_bytes')) is int
                 and receipt[name + '_bytes'] == len(payloads[path])
                 and receipt.get(name + '_truncated') is False
                 and receipt.get(name) == payloads[path].decode('utf-8', errors='replace'),
                 'qualification_raw_receipt_mismatch')
    projection = {k: v for k, v in receipt.items() if k not in ('stdout', 'stderr')}
    operations = evidence['state']['operations']
    _require(ordinal <= len(operations) and _same(operations[ordinal - 1], projection),
             'qualification_native_operation_mismatch')
    return payloads[receipt['stdout_path']]


def qualify_run(episode, evidence, captures, dependency_bank, state_bank=None):
    """Join every frozen early call and return pending banner qualifications.

    Dependency entries bind the entire native 19-field tuple, including exact
    source/dependency maps and environment. A later call additionally needs an
    independently reviewed exact prior-command/state scope. Neither acceptance
    nor removal is performed here. Non-leading occurrences remain untouched.
    Native read calls invoke cat directly, so all their output stays literal;
    they remain part of the history used to assess any later shell startup.
    """
    _require(episode['job']['variant'].get('id') == 'F09L03',
             'qualification_separate_cwd_mechanism_required')
    actual = _startup_tuple(evidence, episode)
    dependency = _dependency_entry(dependency_bank, actual)
    calls, result_indices = _early_calls(episode)
    _require(isinstance(captures, list) and len(captures) == len(calls),
             'qualification_all_native_early_invocations_required')
    limit = episode['config'].get('tool_output_characters', 32000)
    _require(type(limit) is int and limit > 0, 'qualification_renderer_limit_invalid')
    rows, prior, ordinal = [], [], 6
    for number, ((_, call), capture) in enumerate(zip(calls, captures)):
        _require(isinstance(capture, dict) and set(capture) == {
            'call', 'result', 'streams', 'operation_ordinal'}, 'qualification_capture_fields_invalid')
        _require(_same(capture['call'], call), 'qualification_actor_call_mismatch')
        index, result = result_indices[call['id']], capture['result']
        visible = episode['messages'][index]['content']
        _require(visible == runner.render_tool(result, limit), 'qualification_original_render_mismatch')
        if number == 0:
            _require(_same({'call': call, 'result': result}, evidence['tool_capture']),
                     'qualification_first_capture_mismatch')
        row = dict(call_id=call['id'], message_index=index, qualified=False,
                   native_execution_established=False,
                   startup_invoked=None,
                   original_message_sha256=reader.digest(episode['messages'][index]),
                   capture_sha256=reader.digest(capture))
        if capture['operation_ordinal'] is None:
            _require(capture['streams'] == [] and 'argv' not in result,
                     'qualification_refused_capture_invalid')
            row['reason'] = 'native_execution_not_established'
            rows.append(row)
            prior.append(call)
            continue
        raw = _receipt(evidence, capture, ordinal)
        _require(call.get('type') == 'function' and call['function']['name'] in ('bash', 'read'),
                 'qualification_native_tool_required')
        tool = call['function']['name']
        args = json.loads(call['function']['arguments'])
        argument = 'command' if tool == 'bash' else 'path'
        _require(isinstance(args, dict) and set(args) == {argument}
                 and isinstance(args[argument], str), 'qualification_' + tool + '_arguments_invalid')
        command = ['bash', '-lc', args['command']] if tool == 'bash' else ['cat', '--', args['path']]
        _require(result['argv'] == ['docker', 'exec', evidence['state']['container'],
                 'timeout', '-s', 'TERM', '-k', '2', '90'] + command,
                 'qualification_native_argv_mismatch')
        row['native_execution_established'] = True
        row['startup_invoked'] = tool == 'bash'
        if tool == 'read':
            row.update(reason='native_read_does_not_invoke_startup',
                       other_literal_banner_occurrences=raw.count(_BANNER.encode()))
            rows.append(row)
            prior.append(call)
            ordinal += 1
            continue
        scope = state_scope(episode, evidence, prior, evidence['state']['operations'][:ordinal - 1], call, actual)
        state = _later_state(state_bank, scope) if number else None
        start = len(('exit status: ' + str(result['returncode']) + '\nstdout:\n').encode())
        end = start + len(_BANNER.encode())
        if not raw.startswith(_BANNER.encode()):
            row['reason'] = 'leading_injected_banner_not_established'
        elif limit < end or visible.encode()[start:end] != _BANNER.encode():
            row['reason'] = 'injected_banner_not_fully_visible'
        elif number and state is None:
            row['reason'] = 'prior_actor_effects_not_independently_qualified'
        else:
            row.update(qualified=True, start_utf8_byte=start, end_utf8_byte=end,
                       raw_stdout_start=0, raw_stdout_end=len(_BANNER.encode()),
                       state_scope=scope, state_proof_sha256=reader.digest(state) if state else None)
        row['other_literal_banner_occurrences'] = raw.count(_BANNER.encode()) - int(raw.startswith(_BANNER.encode()))
        rows.append(row)
        prior.append(call)
        ordinal += 1
    return dict(schema='causes.grouped_startup_qualification.pending.v1',
                run_id=episode['job']['allocation']['run_id'], episode_sha256=reader.digest(episode),
                evidence_sha256=reader.digest(evidence), captures_sha256=reader.digest(captures),
                dependency_bank_sha256=reader.digest(dependency_bank),
                state_bank_sha256=reader.digest(state_bank) if state_bank else None,
                startup_tuple=copy.deepcopy(actual), assumptions=copy.deepcopy(dependency['assumptions']),
                invocations=rows, all_actor_call_records_joined=True,
                all_native_early_invocations_joined=all(r['native_execution_established'] for r in rows),
                all_injected_startup_banner_intervals_qualified=all(
                    r['qualified'] or (r['native_execution_established'] and r['startup_invoked'] is False)
                    for r in rows),
                independent_run_review_required=True, transformed_view_emitted=False,
                broader_external_cue_neutrality_established=False,
                residual_external_cues_may_remain=True,
                all_actor_messages_and_arguments_unchanged=True)


def qualify_batch(members, dependency_bank, state_bank=None):
    """Preserve fixed member order and explicit unavailable/unsupported members."""
    _require(isinstance(members, list), 'qualification_fixed_members_required')
    rows, seen = [], set()
    for member in members:
        _require(isinstance(member, dict) and isinstance(member.get('key'), str)
                 and member['key'] and member['key'] not in seen, 'qualification_member_identity_invalid')
        seen.add(member['key'])
        if member.get('unavailable_reason') is not None:
            _require(set(member) == {'key', 'unavailable_reason'}
                     and isinstance(member['unavailable_reason'], str) and member['unavailable_reason'],
                     'qualification_unavailable_member_invalid')
            rows.append(dict(key=member['key'], qualification=None, reason=member['unavailable_reason']))
            continue
        _require(set(member) == {'key', 'episode', 'evidence', 'captures'},
                 'qualification_member_fields_invalid')
        try:
            proof = qualify_run(member['episode'], member['evidence'], member['captures'],
                                dependency_bank, state_bank)
        except (ValueError, KeyError, TypeError, IndexError) as error:
            rows.append(dict(key=member['key'], qualification=None,
                             reason='unqualified_saved_inputs', detail=str(error)))
        else:
            rows.append(dict(key=member['key'], qualification=proof, reason=None))
    return dict(schema='causes.grouped_startup_batch.pending.v1', members=rows,
                input_members_sha256=reader.digest(members), fixed_membership_preserved=True,
                transformed_views_emitted=False, independent_run_review_required=True)
