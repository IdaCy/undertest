"""One retained, model-free storage probe using the normal sandbox lifecycle."""

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time

from causes import campaign_restore as restore
from causes import campaign_sandbox as sandbox
from causes import campaign_storage as storage


_MARKER = "causes_storage_qualification.txt"
_CONTENT = b"storage qualification checkpoint\n"
_MODULES = {"probe": __file__, "sandbox": sandbox.__file__,
            "storage": storage.__file__, "restore": restore.__file__}
_LOADED = {key: restore.file_pin(Path(value).resolve())
           for key, value in _MODULES.items()}


def source_inventory():
    for ref in _LOADED.values():
        restore.read_pin(ref)
    return copy.deepcopy(_LOADED)


def _new_path(value):
    path = Path(value)
    storage.absolute(str(path))
    sandbox._real_directory(str(path.parent))
    if os.path.lexists(path):
        raise ValueError("storage_probe_destination_exists")
    return path


def prepare(plan_path, expected_sha256):
    plan_path = restore.regular_file(plan_path)
    if restore.digest_file(plan_path) != expected_sha256:
        raise ValueError("storage_probe_plan_changed")
    plan = json.loads(plan_path.read_bytes())
    if plan["schema"] != "causes.storage_qualification.v1":
        raise ValueError("storage_probe_schema")
    if plan["sources"] != source_inventory():
        raise ValueError("storage_probe_source_changed")
    spec = json.loads(restore.read_pin(plan["spec"]).read_bytes())
    binding = json.loads(restore.read_pin(plan["storage_binding"]).read_bytes())
    if spec.get("allow_plain_copy_for_pilot"):
        raise ValueError("storage_probe_plain_copy_forbidden")
    if not isinstance(plan["run_id"], str) or not plan["run_id"].startswith("storage_qualification_"):
        raise ValueError("storage_probe_separate_identity_required")
    original, copied, evidence = (_new_path(plan[key]) for key in ("original", "copy", "evidence"))
    root = sandbox._real_directory(binding["output"])
    if not root.name.startswith("storage_qualification_") or original.parent != root or evidence.parent != root:
        raise ValueError("storage_probe_separate_namespace_required")
    destinations = (original, copied, evidence)
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for i, a in enumerate(destinations) for b in destinations[i + 1:]):
        raise ValueError("storage_probe_destinations_overlap")
    seed = sandbox._real_directory(spec["seed_path"])
    if any(path == seed or path.is_relative_to(seed) or seed.is_relative_to(path)
           for path in destinations):
        raise ValueError("storage_probe_destination_overlaps_seed")
    if str(copied.parent.stat().st_dev) != binding["root_device"]:
        raise ValueError("storage_probe_copy_volume_changed")
    guard = storage.StorageGuard(binding)
    if guard.check() < binding["floor_bytes"]:
        raise ValueError("storage_probe_floor_unavailable")
    return plan, spec, guard, original, copied, evidence


def _copy_projection(manifest):
    return [{key: value for key, value in row.items() if key not in {"device", "inode"}}
            for row in manifest["entries"]]


def _verify_checkpoint(checkpoint):
    if checkpoint.get("complete") is not True or checkpoint.get("workspace_method") != "overlayfs":
        raise ValueError("storage_probe_checkpoint_incomplete")
    if checkpoint.get("errors") or checkpoint.get("unpause_returncode") != 0:
        raise ValueError("storage_probe_checkpoint_errors")
    archives = {row["role"]: row for row in checkpoint["archives"]}
    if len(archives) != len(checkpoint["archives"]) or "workspace_upper" not in archives:
        raise ValueError("storage_probe_checkpoint_archive_roles")
    for row in archives.values():
        restore.read_pin({key: row[key] for key in ("path", "bytes", "sha256")})
    with tarfile.open(archives["workspace_upper"]["path"], "r:") as archive:
        members = [row for row in archive.getmembers() if row.name in {_MARKER, "./" + _MARKER}]
        if len(members) != 1 or not members[0].isfile():
            raise ValueError("storage_probe_marker_not_archived")
        with archive.extractfile(members[0]) as stream:
            if stream.read(len(_CONTENT) + 1) != _CONTENT:
                raise ValueError("storage_probe_marker_changed")


