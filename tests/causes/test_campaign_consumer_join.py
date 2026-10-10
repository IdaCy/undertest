import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from causes.campaign_consumer_join import join_native_foundation, verify_snapshot_result
from causes import campaign_snapshot as snapshot


class JoinFixture:
    def __init__(self):
        self.plan = {"join_source": "join-source", "native": {"policy": "policy", "launch": "launch",
                     "foundation": None, "waiting_report": None}, "entries": {"sync": "original"},
                     "accepted_stop_chain": {"actual_review": "stop"}, "execution_directory": "/outputs/pair"}
        self.binding = {"schema": "causes.native_startup_foundation_binding.v1", "execution_plan": "plan",
                        "execution_plan_review": "plan-review", "foundation": {"evidence": "foundation",
                        "review": "foundation-review"}, "waiting_report": "report"}
        self.review = {"accepted_native_startup_foundation_binding": True, "binding": "binding",
                       "execution_plan": "plan", "execution_plan_review": "plan-review", "source": "join-source"}
        self.values = {"plan": self.plan, "plan-review": {"accepted_storage_phase_consumer_execution": True,
                       "plan": "plan"}, "binding": self.binding, "binding-review": self.review,
                       "foundation": {"policy": "policy", "stage": "waiting_release", "evidence_pins": ["report"]},
                       "foundation-review": {"accepted_native_admission_foundation": True, "foundation": "foundation"},
                       "report": {"action": "foundation", "stage": "waiting_release", "read_only": True,
                       "observation": {"waiting_launch": "launch", "waiting": {"launch": "launch", "policy": "policy"}}}}

    def join(self):
        return join_native_foundation("plan", "plan-review", "binding", "binding-review", self.values.__getitem__)


def snapshot_values():
    digest = "a" * 64
    attempt = "/snapshots/attempt-one"
    root = "runs/held/family"
    manifest_ref = {"path": attempt + "/MANIFEST.json", "bytes": 123, "sha256": "b" * 64}
    row = {"run": root + "/new", "bytes": 4, "summary_sha256": digest,
           "files": [{"path": "summary.json", "bytes": 4, "sha256": digest}]}
    receipt = {"run": row["run"], "path": attempt + "/data/" + row["run"],
               "manifest": manifest_ref["path"], "manifest_sha256": manifest_ref["sha256"],
               "files": 1, "bytes": 4, "summary_sha256": digest, "verified_at": "2000-01-01T00:00:00+00:00"}
    request = {"host": "fixture", "base": "/remote", "roots": [root], "accepted": [root + "/old"], "limit": 128}
    auth = {"host": "fixture", "remote_base": "/remote", "roots": [root], "evidence": {"registration": "sealed"}}
    sync = {"attempt": attempt, "verified_runs": 1, "verified_bytes": 4, "skipped": []}
    manifest = {"schema": "caus.completed_snapshot_manifest.v1", "host": "fixture", "base": "/remote", "runs": [row], "skipped": []}
    verified = {"schema": "caus.completed_snapshot_verified.v1", "runs": [receipt]}
    kwargs = dict(host="fixture", remote_base="/remote", roots=[root], authorization_evidence=auth["evidence"])
    return [sync, request, auth, manifest, verified, manifest_ref], kwargs


