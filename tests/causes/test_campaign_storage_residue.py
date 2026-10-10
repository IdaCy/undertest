import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from causes import campaign_storage_residue as residue


def mountinfo(target):
    escaped = str(target).replace('\\', '\\134').replace(' ', '\\040')
    return f'48 1 8:32 /backing {escaped} rw - ext4 /dev/example rw\n'


def record(stdout):
    return {'argv': ['fixture'], 'started_at': 'start', 'ended_at': 'end',
            'returncode': 0, 'stdout': stdout, 'stderr': ''}


class ResidueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name).resolve()
        self.roots = [self.base / 'retired_a', self.base / 'retired_b']
        self.label = 'fixture.campaign=sample'

    def observe(self, runner, raw=''):
        return residue.no_prior_writers(self.roots, campaign_label=self.label,
                                       command_runner=runner, mountinfo=raw)

    def test_unrelated_parent_and_prefix_paths_are_preserved(self):
        listed = record('a123\nb456\n')
        first = record(json.dumps([{'Source': str(self.base / 'active' / 'output')},
                                   {'Source': str(self.roots[0]) + '_other'},
                                   {'Source': ''}, {'Type': 'tmpfs'}]))
        second = record(json.dumps([{'Source': str(self.base)}]))
        runner = Mock(side_effect=[listed, first, second])
        raw = mountinfo(self.base) + mountinfo(str(self.roots[0]) + '_other')
        result = self.observe(runner, raw)
        self.assertEqual(result['containers'], listed)
        self.assertEqual(result['container_mounts'], [first, second])
        self.assertEqual(result['scope'], {'retired_roots': list(map(str, self.roots)),
                                          'campaign_label': self.label})
        self.assertEqual([call.args for call in runner.call_args_list], [
            (['docker', 'ps', '--filter', 'label=' + self.label, '--format', '{{.ID}}'], 15),
            (['docker', 'inspect', '--format', '{{json .Mounts}}', 'a123'], 15),
            (['docker', 'inspect', '--format', '{{json .Mounts}}', 'b456'], 15)])

    def test_root_and_child_mounts_refuse_before_any_command(self):
        for target in (self.roots[0], self.roots[1] / 'episode' / 'work dir'):
            with self.subTest(target=target):
                runner = Mock()
                with self.assertRaisesRegex(ValueError, 'residual_child_mount'):
                    self.observe(runner, mountinfo(target))
                runner.assert_not_called()

    def test_root_and_child_container_sources_refuse(self):
        for source in (self.roots[0], self.roots[1] / 'episode' / 'upper'):
            with self.subTest(source=source):
                runner = Mock(side_effect=[record('a1\n'),
                    record(json.dumps([{'Source': str(source)}]))])
                with self.assertRaisesRegex(ValueError, 'residual_container_binding'):
                    self.observe(runner)
                self.assertEqual(runner.call_count, 2)

    def test_container_inventory_is_bounded_and_unambiguous(self):
        invalid = ['a\na\n', 'a\n\n', 'A\n', 'xyz\n', 'a b\n',
                   '\n'.join(format(i, 'x') for i in range(65)) + '\n']
        for stdout in invalid:
            with self.subTest(stdout=stdout):
                runner = Mock(return_value=record(stdout))
                with self.assertRaisesRegex(ValueError, 'bounded_campaign_container_ids_required'):
                    self.observe(runner)
                self.assertEqual(runner.call_count, 1)
        runner = Mock(return_value=record(''))
        self.assertEqual(self.observe(runner)['container_mounts'], [])
        self.assertEqual(runner.call_count, 1)

    def test_exact_container_limit_is_supported(self):
        ids = [format(i, 'x') for i in range(64)]
        runner = Mock(side_effect=[record('\n'.join(ids))] + [record('[]') for _ in ids])
        self.assertEqual(len(self.observe(runner)['container_mounts']), 64)
        self.assertEqual(runner.call_count, 65)

    def test_malformed_mount_metadata_refuses(self):
        runner = Mock()
        with self.assertRaises(ValueError):
            self.observe(runner, 'not mountinfo\n')
        runner.assert_not_called()
        for stdout in ('{', '{}', '[null]', '[4]', '["mount"]'):
            with self.subTest(stdout=stdout):
                runner = Mock(side_effect=[record('a1'), record(stdout)])
                with self.assertRaises(ValueError):
                    self.observe(runner)
                self.assertEqual(runner.call_count, 2)

    def test_explicit_canonical_scope_is_required_before_commands(self):
        link = self.base / 'link'
        link.symlink_to(self.roots[0])
        invalid_roots = [[], [self.roots[0], self.roots[0]], ['relative'],
                         [str(self.base / 'x' / '..' / 'retired_a')], [link]]
        for roots in invalid_roots:
            with self.subTest(roots=roots):
                runner = Mock()
                with self.assertRaises(ValueError):
                    residue.no_prior_writers(roots, campaign_label=self.label,
                                            command_runner=runner, mountinfo='')
                runner.assert_not_called()
        for label in ('', None, 'fixture\nother', 'fixture\x00other'):
            with self.subTest(label=label):
                runner = Mock()
                with self.assertRaises(ValueError):
                    residue.no_prior_writers(self.roots, campaign_label=label,
                                            command_runner=runner, mountinfo='')
                runner.assert_not_called()

    def test_injected_command_failure_is_not_retried(self):
        runner = Mock(side_effect=subprocess.TimeoutExpired(['docker', 'ps'], 15))
        with self.assertRaises(subprocess.TimeoutExpired):
            self.observe(runner)
        self.assertEqual(runner.call_count, 1)

    def test_default_mountinfo_is_read_only(self):
        runner = Mock(return_value=record(''))
        with patch.object(Path, 'read_text', return_value=mountinfo(self.base)) as read:
            result = residue.no_prior_writers(self.roots, campaign_label=self.label,
                                             command_runner=runner)
        read.assert_called_once_with()
        self.assertEqual(result['mount_rows'][0]['target'], str(self.base))


