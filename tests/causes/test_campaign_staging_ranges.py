import base64
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from causes import campaign_staging as staging


def pin(path, raw):
    return {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def literal(raw):
    return {'base64': base64.b64encode(raw).decode(), 'bytes': len(raw)}


def source_range(source, offset, length):
    return {'source': source, 'offset': offset, 'bytes': length}


def row(path, raw, segments):
    return {'destination': str(path), 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest(), 'segments': segments}


class RangeStagingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.source = self.base / 'original'
        self.raw = b'abcdefghij'
        self.source.write_bytes(self.raw)
        self.source_pin = pin(self.source, self.raw)
        self.target = self.base / 'new'

    def packet(self, rows):
        return {'files': rows, 'verify_only': []}

    def test_mixed_packet_reconstructs_whole_bytes_without_changing_source(self):
        before = self.source.stat()
        target = b'<bcd/ij>'
        segmented = row(self.target, target, [literal(b'<'), source_range(self.source_pin, 1, 3),
                                             literal(b'/'), source_range(self.source_pin, 8, 2), literal(b'>')])
        direct = {'destination': str(self.base / 'literal'), 'bytes': 1,
                  'sha256': hashlib.sha256(b'x').hexdigest(), 'base64': 'eA=='}
        copied = {'destination': str(self.base / 'copy'), 'bytes': 10,
                  'sha256': self.source_pin['sha256'], 'source': self.source_pin}
        packet = self.packet([segmented, direct, copied])
        packet['verify_only'] = [self.source_pin]
        result = staging.stage(packet, self.base, [self.source])
        self.assertEqual(self.target.read_bytes(), target)
        self.assertEqual((self.base / 'copy').read_bytes(), self.raw)
        self.assertEqual((self.base / 'literal').read_bytes(), b'x')
        for field in ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
            self.assertEqual(getattr(self.source.stat(), field), getattr(before, field))
        self.assertEqual(result['files'], [{**pin(Path(r['destination']), value), 'existing_identical': False}
                                          for r, value in zip(packet['files'], (target, b'x', self.raw))])
        self.assertEqual(result['verify_only_count'], 1)

    def test_whole_source_is_read_once_per_packet_and_never_reused_across_calls(self):
        packet = self.packet([
            row(self.target, b'abe', [source_range(self.source_pin, 0, 2), source_range(self.source_pin, 4, 1)]),
            row(self.base / 'second', b'ij', [source_range(self.source_pin, 8, 2)])])
        with patch.object(staging, '_read_exact', wraps=staging._read_exact) as checked:
            staging.stage(packet, self.base)
            self.assertEqual(sum(c.args[0] == self.source for c in checked.call_args_list), 1)
        with patch.object(staging, '_read_exact', wraps=staging._read_exact) as checked:
            result = staging.stage(packet, self.base)
            self.assertEqual(sum(c.args[0] == self.source for c in checked.call_args_list), 1)
        self.assertTrue(all(r['existing_identical'] for r in result['files']))

    def test_whole_source_hash_required_even_when_selected_range_is_unchanged(self):
        self.source.write_bytes(b'abcdefghiX')
        with self.assertRaisesRegex(ValueError, 'existing_file_pin_changed'):
            staging.stage(self.packet([row(self.target, b'abc', [source_range(self.source_pin, 0, 3)])]), self.base)
        self.assertFalse(self.target.exists())

    def test_literal_and_final_hash_failures_precede_every_write(self):
        valid = row(self.target, b'ab', [literal(b'a'), literal(b'b')])
        changes = [
            lambda r: r['segments'][0].update(base64='Yg=='),
            lambda r: r['segments'][0].update(base64='!!!!'),
            lambda r: r['segments'][0].update(base64=''),
            lambda r: r['segments'].reverse(),
            lambda r: r.update(sha256='0' * 64),
            lambda r: r['segments'][0].update(bytes=3),
        ]
        for change in changes:
            candidate = copy.deepcopy(valid)
            change(candidate)
            first = row(self.base / 'first', b'x', [literal(b'x')])
            with self.subTest(change=change), self.assertRaises(ValueError):
                staging.stage(self.packet([first, candidate]), self.base)
            self.assertFalse(self.target.exists())
            self.assertFalse((self.base / 'first').exists())

    def test_unique_source_bytes_and_output_bytes_have_independent_packet_bounds(self):
        second = self.base / 'second_source'
        second.write_bytes(b'0123456789')
        refs = [self.source_pin, pin(second, b'0123456789')]
        candidate = row(self.target, b'a0', [source_range(refs[0], 0, 1), source_range(refs[1], 0, 1)])
        with self.assertRaisesRegex(ValueError, 'range_source_byte_limit_exceeded'):
            staging.stage(self.packet([candidate]), self.base, max_bytes=19)
        self.assertFalse(self.target.exists())
        with self.assertRaisesRegex(ValueError, 'byte_limit_exceeded'):
            staging.stage(self.packet([row(self.target, b'abc', [literal(b'abc')])]), self.base, max_bytes=2)
        self.assertFalse(self.target.exists())
        staging.stage(self.packet([candidate]), self.base, max_bytes=20)
        self.assertEqual(self.target.read_bytes(), b'a0')

    def test_source_drift_after_cached_read_fails_before_writes(self):
        candidate = row(self.target, b'ax', [source_range(self.source_pin, 0, 1), literal(b'x')])
        decode = base64.b64decode

        def mutate_source(*args, **kwargs):
            before = self.source.stat()
            self.source.write_bytes(b'abcdefghik')
            os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000000))
            return decode(*args, **kwargs)

        with patch.object(staging.base64, 'b64decode', side_effect=mutate_source):
            with self.assertRaisesRegex(ValueError, 'range_source_changed'):
                staging.stage(self.packet([candidate]), self.base)
        self.assertFalse(self.target.exists())

    def test_segment_limit_types_and_range_bounds(self):
        cases = [[], [literal(b'a')] * 33,
                 [{'source': self.source_pin, 'offset': True, 'bytes': 1}],
                 [{'source': self.source_pin, 'offset': -1, 'bytes': 1}],
                 [{'source': self.source_pin, 'offset': 10, 'bytes': 1}],
                 [{'source': self.source_pin, 'offset': 0, 'bytes': 0}],
                 [{'base64': 'YQ==', 'bytes': True}],
                 [{'base64': 'YQ==', 'bytes': 1, 'unknown': False}]]
        for segments in cases:
            count = sum(s['bytes'] for s in segments)
            with self.subTest(segments=segments), self.assertRaises(ValueError):
                staging.stage(self.packet([row(self.target, b'a' * count, segments)]), self.base)
            self.assertFalse(self.target.exists())
        staging.stage(self.packet([row(self.target, b'a' * 32, [literal(b'a')] * 32)]), self.base)
        self.assertEqual(self.target.read_bytes(), b'a' * 32)

    def test_existing_destination_and_empty_literal_preserve_receipt_contract(self):
        packet = self.packet([row(self.target, b'', [literal(b'')])])
        staging.stage(packet, self.base, max_bytes=0)
        before = self.target.stat()
        result = staging.stage(packet, self.base, max_bytes=0)
        self.assertTrue(result['files'][0]['existing_identical'])
        for field in ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns'):
            self.assertEqual(getattr(self.target.stat(), field), getattr(before, field))
        with self.assertRaisesRegex(ValueError, 'existing_file_size_or_type|existing_file_pin_changed'):
            staging.stage(self.packet([row(self.target, b'a', [literal(b'a')])]), self.base)
        self.assertEqual(self.target.read_bytes(), b'')


if __name__ == '__main__':
    unittest.main()
