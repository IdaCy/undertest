import argparse
import hashlib
import json
import math
import socket
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from inspect_ai import eval_set

from causes.recipe_controls.effects_factorial import ROOT, digest, guarded_json, on_control_host, snapshot_sources, source_files
from causes.recipe_controls.controls.analysis import summarize_control_design
from causes.recipe_controls.controls.design import verify_control_design
from causes.recipe_controls.controls.reconcile import reconcile_control_output
from characteristics.downstream_effects.recipe_controls_secondary import summarize_control_secondary
from shared.results import summarize
from shared.runs import run_directory
from shared.runs import run_metadata
from shared.runs import write_json
from causes.recipe_controls.tasks.control_task import effects_control_task
from causes.recipe_controls.tasks.task import SYSTEM


RUNNER_VERSION = "effects-control-runner-v2"


def execution_config(model, max_tokens, temperature):
    if type(max_tokens) is not int or max_tokens < 1 or not math.isfinite(temperature) or temperature < 0:
        raise ValueError("Generation limits must be positive/finite")
    generation = {"max_tokens": max_tokens, "temperature": temperature, "cache": False,
                  "max_retries": 0, "max_connections": 1, "adaptive_connections": False, "parallel_tool_calls": False}
    if model.startswith("openai-api/qwen/"):
        generation.update(top_p=0.95, extra_body={"top_k": 20, "chat_template_kwargs": {"enable_thinking": True}})
    execution = {"epochs": 1, "max_tasks": 1, "max_samples": 1, "sample_shuffle": False,
                 "retry_attempts": 0, "retry_immediate": False, "retry_on_error": 0, "retry_cleanup": False,
                 "incomplete_action": "error", "fail_on_error": 2.0, "log_samples": True, "log_model_api": True}
    return generation, {"max_retries": 0}, execution


def conservative_bound(assignment_count, episode_token_limit, server_context_limit, allocation):
    if any(type(value) is not int or value < 1 for value in (assignment_count, episode_token_limit, server_context_limit, allocation)):
        raise ValueError("Counts, token limits and allocation must be positive exact integers")
    bound = assignment_count * (episode_token_limit + server_context_limit)
    if bound > allocation:
        raise ValueError("Conservative full-assigned token bound exceeds allocation")
    return bound


def export_control_results(output, design, success, failure):
    status = {"completed_at": datetime.now(timezone.utc).isoformat(), "eval_set_success": success,
              "runner_error": {"type": type(failure).__name__, "message": str(failure)} if failure else None,
              "assigned_count": len(design["assignments"])}
    export_failure = None
    try:
        exported = summarize(output / "logs", output)
        status["exported_rows"] = len(exported)
    except Exception as exc:
        status["export_error"] = {"type": type(exc).__name__, "message": str(exc)}
        export_failure = exc
    try:
        reconciled = reconcile_control_output(output, design)
        write_json(output / "control_reconciliation.json", reconciled)
        references = [{"path": reference["path"], "sha256": reference["sha256"]}
                      for reference in reconciled["source_references"] if reference.get("sha256")]
        analysis = summarize_control_design(design, reconciled["rows"], source_references=references)
        write_json(output / "control_analysis.json", analysis)
        status["reconciled_assignments"] = len(reconciled["rows"])
        secondary = summarize_control_secondary(design, reconciled)
        if secondary["input_references"]["primary_summary_sha256"] != analysis["summary_sha256"]:
            raise ValueError("Secondary analysis is not bound to the exported primary summary")
        write_json(output / "control_secondary.json", secondary)
        status["secondary_assignments"] = len(secondary["assignment_audit"])
        status["secondary_endpoints"] = list(secondary["endpoints"])
    except Exception as exc:
        status["reconciliation_error"] = {"type": type(exc).__name__, "message": str(exc)}
        if export_failure is None:
            export_failure = exc
    write_json(output / "execution_summary.json", status)
    if failure is None and export_failure is not None:
        raise export_failure


