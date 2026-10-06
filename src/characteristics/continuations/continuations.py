import copy
import hashlib
import json
import math
import re
from pathlib import Path

import signals.validation.controls.runner as runner


SCHEMA_VERSION = "sigres-continuations-v1"
CONDITIONS = ("unchanged", "sham", "criterion_text", "reasoning_text")
_PAYLOAD_KEYS = {
    "case", "assignment", "initial_context", "messages", "tools", "state", "model",
    "model_revision", "generation", "usage", "completed_calls", "limits",
    "seed_derivation_version", "next_generation_seed",
}
_EXPECTED_KEYS = {
    "checkpoint_sha256", "case_sha256", "assignment_sha256", "initial_context_sha256",
    "tools_sha256", "model", "model_revision", "generation", "limits",
    "seed_derivation_version", "environment_source_sha256", "restoration_runner_source_sha256",
}
_ASSIGNMENT_KEYS = {
    "branch_id", "parent_checkpoint_sha256", "comparison_id", "replicate_id",
    "condition", "edits", "seed_mode", "request_seeds",
}
_EDIT_KEYS = {"path", "start", "end", "before", "after"}
_RESULT_STATUSES = {
    "completed", "error", "preparation_error", "interrupted", "generation_limit",
    "token_limit", "call_limit",
}


def _json(value):
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json(item)
        return
    raise ValueError("Expected finite JSON data with string object keys")


def _keys(value, expected):
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError("Unexpected or missing object fields")


def _hash(value):
    _json(value)
    return runner.digest(value)


def _sha(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("Expected a lowercase SHA256 digest")


def _identifier(value):
    if type(value) is not str or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value) is None:
        raise ValueError("Expected a nonempty filename-safe identifier")


