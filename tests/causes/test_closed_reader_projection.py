import copy
import hashlib
import json
from pathlib import Path

import tempfile
import unittest

from src.causes.campaign_closed_reader_projection import (
    project_closed_reader, read_pinned_bytes, write_projection,
)


def make_case(tmp_path):
    def put(name, value):
        path = tmp_path / name
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        path.write_bytes(raw)
        return dict(path=str(path), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())

    prior = put("old.json", {"claimed_keys": ["old_failed"]})
    prior_peer = put("old_review.json", dict(accepted_finite_reader_route_closed_cut=True,
                                            actual=prior, findings=[]))
    cuts = [dict(actual=prior, review=prior_peer)]
    source = put("capture.py", b"pass\n")
    ready = put("READY.json", [{"measurement_key": key} for key in ("old_failed", "a", "b")])
    prepared = put("prepared.json", dict(ready=ready, counts=dict(
        copied=4, reader_ready=3, reader_requests=9, selected=6, pending=2,
        self_requests=0, unavailable=1)))
    prepared_peer = put("prepared_peer.json", dict(accepted_fresh_candidate_reader_inputs=True,
                                                   preparation=prepared, findings=[]))
    plan = put("plan.json", dict(prepared=prepared, prepared_review=prepared_peer,
                                additive_ready=ready, source=source, output=str(tmp_path / "reader"),
                                route_predecessor=prior, intervening_reader_cuts=cuts,
                                previous_cohort_claimed_keys=["old_failed"], expected_unclaimed_contexts=2))
    plan_peer = put("plan_peer.json", dict(accepted_fresh_candidate_reader_plan=True,
                                         plan=plan, findings=[]))
    events = [dict(event="claimed", key="a"), dict(event="claimed", key="b"),
              dict(event="unresolved_current_reader_attempt", key="b"),
              dict(event="finished", key="a"),
              dict(event="finite_selection_complete", new_admissions_stopped=True, reason=None)]
    journal = put("journal.jsonl", b"".join(json.dumps(e).encode() + b"\n" for e in events))
    result = put("RESULT.json", dict(claimed=["a", "b"], finished_or_unresolved=["b", "a"],
                                    unclaimed=[], stop_reason=None, quarantine=None,
                                    consecutive_failed_contexts=1))
    execution = put("execution.json", dict(returncode=0, source_stable=True))
    complete = put("COMPLETE.json", dict(all_actors_succeeded=True, executions=[execution]))
    actual = dict(schema="causes.finite_reader_route_closed_cut.v1", source=source,
                  plan=plan, plan_review=plan_peer, reader_source=source, root=str(tmp_path / "reader"),
                  journal=journal, journal_original=dict(path=str(tmp_path / "original_journal.jsonl")),
                  runtime_result=result, prepared=prepared, complete=complete, execution=execution,
                  route_predecessor=prior, route_state_carry={"retained": True},
                  initial_consecutive_failed_contexts=1, final_consecutive_failed_contexts=1,
                  native_failure_events=[{"key": "b", "preserved": True}],
                  annotation_missing_contexts=["a"], claimed=2, ready=2, finished_or_unresolved=2,
                  active=0, unclaimed=0, unclaimed_keys=[], all_claims_accounted=True,
                  stop_reason=None, quarantine=None, owned_pause=None, final_for_reader_analysis=False,
                  claimed_keys=["a", "b"], assigned=4, observed_at="2026-01-01T00:00:00+00:00")
    closure = put("actual.json", actual)
    review = dict(actual, accepted_finite_reader_route_closed_cut=True, actual=closure, findings=[])
    review["annotation_missing_contexts"] = len(actual["annotation_missing_contexts"])
    closure_review = put("review.json", review)
    base = put("base.json", dict(source=source, assignments=ready, manifest=prepared,
                                 outputs={}, physical_measurements=12))
    base_peer = put("base_peer.json", dict(accepted_individual_variant_measurement_readout=True,
                                          actual=base, findings=[], source=source, assignments=ready,
                                          manifest=prepared, outputs={}))
    descriptor = dict(closure=closure, closure_review=closure_review, native_capture_source=source,
                      prior_fresh_closures=cuts, base_result=base, base_review=base_peer, progress=None)
    return descriptor, put, actual, review, events


def reseal(case, actual):
    descriptor, put, _, review, _ = case
    descriptor = copy.deepcopy(descriptor)
    descriptor["closure"] = put("altered_actual.json", actual)
    changed_review = dict(review, **actual)
    changed_review["actual"] = descriptor["closure"]
    changed_review["annotation_missing_contexts"] = len(actual["annotation_missing_contexts"])
    descriptor["closure_review"] = put("altered_review.json", changed_review)
    return descriptor




