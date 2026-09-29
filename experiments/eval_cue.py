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

from undertest.results import summarize
from undertest.tasks.report_task import FRAMING, ROOT, SYSTEM, render_request, report_task


def source_files() -> list[Path]:
    files = [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    for directory in ("src", "experiments", "data", "tests"):
        files.extend(
            p for p in (ROOT / directory).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
            and (p.suffix in {".py", ".json", ".yaml", ".toml"} or p.name == "Dockerfile")
            and not any(part.startswith(".") for part in p.relative_to(ROOT).parts)
        )
    return sorted(p for p in files if p.exists())


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="openai-api/qwen/Qwen/Qwen3.8-27B")
    parser.add_argument("--base-url")
    parser.add_argument("--model-revision", default="unrecorded")
    parser.add_argument("--conditions", nargs="+", choices=FRAMING, default=list(FRAMING))
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.epochs < 1 or args.max_tokens < 1:
        parser.error("epochs and max-tokens must be positive")
    output = args.output or ROOT / "results" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
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
    manifest = {
        "model": args.model, "model_revision": args.model_revision,
        "base_url": base_url, "epochs": args.epochs,
        "conditions": args.conditions, "generation": generation,
        "inspect_ai_version": version("inspect-ai"),
        "source_sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files
        },
    }
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
        parser.error("This output directory belongs to a different configuration or source version; choose a new --output.")
    if not manifest_path.exists():
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        with zipfile.ZipFile(output / "source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, str(path.relative_to(ROOT)))
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
        )
        (output / "revision.json").write_text(json.dumps({
            "git_head": revision.stdout.strip() if revision.returncode == 0 else None,
        }, indent=2) + "\n")
    (output / "inputs.json").write_text(json.dumps({
        condition: {"system": SYSTEM, "user": render_request(condition)}
        for condition in args.conditions
    }, indent=2) + "\n")
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
            **generation,
        )
    finally:
        summarize(output / "logs", output)
    if not success:
        raise SystemExit("Some evaluations failed; see the logs and samples.csv.")


if __name__ == "__main__":
    main()
