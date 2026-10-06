import argparse
import copy
import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path

from shared.runs import run_directory
from shared.runs import write_json
import characteristics.continuations.continuations as continuations
import signals.validation.controls.environment as environment
from signals.validation.controls.runner import ChatEndpoint
from signals.validation.controls.runner import actor_request
from signals.validation.controls.runner import continuation_request
from signals.validation.controls.runner import digest
from signals.validation.controls.runner import run_continuation
from signals.validation.controls.runner import utcnow

if __package__:
    from signals.reasoning.run_monitor import ROOT
    from signals.reasoning.run_monitor import archive_sources
    from signals.reasoning.run_monitor import persist_once
    from signals.reasoning.run_monitor import read_json
    from signals.reasoning.run_monitor import source_hashes
    from signals.reasoning.run_monitor import validate_manifest
else:
    from signals.reasoning.run_monitor import ROOT, archive_sources, persist_once, read_json, source_hashes, validate_manifest


PLAN_SCHEMA = "sigres-continuation-plan-v1"
RENDER_SCHEMA = "sigres-normalized-render-receipt-v1"
REVIEW_SCHEMA = "sigres-render-review-v1"
RENDERER_PROFILE = "vllm-0.28.0-qwen-prior-reasoning-v1"
TERMINAL_STATUSES = {"completed", "error", "preparation_error", "interrupted", "generation_limit", "token_limit", "call_limit"}


