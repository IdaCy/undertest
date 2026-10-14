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


class ContinuationTests(unittest.TestCase):
    def fixture(self):
        documents = {}
        def put(name, value):
            raw = pipeline.encoded(value)
            ref = dict(path='/saved/' + name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            documents[ref['path']] = copy.deepcopy(value)
            return ref
        get = lambda ref: copy.deepcopy(documents[ref['path']])
        assignments = put('assignments.json', {'epochs': {'old': 'frozen', 'new': 'frozen'}})
        collector = put('collector.py', {})
        questions = ('evaluation_strongly_favors', 'evaluation_leans_towards_or_favors')
        members, verified, slots, evidence = [], [], [], {}
        for run in ('old', 'new'):
            for question in questions:
                mid = run + '/' + question
                final = put(mid + '/FINAL.json', {'status': 'saved'})
                value = dict(measurement_id=mid, final=final, capture_files=[],
                             request_body_sha256='request-' + mid, technical_status='complete')
                verified.append(value)
                members.append(dict(measurement_id=mid, run_id=run, readout='self_' + question))
                ev = dict(measurement_id=mid, final=final, request_sha256=value['request_body_sha256'],
                          native_collector=collector, verified_index=len(verified) - 1,
                          custody={'closed_run_id': run, 'terminal': True})
                evidence[mid] = ev
                missing = run == 'old' and question == questions[0]
                slots.append(dict(run_id=run, readout='self_' + question, status='missing' if missing else 'observed',
                    label=None if missing else {'evaluation': True}, reason='invalid_response' if missing else None,
                    evidence=copy.deepcopy(ev), epoch='frozen'))
        current_ref = put('cumulative_native.json', verified)
        for ev in evidence.values():
            ev['verified_finals'] = current_ref
        for slot in slots:
            slot['evidence']['verified_finals'] = current_ref
        original_ref = put('old_native_reversed.json', list(reversed(verified[:2])))
        originals = copy.deepcopy(slots[:2])
        for index, slot in enumerate(originals):
            slot['evidence']['verified_finals'] = original_ref
            slot['evidence']['verified_index'] = 1 - index
            slot['evidence']['custody']['older_cut'] = True
        slots_ref = put('old_slots.json', originals)
        result = put('old_result.json', dict(schema='causes.supplement_pair_pipeline_result.v1',
            mode='project', slots=slots_ref, assignments=assignments, occupied=2))
        peer = put('old_peer.json', dict(accepted_supplement_pair_verified_slots=True,
            actual=result, slots=slots_ref, assignments=assignments, findings=[]))
        entries = [dict(result=result, review=peer)]
        manifest = dict(segments=[dict(payload=slots_ref, physical_measurements=2)])
        presence = [dict(run_id=run, readout='self_evaluation_presence', status='observed',
                         label={'evaluation': False}, reason=None, epoch='frozen') for run in ('old', 'new')]
        occupied = copy.deepcopy(presence + originals)
        projection = dict(slots=slots, terminal_run_ids=['old', 'new'], existing_presence=presence,
            pending_incomplete_membership=[dict(measurement_id='pending/strong', run_id='pending')],
            counts=dict(contexts=2, occupied=4, observed=3, missing=1))
        args = dict(entries=entries, manifest=manifest, assignments=assignments, members=members,
                    verified=verified, occupied=occupied, get=get)
        return args, documents, projection, evidence

    def test_retains_exact_old_pair_and_missingness_with_changed_cumulative_indices(self):
        args, documents, projection, evidence = self.fixture()
        before = copy.deepcopy((args['occupied'], documents, projection, evidence))
        retained = pipeline.retained_pair_ledger(**args)
        result, mappings = pipeline.remove_verified_retained_pairs(projection, retained, evidence)
        self.assertEqual(result['terminal_run_ids'], ['new'])
        self.assertEqual(result['counts'], dict(contexts=1, occupied=2, observed=2, missing=0))
        self.assertEqual(result['pending_incomplete_membership'], projection['pending_incomplete_membership'])
        self.assertEqual([row['run_id'] for row in result['existing_presence']], ['new'])
        self.assertEqual(len(mappings), 2)
        self.assertNotEqual(mappings[0]['original_evidence']['verified_index'],
                            mappings[0]['cumulative_evidence']['verified_index'])
        self.assertEqual((args['occupied'], documents, projection, evidence), before)

    def test_old_pair_missing_or_unknown_ledger_is_rejected(self):
        for mutate in ('no_ledger', 'missing_manifest', 'removed_member', 'partial_occupied',
                       'changed_prior_slot', 'wrong_peer', 'duplicate_result'):
            with self.subTest(mutate=mutate):
                args, docs, _, _ = self.fixture()
                if mutate == 'no_ledger': args['entries'] = []
                elif mutate == 'missing_manifest': args['manifest']['segments'] = []
                elif mutate == 'removed_member': args['members'] = args['members'][1:]
                elif mutate == 'partial_occupied': args['occupied'].pop()
                elif mutate == 'changed_prior_slot': args['occupied'][-1]['reason'] = 'different'
                elif mutate == 'wrong_peer': docs[args['entries'][0]['review']['path']]['actual'] = {}
                else: args['entries'].append(copy.deepcopy(args['entries'][0]))
                with self.assertRaises(ValueError): pipeline.retained_pair_ledger(**args)

    def test_native_identity_or_captured_answer_change_is_rejected(self):
        for field in ('request_body_sha256', 'final', 'technical_status', 'capture_files'):
            with self.subTest(field=field):
                args, _, _, _ = self.fixture()
                args['verified'][0][field] = 'changed'
                with self.assertRaisesRegex(ValueError, 'prior_native_row_changed'):
                    pipeline.retained_pair_ledger(**args)

    def test_retained_labels_reasons_epochs_and_collector_must_match(self):
        for field in ('status', 'label', 'reason', 'epoch', 'native_collector'):
            with self.subTest(field=field):
                args, _, projection, evidence = self.fixture()
                retained = pipeline.retained_pair_ledger(**args)
                if field == 'native_collector': evidence[args['members'][0]['measurement_id']][field] = {}
                else: projection['slots'][0][field] = 'changed'
                with self.assertRaises(ValueError):
                    pipeline.remove_verified_retained_pairs(projection, retained, evidence)

    def test_incomplete_previously_imported_pair_is_rejected(self):
        args, _, projection, evidence = self.fixture()
        retained = pipeline.retained_pair_ledger(**args)
        projection['slots'] = projection['slots'][1:]
        with self.assertRaisesRegex(ValueError, 'retained_pair_not_complete'):
            pipeline.remove_verified_retained_pairs(projection, retained, evidence)

    def test_occupied_reader_preserves_full_supplement_only_when_requested(self):
        args, _, _, _ = self.fixture()
        rows = args['occupied']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'slots.json'
            path.write_text(json.dumps(rows))
            manifest = dict(segments=[dict(payload=pipeline.pin(path), physical_measurements=len(rows))],
                            physical_measurements=len(rows))
            default = pipeline.occupied_rows(dict(terminal_slots=len(rows)), manifest, {'old', 'new'}, lambda ref: None)
            kept = pipeline.occupied_rows(dict(terminal_slots=len(rows)), manifest, {'old', 'new'}, lambda ref: None,
                                          retain_supplements=True)
            self.assertEqual(kept, rows)
            self.assertEqual(default[-1], dict(run_id=rows[-1]['run_id'], readout=rows[-1]['readout']))


class SavedPairPipelineTests(unittest.TestCase):
    def lineage_fixture(self):
        data, documents, get, membership, actual, review = self.closed_fixture()
        def put(name, value):
            raw = pipeline.encoded(value)
            ref = dict(path='/saved/' + name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            documents[ref['path']] = copy.deepcopy(value)
            return ref
        top = documents[actual['path']]
        template = get(top['roles']['route'])
        all_rows = get(template['verified_finals'])
        template.update(schema='causes.finite_supplement_tranche_closed.v1', identity=put('identity.json', {}))
        base = dict(template, verified_finals=put('base-rows.json', all_rows[:1]), terminal=1,
                    complete_ids=[all_rows[0]['measurement_id']], new_finals_verified=1)
        base_ref = put('base.json', base)
        base_peer = put('base-peer.json', dict(accepted_bounded_finite_tranche_closed=True,
                                              actual=base_ref, findings=[]))
        zero = dict(base, schema='causes.finite_zero_claim_closed_extension.v1',
                    previous=base_ref, previous_review=base_peer, new_claims=0, new_finals_verified=0)
        zero_ref = put('zero.json', zero)
        zero_peer = put('zero-peer.json', dict(accepted_bounded_finite_tranche_closed=True,
                                              actual=zero_ref, findings=[]))
        delta = dict(template, verified_finals=put('delta-rows.json', all_rows[1:]),
                     new_finals_verified=1, prior_closed=zero_ref, prior_closed_review=zero_peer,
                     prior_verified_finals=zero['verified_finals'])
        delta_ref = put('delta.json', delta)
        delta_peer = put('delta-peer.json', dict(accepted_bounded_finite_tranche_closed=True,
                                               actual=delta_ref, findings=[]))
        final = dict(template, verified_finals=put('empty-rows.json', []), new_finals_verified=0,
                     prior_closed=delta_ref, prior_closed_review=delta_peer,
                     prior_verified_finals=delta['verified_finals'])
        final_ref = put('final.json', final)
        top['roles']['route'] = final_ref
        return data, documents, get, membership, actual, review, dict(
            base=base_ref, zero=zero_ref, delta=delta_ref, final=final_ref)

    def test_cumulative_delta_zero_and_empty_suffix_keep_original_native_indices(self):
        data, docs, get, membership, actual, review, refs = self.lineage_fixture()
        before = copy.deepcopy(docs)
        values, origins = pipeline.combine_verified(data, membership, get, actual, review,
            'accepted_closed', allow_closed_lineage=True)
        self.assertEqual([v['measurement_id'] for v in values], membership['measurement_ids'])
        self.assertEqual([o['actual'] for o in origins], [refs['delta'], refs['base']])
        self.assertEqual([o['verified_index'] for o in origins], [0, 0])
        self.assertEqual([len(o['closure_lineage']) for o in origins], [1, 3])
        for row, origin in zip(values, origins):
            self.assertEqual(row, get(origin['verified_finals'])[origin['verified_index']])
            self.assertEqual(row['technical_status'], 'request_failure')
        self.assertEqual(before, docs)

    def test_delta_closure_requires_explicit_continuation(self):
        data, _, get, membership, actual, review, _ = self.lineage_fixture()
        with self.assertRaisesRegex(ValueError, 'role_verification_partition'):
            pipeline.combine_verified(data, membership, get, actual, review, 'accepted_closed')

    def test_lineage_missing_reordered_duplicated_and_changed_rows_refuse(self):
        for mode in ('missing', 'duplicate', 'prefix', 'wrong_prior', 'extra_old', 'unqualified'):
            with self.subTest(mode=mode):
                data, docs, get, membership, actual, review, refs = self.lineage_fixture()
                delta, base = docs[refs['delta']['path']], docs[refs['base']['path']]
                if mode == 'missing': docs[delta['verified_finals']['path']] = []
                elif mode == 'duplicate':
                    docs[delta['verified_finals']['path']] = get(base['verified_finals'])
                elif mode == 'prefix': delta['complete_ids'].reverse()
                elif mode == 'wrong_prior': delta['prior_verified_finals'] = delta['verified_finals']
                elif mode == 'extra_old': docs[delta['verified_finals']['path']] += get(base['verified_finals'])
                else: docs[base['verified_finals']['path']][0]['qualification_error'] = 'kept'
                with self.assertRaises(ValueError):
                    pipeline.combine_verified(data, membership, get, actual, review,
                        'accepted_closed', allow_closed_lineage=True)

    def test_lineage_identity_status_and_accepted_peer_mutations_refuse(self):
        for scope, field, value in [('base', 'identity', {}), ('base', 'native_collector', {}),
                ('base', 'native_runner', {}), ('base', 'adapter', {}), ('base', 'role', 'other'),
                ('base', 'output', '/different'), ('base', 'active', True),
                ('base', 'terminal', True), ('base', 'full_tranche_qualified', False),
                ('base', 'qualification_findings', ['bad']), ('base', 'schema', 'unknown'),
                ('delta', 'new_finals_verified', True), ('zero', 'new_claims', 1),
                ('zero', 'new_finals_verified', True)]:
            with self.subTest(scope=scope, field=field):
                data, docs, get, membership, actual, review, refs = self.lineage_fixture()
                docs[refs[scope]['path']][field] = value
                with self.assertRaises(ValueError):
                    pipeline.combine_verified(data, membership, get, actual, review,
                        'accepted_closed', allow_closed_lineage=True)
        data, docs, get, membership, actual, review, refs = self.lineage_fixture()
        peer = docs[docs[refs['delta']['path']]['prior_closed_review']['path']]
        peer['accepted_bounded_finite_tranche_closed'] = False
        with self.assertRaises(ValueError):
            pipeline.combine_verified(data, membership, get, actual, review,
                'accepted_closed', allow_closed_lineage=True)

    def test_zero_claim_artifact_and_lineage_plan_identity_cannot_change(self):
        for mode in ('artifact', 'jobs', 'endpoint', 'output'):
            with self.subTest(mode=mode):
                data, docs, get, membership, actual, review, refs = self.lineage_fixture()
                if mode == 'artifact':
                    docs[refs['zero']['path']]['verified_finals'] = docs[refs['delta']['path']]['verified_finals']
                else:
                    plan = docs[docs[refs['base']['path']]['plan']['path']]
                    plan[{'jobs':'jobs','endpoint':'endpoint','output':'output_root'}[mode]] = 'changed'
                with self.assertRaises(ValueError):
                    pipeline.combine_verified(data, membership, get, actual, review,
                        'accepted_closed', allow_closed_lineage=True)

    def test_cyclic_ambiguous_or_absent_lineage_predecessors_refuse(self):
        for mode in ('cycle', 'ambiguous', 'absent'):
            with self.subTest(mode=mode):
                data, docs, get, membership, actual, review, refs = self.lineage_fixture()
                zero = docs[refs['zero']['path']]
                if mode == 'cycle':
                    zero['previous'] = refs['zero']
                    docs[zero['previous_review']['path']]['actual'] = refs['zero']
                elif mode == 'ambiguous': zero['prior_closed'] = refs['base']
                else: del zero['previous']
                with self.assertRaises(ValueError):
                    pipeline.combine_verified(data, membership, get, actual, review,
                        'accepted_closed', allow_closed_lineage=True)

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


class PartialQualificationTests(unittest.TestCase):
    def fixture(self):
        data, docs, get, members, actual, review, refs = SavedPairPipelineTests().lineage_fixture()
        delta = docs[refs['delta']['path']]
        mid = get(delta['verified_finals'])[0]['measurement_id']
        finding = dict(measurement_id=mid, stage='native_admission', error='stale')
        obs = dict(path='/saved/observation.json', bytes=2, sha256='a' * 64)
        docs[obs['path']] = {'saved': True}
        proofs = {}
        for name in ('delta', 'final'):
            role = docs[refs[name]['path']]
            role.update(full_tranche_qualified=False, qualification_findings=[finding],
                clean_native_ending=True, bounded_native_queue_exhausted=True, ambiguous=[],
                source=data['sources']['collector'])
            rows = get(role['verified_finals'])
            invalid = [mid] if name == 'delta' else []
            peer_ref = (docs[refs['final']['path']]['prior_closed_review'] if name == 'delta'
                        else dict(path='/saved/final-peer.json', bytes=0, sha256='b' * 64))
            peer = dict(accepted_finite_supplement_closed_custody=True,
                accepted_bounded_finite_tranche_closed=False, actual=refs[name], findings=[],
                source=role['source'], plan=role['plan'],
                verified_finals=role['verified_finals'], qualification_findings=[finding],
                qualified_admission_ids=[], invalid_admission_ids=invalid,
                invalid_admission_custody=[dict(measurement_id=mid, scientific_qualified=False,
                    request_replay_permitted=False, observation=obs, native_saved_observation=obs)]
                    if invalid else [])
            docs[peer_ref['path']] = peer
            proofs[refs[name]['path']] = peer_ref
        docs[refs['final']['path']].update(current_qualification_findings=[],
                                         inherited_qualification_findings=[finding])
        docs[actual['path']].update(all_qualified=False,
            qualification_findings=[dict(finding, role='route')])
        docs[review['path']]['accepted_finite_supplement_closed_custody'] = True
        return data, docs, get, members, actual, review, refs, proofs

    def call(self, fixture):
        data, docs, get, members, actual, review, refs, proofs = fixture
        return pipeline.combine_verified(data, members, get, actual, review,
            'accepted_finite_supplement_closed_custody', allow_closed_lineage=True,
            qualification_reviews=proofs)

    def test_invalid_native_row_is_literal_and_carries_exact_missing_proof(self):
        f = self.fixture(); before = copy.deepcopy(f[1]); rows, origins = self.call(f)
        self.assertEqual(f[1], before)
        invalid = [x for x in origins if x.get('admission_qualification', {}).get('qualified') is False]
        self.assertEqual(len(invalid), 1)
        proof = invalid[0]['admission_qualification']
        row = next(x for x in rows if x['measurement_id'] == proof['measurement_id'])
        self.assertEqual(row, f[2](proof['verified_finals'])[proof['verified_index']])
        self.assertEqual(proof['row_sha256'], hashlib.sha256(pipeline.encoded(row)).hexdigest())

    def test_default_mode_does_not_accept_partial_custody(self):
        data, docs, get, members, actual, review, refs, proofs = self.fixture()
        with self.assertRaisesRegex(ValueError, 'combined_closure_not_qualified'):
            pipeline.combine_verified(data, members, get, actual, review,
                'accepted_finite_supplement_closed_custody', allow_closed_lineage=True)

    def test_partition_and_accepted_proof_cannot_be_weakened(self):
        for mutation in ('missing_review', 'false_peer', 'wrong_actual', 'missing_invalid',
                         'invalid_as_qualified', 'duplicate', 'changed_observation', 'wrong_stage'):
            with self.subTest(mutation=mutation):
                f = self.fixture(); docs, refs, proofs = f[1], f[6], f[7]
                peer = docs[proofs[refs['delta']['path']]['path']]
                if mutation == 'missing_review': proofs.pop(refs['delta']['path'])
                elif mutation == 'false_peer': peer['accepted_finite_supplement_closed_custody'] = False
                elif mutation == 'wrong_actual': peer['actual'] = {}
                elif mutation == 'missing_invalid': peer['invalid_admission_ids'] = []
                elif mutation == 'invalid_as_qualified':
                    peer['qualified_admission_ids'] = peer['invalid_admission_ids']; peer['invalid_admission_ids'] = []
                elif mutation == 'duplicate': peer['invalid_admission_ids'] *= 2
                elif mutation == 'changed_observation':
                    peer['invalid_admission_custody'][0]['native_saved_observation'] = dict(
                        path='/saved/other.json', bytes=2, sha256='c' * 64)
                    docs['/saved/other.json'] = {'changed': True}
                else: peer['qualification_findings'][0]['stage'] = 'unrelated'
                with self.assertRaises(ValueError): self.call(f)

    def test_later_clean_suffix_retains_prior_invalid_and_imports_new_rows(self):
        f = self.fixture(); docs, get, refs, proofs = f[1], f[2], f[6], f[7]
        final = docs[refs['final']['path']]
        new = copy.deepcopy(get(docs[refs['delta']['path']]['verified_finals'])[0])
        new['measurement_id'] = 'new/clean'
        docs[final['verified_finals']['path']] = [new]
        final['complete_ids'] = final['complete_ids'] + ['new/clean']
        final.update(terminal=3, new_finals_verified=1)
        docs[proofs[refs['final']['path']]['path']]['qualified_admission_ids'] = ['new/clean']
        part = f[0]['parts'][0]
        rows, origins = pipeline.closed_lineage_rows(refs['final'], part, f[0]['sources'],
            final['identity'], get, qualification_reviews=proofs)
        self.assertEqual(len(rows), 3)
        self.assertTrue(origins[-1]['admission_qualification']['qualified'])
        self.assertEqual(sum(o.get('admission_qualification', {}).get('qualified') is False
                             for o in origins), 1)

    def test_cumulative_finding_cannot_be_removed_or_reassigned(self):
        for field in ('inherited_qualification_findings', 'qualification_findings'):
            f = self.fixture(); f[1][f[6]['final']['path']][field] = []
            with self.assertRaises(ValueError): self.call(f)

    def test_partial_scope_requires_clean_closed_run_and_exact_audit_source(self):
        for field, value in [('clean_native_ending', False), ('bounded_native_queue_exhausted', False),
                             ('ambiguous', ['unknown']), ('active', 1), ('source', {})]:
            f = self.fixture(); f[1][f[6]['delta']['path']][field] = value
            with self.assertRaises(ValueError): self.call(f)

    def test_descendant_cannot_strip_inherited_findings_or_promote_whole_flag(self):
        for promote in (False, True):
            with self.subTest(promote=promote):
                f = self.fixture(); docs, refs, proofs = f[1], f[6], f[7]
                final = docs[refs['final']['path']]
                final.pop('current_qualification_findings')
                final.pop('inherited_qualification_findings')
                final['qualification_findings'] = []
                final['full_tranche_qualified'] = promote
                docs[proofs[refs['final']['path']]['path']]['qualification_findings'] = []
                docs[f[4]['path']]['qualification_findings'] = []
                with self.assertRaisesRegex(ValueError, 'qualification_lineage_findings'):
                    self.call(f)

    def test_false_full_promotion_with_explicit_inherited_findings_refuses(self):
        f = self.fixture(); f[1][f[6]['final']['path']]['full_tranche_qualified'] = True
        with self.assertRaises(ValueError): self.call(f)

    def test_partial_zero_claim_extension_is_explicitly_unsupported(self):
        f = self.fixture()
        f[1][f[6]['final']['path']]['schema'] = 'causes.finite_zero_claim_closed_extension.v1'
        with self.assertRaisesRegex(ValueError, 'partial_zero_claim_extension_unsupported'):
            self.call(f)


if __name__ == '__main__':
    unittest.main()
