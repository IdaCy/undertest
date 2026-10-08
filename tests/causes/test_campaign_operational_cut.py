import json
from pathlib import Path

import tempfile
import unittest

from causes import campaign_operational_cut as cut
from causes import campaign_registry


def write(path, value):
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    path.write_bytes(raw)
    return cut.pin(path)


def fixture(tmp_path):
    original = {"run": "runs/fixed/model/original", "verified_at": "2026-01-01T00:00:00+00:00"}
    new = {"run": "runs/successor/model/new", "verified_at": "2026-01-01T01:00:00+00:00"}
    corpus = {"run": "runs/corpus/model/unselected", "verified_at": "2026-01-01T01:00:00+00:00"}
    prefix = (json.dumps(original) + "\n").encode()
    prior_index = write(tmp_path / "prior_index.jsonl", prefix)
    prior_joins = write(tmp_path / "prior_joins.json", {"model": [original]})
    reader_prefix = b'{"event":"retained"}\n'
    prior_reader = write(tmp_path / "prior_reader.jsonl", reader_prefix)
    prior = write(tmp_path / "prior.json", {
        "accepted_index_consumed": prior_index, "fixed_index_joins": prior_joins,
        "reader_journal_prefix": prior_reader})
    prior_review = write(tmp_path / "prior_review.json", {"accepted_metadata_observation": True, "observation": prior})
    jobs = write(tmp_path / "jobs.json", [{"allocation": {"run_id": name}} for name in ("original", "new")])
    protocol = write(tmp_path / "protocol.json", {"artifacts": [jobs]})
    index = tmp_path / "index.jsonl"
    write(index, prefix + (json.dumps(new) + "\n" + json.dumps(corpus) + "\n").encode() + b'{"torn":')
    registry = write(tmp_path / "registry.json", {
        "schema": "causes.output_root_registry.v1", "fixed_roots": {"model": ["runs/fixed/model", "runs/successor/model"]},
        "corpus_roots": ["runs/corpus/model"], "sync_roots": ["runs/fixed/model", "runs/successor/model", "runs/corpus/model"]})
    health_source = write(tmp_path / "health.py", b'''from pathlib import Path
import hashlib,json
def health():
    journal=HERE/'READER_JOURNAL_PREFIX.jsonl'
    raw=b'{"event":"retained"}\\n{"event":"new"}\\n'
    journal.write_bytes(raw)
    value={'at':'2026-01-01T01:00:00+00:00','events':{'finished':1},
      'instrument_statuses':{'validated':2,'technical':1},
      'missing_calls':[{'reason':'evidence_not_exact_channel_substring:channel'}],
      'missing_contexts':[], 'last_event':{'at_unix':1,'event':'finished'},
      'daemon_read_returncode':0,
      'journal_prefix':{'path':str(journal),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}}
    output=HERE/'HEALTH.json';output.write_text(json.dumps(value));return output
''')
    heartbeat = write(tmp_path / "heartbeat.json", {"delivered": True})
    phase = write(tmp_path / "phase.json", {"accepted": True})
    binding = {
        "schema": "causes.operational_cut_binding.v1", "source": cut.pin(Path(cut.__file__).resolve()),
        "campaign_root": str(tmp_path), "output_directory": str(tmp_path / "cut"),
        "not_before": "2020-01-01T00:00:00+00:00", "phase_inputs": {"phase": phase}, "heartbeat": heartbeat,
        "protocol": protocol, "jobs": {"model": {"source": jobs, "expected_count": 2}},
        "accepted_index": str(index), "registry": registry,
        "registry_source": cut.pin(Path(campaign_registry.__file__).resolve()),
        "reader_health_source": health_source, "preserved_artifacts": [],
        "prior_observation": prior, "prior_review": prior_review}
    return binding


def invoke(tmp_path, binding):
    bound = write(tmp_path / "binding.json", binding)
    review = write(tmp_path / "review.json", {"accepted_operational_cut_binding": True, "binding": bound})
    return cut.capture(bound, review)


class OperationalCutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name)

    def test_successor_membership_corpus_exclusion_and_torn_tail(self):
        tmp_path = self.tmp_path
        binding = fixture(tmp_path)
        ref = invoke(tmp_path, binding)
        result = cut.checked(ref)
        assert result["fixed_ids"]["model"]["accepted_generated_final_ids"] == 2
        assert result["accepted_index_rows"] == 3
        assert result["unconsumed_torn_tail_bytes"] == len(b'{"torn":')
        assert result["prior_comparison"]["model"]["new_fixed_final_ids"] == 1
        assert result["reader_observation"]["missing_reason_counts"] == {"quote": 1, "origin": 0, "other": 0}
        assert "answer" not in result and "outcome" not in result
        with self.assertRaises(FileExistsError):
            cut.capture(cut.pin(tmp_path / "binding.json"), cut.pin(tmp_path / "review.json"))


    def test_duplicate_original_id_refused_without_observation(self):
        tmp_path = self.tmp_path
        binding = fixture(tmp_path)
        index = Path(binding["accepted_index"])
        raw = index.read_bytes().split(b'{"torn":')[0]
        index.write_bytes(raw + b'{"run":"runs/successor/model/original","verified_at":"later"}\n')
        with self.assertRaisesRegex(ValueError, "multiple_custody_rows_for_original_ID"):
            invoke(tmp_path, binding)
        assert not (tmp_path / "cut/OBSERVATION.json").exists()


    def test_changed_prior_index_refused_without_observation(self):
        tmp_path = self.tmp_path
        binding = fixture(tmp_path)
        index = Path(binding["accepted_index"])
        index.write_bytes(index.read_bytes().replace(b"2026-01-01T00", b"2026-01-02T00"))
        with self.assertRaisesRegex(ValueError, "prior_index_prefix_changed"):
            invoke(tmp_path, binding)
        assert not (tmp_path / "cut/OBSERVATION.json").exists()


    def test_changed_reader_prefix_refused_without_observation(self):
        tmp_path = self.tmp_path
        binding = fixture(tmp_path)
        helper = Path(binding["reader_health_source"]["path"])
        helper.write_bytes(helper.read_bytes().replace(b'event":"retained', b'event":"replaced'))
        binding["reader_health_source"] = cut.pin(helper)
        with self.assertRaisesRegex(ValueError, "prior_reader_prefix_changed"):
            invoke(tmp_path, binding)
        assert not (tmp_path / "cut/OBSERVATION.json").exists()


    def test_unaccepted_binding_refused_before_output(self):
        tmp_path = self.tmp_path
        binding = fixture(tmp_path)
        bound = write(tmp_path / "binding.json", binding)
        review = write(tmp_path / "review.json", {"accepted_operational_cut_binding": False, "binding": bound})
        with self.assertRaisesRegex(ValueError, "cut_binding_not_reviewed"):
            cut.capture(bound, review)
        assert not (tmp_path / "cut").exists()


    def test_early_binding_refused_before_output(self):
        tmp_path = self.tmp_path
        binding = fixture(tmp_path)
        binding["not_before"] = "2999-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "cut_before_due_time"):
            invoke(tmp_path, binding)
        assert not (tmp_path / "cut").exists()

if __name__ == "__main__":
    unittest.main()