class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = make_case(self.root)

    def test_no_progress_is_valid_and_failed_attempts_are_preserved(self):
        case = self.case
        tmp_path = self.root
        descriptor, _, original, _, _ = case
        before = copy.deepcopy(descriptor)
        actual, accounting = project_closed_reader(descriptor)
        assert actual["ready"] == 3
        assert actual["admission_ready"] == actual["finished_or_unresolved"] == 2
        assert actual["retained_prior_cohort_claimed"] == 1
        assert actual["native_failure_events"] == original["native_failure_events"]
        assert actual["annotation_missing_contexts"] == ["a"]
        assert actual["final_consecutive_failures"] == 1
        assert accounting["prior_terminal_contexts"] is None
        assert accounting["conditional_new_slots"] == 6
        assert accounting["conditional_total_slots"] == 18
        assert descriptor == before


    def test_incomplete_or_repeated_claims_refuse(self):
        case = self.case
        tmp_path = self.root
        for change, reason in [
            ({"active": 1}, "closed_claim_accounting"),
            ({"all_claims_accounted": False}, "closed_claim_accounting"),
            ({"quarantine": {"reason": "failure_limit"}}, "natural_closure_required"),
            ({"claimed_keys": ["a", "a"]}, "journal_claim_set"),
        ]:
            with self.subTest(change=change):
                actual = dict(case[2], **change)
                with self.assertRaisesRegex(ValueError, reason):
                    project_closed_reader(reseal(case, actual))


    def test_replayed_failed_prior_key_refuses(self):
        case = self.case
        tmp_path = self.root
        descriptor, put, actual, _, _ = case
        plan = json.loads(read_pinned_bytes(actual["plan"]))
        plan["previous_cohort_claimed_keys"] = ["old_failed", "a"]
        plan_ref = put("bad_plan.json", plan)
        plan_peer = put("bad_plan_peer.json", dict(accepted_fresh_candidate_reader_plan=True,
                                                 plan=plan_ref, findings=[]))
        changed = dict(actual, plan=plan_ref, plan_review=plan_peer)
        with self.assertRaisesRegex(ValueError, "claimed_attempt_replay"):
            project_closed_reader(reseal(case, changed))


    def test_optional_progress_requires_exact_same_journal_prefix(self):
        case = self.case
        tmp_path = self.root
        descriptor, put, actual, _, events = case
        prefix = put("prefix.jsonl", b"".join(json.dumps(e).encode() + b"\n" for e in events[:3]))
        p = dict(plan=actual["plan"], root=actual["root"], ready=2, finished_or_unresolved=1,
                 journal=dict(saved=prefix))
        progress = put("progress.json", p)
        peer = put("progress_peer.json", dict(accepted_fresh_candidate_reader_progress=True,
                                             accepted_saved_corpus_reader_progress=True,
                                             actual=progress, findings=[]))
        descriptor["progress"] = dict(actual=progress, review=peer)
        assert project_closed_reader(descriptor)[1]["prior_terminal_contexts"] == 1
        p["journal"]["saved"] = put("unrelated.jsonl", b'{"event":"claimed","key":"z"}\n')
        descriptor["progress"]["actual"] = put("progress2.json", p)
        descriptor["progress"]["review"] = put("progress_peer2.json", dict(
            accepted_fresh_candidate_reader_progress=True, accepted_saved_corpus_reader_progress=True,
            actual=descriptor["progress"]["actual"], findings=[]))
        with self.assertRaisesRegex(ValueError, "progress_prefix"):
            project_closed_reader(descriptor)


    def test_changed_pin_and_duplicate_json_refuse(self):
        case = self.case
        tmp_path = self.root
        descriptor, put, _, _, _ = case
        broken = copy.deepcopy(descriptor)
        broken["closure"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "pin_changed"):
            project_closed_reader(broken)
        broken["closure"] = put("duplicate.json", b'{"active":0,"active":1}')
        with self.assertRaisesRegex(ValueError, "duplicate_json_key"):
            project_closed_reader(broken)


    def test_writer_refuses_output_reuse_and_grants_no_acceptance(self):
        case = self.case
        tmp_path = self.root
        descriptor, put, _, _, _ = case
        reference = put("descriptor.json", descriptor)
        output = tmp_path / "projection"
        actual, preparation = write_projection(reference, output)
        saved = json.loads(read_pinned_bytes(actual))
        assert saved["descriptor"] == reference
        assert not any(key.startswith("accepted_") for key in saved)
        assert json.loads(read_pinned_bytes(preparation))["native_actual"] == descriptor["closure"]
        before = {path.name: path.read_bytes() for path in output.iterdir()}
        with self.assertRaisesRegex(ValueError, "exclusive_output_required"):
            write_projection(reference, output)
        assert before == {path.name: path.read_bytes() for path in output.iterdir()}


if __name__ == "__main__":
    unittest.main()
