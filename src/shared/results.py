import csv
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlsplit

from inspect_ai.log import list_eval_logs, read_eval_log
from inspect_ai.scorer import CORRECT, INCORRECT


RESERVED_ARTIFACTS = {"messages.json", "scores.json", "metadata.json", "export_errors.json"}
RETRY_TRANSPORT_FIELDS = {
    "max_retries", "timeout", "attempt_timeout", "stream_idle_timeout",
    "max_connections", "adaptive_connections",
}


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def csv_value(value):
    return json_text(value) if isinstance(value, (dict, list)) else value


def safe_destination(root: Path, relative: str) -> Path:
    """Reject ambiguous paths and symlinks before creating any export files."""
    if not isinstance(relative, str) or not relative or "\\" in relative or "\0" in relative:
        raise ValueError("artifact path must be a nonempty relative POSIX path")
    path = PurePosixPath(relative)
    if path.is_absolute() or PureWindowsPath(relative).drive or ".." in path.parts or not path.parts:
        raise ValueError("artifact path must stay inside the export directory")
    destination = root
    for part in path.parts:
        destination = destination / part
        if destination.is_symlink():
            raise ValueError("symlinks are not allowed in export paths")
    if not destination.resolve().is_relative_to(root.resolve()):
        raise ValueError("artifact path escapes the export directory")
    return destination


