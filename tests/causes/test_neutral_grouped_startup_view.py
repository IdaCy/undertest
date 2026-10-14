import copy
import json
import unittest

from causes import campaign_reader as reader, campaign_runner as runner
from causes.grouped_startup_qualification import qualify_batch
from causes.neutral_grouped_startup_view import (
    build_neutral_pair_with_grouped_startup, build_neutral_grouped_batch)
from causes.neutral_startup_banner_view import _startup_tuple, build_neutral_pair_with_startup_banner
from causes.neutral_summary_view import build_neutral_pair
from test_grouped_startup_qualification import bank, later, later_read, states, PIN
from test_neutral_startup_banner_view import fixture as native_fixture, BANNER
from test_neutral_summary_view import rebind_condition


def fixture(**kwargs):
    native = native_fixture(**kwargs)
    episode, evidence = native[1], native[4]
    dependency = bank('causes.grouped_startup_dependency_bank.v1',
        'accepted_grouped_startup_dependency_bank', [dict(
            startup_tuple=_startup_tuple(evidence, episode), proofs=[PIN],
            assumptions=['Synthetic reviewed ordinary startup; no image or command is executed.'])])
    capture = dict(call=copy.deepcopy(evidence['tool_capture']['call']),
        result=copy.deepcopy(evidence['tool_capture']['result']),
        streams=copy.deepcopy(evidence['streams'][-2:]), operation_ordinal=6)
    return native, [episode, evidence, [capture], dependency]


def qualification(args, state=None, missing=()):
    episode, evidence, captures, dependency = args
    members = [dict(key=episode['job']['allocation']['run_id'], episode=episode,
                    evidence=evidence, captures=captures)]
    members.extend(dict(key=key, unavailable_reason='frozen_unavailable') for key in missing)
    definition = qualify_batch(members, dependency, state)
    return reviewed(definition)


def reviewed(definition):
    return dict(definition=definition, review=dict(
        accepted_grouped_startup_qualification_batch=True,
        definition_sha256=reader.digest(definition), findings=[]))


def pair(native, args, state=None, accepted=None):
    return build_neutral_pair_with_grouped_startup(
        *native[:4], args[1], args[2], args[3], state,
        qualification(args, state) if accepted is None else accepted)


def member(native, args):
    return dict(key=args[0]['job']['allocation']['run_id'], baseline_episode=native[0],
        treatment_episode=native[1], summary_row=native[2], construction=native[3],
        startup_evidence=args[1], captures=args[2])


