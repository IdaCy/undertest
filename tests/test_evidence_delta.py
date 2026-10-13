import copy
import gzip
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from shared import evidence_bytes as exact
from shared import evidence_delta as delta


DOMAIN = dict(source='pinned-observer', invocation='one', stream='stdout',
              argv=['ssh', 'exact-host', 'exact-command'])


class DeltaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.base = random.Random(371).randbytes(128 * 1024)

    def write(self, name, raw, previous=None, **kwargs):
        return delta.write_transport(self.root / name, raw,
            identity={'observation': name}, domain=DOMAIN, previous=previous, **kwargs)

    def read(self, reference, name, **kwargs):
        return delta.read_transport(reference, identity={'observation': name},
                                    domain=DOMAIN, **kwargs)

    def record(self, reference):
        return json.loads(exact.read_pinned(reference))

    def changed(self, reference, edit, name):
        value = self.record(reference)
        edit(value)
        path = self.root / (name + '.json')
        path.write_bytes(exact._encoded(value) + b'\n')
        return exact.pin(path)

    def pair(self):
        first = self.write('one', self.base)
        raw = b'changed\xff\0prefix' + self.base[:65536] + b'insert' + self.base[65536:] + b'tail'
        second = self.write('two', raw, first)
        self.assertEqual(self.record(second)['encoding'], 'block_delta')
        return first, second, raw

    def test_arbitrary_bytes_and_changed_positions_roundtrip(self):
        first = self.write('base', self.base)
        cases = [b'', b'\0\xffpartial {', self.base,
                 b'prefix' + self.base + b'suffix',
                 self.base[:32000] + b'insert' + self.base[33000:],
                 self.base[65536:] + self.base[:65536]]
        for number, raw in enumerate(cases):
            name = f'case{number}'
            ref = self.write(name, raw, first)
            self.assertEqual(self.read(ref, name), raw)
        self.assertEqual(self.read(first, 'base'), self.base)

    def test_size_fallback_and_single_payload_write(self):
        first = self.write('one', self.base)
        second = self.write('two', random.Random(991).randbytes(len(self.base)), first)
        self.assertEqual(self.record(second)['encoding'], 'gzip')
        self.assertEqual(sorted(p.name for p in (self.root / 'two').iterdir()),
                         ['PAYLOAD.gz', 'TRANSPORT.json'])
        self.assertIsNone(self.record(second)['anchor'])

    def test_delta_cost_includes_manifest_and_reduces_retained_bytes(self):
        first, second, raw = self.pair()
        value = self.record(second)
        full = self.write('independent', raw)
        self.assertLess(second['bytes'] + value['payload']['stored']['bytes'],
                        full['bytes'] + self.record(full)['payload']['stored']['bytes'])
        self.assertEqual(value['anchor'], first)
        self.assertEqual(self.read(second, 'two'), raw)

    def test_current_identity_domain_and_json_types_are_exact(self):
        first, second, unused = self.pair()
        with self.assertRaisesRegex(ValueError, 'identity_changed'):
            delta.read_transport(second, identity={'observation': 'other'}, domain=DOMAIN)
        for domain in [dict(DOMAIN, argv=['other']), dict(DOMAIN, stream='stderr'),
                       dict(DOMAIN, source=True), dict(DOMAIN, invocation='other')]:
            with self.subTest(domain=domain), self.assertRaisesRegex(ValueError, 'domain_changed'):
                delta.read_transport(second, identity={'observation': 'two'}, domain=domain)
        with self.assertRaisesRegex(ValueError, 'repeated_identity'):
            delta.write_transport(self.root / 'refused', self.base,
                identity={'observation': 'one'}, domain=DOMAIN, previous=first)
        self.assertFalse((self.root / 'refused').exists())

    def test_anchor_and_previous_tampering_refuse_before_new_write(self):
        first, second, raw = self.pair()
        (self.root / 'one/PAYLOAD.gz').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):
            self.read(second, 'two')
        with self.assertRaises(ValueError):
            self.write('refused', raw, second)
        self.assertFalse((self.root / 'refused').exists())

    def test_invalid_operations_and_raw_hash_are_rejected(self):
        first, second, unused = self.pair()
        edits = [lambda v: v['operations'][0].__setitem__(1, True),
                 lambda v: v['operations'][0].__setitem__(1, -1),
                 lambda v: v['operations'][0].__setitem__(2, True),
                 lambda v: v['operations'][0].__setitem__(2, 0),
                 lambda v: v['operations'][0].__setitem__(2, delta.RAW_LIMIT + 1),
                 lambda v: v['operations'][0].__setitem__(0, 'unknown'),
                 lambda v: v['operations'].reverse(),
                 lambda v: v['operations'].pop(),
                 lambda v: v['raw'].__setitem__('sha256', '0' * 64),
                 lambda v: v['raw'].__setitem__('bytes', True),
                 lambda v: v.__setitem__('ordinal', True),
                 lambda v: v.__setitem__('extra', 0)]
        for number, edit in enumerate(edits):
            with self.subTest(number=number), self.assertRaises(ValueError):
                self.read(self.changed(second, edit, f'bad{number}'), 'two')

    def test_literal_gzip_bomb_and_trailing_data_refuse(self):
        unused, second, unused_raw = self.pair()
        for index, kind in enumerate(['trailing', 'member', 'understated']):
            value = self.record(second)
            packed = exact.read_pinned(value['payload']['stored'])
            if kind == 'trailing':
                packed += b'garbage'
            elif kind == 'member':
                packed += gzip.compress(b'extra')
            else:
                value['payload']['raw']['bytes'] = 1
            path = self.root / f'bad-gzip-{index}'
            path.write_bytes(packed)
            value['payload']['stored'] = exact.pin(path)
            manifest = self.root / f'bad-manifest-{index}'
            manifest.write_bytes(exact._encoded(value))
            with self.assertRaises(ValueError):
                self.read(exact.pin(manifest), 'two')

    def test_no_recursive_anchor_or_detached_predecessor(self):
        first, second, raw = self.pair()
        third = self.write('three', b'new' + raw, second)
        self.assertEqual(self.record(third)['anchor'], first)
        bad = self.changed(third, lambda v: v.__setitem__('anchor', second), 'recursive')
        with self.assertRaises(ValueError):
            self.read(bad, 'three')
        bad = self.changed(third, lambda v: v.__setitem__('previous', first), 'skipped')
        with self.assertRaisesRegex(ValueError, 'previous_ordinal'):
            self.read(bad, 'three')

    def test_rollover_forces_full_anchor_and_constant_decode_depth(self):
        with patch.object(delta, 'GROUP_SIZE', 3):
            first = self.write('one', self.base)
            second = self.write('two', b'a' + self.base, first)
            third = self.write('three', b'b' + self.base, second)
            fourth = self.write('four', b'c' + self.base, third)
            fifth = self.write('five', b'd' + self.base, fourth)
            self.assertEqual(self.record(fourth)['encoding'], 'gzip')
            self.assertEqual(self.record(fifth)['anchor'], fourth)
            with patch.object(exact, 'read_gzip', wraps=exact.read_gzip) as reader:
                self.assertEqual(self.read(fifth, 'five'), b'd' + self.base)
                self.assertEqual(reader.call_count, 2)

    def test_limits_metadata_exclusivity_and_partial_writer(self):
        first = self.write('one', self.base)
        with self.assertRaises(ValueError):
            self.write('small', self.base, limit=len(self.base) - 1)
        self.assertFalse((self.root / 'small').exists())
        with self.assertRaises(FileExistsError):
            self.write('one', b'other')
        with patch.object(delta, 'MANIFEST_LIMIT', 10):
            with self.assertRaisesRegex(ValueError, 'manifest_size'):
                self.write('metadata', b'x')
        original = exact._write
        def fail_manifest(path, raw):
            if Path(path).name == 'TRANSPORT.json':
                raise OSError('simulated interruption')
            return original(path, raw)
        with patch.object(exact, '_write', side_effect=fail_manifest):
            with self.assertRaisesRegex(OSError, 'interruption'):
                self.write('partial', b'new' + self.base, first)
        self.assertTrue((self.root / 'partial/PAYLOAD.gz').exists())
        self.assertFalse((self.root / 'partial/TRANSPORT.json').exists())
        self.assertEqual(self.read(first, 'one'), self.base)

    def test_duplicate_json_and_symlink_are_rejected(self):
        first = self.write('one', self.base)
        path = self.root / 'duplicate.json'
        path.write_bytes(b'{"schema":"wrong",' + exact.read_pinned(first)[1:])
        with self.assertRaisesRegex(ValueError, 'duplicate_json'):
            self.read(exact.pin(path), 'one')
        alias = self.root / 'alias'
        alias.symlink_to(self.root / 'one')
        with self.assertRaisesRegex(ValueError, 'noncanonical'):
            delta.write_transport(alias / 'child', b'x', identity={'n': 1}, domain=DOMAIN)

    def test_encoded_size_and_operation_count_are_bounded_before_decode(self):
        unused, second, unused_raw = self.pair()
        bad = self.changed(second, lambda v: v['payload']['stored'].__setitem__(
            'bytes', delta.STORED_LIMIT + 1), 'stored-overflow')
        with patch.object(exact, 'read_gzip', side_effect=AssertionError('must not decode')):
            with self.assertRaisesRegex(ValueError, 'byte_count'):
                self.read(bad, 'two')
        bad = self.changed(second, lambda v: v.__setitem__('operations',
            [['copy', 0, 1]] * (delta.OP_LIMIT + 1)), 'operation-overflow')
        with self.assertRaisesRegex(ValueError, 'operation_limit'):
            self.read(bad, 'two')

    def test_booleans_do_not_equal_integer_identity_fields(self):
        first = delta.write_transport(self.root / 'typed', b'bytes',
            identity={'ordinal': 1}, domain={'route': 1})
        for identity, domain in [({'ordinal': True}, {'route': 1}),
                                 ({'ordinal': 1}, {'route': True})]:
            with self.assertRaisesRegex(ValueError, 'changed'):
                delta.read_transport(first, identity=identity, domain=domain)

    def test_exact_raw_cap_and_bool_limit(self):
        raw = b'x' * delta.RAW_LIMIT
        first = self.write('limit', raw)
        self.assertEqual(len(self.read(first, 'limit')), delta.RAW_LIMIT)
        for limit in [True, 0, delta.RAW_LIMIT + 1]:
            with self.assertRaisesRegex(ValueError, 'size_limit'):
                self.write('refused', b'', limit=limit)
        with self.assertRaisesRegex(ValueError, 'raw_size'):
            self.write('overflow', raw + b'x')


if __name__ == '__main__':
    unittest.main()
