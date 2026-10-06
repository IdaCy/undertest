import argparse
import hashlib
import json
import os
import subprocess
import zipfile
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from dotenv import load_dotenv
from inspect_ai import eval_set

from shared.results import summarize
from shared.runs import run_directory
from shared.runs import run_metadata
from shared.runs import write_json
from shared.tasks.report_task import FRAMING
from shared.tasks.report_task import ROOT
from shared.tasks.report_task import SYSTEM
from shared.tasks.report_task import render_request
from shared.tasks.report_task import report_task


def source_files() -> list[Path]:
    files = [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    for directory in ("src", "data", "tests"):
        files.extend(
            p for p in (ROOT / directory).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
            and not any(part.startswith(".") for part in p.relative_to(ROOT).parts)
        )
    return sorted(p for p in files if p.exists())


def main() -> None:
    if ROOT.resolve() != Path(__file__).resolve().parents[3]:
        raise SystemExit("Use this checkout's Python environment; the imported package belongs to another checkout.")
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="openai-api/qwen/Qwen/Qwen3.8-27B")
    parser.add_argument("--base-url")
    parser.add_argument("--model-revision", default="unrecorded")
    parser.add_argument("--section", default="setup")
    parser.add_argument("--run-type", choices=("smoke", "pilot", "confirmation", "diagnostic"), default="pilot")
    parser.add_argument("--conditions", nargs="+", choices=FRAMING, default=list(FRAMING))
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.epochs < 1 or args.max_tokens < 1:
        parser.error("epochs and max-tokens must be positive")
    output = args.output or ROOT / "outputs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    qwen = args.model.startswith("openai-api/qwen/")
    base_url = args.base_url or (os.environ.get("QWEN_BASE_URL") if qwen else None)
    generation = {"max_tokens": args.max_tokens, "cache": False}
    if qwen:
        generation.update(
            temperature=1.0, top_p=0.95,
            extra_body={"top_k": 20, "chat_template_kwargs": {"enable_thinking": True}},
        )
    if args.temperature is not None:
        generation["temperature"] = args.temperature
    files = source_files()
    config = {
        "model": args.model, "model_revision": args.model_revision,
        "section": args.section, "run_type": args.run_type,
        "base_url": base_url, "epochs": args.epochs,
        "conditions": args.conditions, "generation": generation,
        "inspect_ai_version": version("inspect-ai"),
        "source_sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files
        },
    }
    try:
        with run_directory(output, config) as manifest:
            if not (output / "source.zip").exists():
                pending_archive = output / "source.zip.tmp"
                with zipfile.ZipFile(pending_archive, "w", zipfile.ZIP_DEFLATED) as archive:
                    for path in files:
                        contents = path.read_bytes()
                        relative = str(path.relative_to(ROOT))
                        if hashlib.sha256(contents).hexdigest() != config["source_sha256"][relative]:
                            raise ValueError("Sources changed during setup; choose a new output directory.")
                        archive.writestr(relative, contents)
                pending_archive.replace(output / "source.zip")
            if not (output / "revision.json").exists():
                revision = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                )
                write_json(output / "revision.json", {
                    "git_head": revision.stdout.strip() if revision.returncode == 0 else None,
                })
            if not (output / "inputs.json").exists():
                write_json(output / "inputs.json", {
                    condition: {"system": SYSTEM, "user": render_request(condition)}
                    for condition in args.conditions
                })
            print(f"Output: {output.resolve()}")
            if args.dry_run:
                return
            try:
                success, _ = eval_set(
                    [report_task(condition) for condition in args.conditions],
                    model=args.model, model_base_url=base_url,
                    log_dir=str(output / "logs"), epochs=args.epochs,
                    max_tasks=1, max_samples=1, max_connections=1,
                    retry_attempts=2, retry_cleanup=False,
                    fail_on_error=False, log_model_api=True,
                    metadata=run_metadata(manifest),
                    **generation,
                )
            finally:
                summarize(output / "logs", output)
    except ValueError as exc:
        parser.error(str(exc))
    if not success:
        raise SystemExit("Some evaluations failed; see the logs and samples.csv.")


if __name__ == "__main__":
    main()
