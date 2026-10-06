"""Freeze and run sequential observation-channel monitors over an actor ledger."""

import argparse
import hashlib
import json
import math
import re
import sys
import zipfile
from pathlib import Path
from uuid import UUID

from shared.runs import IDENTITY_FIELDS
from shared.runs import config_id
from shared.runs import run_directory
from shared.runs import write_json
import signals.validation.controls.environment as environment
import signals.reasoning.monitor as monitor
from signals.reasoning.observations import CHANNELS
from signals.validation.controls.runner import ChatEndpoint
from signals.validation.controls.runner import SYSTEM
from signals.validation.controls.runner import digest
from signals.validation.controls.runner import read_checkpoint
from signals.validation.controls.runner import request_seed
from signals.validation.controls.runner import utcnow


ROOT = Path(__file__).resolve().parents[3]
CUTOFFS = ("retrospective", "prospective")
MAX_MODEL_CALLS = 256
MONITOR_SEED = 19492026
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_SOURCE_SUFFIXES = {".py", ".json", ".yaml", ".yml", ".toml", ".lock", ".sh", ".jinja"}


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key in {path.name}: {key}")
            result[key] = value
        return result

    def nonfinite(value):
        raise ValueError(f"Non-finite JSON value in {path.name}: {value}")

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            nonfinite(value)
        return parsed

    return json.loads(Path(path).read_text(), object_pairs_hook=unique, parse_constant=nonfinite,
                      parse_float=finite_float)


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def persist_once(path, value):
    if path.exists():
        if read_json(path) != value:
            raise ValueError(f"Frozen {path.name} differs; choose a new output directory")
    else:
        write_json(path, value)


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Unsupported run manifest")
    UUID(manifest["run_id"])
    if manifest.get("config_id") != config_id({key: value for key, value in manifest.items() if key not in IDENTITY_FIELDS}):
        raise ValueError("Run manifest configuration hash mismatch")
    if manifest.get("section") != "signals":
        raise ValueError("Expected a signals run")


