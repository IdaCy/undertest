import copy
import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from causes import campaign_storage_probe as probe


def checkpoint(root, content=probe._CONTENT):
    directory = root / "sandbox/checkpoints/storage_probe"
    directory.mkdir(parents=True)
    archive = directory / "workspace_upper.tar"
    with tarfile.open(archive, "w") as stream:
        member = tarfile.TarInfo("./" + probe._MARKER)
        member.size = len(content)
        stream.addfile(member, io.BytesIO(content))
    row = {"complete": True, "workspace_method": "overlayfs", "errors": [],
           "unpause_returncode": 0,
           "archives": [{"role": "workspace_upper", **probe.restore.file_pin(archive)}]}
    (directory / "CHECKPOINT.json").write_text(json.dumps(row))
    return row


class StorageProbeTests(unittest.TestCase):
    def test_checkpoint_requires_exact_marker_and_archive_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = checkpoint(root)
            probe._verify_checkpoint(good)
            wrong = copy.deepcopy(good)
            wrong["archives"][0]["sha256"] = "0" * 64
            with self.assertRaises(ValueError):
                probe._verify_checkpoint(wrong)
            bad = checkpoint(root / "bad", b"different\n")
            with self.assertRaisesRegex(ValueError, "marker_changed"):
                probe._verify_checkpoint(bad)

    def test_existing_destination_is_never_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "destination_exists"):
                probe._new_path(tmp)

    def test_source_inventory_is_not_mutable_by_caller(self):
        before = probe.source_inventory()
        changed = probe.source_inventory()
        changed["probe"]["sha256"] = "0" * 64
        self.assertEqual(before, probe.source_inventory())

    def test_prepare_pins_namespace_seed_and_floor_before_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output, seed = root / "storage_qualification_test", root / "seed"
            output.mkdir()
            seed.mkdir()
            spec_path, binding_path, plan_path = (root / name for name in ("spec.json", "binding.json", "plan.json"))
            spec_path.write_text(json.dumps({"seed_path": str(seed)}))
            binding_path.write_text(json.dumps({"output": str(output), "root_device": str(root.stat().st_dev), "floor_bytes": 10}))
            plan = {"schema": "causes.storage_qualification.v1", "sources": probe.source_inventory(),
                    "spec": probe.restore.file_pin(spec_path), "storage_binding": probe.restore.file_pin(binding_path),
                    "run_id": "storage_qualification_test", "original": str(output / "original"),
                    "copy": str(root / "copy"), "evidence": str(output / "evidence")}

            def write(value):
                plan_path.write_text(json.dumps(value))
                return probe.restore.digest_file(plan_path)

            with patch.object(probe.storage, "StorageGuard", return_value=SimpleNamespace(check=lambda: 100)):
                digest = write(plan)
                probe.prepare(plan_path, digest)
                with self.assertRaisesRegex(ValueError, "plan_changed"):
                    probe.prepare(plan_path, "0" * 64)
                bad = copy.deepcopy(plan)
                bad["copy"] = str(seed / "copy")
                with self.assertRaisesRegex(ValueError, "overlaps_seed"):
                    probe.prepare(plan_path, write(bad))
                bad = copy.deepcopy(plan)
                bad["sources"]["sandbox"]["sha256"] = "0" * 64
                with self.assertRaisesRegex(ValueError, "source_changed"):
                    probe.prepare(plan_path, write(bad))
            with patch.object(probe.storage, "StorageGuard", return_value=SimpleNamespace(check=lambda: 0)):
                with self.assertRaisesRegex(ValueError, "floor_unavailable"):
                    probe.prepare(plan_path, write(plan))
            self.assertEqual(list(output.iterdir()), [])
            self.assertEqual(list(seed.iterdir()), [])

    def exercise(self, fail=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original, copied, evidence = (root / name for name in ("original", "copy", "evidence"))
            seed = root / "seed"
            seed.mkdir()
            (seed / "original.txt").write_text("unchanged\n")
            plan_path = root / "plan.json"
            plan_path.write_text("{}")
            calls = []

            class Guard:
                binding = {"floor_bytes": 1}
                last_observation = {"root_free_bytes": 100, "output_free_bytes": 100}

                def check(self):
                    calls.append("guard")
                    return 100

            class Box:
                closed = False

                def __init__(self, spec, output, run_id, files):
                    self.root = output
                    output.mkdir()
                    self.spec = spec
                    calls.append("constructed")

                def start(self):
                    calls.append("start")
                    return {"workspace_method": "overlayfs"}

                def run(self, argv, timeout):
                    calls.append("read")
                    return {"returncode": 0, "timeout": False, "stdout": probe._CONTENT.decode()}

                def checkpoint(self, name):
                    calls.append("checkpoint")
                    return checkpoint(self.root, b"bad" if fail else probe._CONTENT)

                def close(self):
                    calls.append("close")
                    self.closed = True
                    return {"state": "closed", "closure_errors": [], "overlay_mounted": False,
                            "retained_container": True}

            prepared = ({"run_id": "storage_qualification_test"}, {"seed_path": str(seed)},
                        Guard(), original, copied, evidence)
            with patch.object(probe, "prepare", return_value=prepared), patch.object(probe.sandbox, "Sandbox", Box):
                if fail:
                    with self.assertRaisesRegex(ValueError, "marker_changed"):
                        probe.run(plan_path, "unused")
                else:
                    result = probe.run(plan_path, "unused")
                    self.assertEqual(probe.restore.file_pin(evidence / "RESULT.json"), result)
            report = json.loads((evidence / "RESULT.json").read_bytes())
            self.assertEqual(report["passed"], not fail)
            self.assertEqual(report["closure"]["state"], "closed")
            self.assertEqual(calls.count("close"), 1)
            self.assertLess(calls.index("checkpoint"), calls.index("close"))
            self.assertEqual(copied.exists(), not fail)
            self.assertEqual((seed / "original.txt").read_text(), "unchanged\n")
            if not fail:
                self.assertTrue(report["copy_verified"])
                self.assertTrue(report["seed_unchanged"])
                self.assertEqual(report["model_requests"], 0)
                self.assertEqual(calls[-1], "guard")

    def test_actual_local_copy_is_verified_after_normal_closure(self):
        self.exercise()

    def test_failed_checkpoint_preserves_result_and_closes_without_copy(self):
        self.exercise(fail=True)


if __name__ == "__main__":
    unittest.main()
