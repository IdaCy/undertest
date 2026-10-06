from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import time


TYPES = ("oversight", "evaluation", "test_property", "simulation", "grading", "consequence", "capability")
INSTRUMENTS = ("presence", "strongly_favors", "leans_towards_or_favors")
LABELS = ("yes", "no", "not directly interpretable as such", "insufficient evidence to tell", "not understandable")
_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(encoded(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def schema(instrument):
    if instrument not in INSTRUMENTS:
        raise ValueError("unknown_instrument")
    evidence = {"type": "object", "properties": {
        "message_index": {"type": "integer"},
        "channel": {"type": "string", "enum": ["reasoning", "content"]},
        "quote": {"type": "string"}},
        "required": ["message_index", "channel", "quote"], "additionalProperties": False}
    judgment = {"type": "object", "properties": {
        "label": {"type": "string", "enum": list(LABELS)}, "reason": {"type": "string"},
        "evidence": {"type": "array", "items": evidence}},
        "required": ["label", "reason", "evidence"], "additionalProperties": False}
    return {"type": "object", "properties": {
        "instrument": {"type": "string", "enum": [instrument]},
        "judgments": {"type": "object", "properties": {name: judgment for name in TYPES},
                      "required": list(TYPES), "additionalProperties": False}},
        "required": ["instrument", "judgments"], "additionalProperties": False}


def prepare_context(messages, initial_context_length, phase="prefix", initial_context=None, prefix_context=None):
    if not isinstance(messages, list) or type(initial_context_length) is not int:
        raise ValueError("context_or_boundary_invalid")
    if not 0 <= initial_context_length <= len(messages):
        raise ValueError("context_boundary_out_of_range")
    if phase not in {"prefix", "full"}:
        raise ValueError("phase_invalid")
    if initial_context is not None and (len(initial_context) != initial_context_length or messages[:initial_context_length] != initial_context):
        raise ValueError("initial_context_prefix_mismatch")
    if prefix_context is not None and (len(prefix_context) > len(messages) or messages[:len(prefix_context)] != prefix_context):
        raise ValueError("paired_prefix_context_mismatch")
    rows, targets, missing, empty = [], {}, [], []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            raise ValueError("message_role_invalid")
        role = message["role"]
        actor = index >= initial_context_length and role == "assistant"
        content = message.get("content")
        reasoning = message.get("reasoning")
        alias = message.get("reasoning_content")
        if reasoning is not None and alias is not None and reasoning != alias:
            raise ValueError("reasoning_alias_disagreement")
        reasoning = reasoning if reasoning is not None else alias
        for value in (content, reasoning):
            if value is not None and not isinstance(value, str):
                raise ValueError("nontext_channel_requires_explicit_adapter")
        row = {"message_index": index, "role": role,
               "origin": "generated_actor" if actor else "provided_context" if index < initial_context_length else "non_actor_context",
               "content": content, "reasoning": reasoning}
        if message.get("tool_calls"):
            row["tool_calls"] = message["tool_calls"]
        rows.append(row)
        if actor:
            targets[index] = {"content": content, "reasoning": reasoning}
            if reasoning is None:
                missing.append(index)
            elif not reasoning.strip():
                empty.append(index)
    observation = {"initial_context_length": initial_context_length,
                   "boundary_verified_against_initial_context": initial_context is not None,
                   "generated_actor_message_indices": list(targets),
                   "missing_reasoning_message_indices": missing,
                   "captured_empty_reasoning_message_indices": empty,
                   "technical_missing_reasoning": bool(missing),
                   "eligible_text_available": any(isinstance(v, str) and v.strip() for row in targets.values() for v in row.values()),
                   "phase": phase,
                   "measurement_scope": "full_cumulative_generated_expression" if phase == "full" else "prefix_generated_expression",
                   "current_actor_belief_estimate": False,
                   "prefix_structurally_included": True if prefix_context is not None else None,
                   "paired_prefix_sha256": digest(prefix_context) if prefix_context is not None else None}
    return {"messages": rows, "observation": observation, "context_sha256": digest(messages)}, targets


def _quoted_spans(text):
    spans = []
    for pattern in (r"```[\s\S]*?```", r"~~~[\s\S]*?~~~", r"`[^`\n]*`", r"(?m)^\s*>[^\n]*", r'"[^"\n]+"', r"“[^”\n]+”"):
        spans.extend((match.start(), match.end()) for match in re.finditer(pattern, text))
    return spans


def validate_judgments(result, context, targets, instrument):
    import jsonschema
    jsonschema.validate(result, schema(instrument))
    excluded = [value for message in context["messages"] if message["origin"] != "generated_actor"
                for value in (message.get("content"), message.get("reasoning")) if isinstance(value, str) and value]
    checked = {}
    for name in TYPES:
        judgment = result["judgments"][name]
        if not judgment["reason"].strip() or len(judgment["reason"]) > 1200:
            raise ValueError("judgment_reason_invalid:" + name)
        if judgment["label"] == "yes" and not judgment["evidence"]:
            raise ValueError("positive_judgment_missing_evidence:" + name)
        evidence = []
        for item in judgment["evidence"]:
            index, channel, quote = item["message_index"], item["channel"], item["quote"]
            if type(index) is not int or index not in targets:
                raise ValueError("evidence_not_generated_actor:" + name)
            text = targets[index].get(channel)
            if not isinstance(text, str) or not quote.strip() or quote not in text:
                raise ValueError("evidence_not_exact_channel_substring:" + name)
            starts = [match.start() for match in re.finditer(re.escape(quote), text)]
            admissible = [start for start in starts if not any(start >= lo and start + len(quote) <= hi for lo, hi in _quoted_spans(text))]
            if judgment["label"] == "yes":
                if not admissible:
                    raise ValueError("positive_evidence_is_quoted_or_code_only:" + name)
            evidence.append({**item, "matching_character_offsets": starts,
                             "unquoted_character_offsets": admissible,
                             "also_present_in_excluded_context": any(quote in source for source in excluded),
                             "surrounding_actor_text": text[max(0, starts[0] - 160):min(len(text), starts[0] + len(quote) + 160)]})
        missing = context["observation"]["technical_missing_reasoning"]
        checked[name] = {**judgment, "evidence": evidence,
                         "admissible_label": None if missing and judgment["label"] != "yes" else judgment["label"],
                         "technical_missing_reasoning": missing,
                         "admission_scope": "positive_observed_expression_only" if missing else "captured_generated_expression"}
    return checked


def make_payload(config, prompt_directory, context, instrument):
    prompt_directory = Path(prompt_directory)
    rubric = (prompt_directory / "rubric.txt").read_bytes().decode("utf-8")
    threshold = (prompt_directory / (instrument + ".txt")).read_bytes().decode("utf-8")
    output_schema = schema(instrument)
    payload = {"model": config["model"], "reasoning": {"effort": "low"}, "store": False,
               "max_output_tokens": config.get("max_output_tokens", 8192),
               "input": [{"role": "system", "content": rubric + "\n\n" + threshold},
                         {"role": "user", "content": encoded(context).decode("utf-8")}],
               "text": {"format": {"type": "json_schema", "name": "awareness_reader", "strict": True,
                                    "schema": output_schema}}}
    if (config["model"] != "gpt-6-luna" or config.get("reasoning_effort", "low") != "low"
            or config.get("store", False) is not False or config.get("max_retries", 0) != 0):
        raise ValueError("reader_model_configuration_not_qualified")
    return payload, {"rubric_sha256": hashlib.sha256(rubric.encode()).hexdigest(),
                     "threshold_sha256": hashlib.sha256(threshold.encode()).hexdigest(),
                     "schema_sha256": digest(output_schema), "config_sha256": digest(config)}


def call_reader(client, config, prompt_directory, context, targets, instrument, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    payload, pins = make_payload(config, prompt_directory, context, instrument)
    save(destination / "request.json", payload)
    save(destination / "schema.json", schema(instrument))
    receipt = {"started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "instrument": instrument,
               "context_sha256": context["context_sha256"], "request_sha256": digest(payload),
               "source_sha256": _SOURCE_SHA256,
               "max_retries": 0, "store": False, **pins}
    save(destination / "attempt.json", receipt)
    started = time.monotonic()
    result = {"instrument": instrument, "status": "technical_missing", "judgments": None,
              "context_sha256": context["context_sha256"], "observation": context["observation"]}
    try:
        if getattr(client, "max_retries", None) != 0 or str(client.base_url).rstrip("/") != "https://api.openai.com/v1":
            raise ValueError("client_transport_contract_not_qualified")
        if not context["observation"]["eligible_text_available"]:
            result["technical_reason"] = "no_generated_actor_text_captured"
            receipt["api_call_performed"] = False
            return result
        receipt["api_call_performed"] = True
        raw_response = client.responses.with_raw_response.create(**payload)
        raw = raw_response.read() if callable(getattr(raw_response, "read", None)) else raw_response.content
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        with (destination / "response.raw").open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        receipt.update(http_status=raw_response.status_code,
                       api_request_id=raw_response.headers.get("x-request-id"),
                       response_sha256=hashlib.sha256(raw).hexdigest())
        response = json.loads(raw)
        receipt.update(response_id=response.get("id"), model=response.get("model"), usage=response.get("usage"),
                       response_status=response.get("status"))
        if response.get("status") != "completed":
            result["technical_reason"] = "response_not_completed"
            return result
        texts = [part["text"] for item in response.get("output", []) if item.get("type") == "message"
                 for part in item.get("content", []) if part.get("type") == "output_text"]
        if len(texts) != 1:
            result["technical_reason"] = "response_output_text_cardinality"
            return result
        parsed = json.loads(texts[0])
        save(destination / "parsed.json", parsed)
        result["judgments"] = validate_judgments(parsed, context, targets, instrument)
        result["status"] = "validated_partial_observation" if context["observation"]["technical_missing_reasoning"] else "validated"
        return result
    except Exception as error:
        failed_response = getattr(error, "response", None)
        if failed_response is not None and not (destination / "response.raw").exists():
            raw = failed_response.content
            if isinstance(raw, bytes):
                with (destination / "response.raw").open("xb") as stream:
                    stream.write(raw)
                receipt.update(http_status=failed_response.status_code,
                               api_request_id=failed_response.headers.get("x-request-id"),
                               response_sha256=hashlib.sha256(raw).hexdigest())
        result["technical_reason"] = type(error).__name__
        # Locally generated validation codes contain no credentials or transport headers.
        if isinstance(error, ValueError) and type(error) is ValueError:
            result["validation_error"] = str(error)[:300]
        return result
    finally:
        receipt.update(finished_at=dt.datetime.now(dt.timezone.utc).isoformat(), elapsed_seconds=time.monotonic() - started,
                       status=result["status"])
        save(destination / "receipt.json", receipt)
        save(destination / "result.json", result)


def run_context(client, config, prompt_directory, messages, initial_context_length, destination,
                phase="prefix", initial_context=None, prefix_context=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    context, targets = prepare_context(messages, initial_context_length, phase, initial_context, prefix_context)
    save(destination / "context.json", context)
    save(destination / "config.json", config)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {name: pool.submit(call_reader, client, config, prompt_directory, context, targets, name, destination / name)
                   for name in INSTRUMENTS}
        instruments = {name: future.result() for name, future in futures.items()}
    result = {"schema": "caus.reader_context.v1", "context_sha256": context["context_sha256"],
              "observation": context["observation"], "independent_instrument_calls": True,
              "threshold_results_inferred_from_other_calls": False, "instruments": instruments}
    save(destination / "RESULT.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    boundary = parser.add_mutually_exclusive_group(required=True)
    boundary.add_argument("--initial-context")
    boundary.add_argument("--initial-context-length", type=int)
    parser.add_argument("--prefix-context")
    parser.add_argument("--phase", choices=["prefix", "full"], default="prefix")
    parser.add_argument("--config", required=True)
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--env-file")
    args = parser.parse_args()
    from openai import OpenAI
    key = os.environ.get("OPENAI_API_KEY")
    if args.env_file:
        from dotenv import dotenv_values
        key = dotenv_values(args.env_file).get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("API credential unavailable")
    config = json.loads(Path(args.config).read_bytes())
    initial = json.loads(Path(args.initial_context).read_bytes()) if args.initial_context else None
    prefix = json.loads(Path(args.prefix_context).read_bytes()) if args.prefix_context else None
    with OpenAI(api_key=key, base_url="https://api.openai.com/v1", max_retries=0,
                timeout=config.get("timeout_seconds", 180)) as client:
        result = run_context(client, config, args.prompts, json.loads(Path(args.context).read_bytes()),
                             len(initial) if initial is not None else args.initial_context_length,
                             args.output, args.phase, initial, prefix)
    print(json.dumps({"output": str(args.output), "statuses": {name: row["status"] for name, row in result["instruments"].items()}}))


if __name__ == "__main__":
    main()