class ConsumerJoinTests(unittest.TestCase):
    def test_join_changes_only_two_null_slots_and_does_not_share_mutable_outputs(self):
        fixture = JoinFixture()
        before = copy.deepcopy(fixture.values)
        joined = fixture.join()
        joined["native"]["foundation"] = None
        joined["native"]["waiting_report"] = None
        self.assertEqual(joined, fixture.plan)
        result = fixture.join()
        result["entries"]["sync"] = "changed"
        result["native"]["foundation"]["evidence"] = "changed"
        self.assertEqual(fixture.values, before)
        self.assertEqual(fixture.join()["native"]["foundation"]["evidence"], "foundation")

    def test_join_rejects_unreviewed_or_changed_execution_identity(self):
        mutations = [lambda f: f.review.update(accepted_native_startup_foundation_binding=False),
                     lambda f: f.review.update(source="other-source"),
                     lambda f: f.review.update(execution_plan="another-plan"),
                     lambda f: f.binding.update(execution_plan_review="another-review"),
                     lambda f: f.values["plan-review"].update(plan="another-plan")]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                fixture = JoinFixture(); mutate(fixture)
                with self.assertRaises(ValueError): fixture.join()

    def test_join_rejects_extra_overrides_and_prepopulated_slots(self):
        mutations = [lambda f: f.binding.update(entries={"sync": "other"}),
                     lambda f: f.binding["foundation"].update(policy="other"),
                     lambda f: f.plan["native"].update(foundation={}),
                     lambda f: f.plan["native"].update(waiting_report="old")]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                fixture = JoinFixture(); mutate(fixture)
                with self.assertRaises(ValueError): fixture.join()

    def test_join_rejects_changed_policy_launch_stage_or_report_membership(self):
        mutations = [lambda f: f.values["foundation"].update(policy="other"),
                     lambda f: f.values["foundation"].update(stage="before_start"),
                     lambda f: f.values["foundation"].update(evidence_pins=[]),
                     lambda f: f.values["foundation-review"].update(foundation="other"),
                     lambda f: f.values["report"]["observation"].update(waiting_launch="other"),
                     lambda f: f.values["report"]["observation"]["waiting"].update(policy="other")]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                fixture = JoinFixture(); mutate(fixture)
                with self.assertRaises(ValueError): fixture.join()

    def test_snapshot_zero_requires_exact_empty_arrays_and_totals(self):
        args, kwargs = snapshot_values()
        args[0].update(verified_runs=0, verified_bytes=0)
        args[3]["runs"] = []; args[4]["runs"] = []
        self.assertEqual(verify_snapshot_result(*args, **kwargs),
                         {"verified_runs": 0, "verified_bytes": 0, "prior_accepted_runs": 1, "new_runs": []})
        for index, key, value in [(0, "verified_bytes", 1), (0, "verified_runs", False),
                                  (3, "skipped", ["missing-root"]), (0, "skipped", ["active-run"])]:
            with self.subTest(key=key):
                changed = copy.deepcopy(args); changed[index][key] = value
                with self.assertRaises(ValueError): verify_snapshot_result(*changed, **kwargs)

    def test_snapshot_nonzero_matches_every_receipt_and_manifest_file(self):
        args, kwargs = snapshot_values()
        before = copy.deepcopy(args)
        self.assertEqual(verify_snapshot_result(*args, **kwargs)["verified_bytes"], 4)
        self.assertEqual(args, before)
        for index, key, value in [(4, "manifest_sha256", "c" * 64), (4, "files", True),
                                 (4, "bytes", 3), (4, "path", "/another/output"),
                                 (3, "summary_sha256", "c" * 64), (3, "bytes", 3)]:
            with self.subTest(key=key):
                changed = copy.deepcopy(args); changed[index]["runs"][0][key] = value
                with self.assertRaises(ValueError): verify_snapshot_result(*changed, **kwargs)

    def test_snapshot_rejects_missing_extra_or_reordered_run_receipts(self):
        args, kwargs = snapshot_values()
        for mode in ("missing", "extra", "run"):
            with self.subTest(mode=mode):
                changed = copy.deepcopy(args)
                if mode == "missing": changed[4]["runs"] = []
                elif mode == "extra": changed[4]["runs"].append(copy.deepcopy(changed[4]["runs"][0]))
                else: changed[4]["runs"][0]["run"] = "runs/held/family/other"
                with self.assertRaises(ValueError): verify_snapshot_result(*changed, **kwargs)

    def test_snapshot_rejects_index_overlap_duplicates_and_unknown_roots(self):
        args, kwargs = snapshot_values()
        for accepted in [["runs/held/family/new"], ["runs/held/family/old"] * 2, ["runs/foreign/family/old"], ["../escape"]]:
            with self.subTest(accepted=accepted):
                changed = copy.deepcopy(args); changed[1]["accepted"] = accepted
                with self.assertRaises(ValueError): verify_snapshot_result(*changed, **kwargs)

    def test_snapshot_rejects_duplicate_files_path_escape_and_bad_hash(self):
        args, kwargs = snapshot_values()
        for mutation in ("duplicate", "escape", "hash", "boolean_size"):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(args); files = changed[3]["runs"][0]["files"]
                if mutation == "duplicate": files.append(copy.deepcopy(files[0]))
                elif mutation == "escape": files[0]["path"] = "../summary.json"
                elif mutation == "hash": files[0]["sha256"] = "bad"
                else: files[0]["bytes"] = True
                with self.assertRaises(ValueError): verify_snapshot_result(*changed, **kwargs)

    def test_snapshot_rejects_authorization_scope_and_count_fabrication(self):
        args, kwargs = snapshot_values()
        for index, key, value in [(1, "roots", ["runs/other/family"]), (1, "host", "another"),
                                  (2, "evidence", {"registration": "changed"}), (3, "base", "/other"),
                                  (0, "verified_runs", True), (0, "verified_bytes", 3)]:
            with self.subTest(key=key):
                changed = copy.deepcopy(args); changed[index][key] = value
                with self.assertRaises(ValueError): verify_snapshot_result(*changed, **kwargs)

    def test_real_snapshot_then_empty_success_preserves_index_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve(); remote = base / "remote"; root = "runs/held/family"
            run = remote / root / "one"; run.mkdir(parents=True)
            (run / "summary.json").write_text(json.dumps({"sandbox_closure": {"state": "closed", "overlay_mounted": False}}))
            (run / "evidence.bin").write_bytes(b"unchanged evidence")
            config = snapshot.SnapshotConfig("fixture", str(remote), (root,), base / "local", free_space_floor_bytes=0)
            expected_auth = {"registration": "sealed"}
            def authorize(value):
                return dict(host=value.host, remote_base=value.remote_base, roots=list(value.roots), evidence=expected_auth)
            actual_run = subprocess.run
            def transport(argv, **kwargs):
                if argv[0] == "ssh":
                    return actual_run([sys.executable, "-c", snapshot.REMOTE_CODE], input=kwargs["input"], capture_output=True, text=True)
                self.assertEqual(argv[0], "rsync")
                listing = Path(next(v.split("=", 1)[1] for v in argv if v.startswith("--files-from=")))
                for name in listing.read_bytes().split(b"\x00"):
                    if name:
                        source = remote / name.decode(); destination = Path(argv[-1]) / name.decode()
                        destination.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(source, destination)
                return subprocess.CompletedProcess(argv, 0)
            with patch.object(snapshot.subprocess, "run", transport):
                results = [snapshot.synchronize_completed(config, authorize) for _ in range(2)]
            self.assertEqual([row["verified_runs"] for row in results], [1, 0])
            for result in results:
                attempt = Path(result["attempt"])
                values = [json.loads((attempt / name).read_text()) for name in ("REQUEST.json", "AUTHORIZATION.json", "MANIFEST.json", "VERIFIED.json")]
                raw = (attempt / "MANIFEST.json").read_bytes()
                ref = dict(path=str(attempt / "MANIFEST.json"), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                checked = verify_snapshot_result(result, *values, ref, host=config.host, remote_base=config.remote_base,
                                                 roots=list(config.roots), authorization_evidence=expected_auth)
                self.assertEqual(checked["verified_runs"], result["verified_runs"])
            index = [json.loads(line) for line in (config.destination / "accepted.jsonl").read_text().splitlines()]
            self.assertEqual([row["run"] for row in index], [root + "/one"])


if __name__ == "__main__":
    unittest.main()
