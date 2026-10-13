import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_supplement_pair_pipeline as pipeline


def fixture():
    documents = {}
    def put(name, value):
        raw = pipeline.encoded(value)
        ref = dict(path='/saved/' + name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        documents[ref['path']] = copy.deepcopy(value)
        return ref
    source = put('caller.py', {})
    context = put('context.json', [])
    config = put('config.json', {'endpoint': 'original'})
    original = {'context': context}
    jobs = [dict(measurement_id='run/' + q, run_id='run', stage='early', question_id=q,
                 family='family', model='model', seed=7, context=context,
                 frozen_config=config, original_refs=original)
            for q in ('evaluation_strongly_favors', 'evaluation_leans_towards_or_favors')]
    global_ref = put('global.json', jobs)
    jobs_ref = put('jobs.json', jobs)
    constructor = put('constructor.json', {})
    plan = dict(source=source, jobs=jobs_ref, constructor=constructor,
                endpoint='http://offline.test', output_root='/original')
    plan_ref = put('plan.json', plan)
    plan_review = put('plan-peer.json', dict(accepted_plan=True, source=source,
                                           plan=plan_ref, findings=[]))
    journal = put('journal.jsonl', [])
    rows, terms = [], []
    for index, job in enumerate(jobs):
        final = put(str(index) + '/FINAL.json', {})
        terminal = dict(measurement_id=job['measurement_id'], final=final,
                        event='route_success', technical_status='complete')
        terms.append(terminal)
        rows.append(dict(**{key: job[key] for key in
            ('measurement_id', 'run_id', 'stage', 'question_id', 'context', 'frozen_config', 'original_refs')},
            role='route', jobs=jobs_ref, endpoint=plan['endpoint'], terminal=terminal,
            final=final, capture_files=[], global_job_ordinal=index, job_index=index,
            global_job_sha256=hashlib.sha256(pipeline.encoded(job)).hexdigest(),
            input=put(str(index) + '/INPUT.json', job), epoch='frozen', readout='self_' + job['question_id']))
    cut = put('cut.json', dict(roles={'route': dict(plan=plan_ref, jobs=jobs_ref,
        journal_prefix=journal, terminal_events=terms)}))
    cut_peer = put('cut-peer.json', dict(accepted_cut=True, actual=cut, findings=[]))
    assignments = put('assignments.json', dict(epochs={'run': 'frozen'}))
    assignment_review = put('assignment-peer.json', dict(accepted_assignments=True,
        assignments=assignments, findings=[]))
    membership = put('membership.json', dict(global_jobs=global_ref, assignments=assignments,
        rows=rows, measurement_ids=[row['measurement_id'] for row in rows], active_rows=[]))
    data = dict(global_jobs=global_ref, membership=membership, assignments=assignments,
        assignment_review=assignment_review, assignment_acceptance_flag='accepted_assignments',
        sources={'collector': source}, parts=[dict(role='route', plan=plan_ref,
        plan_review=plan_review, plan_acceptance_flag='accepted_plan', jobs=jobs_ref,
        constructor=constructor, endpoint=plan['endpoint'], output_root=plan['output_root'],
        cut=cut, cut_review=cut_peer, cut_acceptance_flag='accepted_cut', journal=journal,
        terminal_count=2)])
    return data, documents, lambda ref: copy.deepcopy(documents[ref['path']])


class SavedPairPipelineTests(unittest.TestCase):
    def test_complete_metadata_and_custom_acceptance_flags(self):
        data, documents, get = fixture()
        before = copy.deepcopy((data, documents))
        jobs, membership, assignments, parts = pipeline.validate_membership(data, get)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(membership['measurement_ids'], [row['measurement_id'] for row in jobs])
        self.assertEqual((data, documents), before)

    def closed_fixture(self):
        data, documents, get = fixture()
        def put(name, value):
            raw = pipeline.encoded(value)
            ref = dict(path='/saved/' + name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            documents[ref['path']] = copy.deepcopy(value)
            return ref
        source = data['sources']['collector']
        data['sources'].update(runner=source, adapter=source)
        membership = get(data['membership'])
        values = [dict(measurement_id=row['measurement_id'], final=row['final'], capture_files=[],
                       technical_status='request_failure') for row in reversed(membership['rows'])]
        original = put('verified.json', values)
        part = data['parts'][0]
        role = put('closed-role.json', dict(full_tranche_qualified=True, qualification_findings=[],
            role='route', plan=part['plan'], output=part['output_root'], active=0,
            native_collector=source, native_runner=source, adapter=source, verified_finals=original,
            terminal=len(values), complete_ids=[row['measurement_id'] for row in values]))
        actual = put('closure.json', dict(all_qualified=True, qualification_findings=[], active=0,
            roles={'route': role}, terminal=len(values)))
        review = put('closure-peer.json', dict(accepted_closed=True, actual=actual, findings=[]))
        return data, documents, get, membership, actual, review

    def test_accepted_rows_reordered_with_exact_original_indices_and_failures(self):
        data, documents, get, membership, actual, review = self.closed_fixture()
        original = copy.deepcopy(documents)
        values, origins = pipeline.combine_verified(data, membership, get, actual, review, 'accepted_closed')
        self.assertEqual([row['measurement_id'] for row in values], membership['measurement_ids'])
        self.assertEqual([row['verified_index'] for row in origins], [1, 0])
        self.assertEqual([row['technical_status'] for row in values], ['request_failure'] * 2)
        self.assertEqual(original, documents)
        for row, origin in zip(values, origins):
            self.assertEqual(row, get(origin['verified_finals'])[origin['verified_index']])

    def test_closed_custody_mutations_refuse(self):
        cases = [('combined', 'terminal', True), ('combined', 'all_qualified', False),
                 ('combined', 'qualification_findings', ['refused']), ('role', 'terminal', True),
                 ('role', 'plan', {}), ('role', 'native_collector', {}), ('role', 'native_runner', {}),
                 ('role', 'adapter', {}), ('role', 'active', True), ('role', 'complete_ids', ['foreign'])]
        for scope, field, value in cases:
            with self.subTest(scope=scope, field=field):
                data, documents, get, membership, actual, review = self.closed_fixture()
                target = documents[actual['path']]
                if scope == 'role':
                    target = documents[target['roles']['route']['path']]
                target[field] = value
                with self.assertRaises(ValueError):
                    pipeline.combine_verified(data, membership, get, actual, review, 'accepted_closed')

    def test_closed_native_rows_missing_duplicate_changed_or_unqualified_refuse(self):
        for mode in ('missing', 'duplicate', 'final', 'capture', 'error'):
            with self.subTest(mode=mode):
                data, documents, get, membership, actual, review = self.closed_fixture()
                role = documents[get(actual)['roles']['route']['path']]
                rows = documents[role['verified_finals']['path']]
                if mode == 'missing':
                    rows.pop()
                elif mode == 'duplicate':
                    rows[1] = copy.deepcopy(rows[0])
                elif mode == 'final':
                    rows[0]['final'] = {}
                elif mode == 'capture':
                    rows[0]['capture_files'] = [{}]
                else:
                    rows[0]['qualification_error'] = 'preserved refusal'
                with self.assertRaises(ValueError):
                    pipeline.combine_verified(data, membership, get, actual, review, 'accepted_closed')

    def test_output_limit_refuses_before_oversized_partial_write(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = pipeline.encoded({'first': True}) + b'\n'
            writer = pipeline.LimitedWriter(directory, len(raw))
            first = writer.save('first.json', {'first': True})
            with self.assertRaisesRegex(ValueError, 'output_byte_limit'):
                writer.save('second.json', {'second': True})
            self.assertFalse((Path(directory) / 'second.json').exists())
            self.assertEqual(pipeline.pin(first['path']), first)
            self.assertEqual(writer.written, len(raw))

    def test_metadata_identity_mutations_refuse(self):
        for field, value in [('global_job_ordinal', True), ('job_index', -1),
                             ('epoch', 'other'), ('role', 'other'), ('endpoint', 'changed'),
                             ('global_job_sha256', '0' * 64), ('readout', 'self_evaluation_presence')]:
            with self.subTest(field=field):
                data, documents, get = fixture()
                documents[data['membership']['path']]['rows'][0][field] = value
                with self.assertRaises((ValueError, KeyError)):
                    pipeline.validate_membership(data, get)

    def test_missing_and_extra_terminal_cannot_silently_disappear(self):
        for mode in ('missing', 'duplicate', 'extra'):
            data, documents, get = fixture()
            saved = documents[data['parts'][0]['cut']['path']]['roles']['route']['terminal_events']
            if mode == 'missing':
                saved.pop()
            elif mode == 'duplicate':
                saved.append(copy.deepcopy(saved[0]))
            else:
                saved.append(dict(saved[0], measurement_id='foreign'))
            with self.assertRaises(ValueError):
                pipeline.validate_membership(data, get)

    def test_previous_live_claim_is_not_a_terminal(self):
        data, documents, get = fixture()
        member = documents[data['membership']['path']]
        member['active_rows'] = [{'measurement_id': member['measurement_ids'][0]}]
        with self.assertRaisesRegex(ValueError, 'active_is_not_terminal'):
            pipeline.validate_membership(data, get)

    def test_changed_global_input_rejected_without_native_verifier(self):
        data, documents, get = fixture()
        input_ref = documents[data['membership']['path']]['rows'][0]['input']
        documents[input_ref['path']]['seed'] = True
        with self.assertRaisesRegex(ValueError, 'input_epoch_identity'):
            pipeline.validate_membership(data, get)

    def test_common_artifact_and_exact_global_row_indices(self):
        data, documents, get = fixture()
        membership = get(data['membership'])
        values = [dict(measurement_id=row['measurement_id'], final=row['final'],
                       request_body_sha256=str(index)) for index, row in enumerate(membership['rows'])]
        reference = dict(path='/saved/combined.json', bytes=100, sha256='f' * 64)
        evidence = pipeline.projected_evidence(data, membership, reference, values)
        self.assertEqual([row['verified_index'] for row in evidence.values()], [0, 1])
        self.assertTrue(all(row['verified_finals'] == reference for row in evidence.values()))
        next(iter(evidence.values()))['verified_finals']['bytes'] = 1
        self.assertEqual(reference['bytes'], 100)
        with self.assertRaisesRegex(ValueError, 'combined_verification_row'):
            pipeline.projected_evidence(data, membership, reference, list(reversed(values)))

    def test_occupied_index_retains_literal_missing_presence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'slots.json'
            presence = dict(run_id='run', readout='self_evaluation_presence', status='missing',
                            label=None, reason='original', evidence={'immutable': True}, epoch='frozen')
            rows = [presence, dict(run_id='other', readout='reader_presence', status='observed')]
            path.write_text(json.dumps(rows))
            reference = pipeline.pin(path)
            manifest = dict(physical_measurements=2, segments=[dict(payload=reference,
                                                                   physical_measurements=2)])
            result = pipeline.occupied_rows(dict(terminal_slots=2), manifest, {'run'},
                                            lambda ref: self.assertEqual(pipeline.pin(ref['path']), ref))
            self.assertEqual(result, [presence, dict(run_id='other', readout='reader_presence')])
            path.write_text(json.dumps([presence, presence]))
            manifest['segments'][0]['payload'] = pipeline.pin(path)
            with self.assertRaisesRegex(ValueError, 'duplicate_previous_slot'):
                pipeline.occupied_rows(dict(terminal_slots=2), manifest, {'run'}, lambda ref: None)

    def test_no_effects_policy(self):
        for event in ('socket.connect', 'subprocess.Popen', 'os.exec', 'os.posix_spawn', 'os.kill'):
            with self.assertRaisesRegex(ValueError, 'offline_only'):
                pipeline.deny_effects(event, ())


if __name__ == '__main__':
    unittest.main()
