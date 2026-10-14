import copy
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_supplement as core
from causes import campaign_supplement_completed_actor as direct


class TransferFixture:
    """Synthetic repository-only custody; no process, network or private fixtures."""

    def __init__(self, root, historical=False):
        self.root = root
        self.n = 0
        self.family = ('candidate_saved_self_v1' if historical
                       else 'requested_five_historical_self_v1')
        owner_jobs = [self.job('owner', 0, family='requested_five_historical_self_v1')]
        self.previous = dict(active=0, ambiguous=[], clean_native_ending=True,
                             consecutive_failures=0, unstarted_ids=[],
                             complete_ids=[owner_jobs[0]['measurement_id']],
                             full_jobs=self.save(owner_jobs))
        self.owner = self.peer(self.previous, 'accepted_finite_supplement_closed_custody')
        self.plan = dict(predecessor_custody=self.owner, family=self.family, model='mi27',
                         initial_failures=0, output_root=str(root / 'new_output'))
        self.donors = []
        self.actuals = {}
        self.bank_jobs = {}
        self.priority_rows = []
        self.flags = {}
        roles = ('mi', 'ny', 'texas') if historical else ('texas',)
        for role in roles:
            jobs = [self.job(role, i) for i in range(3)]
            jobs_ref = self.save(jobs)
            ids = [job['measurement_id'] for job in jobs]
            journal = self.save({'saved_journal': role})
            lock = dict(path=str(root / (role + '.lock')), device=1,
                        inode=10 + len(self.donors))
            if historical:
                rows = [dict(ordinal=i, input_sha256=core.digest(job), **{
                    k: job[k] for k in ('measurement_id', 'native_job_sha256', 'original_seed',
                                        'seed', 'context_canonical_sha256',
                                        'question_canonical_sha256', 'request_body_sha256',
                                        'request_file_sha256')})
                        for i, job in enumerate(jobs) if i]
                unstarted = self.save(dict(schema='causes.unstarted_fixed_self_reservation.v1',
                    jobs=jobs_ref, assignment_or_seed_changed=False, original_count=3,
                    unstarted_count=2, rows=rows))
                schemas = dict(mi='causes.requested_priority_mi_self_drain.v1',
                               ny='causes.user_requested_ny_self_drain.v1',
                               texas='causes.user_requested_texas_self_drain.v1')
                actual = dict(schema=schemas[role], all_claimed_terminal=True,
                    counts=dict(active=0, assigned=3, claimed=1, terminal=1, unstarted=2,
                                consecutive_failures=0, technical_failures=1),
                    original_jobs=jobs_ref, unstarted=unstarted,
                    terminal_custody=self.save(dict(claimed_ids=ids[:1], terminal_ids=ids[:1])),
                    original_process_absence=[dict(pid=100 + len(self.donors), absent=True)],
                    executor_lock=dict(lock, free=True), pause=self.save({'pause': role}),
                    journal_original=journal)
                flag = 'accepted_user_requested_' + role + '_self_drained_custody'
                for i in (1, 2):
                    self.priority_rows.append(dict(original_scope=role, original_ordinal=i,
                        unstarted_ref=unstarted, input_sha256=core.digest(jobs[i]),
                        **{k: jobs[i][k] for k in ('measurement_id', 'run_id', 'question_id',
                                                 'seed', 'request_body_sha256')}))
                kind = 'historical_drained'
            else:
                actual = dict(schema='causes.finite_supplement_tranche_closed.v1', role=role,
                    active=0, ambiguous=[], clean_native_ending=True, full_tranche_qualified=True,
                    qualification_findings=[], all_technical_complete=True,
                    plan=self.save(dict(jobs=jobs_ref)), complete_ids=ids[:1],
                    unstarted_ids=ids[1:], terminal=1, consecutive_failures=0,
                    all_invocation_pids_absent=[100],
                    process_absence=[dict(pid=100, expected_start_ticks='7', argv=['fixture'])],
                    executor_lock=dict(lock, acquired_nonblocking=True, released=True),
                    original_journal=journal)
                flag = 'accepted_bounded_finite_tranche_closed'
                kind = 'finite_closed'
            self.actuals[role] = actual
            self.flags[role] = flag
            self.bank_jobs[role] = jobs
            self.donors.append(dict(kind=kind, role=role, custody=self.peer(actual, flag)))
        self.selected = [job for role in roles for job in self.bank_jobs[role][1:]]
        self.plan['jobs_sha256'] = core.digest(self.selected)
        self.manifest = dict(schema='causes.original_supplement_reservation_transfer.v1',
            execution_predecessor=self.owner, target_role='mi', target_output=self.plan['output_root'],
            family=self.family, model='mi27', source_reservations=self.donors,
            selection=dict(mode='entire_unstarted'), job_count=len(self.selected),
            jobs_sha256=core.digest(self.selected), donor_counters={role: 0 for role in roles},
            donor_occupied_ids_sha256=core.digest(sorted(
                self.bank_jobs[role][0]['measurement_id'] for role in roles)))
        if historical:
            self.metadata = dict(schema='causes.saved_unstarted_priority_subset.v1',
                runtime_authorized=False, assignments=self.save({'frozen': 'assignments'}),
                selection=self.save({'frozen': 'selection'}), rows=self.priority_rows)
            self.manifest['selection'] = dict(mode='saved_priority', metadata=self.save(self.metadata))
        self.bind()

    def job(self, role, index, family=None):
        family = family or self.family
        question = 'evaluation_strongly_favors' if index % 2 == 0 else 'evaluation_leans_towards_or_favors'
        return dict(family=family, model='mi27', run_id=role + str(index), stage='early',
                    question_id=question, measurement_id='/'.join(
                        (family, 'mi27', role + str(index), 'early', question)),
                    seed=index, original_seed=index + 10, native_job_sha256='1' * 64,
                    context_canonical_sha256='2' * 64, question_canonical_sha256='3' * 64,
                    request_body_sha256='4' * 64, request_file_sha256='5' * 64)

    def save(self, value):
        path = self.root / (str(self.n) + '.json')
        self.n += 1
        core.save(path, value)
        return core.pin(path)

    def peer(self, value, flag):
        actual = self.save(value)
        review = self.save({flag: True, 'actual': actual, 'findings': []})
        return dict(actual=actual, review=review)

    def bind(self):
        self.plan['original_reservation_transfer'] = self.peer(
            self.manifest, 'accepted_original_supplement_reservation_transfer')

    def replace_actual(self, role, change):
        change(self.actuals[role])
        for donor in self.donors:
            if donor['role'] == role:
                donor['custody'] = self.peer(self.actuals[role], self.flags[role])
        self.bind()

    def resolve(self):
        return direct.original_transfer_jobs(self.plan, self.previous)


class OriginalTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fixture(self, historical=False):
        return TransferFixture(self.root, historical)

    def rejected(self, fixture, reason):
        with self.assertRaisesRegex(core.IntegrityError, reason):
            fixture.resolve()

    def test_finite_preserves_exact_job_objects_order_and_lock(self):
        f = self.fixture()
        result = f.resolve()
        self.assertEqual(result['jobs'], f.selected)
        self.assertEqual(result['donor_counters'], {'texas': 0})
        self.assertEqual(result['donor_locks'][0]['inode'], 10)
        self.assertIn(f.actuals['texas']['original_journal'], result['required_pins'])

    def test_historical_priority_keeps_each_original_order_and_failed_occupancy(self):
        f = self.fixture(True)
        result = f.resolve()
        self.assertEqual(result['jobs'], f.selected)
        self.assertEqual(len(result['donor_occupied_ids']), 3)
        self.assertEqual(len(result['donor_locks']), 3)

    def test_priority_is_entire_exact_metadata_not_arbitrary_prefix(self):
        f = self.fixture(True)
        f.plan['jobs_sha256'] = core.digest(f.selected[:-1])
        self.rejected(f, 'complete_selection')

    def test_unfinished_execution_predecessor_holds(self):
        f = self.fixture()
        f.previous['unstarted_ids'] = ['still-pending']
        self.rejected(f, 'priority_predecessor_not_complete')

    def test_current_failure_counter_is_not_reset(self):
        f = self.fixture()
        f.previous['consecutive_failures'] = 1
        self.rejected(f, 'priority_predecessor_not_complete')
        f.plan['initial_failures'] = 1
        self.assertEqual(f.resolve()['jobs'], f.selected)

    def test_donor_failure_counter_cannot_be_reset(self):
        f = self.fixture()
        f.replace_actual('texas', lambda a: a.update(consecutive_failures=1))
        self.rejected(f, 'nonzero_donor_counter')

    def test_claimed_missing_attempt_cannot_be_selected(self):
        f = self.fixture()
        f.replace_actual('texas', lambda a: a['unstarted_ids'].insert(0, a['complete_ids'][0]))
        self.rejected(f, 'donor_partition')

    def test_unclaimed_order_cannot_change(self):
        f = self.fixture()
        f.replace_actual('texas', lambda a: a['unstarted_ids'].reverse())
        self.rejected(f, 'finite_order')

    def test_no_cross_family_relabeling(self):
        f = self.fixture()
        f.manifest['family'] = 'candidate_saved_self_v1'
        f.plan['family'] = 'candidate_saved_self_v1'
        f.bind()
        self.rejected(f, 'finite_scope')

    def test_old_declared_queue_needs_typed_peer(self):
        f = self.fixture()
        f.donors[0]['custody'] = f.peer(f.actuals['texas'], 'accepted_unstarted_reservation')
        f.bind()
        self.rejected(f, 'independent_peer')

    def test_donor_pid_list_semantics_preserved(self):
        f = self.fixture()
        f.replace_actual('texas', lambda a: a.update(all_invocation_pids_absent=True))
        self.rejected(f, 'finite_owner')

    def test_donor_absence_roster_must_join(self):
        f = self.fixture()
        f.replace_actual('texas', lambda a: a.update(all_invocation_pids_absent=[101]))
        self.rejected(f, 'finite_owner')

    def test_original_row_seed_mutation_refuses_even_with_new_manifest_peer(self):
        f = self.fixture(True)
        original = f.actuals['mi']['unstarted']
        value = json.loads(core.pinned(original))
        value['rows'][0]['seed'] += 1
        f.replace_actual('mi', lambda a: a.update(unstarted=f.save(value)))
        self.rejected(f, 'original_job_changed')

    def test_reordered_priority_rows_refuse(self):
        f = self.fixture(True)
        f.metadata['rows'][0:2] = reversed(f.metadata['rows'][0:2])
        f.manifest['selection']['metadata'] = f.save(f.metadata)
        f.bind()
        self.rejected(f, 'priority_identity')

    def test_replaced_historical_pause_bytes_refuse(self):
        f = self.fixture(True)
        pause = Path(f.actuals['mi']['pause']['path'])
        pause.write_bytes(b'changed')
        self.rejected(f, 'pin|source')

    def test_metadata_changes_between_phases_are_not_cached(self):
        f = self.fixture()
        f.resolve()
        Path(f.actuals['texas']['original_journal']['path']).write_bytes(b'changed')
        self.rejected(f, 'pin|source')

    def test_owner_physical_slot_cannot_be_renamed_by_family(self):
        f = self.fixture()
        owner = copy.deepcopy(f.selected[0])
        owner['family'] = 'candidate_saved_self_v1'
        owner['measurement_id'] = '/'.join(owner[k] for k in
            ('family', 'model', 'run_id', 'stage', 'question_id'))
        f.previous['full_jobs'] = f.save([owner])
        f.previous['complete_ids'] = [owner['measurement_id']]
        self.rejected(f, 'complete_selection')


if __name__ == '__main__':
    unittest.main()
