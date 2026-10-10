"""Independent adverse cases for reviewed donor progression and source succession.

All files and histories are synthetic. No deployment inputs or network calls are
used; accepted metadata is varied independently from the bytes it authorizes.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from causes import campaign_runner as runner
from causes import campaign_supplement as core
from causes import campaign_supplement_launch as launch
from tests.causes import test_supplement_launch_succession as integration_fixtures


class LocalEvidence(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sequence = 0

    def save(self, value, name=None):
        self.sequence += 1
        path = self.root / (name or f'evidence_{self.sequence}.json')
        core.save(path, value)
        return core.pin(path)


class DonorProgressionReviewTests(LocalEvidence):
    @staticmethod
    def state(lifetime=3, **updates):
        return {'lifetime_route_failures': lifetime, 'nonzero_failures': {},
                'quarantines': {}, 'unresolved_events': [],
                'unconsumed_tail_bytes': 0, **updates}

    @staticmethod
    def replay(raw):
        # The public boundary receives a separately pinned replay callback.
        # Synthetic complete states allow independent tests of every field.
        return json.loads(raw.splitlines()[-1])

    def fixture(self, before=None, after=None, *, progression=True):
        before = self.state() if before is None else before
        after = self.state(5) if after is None else after
        baseline = self.save(before)
        self.sequence += 1
        path = self.root / f'journal_{self.sequence}.jsonl'
        path.write_bytes(core.pinned(baseline) + core.encoded(after) + b'\n')
        scope = {'id': 'donor', 'identity': {'owner': 17, 'route': 'model:selected'},
                 'mode': 'live_prefix', 'baseline': baseline,
                 'accepted_state': copy.deepcopy(before),
                 'model_aliases': ['model:selected', 'endpoint:selected']}
        if progression:
            scope['history_progression'] = 'append_only_lifetime_v1'
        row = {'id': 'donor', 'identity': copy.deepcopy(scope['identity']),
               'capture': core.pin(path)}
        return scope, row

    def check(self, scope, row):
        return launch.shared_histories([scope], [row], self.replay)

    def test_opt_in_allows_only_retained_monotonic_lifetime_and_preserves_inputs(self):
        for lifetime in (3, 4, 300):
            with self.subTest(lifetime=lifetime):
                scope, row = self.fixture(after=self.state(lifetime))
                before = copy.deepcopy((scope, row))
                self.assertEqual(self.check(scope, row), 0)
                self.assertEqual((scope, row), before)
                self.assertEqual(self.replay(core.pinned(row['capture']))['lifetime_route_failures'], lifetime)

    def test_absent_opt_in_retains_exact_legacy_equality(self):
        scope, row = self.fixture(progression=False)
        with self.assertRaisesRegex(core.AdmissionHold, 'shared_route_history_changed'):
            self.check(scope, row)
        row['capture'] = scope['baseline']
        self.assertEqual(self.check(scope, row), 0)

    def test_unknown_null_and_sealed_opt_in_refuse(self):
        for value in (None, True, {}, 'append_only_lifetime_v2'):
            with self.subTest(value=value):
                scope, row = self.fixture()
                scope['history_progression'] = value
                with self.assertRaisesRegex(core.IntegrityError, 'unsupported_donor_history_progression'):
                    self.check(scope, row)
        scope, row = self.fixture()
        scope['mode'] = 'sealed'
        row['capture'] = scope['baseline']
        with self.assertRaisesRegex(core.IntegrityError, 'unsupported_donor_history_progression'):
            self.check(scope, row)
        del scope['history_progression']
        self.assertEqual(self.check(scope, row), 0)
        scope, row = self.fixture(progression=False)
        scope['mode'] = 'sealed'
        with self.assertRaisesRegex(core.IntegrityError, 'sealed_donor_changed'):
            self.check(scope, row)

    def test_lifetime_must_be_nonnegative_integer_and_never_decrease(self):
        for old, new in ((3, 2), (-1, 0), (False, 1), (0, True),
                         (3.0, 4), (3, 4.0), (3, '4'), (3, None)):
            with self.subTest(old=old, new=new):
                scope, row = self.fixture(self.state(old), self.state(new))
                with self.assertRaisesRegex(core.IntegrityError, 'invalid_donor_lifetime_progression'):
                    self.check(scope, row)

    def test_every_other_history_field_remains_strict_even_with_recovered_lifetime(self):
        changes = {'nonzero_failures': {'model:selected': 1},
                   'quarantines': {'model:unrelated': {'reason': 'retained'}},
                   'unresolved_events': [{'event': 'unrecognized'}],
                   'unconsumed_tail_bytes': 1}
        for key, value in changes.items():
            with self.subTest(field=key):
                scope, row = self.fixture(after=self.state(5, **{key: value}))
                with self.assertRaises((core.AdmissionHold, core.IntegrityError)):
                    self.check(scope, row)
        scope, row = self.fixture(after=self.state(5, unconsumed_tail_bytes=0.0))
        with self.assertRaisesRegex(core.AdmissionHold, 'shared_route_history_changed'):
            self.check(scope, row)

    def test_existing_unrelated_history_is_preserved_and_selected_residual_is_returned(self):
        retained = {'nonzero_failures': {'model:selected': 2, 'model:other': 7},
                    'quarantines': {'model:other': {'reason': 'historical'}}}
        scope, row = self.fixture(self.state(3, **retained), self.state(5, **retained))
        self.assertEqual(self.check(scope, row), 2)
        # A recovered residual is still a changed field, not silently rebased.
        scope, row = self.fixture(self.state(3, **retained), self.state(5))
        with self.assertRaisesRegex(core.AdmissionHold, 'shared_route_history_changed'):
            self.check(scope, row)
        selected = {'quarantines': {'endpoint:selected': {'reason': 'held'}}}
        scope, row = self.fixture(self.state(3, **selected), self.state(5, **selected))
        with self.assertRaisesRegex(core.AdmissionHold, 'shared_model_route_quarantined'):
            self.check(scope, row)

    def test_accepted_baseline_prefix_and_typed_identity_cannot_be_rebound(self):
        scope, row = self.fixture()
        scope['accepted_state']['lifetime_route_failures'] = 4
        with self.assertRaisesRegex(core.IntegrityError, 'accepted_donor_baseline_changed'):
            self.check(scope, row)
        scope, row = self.fixture()
        row['capture'] = self.save(self.state(5))
        with self.assertRaisesRegex(core.IntegrityError, 'donor_journal_prefix_changed'):
            self.check(scope, row)
        scope, row = self.fixture()
        row['identity']['owner'] = 17.0
        with self.assertRaisesRegex(core.IntegrityError, 'donor_route_identity_changed'):
            self.check(scope, row)

    def test_progression_is_per_donor_and_does_not_relax_another_scope(self):
        live, live_row = self.fixture()
        strict, strict_row = self.fixture(progression=False)
        strict['id'] = strict_row['id'] = 'second'
        with self.assertRaisesRegex(core.AdmissionHold, 'shared_route_history_changed'):
            launch.shared_histories([live, strict], [live_row, strict_row], self.replay)
        strict_row['capture'] = strict['baseline']
        self.assertEqual(launch.shared_histories([live, strict], [live_row, strict_row], self.replay), 0)
        with self.assertRaisesRegex(core.IntegrityError, 'donor_scope_changed'):
            launch.shared_histories([live, strict], [strict_row, live_row], self.replay)


class LauncherSuccessionReviewTests(LocalEvidence):
    def setUp(self):
        super().setUp()
        self.output = self.root / 'output'
        self.control = core.mkdir(self.output / '.control')
        self.old = self.save({'source': 'old'})
        self.new = self.save({'source': 'new'})
        self.expected = {'family': 'family', 'model': 'model',
                         'prepared': self.save({'jobs': ['first', 'second', 'pending']}),
                         'core': self.save({'source': 'core'}),
                         'runner': self.save({'source': 'runner'}), 'launcher': self.new}
        core.save(self.control / 'IDENTITY.json', {**self.expected, 'launcher': self.old})
        self.identity = core.pin(self.control / 'IDENTITY.json')
        self.baseline = self.save({'event': 'controller_finished', 'active': 0})
        (self.control / 'journal.jsonl').write_bytes(core.pinned(self.baseline))
        self.retired = {'accepted_supplement_launcher_retirement': True,
                        'predecessor_identity': self.identity, 'prepared': self.expected['prepared'],
                        'output_root': str(self.output), 'journal_cut': self.baseline,
                        'all_claimed_terminal': True, 'client_process_absent': True,
                        'executor_lock_acquired_nonblocking': True, 'ambiguous_attempts': 0,
                        'evidence': [self.identity, self.baseline]}
        self.review = {'accepted_supplement_launcher_succession': True,
                       'predecessor_identity': self.identity, 'old_launcher': self.old,
                       'new_launcher': self.new, 'output_root': str(self.output),
                       **{k: self.expected[k] for k in ('prepared', 'core', 'runner')},
                       'retirement_review': self.save(self.retired), 'journal_baseline': self.baseline}
        self.policy = {'output_root': str(self.output), 'launcher_source': self.new}

    def bind_policy(self, review, policy=None):
        policy = copy.deepcopy(self.policy if policy is None else policy)
        policy['launcher_succession'] = self.save(review)
        retired = json.loads(core.pinned(review['retirement_review']))
        policy['required_pins'] = [policy['launcher_succession'],
            *[review[k] for k in ('predecessor_identity', 'old_launcher', 'new_launcher',
                                 'prepared', 'core', 'runner', 'retirement_review', 'journal_baseline')],
            *(retired['evidence'] if isinstance(retired.get('evidence'), list) else [])]
        return policy

    def validate(self, review=None, retired=None, expected=None, policy=None):
        review = copy.deepcopy(self.review if review is None else review)
        if retired is not None:
            review['retirement_review'] = self.save(retired)
        policy = self.bind_policy(review, policy)
        return launch.validate_launcher_succession(policy, expected or self.expected, self.control)

    def test_valid_review_is_read_only_and_receipt_binds_exact_old_and_new_sources(self):
        before = {p.name: p.read_bytes() for p in self.control.iterdir()}
        receipt = self.validate()
        self.assertEqual(receipt['old_launcher'], self.old)
        self.assertEqual(receipt['new_launcher'], self.new)
        self.assertEqual(receipt['predecessor_identity'], self.identity)
        self.assertEqual({p.name: p.read_bytes() for p in self.control.iterdir()}, before)
        self.assertNotIn('remaining_must_be_zero', receipt)

    def test_unaccepted_or_nonboolean_review_and_retirement_flags_refuse(self):
        for value in (False, 1, None):
            with self.subTest(flag=value):
                review = {**self.review, 'accepted_supplement_launcher_succession': value}
                with self.assertRaisesRegex(core.IntegrityError, 'accepted_launcher_succession_required'):
                    self.validate(review=review)
                retired = {**self.retired, 'accepted_supplement_launcher_retirement': value}
                with self.assertRaisesRegex(core.IntegrityError, 'accepted_launcher_retirement_required'):
                    self.validate(retired=retired)

    def test_every_retirement_boundary_requires_explicit_true_and_integer_zero(self):
        for key in ('all_claimed_terminal', 'client_process_absent', 'executor_lock_acquired_nonblocking'):
            for value in (False, 1, None):
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(core.IntegrityError, 'launcher_predecessor_not_retired'):
                        self.validate(retired={**self.retired, key: value})
        for value in (False, 0.0, -1, 1, None):
            with self.subTest(ambiguous=value):
                with self.assertRaisesRegex(core.IntegrityError, 'launcher_predecessor_not_retired'):
                    self.validate(retired={**self.retired, 'ambiguous_attempts': value})

    def test_retirement_needs_checked_evidence_and_exact_identity_prepared_output_and_cut(self):
        for value in ([], None, {}):
            with self.subTest(evidence=value):
                with self.assertRaisesRegex(core.IntegrityError, 'launcher_retirement_evidence_required'):
                    self.validate(retired={**self.retired, 'evidence': value})
        bad_pin = {**self.baseline, 'sha256': '0' * 64}
        with self.assertRaisesRegex(core.IntegrityError, 'file_pin_mismatch'):
            self.validate(retired={**self.retired, 'evidence': [bad_pin]})
        for key in ('predecessor_identity', 'prepared', 'output_root', 'journal_cut'):
            with self.subTest(binding=key):
                value = str(self.root / 'other') if key == 'output_root' else self.save({'other': key})
                with self.assertRaisesRegex(core.IntegrityError, 'launcher_retirement_(binding|journal)_changed'):
                    self.validate(retired={**self.retired, key: value})

    def test_all_immutable_identity_fields_and_new_source_are_bound(self):
        for key in ('prepared', 'core', 'runner'):
            with self.subTest(binding=key):
                with self.assertRaisesRegex(core.IntegrityError, 'launcher_succession_binding_changed'):
                    self.validate(review={**self.review, key: self.save({'other': key})})
        with self.assertRaisesRegex(core.IntegrityError, 'launcher_succession_sources_changed'):
            self.validate(review={**self.review, 'new_launcher': self.old})
        with self.assertRaisesRegex(core.IntegrityError, 'launcher_succession_sources_changed'):
            self.validate(policy={**self.policy, 'launcher_source': self.old})
        with self.assertRaisesRegex(core.IntegrityError, 'launcher_succession_sources_changed'):
            self.validate(review={**self.review, 'old_launcher': self.new})
        with self.assertRaisesRegex(core.IntegrityError, 'launcher_succession_identity_changed'):
            self.validate(expected={**self.expected, 'model': 'other'})
        with self.assertRaisesRegex(core.IntegrityError, 'launcher_succession_output_changed'):
            self.validate(review={**self.review, 'output_root': str(self.root / 'other')})

    def test_old_identity_and_both_launcher_bytes_are_checked_without_overwrite(self):
        for ref in (self.identity, self.old, self.new):
            with self.subTest(path=ref['path']):
                path = Path(ref['path']); before = path.read_bytes()
                path.write_bytes(before + b' ')
                try:
                    with self.assertRaisesRegex(core.IntegrityError, 'file_pin_mismatch'):
                        self.validate()
                    self.assertEqual(path.read_bytes(), before + b' ')
                finally:
                    path.write_bytes(before)
        alias = self.save(json.loads(core.pinned(self.identity)))
        with self.assertRaisesRegex(core.IntegrityError, 'launcher_succession_identity_path_changed'):
            self.validate(review={**self.review, 'predecessor_identity': alias})

    def test_journal_baseline_must_be_external_complete_and_exact_current_prefix(self):
        inside = self.control / 'inside.jsonl'
        inside.write_bytes(core.pinned(self.baseline))
        reference = core.pin(inside)
        with self.assertRaisesRegex(core.IntegrityError, 'external_launcher_journal_baseline_required'):
            self.validate(review={**self.review, 'journal_baseline': reference},
                          retired={**self.retired, 'journal_cut': reference})
        for raw in (b'', b'{"event":"partial"}'):
            with self.subTest(raw=raw):
                self.sequence += 1
                path = self.root / f'incomplete_{self.sequence}.jsonl'; path.write_bytes(raw)
                reference = core.pin(path)
                with self.assertRaisesRegex(core.IntegrityError, 'launcher_retirement_journal_prefix_changed'):
                    self.validate(review={**self.review, 'journal_baseline': reference},
                                  retired={**self.retired, 'journal_cut': reference})
        (self.control / 'journal.jsonl').write_bytes(b'{"event":"replacement"}\n')
        with self.assertRaisesRegex(core.IntegrityError, 'launcher_retirement_journal_prefix_changed'):
            self.validate()

    def test_fixed_receipt_rejects_even_a_different_review_of_same_source_pair(self):
        policy = self.bind_policy(self.review)
        receipt = launch.validate_launcher_succession(policy, self.expected, self.control)
        path = self.control / 'LAUNCHER_SUCCESSION.json'; core.save(path, receipt)
        before = core.pin(path)
        self.assertEqual(launch.validate_launcher_succession(policy, self.expected, self.control), receipt)
        # A later journal suffix is not a new migration. Core reconciliation
        # remains responsible for validating the suffix before any request.
        with (self.control / 'journal.jsonl').open('ab') as stream:
            stream.write(b'{"event":"controller_finished","active":0}\n')
        self.assertEqual(launch.validate_launcher_succession(policy, self.expected, self.control), receipt)
        with self.assertRaisesRegex(core.IntegrityError, 'launcher_succession_already_bound_differently'):
            self.validate()
        self.assertEqual(core.pin(path), before)
        self.assertEqual(core.pin(self.control / 'IDENTITY.json'), self.identity)

    def test_every_migration_proof_must_be_in_per_request_required_pins(self):
        extra = self.save({'independent_retirement_evidence': 'closed client'})
        retired = {**self.retired, 'evidence': [*self.retired['evidence'], extra]}
        review = {**self.review, 'retirement_review': self.save(retired)}
        policy = self.bind_policy(review)
        unique = {ref['path']: ref for ref in policy['required_pins']}
        self.assertEqual(len(unique), 10)
        for removed in unique.values():
            with self.subTest(removed=removed['path']):
                incomplete = {**policy, 'required_pins': [ref for ref in policy['required_pins'] if ref != removed]}
                with self.assertRaisesRegex(core.IntegrityError, 'launcher_succession_proof_not_pinned'):
                    launch.validate_launcher_succession(incomplete, self.expected, self.control)
        self.assertFalse((self.control / 'LAUNCHER_SUCCESSION.json').exists())


class LauncherSuccessionLiveBoundaryReviewTests(unittest.TestCase):
    def setUp(self):
        # Reuse the author's synthetic pilot construction only. The mutations
        # and expected refusal points below are independent reviewer cases.
        self.fixture = integration_fixtures.LauncherSuccessionIntegrationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_proof_drift_after_locked_validation_holds_before_first_new_claim(self):
        policy, succession, finals = self.fixture.retired_pilot()
        self.fixture.bind_succession(policy, succession)
        original_queue = core.run_queue
        retirement_path = Path(succession['retirement_review']['path'])

        def alter_proof_then_run(*args, **kwargs):
            # This boundary is after the locked succession check and before
            # the unchanged queue's first admission/source check.
            self.assertTrue((self.fixture.f.output / '.control/LAUNCHER_SUCCESSION.json').is_file())
            retirement_path.write_bytes(retirement_path.read_bytes() + b' ')
            return original_queue(*args, **kwargs)

        with mock.patch.object(core, 'run_queue', side_effect=alter_proof_then_run), \
             mock.patch.object(runner, 'exchange', side_effect=AssertionError('unexpected new request')) as exchange:
            result = self.fixture.invoke(launch, policy, 'changed_retirement_proof')
        self.assertEqual(result['existing_final'], 2)
        self.assertEqual(result['new_final'], 0)
        self.assertEqual(result['remaining_unclaimed'], 1)
        self.assertIn('file_pin_mismatch', result['stop_reason'])
        self.assertEqual(exchange.call_count, 0)
        self.assertFalse((self.fixture.f.output / 'model_a/run2').exists())
        core.pinned(succession['predecessor_identity'])
        for ref in finals:
            core.pinned(ref)

    def test_a_new_ambiguous_suffix_still_cannot_be_replayed_after_retirement_review(self):
        policy, succession, finals = self.fixture.retired_pilot()
        self.fixture.bind_succession(policy, succession)
        prepared = json.loads(core.pinned(policy['prepared']))
        job = prepared['ready_jobs'][2]
        log = self.fixture.f.output / '.control/journal.jsonl'
        core.journal(log, {'event': 'claimed', 'measurement_id': job['measurement_id'],
                           'input_sha256': core.digest(job), 'phase': 'continue'})
        with mock.patch.object(runner, 'exchange', side_effect=AssertionError('ambiguous request replay')) as exchange:
            with self.assertRaisesRegex(core.AdmissionHold, 'partial_attempt_requires_manual_reconciliation'):
                self.fixture.invoke(launch, policy, 'ambiguous_suffix')
        self.assertEqual(exchange.call_count, 0)
        self.assertFalse((self.fixture.f.output / 'model_a/run2').exists())
        core.pinned(succession['predecessor_identity'])
        for ref in finals:
            core.pinned(ref)


if __name__ == '__main__':
    unittest.main()
