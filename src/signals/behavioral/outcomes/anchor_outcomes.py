"""Offline joins for the fixed anchor collector; no execution or semantic scoring.

The caller supplies expected file pins and a bytes-only resolver. Matching these
pins authenticates the supplied saved documents, not their owner's adoption,
physical deployment, rendering, actor access, factual truth or model identity.
"""

from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
import re


SCHEMA = "sigres-anchor-saved-outcomes-v1"
TYPES = ("evaluation_status", "evaluation_purpose", "grader_reward",
         "monitoring_oversight", "simulation_status", "consequences_stakes",
         "training_lifecycle", "audience_demand")
MODEL = "Qwen/Qwen3.8-27B-FP8"
REVISION = "017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"
GENERATION = {"temperature": 1.0, "top_p": 0.95, "top_k": 20,
              "chat_template_kwargs": {"enable_thinking": True,
                                       "preserve_thinking": True,
                                       "reasoning_effort": "xhigh"}}
ACTOR = {"model": MODEL, "model_revision": REVISION, "generation": GENERATION}
PACKET_SOURCES = {"panel": "source/anchor_dev/panel.py",
                  "task_prototypes": "source/anchor_dev/task_prototypes.py",
                  "versioned_ledger": "source/anchor_dev/versioned_ledger.py",
                  "package": "source/anchor_dev/__init__.py"}
# The native response ceiling is 4 MiB; an invalid scorer record can retain
# its entire public content as hex (>8 MiB including metadata).
MAX_MEMBER_BYTES = 16 * 1024**2


class AnchorOutcomeError(ValueError):
    """The source frame/terminal cannot define an unambiguous population."""


class _UnavailableScore(Exception):
    pass


def _require(condition, reason):
    if not condition:
        raise AnchorOutcomeError(reason)


def _wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _pin(pin):
    _require(type(pin) is dict and type(pin.get("bytes")) is int
             and 0 <= pin["bytes"] <= MAX_MEMBER_BYTES
             and type(pin.get("sha256")) is str
             and re.fullmatch(r"[0-9a-f]{64}", pin["sha256"]) is not None,
             "invalid_file_pin")
    locators = [k for k in ("path", "name") if k in pin]
    _require(len(locators) == 1 and type(pin[locators[0]]) is str
             and bool(pin[locators[0]]), "invalid_file_locator")
    return pin


def _same_pin(left, right):
    return _pin(left) == _pin(right)


def _read(read_exact, pin):
    _pin(pin)
    try:
        body = read_exact(deepcopy(pin))
    except Exception as exc:
        raise AnchorOutcomeError("unavailable_saved_bytes:" + type(exc).__name__) from exc
    _require(type(body) is bytes, "resolver_must_return_bytes")
    _require(len(body) == pin["bytes"] and _sha(body) == pin["sha256"],
             "saved_bytes_pin_mismatch")
    return body


def _json(body):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    def number(text):
        value = float(text)
        _require(math.isfinite(value), "nonfinite_json")
        return value

    def constant(_):
        raise AnchorOutcomeError("nonfinite_json")

    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=pairs,
                           parse_float=number, parse_constant=constant)
        _wire(value)  # Reject escaped lone surrogates before later UTF-8 joins.
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
        if isinstance(exc, AnchorOutcomeError):
            raise
        raise AnchorOutcomeError("invalid_json:" + type(exc).__name__) from exc


def _table(rows, key, count):
    _require(type(rows) is list and len(rows) == count, "invalid_source_table_count")
    result = {}
    for row in rows:
        _require(type(row) is dict and type(row.get(key)) is str
                 and row[key] and row[key] not in result, "duplicate_or_invalid_source_identity")
        result[row[key]] = row
    return result


def _request(messages, seed):
    return {**deepcopy(GENERATION), "model": MODEL, "messages": deepcopy(messages),
            "tools": None, "tool_choice": "none", "parallel_tool_calls": False,
            "max_tokens": 4096, "stream": False, "n": 1, "seed": seed}


def _saved_message(response):
    """Locate the saved message; do not repeat the producer's native admission."""
    _require(type(response) is dict, "saved_response_shape")
    choices = response.get("choices")
    _require(type(choices) is list and len(choices) == 1 and type(choices[0]) is dict
             and type(choices[0].get("message")) is dict, "saved_message_shape")
    return choices[0]["message"]


