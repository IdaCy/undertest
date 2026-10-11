import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from causes import campaign_monitor as m


class ModelReadTests(unittest.TestCase):
    policy = {'model': 'example', 'max_model_len': 4096}

    def read(self, *, delay=0, timeout=None, data=None, status=200):
        body = json.dumps({'data': data if data is not None else [self.policy]}).encode()
        calls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, limit):
                assert limit == 1024 * 1024
                return body

        response = Response()
        response.status = status

        def open_once(url, *, timeout):
            calls.append((url, timeout))
            if delay > timeout:
                raise TimeoutError('fixture response exceeded timeout')
            return response

        # Native /models uses id rather than the policy's model key.
        if data is None:
            body = json.dumps({'data': [{'id': self.policy['model'],
                                        'max_model_len': self.policy['max_model_len']}]}).encode()
        with patch.object(m.urllib.request, 'urlopen', side_effect=open_once):
            kwargs = {} if timeout is None else {'timeout_seconds': timeout}
            result = m.models(8000, self.policy, **kwargs)
        self.assertEqual(calls, [('http://127.0.0.1:8000/v1/models', 4 if timeout is None else timeout)])
        self.assertEqual(result, {'port': 8000, 'http_status': 200, 'bytes': len(body),
                                 'sha256': hashlib.sha256(body).hexdigest(), **self.policy})
        return result

    def test_default_four_seconds_and_response_shape_unchanged(self):
        self.read(delay=3.9)

    def test_explicit_ten_accepts_six_second_fixture(self):
        self.read(delay=6, timeout=10)

    def test_fractional_bound_is_passed_exactly(self):
        self.read(delay=2.5, timeout=2.5)

    def test_invalid_timeouts_refuse_before_request(self):
        with patch.object(m.urllib.request, 'urlopen') as opener:
            for value in (False, True, None, '10', 0, -1, 10.1, float('inf'), float('-inf'), float('nan')):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'model_read_timeout_out_of_range'):
                    m.models(8000, self.policy, timeout_seconds=value)
            opener.assert_not_called()

    def test_timeout_propagates_without_retry(self):
        for kwargs in ({}, {'timeout_seconds': 10}):
            with self.subTest(kwargs=kwargs), patch.object(m.urllib.request, 'urlopen', side_effect=TimeoutError('late')) as opener:
                with self.assertRaisesRegex(TimeoutError, 'late'):
                    m.models(8000, self.policy, **kwargs)
                self.assertEqual(opener.call_count, 1)

    def test_default_still_refuses_six_second_fixture(self):
        with self.assertRaisesRegex(TimeoutError, 'fixture response exceeded timeout'):
            self.read(delay=6)

    def test_explicit_ten_still_refuses_later_fixture(self):
        with self.assertRaisesRegex(TimeoutError, 'fixture response exceeded timeout'):
            self.read(delay=10.01, timeout=10)

    def test_model_identity_context_and_cardinality_still_required(self):
        original = {'id': 'example', 'max_model_len': 4096}
        for rows in ([], [original, original], [{**original, 'id': 'other'}],
                     [{**original, 'max_model_len': 8192}]):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, 'endpoint_model_changed'):
                self.read(timeout=10, data=copy.deepcopy(rows))

    def test_http_status_still_required(self):
        with self.assertRaisesRegex(ValueError, 'endpoint_http_failure'):
            self.read(timeout=10, status=503)


if __name__ == '__main__':
    unittest.main()