def _integer(value, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError("Invalid integer or budget")


def _strict_loads(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = item
        return result

    parsed = json.loads(value, object_pairs_hook=unique)
    _json(parsed)
    return parsed


def read_frozen_checkpoint(path: Path, *, expected_bytes: int, expected_file_sha256: str) -> dict:
    _integer(expected_bytes, 1)
    _sha(expected_file_sha256)
    if path.stat().st_size != expected_bytes:
        raise ValueError("Checkpoint byte size differs from frozen artifact")
    body = path.read_bytes()
    if len(body) != expected_bytes or hashlib.sha256(body).hexdigest() != expected_file_sha256:
        raise ValueError("Checkpoint bytes differ from frozen artifact")
    record = _strict_loads(body)
    _keys(record, {"schema_version", "payload", "sha256"})
    return record


def restore_checkpoint(record: dict, *, expected: dict, environment) -> dict:
    """Validate against independently frozen expectations and restore one inspection."""
    _json(record)
    _json(expected)
    _keys(record, {"schema_version", "payload", "sha256"})
    _keys(expected, _EXPECTED_KEYS)
    payload = record["payload"]
    _keys(payload, _PAYLOAD_KEYS)
    for key in expected:
        if key.endswith("sha256"):
            _sha(expected[key])
    if type(record["schema_version"]) is not int or record["schema_version"] != runner.SCHEMA_VERSION:
        raise ValueError("Unsupported checkpoint schema")
    if record["sha256"] != expected["checkpoint_sha256"] or _hash(payload) != record["sha256"]:
        raise ValueError("Checkpoint differs from frozen parent identity")
    for key in ("case", "assignment", "initial_context", "tools"):
        if _hash(payload[key]) != expected[f"{key}_sha256"]:
            raise ValueError(f"Frozen {key} identity mismatch")
    for key in ("model", "model_revision", "generation", "limits", "seed_derivation_version"):
        if _hash(payload[key]) != _hash(expected[key]):
            raise ValueError(f"Frozen {key} identity mismatch")
    for name, module in (("environment", environment), ("restoration_runner", runner)):
        source_hash = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        if source_hash != expected[f"{name}_source_sha256"]:
            raise ValueError(f"Frozen {name} source mismatch")
    if payload["seed_derivation_version"] != runner.SEED_DERIVATION_VERSION:
        raise ValueError("Unsupported seed derivation")
    for key in ("model", "model_revision"):
        if type(payload[key]) is not str or not payload[key].strip():
            raise ValueError("Exact declared model identity is required")
    if type(payload["generation"]) is not dict:
        raise ValueError("Generation configuration must be an object")

    assignment = payload["assignment"]
    required = {"assignment_id", "family", "split", "case_index", "repeat", "generation_seed"}
    if type(assignment) is not dict or not required <= set(assignment) <= required | {"case_id"}:
        raise ValueError("Unsupported parent assignment")
    _identifier(assignment["assignment_id"])
    for key in ("case_index", "repeat", "generation_seed"):
        _integer(assignment[key])
    case = payload["case"]
    regenerated = environment.make_case(assignment["family"], assignment["split"], assignment["case_index"],
                                       public_interface_version=environment.public_interface_version(case))
    if _hash(regenerated) != _hash(case) or assignment.get("case_id", case["case_id"]) != case["case_id"]:
        raise ValueError("Case does not match the immutable parent assignment")
    context = [
        {"role": "system", "content": runner.SYSTEM},
        {"role": "user", "content": environment.render_case(case, **payload["limits"])},
    ]
    if payload["initial_context"] != context or payload["tools"] != environment.tool_definitions():
        raise ValueError("Checkpoint context/tools differ from the current parent protocol")

    _keys(payload["limits"], {"max_calls", "max_completion_tokens", "per_call_tokens"})
    for value in payload["limits"].values():
        _integer(value, 1)
    _keys(payload["usage"], {"prompt_tokens", "completion_tokens"})
    for value in payload["usage"].values():
        _integer(value)
    if type(payload["completed_calls"]) is not int or payload["completed_calls"] != 1:
        raise ValueError("Only the first successful inspection checkpoint is supported")
    remaining_calls = payload["limits"]["max_calls"] - payload["completed_calls"]
    remaining_tokens = payload["limits"]["max_completion_tokens"] - payload["usage"]["completion_tokens"]
    if remaining_calls <= 0 or remaining_tokens <= 0:
        raise ValueError("Checkpoint has no remaining continuation budget")
    if payload["usage"]["completion_tokens"] > payload["limits"]["per_call_tokens"]:
        raise ValueError("Initial response exceeded its recorded token budget")
    _integer(payload["next_generation_seed"], maximum=2**32 - 1)
    next_seed = runner.request_seed(assignment, payload["completed_calls"])
    if payload["next_generation_seed"] != next_seed:
        raise ValueError("Next generation seed does not match the parent schedule")

    messages = payload["messages"]
    if type(messages) is not list or len(messages) != 2:
        raise ValueError("Inspection checkpoint must have exactly one completed tool exchange")
    assistant, result_message = messages
    cleaned, _ = runner.assistant_message({"choices": [{"message": assistant, "finish_reason": "tool_calls"}]})
    if cleaned != assistant:
        raise ValueError("Unsupported assistant metadata in checkpoint")
    calls = assistant["tool_calls"]
    if len(calls) != 1 or calls[0]["function"]["name"] != "inspect_artifact":
        raise ValueError("Checkpoint must precede all mutable operations")
    arguments = _strict_loads(calls[0]["function"]["arguments"])
    if arguments != {}:
        raise ValueError("Initial inspection arguments must be empty")
    _keys(result_message, {"role", "tool_call_id", "content"})
    if result_message["role"] != "tool" or result_message["tool_call_id"] != calls[0]["id"]:
        raise ValueError("Pending or mismatched tool result")
    if type(result_message["content"]) is not str:
        raise ValueError("Tool result must be its recorded JSON text")
    transcript_state = environment.initial_state(case)
    replayed_result = environment.apply_tool(case, transcript_state, "inspect_artifact", arguments)
    if replayed_result.get("ok") is not True or _strict_loads(result_message["content"]) != replayed_result:
        raise ValueError("Recorded tool result does not match replay")
    restored = environment.restore(case, environment.snapshot(case, payload["state"]))
    if _hash(restored) != _hash(transcript_state):
        raise ValueError("Saved state does not match the completed transcript")
    if _hash(environment.grade(case, restored)) != _hash(environment.grade(case, transcript_state)):
        raise ValueError("Restored grading differs from transcript replay")
    full_schedule = [runner.request_seed(assignment, step) for step in range(payload["limits"]["max_calls"])]
    if len(set(full_schedule)) != len(full_schedule):
        raise ValueError("Parent request seed schedule contains a collision")
    return {
        "schema_version": SCHEMA_VERSION, "parent_checkpoint_sha256": record["sha256"],
        "resume_payload": copy.deepcopy(payload), "next_step": 1, "next_generation_seed": next_seed,
        "remaining_calls": remaining_calls, "remaining_completion_tokens": remaining_tokens,
        "remaining_tool_calls": replayed_result["remaining_tool_calls"], "request_seeds": full_schedule[1:],
    }


def _validate_edit(edit):
    _keys(edit, _EDIT_KEYS)
    path = edit["path"]
    if (type(path) is not list or len(path) < 3 or type(path[0]) is not str
            or path[0] not in {"initial_context", "messages"} or type(path[2]) is not str):
        raise ValueError("Edit path must address recorded context or assistant text")
    _integer(path[1])
    if not (len(path) == 3 and path[2] in {"content", "reasoning", "reasoning_content"}):
        if not (len(path) == 5 and path[2] == "content" and type(path[4]) is str and path[4] in {"text", "reasoning"}):
            raise ValueError("Unsupported text edit path")
        _integer(path[3])
    _integer(edit["start"])
    _integer(edit["end"], edit["start"])
    if type(edit["before"]) is not str or type(edit["after"]) is not str or edit["before"] == edit["after"]:
        raise ValueError("An edit requires distinct original and replacement text")
    if len(edit["before"]) != edit["end"] - edit["start"]:
        raise ValueError("Edit span and original text length disagree")


def _validate_assignments(assignments):
    if type(assignments) is not list or not assignments:
        raise ValueError("An explicit nonempty branch assignment ledger is required")
    ids, groups = set(), {}
    for row in assignments:
        _keys(row, _ASSIGNMENT_KEYS)
        for key in ("branch_id", "comparison_id", "replicate_id"):
            _identifier(row[key])
        _sha(row["parent_checkpoint_sha256"])
        if row["branch_id"] in ids:
            raise ValueError("Duplicate branch assignment")
        ids.add(row["branch_id"])
        if row["condition"] not in CONDITIONS:
            raise ValueError("Unsupported branch condition")
        if type(row["edits"]) is not list or (row["condition"] == "unchanged") != (not row["edits"]):
            raise ValueError("Unchanged has no edits; text controls/interventions require explicit edits")
        for edit in row["edits"]:
            _validate_edit(edit)
        if row["seed_mode"] == "original":
            if row["request_seeds"] is not None:
                raise ValueError("Original seed mode cannot supply a replacement schedule")
        elif row["seed_mode"] == "explicit":
            if type(row["request_seeds"]) is not list or not row["request_seeds"]:
                raise ValueError("Explicit seed mode requires a complete schedule")
            for seed in row["request_seeds"]:
                _integer(seed, maximum=2**32 - 1)
            if len(set(row["request_seeds"])) != len(row["request_seeds"]):
                raise ValueError("Explicit seed schedule contains a collision")
        else:
            raise ValueError("Seed mode must be original or explicit")
        group = groups.setdefault((row["comparison_id"], row["replicate_id"]), [])
        group.append(row)
    for group in groups.values():
        conditions = [row["condition"] for row in group]
        if len(set(conditions)) != len(conditions) or "unchanged" not in conditions:
            raise ValueError("Every comparison/replicate requires one unchanged control and unique conditions")
        if len(group) > 1 and not {"sham", "criterion_text"} <= set(conditions):
            raise ValueError("Text comparisons require sham and criterion-text controls")
        matched = {(row["parent_checkpoint_sha256"], row["seed_mode"], _hash(row["request_seeds"])) for row in group}
        if len(matched) != 1:
            raise ValueError("Matched comparison arms must share a parent and seed schedule")


def freeze_branch_ledger(assignments: list[dict], *, protocol_sha256: str, selection_sha256: str) -> dict:
    _json(assignments)
    _sha(protocol_sha256)
    _sha(selection_sha256)
    _validate_assignments(assignments)
    payload = {"protocol_sha256": protocol_sha256, "selection_sha256": selection_sha256,
               "assignments": copy.deepcopy(assignments)}
    return {"schema_version": SCHEMA_VERSION, "payload": payload, "sha256": _hash(payload)}


def _ledger(ledger, expected_sha256):
    _json(ledger)
    _sha(expected_sha256)
    _keys(ledger, {"schema_version", "payload", "sha256"})
    _keys(ledger["payload"], {"protocol_sha256", "selection_sha256", "assignments"})
    if ledger["schema_version"] != SCHEMA_VERSION or ledger["sha256"] != expected_sha256 or _hash(ledger["payload"]) != expected_sha256:
        raise ValueError("Branch ledger differs from the external frozen identity")
    _sha(ledger["payload"]["protocol_sha256"])
    _sha(ledger["payload"]["selection_sha256"])
    _validate_assignments(ledger["payload"]["assignments"])
    return ledger["payload"]["assignments"]


def _edited_payload(payload, edits):
    edited = copy.deepcopy(payload)
    grouped = {}
    for edit in edits:
        path = edit["path"]
        message = payload[path[0]][path[1]]
        allowed_roles = {"system", "user"} if path[0] == "initial_context" else {"assistant"}
        if message["role"] not in allowed_roles:
            raise ValueError("Tool results and nontext metadata cannot be edited")
        if path[0] == "initial_context" and path[2] != "content":
            raise ValueError("Initial context edits must address content")
        if len(path) == 5:
            block = message["content"][path[3]]
            if block.get("type") not in {"text", "reasoning"} or block.get("redacted"):
                raise ValueError("Only readable text/reasoning content blocks can be edited")
            text_key = "text" if block["type"] == "text" or "reasoning" not in block else "reasoning"
            if path[4] != text_key:
                raise ValueError("Edit must target the block's readable text field")
        value = payload
        for key in path:
            value = value[key]
        if type(value) is not str or edit["end"] > len(value) or value[edit["start"]:edit["end"]] != edit["before"]:
            raise ValueError("Edit does not match its frozen original text span")
        grouped.setdefault(tuple(path), []).append(edit)
    for path, spans in grouped.items():
        spans.sort(key=lambda edit: (edit["start"], edit["end"]))
        for left, right in zip(spans, spans[1:]):
            if right["start"] < left["end"] or right["start"] == left["start"]:
                raise ValueError("Overlapping or ambiguous edit spans")
        container = edited
        for key in path[:-1]:
            container = container[key]
        value = container[path[-1]]
        for edit in reversed(spans):
            value = value[:edit["start"]] + edit["after"] + value[edit["end"]:]
        container[path[-1]] = value
    return edited


def prepare_branches(ledger: dict, *, expected_ledger_sha256: str, parents: dict,
                     parent_expectations: dict, environment) -> list[dict]:
    """Return every assigned branch, including preparation failures, without calls."""
    assignments = _ledger(ledger, expected_ledger_sha256)
    restored_parents = {}
    rows = []
    for assignment in assignments:
        parent_hash = assignment["parent_checkpoint_sha256"]
        row = {"branch_id": assignment["branch_id"], "assignment": copy.deepcopy(assignment),
               "assignment_sha256": _hash(assignment), "ledger_sha256": expected_ledger_sha256,
               "parent_checkpoint_sha256": parent_hash, "status": "preparation_error"}
        try:
            if parent_hash not in restored_parents:
                restored_parents[parent_hash] = restore_checkpoint(
                    parents[parent_hash], expected=parent_expectations[parent_hash], environment=environment,
                )
            restored = restored_parents[parent_hash]
            if restored["parent_checkpoint_sha256"] != parent_hash:
                raise ValueError("Parent mapping key differs from checkpoint identity")
            payload = _edited_payload(restored["resume_payload"], assignment["edits"])
            schedule = restored["request_seeds"] if assignment["seed_mode"] == "original" else assignment["request_seeds"]
            if len(schedule) != restored["remaining_calls"]:
                raise ValueError("Explicit schedule must cover every remaining call")
            row.update({
                "status": "ready", "original_payload_sha256": parent_hash,
                "edited_payload_sha256": _hash(payload), "resume_payload": payload,
                "next_step": restored["next_step"], "request_seeds": copy.deepcopy(schedule),
                "next_request_seed": schedule[0], "original_next_generation_seed": restored["next_generation_seed"],
                "remaining_calls": restored["remaining_calls"],
                "remaining_completion_tokens": restored["remaining_completion_tokens"],
                "remaining_tool_calls": restored["remaining_tool_calls"],
            })
        except Exception as exc:
            row["error"] = {"type": type(exc).__name__, "message": str(exc)}
        rows.append(row)
    return rows


def retain_branch_results(ledger: dict, *, expected_ledger_sha256: str, outcomes: list[dict]) -> list[dict]:
    """Keep assigned denominators; absent output is unstarted, never negative."""
    assignments = _ledger(ledger, expected_ledger_sha256)
    _json(outcomes)
    if type(outcomes) is not list:
        raise ValueError("Outcomes must be a list")
    by_id = {row["branch_id"]: row for row in assignments}
    supplied = {}
    for outcome in outcomes:
        if type(outcome) is not dict or not {"branch_id", "assignment_sha256", "status"} <= set(outcome):
            raise ValueError("Outcome lacks branch identity/status")
        branch_id = outcome["branch_id"]
        if type(branch_id) is not str or branch_id not in by_id or branch_id in supplied:
            raise ValueError("Unknown or duplicate branch result")
        if outcome["assignment_sha256"] != _hash(by_id[branch_id]) or outcome["status"] not in _RESULT_STATUSES:
            raise ValueError("Outcome assignment or terminal status mismatch")
        for key, expected in (("assignment", by_id[branch_id]), ("ledger_sha256", expected_ledger_sha256),
                              ("parent_checkpoint_sha256", by_id[branch_id]["parent_checkpoint_sha256"])):
            if key in outcome and _hash(outcome[key]) != _hash(expected):
                raise ValueError("Outcome contains conflicting branch identity")
        supplied[branch_id] = outcome
    return [{"branch_id": row["branch_id"], "assignment": copy.deepcopy(row),
             "assignment_sha256": _hash(row), "ledger_sha256": expected_ledger_sha256,
             "status": supplied[row["branch_id"]]["status"] if row["branch_id"] in supplied else "unstarted",
             "outcome": copy.deepcopy(supplied.get(row["branch_id"]))}
            for row in assignments]