def _keys(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError("Missing or unexpected frozen object fields")


def _sha(value):
    if type(value) is not str or len(value) != 64 or set(value) - set("0123456789abcdef"):
        raise ValueError("Expected a lowercase SHA256 digest")
    return value


def _hashes(value):
    if type(value) is not dict or not value or any(type(key) is not str or not key for key in value):
        raise ValueError("A nonempty explicit source/hash map is required")
    for item in value.values():
        _sha(item)


def _artifact(descriptor, base, captured):
    _keys(descriptor, {"path", "bytes", "sha256"})
    _sha(descriptor["sha256"])
    if type(descriptor["path"]) is not str or not descriptor["path"]:
        raise ValueError("Artifact path is required")
    if type(descriptor["bytes"]) is not int or descriptor["bytes"] < 1:
        raise ValueError("Artifact byte size must be positive")
    path = Path(descriptor["path"])
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if path.stat().st_size != descriptor["bytes"]:
        raise ValueError(f"Frozen artifact byte size differs: {path.name}")
    body = path.read_bytes()
    if len(body) != descriptor["bytes"] or hashlib.sha256(body).hexdigest() != descriptor["sha256"]:
        raise ValueError(f"Frozen artifact hash differs: {path.name}")
    captured[descriptor["sha256"]] = body
    return path, body


def _json_artifact(descriptor, base, captured):
    path, body = _artifact(descriptor, base, captured)
    # Parse exactly the verified bytes, rather than rereading a mutable path.
    return path, continuations._strict_loads(body)


def _source_archive(body, sources):
    _hashes(sources)
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        if sorted(archive.namelist()) != sorted(sources):
            raise ValueError("Parent source archive file list differs")
        for name, expected in sources.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError("Parent source archive content differs")


def _serving_identity(value):
    _keys(value, {"base_url", "model", "model_revision", "context_tokens", "renderer_profile",
                  "tokenizer_files_sha256", "chat_template_sha256", "normalizer_source_sha256",
                  "serving_stack_sha256", "acceptance_sha256"})
    for key in ("base_url", "model", "model_revision"):
        if type(value[key]) is not str or not value[key].strip():
            raise ValueError("Explicit serving/model identity is required")
    if value["model_revision"] == "unrecorded" or value["renderer_profile"] != RENDERER_PROFILE:
        raise ValueError("Unsupported or unrecorded serving/render identity")
    if type(value["context_tokens"]) is not int or value["context_tokens"] < 1:
        raise ValueError("A fixed model context budget is required")
    for key in ("chat_template_sha256", "serving_stack_sha256", "acceptance_sha256"):
        _sha(value[key])
    _hashes(value["tokenizer_files_sha256"])
    _hashes(value["normalizer_source_sha256"])
    ChatEndpoint(value["base_url"])


def load_plan(path, expected_file_sha256, sources):
    path = Path(path).resolve()
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != _sha(expected_file_sha256):
        raise ValueError("Plan differs from the external frozen file hash")
    plan = continuations._strict_loads(body)
    _keys(plan, {"schema_version", "protocol_sha256", "selection_sha256", "ledger", "parents",
                 "source_sha256", "serving_identity", "render_receipt", "render_review"})
    if plan["schema_version"] != PLAN_SCHEMA or plan["source_sha256"] != sources:
        raise ValueError("Continuation plan schema or current source hashes differ")
    _serving_identity(plan["serving_identity"])
    ledger = plan["ledger"]
    expected_ledger = continuations.freeze_branch_ledger(
        ledger["payload"]["assignments"], protocol_sha256=plan["protocol_sha256"],
        selection_sha256=plan["selection_sha256"],
    )
    if digest(ledger) != digest(expected_ledger):
        raise ValueError("Frozen plan and branch ledger differ")
    selected = {row["parent_checkpoint_sha256"] for row in ledger["payload"]["assignments"]}
    if type(plan["parents"]) is not dict or set(plan["parents"]) != selected:
        raise ValueError("Parent table must match the explicit assigned parent set")
    captured = {expected_file_sha256: body}
    parents, expectations, failures = {}, {}, {}
    for parent_hash, entry in plan["parents"].items():
        _sha(parent_hash)
        _keys(entry, {"checkpoint", "expected", "manifest", "assignments", "source_archive"})
        expectations[parent_hash] = entry["expected"]
        try:
            _, manifest = _json_artifact(entry["manifest"], path.parent, captured)
            validate_manifest(manifest)
            _, assigned = _json_artifact(entry["assignments"], path.parent, captured)
            _, archive = _artifact(entry["source_archive"], path.parent, captured)
            _source_archive(archive, manifest["source_sha256"])
            _, record = _json_artifact(entry["checkpoint"], path.parent, captured)
            payload = record["payload"]
            if type(assigned) is not list or sum(digest(row) == digest(payload["assignment"]) for row in assigned) != 1:
                raise ValueError("Checkpoint assignment is absent or duplicated in the parent ledger")
            if manifest.get("run_type") not in {"smoke", "pilot", "confirmation"}:
                raise ValueError("Expected an original actor parent run")
            for key in ("model", "model_revision", "generation", "seed_derivation_version"):
                if digest(payload[key]) != digest(manifest[key]):
                    raise ValueError(f"Parent checkpoint/manifest {key} differs")
            for key, value in payload["limits"].items():
                if value != manifest[key]:
                    raise ValueError("Parent checkpoint/manifest budgets differ")
            serving = plan["serving_identity"]
            if any(payload[key] != serving[key] for key in ("model", "model_revision")):
                raise ValueError("Parent model differs from the frozen serving identity")
            if entry["expected"]["environment_source_sha256"] != manifest["source_sha256"]["src/signals/validation/controls/environment.py"]:
                raise ValueError("Parent environment source identity differs")
            if entry["expected"]["restoration_runner_source_sha256"] != sources["src/signals/validation/controls/runner.py"]:
                raise ValueError("Restoration runtime is not the frozen current runner")
            parents[parent_hash] = record
        except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile) as exc:
            failures[parent_hash] = {"type": type(exc).__name__, "message": str(exc)}
    prepared = continuations.prepare_branches(
        ledger, expected_ledger_sha256=ledger["sha256"], parents=parents,
        parent_expectations=expectations, environment=environment,
    )
    for branch in prepared:
        if branch["parent_checkpoint_sha256"] in failures:
            branch["status"] = "preparation_error"
            branch["error"] = failures[branch["parent_checkpoint_sha256"]]
    return {"path": path, "plan": plan, "prepared": prepared, "parents": parents, "captured": captured}


def canonical_reasoning_paths(payload):
    paths = []
    for index, message in enumerate(payload["messages"]):
        if message["role"] != "assistant":
            continue
        first, alias = message.get("reasoning"), message.get("reasoning_content")
        if first is not None and alias is not None and first != alias:
            raise ValueError("Conflicting reasoning aliases are not an interpretable intervention")
        key = "reasoning" if first is not None else "reasoning_content"
        value = message.get(key)
        if value is not None:
            if type(value) is not str:
                raise ValueError("Current rendering profile requires readable string reasoning")
            paths.append(["messages", index, key])
    return paths


