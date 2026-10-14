import base64
import copy
import gzip
import hashlib
import json
import random
import unittest

from causes import campaign_supplement_provider_status as status


def response(raw, suffix):
    return dict(url='http://127.0.0.1:8080'+suffix, status=200,
                body=dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                          base64=base64.b64encode(raw).decode()))


class ProviderStatusTest(unittest.TestCase):
    def setUp(self):
        self.process = dict(pid=123, process_starttime_ticks='200',
                            argv=['serve', '--max-num-seqs', '16', '--host', '127.0.0.1',
                                  '--port', '8080'])
        self.ref = dict(path='/reviewed/native.py', bytes=10, sha256='a'*64)
        self.plan = dict(execution_host='owned', provider=self.process, boot_id='boot',
                         model='test-model', native_source=self.ref)
        self.identity = dict(host='owned', process=self.process, configuration={'sha256':'b'*64})
        self.raw = (b'vllm:num_requests_running{model_name="test-model"} 2\n'
                    b'vllm:num_requests_waiting 0\nvllm:num_preemptions_total 0\n'
                    b'vllm:kv_cache_usage_perc 0.2\nother_metric NaN\n')
        self.models = b'{"data":[{"id":"test-model"}]}'
        self.calls = []
        def http(suffix):
            self.calls.append(suffix)
            return response(self.models if suffix == '/v1/models' else self.raw, suffix)
        def identity(expected):
            self.calls.append('identity')
            return copy.deepcopy(expected)
        self.native = dict(MODEL='test-model', MAX_HTTP=status.MAX_HTTP, identity=identity,
                           http=http, _verified_native_source=self.ref)

    def capture(self, **kwargs):
        ticks = iter(['2026-01-01T00:00:00+00:00', '2026-01-01T00:00:01+00:00',
                      '2026-01-01T00:00:02+00:00'])
        return status.capture_status(self.native, self.plan, self.identity,
                                     clock=lambda:next(ticks), read_boot=kwargs.get('read_boot', lambda:'boot'))

    def test_exact_raw_body_and_call_order(self):
        value = self.capture()
        self.assertTrue(value['healthy'])
        self.assertEqual(self.calls, ['identity', '/v1/models', '/metrics', 'identity'])
        self.assertEqual(value['running_requests'], 2)
        self.assertEqual(status.unpack_response(value['responses']['metrics'], '/metrics',
                                               'http://127.0.0.1:8080'), self.raw)
        self.assertEqual(status.validate_status(value, self.plan, self.identity)['waiting_requests'], 0)
        self.assertLess(len(status.encoded(value)), status.MAX_PACKET)

    def test_counter_evidence_cannot_change(self):
        value = self.capture(); value['running_requests'] = 3
        with self.assertRaisesRegex(ValueError, 'counter_changed'):
            status.validate_status(value, self.plan, self.identity)
        value = self.capture(); value['responses']['metrics']['body']['sha256'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'body_changed'):
            status.validate_status(value, self.plan, self.identity)

    def test_duplicate_fractional_negative_nonfinite_or_missing_counter(self):
        original = self.raw
        for replacement in (b'0.5', b'-1', b'NaN', b'9223372036854775808'):
            self.raw = original.replace(b'vllm:num_preemptions_total 0',
                                        b'vllm:num_preemptions_total '+replacement)
            self.assertFalse(self.capture()['healthy'])
        self.raw = original+b'vllm:num_requests_running{other="x"} 0\n'
        self.assertFalse(self.capture()['healthy'])
        self.raw = original.replace(b'vllm:num_preemptions_total 0\n', b'')
        self.assertFalse(self.capture()['healthy'])

    def test_completed_invalid_model_response_is_retained_without_next_call(self):
        self.models = b'{"data":[{"id":"wrong-model"}]}'
        value = self.capture()
        self.assertFalse(value['healthy']); self.assertEqual(value['error']['phase'], 'models')
        self.assertEqual(self.calls, ['identity', '/v1/models'])
        self.assertEqual(status.unpack_response(value['responses']['models'], '/v1/models',
                                               'http://127.0.0.1:8080'), self.models)

    def test_timeout_has_no_retry(self):
        def fail(suffix):
            self.calls.append(suffix); raise TimeoutError('timed out')
        self.native['http'] = fail
        value = self.capture()
        self.assertFalse(value['healthy']); self.assertEqual(value['error']['type'], 'TimeoutError')
        self.assertEqual(self.calls, ['identity', '/v1/models'])

    def test_boot_and_process_change_fail(self):
        boots = iter(['boot', 'new-boot'])
        self.assertFalse(self.capture(read_boot=lambda:next(boots))['healthy'])
        self.native['identity'] = lambda expected:dict(expected, process_starttime_ticks='999')
        self.calls.clear()
        self.assertFalse(self.capture()['healthy']); self.assertEqual(self.calls, [])

    def test_address_source_and_capacity_bindings(self):
        self.plan['execution_host'] = 'another'
        self.assertFalse(self.capture()['healthy']); self.assertEqual(self.calls, [])
        self.plan['execution_host'] = 'owned'
        self.native['_verified_native_source'] = dict(self.ref, sha256='0'*64)
        self.assertFalse(self.capture()['healthy']); self.assertEqual(self.calls, [])
        self.native['_verified_native_source'] = self.ref
        self.process['argv'] += ['--max-num-seqs', '32']
        self.assertFalse(self.capture()['healthy']); self.assertEqual(self.calls, [])

    def test_gzip_bound_trailing_member_and_digest(self):
        saved = status.pack_response(response(b'x'*10000, '/metrics'))
        saved['body']['bytes'] = 10
        with self.assertRaises(ValueError):
            status.unpack_response(saved, '/metrics', 'http://127.0.0.1:8080')
        saved = status.pack_response(response(b'x', '/metrics'))
        packed = base64.b64decode(saved['body']['data'])+gzip.compress(b'y')
        saved['body']['data'] = base64.b64encode(packed).decode()
        with self.assertRaises(ValueError):
            status.unpack_response(saved, '/metrics', 'http://127.0.0.1:8080')

    def test_incompressible_body_has_explicit_missing_evidence(self):
        self.models = random.Random(42).randbytes(status.MAX_PACKED_BODY+1000)
        value = self.capture()
        self.assertFalse(value['healthy'])
        self.assertEqual(value['responses']['models']['body']['retention'], 'digest_only_over_bound')
        self.assertLess(len(status.encoded(value)), status.MAX_PACKET)

    def test_load_native_pin_and_non_main(self):
        raw = (b'MAX_HTTP=4*1024**2\nidentity=lambda x:x\nhttp=lambda x:x\n'
               b'if __name__ == "__main__": raise RuntimeError("main called")\n')
        ref = dict(path='native_fixture.py', bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        self.assertEqual(status.load_native(raw, ref)['_verified_native_source'], ref)
        with self.assertRaises(ValueError): status.load_native(raw+b'\n', ref)


if __name__ == '__main__':
    unittest.main()
