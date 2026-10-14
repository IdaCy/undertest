import copy
import json
import unittest

from causes import campaign_supplement_closed_union as union
import test_campaign_supplement_closed_union as previous_tests


class BankFixture(previous_tests.Fixture):
    """Only synthetic closed cuts; no projected closure is written to disk."""
    def __init__(self, family='candidate_saved_self_v1'):
        super().__init__()
        jobs = self.inputs['jobs']; mapping = {}
        for i, job in enumerate(jobs):
            job.update(family='requested_five_historical_self_v1' if i < 2 else family, model='mi27')
            mapping[job['measurement_id']] = '/'.join(job[k] for k in
                ('family', 'model', 'run_id', 'stage', 'question_id'))
        def rewrite(value):
            if isinstance(value, dict): return {k: rewrite(v) for k, v in value.items()}
            if isinstance(value, list): return [rewrite(v) for v in value]
            return mapping.get(value, value) if isinstance(value, str) else value
        self.inputs = rewrite(self.inputs); self.docs = rewrite(self.docs)
        for path, raw in list(self.raw.items()):
            if path.endswith('journal'):
                self.raw[path] = b''.join(union.encoded(rewrite(json.loads(line))) + b'\n'
                                          for line in raw.splitlines())
        jobs = self.inputs['jobs']; self.docs[self.jobs['path']] = copy.deepcopy(jobs)
        for index, row in enumerate(self.inputs['verified_finals']):
            row['input_canonical_sha256'] = union.digest(jobs[index])
        self.docs[self.old_rows['path']] = copy.deepcopy(self.inputs['verified_finals'][:2])
        self.docs[self.rows['path']] = copy.deepcopy(self.inputs['verified_finals'][2:4])
        self.previous_jobs = self.put('previous-full-jobs', jobs[:2])
        prior = self.docs[self.old['path']]
        prior.update(full_jobs=self.previous_jobs, unstarted_ids=[], clean_native_ending=True,
                     native_invocations=[{'legacy_invocation': 'unchanged'}])
        self.docs[self.old_plan['path']]['jobs'] = self.previous_jobs
        plan = self.docs[self.plan['path']]
        plan.update(family=family, model='mi27', maximum_new_requests=4,
                    jobs_sha256=union.digest(jobs[2:]))
        self.docs[self.result['path']]['result']['remaining_unclaimed'] = 2
        self.transfer = self.put('transfer', dict(schema='causes.original_supplement_reservation_transfer.v1',
            execution_predecessor=plan['predecessor_custody'], target_role='mi',
            target_output='/evidence/new', family=family, model='mi27', job_count=4,
            jobs_sha256=plan['jobs_sha256'], source_reservations=[{'saved_donor': 'fixture'}]))
        self.transfer_peer = self.put('transfer-peer', dict(
            accepted_original_supplement_reservation_transfer=True, actual=self.transfer, findings=[]))
        plan['original_reservation_transfer'] = dict(actual=self.transfer, review=self.transfer_peer)
        actual = self.docs[self.actual['path']]
        actual['original_bank_extension'] = dict(
            schema='causes.original_supplement_owner_bank_extension.v1',
            previous_closed=plan['predecessor_custody'], previous_full_jobs=self.previous_jobs,
            transfer=plan['original_reservation_transfer'], source_family=family, model='mi27',
            prior_count=2, appended_count=4, appended_jobs_sha256=union.digest(jobs[2:]),
            appended_ids_sha256=union.digest([j['measurement_id'] for j in jobs[2:]]),
            cumulative_count=6, cumulative_jobs_sha256=union.digest(jobs), full_jobs=self.jobs,
            prior_prefix_unchanged=True, native_families_preserved=True)
        actual['native_invocations'] = copy.deepcopy(prior['native_invocations']) + [dict(
            plan=self.plan, source=plan['sources']['driver'], result=self.result, pid=42,
            process_absent=True, family=family, model='mi27', jobs_sha256=plan['jobs_sha256'],
            original_reservation_transfer=plan['original_reservation_transfer'])]

    def extension(self):
        return union.original_bank_extension(self.get(self.actual), self.get(self.plan),
                                              self.get(self.old), self.get)

    def membership(self, normalized):
        data = super().membership(normalized)
        membership = self.docs[data['membership']['path']]
        for member, origin in zip(membership['rows'], normalized['origins']):
            bank = self.docs[origin['actual']['path']]['full_jobs']
            member['jobs'] = bank
            member['job_index'] = [j['measurement_id'] for j in self.get(bank)].index(member['measurement_id'])
        data['global_jobs_extension'] = self.put('global-jobs-extension', dict(
            schema='causes.supplement_global_jobs_extension.v1',
            previous_global_jobs=self.previous_jobs, previous_assignments=data['assignments'],
            global_jobs=data['global_jobs'], assignments=data['assignments'],
            closed_bank_extensions=[dict(actual=e['actual'], review=e['review'])
                                    for e in normalized['original_bank_extensions']]))
        return data


