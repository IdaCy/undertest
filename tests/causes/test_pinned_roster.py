import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from causes import pinned_roster as p


class RosterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / 'input'
        self.path.write_bytes(b'original bytes')
        self.refs = [self.pin(self.path)]

    @staticmethod
    def pin(path):
        data = path.read_bytes()
        return dict(path=str(path), bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

    def proof(self, **changes):
        value = p.ImmutableRosterProof(self.refs, enabled=True, limits=replace(p.Limits(), **changes))
        value.validate()
        return value

    def assert_poisoned(self, value):
        self.assertTrue(value.poisoned)
        with mock.patch.object(p.os, 'open', side_effect=AssertionError('no recovery IO')):
            for call in (value.validate, lambda: value.verify(self.refs), lambda: value.finish(self.refs)):
                with self.assertRaisesRegex(p.RosterError, 'poisoned'):
                    call()

    def mutate(self):
        before = self.path.stat()
        self.path.write_bytes(b'changed! bytes')
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))

    def test_opt_in_and_schema_refuse_before_io(self):
        bad = [dict(self.refs[0], bytes=True), dict(self.refs[0], sha256='A' * 64),
               dict(self.refs[0], extra=1), dict(self.refs[0], path=str(self.root) + '/./input'),
               dict(self.refs[0], path=str(self.root) + '/../input'),
               dict(self.refs[0], path='//' + str(self.path)[1:])]
        with mock.patch.object(p.os, 'open', side_effect=AssertionError('no IO')):
            with self.assertRaisesRegex(p.RosterError, 'opt_in'):
                p.ImmutableRosterProof(self.refs)
            for ref in bad:
                with self.subTest(ref=ref), self.assertRaises(p.RosterError):
                    p.ImmutableRosterProof([ref], enabled=True)
            for refs in ([], self.refs * 2, [self.refs[0], dict(self.refs[0], sha256='0' * 64)]):
                with self.assertRaises(p.RosterError):
                    p.ImmutableRosterProof(refs, enabled=True)

    def test_full_initial_and_final_reads_but_no_body_read_on_reuse(self):
        value = p.ImmutableRosterProof(self.refs, enabled=True)
        first = value.validate()
        self.assertEqual((first.body_bytes_read, first.read_calls), (len(b'original bytes'), 2))
        with mock.patch.object(p.os, 'read', side_effect=AssertionError('hit must not read bodies')):
            hit = value.verify(copy.deepcopy(self.refs))
        self.assertEqual((hit.paths, hit.bytes, hit.body_bytes_read, hit.read_calls), (1, first.bytes, 0, 0))
        self.assertTrue(hit.reused)
        last = value.finish(self.refs)
        self.assertEqual(last.body_bytes_read, first.body_bytes_read)
        self.assertEqual(last.roster_sha256, first.roster_sha256)
        self.assertFalse(last.reused)
        with self.assertRaisesRegex(p.RosterError, 'invalid_state'):
            value.verify(self.refs)

    def test_empty_file_is_a_real_pin_with_eof_read(self):
        self.path.write_bytes(b'')
        self.refs = [self.pin(self.path)]
        value = p.ImmutableRosterProof(self.refs, enabled=True)
        self.assertEqual(value.validate().read_calls, 1)
        self.assertEqual(value.verify(self.refs).body_bytes_read, 0)
        self.assertEqual(value.finish(self.refs).read_calls, 1)

    def test_caller_invalidation_and_readonly_roster_digest(self):
        value = self.proof()
        with self.assertRaises(AttributeError):
            value.roster_sha256 = '0' * 64
        value.invalidate()
        self.assert_poisoned(value)

    def test_roster_is_owned_and_order_pin_and_membership_cannot_change(self):
        second = self.root / 'second'
        second.write_bytes(b'two')
        self.refs.append(self.pin(second))
        for change in (lambda r: r.reverse(), lambda r: r.pop(),
                       lambda r: r[0].update(sha256='0' * 64)):
            value = self.proof()
            candidate = copy.deepcopy(self.refs)
            change(candidate)
            with self.assertRaisesRegex(p.RosterError, 'allowlist_changed'):
                value.verify(candidate)
            self.assert_poisoned(value)
        external = copy.deepcopy(self.refs)
        value = p.ImmutableRosterProof(external, enabled=True)
        external[0]['sha256'] = '0' * 64
        value.validate()
        value.verify(self.refs)

    def test_initial_hash_failure_permanently_poisoned(self):
        value = p.ImmutableRosterProof([dict(self.refs[0], sha256='0' * 64)], enabled=True)
        with self.assertRaisesRegex(p.RosterError, 'content_changed'):
            value.validate()
        self.assert_poisoned(value)

    def test_changed_content_cannot_refresh_after_restoration(self):
        value = self.proof()
        self.mutate()
        with self.assertRaisesRegex(p.RosterError, 'identity_changed'):
            value.verify(self.refs)
        self.path.write_bytes(b'original bytes')
        self.assert_poisoned(value)

    def test_type_size_inode_mode_timestamp_and_link_changes_refuse(self):
        for kind in ('size', 'inode', 'mode', 'mtime', 'link', 'directory', 'symlink', 'deleted'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(dir=self.root) as folder:
                path = Path(folder) / 'value'
                path.write_bytes(b'value')
                refs = [self.pin(path)]
                value = p.ImmutableRosterProof(refs, enabled=True)
                value.validate()
                if kind == 'size':
                    path.write_bytes(b'value!')
                elif kind == 'inode':
                    other = Path(folder) / 'replacement'
                    other.write_bytes(b'value')
                    other.replace(path)
                elif kind == 'mode':
                    path.chmod(0o400)
                elif kind == 'mtime':
                    st = path.stat()
                    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1000000000))
                elif kind == 'link':
                    os.link(path, Path(folder) / 'link')
                else:
                    path.unlink()
                    if kind == 'directory':
                        path.mkdir()
                    elif kind == 'symlink':
                        path.symlink_to(self.path)
                with self.assertRaises((p.RosterError, OSError)):
                    value.verify(refs)
                self.assertTrue(value.poisoned)

    def test_ctime_owner_and_group_are_part_of_identity(self):
        value = self.proof()
        actual = p._file_identity
        for field in ('st_ctime_ns', 'st_uid', 'st_gid'):
            with self.subTest(field=field):
                value = self.proof()
                def changed(st):
                    result = list(actual(st))
                    result[{'st_ctime_ns': 8, 'st_uid': 4, 'st_gid': 5}[field]] += 1
                    return tuple(result)
                with mock.patch.object(p, '_file_identity', side_effect=changed):
                    with self.assertRaisesRegex(p.RosterError, 'identity_changed'):
                        value.verify(self.refs)

    def test_unrelated_sibling_publication_allowed_but_parent_replacement_refuses(self):
        folder = self.root / 'folder'
        folder.mkdir()
        path = folder / 'input'
        path.write_bytes(b'value')
        refs = [self.pin(path)]
        value = p.ImmutableRosterProof(refs, enabled=True)
        value.validate()
        (folder / 'new_directory').mkdir()
        value.verify(refs)
        folder.rename(self.root / 'moved')
        folder.mkdir()
        (folder / 'input').write_bytes(b'value')
        with self.assertRaisesRegex(p.RosterError, 'parent_changed'):
            value.verify(refs)

    def test_parent_symlink_and_parent_mode_change_refuse(self):
        for kind in ('symlink', 'mode'):
            folder = self.root / kind
            folder.mkdir()
            path = folder / 'input'
            path.write_bytes(b'value')
            refs = [self.pin(path)]
            value = p.ImmutableRosterProof(refs, enabled=True)
            value.validate()
            if kind == 'symlink':
                folder.rename(self.root / 'moved')
                folder.symlink_to(self.root / 'moved', target_is_directory=True)
            else:
                folder.chmod(folder.stat().st_mode ^ 0o100)
            with self.subTest(kind=kind), self.assertRaises(p.RosterError):
                value.verify(refs)

    def test_file_replaced_between_stat_and_open_refuses(self):
        value = self.proof()
        original = os.open
        def changed(path, flags, *args, **kwargs):
            if path == 'input':
                other = self.root / 'replacement'
                other.write_bytes(b'original bytes')
                other.replace(self.path)
            return original(path, flags, *args, **kwargs)
        with mock.patch.object(p.os, 'open', side_effect=changed):
            with self.assertRaisesRegex(p.RosterError, 'open_race'):
                value.verify(self.refs)
        self.assert_poisoned(value)

    def test_parent_replaced_between_stat_and_open_refuses(self):
        folder = self.root / 'folder'
        folder.mkdir()
        path = folder / 'input'
        path.write_bytes(b'value')
        refs = [self.pin(path)]
        value = p.ImmutableRosterProof(refs, enabled=True)
        value.validate()
        original = os.open
        def changed(name, flags, *args, **kwargs):
            if name == 'folder':
                folder.rename(self.root / 'moved')
                folder.mkdir()
                (folder / 'input').write_bytes(b'value')
            return original(name, flags, *args, **kwargs)
        with mock.patch.object(p.os, 'open', side_effect=changed):
            with self.assertRaisesRegex(p.RosterError, 'parent_open_race'):
                value.verify(refs)

    def test_open_parent_renamed_during_hash_refuses(self):
        folder = self.root / 'folder'
        folder.mkdir()
        path = folder / 'input'
        path.write_bytes(b'value')
        value = p.ImmutableRosterProof([self.pin(path)], enabled=True)
        original = os.read
        moved = False
        def changed(fd, size):
            nonlocal moved
            data = original(fd, size)
            if data and not moved:
                moved = True
                folder.rename(self.root / 'moved')
                folder.mkdir()
                (folder / 'input').write_bytes(b'value')
            return data
        with mock.patch.object(p.os, 'read', side_effect=changed):
            with self.assertRaisesRegex(p.RosterError, 'parent_changed_during_check'):
                value.validate()

    def test_initial_read_mutation_and_earlier_file_mutation_refuse(self):
        second = self.root / 'second'
        second.write_bytes(b'two')
        for during_second in (False, True):
            self.path.write_bytes(b'original bytes')
            self.refs = [self.pin(self.path), self.pin(second)]
            value = p.ImmutableRosterProof(self.refs, enabled=True)
            original = os.read
            triggered = False
            def changed(fd, size):
                nonlocal triggered
                data = original(fd, size)
                if data == (b'two' if during_second else b'original bytes') and not triggered:
                    triggered = True
                    self.mutate()
                return data
            with mock.patch.object(p.os, 'read', side_effect=changed):
                with self.assertRaisesRegex(p.RosterError, 'changed'):
                    value.validate()
            self.assertTrue(triggered)
            self.assert_poisoned(value)

    def test_partial_failed_or_interrupted_reads_never_establish_proof_and_close_fds(self):
        for outcome in (b'x', OSError('read failed'), KeyboardInterrupt()):
            value = p.ImmutableRosterProof(self.refs, enabled=True)
            opened, closed = [], []
            original_open, original_close = os.open, os.close
            def tracked_open(*args, **kwargs):
                fd = original_open(*args, **kwargs)
                opened.append(fd)
                return fd
            def tracked_close(fd):
                closed.append(fd)
                return original_close(fd)
            effect = (lambda *args: outcome) if type(outcome) is bytes else outcome
            with mock.patch.object(p.os, 'open', side_effect=tracked_open), \
                 mock.patch.object(p.os, 'close', side_effect=tracked_close), \
                 mock.patch.object(p.os, 'read', side_effect=effect):
                with self.assertRaises((p.RosterError, OSError, KeyboardInterrupt)):
                    value.validate()
            self.assertCountEqual(opened, closed)
            self.assert_poisoned(value)

    def test_file_growth_and_same_identity_lasting_change_fail_final_hash(self):
        value = self.proof()
        original_identity = p._file_identity(self.path.stat())
        self.mutate()
        # Model any byte change whose observed metadata stays unchanged. This
        # is an explicit trust limit of hits, not an equivalence claim.
        with mock.patch.object(p, '_file_identity', return_value=original_identity):
            value.verify(self.refs)
            with self.assertRaisesRegex(p.RosterError, 'content_changed'):
                value.finish(self.refs)
        self.assert_poisoned(value)
        self.path.write_bytes(b'original bytes')
        value = self.proof()
        original = os.read
        def grows(fd, size):
            data = original(fd, size)
            if data:
                with self.path.open('ab') as stream:
                    stream.write(b'!')
            return data
        with mock.patch.object(p.os, 'read', side_effect=grows):
            with self.assertRaisesRegex(p.RosterError, 'content_changed'):
                value.finish(self.refs)

    def test_metadata_file_count_bytes_path_and_parent_caps(self):
        cases = [dict(max_file_bytes=1), dict(max_bytes=1), dict(max_metadata_bytes=1),
                 dict(max_path_bytes=1), dict(max_components=2)]
        for changes in cases:
            with self.subTest(changes=changes), mock.patch.object(p.os, 'open', side_effect=AssertionError('no IO')):
                with self.assertRaises(p.RosterError):
                    p.ImmutableRosterProof(self.refs, enabled=True, limits=replace(p.Limits(), **changes))
        second = self.root / 'empty'
        second.write_bytes(b'')
        with self.assertRaisesRegex(p.RosterError, 'roster_count'):
            p.ImmutableRosterProof([*self.refs, self.pin(second)], enabled=True,
                                   limits=replace(p.Limits(), max_files=1))
        value = p.ImmutableRosterProof(self.refs, enabled=True, limits=replace(p.Limits(), max_parents=1))
        with self.assertRaisesRegex(p.RosterError, 'parent_budget'):
            value.validate()
        self.assert_poisoned(value)

    def test_reuse_count_deadline_and_process_boundaries_poison(self):
        value = self.proof(max_reuses=1)
        value.verify(self.refs)
        with self.assertRaisesRegex(p.RosterError, 'reuse_budget'):
            value.verify(self.refs)
        self.assert_poisoned(value)
        value = self.proof(phase_seconds=1)
        with mock.patch.object(p.time, 'monotonic', side_effect=[0, 2]):
            with self.assertRaisesRegex(p.RosterError, 'deadline'):
                value.verify(self.refs)
        self.assert_poisoned(value)
        value = self.proof()
        with mock.patch.object(p.os, 'getpid', return_value=-1):
            with self.assertRaisesRegex(p.RosterError, 'process_changed'):
                value.verify(self.refs)
        self.assert_poisoned(value)

    def test_parallel_hits_are_serialized_and_never_read_bodies(self):
        value = self.proof()
        gate = threading.Barrier(5)
        results, errors = [], []
        def run():
            try:
                gate.wait(timeout=5)
                results.append(value.verify(self.refs))
            except BaseException as error:
                errors.append(error)
        threads = [threading.Thread(target=run) for _ in range(4)]
        with mock.patch.object(p.os, 'read', side_effect=AssertionError('no body read')):
            for thread in threads:
                thread.start()
            gate.wait(timeout=5)
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(len(results), 4)
        self.assertEqual(value._reuses, 4)

    def test_concurrent_conflicting_caller_poison_reaches_inflight_check(self):
        value = self.proof()
        entered, release = threading.Event(), threading.Event()
        original = value._scan
        errors = []
        def gated(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test gate timed out')
            return original(*args, **kwargs)
        def run():
            try:
                value.verify(self.refs)
            except BaseException as error:
                errors.append(error)
        with mock.patch.object(value, '_scan', side_effect=gated):
            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaisesRegex(p.RosterError, 'allowlist_changed'):
                    value.verify([dict(self.refs[0], sha256='0' * 64)])
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIn('poisoned', str(errors[0]))
        self.assert_poisoned(value)

    def test_lock_timeout_is_permanent_and_initialization_cannot_repeat(self):
        value = self.proof(lock_seconds=.01)
        value._lock.acquire()
        try:
            with self.assertRaisesRegex(p.RosterError, 'lock_timeout'):
                value.verify(self.refs)
        finally:
            value._lock.release()
        self.assert_poisoned(value)
        value = self.proof()
        with self.assertRaisesRegex(p.RosterError, 'invalid_state'):
            value.validate()
        self.assert_poisoned(value)

    def test_identity_change_during_hit_and_permission_denial_poison(self):
        value = self.proof()
        original = os.fstat
        seen = 0
        def changed(fd):
            nonlocal seen
            result = original(fd)
            if result.st_ino == self.path.stat().st_ino:
                seen += 1
                if seen == 2:
                    self.mutate()
                    result = original(fd)
            return result
        with mock.patch.object(p.os, 'fstat', side_effect=changed):
            with self.assertRaisesRegex(p.RosterError, 'changed_during_check'):
                value.verify(self.refs)
        self.assert_poisoned(value)
        self.refs = [self.pin(self.path)]
        value = self.proof()
        with mock.patch.object(p.os, 'open', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                value.verify(self.refs)
        self.assert_poisoned(value)


class AdmissionBoundaryTests(unittest.TestCase):
    # Real Admission and native queue; only synthetic jobs and observations.
    setUp = RosterTests.setUp
    pin = staticmethod(RosterTests.pin)
    proof = RosterTests.proof

    def admission_fixture(self, *, reuse=True):
        from tests.test_campaign_supplement_completed_actor import Fixture
        self.fixture_count = getattr(self, 'fixture_count', 0) + 1
        folder = self.root / ('admission' + str(self.fixture_count))
        folder.mkdir()
        fixture = Fixture(folder, count=1, clients=1)
        critical = self.root / 'current_source'
        critical.write_bytes(b'current code')
        critical_ref = self.pin(critical)
        job_file = self.root / 'job_input'
        job_file.write_bytes(b'exact job bytes')
        job_ref = self.pin(job_file)
        proof = self.proof() if reuse else None
        checks = []
        def source_check(job):
            # This is the intended seam: only the separately reviewed immutable
            # roster uses identities. Current code and each job input still read
            # and hash fully on every original source_check call.
            try:
                if proof:
                    proof.verify(self.refs)
                else:
                    self.assertEqual(self.pin(self.path), self.refs[0])
                checks.append((self.pin(critical), self.pin(job_file)))
                if checks[-1] != (critical_ref, job_ref):
                    raise p.RosterError('critical_or_job_pin_changed')
            except BaseException:
                if proof:
                    proof.invalidate()
                raise
        fixture.admission = fixture.make(source_check=source_check)
        return fixture, proof, checks, critical, job_file

    def test_real_admission_keeps_fresh_guards_clock_expiry_and_ready_records(self):
        fixture, proof, checks, _, _ = self.admission_fixture()
        fixture.prepare_delays = [31, 0]
        candidate = fixture.admission.check()
        initial = copy.deepcopy(candidate)
        fixture.admission.prepare(fixture.jobs[0])
        self.assertEqual(fixture.admission.attempts, 2)
        self.assertEqual(fixture.capture_calls, 2)
        self.assertEqual(len(checks), 2)
        self.assertEqual(len([row for row in fixture.rows if row[0] == 'expiry']), 1)
        ready = [row for row in fixture.rows if row[0] == 'ready']
        self.assertEqual(len(ready), 1)
        self.assertNotEqual(initial['observed_from'], candidate['observed_from'])
        self.assertEqual(candidate['claim_ready'], ready[0][2])
        self.assertEqual([r[1]['phase'] for r in fixture.rows if r[0] == 'guard'],
                         ['admission', 'claim_ready', 'admission', 'claim_ready'])
        proof.finish(self.refs)

    def test_real_queue_full_and_reuse_modes_preserve_ids_failures_and_checks(self):
        outcomes = []
        for reuse in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                old_root, old_path, old_refs = self.root, self.path, self.refs
                self.root = Path(directory).resolve()
                self.path = self.root / 'input'
                self.path.write_bytes(b'original bytes')
                self.refs = [self.pin(self.path)]
                try:
                    fixture, proof, checks, _, _ = self.admission_fixture(reuse=reuse)
                    result = fixture.run(lambda job, destination: dict(
                        technical_status='invalid_answer_schema', admissible=False))
                    self.assertEqual(len(checks), 3)
                    self.assertEqual(result['new_final'], 1)
                    journal = [json.loads(line) for line in
                               (fixture.output / '.control' / 'journal.jsonl').read_text().splitlines()]
                    outcomes.append([(row['event'], row.get('measurement_id'),
                                      row.get('consecutive_failures')) for row in journal])
                    if proof:
                        proof.finish(self.refs)
                finally:
                    self.root, self.path, self.refs = old_root, old_path, old_refs
        self.assertEqual(outcomes[0], outcomes[1])

    def test_real_admission_critical_job_and_live_guard_refusals_remain(self):
        for kind in ('critical', 'job'):
            fixture, proof, checks, critical, job_file = self.admission_fixture()
            fixture.admission.check()
            (critical if kind == 'critical' else job_file).write_bytes(b'changed')
            with self.subTest(kind=kind), self.assertRaisesRegex(p.RosterError, 'critical_or_job'):
                fixture.admission.prepare(fixture.jobs[0])
            self.assertFalse([row for row in fixture.rows if row[0] == 'ready'])
            self.assertTrue(proof.poisoned)
            self.assertEqual(len(checks), 1)
        fixture, proof, checks, _, _ = self.admission_fixture()
        fixture.admission.check()
        fixture.guard_changes['ownership_valid'] = False
        from causes import campaign_supplement as core
        with self.assertRaisesRegex(core.IntegrityError, 'live_guard'):
            fixture.admission.prepare(fixture.jobs[0])
        self.assertFalse([row for row in fixture.rows if row[0] == 'ready'])
        self.assertEqual(len(checks), 1)
        proof.finish(self.refs)


if __name__ == '__main__':
    unittest.main()
