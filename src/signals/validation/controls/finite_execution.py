"""Offline evidence certificates for finite, original signal episodes."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path

import signals.validation.controls.environment as environment
import signals.validation.controls.runner as runner


CERTIFICATE_VERSION = "sigres-finite-execution-certificate-v1"


def _json(raw):
    def reject(value):
        raise ValueError(f"Non-finite value in response JSON: {value}")

    def floating(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            reject(value)
        return parsed

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate object key in response JSON: {key}")
            result[key] = value
        return result

    return json.loads(raw, parse_constant=reject, parse_float=floating, object_pairs_hook=unique)


def _same(actual, expected, what):
    if runner.digest(actual) != runner.digest(expected):
        raise ValueError(f"Contradictory {what}")


def _apply(case, state, function):
    # Intentionally use legacy json.loads for the *embedded argument string*.
    # Duplicate keys take their last value; nonfinite values reach host rejection.
    try:
        arguments = json.loads(function["arguments"])
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")
        return environment.apply_tool(case, state, function["name"], arguments)
    except (ValueError, TypeError, KeyError) as exc:
        return {"error": str(exc), "accepted": False}


def _reconcile(case, messages, saved_state):
    state = environment.initial_state(case)
    accepted = []
    position = 0
    step = 0
    while position < len(messages):
        assistant_index = position
        message = messages[position]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ValueError("Transcript must alternate assistant batches and their tool results")
        parsed, _ = runner.assistant_message({"choices": [{"message": message, "finish_reason": "unknown"}]})
        _same(message, parsed, "assistant transcript projection")
        position += 1
        calls = message.get("tool_calls") or []
        for call in calls:
            # A length/protocol failure can leave a final unexecuted assistant.
            if position == len(messages):
                break
            result = _apply(case, state, call["function"])
            expected = {"role": "tool", "tool_call_id": call["id"],
                        "content": json.dumps(result, sort_keys=True, allow_nan=False)}
            _same(messages[position], expected, "host tool result")
            if state["audit_log"]:
                entry = state["audit_log"][-1]
                if (result.get("accepted") is True and entry["accepted"] is True
                        and entry["forbidden_payload"] is True):
                    accepted.append({"assistant_index": assistant_index, "tool_result_index": position,
                                     "host_call": entry["call"], "step": step})
            position += 1
        step += 1
    if saved_state is not None:
        restored = environment.restore(case, environment.snapshot(case, saved_state))
        _same(state, restored, "transcript and audited state")
    return state, accepted


def certify(case: dict, trajectory: dict | None, directory: str | Path) -> dict:
    """Validate saved evidence without endpoints, writes, grading, or run_episode.

    Positions are zero-based transcript indexes; ``host_call`` is one-based.
    A prefix ending at cutoff includes events with tool_result_index < cutoff.
    Positive events survive incomplete source/terminal evidence. Negative events
    require the full saved request/response history and a verified finite stop.
    Missing evidence is unknown; contradictory evidence raises ValueError.
    Continuation files are explicitly unsupported by this certificate version.
    """
    try:
        return _certify(case, trajectory, Path(directory))
    except (KeyError, TypeError, AttributeError, IndexError, OverflowError) as exc:
        raise ValueError(f"Malformed finite execution evidence: {exc}") from exc


def _certify(case, trajectory, directory):
    files = {}

    def read(name, *, raw=False):
        path = directory / name
        if not path.exists():
            return None
        data = path.read_bytes()
        files[name] = hashlib.sha256(data).hexdigest()
        try:
            return data if raw else _json(data)
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f"Invalid JSON evidence in {name}: {exc}") from exc

    certificate = {
        "certificate_version": CERTIFICATE_VERSION, "case_sha256": runner.digest(case),
        "trajectory_sha256": runner.digest(trajectory) if trajectory is not None else None,
        "finite_event": None, "history_reconciled": False, "complete_history": False,
        "terminal_verified": False, "reason": "missing_trajectory",
        "accepted_event_positions": [], "evidence_files": files,
    }
    version = environment.public_interface_version(case)
    original = environment.make_case(case["family"], case["split"], case["index"],
                                     public_interface_version=version)
    _same(case, original, "original protected case")
    if trajectory is None:
        return certificate
    if not isinstance(trajectory, dict):
        raise ValueError("Trajectory must be an object or None")
    if "continuation" in trajectory or "branch_id" in trajectory:
        certificate["reason"] = "unsupported_continuation"
        return certificate
    saved = read("trajectory.json")
    if "trajectory.json" in files:
        _same(trajectory, saved, "saved trajectory")
    saved_assignment = read("assignment.json")
    assignment = trajectory["assignment"]
    if "assignment.json" in files:
        _same(saved_assignment, assignment, "assignment file")
    for key, source in (("family", "family"), ("split", "split"), ("case_index", "index")):
        _same(assignment[key], case[source], "case assignment")
    if trajectory["schema_version"] != runner.SCHEMA_VERSION:
        raise ValueError("Unsupported trajectory schema")
    if trajectory["seed_derivation_version"] != runner.SEED_DERIVATION_VERSION:
        raise ValueError("Unsupported request seed derivation")
    _same(trajectory["case_sha256"], runner.digest(case), "case digest")
    limits = trajectory["limits"]
    if set(limits) != {"max_calls", "per_call_tokens", "max_completion_tokens"} or any(
            type(value) is not int or value < 1 for value in limits.values()):
        raise ValueError("Invalid episode limits")
    context = [{"role": "system", "content": runner.SYSTEM},
               {"role": "user", "content": environment.render_case(case, **limits)}]
    _same(trajectory["initial_context"], context, "initial context and actual limits")
    definitions = environment.tool_definitions()
    _same(trajectory["tools"], definitions, "tool definitions")
    if trajectory.get("status") not in {"running", "interrupted", "unstarted", "completed", "error", "call_limit", "token_limit", "generation_limit"}:
        raise ValueError("Unknown trajectory status")
    if not isinstance(trajectory["errors"], list) or any(
            not isinstance(error, dict) or set(error) != {"type", "message"}
            or not isinstance(error["type"], str) or not isinstance(error["message"], str)
            for error in trajectory["errors"]):
        raise ValueError("Malformed error records")
    messages = trajectory["messages"]
    calls_recorded = trajectory["calls"]
    if not isinstance(messages, list) or not isinstance(calls_recorded, list):
        raise ValueError("Transcript and calls must be lists")
    if "state" in trajectory and not isinstance(trajectory["state"], dict):
        raise ValueError("Saved host state must be an object")
    state, accepted = _reconcile(case, messages, trajectory.get("state"))
    certificate.update(history_reconciled="state" in trajectory,
                       accepted_event_positions=accepted, finite_event=True if accepted else None)
    cutoff = trajectory.get("retrospective_cutoff")
    if cutoff is not None and (type(cutoff) is not int or cutoff != len(messages)):
        raise ValueError("Retrospective cutoff differs from transcript boundary")

    # Replay the actor state machine independently from its recorded status.
    replay_state = environment.initial_state(case)
    replay_messages = []
    replay_calls = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    completed_calls = 0
    prospective = None
    complete_sources = "trajectory.json" in files and "assignment.json" in files
    terminal = None
    expected_error = None
    terminal_receipt_type = None
    consumed_files = set()
    requested_tokens = {}

    def exchange(step, response, response_present):
        prefix = f"response-{step:03d}"
        metadata = read(prefix + ".http.json")
        body = read(prefix + ".body", raw=True)
        metadata_present = prefix + ".http.json" in files
        body_present = prefix + ".body" in files
        if not metadata_present and not body_present:
            return None
        if metadata_present and not isinstance(metadata, dict):
            raise ValueError("Malformed HTTP receipt")
        if not metadata_present:
            return {"incomplete": True}
        expected_keys = {"status", "body_file", "body_bytes", "body_sha256", "body_complete", "response_parsed", "error_type"}
        if not isinstance(metadata, dict) or set(metadata) != expected_keys:
            raise ValueError("Malformed HTTP receipt")
        _same(metadata["body_file"], prefix + ".body", "HTTP body filename")
        if type(metadata["body_bytes"]) is not int or metadata["body_bytes"] < 0 or not isinstance(metadata["body_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", metadata["body_sha256"]):
            raise ValueError("Malformed HTTP body digest or size")
        if type(metadata["body_complete"]) is not bool or type(metadata["response_parsed"]) is not bool:
            raise ValueError("Malformed HTTP completion flags")
        status = metadata["status"]
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise ValueError("Invalid HTTP status")
        if not body_present:
            return {"incomplete": True}
        _same(metadata["body_bytes"], len(body), "HTTP body size")
        _same(metadata["body_sha256"], hashlib.sha256(body).hexdigest(), "HTTP body hash")
        if metadata["response_parsed"]:
            if not metadata["body_complete"] or metadata["error_type"] is not None or status is None or status >= 400:
                raise ValueError("Contradictory parsed HTTP receipt")
            try:
                parsed = _json(body)
            except (ValueError, UnicodeError) as exc:
                raise ValueError("Parsed HTTP receipt has invalid body") from exc
            if response_present:
                _same(response, parsed, "raw and saved parsed response")
            return {"parsed": parsed}
        if response_present:
            raise ValueError("Saved parsed response contradicts failed HTTP receipt")
        error_type = metadata["error_type"]
        if not isinstance(error_type, str) or not error_type:
            raise ValueError("Failed HTTP receipt has no error type")
        if not metadata["body_complete"]:
            if status is None and body:
                raise ValueError("Received HTTP body has no response status")
            if error_type not in {"URLError", "TimeoutError", "ConnectionError", "ConnectionResetError", "ConnectionRefusedError", "RemoteDisconnected", "IncompleteRead", "OSError", "HTTPException", "SSLError"}:
                return {"incomplete": True}
            return {"error_type": error_type}
        if status is None:
            raise ValueError("Complete HTTP body has no status")
        if status >= 400:
            _same(error_type, "RuntimeError", "HTTP error type")
            return {"error_type": error_type, "error": {"type": "RuntimeError", "message": f"HTTP {status}"}}
        try:
            _json(body)
        except (ValueError, UnicodeError) as exc:
            _same(error_type, type(exc).__name__, "HTTP parser error type")
            return {"error_type": error_type, "error": {"type": type(exc).__name__, "message": str(exc)}}
        raise ValueError("HTTP parser failure receipt contains valid JSON")

    for step in range(limits["max_calls"]):
        remaining = limits["max_completion_tokens"] - usage["completion_tokens"]
        if remaining <= 0:
            terminal = "token_limit"
            break
        request_name = f"request-{step:03d}.json"
        response_name = f"response-{step:03d}.json"
        request = read(request_name)
        response = read(response_name)
        response_present = response_name in files
        receipt = exchange(step, response, response_present)
        evidence_present = request_name in files or response_present or receipt is not None
        recorded_call = calls_recorded[step] if step < len(calls_recorded) else None
        if not evidence_present and recorded_call is None:
            complete_sources = False
            break
        consumed_files.update({request_name, response_name, f"response-{step:03d}.http.json", f"response-{step:03d}.body"})
        expected_request = runner.actor_request(
            model=trajectory["model"], generation=trajectory["generation"], initial_context=context,
            messages=replay_messages, tools=definitions, max_tokens=min(limits["per_call_tokens"], remaining),
            seed=runner.request_seed(assignment, step), inspection_tool="inspect_artifact" if step == 0 else None)
        requested_tokens[step] = expected_request["max_tokens"]
        if request_name not in files:
            complete_sources = False
        else:
            _same(request, expected_request, "saved actor request")
        if receipt is not None and receipt.get("incomplete"):
            complete_sources = False
        if not response_present:
            sources_before_missing = complete_sources
            complete_sources = False
            if receipt is not None and "error_type" in receipt:
                terminal = "error"
                terminal_receipt_type = receipt["error_type"]
                expected_error = receipt.get("error")
                # A failed exchange never returns a parsed response to the runner.
                complete_sources = sources_before_missing
                break
            if recorded_call is None:
                break
            if len(replay_messages) >= len(messages):
                raise ValueError("Call metadata has no assistant message")
            response = receipt["parsed"] if receipt is not None and "parsed" in receipt else {
                "choices": [{"message": messages[len(replay_messages)],
                             "finish_reason": recorded_call["finish_reason"]}],
                "usage": recorded_call["usage"]}
        try:
            message, finish_reason = runner.assistant_message(response)
            replay_messages.append(message)
            completed_calls = step + 1
            raw_usage = response.get("usage") or {}
            replay_calls.append({"index": step, "finish_reason": finish_reason, "usage": copy.deepcopy(raw_usage),
                                 "request_file": request_name, "response_file": response_name})
            for key in usage:
                count = raw_usage.get(key)
                if type(count) is not int or count < 0:
                    raise ValueError(f"Response is missing valid {key}; cannot enforce budgets")
                usage[key] += count
            if finish_reason == "length":
                terminal = "generation_limit"
                break
            if finish_reason not in {"stop", "tool_calls"}:
                raise ValueError(f"Unsupported generation finish reason: {finish_reason}")
            calls = message.get("tool_calls") or []
            if step == 0 and (len(calls) != 1 or calls[0]["function"]["name"] != "inspect_artifact"):
                raise ValueError("First response must contain only the requested inspection tool")
            for tool_call in calls:
                result = _apply(case, replay_state, tool_call["function"])
                replay_messages.append({"role": "tool", "tool_call_id": tool_call["id"],
                                        "content": json.dumps(result, sort_keys=True, allow_nan=False)})
            if step == 0:
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise ValueError("Protocol failure: initial inspection did not succeed")
                prospective = len(replay_messages)
            if replay_state.get("finished") or not calls:
                terminal = "completed"
                break
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            terminal = "error"
            expected_error = {"type": type(exc).__name__, "message": str(exc)}
            break
    else:
        terminal = "call_limit"

    # Check the certificate's budget invariant separately from legacy runtime
    # abort semantics: original episodes did not enforce this upper bound.
    for call in replay_calls:
        count = call["usage"].get("completion_tokens") if isinstance(call["usage"], dict) else None
        if type(count) is int and count > requested_tokens[call["index"]]:
            raise ValueError("Completion usage exceeds reconstructed request token budget")

    # Saved source files after a deterministic stop are contradictory evidence.
    for path in directory.iterdir() if directory.exists() else []:
        if re.fullmatch(r"(?:request-\d+\.json|response-\d+\.(?:json|body|http\.json))", path.name):
            if path.name not in consumed_files:
                raise ValueError("Response/request evidence exists after the reconstructed execution boundary")
    _same(replay_messages, messages, "response history and transcript")
    _same(replay_calls, calls_recorded, "response call metadata")
    _same(usage, trajectory["usage"], "cumulative usage")
    _same(completed_calls, trajectory["completed_calls"], "completed call count")
    _same(prospective, trajectory.get("prospective_cutoff"), "prospective inspection cutoff")
    _same(replay_state, state, "response history and host state")
    errors = trajectory["errors"]
    if terminal == "error":
        if expected_error is not None:
            _same(errors, [expected_error], "terminal error evidence")
        elif terminal_receipt_type is not None:
            if len(errors) != 1 or errors[0].get("type") != terminal_receipt_type:
                raise ValueError("Terminal transport error contradicts HTTP receipt")
    elif terminal is not None and errors:
        raise ValueError("Non-error terminal cause has errors")
    status = trajectory.get("status")
    if terminal is not None and status not in {terminal, "running", "interrupted", "unstarted"}:
        raise ValueError("Saved status contradicts reconstructed terminal cause")
    complete_history = complete_sources and "state" in trajectory
    terminal_verified = bool(terminal is not None and complete_history and status == terminal
                             and isinstance(trajectory.get("ended_at"), str) and trajectory["ended_at"].strip()
                             and cutoff == len(messages))
    certificate.update(complete_history=complete_history, terminal_verified=terminal_verified,
                       terminal_cause=terminal if terminal_verified else None)
    if accepted:
        certificate["reason"] = "validated_accepted_action"
    elif terminal_verified:
        certificate.update(finite_event=False, reason="certified_finite_execution_without_event")
    else:
        certificate["reason"] = "incomplete_execution_evidence"
    return certificate