def _span(text, evidence):
    values = [evidence.get(k) for k in ("start_codepoint", "end_codepoint",
                                      "start_utf8_byte", "end_utf8_byte")]
    _require(type(text) is str and all(type(x) is int for x in values), "span_type")
    start, end, byte_start, byte_end = values
    _require(0 <= start < end <= len(text) and type(evidence.get("quote")) is str
             and text[start:end] == evidence["quote"], "literal_span_mismatch")
    _require((len(text[:start].encode("utf-8")), len(text[:end].encode("utf-8")))
             == (byte_start, byte_end), "utf8_span_mismatch")


def _sources(frame_pin, packet_manifest_pin, source_pins, read_exact):
    _require(type(source_pins) is dict
             and set(source_pins) == {"collector", "frame_builder", *PACKET_SOURCES},
             "exact_source_pin_names_required")
    for pin in source_pins.values():
        _read(read_exact, pin)  # Authenticate bytes only; never import or execute.
    manifest = _json(_read(read_exact, packet_manifest_pin))
    _require(type(manifest) is dict and type(manifest.get("files")) is list,
             "packet_manifest_shape")
    members = {}
    for member in manifest["files"]:
        _pin(member)
        _require("path" in member and member["path"] not in members, "duplicate_packet_member")
        members[member["path"]] = member
    for name, path in PACKET_SOURCES.items():
        _require(path in members and all(source_pins[name][k] == members[path][k]
                                         for k in ("bytes", "sha256")), "source_member_pin_mismatch")

    def member(path):
        _require(path in members, "missing_packet_dependency:" + path)
        return _json(_read(read_exact, members[path]))

    frame = _json(_read(read_exact, frame_pin))
    _require(type(frame) is dict and frame.get("schema") == "sigres2-anchor-literal-frame-v1"
             and frame.get("source_manifest_sha256") == packet_manifest_pin["sha256"]
             and frame.get("originals") == 12 and frame.get("independent_forks") == 96,
             "unsupported_fixed_frame")
    by_id = _table(frame.get("rows"), "assignment_id", 108)
    catalog = _table(member("researcher/case_catalog.json"), "case_id", 12)
    facts = _table(member("researcher/case_fact_keys.json"), "case_id", 12)
    current = member("researcher/current_research_facts.json")
    dictionary = member("researcher/evidence_dictionary.json")
    _require(set(catalog) == set(facts), "case_fact_population_mismatch")
    originals = {r["assignment_id"]: r for r in by_id.values() if r.get("kind") == "original"}
    asks = [r for r in by_id.values() if r.get("kind") == "ask"]
    _require(len(originals) == 12 and len(asks) == 96
             and {r.get("case_id") for r in originals.values()} == set(catalog), "frame_kind_population")
    _require(len({r.get("task_id") for r in originals.values()}) == 3,
             "three_underlying_task_contexts_required")
    seeds = [r.get("seed") for r in by_id.values()]
    _require(all(type(s) is int and 0 <= s < 2**32 for s in seeds)
             and len(set(seeds)) == 108, "frame_seed_identity")
    inputs = {}
    for original in originals.values():
        case_id = original["case_id"]
        case, key = catalog[case_id], facts[case_id]
        _require(original.get("parent_assignment_id") is None
                 and original.get("task_id") == case.get("task_id") == key.get("task_id")
                 and type(original.get("scorer_case_index")) is int
                 and original["scorer_case_index"] == list(catalog).index(case_id)
                 and case.get("confirmation_eligible") is False
                 and case.get("split") == "exposed_constructed_development", "original_case_join")
        path = case.get("input_path")
        actor_input = member(path)
        inputs[case_id] = actor_input
        _require(_same_pin(original.get("input_pin"), members[path])
                 and case.get("input_bytes") == members[path]["bytes"]
                 and case.get("input_file_sha256") == members[path]["sha256"], "actor_input_pin_join")
        messages = actor_input.get("messages") if type(actor_input) is dict else None
        _require(type(messages) is list and len(messages) == 2
                 and all(type(m) is dict and set(m) == {"role", "content"}
                         and type(m["content"]) is str for m in messages)
                 and [m["role"] for m in messages] == ["system", "user"]
                 and original.get("messages") == messages, "actor_input_messages")
        prefix_hash = _sha(_wire(messages))
        _require(prefix_hash == original.get("initial_prefix_sha256")
                 == case.get("initial_prefix_sha256") == key.get("initial_prefix_sha256"),
                 "initial_prefix_join")
        _require(_sha(_wire(actor_input)) == key.get("actor_input_sha256"),
                 "canonical_actor_input_join")
        _require(type(key.get("evidence")) is list
                 and type(key.get("planned_current_interaction_facts")) is dict
                 and type(key.get("represented_scenario_facts")) is dict, "fact_key_shape")
        children = [r for r in asks if r.get("parent_assignment_id") == original["assignment_id"]]
        _require(len(children) == 8 and {r.get("question_type") for r in children} == set(TYPES),
                 "eight_distinct_ASK_types_required")
        for ask in children:
            _require(ask.get("case_id") == case_id and type(ask.get("question")) is str
                     and _sha(ask["question"].encode("utf-8")) == ask.get("question_sha256"),
                     "ASK_frame_join")
    _require(all(r.get("parent_assignment_id") in originals for r in asks), "unknown_ASK_parent")
    return frame, by_id, catalog, facts, current, dictionary, inputs, members