def normalized_conversation(request):
    # This limited profile is bound to the reviewed vLLM 0.28.0 text-only path.
    conversation = []
    for message in request["messages"]:
        role, content = message["role"], message.get("content")
        if content is None:
            content = []
        elif type(content) is str:
            content = [{"type": "text", "text": content}]
        elif (type(content) is not list or any(type(block) is not dict or set(block) != {"type", "text"}
                                              or block["type"] != "text" or type(block["text"]) is not str for block in content)):
            raise ValueError("Unsupported content in the reviewed text rendering profile")
        result = {"role": role, "content": copy.deepcopy(content)}
        if role == "assistant":
            first, alias = message.get("reasoning"), message.get("reasoning_content")
            if first is not None and alias is not None and first != alias:
                raise ValueError("Conflicting reasoning aliases")
            reasoning = first if first is not None else alias
            if reasoning is not None:
                if type(reasoning) is not str:
                    raise ValueError("Reasoning must be a string for this render profile")
                result.update({"reasoning": reasoning, "reasoning_content": reasoning})
            if message.get("tool_calls"):
                result["tool_calls"] = copy.deepcopy(message["tool_calls"])
                for tool in result["tool_calls"]:
                    tool["function"]["arguments"] = continuations._strict_loads(tool["function"]["arguments"])
        elif role == "tool":
            result["tool_call_id"] = message["tool_call_id"]
            result["content"] = "\n".join(block["text"] for block in content)
        elif role not in {"system", "user"}:
            raise ValueError("Unsupported message role in rendering profile")
        conversation.append(result)
    return conversation


def _parent_request(payload):
    return actor_request(
        model=payload["model"], generation=payload["generation"], initial_context=payload["initial_context"],
        messages=payload["messages"], tools=payload["tools"], seed=payload["next_generation_seed"],
        max_tokens=min(payload["limits"]["per_call_tokens"], payload["limits"]["max_completion_tokens"] - payload["usage"]["completion_tokens"]),
    )


def _render_entry(entry, payload, request, base, captured):
    _keys(entry, {"payload_sha256", "request_sha256", "canonical_reasoning_paths", "normalized_messages",
                  "rendered_text", "token_ids", "token_ids_sha256"})
    if entry["payload_sha256"] != digest(payload) or entry["request_sha256"] != digest(request):
        raise ValueError("Rendered receipt does not bind the exact payload/request")
    if entry["canonical_reasoning_paths"] != canonical_reasoning_paths(payload):
        raise ValueError("Rendered receipt has a different canonical reasoning field")
    _, normalized = _json_artifact(entry["normalized_messages"], base, captured)
    if digest(normalized) != digest(normalized_conversation(request)):
        raise ValueError("Rendered receipt normalization differs from the outgoing request")
    _, rendered_bytes = _artifact(entry["rendered_text"], base, captured)
    rendered = rendered_bytes.decode("utf-8")
    _, token_ids = _json_artifact(entry["token_ids"], base, captured)
    if type(token_ids) is not list or not token_ids or any(type(token) is not int or token < 0 for token in token_ids):
        raise ValueError("Rendered token IDs must be a nonempty list of nonnegative integers")
    if digest(token_ids) != _sha(entry["token_ids_sha256"]):
        raise ValueError("Rendered token digest differs")
    return {"rendered_text": rendered, "token_ids": token_ids,
            "rendered_sha256": entry["rendered_text"]["sha256"], "token_ids_sha256": entry["token_ids_sha256"],
            "request_sha256": digest(request), "token_count": len(token_ids)}


