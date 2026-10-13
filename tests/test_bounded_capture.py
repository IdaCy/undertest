import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from shared import bounded_capture as capture


class BoundedCaptureTests(unittest.TestCase):
    def run_child(self, code, **changes):
        settings = dict(timeout=2, stdout_limit=1048576, stderr_limit=1048576,
                        total_limit=2097152, cleanup_timeout=0.5)
        settings.update(changes)
        return capture.collect([sys.executable, '-B', '-c', code], **settings)

    def test_exact_binary_two_streams_and_nonzero_exit(self):
        result = self.run_child("import os; os.write(1,b'a\\x00b'); os.write(2,b'err'); raise SystemExit(7)")
        self.assertEqual((result.stdout, result.stderr, result.returncode), (b'a\x00b', b'err', 7))
        self.assertTrue(result.complete)
        self.assertTrue(result.child_reaped)
        self.assertFalse(result.termination_requested)

    def test_empty_success(self):
        result = self.run_child('pass')
        self.assertTrue(result.complete)
        self.assertEqual((result.stdout, result.stderr), (b'', b''))

    def test_large_input_and_output_do_not_deadlock(self):
        data = b'i' * 200000
        result = self.run_child("import os,sys; os.write(1,b'o'*200000); os.write(2,b'e'*200000); data=sys.stdin.buffer.read(); os.write(1,data)", input_bytes=data)
        self.assertTrue(result.complete)
        self.assertEqual(result.stdout, b'o' * 200000 + data)
        self.assertEqual(result.stderr, b'e' * 200000)
        self.assertEqual(result.stdin_written, len(data))

    def test_timeout_keeps_exact_prefix_and_reaps_owned_child(self):
        result = self.run_child("import os,time; os.write(1,b'prefix'); os.write(2,b'err'); time.sleep(10)", timeout=0.15)
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, 'timeout')
        self.assertEqual((result.stdout, result.stderr), (b'prefix', b'err'))
        self.assertTrue(result.termination_requested)
        self.assertTrue(result.child_reaped)
        self.assertFalse(Path('/proc', str(result.pid)).exists())
        self.assertLess(result.elapsed_seconds, 1.5)

    def test_stream_cap_keeps_exact_prefix(self):
        result = self.run_child("import os; os.write(1,bytes(range(256))*100)", stdout_limit=193)
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, 'output_limit_reached')
        self.assertEqual(result.stdout, bytes(range(193)))

    def test_exact_cap_is_conservatively_incomplete(self):
        result = self.run_child("import os; os.write(1,b'x'*17)", stdout_limit=17)
        self.assertEqual(result.stdout, b'x' * 17)
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, 'output_limit_reached')

    def test_combined_cap_is_respected(self):
        result = self.run_child("import os; os.write(1,b'a'*5000); os.write(2,b'b'*5000)", total_limit=7000)
        self.assertFalse(result.complete)
        self.assertEqual(len(result.stdout) + len(result.stderr), 7000)
        self.assertEqual(result.stdout, b'a' * len(result.stdout))
        self.assertEqual(result.stderr, b'b' * len(result.stderr))

    def test_unconsumed_input_is_not_complete(self):
        result = self.run_child('import os; os.close(0)', input_bytes=b'a' * 100000)
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, 'stdin_closed')
        self.assertLess(result.stdin_written, 100000)

    def test_spawn_failure_has_no_child_and_no_retry(self):
        with patch.object(capture.subprocess, 'Popen', side_effect=OSError('fixture')) as popen:
            result = self.run_child('pass')
        self.assertEqual(popen.call_count, 1)
        self.assertEqual((result.pid, result.reason, result.error_type), (None, 'spawn_error', 'OSError'))
        self.assertFalse(result.complete)

    def test_read_error_preserves_preceding_bytes(self):
        original = capture.os.read
        count = 0
        def read(fd, maximum):
            nonlocal count
            if maximum == capture.CHUNK:
                count += 1
                if count == 2:
                    raise OSError('fixture')
            return original(fd, maximum)
        with patch.object(capture.os, 'read', side_effect=read):
            result = self.run_child("import os,time; os.write(1,b'first'); time.sleep(.1); os.write(1,b'second'); time.sleep(10)")
        self.assertFalse(result.complete)
        self.assertEqual((result.reason, result.error_type), ('capture_error', 'OSError'))
        self.assertEqual(result.stdout, b'first')
        self.assertTrue(result.child_reaped)

    def test_does_not_signal_an_unrelated_child(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])
        try:
            result = self.run_child('import time; time.sleep(10)', timeout=0.05)
            self.assertEqual(result.reason, 'timeout')
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait()

    def test_invalid_limits_refuse_before_spawn(self):
        cases = [dict(total_limit=True), dict(stdout_limit=0), dict(stderr_limit=-1),
                 dict(total_limit=capture.MAX_BYTES + 1), dict(timeout=float('nan')),
                 dict(timeout=float('inf')), dict(timeout=True), dict(cleanup_timeout=6),
                 dict(input_bytes=b'xx', input_limit=1)]
        for case in cases:
            with self.subTest(case=case), patch.object(capture.subprocess, 'Popen') as popen:
                with self.assertRaises(ValueError):
                    self.run_child('pass', **case)
                popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
