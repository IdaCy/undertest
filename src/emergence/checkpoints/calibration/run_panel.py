import argparse
import json
import os
import socket
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from inspect_ai import Task, eval_set, task
from inspect_ai.dataset import Sample
from inspect_ai.model import ChatMessageSystem, ChatMessageUser
from inspect_ai.scorer import Score, Target, scorer
from inspect_ai.solver import TaskState, solver
from inspect_ai.util import store

from emergence.checkpoints.evidence import extract_evidence
from emergence.checkpoints.response import score_response
from emergence.checkpoints.completeness import audit_samples
from emergence.checkpoints.provenance import ROOT
from emergence.checkpoints.provenance import git_state
from emergence.checkpoints.provenance import load_checkpoint
from emergence.checkpoints.provenance import load_panel
from emergence.checkpoints.provenance import save_json_once
from emergence.checkpoints.provenance import save_snapshot
from emergence.checkpoints.provenance import source_files
from emergence.checkpoints.provenance import source_hashes
from emergence.checkpoints.provenance import verify_server_receipt
from shared.results import summarize
from shared.runs import run_directory
from shared.runs import run_metadata
from shared.runs import write_json


SYSTEM = "You are a helpful assistant."


def check_live_server(base_url: str, served_model: str) -> dict:
    with urlopen(base_url.rstrip("/") + "/models", timeout=15) as response:
        models = json.load(response)
    ids = [model["id"] for model in models.get("data", [])]
    if served_model not in ids:
        raise ValueError(f"Server does not expose the checkpoint-specific alias {served_model}")
    return models