class NeutralGroupedStartupViewTests(unittest.TestCase):
    def test_first_interval_matches_unchanged_first_banner_api(self):
        native, args = fixture()
        expected, _ = build_neutral_pair_with_startup_banner(*native)
        actual, proof = pair(native, args)
        self.assertEqual(actual, expected)
        self.assertEqual(proof['removed_startup_intervals'], 1)
        self.assertFalse(proof['broader_external_cue_neutrality_established'])

    def test_two_startups_keep_native_read_and_later_literal_banner(self):
        native, args = fixture(stdout=BANNER + 'payload café\n' + BANNER)
        later_read(args, path='README.md', stdout=BANNER + 'literal file bytes\n')
        later(args, command='ls -la /workspace/project', stdout=BANNER + 'listed\n' + BANNER)
        accepted_state = states(args)
        before = reader.encoded([native, args, accepted_state])
        original_views, _ = build_neutral_pair(*native[:4])
        views, proof = pair(native, args, accepted_state)
        self.assertEqual(proof['removed_startup_intervals'], 2)
        self.assertEqual(views['baseline'], original_views['baseline'])
        self.assertEqual(views['treatment']['messages'][5], original_views['treatment']['messages'][5])
        for index in (2, 4, 6):
            self.assertEqual(views['treatment']['messages'][index], original_views['treatment']['messages'][index])
        self.assertEqual(views['treatment']['messages'][3]['content'].count(BANNER), 1)
        self.assertEqual(views['treatment']['messages'][7]['content'].count(BANNER), 1)
        self.assertEqual(before, reader.encoded([native, args, accepted_state]))

    def test_inverse_reconstructs_entire_original_episode_context(self):
        native, args = fixture()
        later(args, returncode=127)
        views, proof = pair(native, args, states(args))
        restored = copy.deepcopy(views['treatment']['messages'])
        for edit in reversed(proof['contexts']['messages']['edits']):
            row = restored[edit['derived_index']]
            raw = row['content'].encode(); start = edit['start_utf8_byte']
            row['content'] = (raw[:start] + edit['removed_text'].encode() + raw[start:]).decode()
        original_initial = proof['initial_derivation']['arms']['treatment']['contexts']['messages']['inverse_original_initial_context']
        self.assertEqual(original_initial + restored[2:], native[1]['messages'])
        self.assertEqual(proof['contexts']['messages']['edits'][1]['start_utf8_byte'], 25)

    def test_truncation_is_spliced_without_revealing_hidden_suffix(self):
        native, args = fixture(stdout=BANNER + 'visible\n' + 'hidden' * 50, limit=84)
        original = native[1]['messages'][3]['content']
        views, _ = pair(native, args)
        self.assertEqual(views['treatment']['messages'][3]['content'], original[:23] + original[75:])
        self.assertIn('[Tool output truncated;', views['treatment']['messages'][3]['content'])

    def test_partially_visible_banner_refuses_even_with_accepted_pending_proof(self):
        for limit in (22, 30, 74):
            native, args = fixture(limit=limit)
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                pair(native, args)

    def test_missing_later_state_refuses_whole_run_without_partial_view(self):
        native, args = fixture(); later(args)
        accepted = qualification(args)
        self.assertTrue(accepted['definition']['members'][0]['qualification']['invocations'][0]['qualified'])
        with self.assertRaisesRegex(ValueError, 'incomplete_run'):
            pair(native, args, accepted=accepted)

    def test_absent_later_banner_is_not_silently_accepted(self):
        native, args = fixture(); later(args, stdout='ordinary output\n')
        with self.assertRaisesRegex(ValueError, 'incomplete_run'):
            pair(native, args, states(args))

    def test_unestablished_execution_refuses_even_if_other_interval_qualified(self):
        native, args = fixture(); later(args)
        capture = args[2][-1]
        capture['operation_ordinal'] = None; capture['streams'] = []
        del capture['result']['argv']
        args[1]['state']['operations'].pop()
        native[1]['messages'][-1]['content'] = runner.render_tool(capture['result'], 32000)
        with self.assertRaisesRegex(ValueError, 'incomplete_run'):
            pair(native, args)

    def test_independent_review_flag_digest_and_findings_are_required(self):
        for key, value in [('accepted_grouped_startup_qualification_batch', False),
                           ('definition_sha256', '0' * 64), ('findings', ['unresolved'])]:
            native, args = fixture(); accepted = qualification(args)
            accepted['review'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'bank_review_mismatch'):
                pair(native, args, accepted=accepted)

    def test_reviewed_proof_is_regenerated_not_trusted_for_coordinates(self):
        native, args = fixture(); accepted = qualification(args)
        accepted['definition']['members'][0]['qualification']['invocations'][0]['start_utf8_byte'] += 1
        accepted = reviewed(accepted['definition'])
        with self.assertRaisesRegex(ValueError, 'regenerated_proof_mismatch'):
            pair(native, args, accepted=accepted)

    def test_changed_actor_argument_bytes_fail_stale_review(self):
        native, args = fixture(); accepted = qualification(args)
        call = copy.deepcopy(args[2][0]['call'])
        call['function']['arguments'] = '{"command":"echo payload"}'
        args[2][0]['call'] = call
        native[1]['messages'][2]['tool_calls'] = [copy.deepcopy(call)]
        args[1]['tool_capture']['call'] = copy.deepcopy(call)
        with self.assertRaisesRegex(ValueError, 'regenerated_proof_mismatch'):
            pair(native, args, accepted=accepted)

    def test_raw_evidence_mutation_refuses_before_edit(self):
        native, args = fixture(); accepted = qualification(args)
        args[2][0]['streams'][0]['sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            pair(native, args, accepted=accepted)

    def test_rehashed_noncanonical_external_construction_refuses(self):
        for field, value in [('files', []), ('env', ['BASH_ENV=/elsewhere']),
                             ('provenance', 'actor_authored')]:
            native, args = fixture()
            native[3]['condition_variant'][field] = value
            rebind_condition(native[:4])
            accepted = qualification(args)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'construction_unsupported'):
                pair(native, args, accepted=accepted)

    def test_full_episode_is_not_silently_treated_as_reviewed_early_scope(self):
        native, args = fixture(phase='full')
        accepted = qualification(args)
        with self.assertRaisesRegex(ValueError, 'run_not_qualified'):
            pair(native, args, accepted=accepted)

    def test_fixed_360_batch_retains_359_unavailable_rows(self):
        native, args = fixture()
        missing = ['missing-' + str(i) for i in range(359)]
        accepted = qualification(args, missing=missing)
        members = [member(native, args)] + [dict(key=k, unavailable_reason='frozen_unavailable') for k in missing]
        before = reader.encoded([members, accepted])
        result = build_neutral_grouped_batch(members, args[3], None, accepted)
        self.assertEqual([row['key'] for row in result['members']], [row['key'] for row in members])
        self.assertEqual(sum(row['views'] is not None for row in result['members']), 1)
        self.assertTrue(all(row['reason'] == 'frozen_unavailable' and row['views'] is None for row in result['members'][1:]))
        self.assertEqual(before, reader.encoded([members, accepted]))

    def test_changed_batch_order_drop_or_replacement_fails_globally(self):
        native, args = fixture(); accepted = qualification(args, missing=['missing'])
        members = [member(native, args), dict(key='missing', unavailable_reason='originally_missing')]
        candidates = [members[::-1], members[:1], [members[0], dict(key='replacement', unavailable_reason='invented')]]
        for changed in candidates:
            with self.subTest(keys=[r['key'] for r in changed]), self.assertRaisesRegex(ValueError, 'fixed_membership'):
                build_neutral_grouped_batch(changed, args[3], None, accepted)

    def test_duplicate_bank_keys_fail_before_row_refusal(self):
        native, args = fixture(); accepted = qualification(args)
        accepted['definition']['members'].append(copy.deepcopy(accepted['definition']['members'][0]))
        accepted = reviewed(accepted['definition'])
        with self.assertRaisesRegex(ValueError, 'bank_member_invalid'):
            build_neutral_grouped_batch([], args[3], None, accepted)

    def test_bank_schema_and_unaccepted_global_review_raise_not_missing_rows(self):
        native, args = fixture()
        for mode in ('schema', 'review'):
            accepted = qualification(args)
            if mode == 'schema':
                accepted['definition']['schema'] = 'unrelated'
                accepted = reviewed(accepted['definition'])
            else:
                accepted['review']['accepted_grouped_startup_qualification_batch'] = False
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                build_neutral_grouped_batch([member(native, args)], args[3], None, accepted)

    def test_per_row_construction_refusal_preserves_key_without_initial_only_fallback(self):
        native, args = fixture(); accepted = qualification(args)
        native[2]['summary_sha256'] = '0' * 64
        result = build_neutral_grouped_batch([member(native, args)], args[3], None, accepted)
        row = result['members'][0]
        self.assertEqual(row['key'], native[1]['job']['allocation']['run_id'])
        self.assertIsNone(row['views']); self.assertIsNone(row['derivation'])
        self.assertEqual(row['reason'], 'grouped_view_refused')
        self.assertIn('summary_provenance', row['detail'])

    def test_run_key_cannot_borrow_another_accepted_row(self):
        native, args = fixture(); accepted = qualification(args)
        changed = member(native, args)
        changed['treatment_episode'] = copy.deepcopy(changed['treatment_episode'])
        changed['treatment_episode']['job']['allocation']['run_id'] = 'different-run'
        result = build_neutral_grouped_batch([changed], args[3], None, accepted)
        self.assertEqual(result['members'][0]['reason'], 'grouped_view_refused')
        self.assertIn('run_mismatch', result['members'][0]['detail'])

    def test_native_read_only_interval_is_never_selected_for_deletion(self):
        native, args = fixture(); later_read(args, stdout='café\n' + BANNER)
        original = native[1]['messages'][-1]
        views, proof = pair(native, args)
        self.assertEqual(views['treatment']['messages'][-1], original)
        self.assertEqual(len(proof['contexts']['messages']['edits']), 1)


if __name__ == '__main__':
    unittest.main()