def run(plan_path, expected_sha256):
    plan, spec, guard, original, copied, evidence = prepare(plan_path, expected_sha256)
    evidence.mkdir()
    report = {"schema": "causes.storage_qualification_result.v1", "started_at": time.time(),
              "plan": restore.file_pin(plan_path), "sources": source_inventory(),
              "passed": False, "model_requests": 0, "actors_started": 0,
              "original": str(original), "copy": str(copied), "closure": None}
    box = None
    try:
        seed_before = restore.tree_manifest(spec["seed_path"])
        restore.save_new(evidence / "SEED.before.json", seed_before)
        box = sandbox.Sandbox(spec, original, plan["run_id"],
                              files=[{"path": _MARKER, "content": _CONTENT.decode(), "mode": 0o644}])
        started = box.start()
        report["sandbox_started"] = True
        if started["workspace_method"] != "overlayfs":
            raise ValueError("storage_probe_requires_overlayfs")
        result = box.run(["/bin/cat", _MARKER], timeout=10)
        if result["returncode"] or result["timeout"] or result["stdout"].encode() != _CONTENT:
            raise ValueError("storage_probe_workspace_read_failed")
        checkpoint = box.checkpoint("storage_probe")
        _verify_checkpoint(checkpoint)
        report["checkpoint"] = restore.file_pin(original / "sandbox/checkpoints/storage_probe/CHECKPOINT.json")
        report["closure"] = box.close()
        if report["closure"]["state"] != "closed" or report["closure"]["closure_errors"] or report["closure"]["overlay_mounted"]:
            raise ValueError("storage_probe_closure_requires_review")
        seed_after = restore.tree_manifest(spec["seed_path"])
        restore.save_new(evidence / "SEED.after.json", seed_after)
        if seed_before != seed_after:
            raise ValueError("storage_probe_seed_changed")
        if guard.check() < guard.binding["floor_bytes"]:
            raise ValueError("storage_probe_floor_unavailable")
        before = restore.tree_manifest(original)
        restore.save_new(evidence / "ORIGINAL.before_copy.json", before)
        with (evidence / "COPY.stdout").open("xb") as out, (evidence / "COPY.stderr").open("xb") as err:
            process = subprocess.run(["cp", "-a", "--", str(original), str(copied)],
                                     stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=180)
        if process.returncode:
            raise ValueError("storage_probe_copy_failed")
        after, replica = restore.tree_manifest(original), restore.tree_manifest(copied)
        restore.save_new(evidence / "ORIGINAL.after_copy.json", after)
        restore.save_new(evidence / "COPY.manifest.json", replica)
        if before != after or _copy_projection(before) != _copy_projection(replica):
            raise ValueError("storage_probe_copy_mismatch")
        if guard.check() < guard.binding["floor_bytes"]:
            raise ValueError("storage_probe_floor_unavailable")
        source_inventory()
        report.update(passed=True, seed_unchanged=True, copy_verified=True,
                      final_storage_observation=guard.last_observation)
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        if box is not None and not box.closed:
            try:
                report["closure"] = box.close()
            except Exception as exc:
                report["passed"] = False
                report["closure"] = {"state": "closure_requires_review",
                                     "error_type": type(exc).__name__, "message": str(exc)}
        report["finished_at"] = time.time()
        restore.save_new(evidence / "RESULT.json", report)
    if report["passed"] is not True:
        raise ValueError("storage_probe_requires_review")
    return restore.file_pin(evidence / "RESULT.json")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--plan-sha256", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.plan, args.plan_sha256), indent=2))


if __name__ == "__main__":
    main()