class OriginalBankExtensionTests(unittest.TestCase):
    def test_same_bank_is_still_strict_and_has_no_extension_metadata(self):
        f = previous_tests.Fixture(); before = copy.deepcopy(f.docs)
        result = f.normalize(); self.assertNotIn('original_bank_extensions', result)
        self.assertEqual(f.docs, before)
        f.docs[f.actual['path']]['full_jobs'] = f.put('unauthorized-bank', f.get(f.jobs))
        with self.assertRaisesRegex(ValueError, 'completed_actor_same_bank'): f.normalize()

    def test_typed_extension_retains_prefix_rows_and_per_invocation_origin(self):
        f = BankFixture(); before = copy.deepcopy(f.docs); result = f.normalize()
        self.assertEqual(result['rows'], f.get(f.old_rows) + f.get(f.rows))
        self.assertEqual([o['verified_index'] for o in result['origins']], [0, 1, 0, 1])
        extension = f.extension(); self.assertEqual(result['original_bank_extensions'], [dict(
            actual=f.actual, review=f.peer, extension=extension)])
        self.assertNotIn('original_bank_extension', result['origins'][0]['invocation'])
        self.assertEqual(result['origins'][2]['invocation']['original_bank_extension'], extension)
        self.assertEqual(f.docs, before)

    def test_entire_appended_bank_is_preserved_despite_partial_completion(self):
        f = BankFixture(); result = f.normalize(); extension = f.extension()
        self.assertEqual(extension['appended_count'], 4)
        self.assertEqual(len(result['rows']), 4)
        self.assertEqual(len(f.get(f.actual)['unstarted_ids']), 2)

    def test_same_family_original_transfer_is_supported(self):
        f = BankFixture('requested_five_historical_self_v1')
        self.assertEqual(f.extension()['source_family'], 'requested_five_historical_self_v1')
        self.assertEqual(len(f.normalize()['rows']), 4)

    def test_prefix_seed_order_and_unfinished_predecessor_are_rejected(self):
        for case in ('seed', 'order', 'unstarted', 'unresolved', 'not_clean'):
            f = BankFixture()
            if case == 'seed': f.docs[f.jobs['path']][0]['seed'] += 1
            if case == 'order': f.docs[f.jobs['path']][:2] = list(reversed(f.docs[f.jobs['path']][:2]))
            if case == 'unstarted': f.docs[f.old['path']]['unstarted_ids'] = ['not-finished']
            if case == 'unresolved': f.docs[f.old['path']]['unresolved_claimed_ids'] = ['unresolved']
            if case == 'not_clean': f.docs[f.old['path']]['clean_native_ending'] = False
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, 'original_bank_prefix'):
                f.extension()

    def test_exact_extension_roster_types_and_joins_are_required(self):
        for key, value in [('prior_count', True), ('appended_count', 3), ('cumulative_count', 5),
                           ('appended_jobs_sha256', 'wrong'), ('appended_ids_sha256', 'wrong'),
                           ('cumulative_jobs_sha256', 'wrong'), ('source_family', 'replacement'),
                           ('previous_full_jobs', {}), ('full_jobs', {}),
                           ('prior_prefix_unchanged', False), ('native_families_preserved', 1)]:
            f = BankFixture(); f.docs[f.actual['path']]['original_bank_extension'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): f.extension()
        f = BankFixture(); f.docs[f.actual['path']]['original_bank_extension']['extra'] = True
        with self.assertRaisesRegex(ValueError, 'extension_binding'): f.extension()

    def test_changed_appended_seed_cannot_be_requalified_by_new_local_hashes(self):
        f = BankFixture(); full = f.docs[f.jobs['path']]; full[2]['seed'] += 1
        ext = f.docs[f.actual['path']]['original_bank_extension']; plan = f.docs[f.plan['path']]
        ext['cumulative_jobs_sha256'] = union.digest(full)
        ext['appended_jobs_sha256'] = plan['jobs_sha256'] = union.digest(full[2:])
        with self.assertRaisesRegex(ValueError, 'original_bank_accepted_transfer'): f.extension()

    def test_wrong_transfer_peer_or_execution_scope_is_rejected(self):
        for field, value in [('accepted_original_supplement_reservation_transfer', False),
                             ('actual', {}), ('findings', ['unresolved'])]:
            f = BankFixture(); f.docs[f.transfer_peer['path']][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'accepted_transfer'): f.extension()
        for field, value in [('execution_predecessor', {}), ('target_output', '/other'),
                             ('family', 'other'), ('model', 'other'), ('job_count', True)]:
            f = BankFixture(); f.docs[f.transfer['path']][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'accepted_transfer'): f.extension()

    def test_new_bank_requires_original_transfer_and_appended_invocation(self):
        f = BankFixture(); del f.docs[f.plan['path']]['original_reservation_transfer']
        with self.assertRaisesRegex(ValueError, 'extension_binding'): f.extension()
        for case in ('old_prefix', 'family', 'jobs_digest', 'source', 'transfer', 'pid'):
            f = BankFixture(); rows = f.docs[f.actual['path']]['native_invocations']
            if case == 'old_prefix': rows[0]['legacy_invocation'] = 'changed'
            elif case == 'family': rows[-1]['family'] = 'wrong'
            elif case == 'jobs_digest': rows[-1]['jobs_sha256'] = 'wrong'
            elif case == 'source': rows[-1]['source'] = {}
            elif case == 'transfer': rows[-1]['original_reservation_transfer'] = {}
            else: rows[-1]['pid'] = True
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, 'original_bank_invocation'):
                f.extension()

    def test_global_append_retains_original_member_indices_and_native_failures(self):
        f = BankFixture(); f.invalid_admission()
        previous_tests.projection_tests.make_missing(f.inputs, index=3, parsed=True)
        f.docs[f.rows['path']][1] = copy.deepcopy(f.inputs['verified_finals'][3])
        f.docs[f.actual['path']]['journal'] = f.journal('new-failed-journal', range(2, 4))
        f.docs[f.actual['path']]['consecutive_failures'] = 1
        f.docs[f.result['path']]['result']['consecutive_failures'] = 1
        normalized = f.normalize(); data = f.membership(normalized)
        jobs, membership, assignments, rows, origins = union.validate_membership(data, normalized, f.get)
        self.assertEqual(jobs, f.get(f.jobs))
        self.assertEqual(assignments['epochs'], f.inputs['epochs'])
        self.assertEqual([m['job_index'] for m in membership['rows']], [0, 1, 2, 3])
        self.assertEqual([o['verified_index'] for o in origins], [0, 1, 0, 1])
        self.assertEqual(rows, normalized['rows'])
        self.assertEqual(rows[-1]['raw_parsed_label'], 'invalid')
        evidence = union.projected_evidence(data['membership'], membership, f.put('union-rows', rows), rows, origins)
        self.assertEqual(evidence[rows[-1]['measurement_id']]['custody']['invocation'][
            'original_bank_extension'], f.extension())
        invalid = {r['measurement_id']: o['admission_qualification'] for r, o in zip(rows, origins)
                   if 'admission_qualification' in o}
        for mid, proof in invalid.items(): evidence[mid]['invalid_admission'] = proof
        projected = previous_tests.projector.project_verified_supplement_pairs(
            jobs=jobs, epochs=assignments['epochs'], terminal_membership=membership['rows'],
            verified_finals=rows, occupied_rows=f.inputs['occupied_rows'],
            evidence_by_measurement=evidence, invalid_admissions=invalid)
        self.assertEqual(projected['counts'], dict(contexts=2, occupied=4, observed=2, missing=2))
        self.assertEqual(rows, normalized['rows'])

    def test_existing_global_donor_jobs_are_joined_without_duplicate_append(self):
        f = BankFixture('requested_five_historical_self_v1')
        normalized = f.normalize(); data = f.membership(normalized)
        f.docs[data['global_jobs_extension']['path']]['previous_global_jobs'] = data['global_jobs']
        self.assertEqual(union.validate_membership(data, normalized, f.get)[0], f.get(f.jobs))

    def test_global_descriptor_is_required_and_must_join_all_actual_proofs(self):
        for case in ('missing', 'schema', 'extra', 'jobs', 'assignments', 'previous_assignments',
                     'no_proofs', 'wrong_actual', 'wrong_peer', 'duplicate_proof'):
            f = BankFixture(); normalized = f.normalize(); data = f.membership(normalized)
            descriptor = f.docs[data['global_jobs_extension']['path']]
            if case == 'missing': del data['global_jobs_extension']
            elif case == 'schema': descriptor['schema'] = 'other'
            elif case == 'extra': descriptor['replacement_allowed'] = True
            elif case == 'jobs': descriptor['global_jobs'] = f.previous_jobs
            elif case in ('assignments', 'previous_assignments'): descriptor[case] = f.put('new-assignments', {})
            elif case == 'no_proofs': descriptor['closed_bank_extensions'] = []
            elif case == 'wrong_actual': descriptor['closed_bank_extensions'][0]['actual'] = f.old
            elif case == 'wrong_peer': descriptor['closed_bank_extensions'][0]['review'] = f.old_peer
            else: descriptor['closed_bank_extensions'] *= 2
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, 'global_jobs_extension'):
                union.validate_membership(data, normalized, f.get)

    def test_global_old_prefix_and_exact_append_cannot_change(self):
        for case in ('seed', 'prefix_order', 'new_order', 'drop_unstarted', 'extra_job'):
            f = BankFixture(); normalized = f.normalize(); data = f.membership(normalized)
            jobs = f.get(f.jobs)
            if case == 'seed': jobs[0]['seed'] += 1
            elif case == 'prefix_order': jobs[:2] = jobs[1::-1]
            elif case == 'new_order': jobs[2:] = list(reversed(jobs[2:]))
            elif case == 'drop_unstarted': jobs.pop()
            else:
                job = copy.deepcopy(jobs[-1]); job['measurement_id'] += '/extra'; jobs.append(job)
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, 'global_jobs_'):
                union.validate_global_jobs_extension(data, normalized, f.get, jobs)

    def test_global_extension_rechecks_accepted_peer_and_actual_not_marker_alone(self):
        for field, value in [('accepted_completed_actor_saved_closure', False),
                             ('accepted_finite_supplement_closed_custody', False),
                             ('original_denominator_preserved', False),
                             ('all_original_attempts_retained', False),
                             ('actual', {}), ('source', {}), ('findings', ['issue']),
                             ('qualification_findings', [{'lost': 'history'}])]:
            f = BankFixture(); normalized = f.normalize(); data = f.membership(normalized)
            f.docs[f.peer['path']][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'global_extension_closed_peer'):
                union.validate_membership(data, normalized, f.get)
        f = BankFixture(); normalized = f.normalize(); data = f.membership(normalized)
        f.docs[f.actual['path']]['original_bank_extension']['appended_count'] = 3
        with self.assertRaisesRegex(ValueError, 'global_extension_closed_peer'):
            union.validate_membership(data, normalized, f.get)
        f = BankFixture(); normalized = f.normalize(); data = f.membership(normalized)
        f.docs[f.transfer_peer['path']]['actual'] = f.old
        with self.assertRaisesRegex(ValueError, 'original_bank_accepted_transfer'):
            union.validate_membership(data, normalized, f.get)

    def test_global_existing_job_must_be_identical_and_no_physical_alias_added(self):
        for case in ('changed_existing', 'physical_alias'):
            f = BankFixture(); normalized = f.normalize(); data = f.membership(normalized)
            previous = f.get(f.previous_jobs); extra = copy.deepcopy(f.get(f.jobs)[2])
            if case == 'changed_existing': extra['seed'] += 1
            else:
                extra['family'] = 'requested_five_historical_self_v1'
                extra['measurement_id'] = '/'.join(extra[k] for k in
                    ('family', 'model', 'run_id', 'stage', 'question_id'))
            previous.append(extra)
            f.docs[data['global_jobs_extension']['path']]['previous_global_jobs'] = f.put('previous-global', previous)
            jobs = previous + [j for j in f.get(f.jobs)[2:] if j['measurement_id'] != extra['measurement_id']]
            reason = 'global_existing_job_changed' if case == 'changed_existing' else 'global_extension_physical_alias'
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, reason):
                union.validate_global_jobs_extension(data, normalized, f.get, jobs)

    def test_same_bank_rejects_unbound_global_extension(self):
        f = previous_tests.Fixture(); normalized = f.normalize(); data = f.membership(normalized)
        data['global_jobs_extension'] = f.put('unbound-global-extension', {})
        with self.assertRaisesRegex(ValueError, 'global_extension_without_closed_bank'):
            union.validate_membership(data, normalized, f.get)


if __name__ == '__main__': unittest.main()