def export_sample(output: Path, key: str, sample, scores, metadata) -> tuple[Path, list[dict]]:
    directory = output / "artifacts" / key
    errors = []
    try:
        directory = safe_destination(output, f"artifacts/{key}")
        directory.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as exc:
        return directory, [{"path": str(directory), "error": str(exc)}]

    def write(relative: str, content: str) -> None:
        try:
            path = safe_destination(directory, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except (OSError, ValueError) as exc:
            errors.append({"path": relative, "error": str(exc)})

    stored = sample.store or {}
    named = stored.get("artifacts", {})
    if not isinstance(named, dict):
        errors.append({"path": "artifacts", "error": "artifacts must map relative paths to text"})
        named = {}
    else:
        named = dict(named)
    submission = stored.get("submitted_report_py", stored.get("report_py"))
    if submission is not None and "report.py" not in named:
        named["report.py"] = submission
    seen = set()
    for relative, content in named.items():
        try:
            safe_destination(directory, relative)
            normalized = PurePosixPath(relative).as_posix()
            if PurePosixPath(relative).parts[0].casefold() in RESERVED_ARTIFACTS:
                raise ValueError("artifact name is reserved for exporter evidence")
            if normalized in seen:
                raise ValueError("duplicate normalized artifact path")
            if not isinstance(content, str):
                raise ValueError("artifact content must be text")
            seen.add(normalized)
            write(normalized, content)
        except (OSError, TypeError, ValueError) as exc:
            errors.append({"path": str(relative), "error": str(exc)})
    write("messages.json", json.dumps(
        [message.model_dump(mode="json") for message in sample.messages],
        ensure_ascii=False, indent=2,
    ) + "\n")
    write("scores.json", json.dumps(scores, ensure_ascii=False, indent=2) + "\n")
    write("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    write("export_errors.json", json.dumps(errors, ensure_ascii=False, indent=2) + "\n")
    return directory, errors


def attempt_group(log, log_path: Path) -> str:
    spec = log.eval
    metadata = spec.metadata or {}
    undertest = metadata.get("undertest", {})
    if not isinstance(undertest, dict):
        undertest = {}
    if undertest.get("run_id") and undertest.get("config_id"):
        namespace = ("undertest", undertest["run_id"], undertest["config_id"])
    elif spec.task_id:
        namespace = ("legacy", str(log_path.parent.resolve()), spec.eval_set_id, spec.task_id)
    else:
        namespace = ("single_eval", str(log_path.resolve()))
    # Inspect's native run_id/eval_id change on retry. The task_id persists.
    settings = spec.model_dump(mode="json", include={
        "task", "task_id", "task_version", "task_args", "task_args_passed", "task_attribs",
        "solver", "solver_args", "solver_args_passed", "sandbox", "model", "model_base_url",
        "model_args", "model_roles", "model_generate_config", "config", "revision", "packages",
        "metadata", "scorers",
    })
    settings["model_generate_config"] = spec.model_generate_config.model_dump(
        mode="json", exclude=RETRY_TRANSPORT_FIELDS,
    )
    settings["plan"] = log.plan.model_dump(mode="json")
    settings["plan"]["config"] = log.plan.config.model_dump(
        mode="json", exclude=RETRY_TRANSPORT_FIELDS,
    )
    return hashlib.sha256(json_text([namespace, settings]).encode()).hexdigest()


def summarize(log_dir: Path, output: Path) -> list[dict]:
    rows = []
    output.mkdir(parents=True, exist_ok=True)
    for info in sorted(list_eval_logs(str(log_dir), recursive=True), key=lambda info: info.name):
        log = read_eval_log(info.name, resolve_attachments=True)
        log_path = Path(unquote(urlsplit(info.name).path)) if info.name.startswith("file://") else Path(info.name)
        eval_metadata = log.eval.metadata
        undertest = (eval_metadata or {}).get("undertest", {})
        if not isinstance(undertest, dict):
            undertest = {}
        common = {
            "log": str(log_path.resolve()),
            "eval_created": str(log.eval.created),
            "eval_status": log.status,
            "task": log.eval.task,
            "model": log.eval.model,
            "run_id": undertest.get("run_id", ""),
            "config_id": undertest.get("config_id", ""),
            "model_revision": undertest.get("model_revision", ""),
            "section": undertest.get("section", ""),
            "run_type": undertest.get("run_type", ""),
            "eval_set_id": log.eval.eval_set_id,
            "eval_id": log.eval.eval_id,
            "inspect_run_id": log.eval.run_id,
            "task_id": log.eval.task_id,
            "task_version": log.eval.task_version,
            "eval_metadata": json_text(eval_metadata),
            "log_metadata": json_text(log.metadata),
            "eval_error": log.error.message if log.error else "",
            "eval_error_details": json_text(log.error.model_dump(mode="json")) if log.error else "",
            "attempt_group": attempt_group(log, log_path),
        }
        if not log.samples:
            rows.append({**common, "sample_status": "no_samples", "error": log.error.message if log.error else ""})
        for sample in log.samples or []:
            relative_log = log_path.absolute().relative_to(log_dir.absolute())
            identity = f"{relative_log}:{sample.id}:{sample.epoch}"
            key = hashlib.sha256(identity.encode()).hexdigest()[:20]
            stored = sample.store or {}
            scores = sample.scores or {}
            raw_scores = {
                name: value.model_dump(mode="json") for name, value in scores.items()
            } if sample.scores is not None else None
            metadata = {"sample": sample.metadata, "eval": eval_metadata, "log": log.metadata}
            artifacts, export_errors = export_sample(output, key, sample, raw_scores, metadata)
            score = scores.get("task_success")
            success_value = score.value if score else None
            if isinstance(success_value, str):
                success_value = {CORRECT: 1, INCORRECT: 0}.get(success_value, success_value)
            reasoning = any(
                getattr(content, "type", None) == "reasoning"
                for message in sample.messages
                if message.role == "assistant" and isinstance(message.content, list)
                for content in message.content
            )
            rows.append({
                **common,
                "sample_key": key,
                "sample_id": sample.id,
                "epoch": sample.epoch,
                "condition": sample.metadata.get("condition", ""),
                "sample_metadata": json_text(sample.metadata),
                "sample_status": "error" if sample.error else "limit" if sample.limit else "completed",
                "task_success": csv_value(success_value) if score else "",
                "score_detail": score.explanation if score else "",
                "scores": json_text(raw_scores),
                "reasoning_returned": reasoning,
                "error": sample.error.message if sample.error else "",
                "error_details": json_text(sample.error.model_dump(mode="json")) if sample.error else "",
                "error_retries": json_text([error.model_dump(mode="json") for error in sample.error_retries])
                if sample.error_retries is not None else "",
                "limit": json.dumps(sample.limit.model_dump(mode="json")) if sample.limit else "",
                "artifact_error": stored.get("artifact_error", ""),
                "artifact_export_errors": json_text(export_errors),
                "artifacts": str(artifacts.resolve()),
                "input_tokens": sum(usage.input_tokens for usage in (sample.model_usage or {}).values()),
                "output_tokens": sum(usage.output_tokens for usage in (sample.model_usage or {}).values()),
                **{
                    f"score.{name}.{field}": csv_value(payload[field])
                    for name, payload in (raw_scores or {}).items()
                    for field in ("value", "explanation", "metadata")
                },
            })
    rows.sort(key=lambda row: (row["eval_created"], row["log"]))
    latest = {}
    for index, row in enumerate(rows):
        identity = (
            row["attempt_group"], json_text(row.get("condition")),
            row.get("sample_id"), row.get("epoch"),
        ) if "sample_id" in row else (row["log"],)
        if identity in latest:
            rows[latest[identity]]["latest_attempt"] = False
        latest[identity] = index
        row["latest_attempt"] = True
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with (output / "samples.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved {len(rows)} rows to {output / 'samples.csv'}")
    return rows