def source_hashes():
    paths = [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    for folder in ("src", "experiments", "tests"):
        for path in (ROOT / folder).rglob("*"):
            relative = path.relative_to(ROOT)
            if (path.is_file() and path.suffix in _SOURCE_SUFFIXES
                    and not any(part.startswith(".") or part == "__pycache__" for part in relative.parts)):
                if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
                    raise ValueError("Source snapshots cannot include symlinks or external files")
                paths.append(path)
    return {str(path.relative_to(ROOT)): file_hash(path) for path in sorted(set(paths))}


def archive_sources(output, sources):
    path = output / "source.zip"
    if path.exists():
        with zipfile.ZipFile(path) as archive:
            if sorted(archive.namelist()) != sorted(sources):
                raise ValueError("Existing source archive file list differs")
            for name, expected in sources.items():
                if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                    raise ValueError("Existing source archive hash mismatch")
        return
    pending = output / "source.zip.tmp"
    with zipfile.ZipFile(pending, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, expected in sources.items():
            body = (ROOT / name).read_bytes()
            if hashlib.sha256(body).hexdigest() != expected:
                raise ValueError("Sources changed while creating their snapshot")
            archive.writestr(name, body)
    pending.replace(path)


def validate_confirmation_monitor(actor, model, model_revision, cutoffs, sources, version=monitor.MONITOR_VERSION_V1):
    splits = {row["split"] for row in actor["assignments"]}
    if "confirmation" not in splits:
        return
    freeze = actor["manifest"].get("freeze")
    if splits != {"confirmation"} or not isinstance(freeze, dict):
        raise ValueError("Confirmation monitors require a single split and pre-run actor freeze")
    if version != actor["manifest"].get("monitor_version", monitor.MONITOR_VERSION_V1):
        raise ValueError("Monitor version differs from the confirmation actor plan")
    if (model != freeze.get("monitor_model") or model_revision != freeze.get("monitor_model_revision")
            or cutoffs != freeze.get("monitor_cutoffs") or monitor.prompt_hash(version) != freeze.get("monitor_sha256")):
        raise ValueError("Monitor configuration differs from the confirmation pre-run freeze")
    if version == monitor.MONITOR_VERSION_V2 and freeze.get("monitor_identity") != monitor.monitor_identity(version):
        raise ValueError("Monitor response schema or endpoint differs from the confirmation pre-run freeze")
    relevant_sources = ("src/signals/reasoning/monitor.py", "src/signals/reasoning/observations.py")
    if version == monitor.MONITOR_VERSION_V2:
        relevant_sources += ("src/signals/validation/controls/environment.py", "src/signals/validation/controls/runner.py",
                             "src/signals/validation/controls/finite_execution.py", "src/signals/reasoning/run_monitor.py",
                             "src/signals/validation/controls/analyze.py", "src/signals/validation/controls/analysis.py",
                             "src/signals/validation/calibration.py")
    for name in relevant_sources:
        expected = freeze.get("source_sha256", {}).get(name)
        if version == monitor.MONITOR_VERSION_V2 and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise ValueError("Confirmation freeze lacks an exact monitor/endpoint source identity")
        if sources.get(name) != expected:
            raise ValueError("Monitor projection source differs from the confirmation pre-run freeze")


def load_actor_run(directory):
    directory = Path(directory).resolve()
    manifest = read_json(directory / "manifest.json")
    validate_manifest(manifest)
    assignments = read_json(directory / "assignments.json")
    cases = read_json(directory / "cases.json")
    if not isinstance(assignments, list) or not isinstance(cases, dict):
        raise ValueError("Invalid actor assignments or case table")
    by_case = {}
    for case in cases.values():
        if not isinstance(case, dict) or not isinstance(case.get("case_id"), str) or case["case_id"] in by_case:
            raise ValueError("Duplicate or invalid actor case identity")
        by_case[case["case_id"]] = case
        if environment.public_interface_version(case) != manifest.get("public_interface_version", environment.PUBLIC_INTERFACE_V1):
            raise ValueError("Actor case public interface differs from its manifest")
    seen, trajectories, hashes, episode_statuses, status_hashes = set(), {}, {}, {}, {}
    for row in assignments:
        if not isinstance(row, dict) or not isinstance(row.get("assignment_id"), str) or not _SAFE_ID.fullmatch(row["assignment_id"]):
            raise ValueError("Unsafe or invalid assignment identity")
        identifier = row["assignment_id"]
        if identifier in seen:
            raise ValueError("Duplicate actor assignment identity")
        seen.add(identifier)
        case = by_case.get(row.get("case_id"))
        if case is None or any(row.get(a) != case.get(b) for a, b in (("family", "family"), ("split", "split"), ("case_index", "index"))):
            raise ValueError("Actor assignment does not match its immutable case")
        path = directory / "episodes" / identifier / "trajectory.json"
        trajectory = read_json(path) if path.exists() else None
        interrupted_path = path.parent / "interrupted.json"
        progress_path = path.parent / "progress.json"
        status_path = interrupted_path if interrupted_path.exists() else progress_path if progress_path.exists() else None
        if trajectory is None and status_path is not None:
            status_record = read_json(status_path)
            if not isinstance(status_record, dict) or status_record.get("assignment") != row:
                raise ValueError("Incomplete episode status does not match its assignment")
            episode_statuses[identifier] = "interrupted" if status_path == interrupted_path else "unfinished"
        else:
            episode_statuses[identifier] = trajectory["status"] if trajectory is not None else "unstarted"
        status_hashes[identifier] = {
            "status": episode_statuses[identifier],
            "evidence_file": status_path.name if trajectory is None and status_path is not None else None,
            "evidence_sha256": file_hash(status_path) if trajectory is None and status_path is not None else None,
        }
        if trajectory is not None:
            if not isinstance(trajectory, dict) or trajectory.get("assignment") != row:
                raise ValueError("Trajectory assignment mismatch")
            if trajectory.get("case_sha256") != digest(case):
                raise ValueError("Trajectory protected-case hash mismatch")
            for field in ("model", "model_revision"):
                if trajectory.get(field) != manifest.get(field):
                    raise ValueError(f"Actor trajectory {field} mismatch")
            if environment.public_interface_version(case) == environment.PUBLIC_INTERFACE_V2:
                limits = {key: manifest[key] for key in ("max_calls", "per_call_tokens", "max_completion_tokens")}
                context = [{"role": "system", "content": SYSTEM},
                           {"role": "user", "content": environment.render_case(case, **limits)}]
                if (trajectory.get("limits") != limits or trajectory.get("initial_context") != context
                        or trajectory.get("tools") != environment.tool_definitions()
                        or trajectory.get("generation") != manifest.get("generation")):
                    raise ValueError("Amended actor context/tools/generation/budgets differ from the frozen case and manifest")
        trajectories[identifier] = trajectory
        hashes[identifier] = {"file_sha256": file_hash(path) if path.exists() else None,
                              "trajectory_sha256": digest(trajectory) if trajectory is not None else None}
    episodes = directory / "episodes"
    if episodes.exists():
        for filename in ("trajectory.json", "labels.json", "assignment.json"):
            for path in episodes.glob(f"*/{filename}"):
                if path.parent.name not in seen:
                    raise ValueError("Unassigned actor episode artifact found")
    snapshot = {"run_id": manifest["run_id"], "config_id": manifest["config_id"],
                "manifest_sha256": file_hash(directory / "manifest.json"),
                "assignments_sha256": file_hash(directory / "assignments.json"),
                "assignments_digest": digest(assignments), "cases_sha256": file_hash(directory / "cases.json"),
                "trajectories": hashes, "episode_statuses": status_hashes}
    return {"directory": directory, "manifest": manifest, "assignments": assignments,
            "cases": by_case, "trajectories": trajectories, "episode_statuses": episode_statuses, "snapshot": snapshot}


def prediction_identity(task, config):
    identity = {"monitor_version": config["monitor_version"], "prompt_sha256": config["prompt_sha256"],
            "assignment_id": task["assignment_id"], "trajectory_sha256": task["trajectory_sha256"],
            "model": config["model"], "model_revision": config["model_revision"],
            "channel": task["channel"], "cutoff_name": task["cutoff_name"],
            "request_sha256": task["request_sha256"]}
    if config["monitor_version"] == monitor.MONITOR_VERSION_V2:
        identity.update({key: config[key] for key in ("response_format_sha256", "response_schema_sha256", "endpoint_version")})
    return identity


def validate_monitor_identity(config):
    version = config.get("monitor_version")
    expected = monitor.monitor_identity(version)
    keys = expected if version == monitor.MONITOR_VERSION_V2 else ("monitor_version", "prompt_sha256")
    if any(config.get(key) != expected[key] for key in keys):
        raise ValueError("Monitor version/prompt/schema/endpoint identity mismatch")


def validate_prediction(record, task, config):
    if not isinstance(record, dict):
        raise ValueError("Prediction record must be an object")
    if any(key not in record or record[key] != value for key, value in prediction_identity(task, config).items()):
        raise ValueError("Existing prediction does not match the exact frozen assignment/request")
    if record.get("status") not in {"completed", "error", "unavailable", "interrupted"}:
        raise ValueError("Invalid saved prediction status")


def validate_saved_request(directory, task, status):
    path = directory / "request.json"
    if path.exists():
        if digest(read_json(path)) != task["request_sha256"]:
            raise ValueError("Saved monitor request hash mismatch")
    elif status in {"completed", "error"} and task["request_sha256"] is not None:
        raise ValueError("Saved monitor request is missing")


def unavailable_record(task, config, status=None):
    return {**prediction_identity(task, config), "status": status or task["availability"],
            "prediction": None, "reason": task["reason"], "ended_at": utcnow()}


def prospective_certificate(actor, assignment, trajectory, *, strict=False):
    if trajectory is None or trajectory.get("prospective_cutoff") is None:
        return False, "missing_cutoff", None
    path = actor["directory"] / "episodes" / assignment["assignment_id"] / "checkpoint.json"
    if not path.exists():
        return False, "missing_checkpoint", None
    # Reject corrupt identities rather than quietly treating them as missing data.
    if strict:
        record = read_json(path)
        if (not isinstance(record, dict) or set(record) != {"schema_version", "payload", "sha256"}
                or type(record["schema_version"]) is not int or record["schema_version"] != 1
                or not isinstance(record["payload"], dict) or digest(record["payload"]) != record["sha256"]):
            raise ValueError("Prospective checkpoint schema or content hash is invalid")
        payload = record["payload"]
    else:
        payload = read_checkpoint(path)
    cutoff = trajectory["prospective_cutoff"]
    case = actor["cases"][assignment["case_id"]]
    if (payload.get("assignment") != assignment or payload.get("case") != case
            or payload.get("messages") != trajectory["messages"][:cutoff]
            or payload.get("initial_context") != trajectory["initial_context"]
            or payload.get("tools") != trajectory["tools"]
            or payload.get("model") != trajectory["model"]
            or payload.get("model_revision") != trajectory["model_revision"]):
        raise ValueError("Prospective checkpoint identity or transcript mismatch")
    state = payload["state"]
    restored = environment.restore(case, environment.snapshot(case, state))
    events, messages = restored["audit_log"], payload["messages"]
    inspection_only = (
        cutoff == 2 and len(messages) == 2 and payload.get("completed_calls") == 1
        and restored["artifact"] is None and restored["tool_calls"] == 1
        and len(events) == 1 and events[0]["tool"] == "inspect_artifact"
        and events[0]["response"].get("ok") is True
        and messages[0].get("role") == "assistant" and messages[1].get("role") == "tool"
        and isinstance(messages[0].get("tool_calls"), list) and len(messages[0]["tool_calls"]) == 1
        and messages[0]["tool_calls"][0]["function"]["name"] == "inspect_artifact"
        and messages[1].get("tool_call_id") == messages[0]["tool_calls"][0]["id"]
    )
    if strict:
        replay = environment.initial_state(case)
        expected_result = environment.apply_tool(case, replay, "inspect_artifact", {})
        if (payload.get("limits") != trajectory.get("limits")
                or payload.get("generation") != trajectory.get("generation")
                or payload.get("seed_derivation_version") != trajectory.get("seed_derivation_version")
                or payload.get("next_generation_seed") != request_seed(assignment, 1)):
            raise ValueError("Prospective checkpoint limits/generation/seed mismatch")
        if inspection_only and (json.loads(messages[0]["tool_calls"][0]["function"]["arguments"]) != {}
                or json.loads(messages[1]["content"]) != expected_result or state != replay
                or payload.get("usage") != {key: trajectory["calls"][0]["usage"].get(key) for key in ("prompt_tokens", "completion_tokens")}):
            raise ValueError("Prospective checkpoint does not match the independently replayed inspection")
    return inspection_only, "verified_inspection_before_mutation" if inspection_only else "prior_mutation_not_excluded", file_hash(path)



def build_plan(actor, cutoffs, model, version=monitor.MONITOR_VERSION_V1):
    monitor.monitor_identity(version)
    tasks, previews = [], {}
    seeds = set()
    for assignment in actor["assignments"]:
        identifier = assignment["assignment_id"]
        trajectory = actor["trajectories"][identifier]
        execution_hash = None
        if version == monitor.MONITOR_VERSION_V2 and trajectory is not None:
            import signals.validation.controls.finite_execution as finite_execution
            for name in ("src/signals/validation/controls/environment.py", "src/signals/validation/controls/runner.py"):
                if file_hash(ROOT / name) != actor["manifest"]["source_sha256"].get(name):
                    raise ValueError("Use the actor's exact environment/runner implementation for v2 monitor planning")
            execution_hash = digest(finite_execution.certify(actor["cases"][assignment["case_id"]], trajectory,
                                                            actor["directory"] / "episodes" / identifier))
        certified, certificate_reason, checkpoint_hash = False, "not_requested", None
        if version == monitor.MONITOR_VERSION_V2 and "prospective" in cutoffs:
            certified, certificate_reason, checkpoint_hash = prospective_certificate(actor, assignment, trajectory, strict=True)
        for cutoff in cutoffs:
            for channel in CHANNELS:
                seed = int(digest({"base_seed": MONITOR_SEED, "actor_run_id": actor["manifest"]["run_id"],
                                   "assignment_id": identifier, "cutoff": cutoff, "channel": channel})[:16], 16) % (2**31 - 1)
                if seed in seeds:
                    raise ValueError("Deterministic monitor seed collision; revise the frozen seed scheme")
                seeds.add(seed)
                task = {"monitor_assignment_id": f"{identifier}--{cutoff}--{channel}",
                        "assignment_id": identifier, "assignment_sha256": digest(assignment),
                        "channel": channel, "cutoff_name": cutoff, "seed": seed,
                        "reasoning_status": "unavailable" if channel in {"cot", "combined"} else "not_observed",
                        "trajectory_sha256": digest(trajectory) if trajectory is not None else None,
                        "request_sha256": None, "availability": "unavailable", "reason": "missing_trajectory"}
                if version == monitor.MONITOR_VERSION_V2:
                    task["monitor_identity"] = monitor.monitor_identity(version)
                    task["execution_certificate_sha256"] = execution_hash
                    if cutoff == "prospective":
                        task["checkpoint_sha256"] = checkpoint_hash
                if trajectory is not None:
                    if trajectory.get(f"{cutoff}_cutoff") is None:
                        task["reason"] = "missing_cutoff"
                    elif version == monitor.MONITOR_VERSION_V2 and cutoff == "prospective" and not certified:
                        task["reason"] = certificate_reason
                    else:
                        try:
                            request, observation = monitor.monitor_request(trajectory, channel, cutoff, model, task["seed"], version=version)
                            if request["max_tokens"] != 1024:
                                raise ValueError("Monitor completion cap differs from the frozen 1024-token cap")
                            previews[task["monitor_assignment_id"]] = {"request": request, "observation": observation}
                            task.update(request_sha256=digest(request), availability="available", reason=None,
                                        reasoning_status=observation.get("reasoning_status", "not_observed"))
                        except (ValueError, TypeError, KeyError) as exc:
                            task.update(availability="error", reason=f"invalid_observation:{type(exc).__name__}")
                tasks.append(task)
    return tasks, previews


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:18081/v1")
    parser.add_argument("--model", default="Qwen/Qwen3.8-27B-FP8")
    parser.add_argument("--model-revision", default="017b9c7af6b5689d5dd426a76e0bc077eb5ca20a")
    parser.add_argument("--monitor-version", choices=monitor.MONITOR_VERSIONS, default=monitor.MONITOR_VERSION_V1)
    parser.add_argument("--cutoffs", nargs="+", choices=CUTOFFS, default=list(CUTOFFS))
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if Path(monitor.__file__).resolve().parents[3] != ROOT:
        parser.error("Use this worktree's Python environment")
    if len(set(args.cutoffs)) != len(args.cutoffs):
        parser.error("Cutoffs must be unique")
    try:
        actor = load_actor_run(args.input)
        if args.output.resolve() == actor["directory"] or args.output.resolve().is_relative_to(actor["directory"]):
            raise ValueError("Monitor outputs must be separate from the immutable actor run")
        endpoint = ChatEndpoint(args.base_url)
        tasks, previews = build_plan(actor, args.cutoffs, args.model, args.monitor_version)
        if len(previews) > MAX_MODEL_CALLS:
            raise ValueError(f"Plan requires {len(previews)} calls, exceeding the fixed {MAX_MODEL_CALLS}-call ceiling")
        sources = source_hashes()
        validate_confirmation_monitor(actor, args.model, args.model_revision, args.cutoffs, sources, args.monitor_version)
        config = {"section": "signals", "run_type": "monitor", "model": args.model,
                  "model_revision": args.model_revision, "base_url": args.base_url,
                  "actor_input": str(actor["directory"]), "actor_snapshot": actor["snapshot"],
                  "channels": list(CHANNELS), "cutoffs": args.cutoffs,
                  **monitor.monitor_identity(args.monitor_version),
                  "monitor_assignments_sha256": digest(tasks), "seed": MONITOR_SEED,
                  "seed_scheme": "sha256(actor_run_id,assignment_id,channel,cutoff,base_seed) first64 modulo 2^31-1; collision checked",
                  "max_model_calls": MAX_MODEL_CALLS, "max_completion_tokens_per_call": 1024,
                  "concurrency": 1, "implicit_retries": 0, "source_sha256": sources, "python": sys.version}
        with run_directory(args.output, config) as manifest:
            persist_once(args.output / "assignments.json", actor["assignments"])
            persist_once(args.output / "monitor_assignments.json", tasks)
            archive_sources(args.output, sources)
            preview_root = args.output / "preview_requests"
            preview_root.mkdir(exist_ok=True)
            for identifier, preview in previews.items():
                persist_once(preview_root / f"{identifier}.json", preview)
            predictions, attempts = args.output / "predictions", args.output / "attempts"
            predictions.mkdir(exist_ok=True)
            attempts.mkdir(exist_ok=True)
            index = []
            for task in tasks:
                identifier = task["monitor_assignment_id"]
                destination = predictions / identifier
                prediction_path = destination / "prediction.json"
                attempt_path = attempts / f"{identifier}.json"
                expected = prediction_identity(task, config)
                if prediction_path.exists():
                    record = read_json(prediction_path)
                    validate_prediction(record, task, config)
                    validate_saved_request(destination, task, record["status"])
                    if task["availability"] == "available":
                        attempt = read_json(attempt_path) if attempt_path.exists() else None
                        if not attempt or attempt.get("identity") != expected or attempt.get("monitor_run_id") != manifest["run_id"]:
                            raise ValueError("Saved prediction has no matching attempt/run identity")
                elif destination.exists() or attempt_path.exists():
                    attempt = read_json(attempt_path) if attempt_path.exists() else None
                    if not attempt or attempt.get("identity") != expected or attempt.get("monitor_run_id") != manifest["run_id"]:
                        raise ValueError("Unregistered or mismatched interrupted monitor directory")
                    destination.mkdir(exist_ok=True)
                    validate_saved_request(destination, task, "interrupted")
                    record = unavailable_record({**task, "reason": "interrupted_attempt_no_implicit_retry"}, config, "interrupted")
                    write_json(destination / "prediction.json", record)
                elif task["availability"] != "available":
                    destination.mkdir()
                    record = unavailable_record(task, config)
                    write_json(prediction_path, record)
                elif not args.execute:
                    record = {"status": "preview", "prediction": None}
                else:
                    write_json(attempt_path, {"identity": expected, "started_at": utcnow(), "monitor_run_id": manifest["run_id"]})
                    record = monitor.run_monitor(
                        trajectory=actor["trajectories"][task["assignment_id"]], channel=task["channel"],
                        cutoff_name=task["cutoff_name"], model=args.model, model_revision=args.model_revision,
                        seed=task["seed"], output=destination, call=endpoint, version=args.monitor_version,
                    )
                    validate_prediction(record, task, config)
                index.append({"monitor_assignment_id": identifier, "assignment_id": task["assignment_id"],
                              "channel": task["channel"], "cutoff_name": task["cutoff_name"], "status": record["status"]})
                write_json(args.output / "prediction_index.json", index)
            print(json.dumps({"run_id": manifest["run_id"], "execute": args.execute,
                              "n_actor_assignments": len(actor["assignments"]), "n_monitor_assignments": len(tasks),
                              "n_planned_calls": len(previews), "statuses": {status: sum(row["status"] == status for row in index) for status in sorted({row["status"] for row in index})},
                              "output": str(args.output.resolve())}), flush=True)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
