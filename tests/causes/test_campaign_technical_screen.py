import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from causes import campaign_technical_screen as screen


class TechnicalScreenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config, self.observations, self.rows = self.fixture()

    def save(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = value if isinstance(value, bytes) else (json.dumps(value) + "\n").encode()
        path.write_bytes(raw)
        return screen._pin(path)

    def fixture(self):
        rows = []
        for i, (error, probes) in enumerate(((None, []), ("TimeoutError: PRIVATE", []),
                                            (None, [{"technical_status": "failure", "answer": "PRIVATE"}]))):
            rid = f"fixed_{i}"
            summary = self.save(f"runs/{rid}/summary.json", {
                "run_id": rid, "actor_error": error, "probes": probes, "semantic_answer": "PRIVATE"})
            rows.append({"run": f"runs/fixed/mi27/{rid}", "path": str(Path(summary["path"]).parent),
                         "summary_sha256": summary["sha256"]})
        cuts, refs = [], []
        for i, subset in enumerate((rows[:1], rows)):
            observation = {"fixed_protocol": {"identity": "frozen"}, "original_job_pins": [],
                "fixed_ids": {"mi27": {"assigned": 3, "accepted_generated_final_ids": len(subset),
                                         "registered_roots": ["runs/fixed/mi27"]}},
                "fixed_index_joins": self.save(f"joins{i}.json", {"mi27": subset}),
                "accepted_index_consumed": self.save(f"index{i}.jsonl", b"".join(
                    (json.dumps(row) + "\n").encode() for row in subset))}
            if i:
                observation["prior_operational_tick"] = refs[0]
                observation["prior_operational_review"] = prior_review
            ref = self.save(f"observation{i}.json", observation)
            review = self.save(f"review{i}.json", {"accepted_metadata_observation": True, "observation": ref})
            if not i:
                prior_review = review
            cuts.append(observation)
            refs.append(ref)
        config = {"schema": "causes.technical_screen_bindings.v1", "source": screen._pin(Path(screen.__file__).resolve()),
            "model": "mi27", "registered_roots": ["runs/fixed/mi27"], "assigned": 3,
            "prior_observation": refs[0], "current_observation": refs[1], "prior_review": prior_review,
            "current_review": review, "output": str(self.root / "RECEIPT.json")}
        return config, cuts, rows

    def run_screen(self):
        ref = self.save("bindings.json", self.config)
        return screen.screen(Path(ref["path"]), ref["sha256"])

    def rebind_current(self):
        ref = self.save("observation1.json", self.observations[1])
        self.config["current_observation"] = ref
        self.config["current_review"] = self.save("review1.json", {"accepted_metadata_observation": True, "observation": ref})

    def test_positive_flags_without_semantics_or_counter_gate(self):
        result = self.run_screen()
        raw = Path(result["path"]).read_bytes()
        value = json.loads(raw)
        self.assertEqual(value["fixed_counts"], {"prior": 1, "current": 3, "appended": 2})
        self.assertEqual((value["positive_case_count"], value["actor_error_summary_count"], value["failed_probe_record_count"]), (2, 1, 1))
        self.assertNotIn(b"PRIVATE", raw)
        self.assertNotIn(b"semantic_answer", raw)
        with self.assertRaisesRegex(ValueError, "output_not_fresh"):
            self.run_screen()
        self.assertEqual(Path(result["path"]).read_bytes(), raw)

    def test_no_append_emits_one_negative_receipt(self):
        self.observations[1]["fixed_ids"] = self.observations[0]["fixed_ids"]
        self.observations[1]["fixed_index_joins"] = self.observations[0]["fixed_index_joins"]
        self.observations[1]["accepted_index_consumed"] = self.observations[0]["accepted_index_consumed"]
        self.rebind_current()
        result = json.loads(Path(self.run_screen()["path"]).read_bytes())
        self.assertEqual(result["positive_cases"], [])
        self.assertEqual(result["fixed_counts"]["appended"], 0)

    def test_extra_index_row_content_is_not_projected(self):
        self.rows[1]["semantic_payload"] = "PRIVATE_ROW_SENTINEL"
        self.observations[1]["fixed_index_joins"] = self.save("joins1.json", {"mi27": self.rows})
        self.observations[1]["accepted_index_consumed"] = self.save("index1.jsonl", b"".join(
            (json.dumps(row) + "\n").encode() for row in self.rows))
        self.rebind_current()
        raw = Path(self.run_screen()["path"]).read_bytes()
        self.assertNotIn(b"PRIVATE_ROW_SENTINEL", raw)
        self.assertNotIn(b"semantic_payload", raw)
        positive = json.loads(raw)["positive_cases"][0]
        self.assertEqual(set(positive), {"run_id", "summary", "actor_error_present", "failed_probe_records"})

    def test_reject_mutated_summary_without_receipt(self):
        Path(self.rows[1]["path"], "summary.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "summary_custody_hash_mismatch"):
            self.run_screen()
        self.assertFalse(Path(self.config["output"]).exists())

    def test_reject_changed_prior_row_even_with_fresh_pins(self):
        altered = [dict(row) for row in self.rows]
        altered[0]["summary_sha256"] = "0" * 64
        self.observations[1]["fixed_index_joins"] = self.save("joins1.json", {"mi27": altered})
        self.rebind_current()
        with self.assertRaisesRegex(ValueError, "prior_fixed_row_changed"):
            self.run_screen()

    def test_reject_duplicate_original_id(self):
        altered = self.rows + [dict(self.rows[1])]
        self.observations[1]["fixed_index_joins"] = self.save("joins1.json", {"mi27": altered})
        self.rebind_current()
        with self.assertRaisesRegex(ValueError, "duplicate_fixed_id"):
            self.run_screen()

    def test_unaccepted_cut_cannot_publish(self):
        self.config["current_review"] = self.save("review1.json", {
            "accepted_metadata_observation": False, "observation": self.config["current_observation"]})
        with self.assertRaisesRegex(ValueError, "observation_review_not_accepted"):
            self.run_screen()
        self.assertFalse(Path(self.config["output"]).exists())

    def expand_roots(self):
        self.config["schema"] = "causes.technical_screen_bindings.v2"
        self.config["prior_registered_roots"] = ["runs/fixed/mi27"]
        self.config["registered_roots"] = ["runs/fixed/mi27", "runs/return/mi27"]
        self.observations[1]["fixed_ids"]["mi27"]["registered_roots"] = self.config["registered_roots"]
        for row in self.rows[1:]:
            row["run"] = row["run"].replace("runs/fixed/mi27/", "runs/return/mi27/")
        self.observations[1]["fixed_index_joins"] = self.save("joins1.json", {"mi27": self.rows})
        self.observations[1]["accepted_index_consumed"] = self.save("index1.jsonl", b"".join(
            (json.dumps(row) + "\n").encode() for row in self.rows))
        self.rebind_current()

    def test_explicit_expansion_screens_only_exact_new_rows(self):
        self.expand_roots()
        value = json.loads(Path(self.run_screen()["path"]).read_bytes())
        self.assertEqual(value["schema"], "causes.fixed_append_technical_screen.v2")
        self.assertEqual(value["prior_registered_roots"], ["runs/fixed/mi27"])
        self.assertEqual(value["registered_roots"], self.config["registered_roots"])
        self.assertEqual(value["fixed_counts"], {"prior": 1, "current": 3, "appended": 2})
        self.assertEqual(value["positive_case_count"], 2)
        self.assertEqual([row["run_id"] for row in value["scanned_summaries"]], ["fixed_1", "fixed_2"])

    def test_v1_still_refuses_root_expansion(self):
        self.expand_roots()
        self.config["schema"] = "causes.technical_screen_bindings.v1"
        self.config.pop("prior_registered_roots")
        with self.assertRaisesRegex(ValueError, "fixed_scope_changed"):
            self.run_screen()

    def test_expansion_rejects_removal_and_wrong_prior_scope(self):
        self.expand_roots()
        self.config["registered_roots"] = ["runs/return/mi27"]
        with self.assertRaisesRegex(ValueError, "invalid_prior_roots_or_root_removal"):
            self.run_screen()
        self.config["registered_roots"] = ["runs/fixed/mi27", "runs/return/mi27"]
        self.config["prior_registered_roots"] = ["runs/return/mi27"]
        with self.assertRaisesRegex(ValueError, "fixed_scope_changed"):
            self.run_screen()
        self.config["prior_registered_roots"] = ["runs/fixed/mi27", "runs/fixed/mi27"]
        with self.assertRaisesRegex(ValueError, "invalid_prior_roots_or_root_removal"):
            self.run_screen()

    def test_expansion_rejects_rehomed_or_duplicate_prior_id(self):
        self.expand_roots()
        self.rows[0] = dict(self.rows[0], run="runs/return/mi27/fixed_0")
        self.observations[1]["fixed_index_joins"] = self.save("joins1.json", {"mi27": self.rows})
        self.rebind_current()
        with self.assertRaisesRegex(ValueError, "prior_fixed_row_changed"):
            self.run_screen()
        self.rows[0] = dict(self.rows[0], run="runs/fixed/mi27/fixed_0")
        self.rows[1] = dict(self.rows[1], run="runs/return/mi27/fixed_0")
        self.observations[1]["fixed_index_joins"] = self.save("joins1.json", {"mi27": self.rows})
        self.rebind_current()
        with self.assertRaisesRegex(ValueError, "duplicate_fixed_id"):
            self.run_screen()

    def test_expansion_rejects_index_prefix_change_and_unaccepted_cut(self):
        self.expand_roots()
        self.observations[1]["accepted_index_consumed"] = self.save("index1.jsonl", b"{}\n")
        self.rebind_current()
        with self.assertRaisesRegex(ValueError, "prior_index_prefix_changed"):
            self.run_screen()
        self.config["current_review"] = self.save("review1.json", {
            "accepted_metadata_observation": False, "observation": self.config["current_observation"]})
        with self.assertRaisesRegex(ValueError, "observation_review_not_accepted"):
            self.run_screen()
        self.assertFalse(Path(self.config["output"]).exists())

    def test_import_has_no_execution(self):
        spec = importlib.util.spec_from_file_location("screen_import_fixture", screen.__file__)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertFalse(Path(self.config["output"]).exists())


if __name__ == "__main__":
    unittest.main()
