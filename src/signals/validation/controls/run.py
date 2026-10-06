import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from importlib.metadata import version
from pathlib import Path

from shared.runs import run_directory
from shared.runs import write_json
import signals.validation.controls.environment as environment
import signals.reasoning.monitor as monitor
from signals.validation.controls.runner import ChatEndpoint
from signals.validation.controls.runner import SEED_DERIVATION_VERSION
from signals.validation.controls.runner import assignment_ledger
from signals.validation.controls.runner import digest
from signals.validation.controls.runner import run_episode
from signals.validation.controls.runner import utcnow
from signals.validation.controls.runner import validate_request_seeds

if __package__:
    from signals.reasoning.run_monitor import read_json
    from signals.validation.controls.analyze import compatibility_identity
    from signals.validation.controls.analyze import load_operating_points
else:
    from signals.reasoning.run_monitor import read_json
    from signals.validation.controls.analyze import compatibility_identity, load_operating_points


ROOT = Path(__file__).resolve().parents[4]
FROZEN_ACTOR_FIELDS = ("model", "model_revision", "generation", "families", "split", "instances", "repeats", "seed",
                       "seed_derivation_version", "max_calls", "max_completion_tokens", "per_call_tokens", "environment_schema")


def source_files():
    files = [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    for directory in ("src", "data", "tests"):
        files.extend(path for path in (ROOT / directory).rglob("*") if path.is_file()
                     and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"})
    return sorted(files)


def persist_once(path, value):
    if path.exists():
        if json.loads(path.read_text()) != value:
            raise ValueError(f"Existing {path.name} differs from frozen configuration")
    else:
        write_json(path, value)


def validate_freeze(freeze, source, monitor_version=monitor.MONITOR_VERSION_V1):
    if not isinstance(freeze, dict) or freeze.get("source_sha256") != source:
        raise ValueError("Frozen source hashes are required and must match")
    for key in ("monitor_sha256", "protocol_sha256", "operating_points_file_sha256"):
        value = freeze.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError(f"Frozen {key} must be an exact lowercase SHA256")
    if freeze["monitor_sha256"] != monitor.prompt_hash(monitor_version):
        raise ValueError("Frozen monitor prompt hash does not match")
    if monitor_version == monitor.MONITOR_VERSION_V2 and freeze.get("monitor_identity") != monitor.monitor_identity(monitor_version):
        raise ValueError("Frozen monitor schema or endpoint identity does not match")


def confirmation_preflight(freeze, config, operating_path, protocol_path):
    selected_monitor = config.get("monitor_version", monitor.MONITOR_VERSION_V1)
    validate_freeze(freeze, config["source_sha256"], selected_monitor)
    if freeze.get("actor_settings") != {key: config[key] for key in FROZEN_ACTOR_FIELDS}:
        raise ValueError("Confirmation actor settings differ from the pre-run freeze")
    if config.get("public_interface_version", environment.PUBLIC_INTERFACE_V1) != freeze.get("public_interface_version", environment.PUBLIC_INTERFACE_V1):
        raise ValueError("Confirmation public interface differs from the pre-run freeze")
    if operating_path is None or protocol_path is None:
        raise ValueError("Confirmation requires the actual operating-point and protocol files")
    protocol_bytes = Path(protocol_path).read_bytes()
    if hashlib.sha256(protocol_bytes).hexdigest() != freeze["protocol_sha256"]:
        raise ValueError("Confirmation protocol file differs from the pre-run freeze")
    cutoffs = freeze.get("monitor_cutoffs")
    if (not isinstance(cutoffs, list) or not cutoffs or len(set(cutoffs)) != len(cutoffs)
            or any(value not in {"retrospective", "prospective"} for value in cutoffs)):
        raise ValueError("Frozen monitor cutoffs must be explicit and unique")
    actor = {"manifest": config, "assignments": [{"family": family} for family in config["families"]]}
    monitors = {"cutoffs": cutoffs, "manifest": {
        "model": freeze.get("monitor_model"), "model_revision": freeze.get("monitor_model_revision"),
        **monitor.monitor_identity(selected_monitor),
        "source_sha256": config["source_sha256"],
    }}
    _, operating_bytes = load_operating_points(operating_path, freeze["operating_points_file_sha256"],
                                             compatibility_identity(actor, monitors))
    return {"operating_points.json": operating_bytes, "protocol.txt": protocol_bytes}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="Qwen/Qwen3.8-27B-FP8")
    parser.add_argument("--model-revision", default="017b9c7af6b5689d5dd426a76e0bc077eb5ca20a")
    parser.add_argument("--base-url", default="http://127.0.0.1:18081/v1")
    parser.add_argument("--families", nargs="+", choices=environment.FAMILIES, default=list(environment.FAMILIES))
    parser.add_argument("--split", choices=environment.SPLITS, default="development")
    parser.add_argument("--run-type", choices=("smoke", "pilot", "confirmation"), default="pilot")
    parser.add_argument("--instances", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--seed", type=int, default=72026)
    parser.add_argument("--max-calls", type=int, default=6)
    parser.add_argument("--max-completion-tokens", type=int, default=16000)
    parser.add_argument("--per-call-tokens", type=int, default=3072)
    parser.add_argument("--public-interface-version", choices=environment.PUBLIC_INTERFACE_VERSIONS, default=environment.PUBLIC_INTERFACE_V1)
    parser.add_argument("--monitor-version", choices=monitor.MONITOR_VERSIONS, default=monitor.MONITOR_VERSION_V1)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--freeze-manifest", type=Path)
    parser.add_argument("--operating-points", type=Path)
    parser.add_argument("--protocol-file", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if Path(environment.__file__).resolve().parents[4] != ROOT:
        parser.error("Use this worktree's Python environment")
    if min(args.instances, args.repeats, args.max_calls, args.max_completion_tokens, args.per_call_tokens) < 1:
        parser.error("All counts and budgets must be positive")
    if args.run_type == "confirmation" and (args.split != "confirmation" or not args.freeze_manifest):
        parser.error("Confirmation requires the confirmation split and a frozen manifest")
    if args.split == "confirmation" and args.run_type != "confirmation":
        parser.error("The confirmation split is reserved for frozen confirmation runs")
    if args.freeze_manifest and not args.freeze_manifest.is_file():
        parser.error("Missing frozen manifest")
    source = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files()}
    generation = {
        "temperature": args.temperature, "top_p": 0.95, "top_k": 20,
        "chat_template_kwargs": {"enable_thinking": True},
    }
    freeze = read_json(args.freeze_manifest) if args.freeze_manifest else None
    if args.freeze_manifest is not None:
        try:
            validate_freeze(freeze, source, args.monitor_version)
        except ValueError as exc:
            parser.error(str(exc))
    config = {
        "section": "signals", "run_type": args.run_type,
        "environment_schema": environment.SCHEMA_VERSION,
        "public_interface_version": args.public_interface_version,
        "monitor_version": args.monitor_version,
        "model": args.model, "model_revision": args.model_revision,
        "base_url": args.base_url, "generation": generation,
        "families": args.families, "split": args.split,
        "instances": args.instances, "repeats": args.repeats, "seed": args.seed,
        "seed_derivation_version": SEED_DERIVATION_VERSION,
        "max_calls": args.max_calls, "max_completion_tokens": args.max_completion_tokens,
        "per_call_tokens": args.per_call_tokens,
        "source_sha256": source, "python": sys.version, "inspect_ai_version": version("inspect-ai"),
        "freeze": freeze,
    }
    frozen_files = {}
    if args.run_type == "confirmation":
        try:
            frozen_files = confirmation_preflight(freeze, config, args.operating_points, args.protocol_file)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            parser.error(str(exc))
    elif args.operating_points is not None or args.protocol_file is not None:
        parser.error("Operating-point/protocol input files are only used by confirmation")
    assignments = assignment_ledger(args.families, args.split, args.instances, args.repeats, args.seed)
    validate_request_seeds(assignments, args.max_calls)
    cases = {
        f"{family}-{index}": environment.make_case(family, args.split, index, public_interface_version=args.public_interface_version)
        for family in args.families for index in range(args.instances)
    }
    for row in assignments:
        row["case_id"] = cases[f"{row['family']}-{row['case_index']}"]["case_id"]
    public_fingerprints = [digest({key: value for key, value in case["public"].items() if key != "case_id"}) for case in cases.values()]
    if len(set(public_fingerprints)) != len(public_fingerprints):
        parser.error("Duplicate public cases in the assignment ledger")
    endpoint = ChatEndpoint(args.base_url, os.environ.get("QWEN_API_KEY", "unused"))
    with run_directory(args.output, config) as manifest:
        for name, body in frozen_files.items():
            path = args.output / f"frozen_{name}"
            if path.exists() and path.read_bytes() != body:
                raise ValueError("Existing frozen confirmation file differs")
            if not path.exists():
                path.write_bytes(body)
        persist_once(args.output / "assignments.json", assignments)
        persist_once(args.output / "cases.json", cases)
        if args.public_interface_version == environment.PUBLIC_INTERFACE_V2:
            from signals.validation.controls.runner import SYSTEM
            from signals.validation.controls.runner import actor_request
            from signals.validation.controls.runner import request_seed
            for row in assignments:
                case = cases[f"{row['family']}-{row['case_index']}"]
                context = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": environment.render_case(
                    case, max_calls=args.max_calls, per_call_tokens=args.per_call_tokens,
                    max_completion_tokens=args.max_completion_tokens,
                )}]
                request = actor_request(model=args.model, generation=generation, initial_context=context, messages=[],
                                        tools=environment.tool_definitions(), max_tokens=min(args.per_call_tokens, args.max_completion_tokens),
                                        seed=request_seed(row, 0), inspection_tool="inspect_artifact")
                preview = args.output / "preview_requests"
                preview.mkdir(exist_ok=True)
                persist_once(preview / f"{row['assignment_id']}.json", {
                    "request": request, "initial_context": context, "tools": environment.tool_definitions(),
                    "request_sha256": digest(request), "case_sha256": digest(case),
                    "artifact_schema_sha256": digest(case["public"]["artifact_schema"]),
                    "public_interface_version": args.public_interface_version,
                })
        archive_path = args.output / "source.zip"
        if archive_path.exists():
            with zipfile.ZipFile(archive_path) as archive:
                if sorted(archive.namelist()) != sorted(source):
                    raise ValueError("Existing source archive file list differs")
                for relative, expected_hash in source.items():
                    if hashlib.sha256(archive.read(relative)).hexdigest() != expected_hash:
                        raise ValueError("Existing source archive hash mismatch")
        else:
            with zipfile.ZipFile(args.output / "source.zip.tmp", "w", zipfile.ZIP_DEFLATED) as archive:
                for relative, expected_hash in source.items():
                    body = (ROOT / relative).read_bytes()
                    if hashlib.sha256(body).hexdigest() != expected_hash:
                        raise ValueError("Sources changed while creating the snapshot")
                    archive.writestr(relative, body)
            (args.output / "source.zip.tmp").replace(archive_path)
        if not (args.output / "revision.json").exists():
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
            status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
            write_json(args.output / "revision.json", {"git_head": head.stdout.strip(), "working_tree_status": status.stdout})
        print(json.dumps({"run_id": manifest["run_id"], "assignments": len(assignments), "execute": args.execute, "output": str(args.output.resolve())}), flush=True)
        if not args.execute:
            return
        episodes = args.output / "episodes"
        episodes.mkdir(exist_ok=True)
        index = []
        for row in assignments:
            destination = episodes / row["assignment_id"]
            if (destination / "trajectory.json").exists():
                record = json.loads((destination / "trajectory.json").read_text())
            elif destination.exists():
                # Interrupted attempts are retained; do not silently resample them on resume.
                record = {"assignment": row, "status": "interrupted", "ended_at": utcnow()}
                write_json(destination / "interrupted.json", record)
            else:
                record = run_episode(
                    case=cases[f"{row['family']}-{row['case_index']}"], assignment=row,
                    output=destination, model=args.model, model_revision=args.model_revision,
                    generation=generation, call=endpoint, environment=environment,
                    max_calls=args.max_calls, max_completion_tokens=args.max_completion_tokens,
                    per_call_tokens=args.per_call_tokens,
                )
            index.append({"assignment_id": row["assignment_id"], "status": record["status"]})
            write_json(args.output / "episode_index.json", index)
            print(json.dumps(index[-1]), flush=True)
        print(json.dumps({"finished_assignments": len(index), "completed": sum(row["status"] == "completed" for row in index)}), flush=True)


if __name__ == "__main__":
    main()
