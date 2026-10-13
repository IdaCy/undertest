import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[2] / "src/shared/durable_process.py"
spec = importlib.util.spec_from_file_location("durable_process", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DurableProcessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)

    def packet(self, bodies):
        actors = []
        for i, body in enumerate(bodies):
            source = self.root / f"actor{i}.py"
            source.write_text(body)
            ref = module.pin(source)
            actors.append(dict(name=f"actor{i}", source=ref, pins=[ref],
                               argv=[sys.executable, "-B", str(source)]))
        packet = dict(attempt_id="test-attempt", supervisor_source=module.pin(SOURCE),
                      actors=actors, output_directory=str(self.root / "output"), cwd=str(self.root))
        packet_ref = module.save(self.root / "packet.json", packet)
        adoption_ref = module.save(self.root / "adoption.json", dict(
            root_authorized=True, packet=packet_ref, automatic_retry=False,
            maximum_invocations_per_actor=1, attempt_id="test-attempt"))
        return packet_ref, adoption_ref

    def run_packet(self, refs):
        with patch.dict(os.environ, {"TMUX": "test", "TMUX_PANE": "%test"}), patch.object(
                module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "session:window:pane\n")) as tmux:
            result = module.run(*refs)
        self.assertEqual(tmux.call_args.args[0][-3:], ["-t", "%test", "#{session_name}:#{window_id}:#{pane_id}"])
        return result

    def test_failed_actor_is_recorded_once_and_success_is_preserved(self):
        refs = self.packet(["print('ok')", "raise SystemExit(7)"])
        self.assertEqual(self.run_packet(refs), 1)
        output = self.root / "output"
        self.assertEqual(json.loads((output / "actor0/EXECUTION.json").read_text())["returncode"], 0)
        self.assertEqual(json.loads((output / "actor1/EXECUTION.json").read_text())["returncode"], 7)
        self.assertFalse(json.loads((output / "COMPLETE.json").read_text())["all_actors_succeeded"])
        with self.assertRaisesRegex(ValueError, "output_not_exclusive"):
            module.run(*refs)

    def test_partial_spawn_failure_joins_started_child(self):
        refs = self.packet(["import time; time.sleep(0.2); print('survived')", "print('unused')"])
        real_popen = subprocess.Popen
        calls = []

        def launch(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise OSError("simulated spawn failure")
            return real_popen(*args, **kwargs)

        with patch.object(module.subprocess, "Popen", side_effect=launch):
            self.assertEqual(self.run_packet(refs), 1)
        self.assertEqual(len(calls), 2)
        output = self.root / "output"
        self.assertEqual((output / "actor0/stdout").read_text(), "survived\n")
        self.assertTrue((output / "actor0/EXECUTION.json").is_file())
        self.assertTrue((output / "LAUNCH_ERROR.json").is_file())
        process = json.loads((output / "actor0/PROCESS.json").read_text())
        self.assertTrue(process["observations"])
        self.assertEqual(process["intent"], module.pin(output / "actor0/INTENT.json"))
        execution = json.loads((output / "actor0/EXECUTION.json").read_text())
        self.assertFalse(execution["signals_sent"])
        self.assertFalse(execution["automatic_retry"])

    def test_missing_completed_source_does_not_abandon_other_child(self):
        refs = self.packet(["from pathlib import Path; Path(__file__).unlink()",
                            "import time; time.sleep(0.2); print('still completed')"])
        self.assertEqual(self.run_packet(refs), 1)
        output = self.root / "output"
        result = json.loads((output / "actor0/EXECUTION.json").read_text())
        self.assertFalse(result["source_stable"])
        self.assertIn("FileNotFoundError", result["source_error"])
        self.assertEqual((output / "actor1/stdout").read_text(), "still completed\n")

    def test_authority_cannot_enable_retry(self):
        packet_ref, adoption_ref = self.packet(["print('unused')"])
        adoption = module.read(adoption_ref)
        adoption["automatic_retry"] = True
        changed_ref = module.save(self.root / "bad-adoption.json", adoption)
        with self.assertRaisesRegex(ValueError, "retry_not_authorized"):
            module.validate(packet_ref, changed_ref)
        self.assertFalse((self.root / "output").exists())


class ProcessObservationTest(unittest.TestCase):
    def setUp(self):
        self.child = Mock(pid=123)
        self.child.poll.return_value = None
        self.argv = ["python", "actor.py"]
        self.intent = {"path": "/intent", "bytes": 1, "sha256": "fixture"}
        self.seconds = 0.0

    def identity(self, argv, start="456"):
        return {"pid": 123, "start_ticks": start, "state": "R", "argv": argv}

    def sleep(self, seconds):
        self.seconds += seconds

    def observe(self, identities):
        with patch.object(module, "identity", side_effect=identities) as read_identity, \
                patch.object(module, "now", side_effect=lambda: f"at-{self.seconds:.3f}"), \
                patch.object(module.time, "monotonic", side_effect=lambda: self.seconds), \
                patch.object(module.time, "sleep", side_effect=self.sleep):
            result = module.observe_process(self.child, self.argv, self.intent)
        self.child.terminate.assert_not_called()
        self.child.kill.assert_not_called()
        return result, read_identity.call_count

    def test_transient_empty_preserves_initial_identity_and_time(self):
        empty = self.identity([""])
        matching = self.identity(self.argv)
        result, calls = self.observe([empty, matching])
        self.assertEqual(calls, 2)
        self.assertEqual(result["identity_status"], "matched")
        self.assertEqual(result["identity"], matching)
        self.assertEqual(result["at"], "at-0.010")
        self.assertEqual(result["observations"], [
            {"at": "at-0.000", "identity": empty},
            {"at": "at-0.010", "identity": matching}])

    def test_initial_match_needs_no_second_observation(self):
        result, calls = self.observe([self.identity(self.argv)])
        self.assertEqual(calls, 1)
        self.assertEqual(result["identity_status"], "matched")
        self.assertEqual(result["observation_elapsed_seconds"], 0)

    def test_persistent_mismatch_times_out_without_replacing_argv(self):
        mismatch = self.identity(["another-command"])
        result, calls = self.observe(lambda pid: mismatch)
        self.assertGreater(calls, 1)
        self.assertEqual(result["identity_status"], "timeout")
        self.assertEqual(result["argv_status"], "mismatch")
        self.assertEqual(result["identity"], mismatch)
        self.assertEqual(result["observation_elapsed_seconds"], 0.5)
        self.assertTrue(all(row["identity"] == mismatch for row in result["observations"]))

    def test_persistent_empty_is_distinct_from_other_mismatch(self):
        result, _ = self.observe(lambda pid: self.identity([""]))
        self.assertEqual(result["identity_status"], "timeout")
        self.assertEqual(result["argv_status"], "empty")

    def test_fast_exit_records_actual_returncode_without_retry(self):
        self.child.poll.return_value = 7
        result, calls = self.observe([FileNotFoundError("process exited")])
        self.assertEqual(calls, 1)
        self.assertEqual(result["identity_status"], "exited")
        self.assertEqual(result["returncode_at_observation"], 7)
        self.assertEqual(result["identity"], {"pid": 123, "already_exited": True})
        self.assertIn("FileNotFoundError", result["observations"][0]["error"])

    def test_exit_after_empty_does_not_observe_reused_pid(self):
        self.child.poll.side_effect = [None, 0]
        result, calls = self.observe([self.identity([""])])
        self.assertEqual(calls, 1)
        self.assertEqual(result["identity_status"], "exited")
        self.assertEqual(result["returncode_at_observation"], 0)
        self.assertEqual(result["at"], "at-0.000")

    def test_different_start_ticks_cannot_resolve_empty_observation(self):
        result, calls = self.observe([self.identity([""]), self.identity(self.argv, start="457")])
        self.assertEqual(calls, 2)
        self.assertEqual(result["identity_status"], "identity_changed")
        self.assertEqual(result["observations"][0]["identity"]["start_ticks"], "456")
        self.assertEqual(result["observations"][1]["identity"]["start_ticks"], "457")
        self.assertEqual(result["identity"]["start_ticks"], "456")
        self.assertEqual(result["argv_status"], "empty")
        self.assertEqual(result["at"], "at-0.000")

    def test_unavailable_identity_is_not_reported_as_process_exit(self):
        result, calls = self.observe([PermissionError("denied")])
        self.assertEqual(calls, 1)
        self.assertEqual(result["identity_status"], "unavailable")
        self.assertFalse(result["identity"]["already_exited"])
        self.assertIsNone(result["returncode_at_observation"])


if __name__ == "__main__":
    unittest.main()
