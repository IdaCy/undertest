import json
import tempfile
import unittest
from pathlib import Path

from emergence.checkpoints.provenance import load_checkpoint
from emergence.checkpoints.provenance import load_panel
from emergence.checkpoints.provenance import save_snapshot
from emergence.checkpoints.provenance import source_hashes
from emergence.checkpoints.provenance import verify_server_receipt


class DevelopmentProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.checkpoint = {
            "checkpoint_id": "sft", "model_id": "org/model", "model_revision": "a" * 40,
            "tokenizer_id": "org/model", "tokenizer_revision": "b" * 40,
            "stage": "sft", "lineage": "candidate",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_mutable_revisions_and_duplicate_checkpoint_ids_fail(self):
        path = self.root / "lineage.json"
        path.write_text(json.dumps({"checkpoints": [self.checkpoint]}))
        selected, digest = load_checkpoint(path, "sft")
        self.assertEqual(selected, self.checkpoint)
        self.assertEqual(len(digest), 64)
        for rows in (
            [self.checkpoint, self.checkpoint],
            [{**self.checkpoint, "model_revision": "main"}],
            [{**self.checkpoint, "tokenizer_revision": "step_100"}],
        ):
            path.write_text(json.dumps({"checkpoints": rows}))
            with self.assertRaises(ValueError):
                load_checkpoint(path, "sft")

    def test_panel_rejects_collision_and_invalid_seed(self):
        sample = {"id": "case", "input": "Request", "metadata": {
            "condition": "ordinary_broad", "family": "numeric", "instance_id": "a",
            "replicate": 0, "seed": 12,
        }}
        path = self.root / "panel.json"
        for rows in ([sample, sample], [{**sample, "metadata": {**sample["metadata"], "seed": True}}]):
            path.write_text(json.dumps({"schema_version": 1, "assay_version": "v1", "samples": rows}))
            with self.assertRaises(ValueError):
                load_panel(path)

    def test_receipt_must_bind_alias_and_tokenizer_to_checkpoint(self):
        receipt = {
            **self.checkpoint, "served_model": "traindev-sft-" + "a" * 12,
            "hostname": "remote", "precision": "bfloat16", "server_version": "vllm/test",
            "launch_argv": ["vllm", "serve"], "verified_utc": "2026-09-30T04:00:00Z",
        }
        path = self.root / "receipt.json"
        path.write_text(json.dumps(receipt))
        self.assertEqual(verify_server_receipt(path, self.checkpoint, "raw"), receipt)
        with self.assertRaises(ValueError):
            verify_server_receipt(path, self.checkpoint, "native-chat")
        for changes in ({"served_model": "generic-alias"}, {"tokenizer_revision": "c" * 40}):
            path.write_text(json.dumps({**receipt, **changes}))
            with self.assertRaises(ValueError):
                verify_server_receipt(path, self.checkpoint, "raw")

    def test_archive_guards_against_changed_bytes_and_corruption(self):
        source = self.root / "fixture.py"
        source.write_text("answer = 1\n")
        hashes = source_hashes([source], self.root)
        output = self.root / "run"
        output.mkdir()
        source.write_text("answer = 2\n")
        with self.assertRaises(ValueError):
            save_snapshot(output, hashes, self.root)
        source.write_text("answer = 1\n")
        save_snapshot(output, hashes, self.root)
        save_snapshot(output, hashes, self.root)
        with self.assertRaises(ValueError):
            save_snapshot(output, {"fixture.py": "0" * 64}, self.root)


if __name__ == "__main__":
    unittest.main()