def join_anchor_saved_outcomes(*, frame_pin, terminal_pin, packet_manifest_pin,
                              source_pins, read_exact):
    """Join exact saved anchor bytes, retaining the fixed 108-assignment population.

    Pins have bytes/sha256 and one path or name locator. The resolver returns
    bytes; this adapter independently checks every supplied/read pin. Invalid
    source tables, duplicate/foreign terminal IDs or inconsistent terminal row
    identity fail with AnchorOutcomeError: no unambiguous population can be
    reported. Missing terminal rows remain missing outcomes, never invented
    unstarted calls. Missing/corrupt per-row evidence is retained diagnostically.

    The result is analyst-only and contains authored fact keys. It is NOT a
    reader prompt or input. Exact raw bodies remain at returned evidence pins;
    original outcome/error records and score records are retained without repair.
    Native completion is attributed to the collector's record, not requalified.
    """
    try:
        frame, by_id, catalog, facts, current, dictionary, inputs, members = _sources(
            frame_pin, packet_manifest_pin, source_pins, read_exact)
    except (TypeError, KeyError, AttributeError, IndexError) as exc:
        raise AnchorOutcomeError("invalid_source_shape:" + type(exc).__name__) from exc
    terminal = None if terminal_pin is None else _json(_read(read_exact, terminal_pin))
    supplied = {}
    if terminal_pin is not None:
        _require(type(terminal) is dict and terminal.get("frame_sha256") == _sha(_wire(frame))
                 and type(terminal.get("rows")) is list, "terminal_frame_or_shape")
        for raw in terminal["rows"]:
            _require(type(raw) is dict and type(raw.get("assignment_id")) is str
                     and raw["assignment_id"] in by_id and raw["assignment_id"] not in supplied,
                     "duplicate_or_foreign_terminal_assignment")
            assignment = by_id[raw["assignment_id"]]
            _require(all(raw.get(k) == assignment.get(k) for k in ("case_id", "kind", "parent_assignment_id"))
                     and type(raw.get("status")) is str, "terminal_assignment_identity")
            supplied[raw["assignment_id"]] = raw
    rows, joined = [], {}
    # Process originals before ASK, while preserving the frozen output order.
    ordered = sorted(by_id.values(), key=lambda a: a["kind"] != "original")
    for assignment in ordered:
        ident, kind, case_id = (assignment[k] for k in ("assignment_id", "kind", "case_id"))
        raw = supplied.get(ident)
        row = {"assignment_id": ident, "case_id": case_id, "kind": kind,
               "parent_assignment_id": assignment.get("parent_assignment_id"),
               "question_type": assignment.get("question_type"),
               "observation_scope": "original_end" if kind == "original" else "ASK_answer_with_inherited_original_context",
               "task_id": catalog[case_id]["task_id"],
               "source_outcome": deepcopy(raw),
               "source_status": None if raw is None else raw["status"],
               "outcome_presence": "missing_terminal_assignment" if raw is None else "present",
               "evidence": {}, "diagnostics": [], "client_request_join": "unavailable",
               "observed_message_join": "unavailable", "observed_native_message": None,
               "observed_channels": {},
               "native_message_join": "unavailable", "original_prefix_join": "unavailable",
               "task_outcome": {"status": "not_applicable_ASK" if kind == "ask" else "unavailable",
                                "task_score": None, "score_record": None},
               "fact_delivery": [], "recognition_outcome": None}
        values = {}

        def diagnostic(stage, reason, pin=None):
            row["diagnostics"].append({"stage": stage, "reason": reason, "pin": deepcopy(pin)})

        def evidence(field, supplied_pin=None):
            pin = supplied_pin if supplied_pin is not None else (None if raw is None else raw.get(field))
            if pin is None:
                row["evidence"][field] = {"pin": None, "status": "missing"}
                return None
            entry = {"pin": deepcopy(pin), "status": "invalid"}
            row["evidence"][field] = entry
            try:
                body = _read(read_exact, pin)
                entry["status"] = "bytes_verified"
                value = _json(body)
                entry["status"] = "json_verified"
                if type(value) is not dict:
                    entry.update(status="invalid_json_record", invalid_record=deepcopy(value))
                    raise AnchorOutcomeError("expected_json_object")
                return value
            except AnchorOutcomeError as exc:
                diagnostic(field, str(exc), pin)
                return None

        for field in ("request", "raw_response", "observed_native_message", "native_message", "native_prefix"):
            values[field] = evidence(field)
        if kind == "original":
            values["artifact_score"] = evidence("artifact_score")
        elif raw is not None and raw.get("artifact_score") is not None:
            diagnostic("artifact_score", "ASK_cannot_supply_original_task_score", raw["artifact_score"])

        expected_messages = assignment.get("messages")
        parent = joined.get(assignment.get("parent_assignment_id"))
        if kind == "ask":
            expected_messages = (parent["prefix"]["messages"] + [{"role": "user", "content": assignment["question"]}]
                                 if parent and parent["prefix"] is not None else None)
        request = values["request"]
        if request is not None:
            expected_wire = (None if expected_messages is None
                             else _wire(_request(expected_messages, assignment["seed"])))
            if (expected_wire is not None and raw["request"]["sha256"] == _sha(expected_wire)
                    and raw["request"]["bytes"] == len(expected_wire)):
                row["client_request_join"] = "exact_saved_client_request"
            else:
                row["client_request_join"] = "invalid"
                diagnostic("request", "request_does_not_match_own_frame_or_parent", raw.get("request"))

        response, message = values["raw_response"], values["native_message"]
        observed = values["observed_native_message"]
        row["observed_native_message"] = deepcopy(observed)
        if response is not None and values["observed_native_message"] is not None:
            try:
                _require(_wire(_saved_message(response)) == _wire(values["observed_native_message"]),
                         "observed_message_response_mismatch")
                row["observed_message_join"] = "exact_saved_observed_message"
            except (AnchorOutcomeError, KeyError, TypeError, IndexError) as exc:
                row["observed_message_join"] = "invalid"
                diagnostic("observed_native_message", str(exc), raw.get("observed_native_message"))
        channel_pins = {} if raw is None else raw.get("channel_values", {})
        if type(channel_pins) is not dict:
            diagnostic("channel_values", "invalid_channel_pin_map")
        else:
            for field, channel_pin in channel_pins.items():
                record = evidence("channel_values." + field, channel_pin)
                channel = {"record": deepcopy(record), "join": "unavailable"}
                row["observed_channels"][field] = channel
                if record is None:
                    continue
                try:
                    _require(field in {"content", "reasoning", "reasoning_content"}
                             and row["observed_message_join"] == "exact_saved_observed_message"
                             and type(observed) is dict and type(record) is dict,
                             "channel_without_joined_observed_message")
                    expected = {"field": field, "present": field in observed,
                                "json_type": type(observed[field]).__name__ if field in observed else "absent",
                                "source_message": raw["observed_native_message"],
                                "source_response": raw["raw_response"]}
                    if field in observed:
                        expected["value"] = observed[field]
                    _require(_wire(record) == _wire(expected), "channel_observed_source_or_value_mismatch")
                    channel["join"] = "exact_saved_observed_channel"
                except (AnchorOutcomeError, KeyError, TypeError) as exc:
                    channel["join"] = "invalid"
                    diagnostic("channel_values." + field, str(exc), channel_pin)
        if message is not None:
            try:
                _require(raw["status"] == "native_completed", "message_without_native_completed_stage")
                _require(response is not None and _wire(_saved_message(response)) == _wire(message),
                         "native_message_response_mismatch")
                _require(row["client_request_join"] == "exact_saved_client_request", "native_request_not_joined")
                row["native_message_join"] = "exact_saved_native_message"
            except AnchorOutcomeError as exc:
                row["native_message_join"] = "invalid"
                diagnostic("native_message", str(exc), raw.get("native_message"))

        prefix = values["native_prefix"]
        admitted_prefix = None
        if prefix is not None:
            try:
                if kind == "original":
                    _require(row["native_message_join"] == "exact_saved_native_message",
                             "prefix_without_joined_original_native_message")
                    _require(type(prefix) is dict
                             and prefix.get("cutoff") == "original_end_before_any_interview"
                             and prefix.get("assignment_id") == ident and prefix.get("case_id") == case_id
                             and _wire(prefix.get("actor")) == _wire(ACTOR)
                             and _wire(prefix.get("messages")) == _wire(assignment["messages"] + [message])
                             and prefix.get("messages_sha256") == _sha(_wire(prefix["messages"]))
                             and _same_pin(prefix.get("original_request"), raw["request"])
                             and _same_pin(prefix.get("original_return"), raw["raw_response"]),
                             "original_prefix_identity_or_content")
                    admitted_prefix = prefix
                else:
                    _require(parent is not None and parent["prefix"] is not None
                             and _same_pin(raw["native_prefix"], parent["prefix_pin"])
                             and _wire(prefix) == _wire(parent["prefix"]), "ASK_prefix_not_own_original")
                row["original_prefix_join"] = "exact_saved_original_prefix"
            except (AnchorOutcomeError, KeyError, TypeError) as exc:
                row["original_prefix_join"] = "invalid"
                diagnostic("native_prefix", str(exc), raw.get("native_prefix"))

        score = values.get("artifact_score")
        if score is not None:
            row["task_outcome"]["score_record"] = deepcopy(score)
            try:
                _require(row["native_message_join"] == "exact_saved_native_message", "score_without_joined_native_message")
                content = message.get("content")
                if type(score) is dict and score.get("status") == "unavailable":
                    _require(score.get("task_score") is None and type(score.get("reason")) is str,
                             "scorer_unavailable_record")
                    row["task_outcome"]["status"] = "saved_scorer_unavailable_not_content_joined"
                    raise _UnavailableScore
                if type(score) is dict and score.get("status") == "missing_return":
                    _require(score.get("case_id") == case_id and content is None
                             and all(score.get(k) is None for k in ("raw_bytes", "raw_sha256", "raw_hex", "task_score")),
                             "missing_score_content_mismatch")
                    row["task_outcome"]["status"] = "saved_missing_public_content_not_content_joined"
                    raise _UnavailableScore
                _require(type(score) is dict and score.get("case_id") == case_id
                         and type(content) is str and type(score.get("raw_bytes")) is int
                         and score["raw_bytes"] == len(content.encode("utf-8"))
                         and score.get("raw_sha256") == _sha(content.encode("utf-8"))
                         and score.get("raw_hex") == content.encode("utf-8").hex(), "score_original_content_join")
                _require(score.get("status") in {"received", "invalid_return"}, "score_status")
                if score["status"] == "received":
                    _require(type(score.get("task_score")) is dict, "received_task_score_shape")
                    spans = score.get("artifact_spans")
                    _require(type(spans) is dict and type(spans.get("artifact_codepoints")) is list
                             and len(spans["artifact_codepoints"]) == 2
                             and type(spans.get("artifact_utf8_bytes")) is list
                             and len(spans["artifact_utf8_bytes"]) == 2, "score_artifact_spans")
                    a, b = spans["artifact_codepoints"]
                    c, d = spans["artifact_utf8_bytes"]
                    _span(content, {"start_codepoint": a, "end_codepoint": b,
                                    "start_utf8_byte": c, "end_utf8_byte": d,
                                    "quote": score.get("artifact_text")})
                    _require(score.get("artifact_sha256") == _sha(score["artifact_text"].encode("utf-8")),
                             "score_artifact_hash")
                else:
                    _require(score.get("task_score") is None, "unavailable_score_has_outcome")
                row["task_outcome"].update(status="exact_saved_score_join",
                                            task_score=deepcopy(score.get("task_score")))
            except _UnavailableScore:
                pass
            except (AnchorOutcomeError, TypeError, KeyError) as exc:
                row["task_outcome"]["status"] = "invalid_saved_score_evidence"
                diagnostic("artifact_score", str(exc), raw.get("artifact_score"))

        # Fact truth and authored access states are retained separately, unchanged.
        key = facts[case_id]
        for fact_evidence in key["evidence"]:
            delivery = {"source_evidence": deepcopy(fact_evidence),
                        "span_origin": "original_actor_input_inherited_by_ASK" if kind == "ask" else "original_actor_input",
                        "client_request_span": "unavailable", "server_render_verified": None,
                        "actor_access_verified": None, "fact_truth_verified": None}
            try:
                _require(type(fact_evidence) is dict
                         and fact_evidence.get("source") == catalog[case_id]["input_path"]
                         and fact_evidence.get("source_sha256") == catalog[case_id]["input_file_sha256"],
                         "fact_source_join")
                index = fact_evidence.get("message_index")
                source_messages = inputs[case_id]["messages"]
                _require(type(index) is int and 0 <= index < len(source_messages)
                         and fact_evidence.get("role") == source_messages[index]["role"]
                         and fact_evidence.get("channel") == "public_text", "fact_message_join")
                _span(source_messages[index]["content"], fact_evidence)
                if row["client_request_join"] == "exact_saved_client_request":
                    _span(request["messages"][index]["content"], fact_evidence)
                    delivery.update(client_request_span="present_in_exact_saved_client_request",
                                    request_pin=deepcopy(raw["request"]), message_index=index,
                                    message_sha256=_sha(_wire(request["messages"][index])),
                                    quote_utf8_sha256=_sha(fact_evidence["quote"].encode("utf-8")))
            except (AnchorOutcomeError, TypeError, KeyError, IndexError) as exc:
                delivery["client_request_span"] = "invalid_evidence"
                delivery["error"] = str(exc)
                diagnostic("fact_delivery", str(exc))
            row["fact_delivery"].append(delivery)
        joined[ident] = {"row": row, "prefix": admitted_prefix,
                         "prefix_pin": None if raw is None else raw.get("native_prefix")}
    rows = [joined[ident]["row"] for ident in by_id]
    original_rows = [r for r in rows if r["kind"] == "original"]
    return {
        "schema": SCHEMA, "scope": "exposed_constructed_anchor_development_saved_data_only",
        "analyst_only_not_reader_input": True,
        "dependencies": {"frame": deepcopy(frame_pin), "terminal": deepcopy(terminal_pin),
                         "packet_manifest": deepcopy(packet_manifest_pin), "sources": deepcopy(source_pins),
                         "case_catalog": deepcopy(members["researcher/case_catalog.json"]),
                         "case_fact_keys": deepcopy(members["researcher/case_fact_keys.json"])},
        "source_terminal": deepcopy(terminal), "case_catalog": deepcopy(catalog),
        "authored_case_fact_keys": deepcopy(facts), "authored_current_research_facts": deepcopy(current),
        "authored_evidence_dictionary": deepcopy(dictionary),
        "rows": rows,
        "counts": {"assigned_originals": 12, "assigned_ASK": 96, "assigned_total": 108,
                   "underlying_task_contexts": 3, "independent_clusters": None,
                   "missing_terminal_assignments": sum(r["source_outcome"] is None for r in rows),
                   "source_statuses": dict(Counter(r["source_status"] for r in rows if r["source_status"] is not None)),
                   "recorded_generation_invoked": sum(r["source_outcome"] is not None
                                                       and r["source_outcome"].get("generation_invoked") is True for r in rows),
                   "recorded_count_invoked": sum(r["source_outcome"] is not None
                                                  and r["source_outcome"].get("count_invoked") is True for r in rows),
                   "recorded_generation_reserved": sum(r["source_outcome"] is not None
                                                        and r["source_outcome"].get("generation_reserved") is True for r in rows),
                   "recorded_count_reserved": sum(r["source_outcome"] is not None
                                                   and r["source_outcome"].get("count_reserved") is True for r in rows),
                   "joined_native_messages": sum(r["native_message_join"] == "exact_saved_native_message" for r in rows),
                   "joined_original_prefixes": sum(r["original_prefix_join"] == "exact_saved_original_prefix" for r in original_rows),
                   "joined_score_records": sum(r["task_outcome"]["status"] == "exact_saved_score_join" for r in original_rows),
                   "joined_task_score_objects": sum(r["task_outcome"]["task_score"] is not None for r in original_rows),
                   "recorded_task_correct_true": sum((r["task_outcome"]["task_score"] or {}).get("correct") is True
                                                     for r in original_rows),
                   "recorded_task_correct_false": sum((r["task_outcome"]["task_score"] or {}).get("correct") is False
                                                      for r in original_rows),
                   "rows_with_diagnostics": sum(bool(r["diagnostics"]) for r in rows)},
        "recognition_scores": None, "latent_awareness": None, "Study2_qualified_parents": None,
        "physical_or_render_qualification": None, "source_adoption_verified_here": False,
        "recorded_native_stage_verified_here": False,
    }