def tokenize_inputs(base_url: str, served_model: str, samples: list[dict], mode: str) -> dict:
    endpoint = base_url.removesuffix("/").removesuffix("/v1") + "/tokenize"
    tokenized = {}
    for sample in samples:
        payload = {"model": served_model, "add_special_tokens": False}
        if mode == "raw":
            payload["prompt"] = sample["input"]
        else:
            payload["messages"] = [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": sample["input"]},
            ]
            payload["add_generation_prompt"] = True
        request = Request(endpoint, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        ids = result.get("tokens")
        if not isinstance(ids, list) or not ids or any(type(token) is not int for token in ids):
            raise ValueError("Tokenize endpoint did not return valid prompt token IDs")
        tokenized[sample["id"]] = {"request": payload, "response": result}
    return tokenized


@solver
def seeded_completion():
    async def solve(state, generate):
        seed = state.metadata["seed"]
        state.metadata["generation_seed"] = seed
        return await generate(state, tool_calls="none", cache=False, seed=seed)
    return solve


async def retain_evidence(state: TaskState) -> None:
    output = state.output.model_dump(mode="json")
    evidence = extract_evidence(output, reasoning_prefix_in_prompt=state.metadata.get("reasoning_prefix_in_prompt", False))
    outcomes, annotation = score_response(state.metadata, evidence)
    evidence.update(annotation)
    artifacts = dict(store().get("artifacts", {}))
    artifacts.update({
        "development/model_output.json": json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        "development/evidence.json": json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        "development/outcomes.json": json.dumps(outcomes, ensure_ascii=False, indent=2) + "\n",
    })
    store().set("artifacts", artifacts)
    store().set("development_evidence", evidence)
    store().set("development_outcomes", outcomes)


@scorer(metrics=[])
def development_outcomes():
    async def score(state: TaskState, target: Target) -> Score:
        await retain_evidence(state)
        outcomes = store().get("development_outcomes")
        values = {
            key: value for key, value in outcomes.items()
            if value is None or type(value) in (str, int, float, bool)
        }
        return Score(value=values, metadata={
            "outcomes": outcomes,
            "evidence": store().get("development_evidence"),
        })
    return score


@task
def development_panel(samples: list[dict], mode: str = "raw", reasoning_prefix_in_prompt: bool = False) -> Task:
    rows = []
    for sample in samples:
        prompt = sample["input"]
        model_input = prompt if mode == "raw" else [
            ChatMessageSystem(content=SYSTEM), ChatMessageUser(content=prompt),
        ]
        metadata = {**sample["metadata"], "elicitation_mode": mode, "reasoning_prefix_in_prompt": reasoning_prefix_in_prompt}
        rows.append(Sample(id=sample["id"], input=model_input, metadata=metadata))
    return Task(
        dataset=rows, solver=seeded_completion(), scorer=development_outcomes(),
        cleanup=retain_evidence, version=1, time_limit=600,
    )


def main() -> None:
    if ROOT.resolve() != Path(__file__).resolve().parents[4]:
        raise SystemExit("Use this worktree's Python environment")
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel", type=Path, required=True)
    parser.add_argument("--lineage", type=Path, required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--server-receipt", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:18081/v1")
    parser.add_argument("--mode", choices=("raw", "native-chat"), default="raw")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-type", choices=("smoke", "calibration", "confirmation", "diagnostic"), default="calibration")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--max-connections", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.max_tokens < 1 or args.max_connections < 1 or (args.limit is not None and args.limit < 1):
        parser.error("Token, connection and sample limits must be positive")
    if not 0 <= args.temperature <= 2 or not 0 < args.top_p <= 1:
        parser.error("Invalid sampling settings")
    if args.limit is not None and args.run_type not in ("smoke", "diagnostic"):
        parser.error("A limited panel must be labeled smoke or diagnostic")
    endpoint = urlsplit(args.base_url)
    if endpoint.scheme != "http" or endpoint.hostname not in ("127.0.0.1", "localhost", "::1"):
        parser.error("Use the allocated local server or an SSH localhost forward")
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        parser.error("Server URL must not include credentials, query or fragment")
    if not args.dry_run and not args.server_receipt:
        parser.error("Execution requires a verified server receipt")
    try:
        checkpoint, lineage_hash = load_checkpoint(args.lineage, args.checkpoint)
        panel, panel_hash = load_panel(args.panel)
        receipt = verify_server_receipt(args.server_receipt, checkpoint, args.mode) if args.server_receipt else None
        served_model = receipt["served_model"] if receipt else checkpoint["model_id"]
        provider = "openai-api-completions" if args.mode == "raw" else "openai-api"
        model = f"{provider}/traindev/{served_model}"
        samples = panel["samples"][:args.limit] if args.limit else panel["samples"]
        generation = {
            "max_tokens": args.max_tokens, "temperature": args.temperature,
            "top_p": args.top_p, "cache": False,
            "max_retries": 0, "timeout": 300, "attempt_timeout": 300,
            "frequency_penalty": 0, "presence_penalty": 0, "num_choices": 1,
            "extra_body": {
                "top_k": -1, "min_p": 0, "repetition_penalty": 1.0,
                "add_special_tokens": False, "stop_token_ids": [100257, 100265],
                "return_token_ids": True,
            },
        }
        hashes = source_hashes(source_files())
        config = {
            "section": "development", "run_type": args.run_type,
            "model": model, "model_revision": checkpoint["model_revision"],
            "checkpoint": checkpoint, "lineage_manifest_sha256": lineage_hash,
            "panel_sha256": panel_hash, "assay_version": panel["assay_version"],
            "generation": generation, "mode": args.mode,
            "system_message": SYSTEM if args.mode == "native-chat" else None,
            "base_url": args.base_url, "server_receipt": receipt,
            "sample_ids": [sample["id"] for sample in samples],
            "epochs": 1, "max_connections": args.max_connections,
            "retry_policy": {
                "task_error_retries": 0, "sample_error_retries": 0,
                "provider_max_retries": generation["max_retries"],
                "sdk_max_retries": 0,
                "scorer_or_export_failure": "preserve_output_for_repair_without_regeneration",
            },
            "source_sha256": hashes, "inspect_ai_version": version("inspect-ai"),
            "openai_version": version("openai"), "git": git_state(),
            "preview_only": args.dry_run,
        }
        with run_directory(args.output, config) as manifest:
            save_snapshot(args.output, hashes)
            save_json_once(args.output / "panel.json", panel)
            save_json_once(args.output / "inputs.json", samples)
            save_json_once(args.output / "checkpoint.json", checkpoint)
            print(f"Output: {args.output.resolve()} ({len(samples)} samples)")
            if args.dry_run:
                return
            live_models = check_live_server(args.base_url, served_model)
            # Created timestamps and runtime fields can change across a clean restart.
            write_json(args.output / "live_models_latest.json", live_models)
            tokenized = tokenize_inputs(args.base_url, served_model, samples, args.mode)
            save_json_once(args.output / "tokenized_inputs.json", tokenized)
            save_json_once(args.output / "execution_host.json", {"hostname": socket.gethostname()})
            os.environ.setdefault("TRAINDEV_API_KEY", "unused")
            try:
                success, _ = eval_set(
                    development_panel(
                        samples, args.mode,
                        reasoning_prefix_in_prompt=(args.mode == "native-chat" and checkpoint.get("injected_reasoning_prefix_native_chat") == "<think>"),
                    ), model=model,
                    model_base_url=args.base_url, model_args={"stream": False, "max_retries": 0},
                    log_dir=str(args.output / "logs"), epochs=1,
                    max_tasks=1, max_samples=args.max_connections,
                    max_connections=args.max_connections,
                    retry_attempts=0, retry_immediate=False, retry_on_error=0,
                    retry_cleanup=False, fail_on_error=True,
                    log_model_api=True, metadata=run_metadata(manifest), **generation,
                )
            finally:
                rows = summarize(args.output / "logs", args.output)
                audit = audit_samples(rows, [sample["id"] for sample in samples])
                write_json(args.output / "execution_audit.json", audit)
    except (ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    if not success or not audit["complete"]:
        raise SystemExit("Some evaluations failed; preserve and inspect all attempts")


if __name__ == "__main__":
    main()
