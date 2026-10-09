import copy
import json
import shlex
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from causes import campaign_monitor as m
from test_campaign_monitor import fixture


def policy():
    return {'transfer_host':'tasks', 'controller_host':'provider'}


def success():
    return SimpleNamespace(returncode=0, stdout=b'{"valid":true}', stderr=b'')


class RemoteBudgetTests(unittest.TestCase):
    def test_unconfigured_remote_preserves_command_deadline_request_and_result(self):
        value = policy()
        with patch.object(m.subprocess, 'run', return_value=success()) as run:
            report = m.remote('jp', {'identity':'fixed'}, value)
        run.assert_called_once_with(
            ['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','provider','python3','-c',shlex.quote(m.REMOTE)],
            input=m.encoded({'side':'jp','registration':{'identity':'fixed'},'policy':value}),
            capture_output=True, timeout=28)
        self.assertNotIn('execution_budget', report)
        self.assertNotIn('elapsed_seconds', report)

    def test_japan_budget_wraps_exact_validator_and_does_not_extend_transfer_side(self):
        value = policy() | {'remote_validation_budgets':{'jp':45}}
        before = copy.deepcopy(value)
        with patch.object(m.subprocess, 'run', return_value=success()) as run, \
                patch.object(m.time, 'monotonic', side_effect=[10.0, 40.0, 41.0, 42.0]):
            jp = m.remote('jp', {}, value)
            mi = m.remote('mi', {}, value)
        first, second = run.call_args_list
        self.assertEqual(first.args[0][5:], ['provider','timeout','--signal=TERM','--kill-after=5s','45s','python3','-c',shlex.quote(m.REMOTE)])
        self.assertEqual(first.kwargs['timeout'], 58)
        self.assertEqual(json.loads(first.kwargs['input']), {'side':'jp','registration':{},'policy':value})
        self.assertEqual(second.args[0][5:], ['tasks','python3','-c',shlex.quote(m.REMOTE)])
        self.assertEqual(second.kwargs['timeout'], 28)
        self.assertEqual(jp['execution_budget'], {'remote_seconds':45,'kill_grace_seconds':5,'ssh_seconds':58})
        self.assertEqual(jp['elapsed_seconds'], 30.0)
        self.assertNotIn('execution_budget', mi)
        self.assertEqual(value, before)

    def test_malformed_budget_is_rejected_before_transport_and_at_policy_admission(self):
        for budgets in [None, [], {'other':45}, {'jp':True}, {'jp':45.0}, {'jp':'45'}, {'jp':27}, {'jp':61}]:
            with self.subTest(budgets=budgets):
                value, _, _, _ = fixture()
                value['remote_validation_budgets'] = budgets
                with self.assertRaisesRegex(ValueError, 'invalid_remote_validation'):
                    m.validate_policy(value)
                with patch.object(m.subprocess, 'run') as run, self.assertRaisesRegex(ValueError, 'invalid_remote_validation'):
                    m.remote('jp', {}, value)
                run.assert_not_called()

    def test_remote_timeout_missing_utility_and_transport_errors_cannot_accept_valid_stdout(self):
        value = policy() | {'remote_validation_budgets':{'jp':45}}
        for returncode in [124, 137, 127, 255]:
            with self.subTest(returncode=returncode):
                result = success()
                result.returncode = returncode
                with patch.object(m.subprocess, 'run', return_value=result) as run, self.assertRaisesRegex(ValueError, 'remote_monitor_failed'):
                    m.remote('jp', {}, value)
                run.assert_called_once()

    def test_outer_timeout_is_not_retried_or_converted_to_qualified_observation(self):
        value = policy() | {'remote_validation_budgets':{'jp':45}}
        with patch.object(m.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 58)) as run, \
                self.assertRaises(subprocess.TimeoutExpired):
            m.remote('jp', {}, value)
        run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
