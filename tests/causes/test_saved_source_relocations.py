import copy
from pathlib import Path
import tempfile
import unittest

from causes import campaign_source_relocation as relocation
from causes import campaign_supplement_pair_pipeline as pipeline


class Guard:
    """A strict synthetic recorder: require exact listed pins before any read."""
    def __init__(self, files):
        self.files = {ref['path']: ref for ref in files}
        self.recorded = []

    def record(self, ref):
        if self.files.get(ref['path']) != ref or pipeline.pin(ref['path']) != ref:
            raise ValueError('guard_pin')
        self.recorded.append(copy.deepcopy(ref))


class SavedSourceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.original = self.file('checkout.py', b'VALUE = 3\n')
        self.physical = self.file('retained.py', b'VALUE = 3\n')
        self.data = self.file('result.json', b'{"native_label": "unchanged"}')
        self.rows = [dict(original=self.original, physical=self.physical)]
        self.files = [self.original, self.data]

    def file(self, name, raw):
        path = self.root / name
        path.write_bytes(raw)
        return pipeline.pin(path)

    def document(self, name, value):
        return self.file(name, pipeline.encoded(value))

    def prepare(self, rows=None, peer_update=None):
        declaration = self.document('declaration.json', dict(
            schema='causes.saved_source_relocations.v1',
            relocations=self.rows if rows is None else rows))
        peer = dict(accepted_saved_source_relocations=True, declaration=declaration, findings=[])
        peer.update(peer_update or {})
        review = self.document('review.json', peer)
        return declaration, review

    def reader(self, rows=None, peer_update=None):
        declaration, review = self.prepare(rows, peer_update)
        return relocation.SavedSourceRelocations(declaration, review, pipeline.checked, self.files)

    def test_changed_original_reads_only_exact_copy_preserving_logical_evidence(self):
        before = copy.deepcopy((self.rows, self.files))
        literal = dict(source=self.original, text=Path(self.original['path']).read_text())
        Path(self.original['path']).write_bytes(b'VALUE = 99\n')
        reader = self.reader()
        guard = Guard(self.files + reader.physical_sources)
        raw = pipeline.guarded_raw_reader(guard, reader)
        actual = dict(source=self.original, text=raw(self.original).decode())
        self.assertEqual(pipeline.encoded(actual), pipeline.encoded(literal))
        self.assertEqual(guard.recorded, [self.physical])
        self.assertEqual((self.rows, self.files), before)
        self.assertEqual(Path(self.original['path']).read_bytes(), b'VALUE = 99\n')

    def test_missing_original_is_not_opened_or_recreated(self):
        Path(self.original['path']).unlink()
        reader = self.reader()
        guard = Guard(self.files + reader.physical_sources)
        self.assertEqual(pipeline.guarded_raw_reader(guard, reader)(self.original), b'VALUE = 3\n')
        self.assertFalse(Path(self.original['path']).exists())
        self.assertEqual(guard.recorded, [self.physical])

    def test_nonmapped_data_stays_literal_and_hash_change_refuses(self):
        reader = self.reader()
        guard = Guard(self.files + reader.physical_sources)
        raw = pipeline.guarded_raw_reader(guard, reader)
        self.assertEqual(raw(self.data), Path(self.data['path']).read_bytes())
        self.assertEqual(guard.recorded, [self.data])
        Path(self.data['path']).write_bytes(b'{"native_label": "different"}')
        with self.assertRaisesRegex(ValueError, 'guard_pin'):
            raw(self.data)

    def test_nonmapped_source_does_not_fall_back_to_matching_copy(self):
        unrelated = self.file('unmapped.py', b'VALUE = 3\n')
        self.files.append(unrelated)
        reader = self.reader()
        Path(unrelated['path']).unlink()
        with self.assertRaises(FileNotFoundError):
            pipeline.guarded_raw_reader(Guard(self.files + reader.physical_sources), reader)(unrelated)

    def test_changed_pin_at_mapped_path_refuses_instead_of_falling_back(self):
        reader = self.reader()
        changed = dict(self.original, sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'relocation_original_mismatch'):
            reader.resolve(changed)

    def test_copy_mutation_after_acceptance_refuses_on_resolve(self):
        reader = self.reader()
        Path(self.physical['path']).write_bytes(b'VALUE = 4\n')
        with self.assertRaisesRegex(ValueError, 'pin_changed'):
            reader.resolve(self.original)

    def test_wrong_callback_bytes_refuse(self):
        reader = self.reader()
        with self.assertRaisesRegex(ValueError, 'pin_changed'):
            reader.read(self.original, lambda _: b'VALUE = 4\n')

    def test_final_verification_resolves_copy_without_rewriting_original_list(self):
        reader = self.reader()
        original_files = copy.deepcopy(self.files)
        Path(self.original['path']).unlink()
        for logical in self.files:
            physical = reader.resolve(logical)
            self.assertEqual(pipeline.pin(physical['path']), physical)
        self.assertEqual(self.files, original_files)

    def test_peer_requires_exact_declaration_acceptance_and_no_findings(self):
        for update in ({'accepted_saved_source_relocations': False},
                       {'declaration': self.data}, {'findings': ['unresolved']}):
            with self.subTest(update=update), self.assertRaisesRegex(ValueError, 'relocation_review'):
                self.reader(peer_update=update)

    def test_unlisted_original_and_conflicting_allowlist_refuse(self):
        self.files = [self.data]
        with self.assertRaisesRegex(ValueError, 'unknown_original_source'):
            self.reader()
        self.files = [self.original, dict(self.original, bytes=99)]
        with self.assertRaisesRegex(ValueError, 'conflicting_original_allowlist'):
            self.reader()

    def test_duplicate_original_duplicate_physical_and_chains_refuse(self):
        other = self.file('other.py', b'VALUE = 3\n')
        self.files.append(other)
        for rows in (self.rows * 2,
                     self.rows + [dict(original=other, physical=self.physical)],
                     self.rows + [dict(original=other, physical=self.original)]):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, 'duplicate|overlapping'):
                self.reader(rows)

    def test_json_same_path_different_content_and_invalid_pin_types_refuse(self):
        json_copy = self.file('copy.json', Path(self.data['path']).read_bytes())
        different = self.file('different.py', b'VALUE = 77\n')
        rows = [dict(original=self.data, physical=json_copy),
                dict(original=self.original, physical=self.original),
                dict(original=self.original, physical=different),
                dict(original=self.original, physical=dict(self.physical, bytes=float(self.physical['bytes']))),
                dict(original=self.original, physical=dict(self.physical, sha256='G' * 64))]
        for row in rows:
            with self.subTest(row=row), self.assertRaises(ValueError):
                self.reader([row])

    def test_conflicting_known_physical_pin_refuses(self):
        self.files.append(dict(self.physical, bytes=99))
        with self.assertRaisesRegex(ValueError, 'conflicting_physical_allowlist'):
            self.reader()

    def test_returned_source_roster_is_not_mutable_internal_authority(self):
        reader = self.reader()
        reader.physical_sources[0]['sha256'] = '0' * 64
        self.assertEqual(reader.resolve(self.original), self.physical)

    def test_pipeline_binding_checks_helper_review_and_exposes_exact_physical_roster(self):
        declaration, review = self.prepare()
        source = pipeline.pin(relocation.__file__)
        source_review = self.document('source_review.json', dict(
            accepted_saved_source_read_relocation_source=True, relocation_source=source, findings=[]))
        binding = dict(normalization=self.data, source_relocation=dict(
            source=source, source_review=source_review, declaration=declaration, review=review))
        before = copy.deepcopy(binding)
        reader = pipeline.saved_source_relocations(binding, dict(files=self.files, source_files=[]))
        self.assertEqual(reader.physical_sources, [self.physical])
        self.assertEqual(binding, before)
        bad_peer = self.document('source_review.json', dict(
            accepted_saved_source_read_relocation_source=True, relocation_source=self.original, findings=[]))
        binding['source_relocation']['source_review'] = bad_peer
        with self.assertRaisesRegex(ValueError, 'accepted_saved_source_read_relocation_source'):
            pipeline.saved_source_relocations(binding, dict(files=self.files, source_files=[]))

    def test_default_read_and_binding_are_unchanged_and_opt_in_requires_normalization(self):
        self.assertIsNone(pipeline.saved_source_relocations({}, {}))
        guard = Guard(self.files)
        self.assertEqual(pipeline.guarded_raw_reader(guard)(self.original), b'VALUE = 3\n')
        self.assertEqual(guard.recorded, [self.original])
        with self.assertRaisesRegex(ValueError, 'requires_normalization'):
            pipeline.saved_source_relocations(dict(source_relocation={}), {})


if __name__ == '__main__':
    unittest.main()
