import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from causes import campaign_supplement_evidence as storage
from shared import evidence_bytes as exact


class TransportPairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.domain = dict(source='pinned-observer', invocation='fixed-role-invocation', route='strict-route')

    def identity(self, n=1):
        return dict(observer='pinned-observer', directory=f'fresh-observation-{n}', route='strict-route')

    def write(self, n=1, stdout=b'partial\x00\xff{', stderr=b'error\xff', previous=None):
        return storage.write_transport_pair(self.root / str(n), stdout, stderr,
            identity=self.identity(n), domain=self.domain, previous=previous)

    def test_exact_split_and_two_files(self):
        pair = self.write()
        self.assertEqual(storage.read_transport_pair(pair, identity=self.identity(), domain=self.domain),
                         dict(stdout=b'partial\x00\xff{', stderr=b'error\xff'))
        self.assertEqual(len(list((self.root / '1').iterdir())), 2)
        for name in ('stdout', 'stderr'):
            view = storage.transport_stream_reference(pair, name)
            self.assertEqual(storage.read_transport_stream(view, identity=dict(self.identity(), stream=name), domain=self.domain),
                             b'partial\x00\xff{' if name == 'stdout' else b'error\xff')

    def test_empty_stream_boundaries(self):
        for n, out, err in ((1, b'', b''), (2, b'out', b''), (3, b'', b'err')):
            pair = self.write(n, out, err)
            self.assertEqual(storage.read_transport_pair(pair, identity=self.identity(n), domain=self.domain), dict(stdout=out, stderr=err))

    def test_raw_and_gzip_passthrough(self):
        for n, encoding in enumerate(('raw', 'gzip')):
            identity = dict(self.identity(), stream='stdout');raw = b'noncanonical\x00\xff'
            ref = storage.write_transport(self.root / f'old{n}', raw, identity=identity, encoding=encoding)
            self.assertEqual(storage.read_transport_stream(ref, identity=identity), raw)

    def test_current_identity_and_domain_required(self):
        pair = self.write()
        for identity, domain in ((self.identity(2), self.domain), (self.identity(), dict(self.domain, route='other')), (self.identity(), None)):
            with self.assertRaises((ValueError, TypeError)):
                storage.read_transport_pair(pair, identity=identity, domain=domain)

    def test_stream_switch_refuses(self):
        view = storage.transport_stream_reference(self.write(), 'stdout')
        with self.assertRaises(ValueError):
            storage.read_transport_stream(view, identity=dict(self.identity(), stream='stderr'), domain=self.domain)

    def test_digest_and_split_tamper_refuse(self):
        pair = self.write()
        for edit in ('bytes', 'sha256'):
            bad = copy.deepcopy(pair);bad['streams']['stdout'][edit] = 1 if edit == 'bytes' else '0' * 64
            with self.assertRaises(ValueError):
                storage.read_transport_pair(bad, identity=self.identity(), domain=self.domain)

    def test_previous_digest_tamper_refuses_before_write(self):
        pair = self.write();bad = copy.deepcopy(pair);bad['streams']['stderr']['sha256'] = '0' * 64
        with self.assertRaises(ValueError):self.write(2, previous=bad)
        self.assertFalse((self.root / '2').exists())

    def test_prior_identity_is_not_current_identity(self):
        pair = self.write()
        with self.assertRaises(ValueError):
            storage.write_transport_pair(self.root / 'again', b'new', b'', identity=self.identity(), domain=self.domain, previous=pair)
        self.assertFalse((self.root / 'again').exists())

    def test_total_and_stderr_caps_before_write(self):
        for n, stdout, stderr, limit in ((1, b'123', b'45', 4), (2, b'', b'x' * (1024**2 + 1), 2 * 1024**2)):
            with self.assertRaises(ValueError):
                storage.write_transport_pair(self.root / str(n), stdout, stderr, identity=self.identity(n), domain=self.domain, limit=limit)
            self.assertFalse((self.root / str(n)).exists())

    def test_delta_successor_preserves_both_streams(self):
        raw = b''.join(hashlib.sha256(str(n).encode()).digest() for n in range(2000))
        first = self.write(1, raw, b'');second = self.write(2, b'new-prefix' + raw + b'new-tail', b'partial error', first)
        result = storage.read_transport_pair(second, identity=self.identity(2), domain=self.domain)
        self.assertEqual(result, dict(stdout=b'new-prefix' + raw + b'new-tail', stderr=b'partial error'))
        manifest = json.loads(exact.read_pinned(second['manifest']))
        self.assertEqual(manifest['encoding'], 'block_delta')
        self.assertEqual(manifest['previous'], first['manifest'])

    def test_unknown_pair_or_view_shape_refuses(self):
        pair = self.write();pair['schema'] = 'unknown'
        with self.assertRaises(ValueError):storage.read_transport_pair(pair, identity=self.identity(), domain=self.domain)
        with self.assertRaises(ValueError):storage.transport_stream_reference(pair, 'stdout')

    def test_corrupt_payload_refuses(self):
        pair = self.write();manifest = json.loads(exact.read_pinned(pair['manifest']))
        Path(manifest['payload']['stored']['path']).write_bytes(b'changed')
        with self.assertRaises(ValueError):storage.read_transport_pair(pair, identity=self.identity(), domain=self.domain)


if __name__ == '__main__':
    unittest.main()
