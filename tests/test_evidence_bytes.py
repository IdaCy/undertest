import copy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shared import evidence_bytes as evidence


IDENTITY = {'domain': 'composite_history', 'route': 'route-a', 'source': 'journal-a'}


def retained(path, raw):
    path.write_bytes(raw)
    return evidence.pin(path)


class EvidenceBytesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def case(self, name):
        result = self.root / name
        result.mkdir()
        return result

    def test_gzip_exact_roundtrip_and_exclusive_storage(self):
        for index, raw in enumerate([b'', b'hello\x00world\n', bytes(range(256)) * 4096]):
            with self.subTest(index=index):
                root = self.case(str(index))
                record = evidence.write_gzip(root / 'saved.gz', raw)
                self.assertEqual(evidence.read_gzip(record), raw)
                self.assertEqual(record['raw']['sha256'], hashlib.sha256(raw).hexdigest())
                second = evidence.write_gzip(root / 'other.gz', raw)
                self.assertEqual(evidence.read_pinned(record['stored']),
                                 evidence.read_pinned(second['stored']))
                with self.assertRaises(FileExistsError):
                    evidence.write_gzip(root / 'saved.gz', b'replacement')
                self.assertEqual(evidence.read_gzip(record), raw)

    def test_gzip_rejects_untrustworthy_retained_data(self):
        kinds = ['corrupt', 'truncated', 'trailing', 'multimember',
                 'understated_length', 'wrong_hash']
        for kind in kinds:
            with self.subTest(kind=kind):
                root = self.case(kind)
                raw = b'important journal\n' * 200
                record = evidence.write_gzip(root / 'saved.gz', raw)
                packed = evidence.read_pinned(record['stored'])
                if kind == 'corrupt':
                    packed = packed[:-8] + bytes([packed[-8] ^ 1]) + packed[-7:]
                elif kind == 'truncated':
                    packed = packed[:-1]
                elif kind == 'trailing':
                    packed += b'garbage'
                elif kind == 'multimember':
                    packed += gzip.compress(b'extra', mtime=0)
                elif kind == 'understated_length':
                    record['raw']['bytes'] -= 1
                else:
                    record['raw']['sha256'] = '0' * 64
                record['stored'] = retained(root / 'untrusted.gz', packed)
                with self.assertRaises((ValueError, OSError, evidence.zlib.error)):
                    evidence.read_gzip(record)

    def test_gzip_limits_and_types_are_enforced_before_decode(self):
        record = evidence.write_gzip(self.root / 'data.gz', b'x' * 10000)
        with self.assertRaisesRegex(ValueError, 'byte_count'):
            evidence.read_gzip(record, limit=9999)
        forged = copy.deepcopy(record)
        forged['raw']['bytes'] = 1
        with self.assertRaisesRegex(ValueError, 'decoded_size'):
            evidence.read_gzip(forged)
        forged['raw']['bytes'] = True
        with self.assertRaisesRegex(ValueError, 'byte_count'):
            evidence.read_gzip(forged)
        with self.assertRaisesRegex(ValueError, 'raw_size'):
            evidence.write_gzip(self.root / 'absent.gz', b'123', limit=2)
        self.assertFalse((self.root / 'absent.gz').exists())

    def test_capture_retains_only_new_bytes_and_empty_observations(self):
        base = retained(self.root / 'baseline', b'old\n')
        first = evidence.write_prefix_capture(self.root / 'one', b'old\nnew\n',
                                              baseline=base, identity=IDENTITY)
        second = evidence.write_prefix_capture(self.root / 'two', b'old\nnew\nmore\n',
                                               baseline=base, identity=IDENTITY, previous=first)
        same = evidence.write_prefix_capture(self.root / 'three', b'old\nnew\nmore\n',
                                             baseline=base, identity=IDENTITY, previous=second)
        self.assertEqual(evidence.read_prefix_capture(same, identity=IDENTITY),
                         b'old\nnew\nmore\n')
        self.assertEqual((self.root / 'one/SUFFIX.bytes').read_bytes(), b'new\n')
        self.assertEqual((self.root / 'two/SUFFIX.bytes').read_bytes(), b'more\n')
        self.assertEqual((self.root / 'three/SUFFIX.bytes').read_bytes(), b'')
        self.assertNotEqual(first, second)
        self.assertNotEqual(second, same)
        self.assertEqual(evidence.read_prefix_capture(first, identity=IDENTITY), b'old\nnew\n')
        with self.assertRaisesRegex(ValueError, 'prefix_changed'):
            evidence.write_prefix_capture(self.root / 'refused', b'old\nreplacement\n',
                                          baseline=base, identity=IDENTITY, previous=first)
        self.assertFalse((self.root / 'refused').exists())

    def test_capture_rejects_corrupt_or_mixed_evidence(self):
        kinds = ['offset', 'order', 'hash', 'identity', 'baseline',
                 'parent', 'missing', 'changed', 'duplicate_key']
        for kind in kinds:
            with self.subTest(kind=kind):
                root = self.case(kind)
                base = retained(root / 'baseline', b'a\n')
                first = evidence.write_prefix_capture(root / 'one', b'a\nb\n',
                                                      baseline=base, identity=IDENTITY)
                second = evidence.write_prefix_capture(root / 'two', b'a\nb\nc\n',
                                                       baseline=base, identity=IDENTITY,
                                                       previous=first)
                value = json.loads(evidence.read_pinned(second))
                if kind == 'offset':
                    value['segments'][1]['offset'] += 1
                elif kind == 'order':
                    value['segments'].reverse()
                elif kind == 'hash':
                    value['raw']['sha256'] = '0' * 64
                elif kind == 'identity':
                    value['identity']['domain'] = 'live_journal'
                elif kind == 'baseline':
                    value['baseline'] = retained(root / 'different', b'x\n')
                elif kind == 'parent':
                    value['previous'] = None
                elif kind == 'missing':
                    value['segments'][0]['file']['path'] = str(root / 'absent')
                elif kind == 'changed':
                    (root / 'one/SUFFIX.bytes').write_bytes(b'z\n')
                raw = json.dumps(value).encode()
                if kind == 'duplicate_key':
                    raw = b'{"schema":"wrong",' + raw[1:]
                ref = retained(root / 'untrusted.json', raw)
                with self.assertRaises((ValueError, OSError)):
                    evidence.read_prefix_capture(ref, identity=IDENTITY)

    def test_capture_bounds_preserve_prior_files(self):
        base = retained(self.root / 'baseline', b'a\n')
        first = evidence.write_prefix_capture(self.root / 'one', b'a\nb\n',
                                              baseline=base, identity=IDENTITY)
        with patch.object(evidence, 'SEGMENT_LIMIT', 1):
            with self.assertRaisesRegex(ValueError, 'segment_count'):
                evidence.write_prefix_capture(self.root / 'refused', b'a\nb\nc\n',
                                              baseline=base, identity=IDENTITY, previous=first)
            self.assertFalse((self.root / 'refused').exists())
        self.assertEqual(evidence.read_prefix_capture(first, identity=IDENTITY), b'a\nb\n')
        with self.assertRaisesRegex(ValueError, 'byte_count'):
            evidence.read_prefix_capture(first, identity=IDENTITY, limit=3)
        alias = self.root / 'alias'
        alias.symlink_to(self.root / 'baseline')
        with self.assertRaisesRegex(ValueError, 'noncanonical'):
            evidence.pin(alias)

    def test_interrupted_manifest_keeps_suffix_and_original(self):
        base = retained(self.root / 'baseline', b'a\n')
        original = evidence._write

        def fail_manifest(path, raw):
            if Path(path).name == 'CAPTURE.json':
                raise OSError('simulated full disk')
            return original(path, raw)

        with patch.object(evidence, '_write', side_effect=fail_manifest):
            with self.assertRaisesRegex(OSError, 'simulated full disk'):
                evidence.write_prefix_capture(self.root / 'one', b'a\nb\n',
                                              baseline=base, identity=IDENTITY)
        self.assertEqual(evidence.read_pinned(base), b'a\n')
        self.assertEqual((self.root / 'one/SUFFIX.bytes').read_bytes(), b'b\n')
        self.assertFalse((self.root / 'one/CAPTURE.json').exists())
        with self.assertRaises(FileExistsError):
            evidence.write_prefix_capture(self.root / 'one', b'a\nb\n',
                                          baseline=base, identity=IDENTITY)

    def test_raw_pin_never_sniffs_compression(self):
        record = evidence.write_gzip(self.root / 'saved.gz', b'text')
        self.assertNotEqual(evidence.read_pinned(record['stored']), b'text')
        with self.assertRaisesRegex(ValueError, 'gzip_record_schema'):
            evidence.read_gzip(record['stored'])

    def test_pin_rejects_normalized_path_aliases(self):
        reference = retained(self.root / 'suffix', b'data')
        for alias in (str(self.root) + '/./suffix', str(self.root) + '//suffix'):
            with self.subTest(alias=alias):
                forged = {**reference, 'path': alias}
                with self.assertRaisesRegex(ValueError, 'nonliteral_canonical_path'):
                    evidence.read_pinned(forged)

    def test_child_rejects_invalid_immediate_parent_metadata(self):
        kinds = ['raw_length', 'baseline_boolean', 'offset_boolean',
                 'segment_size_boolean', 'previous_boolean']
        for kind in kinds:
            with self.subTest(kind=kind):
                root = self.case(kind)
                base = retained(root / 'baseline', b'a')
                first = evidence.write_prefix_capture(root / 'one', b'ab',
                                                      baseline=base, identity=IDENTITY)
                second = evidence.write_prefix_capture(root / 'two', b'abc',
                                                       baseline=base, identity=IDENTITY,
                                                       previous=first)
                parent = json.loads(evidence.read_pinned(first))
                if kind == 'raw_length':
                    parent['raw'] = {'bytes': 0, 'sha256': hashlib.sha256(b'').hexdigest()}
                elif kind == 'baseline_boolean':
                    parent['baseline']['bytes'] = True
                elif kind == 'offset_boolean':
                    parent['segments'][0]['offset'] = True
                elif kind == 'segment_size_boolean':
                    parent['segments'][0]['file']['bytes'] = True
                else:
                    parent['previous'] = True
                parent_ref = retained(root / 'bad_parent.json', json.dumps(parent).encode())
                with self.assertRaises(ValueError):
                    evidence.read_prefix_capture(parent_ref, identity=IDENTITY)
                child = json.loads(evidence.read_pinned(second))
                child['previous'] = parent_ref
                child_ref = retained(root / 'bad_child.json', json.dumps(child).encode())
                with self.assertRaises(ValueError):
                    evidence.read_prefix_capture(child_ref, identity=IDENTITY)

    def test_immediate_parent_must_have_a_valid_initial_shape(self):
        base = retained(self.root / 'baseline', b'a')
        first = evidence.write_prefix_capture(self.root / 'one', b'ab',
                                              baseline=base, identity=IDENTITY)
        value = json.loads(evidence.read_pinned(first))
        invalid_parent = copy.deepcopy(value)
        invalid_parent['segments'] = []
        invalid_parent['raw'] = {'bytes': 1, 'sha256': hashlib.sha256(b'a').hexdigest()}
        parent_ref = retained(self.root / 'bad_parent.json', json.dumps(invalid_parent).encode())
        with self.assertRaisesRegex(ValueError, 'initial_capture_segment_count'):
            evidence.read_prefix_capture(parent_ref, identity=IDENTITY)
        value['previous'] = parent_ref
        child_ref = retained(self.root / 'bad_child.json', json.dumps(value).encode())
        with self.assertRaisesRegex(ValueError, 'continued_capture_segment_count'):
            evidence.read_prefix_capture(child_ref, identity=IDENTITY)


if __name__ == '__main__':
    unittest.main()