class CommandTests(unittest.TestCase):
    def test_command_preserves_exact_bounded_invocation_and_metadata(self):
        argv = ['docker', 'ps', '--format', '{{.ID}}']
        completed = subprocess.CompletedProcess(argv, 0, b'abc\n', b'warning\n')
        with patch.object(residue.subprocess, 'run', return_value=completed) as run:
            with patch.object(residue.h, 'now', side_effect=['start', 'end']):
                result = residue.command(argv, 15)
        run.assert_called_once_with(argv, stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=15)
        self.assertEqual(result, {'argv': argv, 'started_at': 'start', 'ended_at': 'end',
                                 'returncode': 0, 'stdout': 'abc\n', 'stderr': 'warning\n'})

    def test_bounds_accept_equality_and_reject_excess(self):
        argv = ['docker', 'ps']
        for out, err, valid in ((2 * 1024**2, 65536, True),
                                (2 * 1024**2 + 1, 0, False), (0, 65537, False)):
            with self.subTest(out=out, err=err):
                completed = subprocess.CompletedProcess(argv, 0, b'x' * out, b'x' * err)
                with patch.object(residue.subprocess, 'run', return_value=completed) as run:
                    if valid:
                        residue.command(argv, 15)
                    else:
                        with self.assertRaisesRegex(ValueError, 'bounded_mount_metadata_required'):
                            residue.command(argv, 15)
                    run.assert_called_once()

    def test_nonzero_timeout_and_invalid_encoding_refuse_without_retry(self):
        argv = ['docker', 'ps']
        cases = [(subprocess.CompletedProcess(argv, 1, b'', b'failure'), ValueError),
                 (subprocess.CompletedProcess(argv, 0, b'\xff', b''), UnicodeDecodeError)]
        for completed, error in cases:
            with self.subTest(error=error):
                with patch.object(residue.subprocess, 'run', return_value=completed) as run:
                    with self.assertRaises(error):
                        residue.command(argv, 15)
                    run.assert_called_once()
        with patch.object(residue.subprocess, 'run',
                          side_effect=subprocess.TimeoutExpired(argv, 15)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                residue.command(argv, 15)
            run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
