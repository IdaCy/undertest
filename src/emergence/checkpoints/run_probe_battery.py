#!/usr/bin/env python3
"""Small Responses API worker. SQLite is the durable request/result ledger.

No model is selected implicitly. Mock and dry-run modes never instantiate a live
client. All live SDK retries are disabled: every worker retry is budgeted/saved.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import email.utils
import fcntl
import hashlib
import inspect
import json
import os
from pathlib import Path
import random
import sqlite3
import time

from jsonschema import Draft202012Validator
from signals.reasoning.checkpoint_probes import judgment_validation


VERSION = "broad_api_worker.20261003.v1"
FORBIDDEN = {"checkpoint_id", "model_revision", "training_stage", "awareness_labels",
             "other_pass", "other_pass_labels", "downstream_outcomes", "other_branches"}
HEADERS = {"x-request-id", "retry-after", "x-ratelimit-limit-requests",
           "x-ratelimit-limit-tokens", "x-ratelimit-remaining-requests",
           "x-ratelimit-remaining-tokens", "x-ratelimit-reset-requests",
           "x-ratelimit-reset-tokens", "x-ratelimit-limit-project-tokens",
           "x-ratelimit-remaining-project-tokens", "x-ratelimit-reset-project-tokens"}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def strict_loads(text):
    def reject_constant(value):
        raise ValueError("nonfinite_json")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result
    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=pairs)


def utc():
    return datetime.now(timezone.utc).isoformat()


def check_blinding(value):
    if isinstance(value, dict):
        if FORBIDDEN.intersection(value):
            raise ValueError("judge_input contains forbidden metadata fields")
        for child in value.values():
            check_blinding(child)
    elif isinstance(value, list):
        for child in value:
            check_blinding(child)


def strict_schema(value):
    """Remove only unsupported uniqueness metadata; enforce it locally instead.

    In particular this does not silently discard unions, required fields, bounds,
    or additionalProperties. Endpoint/model compatibility still needs a real call.
    """
    if isinstance(value, list):
        return [strict_schema(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {k: strict_schema(v) for k, v in value.items()
           if k not in {"$schema", "$id", "uniqueItems"}}
    if "const" in out:
        out["enum"] = [out.pop("const")]
    if "enum" in out and "type" not in out:
        names = {type(None): "null", bool: "boolean", int: "integer", float: "number", str: "string"}
        kinds = sorted({names[type(v)] for v in out["enum"]})
        out["type"] = kinds[0] if len(kinds) == 1 else kinds
    return out


@dataclass(frozen=True)
class Config:
    model: str
    label_version: str
    mode: str = "dry-run"
    service_tier: str = "default"
    reasoning_effort: str | None = None
    passes: int = 2
    concurrency: int = 4
    initial_concurrency: int = 2
    requests_per_minute: int = 30
    tokens_per_minute: int = 200000
    max_requests: int = 20
    max_reserved_tokens: int = 1000000
    max_output_tokens: int = 4096
    max_input_bytes: int = 1000000
    max_attempts: int = 3
    timeout_seconds: float = 180
    retry_base_seconds: float = 1
    rate_window_seconds: float = 60  # Exposed to tests, fixed at 60 by CLI.

    def check(self):
        if self.mode not in {"dry-run", "mock", "live"}:
            raise ValueError("invalid mode")
        if not self.model or not self.label_version:
            raise ValueError("model and new label version are required")
        for field in ("passes", "concurrency", "initial_concurrency", "requests_per_minute",
                      "tokens_per_minute", "max_requests", "max_reserved_tokens", "max_output_tokens",
                      "max_input_bytes", "max_attempts", "timeout_seconds", "rate_window_seconds"):
            if getattr(self, field) <= 0:
                raise ValueError(f"{field} must be positive")
        if self.initial_concurrency > self.concurrency or self.retry_base_seconds < 0:
            raise ValueError("invalid concurrency/retry setting")


class Ledger:
    def __init__(self, output: Path, manifest):
        output.mkdir(parents=True, exist_ok=True)
        self.lock = (output / ".worker.lock").open("a")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.db = sqlite3.connect(output / "ledger.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, record TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, job TEXT NOT NULL,
            attempt INTEGER NOT NULL, sent_at REAL NOT NULL, reserved_tokens INTEGER NOT NULL,
            request TEXT NOT NULL, UNIQUE(job, attempt));
          CREATE TABLE IF NOT EXISTS responses (request_id TEXT PRIMARY KEY, body TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS results (job TEXT PRIMARY KEY, body TEXT NOT NULL);
        """)
        old = self.db.execute("SELECT value FROM meta WHERE key='manifest'").fetchone()
        if old and old[0] != dumps(manifest):
            self.close()
            raise ValueError("output manifest differs; use a new version/output for changed settings")
        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('manifest', ?)", (dumps(manifest),))
        self.db.commit()

    def close(self):
        self.db.close()
        self.lock.close()

    def job(self, job_id, record):
        body = dumps(record)
        old = self.db.execute("SELECT record FROM jobs WHERE id=?", (job_id,)).fetchone()
        if old and old[0] != body:
            raise ValueError("same stable ID/version/pass has changed content; use a new label version")
        self.db.execute("INSERT OR IGNORE INTO jobs VALUES (?, ?)", (job_id, body))
        self.db.commit()

    def finished(self, job_id):
        return self.db.execute("SELECT 1 FROM results WHERE job=?", (job_id,)).fetchone() is not None

    def finish(self, job_id, status, **fields):
        record = json.loads(self.db.execute("SELECT record FROM jobs WHERE id=?", (job_id,)).fetchone()[0])
        body = {k: record[k] for k in ("original_id", "branch_id", "task_kind", "pass_id", "label_version")}
        body.update(job_id=job_id, status=status, completed_at=utc(), judgment=None)
        body.update(fields)
        self.db.execute("INSERT INTO results VALUES (?, ?)", (job_id, dumps(body)))
        self.db.commit()

    def recover(self):
        # A persisted dispatch without a final record may have incurred cost.
        # Never silently replay it after a crash, even if a response was saved.
        for (job_id,) in self.db.execute("SELECT DISTINCT job FROM requests WHERE job NOT IN (SELECT job FROM results)").fetchall():
            self.finish(job_id, "unknown_interrupted", reason="prior_attempt_has_no_terminal_record; inspect_saved_response_before_new_version")

    def has_budget(self, tokens, config):
        count, spent = self.db.execute("SELECT COUNT(*), COALESCE(SUM(reserved_tokens),0) FROM requests").fetchone()
        return count < config.max_requests and spent + tokens <= config.max_reserved_tokens

    def request(self, job_id, attempt, payload, tokens, config):
        if not self.has_budget(tokens, config):
            return None
        request_id = f"{job_id}.a{attempt}"
        self.db.execute("INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?)",
                        (request_id, job_id, attempt, time.time(), tokens, dumps(payload)))
        self.db.commit()  # Before any network request.
        return request_id

    def response(self, request_id, response):
        self.db.execute("INSERT INTO responses VALUES (?, ?)", (request_id, dumps(response)))
        self.db.commit()

    def export(self, output):
        rows = [json.loads(r[0]) for r in self.db.execute("SELECT body FROM results ORDER BY job")]
        tmp = output / "results.jsonl.tmp"
        with tmp.open("w") as file:
            for row in rows:
                file.write(dumps(row) + "\n")
            file.flush()
            os.fsync(file.fileno())
        tmp.replace(output / "results.jsonl")
        requests, reserved = self.db.execute("SELECT COUNT(*),COALESCE(SUM(reserved_tokens),0) FROM requests").fetchone()
        usage = Counter()
        for (body,) in self.db.execute("SELECT body FROM responses"):
            raw = json.loads(body).get("raw") or {}
            for k in ("input_tokens", "output_tokens", "total_tokens"):
                usage[k] += (raw.get("usage") or {}).get(k) or 0
        return {"statuses": dict(Counter(r["status"] for r in rows)), "requests": requests,
                "reserved_tokens": reserved, "reported_usage": dict(usage)}