def apply_render_gate(loaded):
    plan, captured = loaded["plan"], loaded["captured"]
    ready = [branch for branch in loaded["prepared"] if branch["status"] == "ready"]
    try:
        if plan["render_receipt"] is None or plan["render_review"] is None:
            raise ValueError("Execution requires a frozen offline render receipt and accepted review")
        receipt_path, receipt = _json_artifact(plan["render_receipt"], loaded["path"].parent, captured)
        _, review = _json_artifact(plan["render_review"], loaded["path"].parent, captured)
        _keys(receipt, {"schema_version", "ledger_sha256", "serving_identity_sha256", "source_sha256",
                        "renderer_profile", "parents", "branches"})
        _keys(review, {"schema_version", "receipt_file_sha256", "ledger_sha256", "serving_identity_sha256",
                       "decision", "reviewer_ids", "review_report_sha256"})
        if (receipt["schema_version"] != RENDER_SCHEMA or receipt["ledger_sha256"] != plan["ledger"]["sha256"]
                or receipt["serving_identity_sha256"] != digest(plan["serving_identity"])
                or receipt["source_sha256"] != plan["source_sha256"] or receipt["renderer_profile"] != RENDERER_PROFILE):
            raise ValueError("Render receipt differs from the frozen continuation/serving identity")
        if (review["schema_version"] != REVIEW_SCHEMA or review["decision"] != "accepted"
                or review["receipt_file_sha256"] != plan["render_receipt"]["sha256"]
                or review["ledger_sha256"] != receipt["ledger_sha256"]
                or review["serving_identity_sha256"] != receipt["serving_identity_sha256"]):
            raise ValueError("Accepted render review does not cover this exact receipt")
        if (type(review["reviewer_ids"]) is not list or not review["reviewer_ids"]
                or any(type(name) is not str or not name.strip() for name in review["reviewer_ids"])):
            raise ValueError("Render review must name its independent reviewers")
        _sha(review["review_report_sha256"])
        assigned_ids = {row["branch_id"] for row in plan["ledger"]["payload"]["assignments"]}
        if type(receipt["branches"]) is not dict or set(receipt["branches"]) - assigned_ids:
            raise ValueError("Render receipt contains unassigned branches")
        if type(receipt["parents"]) is not dict or set(receipt["parents"]) - set(plan["parents"]):
            raise ValueError("Render receipt contains unassigned parents")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        for branch in ready:
            branch.update({"status": "preparation_error", "error": {"type": type(exc).__name__, "message": str(exc)}})
        return {"status": "error", "error": {"type": type(exc).__name__, "message": str(exc)}}
    baseline_cache = {}
    for branch in ready:
        try:
            parent_hash = branch["parent_checkpoint_sha256"]
            parent = loaded["parents"][parent_hash]["payload"]
            if parent_hash not in baseline_cache:
                baseline_cache[parent_hash] = _render_entry(
                    receipt["parents"][parent_hash], parent, _parent_request(parent), receipt_path.parent, captured,
                )
            canonical = canonical_reasoning_paths(parent)
            for edit in branch["assignment"]["edits"]:
                path = edit["path"]
                is_reasoning_field = path[0] == "messages" and path[2] in {"reasoning", "reasoning_content"}
                if is_reasoning_field and path not in canonical:
                    raise ValueError("Edit targets a shadowed or unavailable reasoning alias")
                if branch["assignment"]["condition"] == "reasoning_text" and path not in canonical:
                    raise ValueError("Reasoning-text condition must edit the canonical reasoning field")
            request = continuation_request(branch, environment=environment)
            rendered = _render_entry(receipt["branches"][branch["branch_id"]], branch["resume_payload"],
                                     request, receipt_path.parent, captured)
            baseline = baseline_cache[parent_hash]
            same_text = rendered["rendered_text"] == baseline["rendered_text"]
            same_tokens = rendered["token_ids"] == baseline["token_ids"]
            if branch["assignment"]["condition"] == "unchanged":
                if not same_text or not same_tokens:
                    raise ValueError("Unchanged branch differs from its rendered parent baseline")
            elif same_text or same_tokens:
                raise ValueError("Edited branch is an accidental no-op in rendered text or tokens")
            if rendered["token_count"] + request["max_tokens"] > plan["serving_identity"]["context_tokens"]:
                raise ValueError("Rendered continuation exceeds the frozen model context budget")
            branch["render_gate"] = {key: value for key, value in rendered.items() if key not in {"rendered_text", "token_ids"}}
            branch["render_gate"].update({"receipt_file_sha256": plan["render_receipt"]["sha256"],
                                         "review_file_sha256": plan["render_review"]["sha256"]})
        except (ValueError, KeyError, TypeError, OSError) as exc:
            branch.update({"status": "preparation_error", "error": {"type": type(exc).__name__, "message": str(exc)}})
    return {"status": "checked", "ready": sum(branch["status"] == "ready" for branch in loaded["prepared"]),
            "preparation_errors": sum(branch["status"] == "preparation_error" for branch in loaded["prepared"])}


