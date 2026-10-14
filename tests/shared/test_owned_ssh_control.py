import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from shared import owned_ssh_control as control


class OwnedControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ssh-control-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cfg = {
            "schema": control.SCHEMA, "source": control.pin(control.__file__),
            "ssh": control.pin("/usr/bin/ssh"), "false_executable": control.pin("/usr/bin/false"),
            "destination": "approved-test", "remote_command": "cat; printf '%s' exact",
            "connection": {"hostname": "127.0.0.1", "port": 22022, "user": "test",
                           "identity_file": str(self.root / "private-key-not-read"),
                           "known_hosts_file": str(self.root / "hosts-not-read"),
                           "connect_timeout": 3, "server_alive_interval": 5,
                           "server_alive_count_max": 2},
            "control_directory": str(self.root / "s"), "output_directory": str(self.root / "out"),
            "maximum_lifetime_seconds": 60, "startup_timeout_seconds": 5,
        }

    def freeze(self, cfg=None, name="binding.json"):
        path = self.root / name
        path.write_text(json.dumps(self.cfg if cfg is None else cfg))
        return control.pin(path)

    def ready_fixture(self):
        ref = self.freeze()
        Path(self.cfg["control_directory"]).mkdir(mode=0o700)
        Path(self.cfg["output_directory"]).mkdir(mode=0o700)
        channel = socket.socket(socket.AF_UNIX)
        self.addCleanup(channel.close)
        path = self.cfg["control_directory"] + "/control"
        channel.bind(path)
        os.chmod(path, 0o600)
        record = {
            "schema": control.SCHEMA + ".ready", "source": self.cfg["source"], "binding": ref,
            "master": {"pid": 12345, "start_ticks": "4321", "argv": control.argv(self.cfg, True)},
            "socket": control.socket_identity(path), "master_socket_fds": ["socket:[55]"],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "expires_monotonic": time.monotonic() + 60,
            "control_check": "Master running (pid=12345)\r\n",
        }
        return ref, record

    def check_ready(self, ref, record):
        ready_ref = control.save(self.root / "ready.json", record)
        with patch.object(control, "process", return_value=record["master"]), \
                patch.object(control, "socket_fds", return_value=["socket:[55]"]):
            return control.ready(ref, ready_ref)

    def test_binding_does_not_read_authentication_files(self):
        self.assertEqual(control.binding(self.freeze()), self.cfg)
        self.assertFalse(Path(self.cfg["connection"]["identity_file"]).exists())

    def test_changed_source_refused(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["source"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "transport source"):
            control.binding(self.freeze(cfg))

    def test_pin_reads_reject_symlinks_and_changed_bytes(self):
        ref = self.freeze()
        Path(ref["path"]).write_text("{}")
        with self.assertRaisesRegex(ValueError, "pin mismatch"):
            control.checked(ref)
        link = self.root / "link"
        link.symlink_to(ref["path"])
        with self.assertRaises(OSError):
            control.pin(link)

    def test_boolean_and_unbounded_lifetimes_refused(self):
        for value in (True, 0, 86_401, 2.5):
            cfg = copy.deepcopy(self.cfg)
            cfg["maximum_lifetime_seconds"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                control.binding(self.freeze(cfg))

    def test_unknown_connection_options_cannot_enable_proxy_or_forwarding(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["connection"]["ProxyCommand"] = "anything"
        with self.assertRaisesRegex(ValueError, "unknown connection"):
            control.binding(self.freeze(cfg))

    def test_long_control_path_and_option_destination_refused(self):
        for key, value in (("control_directory", "/tmp/" + "x" * 100),
                           ("destination", "-oProxyCommand=anything")):
            cfg = copy.deepcopy(self.cfg)
            cfg[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                control.binding(self.freeze(cfg))

    def test_client_command_is_literal_and_fresh_fallback_is_false(self):
        result = control.argv(control.binding(self.freeze()), False)
        self.assertEqual(result[-2:], [self.cfg["destination"], self.cfg["remote_command"]])
        self.assertIn("ProxyCommand=/usr/bin/false", result)
        self.assertIn("ControlMaster=no", result)
        self.assertIn("ControlPersist=no", result)
        self.assertEqual(result[1:3], ["-F", "/dev/null"])
        self.assertNotIn("-f", result)

    def test_ready_requires_owned_private_socket(self):
        ref, record = self.ready_fixture()
        self.assertEqual(self.check_ready(ref, record)[0], self.cfg)

    def test_replaced_socket_refused(self):
        ref, record = self.ready_fixture()
        record["socket"]["inode"] += 1
        with self.assertRaisesRegex(ValueError, "socket changed"):
            self.check_ready(ref, record)

    def test_expired_master_refused(self):
        ref, record = self.ready_fixture()
        record["expires_monotonic"] = time.monotonic() - 1
        with self.assertRaisesRegex(ValueError, "lifetime expired"):
            self.check_ready(ref, record)

    def test_stopping_master_refused(self):
        ref, record = self.ready_fixture()
        (Path(self.cfg["output_directory"]) / "STOP.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "master stopping"):
            self.check_ready(ref, record)

    def test_socket_permissions_refused(self):
        ref, record = self.ready_fixture()
        os.chmod(self.cfg["control_directory"] + "/control", 0o666)
        with self.assertRaisesRegex(ValueError, "socket owner or mode"):
            self.check_ready(ref, record)

    def test_pid_reuse_refused(self):
        ref, record = self.ready_fixture()
        ready_ref = control.save(self.root / "ready.json", record)
        changed = dict(record["master"], start_ticks="4322")
        with patch.object(control, "process", return_value=changed), \
                self.assertRaisesRegex(ValueError, "process identity"):
            control.ready(ref, ready_ref)

    def test_socket_fd_loss_refused(self):
        ref, record = self.ready_fixture()
        ready_ref = control.save(self.root / "ready.json", record)
        with patch.object(control, "process", return_value=record["master"]), \
                patch.object(control, "socket_fds", return_value=[]), \
                self.assertRaisesRegex(ValueError, "socket ownership"):
            control.ready(ref, ready_ref)

    def test_client_replaces_itself_without_reading_or_rewriting_stdin(self):
        with patch.object(control, "ready", return_value=(self.cfg, {})), \
                patch.object(control.os, "execve", side_effect=RuntimeError("exec boundary")) as run:
            with self.assertRaisesRegex(RuntimeError, "exec boundary"):
                control.exec_client({}, {}, self.cfg["destination"], self.cfg["remote_command"])
        run.assert_called_once_with(self.cfg["ssh"]["path"], control.argv(self.cfg, False), control.ENV)

    def test_changed_destination_or_command_cannot_exec(self):
        for destination, command in (("other", self.cfg["remote_command"]),
                                     (self.cfg["destination"], "echo changed")):
            with patch.object(control, "ready", return_value=(self.cfg, {})), \
                    patch.object(control.os, "execve") as run, self.assertRaises(ValueError):
                control.exec_client({}, {}, destination, command)
            run.assert_not_called()

    def test_client_cli_accepts_options_before_exact_remote_arguments(self):
        arguments = ["owned_ssh_control.py", "client", "--binding", "{}", "--ready", "{}",
                     "approved-test", "printf 'literal command'"]
        with patch.object(control.sys, "argv", arguments), patch.object(control, "exec_client") as run:
            self.assertEqual(control.main(), 0)
        run.assert_called_once_with({}, {}, "approved-test", "printf 'literal command'")

    def test_shutdown_signal_cannot_be_swallowed_as_interrupted_system_call(self):
        with self.assertRaises(RuntimeError) as error:
            control.interrupted(15, None)
        self.assertNotIsInstance(error.exception, InterruptedError)

    def test_selector_setup_failure_reaps_already_spawned_owned_child(self):
        ref = self.freeze()
        original = subprocess.Popen
        children = []

        def local_child(*args, **kwargs):
            child = original(["/bin/sleep", "30"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            children.append(child)
            return child

        with patch.object(control.subprocess, "Popen", side_effect=local_child), \
                patch.object(control.selectors, "DefaultSelector", side_effect=OSError("setup failure")):
            result = control.run_master(ref)
        self.assertEqual(result["reason"], "supervisor_error")
        self.assertEqual(result["error"]["message"], "setup failure")
        self.assertIsNotNone(children[0].returncode)
        self.assertFalse(Path("/proc", str(children[0].pid)).exists())
        self.assertTrue((Path(self.cfg["output_directory"]) / "RESULT.json").exists())

    def test_spawn_failure_is_retained_without_ready_or_retry(self):
        ref = self.freeze()
        with patch.object(control.subprocess, "Popen", side_effect=OSError("spawn failure")) as run:
            result = control.run_master(ref)
        self.assertEqual(run.call_count, 1)
        self.assertIsNone(result["returncode"])
        self.assertIsNone(result["ready"])
        self.assertEqual(result["error"]["message"], "spawn failure")


if __name__ == "__main__":
    unittest.main()