class Gate:
    """Rolling request+token reservation limits, AIMD concurrency, global cooldown."""
    def __init__(self, config, prior=()):
        self.cfg = config
        self.target = config.initial_concurrency
        self.active = 0
        self.peak = 0
        self.successes = 0
        self.cooldown = 0.0
        self.history = deque(prior)
        self.changes = []
        self.condition = asyncio.Condition()

    async def enter(self, tokens, allowed=lambda: True):
        if tokens > self.cfg.tokens_per_minute:
            raise ValueError("request_reservation_exceeds_tokens_per_minute")
        async with self.condition:
            while True:
                if not allowed():
                    return False
                now = time.time()
                while self.history and self.history[0][0] <= now - self.cfg.rate_window_seconds:
                    self.history.popleft()
                room = len(self.history) < self.cfg.requests_per_minute and sum(v for _, v in self.history) + tokens <= self.cfg.tokens_per_minute
                if self.active < self.target and room and now >= self.cooldown:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    self.history.append((now, tokens))
                    return True
                delay = max(0.01, self.cooldown - now)
                if not room and self.history:
                    delay = max(delay, self.history[0][0] + self.cfg.rate_window_seconds - now)
                try:
                    await asyncio.wait_for(self.condition.wait(), min(delay, 60))
                except TimeoutError:
                    pass

    async def leave(self, throttled=False, delay=0.0, success=False):
        async with self.condition:
            self.active -= 1
            old = self.target
            if throttled:
                self.target = max(1, self.target // 2)
                self.successes = 0
                self.cooldown = max(self.cooldown, time.time() + delay)
            elif success:
                self.successes += 1
                if self.successes >= max(10, self.target * 2):
                    self.target = min(self.cfg.concurrency, self.target + 1)
                    self.successes = 0
            if self.target != old:
                self.changes.append({"time": utc(), "old": old, "new": self.target})
            self.condition.notify_all()


class CallError(Exception):
    def __init__(self, kind, status=None, code=None, headers=None):
        self.kind, self.status, self.code = kind, status, code
        self.headers = {k.lower(): v for k, v in (headers or {}).items() if k.lower() in HEADERS}
        super().__init__(kind)

    @property
    def retryable(self):
        return self.status in {408, 409, 429, 500, 502, 503, 504} and self.code not in {
            "insufficient_quota", "organization_usage_limit_exceeded", "billing_hard_limit_reached"}

    def record(self):
        # Never persist raw authentication exception text or request headers.
        return {"kind": self.kind, "http_status": self.status, "code": self.code, "headers": self.headers}

    def retry_after(self):
        raw = self.headers.get("retry-after", "0")
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                return max(0.0, email.utils.parsedate_to_datetime(raw).timestamp() - time.time())
            except (ValueError, TypeError):
                return 0.0


class OpenAITransport:
    def __init__(self, config):
        from openai import AsyncOpenAI
        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY is absent; live mode requires owner-provisioned API credentials")
        # Explicit official endpoint prevents an inherited local actor URL.
        self.client = AsyncOpenAI(base_url="https://api.openai.com/v1", max_retries=0, timeout=config.timeout_seconds)

    async def call(self, payload, record, attempt):
        from openai import APIConnectionError, APIStatusError
        try:
            response = await self.client.responses.with_raw_response.create(**payload)
            parsed = response.parse()
            if inspect.isawaitable(parsed):
                parsed = await parsed
            headers = {k: v for k, v in response.headers.items() if k.lower() in HEADERS}
            return {"raw": parsed.model_dump(mode="json"), "headers": headers}
        except APIStatusError as exc:
            raise CallError(type(exc).__name__, exc.status_code, getattr(exc, "code", None), exc.response.headers) from None
        except APIConnectionError as exc:
            raise CallError(type(exc).__name__) from None

    async def close(self):
        await self.client.close()


class MockTransport:
    """Scripted fixtures are local transport data and are never sent to a model."""
    def __init__(self, script):
        self.script = script
        self.calls = []

    async def call(self, payload, record, attempt):
        self.calls.append((record["original_id"], record["pass_id"], payload))
        await asyncio.sleep(0.002)
        sequence = self.script[record["original_id"]]
        item = sequence[min(attempt - 1, len(sequence) - 1)]
        if "error" in item:
            raise CallError(**item["error"])
        return {"raw": item, "headers": {"x-request-id": f"mock-{len(self.calls)}"}}

    async def close(self):
        pass


def parse_response(response, validator):
    raw = response["raw"]
    if raw.get("status") != "completed":
        return "incomplete", None, raw.get("incomplete_details") or raw.get("error")
    content = [part for message in raw.get("output", []) if message.get("type") == "message"
               for part in message.get("content", [])]
    if any(part.get("type") == "refusal" for part in content):
        return "refused", None, "api_refusal"
    text = "".join(part["text"] for part in content if part.get("type") == "output_text")
    try:
        value = strict_loads(text)
        validator.validate(value)
    except (ValueError, TypeError, KeyError):
        return "invalid_output", None, "invalid_json"
    except Exception as exc:
        from jsonschema.exceptions import ValidationError
        if not isinstance(exc, ValidationError):
            raise
        return "invalid_output", None, "json_schema_validation_failed"
    return "completed", value, None


def load_rows(path):
    with Path(path).open() as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = strict_loads(line)
            if set(row) not in ({"original_id", "branch_id", "task_kind", "judge_input"},
                                {"original_id", "branch_id", "task_kind", "judge_input", "local_validation"}):
                raise ValueError(f"row {line_no}: exact input envelope fields required")
            if not isinstance(row["original_id"], str) or not row["original_id"] or not isinstance(row["task_kind"], str) or not row["task_kind"]:
                raise ValueError(f"row {line_no}: nonempty stable IDs required")
            if row["branch_id"] is not None and (not isinstance(row["branch_id"], str) or not row["branch_id"]):
                raise ValueError(f"row {line_no}: invalid branch ID")
            if not isinstance(row["judge_input"], dict):
                raise ValueError(f"row {line_no}: judge_input must be a blinded object")
            check_blinding(row["judge_input"])
            judgment_validation.preflight(row)
            yield row


async def run(config, input_path, output, prompt, schema, transport=None):
    config.check()
    Draft202012Validator.check_schema(schema)
    wire_schema = strict_schema(schema)
    validator = Draft202012Validator(schema)
    output = Path(output)
    manifest = {"worker_version": VERSION, "worker_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "config": asdict(config), "prompt_sha256": digest(prompt),
                "schema_sha256": digest(schema), "wire_schema_sha256": digest(wire_schema),
                "validator_sha256": hashlib.sha256(Path(judgment_validation.__file__).read_bytes()).hexdigest(),
                "annotation_implementations": judgment_validation.implementation_hashes(),
                "token_reservation": "serialized_request_utf8_bytes_plus_1024_plus_max_output_tokens; conservative_estimate_not_tokenizer",
                "validation": "task_kind selects preserved V3 or broad-score evidence checks; semantic accuracy still requires calibration"}
    ledger = Ledger(output, manifest)
    ledger.recover()
    prior = ledger.db.execute("SELECT sent_at,reserved_tokens FROM requests ORDER BY sent_at").fetchall()
    gate = Gate(config, prior)
    queue = asyncio.Queue(maxsize=2 * config.concurrency)
    if config.mode == "live" and transport is None:
        try:
            transport = OpenAITransport(config)
        except Exception:
            ledger.close()
            raise
    if config.mode == "mock" and transport is None:
        ledger.close()
        raise ValueError("mock mode requires scripted transport")

    control = {"stop": None}

    def payload_for(record):
        payload = {"model": config.model, "service_tier": config.service_tier, "store": False,
                   "truncation": "disabled", "max_output_tokens": config.max_output_tokens,
                   "instructions": prompt,
                   "input": [{"role": "user", "content": dumps(record["judge_input"])}],
                   "text": {"format": {"type": "json_schema", "name": "undertest_judgment", "strict": True, "schema": wire_schema}}}
        if config.reasoning_effort:
            payload["reasoning"] = {"effort": config.reasoning_effort}
        return payload

    async def produce():
        seen = set()
        for row in load_rows(input_path):
            for pass_number in range(1, config.passes + 1):
                record = dict(row, pass_id=f"pass{pass_number}", label_version=config.label_version)
                identity = {k: record[k] for k in ("original_id", "branch_id", "task_kind", "pass_id", "label_version")}
                job_id = "J-" + digest(identity)
                ledger.job(job_id, record)
                if job_id in seen or ledger.finished(job_id):
                    continue
                seen.add(job_id)
                await queue.put((job_id, record))
        for _ in range(config.concurrency):
            await queue.put(None)

    async def process(job_id, record):
        payload = payload_for(record)
        size = len(dumps(payload).encode())
        reservation = size + 1024 + config.max_output_tokens
        if config.mode == "dry-run":
            ledger.finish(job_id, "dry_run", request=payload, reserved_tokens=reservation)
            return
        if size > config.max_input_bytes or reservation > config.tokens_per_minute:
            ledger.finish(job_id, "unavailable", reason="input_or_rate_capacity_exceeded; no_truncation", reserved_tokens=reservation)
            return
        for attempt in range(1, config.max_attempts + 1):
            if control["stop"]:
                ledger.finish(job_id, "unavailable", reason=control["stop"])
                return
            if not ledger.has_budget(reservation, config):
                ledger.finish(job_id, "budget_exhausted", reason="request_or_reserved_token_limit")
                return
            admitted = await gate.enter(reservation, lambda: ledger.has_budget(reservation, config) and control["stop"] is None)
            if not admitted:
                ledger.finish(job_id, "unavailable" if control["stop"] else "budget_exhausted",
                              reason=control["stop"] or "request_or_reserved_token_limit")
                return
            if control["stop"]:
                await gate.leave()
                ledger.finish(job_id, "unavailable", reason=control["stop"])
                return
            request_id = ledger.request(job_id, attempt, payload, reservation, config)
            if request_id is None:
                await gate.leave()
                ledger.finish(job_id, "budget_exhausted", reason="request_or_reserved_token_limit")
                return
            started = time.monotonic()
            try:
                response = await transport.call(payload, record, attempt)
            except CallError as exc:
                delay = max(exc.retry_after(), config.retry_base_seconds * 2 ** (attempt - 1) * random.uniform(1, 1.25))
                ledger.response(request_id, {"error": exc.record(), "elapsed_seconds": time.monotonic() - started})
                await gate.leave(throttled=exc.status in {429, 503}, delay=delay)
                if exc.status in {400, 401, 403, 404} or exc.code in {"insufficient_quota", "organization_usage_limit_exceeded", "billing_hard_limit_reached"}:
                    control["stop"] = "api_access_schema_model_tier_or_quota_error; inspect_immutable_error_record"
                if exc.retryable and attempt < config.max_attempts:
                    await asyncio.sleep(delay)
                    continue
                status = "unknown_delivery" if exc.status is None else "error"
                ledger.finish(job_id, status, request_id=request_id, reason=exc.record())
                return
            except Exception as exc:
                ledger.response(request_id, {"error": {"kind": type(exc).__name__}, "elapsed_seconds": time.monotonic() - started})
                await gate.leave()
                ledger.finish(job_id, "unknown_delivery", request_id=request_id, reason="unexpected_transport_exception")
                return
            response["elapsed_seconds"] = time.monotonic() - started
            ledger.response(request_id, response)
            status, value, reason = parse_response(response, validator)
            validation = "json_schema_only"
            if status == "completed":
                try:
                    validation = judgment_validation.validate(record, value, schema)
                except (ValueError, KeyError, TypeError) as exc:
                    status, value, reason = "invalid_output", None, "evidence_or_metadata_validation_failed:" + str(exc)[:200]
            await gate.leave(success=status == "completed")
            usage = response["raw"].get("usage") or {}
            if (usage.get("total_tokens") or 0) > reservation:
                control["stop"] = "token_reservation_underestimated; revise_estimator_before_more_calls"
            ledger.finish(job_id, status, request_id=request_id, judgment=value, reason=reason,
                          validation=validation, response_id=response["raw"].get("id"),
                          actual_model=response["raw"].get("model"), actual_service_tier=response["raw"].get("service_tier"),
                          usage=usage, reservation_underestimated=(usage.get("total_tokens") or 0) > reservation)
            return

    async def consume():
        while (item := await queue.get()) is not None:
            await process(*item)

    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(produce())
            for _ in range(config.concurrency):
                group.create_task(consume())
        summary = ledger.export(output)
        summary.update(mode=config.mode, peak_concurrency=gate.peak, final_concurrency=gate.target,
                       concurrency_changes=gate.changes, label_version=config.label_version)
        (output / "summary.json").write_text(dumps(summary) + "\n")
        return summary
    finally:
        if transport:
            await transport.close()
        ledger.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("input", "output", "prompt", "schema", "model", "label-version"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--mode", choices=["dry-run", "mock", "live"], default="dry-run")
    parser.add_argument("--mock-script")
    parser.add_argument("--service-tier", default="default")
    parser.add_argument("--reasoning-effort")
    defaults = Config("placeholder", "placeholder")
    for name in ("passes", "concurrency", "initial_concurrency", "requests_per_minute", "tokens_per_minute",
                 "max_requests", "max_reserved_tokens", "max_output_tokens", "max_input_bytes", "max_attempts"):
        parser.add_argument("--" + name.replace("_", "-"), type=int, default=getattr(defaults, name))
    parser.add_argument("--timeout-seconds", type=float, default=180)
    args = parser.parse_args()
    fields = {k: getattr(args, k) for k in Config.__dataclass_fields__ if hasattr(args, k)}
    transport = MockTransport(json.loads(Path(args.mock_script).read_text())) if args.mode == "mock" else None
    summary = asyncio.run(run(Config(**fields), args.input, args.output, Path(args.prompt).read_text(),
                              json.loads(Path(args.schema).read_text()), transport))
    print(dumps(summary))


if __name__ == "__main__":
    main()