def archive_inputs(output, captured):
    directory = output / "inputs"
    directory.mkdir(exist_ok=True)
    for sha, body in captured.items():
        path = directory / f"{sha}.body"
        if hashlib.sha256(body).hexdigest() != sha:
            raise ValueError("Captured input changed before archival")
        if path.exists():
            if path.read_bytes() != body:
                raise ValueError("Previously archived input changed")
        else:
            with path.open("xb") as stream:
                stream.write(body)
    persist_once(output / "input_artifacts.json", {sha: {"bytes": len(body), "file": f"inputs/{sha}.body"}
                                                  for sha, body in sorted(captured.items())})


def _outcome(branch, status, **fields):
    return {"branch_id": branch["branch_id"], "assignment_sha256": branch["assignment_sha256"],
            "ledger_sha256": branch["ledger_sha256"], "parent_checkpoint_sha256": branch["parent_checkpoint_sha256"],
            "status": status, **fields}


def _attempt_artifacts(target):
    artifacts = {}
    for path in sorted(target.rglob("*")):
        if path == target / "result.json" or not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError("Branch evidence cannot contain symlinks")
        body = path.read_bytes()
        artifacts[str(path.relative_to(target))] = {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    return artifacts


def _trajectory_result(branch, target, trajectory, **fields):
    if (trajectory.get("branch_id") != branch["branch_id"]
            or trajectory.get("branch_assignment_sha256") != branch["assignment_sha256"]
            or trajectory.get("branch_assignment") != branch["assignment"]
            or trajectory.get("continuation", {}).get("edited_payload_sha256") != branch["edited_payload_sha256"]
            or trajectory.get("assignment") != branch["resume_payload"]["assignment"]):
        raise ValueError("Trajectory differs from frozen branch")
    result = _outcome(branch, trajectory["status"], trajectory_sha256=digest(trajectory),
                      continuation=trajectory["continuation"], **fields)
    if (target / "labels.json").exists():
        labels = read_json(target / "labels.json")
        if (labels.get("branch_id") != branch["branch_id"]
                or labels.get("branch_assignment_sha256") != branch["assignment_sha256"]
                or labels.get("parent_checkpoint_sha256") != branch["parent_checkpoint_sha256"]
                or labels.get("assignment_id") != branch["resume_payload"]["assignment"]["assignment_id"]
                or labels.get("case_sha256") != digest(branch["resume_payload"]["case"])
                or labels.get("episode_status") != trajectory["status"]
                or labels.get("complete_episode") != (trajectory["status"] == "completed")):
            raise ValueError("Labels differ from frozen branch/trajectory identity")
        result.update({"labels_sha256": digest(labels), "observed_outcomes": labels["observed_outcomes"],
                       "complete_episode": labels["complete_episode"]})
    return result


def run_branches(loaded, output, *, execute, call):
    plan, prepared = loaded["plan"], loaded["prepared"]
    ledger = plan["ledger"]
    directory = output / "branches"
    directory.mkdir(exist_ok=True)
    assigned = {branch["branch_id"] for branch in prepared}
    if any(path.name not in assigned for path in directory.iterdir()):
        raise ValueError("Unassigned branch artifact directory")
    outcomes = {}

    def save_index():
        index = continuations.retain_branch_results(
            ledger, expected_ledger_sha256=ledger["sha256"], outcomes=list(outcomes.values()),
        )
        write_json(output / "branch_index.json", index)
        return index

    # Load all existing attempts and preparation failures before launching any call.
    for branch in prepared:
        target = directory / branch["branch_id"]
        result_path = target / "result.json"
        if result_path.exists():
            result = read_json(result_path)
            continuations.retain_branch_results(ledger, expected_ledger_sha256=ledger["sha256"], outcomes=[result])
            if result["branch_id"] != branch["branch_id"]:
                raise ValueError("Saved result is in the wrong branch directory")
            preparation_path = target / "prepared_branch.json"
            if not preparation_path.exists() and result["status"] == "interrupted":
                preparation_path = target / "recovered_preparation.json"
            if not preparation_path.exists() or read_json(preparation_path) != branch:
                raise ValueError("Saved branch differs from frozen preparation")
            if result.get("artifacts") != _attempt_artifacts(target):
                raise ValueError("Saved branch evidence files changed")
            if "trajectory_sha256" in result:
                trajectory = read_json(target / "trajectory.json")
                expected_result = _trajectory_result(branch, target, trajectory)
                if any(result.get(key) != value for key, value in expected_result.items()):
                    raise ValueError("Saved branch result differs from its evidence")
            outcomes[branch["branch_id"]] = result
        elif target.exists():
            prepared_path = target / "prepared_branch.json"
            if prepared_path.exists() and read_json(prepared_path) != branch:
                raise ValueError("Interrupted branch has changed frozen preparation")
            status = "interrupted"
            trajectory_path = target / "trajectory.json"
            trajectory = None
            if trajectory_path.exists() and prepared_path.exists():
                trajectory = read_json(trajectory_path)
                _trajectory_result(branch, target, trajectory)
                if trajectory["status"] in TERMINAL_STATUSES:
                    status = trajectory["status"]
            result = (_trajectory_result(branch, target, trajectory, recovered_at=utcnow())
                      if status != "interrupted" else _outcome(branch, status, recovered_at=utcnow()))
            if status == "interrupted":
                if not prepared_path.exists():
                    result["missing_preparation_at_recovery"] = True
                    write_json(target / "recovered_preparation.json", branch)
                write_json(target / "interrupted.json", result)
            result["artifacts"] = _attempt_artifacts(target)
            write_json(result_path, result)
            outcomes[branch["branch_id"]] = result
        elif branch["status"] != "ready":
            target.mkdir()
            write_json(target / "prepared_branch.json", branch)
            result = _outcome(branch, "preparation_error", error=branch["error"])
            result["artifacts"] = _attempt_artifacts(target)
            write_json(result_path, result)
            outcomes[branch["branch_id"]] = result
    index = save_index()
    if not execute:
        return index
    for branch in prepared:
        if branch["branch_id"] in outcomes:
            continue
        if "render_gate" not in branch:
            raise ValueError("Refusing a branch without its reviewed rendered-token gate")
        if digest(continuation_request(branch, environment=environment)) != branch["render_gate"]["request_sha256"]:
            raise ValueError("Prepared outgoing request changed after rendering review")
        target = directory / branch["branch_id"]
        trajectory = run_continuation(branch=branch, output=target, call=call, environment=environment)
        result = _trajectory_result(branch, target, trajectory, ended_at=trajectory["ended_at"])
        result["artifacts"] = _attempt_artifacts(target)
        write_json(target / "result.json", result)
        outcomes[branch["branch_id"]] = result
        index = save_index()
        print(json.dumps({"branch_id": branch["branch_id"], "status": result["status"]}), flush=True)
    return index


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        if Path(environment.__file__).resolve().parents[4] != ROOT:
            raise ValueError("Use this worktree's Python environment")
        sources = source_hashes()
        loaded = load_plan(args.plan, args.plan_sha256, sources)
        plan = loaded["plan"]
        gate = apply_render_gate(loaded)
        config = {"section": "signals", "run_type": "continuation", "model": plan["serving_identity"]["model"],
                  "model_revision": plan["serving_identity"]["model_revision"], "source_sha256": sources,
                  "plan_file_sha256": args.plan_sha256, "plan": plan, "python": sys.version}
        with run_directory(args.output, config) as manifest:
            archive_sources(args.output, sources)
            archive_inputs(args.output, loaded["captured"])
            persist_once(args.output / "ledger.json", plan["ledger"])
            persist_once(args.output / "prepared_branches.json", loaded["prepared"])
            persist_once(args.output / "render_gate.json", gate)
            endpoint = ChatEndpoint(plan["serving_identity"]["base_url"], os.environ.get("QWEN_API_KEY", "unused"))
            index = run_branches(loaded, args.output, execute=args.execute and gate["status"] != "error", call=endpoint)
            print(json.dumps({"run_id": manifest["run_id"], "assignments": len(index), "execute": args.execute,
                              "render_gate": gate, "statuses": {status: sum(row["status"] == status for row in index)
                                                                   for status in sorted({row["status"] for row in index})}}), flush=True)
            if args.execute and gate["status"] == "error":
                raise ValueError(gate["error"]["message"])
    except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
