import csv
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from inspect_ai.log import list_eval_logs, read_eval_log
from inspect_ai.scorer import CORRECT, INCORRECT


def summarize(log_dir: Path, output: Path) -> list[dict]:
    rows = []
    output.mkdir(parents=True, exist_ok=True)
    for info in sorted(list_eval_logs(str(log_dir), recursive=True), key=lambda info: info.name):
        log = read_eval_log(info.name, resolve_attachments=True)
        log_path = Path(unquote(urlsplit(info.name).path)) if info.name.startswith("file://") else Path(info.name)
        common = {
            "log": str(log_path.resolve()),
            "eval_created": str(log.eval.created),
            "eval_status": log.status,
            "task": log.eval.task,
            "model": log.eval.model,
        }
        if not log.samples:
            rows.append({**common, "sample_status": "no_samples", "error": log.error.message if log.error else ""})
        for sample in log.samples or []:
            relative_log = log_path.absolute().relative_to(log_dir.absolute())
            identity = f"{relative_log}:{sample.id}:{sample.epoch}"
            key = hashlib.sha256(identity.encode()).hexdigest()[:20]
            artifacts = output / "artifacts" / key
            artifacts.mkdir(parents=True, exist_ok=True)
            stored = sample.store or {}
            submission = stored.get("submitted_report_py", stored.get("report_py"))
            if submission is not None:
                (artifacts / "report.py").write_text(submission)
            (artifacts / "messages.json").write_text(json.dumps(
                [message.model_dump(mode="json") for message in sample.messages],
                indent=2,
            ) + "\n")
            scores = sample.scores or {}
            score = next(iter(scores.values()), None)
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
                "sample_status": "error" if sample.error else "limit" if sample.limit else "completed",
                "task_success": {CORRECT: 1, INCORRECT: 0}.get(score.value, score.value) if score else "",
                "score_detail": score.explanation if score else "",
                "reasoning_returned": reasoning,
                "error": sample.error.message if sample.error else "",
                "limit": json.dumps(sample.limit.model_dump(mode="json")) if sample.limit else "",
                "artifact_error": stored.get("artifact_error", ""),
                "artifacts": str(artifacts.resolve()),
                "input_tokens": sum(usage.input_tokens for usage in (sample.model_usage or {}).values()),
                "output_tokens": sum(usage.output_tokens for usage in (sample.model_usage or {}).values()),
            })
    rows.sort(key=lambda row: (row["eval_created"], row["log"]))
    latest = {}
    for index, row in enumerate(rows):
        identity = (
            row["task"], row["model"], row.get("condition"),
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
