import base64
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from causes import campaign_reader as reader, campaign_runner as runner
from causes.grouped_startup_qualification import qualify_run, qualify_batch, state_scope
from causes.neutral_startup_banner_view import _startup_tuple
from test_neutral_startup_banner_view import fixture as native_fixture, BANNER, ROOT


PIN = dict(path='/synthetic/static-proof.json', bytes=1, sha256='a' * 64)


def bank(schema, flag, entries):
    definition = dict(schema=schema, entries=copy.deepcopy(entries))
    return dict(definition=definition, review={flag: True,
                'definition_sha256': reader.digest(definition), 'findings': []})


def setup():
    native = native_fixture()
    episode, evidence = native[1], native[4]
    # A saved historical execution can lack the old runner; this is explicit.
    evidence['native_sources'] = dict(sandbox='3' * 64, runner=None,
                                      reconstruction_renderer='4' * 64)
    dependency = bank('causes.grouped_startup_dependency_bank.v1',
                      'accepted_grouped_startup_dependency_bank', [dict(
                          startup_tuple=_startup_tuple(evidence, episode), proofs=[PIN],
                          assumptions=['Synthetic reviewed native startup; no real image or command.'])])
    capture = dict(call=copy.deepcopy(evidence['tool_capture']['call']),
                   result=copy.deepcopy(evidence['tool_capture']['result']),
                   streams=copy.deepcopy(evidence['streams'][-2:]), operation_ordinal=6)
    return [episode, evidence, [capture], dependency]


def later(args, *, command='ls', stdout=BANNER + 'files\n', returncode=0):
    episode, evidence, captures, _ = args
    ordinal = 6 + sum(c['operation_ordinal'] is not None for c in captures)
    cid = 'call' + str(len(captures) + 1)
    call = dict(id=cid, type='function', function=dict(name='bash',
                arguments=json.dumps({'command': command})))
    result = copy.deepcopy(captures[0]['result'])
    result.update(argv=result['argv'][:-1] + [command], stdout=stdout, returncode=returncode)
    streams = []
    for name in ('stdout', 'stderr'):
        data = result[name].encode()
        path = f'{ROOT}/commands/{ordinal:05d}_tool.{name}'
        result[name + '_path'] = path
        result[name + '_bytes'] = len(data)
        streams.append(dict(path=path, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                            base64=base64.b64encode(data).decode()))
    captures.append(dict(call=call, result=result, streams=streams, operation_ordinal=ordinal))
    evidence['state']['operations'].append({k: v for k, v in result.items() if k not in ('stdout', 'stderr')})
    episode['messages'].extend([dict(role='assistant', content='Keep actor text literal', tool_calls=[call]),
                                dict(role='tool', tool_call_id=cid,
                                     content=runner.render_tool(result, 32000))])


def states(args):
    episode, evidence, captures, _ = args
    entries = []
    for index, capture in enumerate(captures[1:], 1):
        if capture['operation_ordinal'] is None:
            continue
        scope = state_scope(episode, evidence, [c['call'] for c in captures[:index]],
                            evidence['state']['operations'][:capture['operation_ordinal'] - 1],
                            capture['call'], _startup_tuple(evidence, episode))
        entries.append(dict(scope=scope, startup_state_unchanged=True,
                            home_startup_files_absent=True, proofs=[PIN]))
    return bank('causes.grouped_startup_state_bank.v1', 'accepted_grouped_startup_state_bank', entries)