def main():
    if ROOT.resolve() != Path(__file__).resolve().parents[3]:
        raise SystemExit("Use this checkout's Python environment")
    parser = argparse.ArgumentParser()
    parser.add_argument("--design", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--server-context-limit", type=int, required=True)
    parser.add_argument("--actual-token-budget", type=int, required=True)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.dry_run and on_control_host():
        parser.error("Control-study inference runs require the allocated remote host")
    endpoint = urlsplit(args.base_url)
    if endpoint.scheme not in ("http", "https") or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        parser.error("Use an HTTP(S) endpoint without credentials, query or fragment")
    if args.model_revision.lower() in {"unrecorded", "unknown", "latest", "main", "not-executed", ""}:
        parser.error("An immutable model revision is required")
    if args.server_context_limit < args.max_tokens:
        parser.error("Accepted context limit must cover maximum generated tokens")
    try:
        design = json.loads(args.design.read_text())
        verify_control_design(design)
        output = args.output.resolve()
        configured_task = effects_control_task(design, ledger_dir=str(output / "ledgers"))
        bound = conservative_bound(len(design["assignments"]), configured_task.token_limit, args.server_context_limit, args.actual_token_budget)
        generation, model_args, execution = execution_config(args.model, args.max_tokens, args.temperature)
        files = source_files()
        hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        if any(hashes.get(path) != sha for path, sha in design["manifest"]["source_sha256"].items()):
            raise ValueError("Execution source differs from the design's declared source binding")
        inputs = {identity: {"system": SYSTEM, "user": prompt} for identity, prompt in design["inputs"].items()}
        config = {"section": "effe", "run_type": design["manifest"]["stage"],
                  "runner_version": RUNNER_VERSION, "study_id": design["manifest"]["study_id"],
                  "stage": design["manifest"]["stage"], "schedule_id": design["manifest"]["schedule_id"],
                  "design_sha256": digest(design), "assignment_count": len(design["assignments"]),
                  "schedule_sha256": digest(design["assignments"]), "input_sha256": {key: digest(value) for key, value in inputs.items()},
                  "model": args.model, "model_revision": args.model_revision, "base_url": args.base_url,
                  "generation": generation, "model_args": model_args, "execution": execution,
                  "task_limits": {"token_limit": configured_task.token_limit, "message_limit": configured_task.message_limit, "time_limit": configured_task.time_limit},
                  "resource_allocation": {"actual_input_output_token_budget": args.actual_token_budget,
                                          "accepted_server_context_limit": args.server_context_limit,
                                          "conservative_block_token_bound": bound,
                                          "external_deadline_and_cpu_memory_guard_required": True},
                  "inspect_ai_version": version("inspect-ai"), "source_sha256": hashes}
        with run_directory(output, config) as manifest:
            snapshot_sources(output, files, hashes)
            guarded_json(output / "control_design.json", design)
            guarded_json(output / "assignments.json", design["assignments"])
            guarded_json(output / "inputs.json", inputs)
            print(f"Output: {output}; assigned: {len(design['assignments'])}; conservative token bound: {bound}")
            if args.dry_run:
                return
            started = output / "execution_started.json"
            if started.exists():
                raise ValueError("This fixed design output already started; automatic retries/resumes are disabled")
            load_dotenv(ROOT / ".env")
            (output / "logs").mkdir(exist_ok=True)
            with started.open("x", encoding="utf-8") as handle:
                json.dump({"started_at": datetime.now(timezone.utc).isoformat(), "hostname": socket.gethostname(),
                           "schedule_id": design["manifest"]["schedule_id"]}, handle)
                handle.write("\n")
            success = False
            failure = None
            try:
                success, _ = eval_set(configured_task, model=args.model, model_base_url=args.base_url, model_args=model_args,
                                      log_dir=str(output / "logs"), metadata=run_metadata(manifest), **execution, **generation)
            except BaseException as exc:
                failure = exc
                raise
            finally:
                export_control_results(output, design, success, failure)
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))
    if not success:
        raise SystemExit("Control study failed; preserve raw evidence and execution_summary.json")


if __name__ == "__main__":
    main()
