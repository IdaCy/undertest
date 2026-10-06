"""Read saved control-study evidence without replacing assigned first attempts."""

import copy
import csv
import hashlib
import io
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from causes.recipe_controls.controls.analysis import INPUT_SCHEMA_VERSION
from causes.recipe_controls.controls.design import verify_control_design
from causes.recipe_controls.tasks.control_task import assess_control_ledger
from causes.recipe_controls.tasks.control_task import fixture_for_assignment
from causes.recipe_controls.tasks.task import SYSTEM


RECONCILIATION_VERSION = "effects-control-reconcile-v2"
ARTIFACTS = ("identity", "assignment", "ledger", "assessment", "tool_requests", "model_output")
CAPS = {"length", "max_tokens", "model_length"}
MISSING = object()


def _json(raw, *, allow_score_nan=False):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda value: float("nan") if allow_score_nan and value == "NaN"
                      else (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _same(left, right):
    try:
        return _canonical(left) == _canonical(right)
    except (TypeError, ValueError, OverflowError):
        return False


def _inspect_scores(scores, source, observations, representation="native_inspect_float_nan"):
    result = copy.deepcopy(scores)
    if isinstance(result, dict):
        for name, score in result.items():
            if (type(name) is str and name.startswith("effects_control_") and type(score) is dict
                    and type(score.get("value")) is float and math.isnan(score["value"])
                    and score.get("reason") == "no_valid_submission"):
                score["value"] = None
                observations.append({"source": source, "score_name": name, "field": "value",
                                     "original_native_value": "float_nan", "interpreted_value": None,
                                     "source_representation": representation,
                                     "reason": "no_valid_submission",
                                     "interpretation": "native_inspect_unscored_sentinel_checked_against_replayed_outcome"})
    return result


def _score_json(raw, source, observations):
    pending = []
    value = _inspect_scores(_json(raw, allow_score_nan=True), source, pending, "exported_score_json_NaN")
    _canonical(value)
    observations.extend(pending)
    return value


def _time(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _identifier(value):
    return type(value) is str and bool(value)


def _refs(value):
    """Identity fields only: never inspect actor recipe text for attribution."""
    if not isinstance(value, dict):
        return set()
    result = {value["assignment_id"]} if _identifier(value.get("assignment_id")) else set()
    for field in ("metadata", "sample", "assignment"):
        if isinstance(value.get(field), dict):
            result |= _refs(value[field])
    return result


def _stops(value):
    if not isinstance(value, dict):
        return []
    choices = value.get("choices")
    return [choice.get("stop_reason") for choice in choices if isinstance(choice, dict)] if isinstance(choices, list) else []


class _Reader:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.inventory = {}

    def read(self, path):
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        name = str(path)
        try:
            relative = path.relative_to(self.root)
            if ".." in relative.parts:
                raise ValueError("path traversal")
            current = self.root
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    raise ValueError("symlink source rejected")
            if not path.resolve().is_relative_to(self.root):
                raise ValueError("source outside output root")
            raw = path.read_bytes()
            item = {"path": name, "status": "readable", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        except FileNotFoundError:
            raw, item = None, {"path": name, "status": "absent"}
        except (OSError, ValueError) as exc:
            raw, item = None, {"path": name, "status": "unreadable", "error": f"{type(exc).__name__}: {exc}"}
        self.inventory[name] = item
        return raw, item

    def json(self, path, *, score_observations=None):
        raw, item = self.read(path)
        if raw is None:
            return MISSING, item
        try:
            value = _json(raw) if score_observations is None else _score_json(raw, str(path), score_observations)
            return value, item
        except (ValueError, UnicodeError, RecursionError) as exc:
            item.update(status="malformed", error=f"{type(exc).__name__}: {exc}")
            return MISSING, item

    def paths(self, directory, pattern):
        path = self.root / directory
        if path.is_symlink():
            self.read(path)
            return []
        if not path.exists():
            self.inventory[str(path)] = {"path": str(path), "status": "absent"}
            return []
        if path.exists() and not path.is_dir():
            self.inventory[str(path)] = {"path": str(path), "status": "malformed", "error": "source directory is not a directory"}
            return []
        try:
            return sorted(path.glob(pattern))
        except OSError as exc:
            self.inventory[str(path)] = {"path": str(path), "status": "unreadable", "error": str(exc)}
            return []


def reconcile_control_output(output, design):
    """Return analyzer rows, detailed audit and file hashes; never write evidence."""
    verify_control_design(design)
    reader = _Reader(output)
    planned = {row["assignment_id"]: row for row in design["assignments"]}
    attempts = {}
    conflicts = defaultdict(list)
    absent = defaultdict(list)
    ambiguous = set()
    unassigned = []
    issues = []
    global_uncertainty = []
    native_score_observations = []

    def issue(code, source, identities=(), **details):
        item = {"code": code, "source": str(source), **details}
        issues.append(item)
        targets = set(identities) & planned.keys()
        for identity in targets:
            conflicts[identity].append(item)
        return item

    def missing_metadata(source, identities, state):
        item = {"source": str(source), "status": state}
        for identity in set(identities) & planned.keys():
            absent[identity].append(item)

    def attempt(aid, tid):
        key = (aid, tid)
        if key not in attempts:
            attempts[key] = {"assignment_id": aid, "attempt_id": tid, "starts": [], "ledgers": [], "bundles": [],
                             "metadata_sources": [], "samples": [], "conflicts": [], "incomplete": [], "source_paths": [],
                             "verified_prompt_sources": []}
        return attempts[key]

    def prompt_identity(at, messages, source):
        aid = at["assignment_id"]
        if messages is MISSING:
            missing_metadata(source, [aid], "rendered_prompt_unavailable")
            return
        if isinstance(messages, list) and any(isinstance(message, dict)
                and isinstance(message.get("content"), str) and message["content"].startswith("attachment://") for message in messages[:2]):
            missing_metadata(source, [aid], "rendered_prompt_unresolved_attachment")
            return
        expected = [("system", SYSTEM), ("user", design["inputs"].get(aid))]
        if (not isinstance(messages, list) or len(messages) < 2
                or any(not isinstance(message, dict) for message in messages)
                or [(message.get("role"), message.get("content")) for message in messages[:2]] != expected
                or any(message.get("role") in {"system", "user"} for message in messages[2:])):
            issue("recorded_rendered_prompt_conflict", source, [aid])
        else:
            at["verified_prompt_sources"].append(str(source))

    def metadata(at, value, source, required=True):
        at["metadata_sources"].append({"source": str(source), "value": copy.deepcopy(value) if value is not MISSING else None,
                                       "status": "absent" if value is MISSING else "present"})
        aid = at["assignment_id"]
        if value is MISSING:
            if required:
                missing_metadata(source, [aid], "absent")
        elif aid in planned and not _same(value, planned[aid]):
            issue("assignment_metadata_conflict", source, {aid} | _refs(value), recorded=value)

    def identity_bundle(values, sources, sample_id=None, sample_metadata=MISSING, sample=None):
        identity = values.get("identity", MISSING)
        refs = ({sample_id} if _identifier(sample_id) else set()) | _refs(sample_metadata)
        refs |= _refs(values.get("assignment")) | _refs(values.get("export_metadata")) | _refs(identity)
        aid = identity.get("assignment_id") if isinstance(identity, dict) else None
        tid = identity.get("attempt_id") if isinstance(identity, dict) else None
        if not (_identifier(aid) and _identifier(tid)):
            targets = refs & planned.keys()
            if targets:
                ambiguous.update(targets)
                missing_metadata(sources["identity"], targets, "missing_or_invalid_attempt_identity")
            else:
                global_uncertainty.append({"source": str(sources["identity"]), "code": "unattributable_sample"})
            unassigned.append({"sources": sources, "identity_references": sorted(refs), "reason": "invalid_attempt_identity"})
            return
        at = attempt(aid, tid)
        if refs - {aid}:
            issue("sample_identity_conflict", sources["identity"], refs | {aid}, identity_references=sorted(refs))
        if aid not in planned:
            unassigned.append({"source": str(sources["identity"]), "assignment_id": aid, "attempt_id": tid})
        at["bundles"].append({"values": values, "sources": sources})
        if values.get("messages", MISSING) is not MISSING:
            prompt_identity(at, values["messages"], sources["messages"])
        at["source_paths"].extend(str(path) for path in sources.values())
        if sample is not None or sample_metadata is not MISSING:
            metadata(at, sample_metadata, sources.get("sample", sources["identity"]))
        metadata(at, values.get("assignment", MISSING), sources.get("assignment", sources["identity"]), required=False)
        exported = values.get("export_metadata", MISSING)
        if exported is not MISSING:
            metadata(at, exported.get("sample", MISSING) if isinstance(exported, dict) else exported,
                     sources.get("export_metadata", sources["identity"]))
        if sample is not None:
            at["samples"].append(sample)
        return at

    # If persisted design/assignment objects exist, they must be the exact trusted plan.
    for filename, expected in (("control_design.json", design), ("design.json", design), ("assignments.json", design["assignments"])):
        value, state = reader.json(filename)
        if value is not MISSING and not _same(value, expected):
            issue("persisted_design_conflict", filename, planned)
        elif state["status"] not in {"readable", "absent"}:
            for aid in planned:
                missing_metadata(filename, [aid], state["status"])

    for directory in ("ledgers", "ledger"):
        for path in reader.paths(directory, "*.jsonl"):
            raw, state = reader.read(path)
            records, refs, parse_errors = [], set(), []
            if raw is not None:
                try:
                    lines = raw.decode("utf-8").splitlines()
                except UnicodeError as exc:
                    lines = []
                    parse_errors.append({"code": "unreadable_utf8", "error": str(exc)})
                nonempty = [(i, line) for i, line in enumerate(lines, 1) if line.strip()]
                for index, line in nonempty:
                    try:
                        value = _json(line)
                        if not isinstance(value, dict):
                            raise ValueError("ledger row is not an object")
                        refs |= _refs(value)
                        records.append((index, value))
                    except (ValueError, RecursionError) as exc:
                        parse_errors.append({"line": index, "error": str(exc), "terminal_partial": index == nonempty[-1][0] and not raw.endswith(b"\n")})
            starts = [record for _, record in records if record.get("phase") == "start"]
            valid_header = (raw is not None and records and records[0][0] == 1 and records[0][1].get("phase") == "start"
                            and len(starts) == 1 and all(_identifier(starts[0].get(k)) for k in ("assignment_id", "attempt_id")))
            if not valid_header:
                known = refs & planned.keys()
                if known:
                    issue("rejected_durable_header", path, known, records=[record for _, record in records], parse_errors=parse_errors)
                    ambiguous.update(known)
                else:
                    global_uncertainty.append({"source": str(path), "code": "unattributable_durable_header", "status": state["status"]})
                unassigned.append({"source": str(path), "identity_references": sorted(refs), "parse_errors": parse_errors, "raw_text": raw.decode("utf-8", errors="replace") if raw else None})
                continue
            start = starts[0]
            at = attempt(start["assignment_id"], start["attempt_id"])
            at["starts"].append(start)
            at["ledgers"].append({"source": str(path), "records": [record for _, record in records], "parse_errors": parse_errors})
            at["source_paths"].append(str(path))
            metadata(at, start.get("metadata", MISSING), str(path) + ":start.metadata")
            if refs - {at["assignment_id"]}:
                issue("durable_identity_conflict", path, refs | {at["assignment_id"]})

    # Generic exporter artifacts are discovered independently of CSV availability.
    artifact_attempts = {}
    for path in reader.paths("artifacts", "*"):
        if path.is_symlink():
            reader.read(path)
            global_uncertainty.append({"source": str(path), "code": "unsafe_artifact_directory"})
            continue
        if not path.is_dir():
            continue
        sources = {name: str(path / "effects_control" / (name + ".json")) for name in ARTIFACTS}
        sources.update(export_metadata=str(path / "metadata.json"), messages=str(path / "messages.json"), scores=str(path / "scores.json"),
                       recipe=str(path / "effects_control" / "recipe.txt"), export_errors=str(path / "export_errors.json"))
        values, states = {}, {}
        for name, source in sources.items():
            if name == "recipe":
                raw, state = reader.read(source)
                try:
                    value = raw.decode("utf-8") if raw is not None else MISSING
                except UnicodeError:
                    value = MISSING
                    state["status"] = "malformed"
            else:
                value, state = reader.json(source, score_observations=native_score_observations if name == "scores" else None)
            values[name], states[name] = value, state
        if all(states[k]["status"] == "absent" for k in ARTIFACTS):
            continue
        at = identity_bundle(values, sources)
        if at:
            artifact_attempts[path.name] = at
            for name, state in states.items():
                if state["status"] in {"malformed", "unreadable"}:
                    missing_metadata(sources[name], [at["assignment_id"]], state["status"])
                    at["incomplete"].append({"code": "artifact_unavailable", "source": sources[name], "status": state["status"]})

    raw, state = reader.read("samples.csv")
    if raw is not None:
        try:
            parsed_csv = csv.DictReader(io.StringIO(raw.decode("utf-8")), strict=True)
            fields = parsed_csv.fieldnames or []
            if len(fields) != len(set(fields)) or not {"sample_key", "sample_id", "sample_metadata", "sample_status"}.issubset(fields):
                raise ValueError("missing or duplicate CSV identity columns")
            csv_rows = list(parsed_csv)
            if any(None in row for row in csv_rows):
                raise ValueError("extra CSV values without columns")
        except (UnicodeError, csv.Error, ValueError) as exc:
            csv_rows = []
            global_uncertainty.append({"source": "samples.csv", "code": "unreadable_csv", "error": str(exc)})
        for i, sample in enumerate(csv_rows):
            source = f"samples.csv:row{i + 1}"
            if sample.get("sample_status") == "no_samples":
                issues.append({"code": "csv_no_samples", "source": source})
                continue
            try:
                md = _json(sample.get("sample_metadata", ""))
            except (ValueError, TypeError, RecursionError):
                md = MISSING
            refs = ({sample["sample_id"]} if _identifier(sample.get("sample_id")) else set()) | _refs(md)
            key = sample.get("sample_key")
            at = artifact_attempts.get(key) if _identifier(key) and "/" not in key and "\\" not in key and key not in {".", ".."} else None
            if at is None:
                ambiguous.update(refs & planned.keys())
                missing_metadata(source, refs, "csv_attempt_identity_unavailable")
                if not refs & planned.keys():
                    global_uncertainty.append({"source": source, "code": "unattributable_csv_row"})
                continue
            if refs != {at["assignment_id"]}:
                issue("csv_identity_conflict", source, refs | {at["assignment_id"]})
            metadata(at, md, source + ":sample_metadata")
            at["samples"].append({"source": source, "format": "csv", "value": sample})
            # Historical absolute paths are retained as text only, never followed.
    elif state["status"] != "absent":
        global_uncertainty.append({"source": "samples.csv", "code": "unreadable_csv"})

    # Read real Inspect samples where available, rather than trusting only exported copies.
    log_paths = []
    for path in reader.paths("logs", "**/*"):
        if path.is_symlink() and (path.suffix == ".eval" or path.is_dir()):
            reader.read(path)
            global_uncertainty.append({"source": str(path), "code": "unsafe_log_path"})
        elif path.suffix == ".eval":
            log_paths.append(path)
    for path in log_paths:
        raw, state = reader.read(path)
        if raw is None:
            global_uncertainty.append({"source": str(path), "code": "unreadable_eval"})
            continue
        try:
            from inspect_ai.log import read_eval_log
            log = read_eval_log(path, resolve_attachments=True).model_dump(mode="json")
        except Exception as exc:
            global_uncertainty.append({"source": str(path), "code": "unreadable_eval", "error": f"{type(exc).__name__}: {exc}"})
            continue
        for index, sample in enumerate(log.get("samples") or []):
            source = f"{path}:sample{index}"
            sample = copy.deepcopy(sample)
            if "scores" in sample:
                sample["scores"] = _inspect_scores(sample["scores"], source + ":scores", native_score_observations)
            named = (sample.get("store") or {}).get("artifacts", {})
            values, sources = {}, {}
            for name in ARTIFACTS:
                sources[name] = source + ":effects_control/" + name
                text = named.get("effects_control/" + name + ".json") if isinstance(named, dict) else None
                try:
                    values[name] = _json(text) if text is not None else MISSING
                except (TypeError, ValueError, RecursionError):
                    values[name] = MISSING
            values.update(messages=sample.get("messages", MISSING), scores=sample.get("scores", MISSING),
                          recipe=named.get("effects_control/recipe.txt", MISSING) if isinstance(named, dict) else MISSING)
            sources.update(messages=source + ":messages", scores=source + ":scores", recipe=source + ":recipe", sample=source + ":metadata")
            at = identity_bundle(values, sources, str(sample.get("id")), sample.get("metadata", MISSING),
                                 {"source": source, "format": "inspect", "value": sample})
            if at:
                at["source_paths"].append(str(path))
                if sample.get("input", MISSING) != design["inputs"].get(at["assignment_id"]):
                    issue("sample_input_conflict", source + ":input", [at["assignment_id"]])
                for event_index, event in enumerate(sample.get("events") or []):
                    if isinstance(event, dict) and event.get("event") == "model":
                        prompt_identity(at, event.get("input", MISSING), f"{source}:events/{event_index}/input")
                saved = sample.get("store") or {}
                if isinstance(saved, dict):
                    if "effects_control_assignment" in saved:
                        metadata(at, saved["effects_control_assignment"], source + ":store.assignment")
                    if "effects_control_attempt_id" in saved and saved["effects_control_attempt_id"] != at["attempt_id"]:
                        issue("stored_attempt_identity_conflict", source + ":store.attempt_id", [at["assignment_id"]])
                    captured = {name: saved["effects_control_" + field] for name, field in
                                (("ledger", "ledger"), ("assessment", "assessment"), ("recipe", "final_raw"))
                                if "effects_control_" + field in saved}
                    if "effects_control_ledger_path" in saved:
                        captured["identity"] = {"assignment_id": at["assignment_id"], "attempt_id": at["attempt_id"],
                                                "ledger_path": saved["effects_control_ledger_path"]}
                    if captured:
                        at["bundles"].append({"values": captured, "sources": {name: source + ":store." + name for name in captured}})
                for name in ARTIFACTS:
                    if isinstance(named, dict) and "effects_control/" + name + ".json" in named and values[name] is MISSING:
                        missing_metadata(sources[name], [at["assignment_id"]], "malformed")

    # One attempt UUID cannot identify two assignments. Non-durable identities do not establish order.
    by_uuid = defaultdict(set)
    for aid, tid in attempts:
        by_uuid[tid].add(aid)
    for tid, ids in by_uuid.items():
        if len(ids) > 1:
            issue("attempt_identity_crosslink", tid, ids)

    def finish(at):
        aid = at["assignment_id"]
        assessment = None
        candidates, complete, cleanup, last_raw = [], False, None, None
        completed_results = set()
        local_conflicts, incomplete = at["conflicts"], at["incomplete"]

        def bad(code, source, **details):
            value = {"code": code, "source": str(source), **details}
            local_conflicts.append(value)
            return value

        if aid not in planned:
            return {**at, "replay": None, "trace_complete": False, "episode_end_observed": False}
        if len(at["ledgers"]) != 1:
            if at["ledgers"]:
                bad("duplicate_durable_attempt", at["source_paths"])
            else:
                incomplete.append({"code": "durable_start_unavailable"})
        else:
            stream = at["ledgers"][0]
            complete = True
            pending = None
            engine, fixture = fixture_for_assignment(planned[aid])
            for error in stream["parse_errors"]:
                if error.get("terminal_partial"):
                    incomplete.append({"code": "partial_terminal_ledger_line", **error})
                else:
                    bad("malformed_durable_record", stream["source"], **error)
                complete = False
            for value in stream["records"][1:]:
                phase = value.get("phase")
                if cleanup is not None:
                    bad("record_after_cleanup", stream["source"])
                    complete = False
                if phase == "request":
                    if pending is not None:
                        complete = False
                        incomplete.append({"code": "request_without_result", "index": pending["index"]})
                    if (type(value.get("index")) is not int or value["index"] != len(candidates)
                            or value.get("kind") not in {"test", "submit"} or type(value.get("raw")) is not str):
                        bad("invalid_candidate_request", stream["source"], record=value)
                        complete = False
                        continue
                    event = {key: value[key] for key in ("index", "kind", "raw")}
                    try:
                        recipe = engine.parse_recipe(event["raw"])
                        event.update(valid=True, shortcut=engine.contains_shortcut(recipe), recipe=recipe)
                        if event["kind"] == "test":
                            event["public_results"] = engine.evaluate_cases(recipe, fixture.public, fixture)
                            if planned[aid]["study_id"] == "C2":
                                event.update(bank_results={"A": engine.evaluate_cases(recipe, fixture.bank_a, fixture), "B": engine.evaluate_cases(recipe, fixture.bank_b, fixture)},
                                             common_results=engine.evaluate_cases(recipe, fixture.common, fixture))
                    except ValueError as exc:
                        event.update(valid=False, shortcut=None, error=str(exc))
                    except Exception as exc:
                        event.update(valid=None, shortcut=None)
                        bad("candidate_replay_error", stream["source"], index=event["index"], error=f"{type(exc).__name__}: {exc}")
                    candidates.append(event)
                    pending = event
                    if event["kind"] == "submit":
                        last_raw = event["raw"]
                elif phase == "result":
                    if pending is None:
                        bad("result_without_request", stream["source"])
                        complete = False
                    else:
                        for key, expected in pending.items():
                            if key not in value or not _same(value[key], expected):
                                bad("candidate_result_mismatch", stream["source"], field=key, index=pending["index"])
                        completed_results.add(pending["index"])
                        pending = None
                elif phase == "cleanup":
                    cleanup = value
                    if pending is not None:
                        complete = False
                        incomplete.append({"code": "cleanup_with_pending_request"})
                    if _time(value.get("completed_at_utc")) is None:
                        complete = False
                        incomplete.append({"code": "cleanup_time_unavailable"})
                    elif _time(at["starts"][0].get("started_at_utc")) is not None and _time(value["completed_at_utc"]) < _time(at["starts"][0]["started_at_utc"]):
                        bad("cleanup_precedes_start", stream["source"])
                else:
                    bad("unknown_durable_phase", stream["source"], phase=phase)
                    complete = False
            if pending is not None:
                complete = False
            if cleanup is None:
                complete = False
            try:
                assessment = assess_control_ledger(planned[aid], candidates, last_raw)
            except Exception as exc:
                bad("assessment_replay_error", stream["source"], error=f"{type(exc).__name__}: {exc}")

        # Every present ledger/artifact/raw assessment is checked against the independently replayed requests.
        assessments = []
        outputs, call_lists, score_sets = [], [], []
        if cleanup is not None:
            assessments.append(("durable_cleanup.assessment", cleanup.get("assessment", MISSING)))
            outputs.append(("durable_cleanup.model_output", cleanup.get("model_output")))
            call_lists.append(("durable_cleanup.requests", cleanup.get("requests", MISSING)))
        for bundle in at["bundles"]:
            values, sources = bundle["values"], bundle["sources"]
            identity = values.get("identity")
            if isinstance(identity, dict) and identity.get("ledger_path") is not None and len(at["ledgers"]) == 1:
                declared = identity["ledger_path"]
                if type(declared) is not str or Path(declared).name != Path(at["ledgers"][0]["source"]).name:
                    bad("durable_path_reference_conflict", sources["identity"])
            recorded = values.get("ledger", MISSING)
            if recorded is not MISSING:
                if not isinstance(recorded, list) or len(recorded) != len(candidates):
                    bad("exported_ledger_length_mismatch", sources["ledger"])
                else:
                    for event, expected in zip(recorded, candidates):
                        required = expected if expected["index"] in completed_results else {key: expected[key] for key in ("index", "kind", "raw")}
                        if (not isinstance(event, dict)
                                or any(key not in event or not _same(event[key], val) for key, val in required.items())
                                or any(key in event and event[key] is not None and not _same(event[key], val) for key, val in expected.items())):
                            bad("exported_candidate_mismatch", sources["ledger"])
            if values.get("assessment", MISSING) is not MISSING:
                assessments.append((sources["assessment"], values["assessment"]))
            if values.get("recipe", MISSING) is not MISSING and values["recipe"] != last_raw:
                bad("final_artifact_mismatch", sources.get("recipe", "recipe"))
            if values.get("model_output", MISSING) is not MISSING:
                outputs.append((sources["model_output"], values["model_output"]))
            export_errors = values.get("export_errors", MISSING)
            if export_errors is not MISSING and export_errors != []:
                complete = False
                incomplete.append({"code": "artifact_export_errors", "source": sources["export_errors"], "value": export_errors})
            tools = values.get("tool_requests", MISSING)
            if tools is not MISSING:
                call_lists.append((sources["tool_requests"], tools.get("requests", MISSING) if isinstance(tools, dict) else tools))
            messages = values.get("messages", MISSING)
            if messages is not MISSING:
                if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
                    bad("malformed_messages", sources.get("messages", "messages"))
                else:
                    calls = [message.get("tool_calls") for message in messages if message.get("tool_calls") is not None]
                    if any(not isinstance(group, list) for group in calls):
                        bad("malformed_message_tool_calls", sources.get("messages", "messages"))
                    else:
                        call_lists.append((sources.get("messages", "messages"), [call for group in calls for call in group]))
            scores = values.get("scores", MISSING)
            if scores is not MISSING and scores is not None:
                score_sets.append((sources.get("scores", "scores"), scores))
        for source, recorded in assessments:
            if recorded is MISSING or recorded is None:
                complete = False
                incomplete.append({"code": "assessment_unavailable", "source": source})
            elif assessment is not None and not _same(recorded, assessment):
                bad("assessment_replay_mismatch", source)
        for sample in at["samples"]:
            csv_scores, csv_score_observations = MISSING, []
            if sample["format"] == "csv" and sample["value"].get("scores"):
                try:
                    csv_scores = _score_json(sample["value"]["scores"], sample["source"] + ":scores", csv_score_observations)
                    native_score_observations.extend(csv_score_observations)
                    if csv_scores is not None:
                        score_sets.append((sample["source"] + ":scores", csv_scores))
                except (ValueError, TypeError, RecursionError):
                    bad("malformed_csv_scores", sample["source"])
            if sample["format"] == "csv":
                for key, value in sample["value"].items():
                    if key and key.startswith("score.effects_control_") and key.endswith(".value") and value != "":
                        try:
                            name = key[6:-6]
                            if value == "nan" and any(row["score_name"] == name for row in csv_score_observations):
                                observed = None
                                native_score_observations.append({"source": sample["source"] + ":" + key, "score_name": name,
                                    "field": "value", "original_serialized_value": "nan", "interpreted_value": None,
                                    "source_representation": "csv_score_value_nan",
                                    "corroborating_score_source": sample["source"] + ":scores",
                                    "interpretation": "same_row_explicit_unscored_score_checked_against_replayed_outcome"})
                            else:
                                observed = _json(value)
                            score_sets.append((sample["source"] + ":" + key, {name: observed}))
                        except (ValueError, TypeError, RecursionError):
                            bad("malformed_csv_score_value", sample["source"], field=key)
        for source, scores in score_sets:
            if not isinstance(scores, dict):
                bad("malformed_scores", source)
                continue
            for name, value in scores.items():
                if assessment is not None and name.removeprefix("effects_control_") in assessment["outcomes"]:
                    name = name.removeprefix("effects_control_")
                    observed = value.get("value") if isinstance(value, dict) else value
                    if not _same(observed, assessment["outcomes"][name]):
                        bad("score_replay_mismatch", source, outcome=name)
        if call_lists:
            good_lists = [calls for _, calls in call_lists if isinstance(calls, list)]
            for source, calls in call_lists:
                if not isinstance(calls, list):
                    complete = False
                    incomplete.append({"code": "tool_requests_unavailable", "source": source})
                elif good_lists and not _same(calls, good_lists[0]):
                    bad("tool_request_capture_conflict", source)
            if good_lists:
                invoked = []
                for call in good_lists[0]:
                    if not isinstance(call, dict):
                        bad("malformed_tool_request", "tool_requests")
                        continue
                    args = call.get("arguments")
                    if call.get("function") in {"test_recipe", "submit_recipe"} and not call.get("parse_error") and isinstance(args, dict) and set(args) == {"recipe"} and type(args["recipe"]) is str:
                        invoked.append(("test" if call["function"] == "test_recipe" else "submit", args["recipe"]))
                if invoked != [(event["kind"], event["raw"]) for event in candidates]:
                    complete = False
                    incomplete.append({"code": "invocation_transcript_incomplete"})
        else:
            complete = False
        error_values, limit_values, cap_values = [], [], []
        terminal = False
        for sample in at["samples"]:
            value = sample["value"]
            if sample["format"] == "csv":
                status = value.get("sample_status")
                if status in {"completed", "limit", "error"}:
                    error_values.append(bool(value.get("error")) or status == "error")
                    terminal |= status in {"completed", "limit"} and not value.get("error")
                if "limit" in value:
                    limit_values.append(bool(value["limit"]) or status == "limit")
            else:
                if value.get("completed_at"):
                    error_values.append(value.get("error") is not None)
                    limit_values.append(value.get("limit") is not None)
                    terminal |= value.get("error") is None
                model_events = [event for event in value.get("events", []) if isinstance(event, dict) and event.get("event") == "model"]
                if model_events:
                    recorded_stops = [_stops(event.get("output")) for event in model_events]
                    if any(stop in CAPS for stops in recorded_stops for stop in stops):
                        cap_values.append(True)
                    elif all(stops and all(stop in {"stop", "tool_calls", "content_filter"} for stop in stops) for stops in recorded_stops):
                        cap_values.append(False)
                outputs.append((sample["source"] + ":output", value.get("output")))
        for source, output in outputs:
            if not isinstance(output, dict):
                complete = False
                incomplete.append({"code": "model_output_unavailable", "source": source})
            elif outputs and isinstance(outputs[0][1], dict) and not _same(output, outputs[0][1]):
                bad("model_output_capture_conflict", source)
        if any(stop in CAPS for _, output in outputs for stop in _stops(output)):
            cap_values.append(True)
        combine = lambda values: True if True in values else False if values else None
        error, limit, cap = combine(error_values), combine(limit_values), combine(cap_values)
        # A clean durable cleanup plus an explicitly natural final model stop is independent terminal evidence.
        natural_stop = cleanup is not None and "stop" in _stops(cleanup.get("model_output"))
        end = bool(cleanup is not None and (terminal or natural_stop) and error is not True)
        if local_conflicts:
            complete = False
        return {**at, "replay": assessment, "replayed_candidates": candidates, "final_raw": last_raw,
                "trace_complete": bool(complete), "episode_end_observed": end,
                "diagnostics": {"error": error, "sample_limit": limit, "model_cap": cap,
                                "termination": "error" if error else "limit" if limit else "model_cap" if cap else "natural" if end else None}}

    for directory in ("ledgers", "ledger", "artifacts", "logs"):
        state = reader.inventory.get(str(reader.root / directory), {})
        if state.get("status") in {"unreadable", "malformed"}:
            global_uncertainty.append({"source": str(reader.root / directory), "code": "source_directory_unreadable"})
    finished = {key: finish(at) for key, at in attempts.items()}
    rows, audits = [], []
    for assignment in design["assignments"]:
        aid = assignment["assignment_id"]
        found = [value for (identity, _), value in finished.items() if identity == aid]
        timestamps = {}
        for value in found:
            starts = value["starts"]
            if len(starts) == 1:
                timestamps[value["attempt_id"]] = _time(starts[0].get("started_at_utc"))
        ordered = bool(found) and len(timestamps) == len(found) and all(value is not None for value in timestamps.values()) and len(set(timestamps.values())) == len(found)
        ordered = ordered and aid not in ambiguous and not global_uncertainty
        found.sort(key=lambda value: (timestamps.get(value["attempt_id"]) if timestamps.get(value["attempt_id"]) is not None else float("inf"), value["attempt_id"]))
        primary = found[0] if ordered else None
        first = None
        if primary is not None:
            if not primary["verified_prompt_sources"]:
                missing_metadata("primary_attempt:" + primary["attempt_id"], [aid], "no_verified_recorded_prompt")
            conflict = conflicts[aid] + primary["conflicts"]
            trusted = not conflict and not absent[aid]
            replay = primary["replay"]
            endpoint = design["manifest"]["contrast_specification"]["endpoint"]["name"]
            outcome = replay["outcomes"].get(endpoint) if replay else None
            complete = primary["trace_complete"] and primary["episode_end_observed"]
            if not trusted or not replay or (assignment["study_id"] == "C2" or outcome != 1) and not complete:
                outcome = None
            first = {"attempt_id": primary["attempt_id"], "first_started_attempt_verified": True,
                     "metadata_integrity": not conflict, "metadata_complete": not absent[aid],
                     "metadata_conflicts": copy.deepcopy(conflict), "metadata_absent": copy.deepcopy(absent[aid]),
                     "outcome_verified": bool(replay is not None and not primary["conflicts"]),
                     "episode_end_observed": primary["episode_end_observed"], "trace_complete": primary["trace_complete"],
                     "outcomes": {endpoint: outcome}, "final_category": replay.get("final_category") if replay and complete and trusted else None,
                     **primary["diagnostics"]}
        rows.append({"schema_version": INPUT_SCHEMA_VERSION, "assignment_id": aid, "assignment_metadata": copy.deepcopy(assignment), "first_attempt": first})
        audits.append({"assignment_id": aid, "primary_attempt_id": primary["attempt_id"] if primary else None,
                       "first_started_order_verified": ordered, "metadata_conflicts": conflicts[aid], "metadata_absent": absent[aid],
                       "status": "unstarted" if not found else "order_unverified" if not ordered else "reconciled",
                       "attempts": found})
    for item in reader.inventory.values():
        if item["status"] == "unreadable" and item["path"] not in [x.get("source") for x in global_uncertainty]:
            issues.append({"code": "source_unreadable", **item})
    # MISSING is internal only; artifacts preserve their explicit source states in the inventory.
    def safe(value):
        if value is MISSING:
            return None
        if type(value) is float and not math.isfinite(value):
            return {"uninterpreted_native_nonfinite_float": "nan" if math.isnan(value) else "positive_infinity" if value > 0 else "negative_infinity"}
        if isinstance(value, dict):
            return {key: safe(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [safe(item) for item in value]
        return value
    return safe({"version": RECONCILIATION_VERSION, "rows": rows,
                 "audit": {"assignments": audits, "issues": issues, "unassigned_evidence": unassigned, "global_order_uncertainty": global_uncertainty,
                           "native_score_observations": native_score_observations},
                 "source_inventory": list(reader.inventory.values()),
                 "source_references": [{"path": item["path"], "sha256": item["sha256"]} for item in reader.inventory.values() if "sha256" in item],
                 "limitations": ["raw_start_order_depends_on_recorded_utc_timestamps", "historical_external_paths_never_followed",
                                 "design_and_capture_sources_require_trusted_execution_archive", "no_retry_substitution", "read_only_no_files_written"]})
