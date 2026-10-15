"""Bounded saved-byte reuse and synthetic transport equivalence."""
import ast
import hashlib
import inspect
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from causes import campaign_supplement_closed_audit as audit
from tests.test_campaign_closed_audit_transport import SavedTransportFixture


def run_accounting(fixture, closed, *, cached):
    """Count actual body reads, not filesystem/disk traffic or modeled savings."""
    cache = audit.PinnedBytes(maximum_bytes=audit.PinnedBytes.MAX_BYTES if cached else 0)
    original = audit.read_pinned_bytes
    counts = dict(body_reads=0, body_bytes=0)

    def counted(ref):
        body, identity = original(ref)
        counts['body_reads'] += 1
        counts['body_bytes'] += len(body)
        return body, identity

    started = time.perf_counter()
    with patch.object(audit, 'read_pinned_bytes', side_effect=counted):
        saved = fixture.auditor(byte_cache=cache)
        result = saved.remote_accounting(fixture.files, len(fixture.files)//4, closed)
        saved.revalidate()
    counts['elapsed_seconds'] = time.perf_counter() - started
    counts['retained_bytes_after_revalidation'] = cache.retained_bytes
    evidence = audit.encoded(dict(result=result, pins=list(saved.pins.values()),
        ready=saved.control_ready_refs, times=saved.control_intent_times,
        prefixes=list(saved.prefixes.values())))
    cache.clear()
    return evidence, counts


def benchmark_saved_reads(*, calls=64, executable_bytes=128*1024):
    """Finite local synthetic benchmark; no inference about live GPU work."""
    with tempfile.TemporaryDirectory() as root:
        fixture = SavedTransportFixture(root, executable_bytes=executable_bytes)
        fixture.make_calls(calls)
        closed = fixture.close()
        uncached, before = run_accounting(fixture, closed, cached=False)
        cached, after = run_accounting(fixture, closed, cached=True)
        if cached != uncached:
            raise AssertionError('saved accounting evidence changed')
        return dict(calls=calls, executable_bytes_each=executable_bytes,
                    byte_identical_evidence=True, uncached=before, cached=after)


class PinnedBytesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.path = self.root/'value'
        self.path.write_bytes(b'original')
        self.ref = audit.pin(self.path)
        self.cache = audit.PinnedBytes()

    def test_repeated_read_hashes_once_and_checks_identity_on_every_hit(self):
        with patch.object(audit, 'read_pinned_bytes', wraps=audit.read_pinned_bytes) as reads:
            with patch.object(audit, 'named_identity', wraps=audit.named_identity) as identities:
                self.assertEqual(self.cache.read(self.ref), b'original')
                before = identities.call_count
                self.assertEqual(self.cache.read(dict(self.ref)), b'original')
                self.assertGreater(identities.call_count, before)
            self.assertEqual(reads.call_count, 1)

    def test_cache_limits_are_finite_and_zero_disables_reuse(self):
        for field, maximum in (('maximum_bytes', 32*1024**2),
                               ('maximum_entries', 256),
                               ('maximum_object_bytes', 16*1024**2)):
            for bad in (True, -1, maximum+1):
                with self.subTest(field=field, bad=bad), self.assertRaisesRegex(ValueError, 'cache_bound'):
                    audit.PinnedBytes(**{field: bad})
        cache = audit.PinnedBytes(maximum_bytes=0)
        with patch.object(audit, 'read_pinned_bytes', wraps=audit.read_pinned_bytes) as reads:
            cache.read(self.ref); cache.read(self.ref)
        self.assertEqual(reads.call_count, 2)
        self.assertEqual(cache.retained_bytes, 0)
        self.assertEqual(len(cache.entries), 0)

    def test_wrong_full_pin_cannot_hit(self):
        self.cache.read(self.ref)
        for changed in (dict(self.ref, sha256='0'*64), dict(self.ref, bytes=7),
                        dict(self.ref, extra=True), dict(self.ref, bytes=True)):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'pin_mismatch'):
                self.cache.read(changed)

    def test_replacement_with_identical_bytes_refuses(self):
        self.cache.read(self.ref)
        replacement = self.root/'replacement'
        replacement.write_bytes(b'original')
        replacement.replace(self.path)
        with self.assertRaisesRegex(ValueError, 'pin_changed'):
            self.cache.read(self.ref)

    def test_observable_in_place_timestamp_drift_refuses(self):
        self.cache.read(self.ref)
        old = self.path.stat()
        self.path.write_bytes(b'modified')
        os.utime(self.path, ns=(old.st_atime_ns, old.st_mtime_ns+1))
        with self.assertRaisesRegex(ValueError, 'pin_changed'):
            self.cache.read(self.ref)

    def test_restored_mtime_never_bypasses_final_uncached_hash(self):
        self.cache.read(self.ref)
        identity = audit.named_identity(self.ref)
        old = self.path.stat()
        self.path.write_bytes(b'modified')
        os.utime(self.path, ns=(old.st_atime_ns, old.st_mtime_ns))
        # A filesystem may coalesce ctime updates. Hits then retain the original
        # bytes; final uncached authentication must still reject the new body.
        if audit.named_identity(self.ref) != identity:
            with self.assertRaisesRegex(ValueError, 'pin_changed'):
                self.cache.read(self.ref)
        else:
            self.assertEqual(self.cache.read(self.ref), b'original')
        with self.assertRaisesRegex(ValueError, 'pin_mismatch'):
            audit.raw(self.ref)

    def test_mode_and_link_count_changes_refuse(self):
        for change in ('mode', 'link'):
            with self.subTest(change=change):
                cache = audit.PinnedBytes(); cache.read(self.ref)
                if change == 'mode':
                    self.path.chmod(0o400)
                else:
                    os.link(self.path, self.root/'hardlink')
                with self.assertRaisesRegex(ValueError, 'pin_changed'):
                    cache.read(self.ref)

    def test_each_identity_field_is_required(self):
        self.cache.read(self.ref)
        identity = audit.named_identity(self.ref)
        for index in range(len(identity)):
            changed = list(identity); changed[index] += 1
            with self.subTest(index=index), patch.object(audit, 'named_identity', return_value=tuple(changed)):
                with self.assertRaisesRegex(ValueError, 'pin_changed'):
                    self.cache.read(self.ref)

    def test_symlink_directory_and_noncanonical_paths_refuse(self):
        self.cache.read(self.ref)
        target = self.root/'target'; self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'pin_path'):
            self.cache.read(self.ref)
        self.path.unlink(); self.path.mkdir()
        with self.assertRaisesRegex(ValueError, 'pin_path'):
            self.cache.read(self.ref)
        for path in ('relative', str(self.root)+'/../'+self.root.name+'/value'):
            with self.assertRaisesRegex(ValueError, 'pin_path'):
                self.cache.read(dict(self.ref, path=path))

    def test_deletion_and_parent_symlink_replacement_refuse(self):
        self.cache.read(self.ref)
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.cache.read(self.ref)
        parent = self.root/'parent'; parent.mkdir()
        child = parent/'value'; child.write_bytes(b'original')
        ref = audit.pin(child); self.cache.read(ref)
        renamed = self.root/'renamed'; parent.rename(renamed)
        parent.symlink_to(renamed, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'pin_path'):
            self.cache.read(ref)

    def test_open_race_refuses_and_releases_descriptor(self):
        original = audit.os.open
        opened = []
        def replacing(path, flags):
            other = self.root/'new'; other.write_bytes(b'original'); other.replace(self.path)
            fd = original(path, flags); opened.append(fd); return fd
        with patch.object(audit.os, 'open', side_effect=replacing):
            with self.assertRaisesRegex(ValueError, 'pin_changed'):
                self.cache.read(self.ref)
        self.assertEqual(len(self.cache.entries), 0)
        with self.assertRaises(OSError): os.fstat(opened[0])

    def test_mutation_during_read_refuses_before_caching(self):
        original = audit.os.fdopen
        def changing(fd, mode):
            stream = original(fd, mode)
            read = stream.read
            def mutate(amount):
                data = read(amount)
                before = self.path.stat()
                self.path.write_bytes(b'modified')
                os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns+1))
                return data
            stream.read = mutate
            return stream
        with patch.object(audit.os, 'fdopen', side_effect=changing):
            with self.assertRaisesRegex(ValueError, 'pin_changed'):
                self.cache.read(self.ref)
        self.assertEqual(self.cache.retained_bytes, 0)
        self.assertEqual(len(self.cache.entries), 0)

    def test_partial_or_failed_read_never_populates_cache(self):
        original = audit.os.fdopen
        for error in (False, True):
            def incomplete(fd, mode):
                stream = original(fd, mode)
                if error:
                    stream.read = lambda n: (_ for _ in ()).throw(OSError('synthetic read error'))
                else:
                    stream.read = lambda n: b'orig'
                return stream
            with self.subTest(error=error), patch.object(audit.os, 'fdopen', side_effect=incomplete):
                with self.assertRaises((OSError, ValueError)):
                    self.cache.read(self.ref)
            self.assertEqual(len(self.cache.entries), 0)
        self.assertEqual(self.cache.read(self.ref), b'original')

    def test_lru_byte_limit_entry_limit_and_oversized_fallback(self):
        refs = []
        for i in range(3):
            path = self.root/str(i); path.write_bytes(bytes([65+i])*4); refs.append(audit.pin(path))
        for bounds in (dict(maximum_bytes=8), dict(maximum_entries=2)):
            cache = audit.PinnedBytes(**bounds)
            cache.read(refs[0]); cache.read(refs[1]); cache.read(refs[0]); cache.read(refs[2])
            self.assertEqual(list(cache.entries), [refs[0]['path'], refs[2]['path']])
            self.assertEqual(cache.retained_bytes, 8)
            with patch.object(audit, 'read_pinned_bytes', wraps=audit.read_pinned_bytes) as reads:
                cache.read(refs[1])
                self.assertEqual(reads.call_count, 1)
        cache = audit.PinnedBytes(maximum_object_bytes=3)
        with patch.object(audit, 'read_pinned_bytes', wraps=audit.read_pinned_bytes) as reads:
            cache.read(refs[0]); cache.read(refs[0])
            self.assertEqual(reads.call_count, 2)
        self.assertEqual(cache.retained_bytes, 0)

    def test_clear_releases_all_retained_bytes(self):
        self.cache.read(self.ref)
        self.cache.clear()
        self.assertEqual(self.cache.retained_bytes, 0)
        self.assertEqual(len(self.cache.entries), 0)

    def test_empty_files_are_pinned_and_entry_bounded(self):
        cache = audit.PinnedBytes(maximum_entries=2)
        refs = []
        for index in range(3):
            path = self.root/('empty%d' % index); path.write_bytes(b'')
            refs.append(audit.pin(path))
            self.assertEqual(refs[-1]['sha256'], hashlib.sha256(b'').hexdigest())
            self.assertEqual(cache.read(refs[-1]), b'')
        self.assertEqual(cache.retained_bytes, 0)
        self.assertEqual(list(cache.entries), [r['path'] for r in refs[1:]])
        with patch.object(audit, 'read_pinned_bytes', wraps=audit.read_pinned_bytes) as reads:
            self.assertEqual(cache.read(refs[0]), b'')
            self.assertEqual(reads.call_count, 1)


class SavedAuditCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.f = SavedTransportFixture(tmp.name)

    def test_cached_and_uncached_accounting_and_pin_order_are_byte_identical(self):
        self.f.make_calls(6)
        closed = self.f.close()
        before, uncached = run_accounting(self.f, closed, cached=False)
        after, cached = run_accounting(self.f, closed, cached=True)
        self.assertEqual(after, before)
        self.assertLess(cached['body_reads'], uncached['body_reads']//4)
        self.assertLess(cached['body_bytes'], uncached['body_bytes']//4)

    def test_evidence_registration_and_decoded_values_are_not_cached_or_shared(self):
        a = self.f.auditor()
        ref = self.f.put('data.json', {'items': ['saved']})
        a.value(ref)['items'].append('local mutation')
        self.assertEqual(a.value(ref), {'items': ['saved']})
        self.assertEqual(a.pins[ref['path']], ref)
        with self.assertRaisesRegex(ValueError, 'conflicting_pin'):
            a.record(dict(ref, sha256='0'*64))

    def test_changed_source_pin_conflicts_even_after_cache_eviction(self):
        a = self.f.auditor(byte_cache=audit.PinnedBytes(maximum_entries=1))
        ref = self.f.plan['sources']['remote']; a.record(ref)
        a.record(self.f.put('pressure.json', {'pressure': True}))
        self.assertNotIn(ref['path'], a.byte_cache.entries)
        Path(ref['path']).write_bytes(b'# changed synthetic source\n')
        changed = audit.pin(ref['path'])
        with self.assertRaisesRegex(ValueError, 'conflicting_pin'):
            a.record(changed)

    def test_historical_prefixes_stay_distinct_and_are_not_file_cache_entries(self):
        a = self.f.auditor()
        a.journal = b'{"step":1}\n{"step":2}\n'
        path = str(self.f.out/'.control/journal.jsonl')
        prefix = b'{"step":1}\n'
        ref = dict(path=path, bytes=len(prefix), sha256=hashlib.sha256(prefix).hexdigest())
        self.assertEqual(a.read(ref), prefix)
        self.assertEqual(a.read(ref), prefix)
        self.assertNotIn(path, a.byte_cache.entries)
        self.assertNotIn(path, a.pins)
        self.assertEqual(list(a.prefixes.values()), [ref])
        with self.assertRaisesRegex(ValueError, 'historical_journal_prefix'):
            a.read(dict(ref, sha256='0'*64))
        with self.assertRaisesRegex(ValueError, 'historical_journal_prefix'):
            a.read(dict(ref, bytes=len(prefix)-1, sha256=hashlib.sha256(prefix[:-1]).hexdigest()))

    def test_final_revalidation_reads_even_when_hit_identity_is_spoofed(self):
        a = self.f.auditor()
        ref = self.f.put('final.json', {'value': 'old'})
        a.read(ref)
        identity = audit.named_identity(ref)
        Path(ref['path']).write_bytes(audit.encoded({'value': 'new'}))
        with patch.object(audit, 'named_identity', return_value=identity):
            self.assertEqual(a.value(ref), {'value': 'old'})
        with patch.object(a.byte_cache, 'read', side_effect=AssertionError('must bypass cache')):
            with self.assertRaisesRegex(ValueError, 'pin_mismatch'):
                a.revalidate()

    def test_final_revalidation_and_cache_clear_keep_publication_order(self):
        # The real main must still refuse before mkdir/writes, under held locks.
        tree = ast.parse(inspect.getsource(audit.main))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        revalidate = next(n for n in calls if ast.unparse(n.func) == 'audit.revalidate')
        mkdir = next(n for n in calls if ast.unparse(n.func) == 'destination.mkdir')
        self.assertLess(revalidate.lineno, mkdir.lineno)
        finalizers = [n for n in ast.walk(tree) if isinstance(n, ast.Try) and n.finalbody]
        self.assertTrue(any('audit.byte_cache.clear()' in ast.unparse(n.finalbody) for n in finalizers))


if __name__ == '__main__':
    unittest.main()
