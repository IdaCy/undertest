import copy
import datetime as dt
from pathlib import PurePosixPath
import re


def require(condition, message):
    if not condition:
        raise ValueError(message)


def relative_path(value):
    require(isinstance(value, str) and value and "\x00" not in value,
            "invalid_consumer_relative_path")
    path = PurePosixPath(value)
    require(path.parts and not path.is_absolute() and str(path) == value
            and all(part not in (".", "..") for part in path.parts),
            "noncanonical_consumer_relative_path")
    return path


def join_native_foundation(plan_ref, plan_review_ref, binding_ref, binding_review_ref, reader):
    plan, plan_review, binding, review = map(reader,
        (plan_ref, plan_review_ref, binding_ref, binding_review_ref))
    require(plan_review.get("accepted_storage_phase_consumer_execution") is True
            and plan_review.get("plan") == plan_ref, "consumer_execution_plan_not_reviewed")
    require(review.get("accepted_native_startup_foundation_binding") is True
            and review.get("binding") == binding_ref
            and review.get("execution_plan") == plan_ref
            and review.get("execution_plan_review") == plan_review_ref
            and review.get("source") == plan["join_source"], "consumer_foundation_binding_not_reviewed")
    require(set(binding) == {"schema", "execution_plan", "execution_plan_review",
                            "foundation", "waiting_report"}
            and binding["schema"] == "causes.native_startup_foundation_binding.v1"
            and binding["execution_plan"] == plan_ref
            and binding["execution_plan_review"] == plan_review_ref,
            "consumer_foundation_execution_changed")
    native = plan["native"]
    require(native["foundation"] is None and native["waiting_report"] is None,
            "consumer_foundation_slots_not_empty")
    pair = binding["foundation"]
    require(type(pair) is dict and set(pair) == {"evidence", "review"},
            "consumer_foundation_pair_scope")
    foundation, foundation_review, report = map(reader,
        (pair["evidence"], pair["review"], binding["waiting_report"]))
    require(foundation_review.get("accepted_native_admission_foundation") is True
            and foundation_review.get("foundation") == pair["evidence"],
            "consumer_foundation_not_reviewed")
    require(foundation["policy"] == native["policy"]
            and foundation["stage"] == "waiting_release"
            and binding["waiting_report"] in foundation["evidence_pins"],
            "consumer_foundation_scope_changed")
    require(report["action"] == "foundation" and report["stage"] == "waiting_release"
            and report["read_only"] is True
            and report["observation"]["waiting_launch"] == native["launch"]
            and report["observation"]["waiting"]["launch"] == native["launch"]
            and report["observation"]["waiting"]["policy"] == native["policy"],
            "consumer_waiting_identity_changed")
    result = copy.deepcopy(plan)
    result["native"]["foundation"] = copy.deepcopy(pair)
    result["native"]["waiting_report"] = copy.deepcopy(binding["waiting_report"])
    return result


def verify_snapshot_result(sync, request, authorization, manifest, verified, manifest_ref,
                           *, host, remote_base, roots, authorization_evidence):
    require(type(roots) is list and roots and len(roots) == len(set(roots)),
            "consumer_snapshot_roots_invalid")
    for root in roots:
        relative_path(root)
    require(request["host"] == host and request["base"] == remote_base
            and request["roots"] == roots and type(request["limit"]) is int
            and request["limit"] > 0, "consumer_snapshot_request_scope_changed")
    require(authorization == {"host": host, "remote_base": remote_base, "roots": roots,
                              "evidence": authorization_evidence},
            "consumer_snapshot_authorization_changed")
    accepted = request["accepted"]
    require(type(accepted) is list and len(accepted) == len(set(accepted)),
            "consumer_snapshot_index_invalid")
    require(all(str(relative_path(run).parent) in roots for run in accepted),
            "consumer_snapshot_index_scope_changed")
    require(manifest["schema"] == "caus.completed_snapshot_manifest.v1"
            and manifest["host"] == host and manifest["base"] == remote_base
            and manifest["skipped"] == [] and sync["skipped"] == [],
            "consumer_snapshot_manifest_scope_changed")
    require(verified["schema"] == "caus.completed_snapshot_verified.v1",
            "consumer_snapshot_verified_schema")
    rows, receipts = manifest["runs"], verified["runs"]
    require(type(rows) is list and type(receipts) is list
            and type(sync["verified_runs"]) is int and sync["verified_runs"] >= 0
            and len(rows) == len(receipts) == sync["verified_runs"] <= request["limit"],
            "consumer_snapshot_run_count_changed")
    attempt = PurePosixPath(sync["attempt"])
    require(attempt.is_absolute() and str(attempt) == sync["attempt"]
            and ".." not in attempt.parts
            and manifest_ref["path"] == str(attempt / "MANIFEST.json"),
            "consumer_snapshot_attempt_changed")
    seen, total = set(), 0
    for row, receipt in zip(rows, receipts):
        run = relative_path(row["run"])
        require(str(run.parent) in roots and row["run"] not in seen
                and row["run"] not in accepted, "consumer_snapshot_run_scope_changed")
        seen.add(row["run"])
        files = row["files"]
        require(type(files) is list, "consumer_snapshot_files_invalid")
        names, size, summary = set(), 0, None
        for item in files:
            relative_path(item["path"])
            require(item["path"] not in names and type(item["bytes"]) is int
                    and item["bytes"] >= 0 and isinstance(item["sha256"], str)
                    and re.fullmatch(r"[0-9a-f]{64}", item["sha256"]),
                    "consumer_snapshot_file_invalid")
            names.add(item["path"])
            size += item["bytes"]
            if item["path"] == "summary.json":
                summary = item["sha256"]
        require(summary is not None and summary == row["summary_sha256"]
                and type(row["bytes"]) is int and row["bytes"] == size,
                "consumer_snapshot_manifest_bytes_changed")
        require(receipt["run"] == row["run"]
                and receipt["path"] == str(attempt / "data" / run)
                and receipt["manifest"] == manifest_ref["path"]
                and receipt["manifest_sha256"] == manifest_ref["sha256"]
                and type(receipt["files"]) is int and receipt["files"] == len(files)
                and type(receipt["bytes"]) is int and receipt["bytes"] == size
                and receipt["summary_sha256"] == summary,
                "consumer_snapshot_receipt_changed")
        require(dt.datetime.fromisoformat(receipt["verified_at"]).tzinfo is not None,
                "consumer_snapshot_receipt_time_missing")
        total += size
    require(type(sync["verified_bytes"]) is int and sync["verified_bytes"] == total,
            "consumer_snapshot_total_changed")
    return {"verified_runs": len(rows), "verified_bytes": total,
            "prior_accepted_runs": len(accepted), "new_runs": [row["run"] for row in rows]}
