import argparse
import hashlib
import json
import math
import os
import random
import socket
import subprocess
import zipfile
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from inspect_ai import eval_set

from causes.recipe_controls.results import reconcile
from shared.results import summarize
from shared.runs import run_directory
from shared.runs import run_metadata
from shared.runs import write_json
from causes.recipe_controls.tasks.task import ROOT
from causes.recipe_controls.tasks.task import SYSTEM
from causes.recipe_controls.tasks.task import effects_task
from causes.recipe_controls.tasks.task import render_request


SCHEDULE_VERSION = "effects-factorial-v1"
CELLS = (
    ("ordinary", "limited"),
    ("ordinary", "broad"),
    ("evaluation", "limited"),
    ("evaluation", "broad"),
)


def digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def assignment_schedule(instances: list[str], replicas: int, seed: int, prompt_variant: str) -> list[dict]:
    if not instances or len(set(instances)) != len(instances):
        raise ValueError("instances must be nonempty and unique")
    if replicas < 1:
        raise ValueError("replicas must be positive")
    rng = random.Random(seed)
    blocks = [(instance, rollout) for instance in instances for rollout in range(replicas)]
    rng.shuffle(blocks)
    assignments = []
    for instance, rollout in blocks:
        cells = list(CELLS)
        rng.shuffle(cells)
        for framing, grading in cells:
            assignment = {
                "instance_id": instance, "framing": framing, "grading": grading,
                "rollout": rollout, "prompt_variant": prompt_variant,
            }
            identity = digest({"schedule_version": SCHEDULE_VERSION, "seed": seed, **assignment})
            assignments.append({"assignment_id": f"effe-{identity}", **assignment})
    return assignments