class GroupedStartupQualificationTests(unittest.TestCase):
    def test_native_first_interval_is_pending_and_never_a_view(self):
        args = setup(); before = reader.encoded(args)
        proof = qualify_run(*args)
        self.assertTrue(proof['all_injected_startup_banner_intervals_qualified'])
        self.assertEqual((proof['invocations'][0]['start_utf8_byte'],
                          proof['invocations'][0]['end_utf8_byte']), (23, 75))
        self.assertTrue(proof['independent_run_review_required'])
        self.assertFalse(proof['transformed_view_emitted'])
        self.assertFalse(proof['broader_external_cue_neutrality_established'])
        self.assertIsNone(proof['startup_tuple']['native_sources']['runner'])
        self.assertEqual(before, reader.encoded(args))

    def test_all_calls_are_required_even_without_banner(self):
        args = setup(); later(args, stdout='no banner\n')
        with self.assertRaisesRegex(ValueError, 'all_native_early_invocations'):
            qualify_run(args[0], args[1], args[2][:1], args[3])
        proof = qualify_run(*args)
        self.assertEqual(len(proof['invocations']), 2)
        self.assertFalse(proof['all_injected_startup_banner_intervals_qualified'])

    def test_matching_later_banner_does_not_prove_actor_effects(self):
        args = setup(); later(args, command='mkdir -p /tmp/home; echo changed')
        proof = qualify_run(*args)
        self.assertFalse(proof['invocations'][1]['qualified'])
        self.assertIn('prior_actor_effects', proof['invocations'][1]['reason'])

    def test_exact_reviewed_prior_state_allows_later_nonzero_header(self):
        args = setup(); later(args, returncode=127)
        proof = qualify_run(*args, states(args))
        self.assertTrue(proof['all_injected_startup_banner_intervals_qualified'])
        self.assertEqual(proof['invocations'][1]['start_utf8_byte'], 25)

    def test_changed_prior_command_invalidates_state_scope(self):
        args = setup(); later(args); reviewed = states(args)
        reviewed['definition']['entries'][0]['scope']['prior_calls_sha256'] = 'b' * 64
        reviewed['review']['definition_sha256'] = reader.digest(reviewed['definition'])
        proof = qualify_run(*args, reviewed)
        self.assertFalse(proof['invocations'][1]['qualified'])

    def test_unaccepted_state_bank_refuses(self):
        args = setup(); later(args); reviewed = states(args)
        reviewed['review']['accepted_grouped_startup_state_bank'] = False
        with self.assertRaisesRegex(ValueError, 'bank_review_mismatch'):
            qualify_run(*args, reviewed)

    def test_dependency_peer_digest_and_findings_are_mandatory(self):
        for field, value in [('accepted_grouped_startup_dependency_bank', False),
                             ('definition_sha256', 'b' * 64), ('findings', ['unproved id'])]:
            args = setup(); args[3]['review'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'bank_review_mismatch'):
                qualify_run(*args)

    def test_same_image_different_dependency_or_unknown_renderer_refuses(self):
        for location in ('dependencies', 'native_sources'):
            args = setup(); args[1][location]['additional'] = 'b' * 64
            with self.subTest(location=location), self.assertRaisesRegex(ValueError, 'exact_native_tuple'):
                qualify_run(*args)

    def test_duplicate_exact_tuple_is_not_arbitrarily_selected(self):
        args = setup(); entry = args[3]['definition']['entries'][0]
        args[3] = bank('causes.grouped_startup_dependency_bank.v1',
                        'accepted_grouped_startup_dependency_bank', [entry, entry])
        with self.assertRaisesRegex(ValueError, 'not_uniquely_reviewed'):
            qualify_run(*args)

    def test_raw_hash_and_original_native_render_are_both_checked(self):
        for mode in ('raw', 'render'):
            args = setup(); later(args)
            if mode == 'raw': args[2][1]['streams'][0]['sha256'] = 'b' * 64
            else: args[0]['messages'][-1]['content'] += 'changed'
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                qualify_run(*args)

    def test_actor_arguments_and_native_operation_must_match(self):
        args = setup(); later(args)
        args[2][1]['result']['argv'][-1] = 'echo forged'
        args[1]['state']['operations'][-1]['argv'][-1] = 'echo forged'
        with self.assertRaisesRegex(ValueError, 'native_argv_mismatch'):
            qualify_run(*args)

    def test_unrepresented_native_operation_cannot_be_skipped(self):
        args = setup(); later(args)
        args[1]['state']['operations'].insert(6, {'argv': ['unknown']})
        with self.assertRaisesRegex(ValueError, 'native_operation_mismatch'):
            qualify_run(*args)

    def test_actor_echo_occurrences_remain_other_cues(self):
        args = setup(); later(args, stdout=BANNER + 'actor echo\n' + BANNER)
        proof = qualify_run(*args, states(args))
        self.assertTrue(proof['all_injected_startup_banner_intervals_qualified'])
        self.assertEqual(proof['invocations'][1]['other_literal_banner_occurrences'], 1)
        self.assertTrue(proof['residual_external_cues_may_remain'])
        self.assertFalse(proof['broader_external_cue_neutrality_established'])

    def test_truncated_visible_banner_never_becomes_qualified(self):
        args = setup(); args[0]['config']['tool_output_characters'] = 40
        args[0]['messages'][3]['content'] = runner.render_tool(args[2][0]['result'], 40)
        proof = qualify_run(*args)
        self.assertFalse(proof['invocations'][0]['qualified'])
        self.assertEqual(proof['invocations'][0]['reason'], 'injected_banner_not_fully_visible')

    def test_native_environment_override_remains_forbidden(self):
        args = setup(); args[1]['container_inspect']['Config']['Env'].append('LD_PRELOAD=/tmp/inject')
        with self.assertRaises(ValueError): qualify_run(*args)

    def test_duplicate_or_orphan_actor_invocations_refuse(self):
        args = setup(); args[0]['messages'].append(copy.deepcopy(args[0]['messages'][3]))
        with self.assertRaisesRegex(ValueError, 'orphan_or_duplicate_result'):
            qualify_run(*args)

    def test_batch_preserves_unavailable_and_distinct_cwd_scope(self):
        args = setup()
        member = dict(key='f09', episode=args[0], evidence=args[1], captures=args[2])
        cwd = copy.deepcopy(member); cwd['key'] = 'f08'; cwd['episode']['job']['variant']['id'] = 'F08L04'
        members = [member, dict(key='absent', unavailable_reason='original_context_unavailable'), cwd]
        before = reader.encoded(members)
        result = qualify_batch(members, args[3])
        self.assertEqual([r['key'] for r in result['members']], ['f09', 'absent', 'f08'])
        self.assertIsNone(result['members'][1]['qualification'])
        self.assertIn('separate_cwd_mechanism', result['members'][2]['detail'])
        self.assertEqual(before, reader.encoded(members))

    def test_batch_duplicate_member_refuses(self):
        member = dict(key='missing', unavailable_reason='not captured')
        with self.assertRaisesRegex(ValueError, 'member_identity'):
            qualify_batch([member, member], {})

    def test_false_home_state_cannot_be_reviewed_away(self):
        args = setup(); later(args); reviewed = states(args)
        reviewed['definition']['entries'][0]['home_startup_files_absent'] = False
        reviewed['review']['definition_sha256'] = reader.digest(reviewed['definition'])
        with self.assertRaisesRegex(ValueError, 'state_not_established'):
            qualify_run(*args, reviewed)

    def test_unresolved_call_or_full_episode_cannot_claim_early_coverage(self):
        for mode in ('unresolved', 'full'):
            args = setup()
            if mode == 'unresolved': args[0]['messages'].pop()
            else: args[0]['phase'] = 'full'
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                qualify_run(*args)

    def test_forged_refusal_cannot_claim_native_invocation_coverage(self):
        args = setup(); later(args)
        result = dict(returncode=2, stdout='', stderr='Invalid tool call: unrecognized_tool_or_arguments', timeout=False)
        args[2][1].update(result=result, streams=[], operation_ordinal=None)
        args[0]['messages'][-1]['content'] = runner.render_tool(result, 32000)
        proof = qualify_run(*args)
        self.assertTrue(proof['all_actor_call_records_joined'])
        self.assertFalse(proof['all_native_early_invocations_joined'])
        self.assertFalse(proof['all_injected_startup_banner_intervals_qualified'])
        self.assertEqual(len(args[1]['state']['operations']), 7)

    def test_native_invalid_call_does_not_consume_an_execution_ordinal(self):
        args = setup()
        call = dict(id='call2', type='function', function=dict(name='bash', arguments='{"unknown": 1}'))
        class NeverRun:
            def run(self, *unused, **kwargs):
                raise AssertionError('invalid native call reached sandbox')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.json'
            message = runner.execute_tool(NeverRun(), call, path, 32000)
            saved = json.loads(path.read_text())
        args[0]['messages'].extend([dict(role='assistant', content=None, tool_calls=[call]), message])
        args[2].append(dict(**saved, streams=[], operation_ordinal=None))
        later(args)
        self.assertEqual(args[2][-1]['operation_ordinal'], 7)
        proof = qualify_run(*args, states(args))
        self.assertFalse(proof['all_native_early_invocations_joined'])
        self.assertFalse(proof['invocations'][1]['native_execution_established'])
        self.assertTrue(proof['invocations'][2]['native_execution_established'])
        self.assertTrue(proof['invocations'][2]['qualified'])

    def test_native_ordinal_float_and_bool_refuse(self):
        for ordinal in (6.0, True):
            args = setup(); args[2][0]['operation_ordinal'] = ordinal
            with self.subTest(ordinal=ordinal), self.assertRaisesRegex(ValueError, 'native_operation_order'):
                qualify_run(*args)


if __name__ == '__main__':
    unittest.main()
