import base64
import hashlib
import random
import unittest

from causes import campaign_staging_ranges as selector


def reference(name, data):
    return {'path': '/accepted/'+name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


class ExactRangeCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = random.Random(175).randbytes(5*1024**2+123)

    def check(self, old, new):
        oldref, newref = reference('old', old), reference('new', new)
        result = selector.select_segments(oldref, old, newref, new)
        if result is None:
            return None
        pieces = []
        for segment in result['segments']:
            self.assertGreater(segment['bytes'], 0)
            if 'source' in segment:
                self.assertEqual(segment['source'], oldref)
                self.assertGreaterEqual(segment['offset'], 0)
                self.assertLessEqual(segment['offset']+segment['bytes'], len(old))
                piece = old[segment['offset']:segment['offset']+segment['bytes']]
            else:
                piece = base64.b64decode(segment['base64'], validate=True)
            self.assertEqual(len(piece), segment['bytes'])
            pieces.append(piece)
        self.assertEqual(b''.join(pieces), new)
        self.assertLessEqual(len(pieces), 17)
        self.assertEqual(result['literal_bytes']+result['reused_bytes'], len(new))
        return result

    def test_identical_and_empty_targets(self):
        result = self.check(self.raw, self.raw)
        self.assertEqual(result['literal_bytes'], 0)
        self.assertEqual(result['source_range_count'], 1)
        self.assertIsNone(self.check(b'', b''))
        self.assertIsNone(self.check(self.raw, b''))

    def test_insert_delete_replace_across_anchor_boundaries(self):
        m = 1024**2
        new = b'prefix'+self.raw[:m-3]+b'insert'+self.raw[m-3:2*m]+self.raw[2*m+19:3*m]+b'new'*91+self.raw[3*m+273:]+b'tail'
        result = self.check(self.raw, new)
        self.assertIsNotNone(result)
        self.assertGreater(result['reused_bytes'], 4*m-1000)

    def test_only_internal_span_is_reusable(self):
        new = b'z'*100000+self.raw[2*1024**2:4*1024**2]+b'q'*100000
        result = self.check(self.raw, new)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result['reused_bytes'], 2*1024**2)

    def test_unrelated_bytes_keep_payload_fallback(self):
        self.assertIsNone(self.check(self.raw, b'x'*200000))

    def test_repeated_source_anchors_still_reconstruct_exactly(self):
        old = (b'xyz123'*300000)+self.raw[:100000]
        new = b'header'+old[333:]+b'footer'
        self.assertIsNotNone(self.check(old, new))

    def test_changed_source_or_target_pin_refuses(self):
        for which in ('source', 'target'):
            with self.subTest(which=which):
                oldref, newref = reference('old', self.raw), reference('new', self.raw)
                (oldref if which == 'source' else newref)['sha256'] = '0'*64
                with self.assertRaises(ValueError):
                    selector.select_segments(oldref, self.raw, newref, self.raw)

    def test_boolean_size_is_not_an_integer_count(self):
        oldref = reference('old', b'x')
        oldref['bytes'] = True
        with self.assertRaises(ValueError):
            selector.select_segments(oldref, b'x', reference('new', b'x'), b'x')


class RangeSelectionContractCases(unittest.TestCase):
    def test_strict_pin_types_and_limit(self):
        raw = b'a'*70000
        ref = reference('old', raw)
        for alteration in ({'path': 2}, {'bytes': True}, {'sha256': 'A'*64}, {'extra': False}):
            invalid = dict(ref, **alteration)
            with self.subTest(alteration=alteration), self.assertRaises(ValueError):
                selector.select_segments(invalid, raw, reference('new', raw), raw)
        for limit in (True, -1, len(raw)-1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                selector.select_segments(ref, raw, reference('new', raw), raw, max_bytes=limit)
        with self.assertRaises(ValueError):
            selector.select_segments(ref, bytearray(raw), reference('new', raw), raw)

    def test_deterministic_selection_does_not_mutate_inputs(self):
        import copy
        raw = random.Random(81).randbytes(2*1024**2)
        new = b'prefix'+raw[77:]+b'tail'
        oldref, newref = reference('old', raw), reference('new', new)
        before = copy.deepcopy((oldref, newref))
        first = selector.select_segments(oldref, raw, newref, new)
        second = selector.select_segments(oldref, raw, newref, new)
        self.assertEqual(first, second)
        self.assertEqual((oldref, newref), before)
        first['segments'][1 if 'base64' in first['segments'][0] else 0]['source']['path'] = '/changed'
        self.assertEqual((oldref, newref), before)

    def test_selected_packet_round_trips_through_public_stager(self):
        import tempfile
        from pathlib import Path
        from causes import campaign_staging
        raw = random.Random(923).randbytes(3*1024**2)
        new = b'prefix'+raw[:1024**2]+b'insert'+raw[1024**2+17:]+b'tail'
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source, target = base/'old', base/'new'
            source.write_bytes(raw)
            oldref, newref = reference('old', raw), reference('new', new)
            oldref['path'], newref['path'] = str(source), str(target)
            selected = selector.select_segments(oldref, raw, newref, new)
            packet = {'files': [dict(destination=str(target), bytes=len(new), sha256=newref['sha256'], segments=selected['segments'])], 'verify_only': []}
            result = campaign_staging.stage(packet, base, max_bytes=max(len(raw), len(new)))
            self.assertEqual(target.read_bytes(), new)
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(result['files'], [dict(newref, existing_identical=False)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