def source_files() -> list[Path]:
    files = [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    for directory in ("src", "data", "tests"):
        for path in (ROOT / directory).rglob("*"):
            relative = path.relative_to(ROOT)
            if "__pycache__" in relative.parts or any(part.startswith(".") for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError(f"Source snapshots cannot include symlinks: {relative}")
            if path.is_file():
                files.append(path)
    return sorted(path for path in files if path.exists())


def guarded_json(path: Path, value) -> None:
    if path.exists():
        if json.loads(path.read_text()) != value:
            raise ValueError(f"Saved {path.name} differs; choose a new output directory.")
    else:
        write_json(path, value)


def snapshot_sources(output: Path, files: list[Path], hashes: dict[str, str]) -> None:
    destination = output / "source.zip"
    if not destination.exists():
        pending = output / "source.zip.tmp"
        with zipfile.ZipFile(pending, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                relative = str(path.relative_to(ROOT))
                contents = path.read_bytes()
                if hashlib.sha256(contents).hexdigest() != hashes[relative]:
                    raise ValueError("Sources changed during setup; choose a new output directory.")
                archive.writestr(relative, contents)
        pending.replace(destination)
    with zipfile.ZipFile(destination) as archive:
        if sorted(archive.namelist()) != sorted(hashes):
            raise ValueError("Saved source archive has unexpected members.")
        if any(hashlib.sha256(archive.read(name)).hexdigest() != expected for name, expected in hashes.items()):
            raise ValueError("Saved source archive differs from the manifest.")


def on_control_host() -> bool:
    # Deployment policy is explicit; the scientific runner has no host registry.
    return os.environ.get("UNDERTEST_CONTROL_HOST", "1").lower() in {"1", "true", "yes"}


def export_results(output: Path, assignments: list[dict], success: bool, failure: BaseException | None) -> None:
    status = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "eval_set_success": success,
        "runner_error": {"type": type(failure).__name__, "message": str(failure)} if failure else None,
        "assigned_count": len(assignments),
    }
    export_failure = None
    try:
        rows = summarize(output / "logs", output)
        observed = {}
        for row in rows:
            metadata = json.loads(row.get("sample_metadata") or "{}")
            assignment_id = metadata.get("assignment_id", row.get("sample_id"))
            if assignment_id is not None:
                observed.setdefault(str(assignment_id), []).append({
                    key: row.get(key) for key in ("sample_status", "error", "log", "sample_key", "latest_attempt")
                })
        write_json(output / "assignment_outcomes.json", [
            {**assignment, "observed": assignment["assignment_id"] in observed,
             "records": observed.get(assignment["assignment_id"], [])}
            for assignment in assignments
        ])
        status["exported_rows"] = len(rows)
        status["unobserved_assignment_ids"] = [
            assignment["assignment_id"] for assignment in assignments
            if assignment["assignment_id"] not in observed
        ]
        status["export_error"] = None
    except Exception as exc:
        export_failure = exc
        status["export_error"] = {"type": type(exc).__name__, "message": str(exc)}
    try:
        reconciled = reconcile(output)
        status["reconciled_assignments"] = len(reconciled["assignments"])
        status["reconciliation_warnings"] = reconciled["warnings"]
    except Exception as exc:
        status["reconciliation_error"] = {"type": type(exc).__name__, "message": str(exc)}
        if export_failure is None:
            export_failure = exc
    write_json(output / "execution_summary.json", status)
    if failure is None and export_failure is not None:
        raise export_failure


def main() -> None:
    if ROOT.resolve() != Path(__file__).resolve().parents[3]:
        raise SystemExit("Use this checkout's Python environment; the imported package belongs to another checkout.")
    parser = argparse.ArgumentParser()
    parser.add_argument("--instances", nargs="+", default=["ledger-dev-0"])
    parser.add_argument("--replicas", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20260930)
    parser.add_argument("--prompt-variant", choices=("base", "paraphrase"), default="base")
    parser.add_argument("--run-type", choices=("smoke", "pilot", "confirmation", "diagnostic"), default="pilot")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--model-revision")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--max-connections", type=int, choices=(1,), default=1)
    parser.add_argument("--actual-token-budget", type=int, default=512000)
    parser.add_argument("--server-context-limit", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-local-smoke", action="store_true")
    args = parser.parse_args()
    if args.replicas < 1 or args.max_tokens < 1:
        parser.error("replicas and max-tokens must be positive")
    if args.actual_token_budget < 1 or (args.server_context_limit is not None and args.server_context_limit < args.max_tokens):
        parser.error("actual-token-budget must be positive and server-context-limit must cover max-tokens")
    if not math.isfinite(args.temperature) or args.temperature < 0:
        parser.error("temperature must be finite and nonnegative")
    if not args.dry_run and not all((args.model, args.base_url, args.model_revision)):
        parser.error("Real runs require --model, --base-url, and --model-revision.")
    if not args.dry_run and args.server_context_limit is None:
        parser.error("Real runs require the accepted endpoint's --server-context-limit for conservative token admission.")
    if not args.dry_run and args.model_revision.lower() in {"unrecorded", "unknown", "latest", "main", "not-executed"}:
        parser.error("Real runs require an immutable recorded model revision.")
    if args.base_url:
        endpoint = urlsplit(args.base_url)
        if endpoint.scheme not in {"http", "https"} or not endpoint.hostname:
            parser.error("base-url must be an HTTP(S) endpoint")
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            parser.error("base-url must not contain credentials, query parameters, or fragments")
    try:
        assignments = assignment_schedule(args.instances, args.replicas, args.seed, args.prompt_variant)
        if not args.dry_run and on_control_host() and not (
            args.allow_local_smoke and args.run_type == "smoke" and len(assignments) <= 4
        ):
            parser.error("Real runs on ido require --allow-local-smoke, --run-type smoke, and at most four assignments; run substantive work remotely.")
        inputs = {
            assignment["assignment_id"]: {
                "system": SYSTEM,
                "user": render_request(
                    assignment["instance_id"], assignment["framing"], assignment["grading"],
                    prompt_variant=assignment["prompt_variant"],
                ),
            }
            for assignment in assignments
        }
        output = (args.output or ROOT / "outputs" / ("effe-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))).resolve()
        configured_task = effects_task(assignments, ledger_dir=str(output / "ledgers"))
        task_limits = {
            "token_limit": configured_task.token_limit, "message_limit": configured_task.message_limit,
            "time_limit": configured_task.time_limit,
        }
        conservative_bound = (
            len(assignments) * (configured_task.token_limit + args.server_context_limit)
            if args.server_context_limit is not None else None
        )
        if conservative_bound is not None and conservative_bound > args.actual_token_budget:
            parser.error("Conservative block usage exceeds actual-token-budget; reduce assigned work or obtain an increased allocation.")
        generation = {
            "max_tokens": args.max_tokens, "temperature": args.temperature,
            "cache": False, "max_retries": 0, "max_connections": args.max_connections,
            "adaptive_connections": False, "parallel_tool_calls": False,
        }
        if (args.model or "").startswith("openai-api/qwen/"):
            generation.update(
                top_p=0.95,
                extra_body={"top_k": 20, "chat_template_kwargs": {"enable_thinking": True}},
            )
        files = source_files()
        hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        model_args = {"max_retries": 0}
        execution = {
            "epochs": 1, "max_tasks": 1, "max_samples": 1, "sample_shuffle": False,
            "retry_attempts": 0, "retry_immediate": False, "retry_on_error": 0, "retry_cleanup": False,
            "incomplete_action": "error", "fail_on_error": 2.0,
            "log_samples": True, "log_model_api": True,
        }
        config = {
            "section": "effe", "run_type": args.run_type,
            "model": args.model or "dry-run/unconfigured", "base_url": args.base_url,
            "model_revision": args.model_revision or "not-executed",
            "instances": args.instances, "replicas": args.replicas,
            "randomization_seed": args.seed, "prompt_variant": args.prompt_variant,
            "schedule_version": SCHEDULE_VERSION, "assignment_count": len(assignments),
            "schedule_sha256": digest(assignments),
            "input_sha256": {key: digest(value) for key, value in inputs.items()},
            "generation": generation, "model_args": model_args, "execution": execution,
            "task_limits": task_limits,
            "resource_allocation": {
                "actual_input_output_token_budget": args.actual_token_budget,
                "accepted_server_context_limit": args.server_context_limit,
                "conservative_block_token_bound": conservative_bound,
                "admission_rule": "each episode stops at its usage threshold plus at most one accepted-context-sized generation",
            },
            "inspect_ai_version": version("inspect-ai"), "source_sha256": hashes,
        }
        with run_directory(output, config) as manifest:
            snapshot_sources(output, files, hashes)
            guarded_json(output / "assignments.json", assignments)
            guarded_json(output / "inputs.json", inputs)
            if not (output / "revision.json").exists():
                revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
                write_json(output / "revision.json", {
                    "git_head": revision.stdout.strip() if revision.returncode == 0 else None,
                    "source_sha256": hashes,
                })
            print(f"Output: {output}")
            print(f"Assignments: {len(assignments)}; schedule SHA-256: {config['schedule_sha256']}")
            if args.dry_run:
                return
            if (output / "execution_started.json").exists():
                raise ValueError("This output already started execution; automatic retries are disabled. Preserve it and choose a new output directory.")
            load_dotenv(ROOT / ".env")
            (output / "logs").mkdir(exist_ok=True)
            write_json(output / "execution_started.json", {
                "started_at": datetime.now(timezone.utc).isoformat(),
                "hostname": socket.gethostname(),
                "schedule_sha256": config["schedule_sha256"],
            })
            success = False
            failure = None
            try:
                success, _ = eval_set(
                    configured_task,
                    model=args.model, model_base_url=args.base_url, model_args=model_args,
                    log_dir=str(output / "logs"), metadata=run_metadata(manifest),
                    **execution, **generation,
                )
            except BaseException as exc:
                failure = exc
                raise
            finally:
                export_results(output, assignments, success, failure)
    except (ValueError, KeyError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    if not success:
        raise SystemExit("Some evaluations failed; see the raw logs, assignment_outcomes.json, and execution_summary.json.")


if __name__ == "__main__":
    main()
