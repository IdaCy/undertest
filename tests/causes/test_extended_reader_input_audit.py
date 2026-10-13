import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from causes import campaign_reader_input_audit as audit


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def payload_identity(payload, encode):
    body = encode(payload)
    return dict(canonical_sha256=digest(payload), body_bytes=len(body),
                file_sha256=hashlib.sha256(body).hexdigest())


class ExtendedInputAuditTests(unittest.TestCase):
    """Exercise the adapter with pinned files and deterministic native collaborators.

    Native snapshot/context algorithms have separate tests. These fixtures expose
    their outputs while exercising all new custody, row-preservation and receipt
    comparisons, without running a materializer or a reader.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.file('extension.py', b'# reviewed fixture source\n')
        self.reader_source = self.file('reader.py', b'# frozen reader fixture\n')
        self.context = {'messages': ['original actor context']}
        self.targets = {'0': {'content': 'original actor context'}}
        self.payload = {'store': False, 'input': [dict(role='system', content='rubric'),
            dict(role='user', content=encoded(self.context).decode())]}
        self.payloads = {instrument: dict(payload_identity(self.payload, encoded), pins={'instrument': instrument})
                         for instrument in ('presence', 'strong', 'lean')}
        self.base = dict(run_id='base', measurement_key='base-key', disposition='prepared', value=False)
        self.base_missing = dict(run_id='base-missing', measurement_key='base-missing-key', disposition='unavailable')
        self.prior_receipt = {'run': 'prior', 'manifest_sha256': 'dated-prior'}
        self.prior = dict(run_id='prior', measurement_key='prior-key', disposition='prepared',
                          original_binding={'snapshot_receipt': self.prior_receipt}, payload={'count': 1})
        self.jobs = {name: {'job': {'allocation': name}} for name in ('prior', 'new', 'missing', 'pending')}
        self.new = dict(run_id='new', measurement_key='new-key', stage='early', disposition='prepared',
            native_job_sha256=digest(self.jobs['new']['job']), judge_context_sha256=digest(self.context),
            targets_sha256=digest(self.targets), payloads=self.payloads)
        self.missing = dict(run_id='missing', measurement_key='missing-key', stage='early',
            disposition='unavailable', native_job_sha256=digest(self.jobs['missing']['job']),
            reason='required_stage_or_initial_context_not_captured', closed_native_snapshot=True)
        self.pending_rows = [dict(run_id='pending', measurement_key='pending-key', stage='early',
            disposition='pending', reason='no_accepted_reader_input_copy_at_cut')]
        self.copied = {'prior': {'receipt': self.prior_receipt}}
        for name in ('new', 'missing'):
            manifest = self.file(name + '/MANIFEST.json', encoded({'native': name}))
            receipt = dict(run=name, manifest=manifest['path'], manifest_sha256=manifest['sha256'])
            self.save(name + '/VERIFIED.json', dict(schema='caus.reader_input_verified.v1', runs=[receipt]))
            self.copied[name] = dict(receipt=receipt, role='provider', fixture_run=name)
        self.cohort = self.save('cohort.json', {'fixed': ['base', 'base-missing', *self.jobs]})
        self.known = self.save('known.json', {'reader_source': self.reader_source})
        previous = dict(source=self.source, cohort=self.cohort,
            ready=self.save('old-ready.json', [self.base, self.prior]),
            unavailable=self.save('old-missing.json', [self.base_missing]),
            pending=self.save('old-pending.json', []), self_jobs=self.save('old-self.json', []),
            reader_source=self.reader_source)
        self.previous_ref = self.save('previous-preparation.json', previous)
        self.previous_peer = self.save('previous-review.json', dict(previous,
            preparation=self.previous_ref, accepted_fresh_candidate_reader_inputs=True, findings=[]))
        self.previous_plan = self.save('previous-plan.json', dict(prepared=self.previous_ref,
            prepared_review=self.previous_peer, source=self.source, cohort=self.cohort,
            expected_unclaimed_contexts=1, previous_cohort_claimed_keys=['base-key']))
        closed = dict(plan=self.previous_plan, reader_source=self.source, root='/fixture/closed',
            journal=self.save('closed-journal.json', {}), runtime_result=self.save('closed-result.json', {}),
            all_claims_accounted=True, active=0, unclaimed=0, quarantine=None, stop_reason=None,
            owned_pause=None, observed_at='2026-01-01T00:00:00+00:00', ready=1, claimed=1,
            finished_or_unresolved=1, claimed_keys=['prior-key'], final_consecutive_failed_contexts=1)
        self.closed_ref = self.save('closed.json', closed)
        self.closed_peer = self.save('closed-review.json', dict(
            {key: closed[key] for key in ('plan', 'reader_source', 'root', 'journal', 'runtime_result')},
            accepted_finite_reader_route_closed_cut=True, actual=self.closed_ref, findings=[],
            at='2026-01-01T00:00:01+00:00'))
        self.binding = dict(source=self.source, cohort=self.cohort, known=self.known,
            row_materializer=self.file('materializer.py', b'# native materializer fixture\n'),
            previous_plan=self.previous_plan, previous_closed=self.closed_ref,
            previous_closed_review=self.closed_peer)
        self.binding_ref = self.save('binding.json', self.binding)
        binding_review = self.save('binding-review.json', {'accepted': True})
        self.request = dict(schema='causes.extended_reader_materialization_request.v1', source=self.source,
            binding=self.binding_ref, binding_review=binding_review, root_authorized=True,
            previous_preparation=self.previous_ref, previous_review=self.previous_peer,
            feed_cut=self.save('feed-cut.json', {}), feed_cut_review=self.save('feed-review.json', {}),
            output=str(self.root / 'output'), producers=[{'role': 'provider'}])
        pending = dict(self.request, root_authorized=False, feed_cut=None, feed_cut_review=None)
        self.request_ref = self.save('request.json', self.request)
        self.pending_ref = self.save('pending-request.json', pending)
        self.prep = dict(schema='causes.fresh_candidate_reader_inputs.v1', source=self.source,
            cohort_extension=self.binding_ref, cohort_extension_review=binding_review,
            feed_cut=self.request['feed_cut'], feed_cut_review=self.request['feed_cut_review'],
            previous_preparation=self.previous_ref, previous_review=self.previous_peer,
            cohort=self.cohort, known=self.known, row_materializer=self.binding['row_materializer'],
            ready=self.save('ready.json', [self.base, self.prior, self.new]),
            unavailable=self.save('missing.json', [self.base_missing, self.missing]),
            pending=self.save('pending.json', self.pending_rows), self_jobs=self.save('self.json', []),
            reader_source=self.reader_source, checker_config=self.save('config.json', {}),
            prompts={'rubric': self.file('rubric.txt', b'fixed rubric')}, prompts_directory=str(self.root),
            counts={'copied': 5}, runtime_authorized=False, model_calls=0,
            original_allocations_unchanged=True, treatments_preserved=True,
            payloads_contain_no_analysis_labels=True, pending_is_not_missing=True,
            cumulative_ready_requires_closed_history_filter=True)
        self.prep_ref = self.save('output/PREPARATION.v1.json', self.prep)
        authority = dict(action='prepare_extended_reader_inputs', root_authorized=True, automatic_retry=False,
            request=self.request_ref, source=self.source, output=self.request['output'],
            issued_at='2026-01-01T00:00:02+00:00', expires_at='2026-01-01T00:03:02+00:00')
        self.authority_ref = self.save('authority.json', authority)
        self.save('output/INTENT.v1.json', dict(request=self.request_ref, authority=self.authority_ref))
        argv = [sys.executable, '-B', self.source['path'], '--prepare-request', self.request_ref['path'],
                '--prepare-authority', self.authority_ref['path']]
        root_intent = dict(request=self.request_ref, authority=self.authority_ref, source=self.source,
            argv=argv, maximum_invocations=1, automatic_retry=False)
        execution = dict(root_intent, intent=self.save('execution/INTENT.v1.json', root_intent),
            returncode=0, error=None, source_after=self.source,
            stderr=self.file('execution/stderr', b''), stdout=self.save('execution/stdout', self.prep_ref),
            host_calls=0, model_calls=0, started_at='2026-01-01T00:00:03+00:00',
            finished_at='2026-01-01T00:00:04+00:00')
        self.settings = dict(schema=audit.EXTENDED_SCHEMA, interpreter=sys.executable,
            request=self.request_ref, pending_request=self.pending_ref, preparation=self.prep_ref,
            execution=self.save('execution/EXECUTION.v1.json', execution),
            source_review=self.save('source-review.json', dict(accepted_reader_cohort_extension_source=True,
                source=self.source, findings=[])), previous_plan=self.previous_plan,
            previous_preparation=self.previous_ref, previous_review=self.previous_peer,
            closed_actual=self.closed_ref, closed_review=self.closed_peer)
        reader = SimpleNamespace(__file__=self.reader_source['path'], encoded=encoded, digest=digest,
            prepare_context=lambda *args: (self.context, self.targets),
            make_payload=lambda config, prompts, context, instrument: (self.payload, {'instrument': instrument}))
        native = SimpleNamespace(bind_row=lambda feed, known, modules: copy.deepcopy(
            {'new': self.new, 'missing': self.missing}[feed['fixture_run']]),
            parent=lambda: SimpleNamespace(payload_identity=payload_identity))
        self.extension = SimpleNamespace(
            runtime_namespace=lambda *args: {'native_modules': lambda: {'campaign_reader': reader}},
            cohort=lambda binding: ({}, [self.base], [self.base_missing], {}, self.jobs),
            validate_cumulative=lambda binding, prep, previous: tuple(audit.ck(prep[k]) for k in ('ready', 'unavailable', 'pending')),
            copied_rows=lambda *args: self.copied,
            unique=lambda rows, field, reason: {row[field]: row for row in rows},
            module=lambda ref: native, measurement_key=lambda run: run + '-key',
            prepare_reader=lambda *args: dict(phase='prefix', prefix_context=None, messages=[], initial_context=[]),
            READERS=('presence', 'strong', 'lean'))

    def file(self, name, raw):
        path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        return audit.pin(path)

    def save(self, name, value):
        return self.file(name, encoded(value))

    def run_review(self):
        with patch.object(audit, 'module', return_value=self.extension):
            return audit.review_saved_inputs(self.settings)

    def change_preparation(self, field, value):
        self.prep[field] = value
        self.prep_ref = self.save('output/PREPARATION.v1.json', self.prep)
        self.settings['preparation'] = self.prep_ref
        execution = audit.ck(self.settings['execution'])
        execution['stdout'] = self.save('execution/stdout', self.prep_ref)
        self.settings['execution'] = self.save('execution/EXECUTION.v1.json', execution)

    def test_complete_saved_adapter_preserves_failed_rows_and_reconstructs_only_new_payloads(self):
        result = self.run_review()
        self.assertTrue(result['accepted_extended_reader_inputs'])
        self.assertTrue(result['accepted_fresh_candidate_reader_inputs'])
        self.assertEqual(result['preserved_previous_counts'], {'ready': 2, 'unavailable': 1})
        self.assertEqual(result['prepared_payloads_reconstructed'], 3)
        self.assertEqual(result['preceding_closed_actual'], self.closed_ref)
        self.assertEqual(result['new_role_counts'], [dict(role='provider', disposition='prepared', count=1),
                                                  dict(role='provider', disposition='unavailable', count=1)])

    def test_removed_prior_row_is_rejected(self):
        self.change_preparation('ready', self.save('ready.json', [self.base, self.new]))
        with self.assertRaisesRegex(ValueError, 'previous_row_removed_or_changed'):
            self.run_review()

    def test_relabelled_missing_row_is_rejected(self):
        self.change_preparation('unavailable', self.save('missing.json', [self.missing]))
        with self.assertRaisesRegex(ValueError, 'previous_row_removed_or_changed'):
            self.run_review()

    def test_boolean_integer_change_in_prior_row_is_rejected(self):
        changed = dict(self.base, value=0)
        self.change_preparation('ready', self.save('ready.json', [changed, self.prior, self.new]))
        with self.assertRaisesRegex(ValueError, 'previous_row_removed_or_changed'):
            self.run_review()

    def test_previous_copy_removal_and_receipt_change_are_rejected(self):
        for value, message in ((None, 'previous_copy_removed'), ({'run': 'prior', 'manifest_sha256': 'new'}, 'previous_copy_changed')):
            with self.subTest(value=value):
                if value is None:
                    del self.copied['prior']
                else:
                    self.copied['prior'] = {'receipt': value}
                with self.assertRaisesRegex(ValueError, message):
                    self.run_review()

    def test_changed_native_payload_and_target_custody_are_rejected(self):
        changed = copy.deepcopy(self.new); changed['payloads']['presence']['body_bytes'] += 1
        self.change_preparation('ready', self.save('ready.json', [self.base, self.prior, changed]))
        with self.assertRaisesRegex(ValueError, 'reconstructed_rows_changed'):
            self.run_review()

    def test_wrong_predecessor_ref_is_rejected_before_native_modules(self):
        self.settings['closed_actual'] = self.save('different-closed.json', audit.ck(self.closed_ref))
        with patch.object(audit, 'module') as load:
            with self.assertRaisesRegex(ValueError, 'latest_predecessor'):
                audit.review_saved_inputs(self.settings)
            load.assert_not_called()

    def test_changed_authority_window_is_rejected(self):
        authority = audit.ck(self.authority_ref); authority['expires_at'] = '2026-01-01T00:03:03+00:00'
        authority_ref = self.save('authority-long.json', authority)
        self.save('output/INTENT.v1.json', dict(request=self.request_ref, authority=authority_ref))
        # A rehashed wrapper cannot conceal the changed authority or its argv.
        with self.assertRaisesRegex(ValueError, 'extended_execution_join'):
            self.run_review()

    def test_unknown_descriptor_field_and_stale_pin_are_rejected(self):
        self.settings['skip_old_rows'] = True
        with self.assertRaisesRegex(ValueError, 'exact_extended_descriptor'):
            self.run_review()
        del self.settings['skip_old_rows']
        self.settings['closed_review'] = dict(self.closed_peer, sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'saved_pin_changed'):
            self.run_review()

    def test_only_cut_and_authority_fields_may_change_in_request(self):
        pending = audit.ck(self.pending_ref)
        for mutation in ('producers', 'previous', 'boolean_integer'):
            current = copy.deepcopy(self.request)
            if mutation == 'producers': current['producers'] = []
            elif mutation == 'previous': current['previous_preparation'] = self.cohort
            else: current['producers'][0]['enabled'] = 1; pending['producers'][0]['enabled'] = True
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'changed_beyond_cut'):
                audit.extended_request_binding(current, pending)


if __name__ == '__main__':
    unittest.main()
