import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


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


if __name__ == "__main__":
    unittest.main()
