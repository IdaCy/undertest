"""Preserve historical annotations and reconcile supplied P1 evidence.

This module performs source and supplied-data checks. It does not annotate text,
establish reader independence, or qualify historical records for a main frame.
"""

from __future__ import annotations

import copy
import builtins
import datetime as dt
import hashlib
import importlib.util
import json
import types
from fractions import Fraction
from pathlib import Path


TYPE_MAP = {
    "evaluation_status": "evaluation_status",
    "evaluation_purpose": "evaluation_purpose",
    "grader_reward": "grader_reward",
    "monitoring_oversight": "monitoring",
    "simulation_status": "simulation",
    "consequences_stakes": "stakes",
    "training_lifecycle": "training_lifecycle",
    "audience_demand": None,
}
COMPARABILITY = (
    "same_type", "same_channel", "same_actor", "same_referent",
    "same_target_time", "same_conditions", "same_aggregation_domain",
)
MAPPING_FIELDS = {
    "interpretation_kind": {"actor_assertion", "quotation", "hypothetical", "reported_prior_thought", "analyst_inference"},
    "attributed_actor": None,
    "referent": {"acting_model", "task_subject", "represented_scenario", "other", "unknown"},
    "target_time": {"current_episode_at_original_cutoff", "historical_episode", "future_hypothetical", "unknown"},
    "polarity": {"affirmative", "denial"},
    "certainty": {"asserted", "uncertain"},
}
MAX_FILE_BYTES = 8 * 1024**2
MAX_PACKET_BYTES = 32 * 1024**2
TARGET_SOURCE_SHA256 = "ea87aec10672799ce7b013a33a0236b6f6b2d1ddd4950355c54b8b19a5b99dc6"
TARGET_DEPENDENCIES_SHA256 = "8a2977eeea680b280119eae847c5b4de04e674811dced9cbf6a5a42428df8cef"


class SourceError(ValueError):
    """A supplied source cannot be used; callers must retain its assignment."""


def require(condition, reason):
    if not condition:
        raise SourceError(reason)


def sha(body):
    return hashlib.sha256(body).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def strict_json(body):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key: " + key)
            result[key] = value
        return result

    def invalid(value):
        raise SourceError("nonfinite JSON number: " + value)

    return json.loads(body, object_pairs_hook=pairs, parse_constant=invalid)


def unique(rows, key, label):
    result = {}
    require(type(rows) is list and len(rows) <= 20000, "unbounded " + label)
    for row in rows:
        ident = key(row)
        require(ident not in result, "duplicate " + label)
        result[ident] = row
    return result


def read_pinned(root, relative, pin):
    path = Path(relative)
    require(not path.is_absolute() and ".." not in path.parts, "nonlocal source path")
    current = root
    for part in path.parts:
        current = current / part
        require(not current.is_symlink(), "symlink source path")
    require(type(pin["bytes"]) is int and 0 <= pin["bytes"] <= MAX_FILE_BYTES, "source byte bound")
    with current.open("rb") as stream:
        body = stream.read(pin["bytes"] + 1)
    require(len(body) == pin["bytes"] and sha(body) == pin["sha256"], "source byte/hash mismatch: " + relative)
    return body


def verified_p1(location):
    """Load the accepted source and dependency roster, not caller functions."""
    source = Path(location).resolve()
    root = source.parents[1]
    body = read_pinned(root, "source/assignment_join.py", {"bytes": 31307, "sha256": TARGET_SOURCE_SHA256})
    roster = read_pinned(root, "dependency_pins.json", {"bytes": 3814, "sha256": TARGET_DEPENDENCIES_SHA256})
    for pin in strict_json(roster)["files"]:
        read_pinned(root, pin["copy"], pin)
    require(source == root / "source/assignment_join.py", "unexpected P1 source location")
    # Keep the accepted source unchanged while supplying its one file import
    # from verified bytes. A normal SourceFileLoader can consume an unlisted
    # timestamp-valid .pyc even when the on-disk .py was just authenticated.
    helper_path = root / "dependencies/registry/build_packet.py"
    helper_pin = next(pin for pin in strict_json(roster)["files"] if pin["copy"] == "dependencies/registry/build_packet.py")
    helper_body = read_pinned(root, helper_pin["copy"], helper_pin)

    class VerifiedHelperLoader:
        def create_module(self, spec):
            return None

        def exec_module(self, helper):
            helper.__file__ = str(helper_path)
            exec(compile(helper_body, str(helper_path), "exec"), helper.__dict__)

    def helper_spec(name, filename):
        require(name == "pinned_registry_arithmetic" and Path(filename) == helper_path, "unexpected P1 file import")
        return importlib.util.spec_from_loader(name, VerifiedHelperLoader(), origin=str(helper_path))

    import_proxy = types.SimpleNamespace(util=types.SimpleNamespace(
        spec_from_file_location=helper_spec,
        module_from_spec=importlib.util.module_from_spec,
    ))

    def source_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "importlib.util" and level == 0:
            return import_proxy
        return builtins.__import__(name, globals, locals, fromlist, level)

    module = types.ModuleType("verified_assignment_p1")
    module.__file__ = str(source)
    module.__dict__["__builtins__"] = {**vars(builtins), "__import__": source_import}
    exec(compile(body, str(source), "exec"), module.__dict__)
    return module


def jsonl(body):
    rows, locations = [], []
    for line_number, line in enumerate(body.splitlines(), 1):
        if line.strip():
            rows.append(strict_json(line))
            locations.append({"jsonl_line_1based": line_number, "record_bytes": len(line), "record_sha256": sha(line)})
    return rows, locations


def _key(row, manifest_hash):
    return {
        "manifest_sha256": manifest_hash, "item_id": row["item_id"],
        "source_type": row["source_type"], "frame": row["raw_selected_cell"]["frame"],
        "view": row["proposed_view_name"],
    }


def _empty_sidecar(row, manifest_hash, source_pins):
    unavailable = row["source_item_identity"]["availability"] in {"partial", "unavailable"} or row["raw_summary"]["state"] == "unavailable"
    return {
        "source_key": _key(row, manifest_hash),
        "source_record": None,
        "source_statuses": {
            "availability": row["source_item_identity"]["availability"],
            "reading_status": row["raw_reading_status"],
            "cell_state": row["raw_summary"]["state"],
            "unresolved_alternatives": copy.deepcopy(row["raw_unresolved_alternatives"]),
        },
        "raw_summary": copy.deepcopy(row["raw_summary"]),
        "raw_propositions": copy.deepcopy(row["raw_components_in_type_frame"]),
        "target_binding": None,
        "summary_rule": {
            "schema_sha256": source_pins.get("source_schema", {}).get("sha256"),
            "rubric_sha256": source_pins.get("source_rubric", {}).get("sha256"),
            "aggregation_rule_id": None,
            "declared_scope": None,
        },
        "scope_comparability": dict.fromkeys(COMPARABILITY),
        "mapping_decisions": [], "reference_admission": None,
        "mechanical_candidate": {"value": None, "source_output_pin": None, "reason": "no_translated_reference_envelope_and_no_P1_execution"},
        "qa": {
            "complete": False, "record_conflicts": [],
            "unmapped_fields": ["target_binding", "reference_admission", "complete_view_review", "semantic_mappings"] + (["source_view_unavailable_or_partial"] if unavailable else []),
            "disposition": "unavailable" if unavailable else "coding_unresolved", "value": None, "reconciliation_record_id": None,
        },
        "assignment_weight": None, "reconciliation_ledger": [], "analysis_cutoff": None,
        "main_frame_eligible": False,
    }


def ingest_history(packet_root, *, expected_seal_sha256):
    """Import one frozen worklist; retain all its rows when other inputs fail.

    A missing or corrupt assignment worklist raises SourceError: its population
    cannot be reconstructed safely. Other import failures are returned in the
    assignment ledger, with every known assigned view retained as unavailable.
    """
    root = Path(packet_root)
    with (root / "seal_manifest.json").open("rb") as stream:
        seal_body = stream.read(MAX_FILE_BYTES + 1)
    require(len(seal_body) <= MAX_FILE_BYTES and sha(seal_body) == expected_seal_sha256, "untrusted seal")
    seal = strict_json(seal_body)
    members = unique(seal["files"], lambda x: x["path"], "sealed member")
    require(sum(x["bytes"] for x in members.values()) <= MAX_PACKET_BYTES, "packet byte bound")
    work_body = read_pinned(root, "historical_raw_worklist.json", members["historical_raw_worklist.json"])
    work = strict_json(work_body)
    rows = work["records"]
    unique(rows, lambda x: (x["item_id"], x["source_type"], x["raw_selected_cell"]["frame"], x["proposed_view_name"]), "assigned view")
    require(len(rows) == 32 and work["type_mapping"] == TYPE_MAP, "frozen historical population changed")
    for row in rows:
        require(row["source_type"] in TYPE_MAP and row["target_awareness_type"] == TYPE_MAP[row["source_type"]], "type alias drift")
        require(row["proposed_view_name"] in {"E_public", "R_ASK"}, "invalid view")
        require(all(row[name] is None for name in ("target_binding", "mechanical_p1_candidate", "empirical_endpoint", "assigned_main_frame_weight")), "historical preparation contains unsupplied outcomes")
        require(row["main_frame_eligibility"] is False and row["raw_human_reader"] is False and row["proposition_mapping_decisions"] == [], "historical provenance drift")
    failures, bodies, pins = [], {}, {}
    manifest_body = None
    try:
        manifest_body = read_pinned(root, "source_pins.json", members["source_pins.json"])
        manifest = strict_json(manifest_body)
        pins = unique(manifest["files"], lambda x: x["key"], "source pin key")
        unique(manifest["files"], lambda x: x["copy"], "source pin copy")
        for pin in pins.values():
            require(pin["copy"] in members, "unsealed source")
            require(all(pin[k] == members[pin["copy"]][k] for k in ("bytes", "sha256")), "manifest/seal disagreement")
            bodies[pin["key"]] = read_pinned(root, pin["copy"], pin)
        contract = strict_json(read_pinned(root, "mapping_contract.json", members["mapping_contract.json"]))
        require(contract["type_map"] == TYPE_MAP, "mapping type policy changed")
    except (SourceError, OSError, KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
        failures.append({"stage": "source_integrity", "reason": str(exc)})
    manifest_hash = sha(manifest_body) if manifest_body is not None else members.get("source_pins.json", {}).get("sha256")
    sidecars = [_empty_sidecar(row, manifest_hash, pins) for row in rows]
    ledger, raw_provenance = [], []
    indexes, locations = {}, {}
    if not failures:
        try:
            for name, key in (
                ("joined_items", lambda x: x["item_id"]),
                ("joined_cells", lambda x: (x["item_id"], x["type"], x["frame"])),
                ("joined_propositions", lambda x: x["proposition_id"]),
                ("adjudicated_annotations", lambda x: x["item_id"]),
                ("joined_pairs", lambda x: (x["parent_id"], x["type"], x["frame"], x["channel"])),
            ):
                records, lines = jsonl(bodies[name])
                indexes[name] = unique(records, key, name)
                locations[name] = {key(record): line for record, line in zip(records, lines)}
            require(sha(bodies["adjudicated_annotations"]) == strict_json(bodies["adjudication_manifest"])["adjudicated_annotations_sha256"], "adjudication source disagreement")
        except (SourceError, KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
            failures.append({"stage": "source_index", "reason": str(exc)})
    for raw, sidecar in zip(rows, sidecars):
        errors = copy.deepcopy(failures)
        auxiliary = {"source_key": sidecar["source_key"], "raw_worklist_record": copy.deepcopy(raw), "literal_spans": [], "reference_provenance": None}
        if not errors:
            try:
                _join_row(raw, sidecar, auxiliary, indexes, locations, bodies, pins)
            except (SourceError, KeyError, TypeError, IndexError, json.JSONDecodeError, UnicodeError) as exc:
                errors.append({"stage": "row_source_join", "reason": str(exc)})
        if errors:
            sidecar["qa"].update(disposition="unavailable", unmapped_fields=["source_import"], value=None)
        ledger.append({"source_key": sidecar["source_key"], "status": "unavailable" if errors else "source_joined_view_unavailable" if sidecar["qa"]["disposition"] == "unavailable" else "source_joined_mapping_unresolved", "errors": errors})
        raw_provenance.append(auxiliary)
    return {
        "schema": "sigres-p1-ingestion-sidecars-v1", "analysis_scope": "historical_exposed_DEV_no_main_frame",
        "seal_sha256": expected_seal_sha256, "worklist_sha256": sha(work_body), "source_manifest_sha256": manifest_hash,
        "source_packet": str(root), "source_pins": list(pins.values()),
        "records": sidecars, "raw_provenance": raw_provenance, "assignment_ledger": ledger,
        "counts": {"assigned_views": len(rows), "source_pairs": len({x["source_pair_index"] for x in rows}), "physical_items": len({x["item_id"] for x in rows}), "primary_views": sum(TYPE_MAP[x["source_type"]] is not None for x in rows), "audience_views": sum(TYPE_MAP[x["source_type"]] is None for x in rows), "literal_spans_verified": sum(len(x["literal_spans"]) for x in raw_provenance)},
        "P1_executions": 0, "new_labels": 0, "new_human_readings": 0, "runtime_authority": False,
        "empirical_main_estimate": None,
    }


def _join_row(raw, sidecar, auxiliary, tables, locations, bodies, pins):
    iid, typ = raw["item_id"], raw["source_type"]
    frame = raw["raw_selected_cell"]["frame"]
    item = tables["joined_items"][iid]
    ann = tables["adjudicated_annotations"][iid]
    cell = tables["joined_cells"][(iid, typ, frame)]
    pair = tables["joined_pairs"][(raw["parent_id"], typ, frame, "public_text")]
    view = "original" if raw["proposed_view_name"] == "E_public" else "ask"
    require(pair[view + "_item_id"] == iid and pair[view + "_expression"] == raw["raw_summary"], "pair/item/summary join")
    require(cell == raw["raw_selected_cell"] and ann["type_summaries"][typ][frame] == raw["raw_summary"] == item["type_summaries"][typ][frame], "raw summary changed")
    require(ann["reading_status"] == item["reading_status"] == raw["raw_reading_status"], "reading status changed")
    require(ann["human_reader"] is raw["raw_human_reader"] and ann["reader_id"] == raw["raw_reader_id"] and ann["unresolved_alternatives"] == raw["raw_unresolved_alternatives"], "reader provenance changed")
    ident = raw["source_item_identity"]
    require(item["source_item"] == ident["source_item"] and item["parent_id"] == raw["parent_id"], "source identity changed")
    source = strict_json(bodies["source_item:" + iid])
    require(source["item_id"] == iid and source["designated_channel"] == item["channel"] == "public_text", "nonpublic or foreign source")
    require(source["eligible_message_indices"] == ident["eligible_message_indices"] and source["cutoff_exclusive_message_index"] == ident["cutoff_exclusive_message_index"], "source cutoff changed")
    require(source["availability"] == ident["availability"] == item["availability"], "source availability changed")
    texts = unique(source["target_texts"], lambda x: x["message_index"], "source message")
    for index, target in texts.items():
        require(type(index) is int and index in source["eligible_message_indices"] and 0 <= index < source["cutoff_exclusive_message_index"], "source message cutoff")
        native = source["native_messages_at_cutoff"][index]
        require(native["role"] == "assistant" and native.get("content") == target["text"], "public source is not literal assistant content")
    selected = [x for x in tables["joined_propositions"].values() if (x["item_id"], x["type"], x["frame"]) == (iid, typ, frame)]
    require(selected == raw["raw_components_in_type_frame"], "component subset changed")
    props = unique(ann["propositions"], lambda x: x["proposition_id"], "annotation proposition")
    require(set(raw["raw_summary"]["proposition_ids"]) <= {x["proposition_id"] for x in selected}, "foreign summary proposition")
    for prop in selected:
        require(all(prop[k] == v for k, v in props[prop["proposition_id"]].items()), "annotation component changed")
        ev = prop["evidence"]
        require(ev["channel"] == "public_text" and ev["message_index"] in texts, "foreign literal source")
        text = texts[ev["message_index"]]["text"]
        a, b, u, v = (ev[k] for k in ("start_codepoint", "end_codepoint", "start_utf8_byte", "end_utf8_byte"))
        require(all(type(x) is int for x in (a, b, u, v)) and 0 <= a < b <= len(text) and 0 <= u < v <= len(text.encode()), "invalid literal offsets")
        require(text[a:b] == ev["quote"] and text.encode()[u:v] == ev["quote"].encode(), "literal span mismatch")
        require(len(text[:a].encode()) == u and len(text[:b].encode()) == v, "codepoint/byte offset disagreement")
        auxiliary["literal_spans"].append({"proposition_id": prop["proposition_id"], "source_text_sha256": sha(text.encode()), "literal_sha256": sha(ev["quote"].encode()), "evidence": copy.deepcopy(ev)})
    sidecar["source_record"] = {"file_pin_key": "adjudicated_annotations", **locations["adjudicated_annotations"][iid], "json_pointer": "/type_summaries/" + typ + "/" + frame}
    auxiliary["source_annotation"] = copy.deepcopy(ann)
    auxiliary["source_item_pin"] = copy.deepcopy(pins["source_item:" + iid])
    auxiliary["source_original_item_pin"] = copy.deepcopy(pins["source_item:" + pair["original_item_id"]])
    auxiliary["reference_provenance"] = strict_json(bodies["adjudication_manifest"])


def json_pointer(value, pointer):
    require(type(pointer) is str and (not pointer or pointer.startswith("/")), "invalid JSON pointer")
    if not pointer:
        return value
    for token in pointer[1:].split("/"):
        require("~" not in token.replace("~0", "").replace("~1", ""), "invalid pointer escape")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            require(token.isdecimal() and (token == "0" or not token.startswith("0")), "invalid list pointer")
            value = value[int(token)]
        else:
            require(isinstance(value, dict), "pointer traverses scalar")
            value = value[token]
    return value


def _evidence_documents(artifacts):
    require(type(artifacts) is dict and len(artifacts) <= 20000, "bounded decision artifacts")
    require(all(type(body) is str for body in artifacts.values()), "decision artifacts must preserve UTF-8 text")
    require(sum(len(body.encode()) for body in artifacts.values()) <= MAX_PACKET_BYTES, "decision evidence byte bound")
    return {sha(body.encode()): body for body in artifacts.values()}


def _refs(refs, documents, literal_ids, *, required=False):
    require(type(refs) is list and (refs or not required), "missing decision evidence")
    for ref in refs:
        require(set(ref) == {"artifact_sha256", "json_pointer", "literal_span_ref"}, "source reference fields")
        body = documents[ref["artifact_sha256"]]
        json_pointer(strict_json(body), ref["json_pointer"])
        require(ref["literal_span_ref"] is None or ref["literal_span_ref"] in literal_ids, "foreign literal evidence reference")


def _provenance_pin(pin, documents):
    require(type(pin) is dict and type(pin.get("bytes")) is int, "decision record pin required")
    body = documents[pin["sha256"]]
    require(len(body.encode()) == pin["bytes"], "decision record byte mismatch")
    return strict_json(body)


def _mapping_check(sidecar, auxiliary, supplied, projection, reference, documents, inputs):
    literal_ids = {x["proposition_id"] for x in auxiliary["literal_spans"]}
    source_props = {p["proposition_id"]: p for p in sidecar["raw_propositions"]}
    target_props = unique(reference["propositions"], lambda x: x["proposition_id"], "target proposition")
    require(set(target_props) <= set(source_props), "target proposition has no raw source")
    spans = {span["span_id"]: span for span in inputs["corpus"]["evidence_spans"]}
    channels = {row["source_message_index"]: row["channel_item_id"] for row in supplied["target_binding"]["native_message_to_corpus_event_map"]}
    for pid, proposition in target_props.items():
        span, source = spans[proposition["span_id"]], source_props[pid]["evidence"]
        require(span["channel_item_id"] == channels[source["message_index"]], "proposition mapped to a different source message")
        require(span["start_unicode"] == source["start_codepoint"] and span["end_unicode"] == source["end_codepoint"] and span["literal_text"] == source["quote"] and span["literal_sha256"] == sha(source["quote"].encode()), "proposition literal source changed")
    decisions = unique(supplied["mapping_decisions"], lambda x: x["decision_id"], "mapping decision")
    mapped = {}
    required = {"decision_id", "source_record_sha256", "source_json_pointer", "source_proposition_id", "target_field", "target_value", "author_id", "decision_version", "supporting_source_refs", "counterevidence_source_refs", "unresolved_alternatives"}
    for decision in decisions.values():
        require(set(decision) == required, "mapping decision fields")
        require(all(type(decision[k]) is str and decision[k] for k in ("decision_id", "author_id", "decision_version")), "mapping author/version required")
        require(decision["source_record_sha256"] == sidecar["source_record"]["record_sha256"], "mapping source record drift")
        prop = json_pointer(auxiliary["source_annotation"], decision["source_json_pointer"])
        pid, field, value = decision["source_proposition_id"], decision["target_field"], decision["target_value"]
        require(type(prop) is dict and prop.get("proposition_id") == pid and pid in source_props, "mapping pointer/proposition mismatch")
        require(field in MAPPING_FIELDS and (pid, field) not in mapped, "duplicate/foreign target field")
        require(type(decision["unresolved_alternatives"]) is list, "mapping alternatives required")
        _refs(decision["supporting_source_refs"], documents, literal_ids, required=True)
        _refs(decision["counterevidence_source_refs"], documents, literal_ids)
        if value is not None:
            require(type(value) is str and value, "unknown mapping must be null")
            require(MAPPING_FIELDS[field] is None or value in MAPPING_FIELDS[field], "unrecognized target enum")
            require(pid in target_props and target_props[pid][field] == value, "supplied reference differs from mapping")
        mapped[pid, field] = value if not decision["unresolved_alternatives"] else None
    unresolved = [pid + ":" + field for pid in source_props for field in MAPPING_FIELDS if mapped.get((pid, field)) is None]
    established = []
    for pid in projection.get("qualifying_assertion_ids", []):
        require(pid in target_props, "foreign qualifying assertion")
        if all(mapped.get((pid, field)) is not None for field in MAPPING_FIELDS):
            established.append(pid)
    return unresolved, established


def _binding_check(sidecar, auxiliary, supplied, projection, p1, inputs, mechanical_row, documents):
    binding = supplied["target_binding"]
    fields = {"assignment_id", "actor_model_id", "config_sha256", "case_id", "original_trajectory_id", "view_trajectory_id", "original_cutoff_id", "view_cutoff_id", "signal_assignment_id", "native_prefix_sha256", "native_prefix_messages_pointer", "original_native_prefix_messages_pointer", "native_message_to_corpus_event_map", "channel_item_ids", "complete_view_review_decision", "ASK_isolation_record"}
    require(type(binding) is dict and set(binding) == fields, "complete target binding required")
    tables = {name: {row[key]: row for row in inputs["corpus"].get(name, [])} for name, key in p1.TABLE_KEYS.items()}
    aid = binding["assignment_id"]
    require(aid == mechanical_row["assignment_id"], "target assignment drift")
    assignment = tables["assignments"][aid]
    model = tables["model_configs"][assignment["actor_model_id"]]
    require(binding["actor_model_id"] == assignment["actor_model_id"] and binding["config_sha256"] == model["config_sha256"] and binding["case_id"] == assignment["case_id"], "actual actor/config/case mismatch")
    links = projection["source_links"]
    for target, key in (("view_trajectory_id", "trajectory_id"), ("view_cutoff_id", "cutoff_id"), ("original_cutoff_id", "original_cutoff_id"), ("signal_assignment_id", "signal_assignment_id")):
        require(binding[target] == links[key], "target endpoint mismatch: " + target)
    original_cutoff = tables["cutoffs"][binding["original_cutoff_id"]]
    require(binding["original_trajectory_id"] == original_cutoff["trajectory_id"], "original trajectory mismatch")
    original = tables["trajectories"][binding["original_trajectory_id"]]
    raw = auxiliary["raw_worklist_record"]
    require(original["trajectory_file_sha256"] == raw["source_item_identity"]["source_trajectory"]["sha256"], "raw original trajectory mismatch")
    cutoff = tables["cutoffs"][binding["view_cutoff_id"]]
    require(binding["native_prefix_sha256"] == cutoff["native_prefix_sha256"], "native prefix mismatch")
    maps = unique(binding["native_message_to_corpus_event_map"], lambda x: x["source_message_index"], "native message mapping")
    require(set(maps) == set(raw["source_item_identity"]["eligible_message_indices"]), "native message coverage mismatch")
    require(len({x["channel_item_id"] for x in maps.values()}) == len(maps), "reused channel mapping")
    require(sorted(binding["channel_item_ids"]) == links["channel_item_ids"] == sorted(x["channel_item_id"] for x in maps.values()), "public channel coverage mismatch")
    source_item = _provenance_pin(auxiliary["source_item_pin"], documents)
    original_item = _provenance_pin(auxiliary["source_original_item_pin"], documents)
    for actual_cutoff, reader, pointer in (
        (cutoff, source_item, binding["native_prefix_messages_pointer"]),
        (original_cutoff, original_item, binding["original_native_prefix_messages_pointer"]),
    ):
        prefix = p1.strict_json(p1.source_artifact(inputs["artifacts"], actual_cutoff["native_prefix_artifact"], actual_cutoff["native_prefix_sha256"]))
        messages = json_pointer(prefix, pointer)
        require(type(messages) is list and messages == reader["native_messages_at_cutoff"], "native prefix differs from pinned reader history")
        require(len(messages) == reader["cutoff_exclusive_message_index"], "reader native cutoff mismatch")
    require(source_item["native_messages_at_cutoff"][:original_item["cutoff_exclusive_message_index"]] == original_item["native_messages_at_cutoff"], "ASK native prefix differs from original reader history")
    texts = {x["message_index"]: x["text"] for x in source_item["target_texts"]}
    for index, mapped in maps.items():
        channel = tables["channel_items"][mapped["channel_item_id"]]
        event = mapped["corpus_event_index"]
        require(type(index) is int and type(event) is int and 0 <= event < cutoff["event_count"] and event == channel["event_index"], "mapped event outside cutoff")
        require(channel["trajectory_id"] == binding["view_trajectory_id"] and channel["channel"] == "public_assistant" and channel["origin"] == "actor_generated", "nonactor public source")
        text = inputs["artifacts"][channel["raw_artifact"]]
        require(text == texts[index] and sha(text.encode()) == mapped["source_text_sha256"], "native/corpus text mismatch")
    complete = binding["complete_view_review_decision"]
    require(set(complete) == {"author_id", "decision_version", "supporting_source_refs", "result"} and type(complete["result"]) is bool, "explicit complete-view decision required")
    require(all(type(complete[k]) is str and complete[k] for k in ("author_id", "decision_version")), "complete-view author/version required")
    literals = {x["proposition_id"] for x in auxiliary["literal_spans"]}
    _refs(complete["supporting_source_refs"], documents, literals, required=True)
    view = sidecar["source_key"]["view"]
    target_binding = next(x for x in inputs["bindings"] if (x["assignment_id"], x["awareness_type"], x["view"]) == (aid, mechanical_row["awareness_type"], view))
    if view == "E_public":
        require(binding["ASK_isolation_record"] is None, "original view has ASK isolation")
    else:
        isolated = p1.strict_json(p1.source_artifact(inputs["artifacts"], target_binding["isolation"]["artifact"], target_binding["isolation"]["sha256"]))
        require(binding["ASK_isolation_record"] == isolated, "ASK isolation evidence mismatch")
    envelope = next(x for x in inputs["adjudications"] if x["adjudication_id"] == target_binding["adjudication_id"])
    reference = p1.strict_json(p1.source_artifact(inputs["artifacts"], envelope["artifact"], envelope["sha256"]))
    require(reference["reference_provenance"]["reader_id"] == auxiliary["source_annotation"]["reader_id"], "historical reader identity changed")
    require(reference["reference_provenance"].get("human_reader", False) is auxiliary["source_annotation"]["human_reader"], "human-reader provenance changed")
    admission = supplied["reference_admission"]
    require(set(admission) == {"decision_id", "provenance_record_pin", "permitted_analysis_scope"} and type(admission["decision_id"]) is str and admission["decision_id"], "reference admission required")
    record = _provenance_pin(admission["provenance_record_pin"], documents)
    require(record["decision_id"] == admission["decision_id"] and record["permitted_analysis_scope"] == admission["permitted_analysis_scope"] == inputs["frame"]["analysis_scope"], "reference admission scope mismatch")
    require(record["reference_sha256"] == envelope["sha256"] and record["source_key"] == sidecar["source_key"], "reference admission source mismatch")
    return reference, complete["result"]


def reconcile_value(candidate, summary_state, comparability, *, exclusive_summary_rule, complete_reference, unresolved_fields, established_Q, unresolved_does_not_undermine_Q=False):
    """Finite supplied-evidence QA; this calculation is not reference admission."""
    require(candidate is None or type(candidate) is int and candidate in (0, 1), "candidate must be 0/1/null")
    require(summary_state in {"asserted", "denied", "uncertain", "mixed", "no_relevant_statement", "unavailable", "coding_unresolved"}, "unknown raw summary state")
    require(type(established_Q) is bool and not (candidate == 0 and established_Q), "candidate/Q disagreement")
    require(set(comparability) == set(COMPARABILITY) and all(x is None or type(x) is bool for x in comparability.values()), "explicit scope comparability required")
    require(type(complete_reference) is bool and type(exclusive_summary_rule) is bool and type(unresolved_does_not_undermine_Q) is bool, "explicit QA conditions required")
    same_scope = all(value is True for value in comparability.values())
    conflicts = []
    if same_scope and candidate == 1 and established_Q and summary_state == "no_relevant_statement":
        conflicts.append("same_scope_no_relevant_statement_with_Q")
    if same_scope and exclusive_summary_rule:
        if candidate == 1 and established_Q and summary_state in {"denied", "uncertain"}:
            conflicts.append("exclusive_summary_disagrees_with_Q")
        if candidate == 0 and summary_state == "asserted":
            conflicts.append("exclusive_asserted_summary_without_Q")
    value = candidate
    if (not complete_reference or not same_scope or summary_state in {"unavailable", "coding_unresolved"}
            or conflicts or candidate is None or candidate == 1 and not established_Q
            or unresolved_fields and not (candidate == 1 and established_Q and unresolved_does_not_undermine_Q)):
        value = None
    return {"disposition": "resolved" if value is not None else "coding_unresolved", "value": value, "record_conflicts": conflicts}


def _utc(value):
    require(type(value) is str, "analysis cutoff/timestamp required")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.utcoffset() == dt.timedelta(0), "timestamp must declare UTC")
    return parsed


def _reconciliation(sidecar, supplied, reference, documents, frame_hash):
    ledger = supplied["reconciliation_ledger"]
    require(type(ledger) is list and len(ledger) <= 1, "only one finite reconciliation pass")
    cutoff = _utc(supplied["analysis_cutoff"])
    state = sidecar["raw_summary"]["state"]
    pending = False
    if ledger:
        record = ledger[0]
        fields = {"record_id", "prior_record_hashes", "frozen_input_hashes", "initial_decision_refs", "adjudicator_initial_ref", "resolution", "changes", "rationale", "supporting_source_refs", "counterevidence_source_refs", "retained_alternatives", "author_id", "version", "timestamp_UTC"}
        require(set(record) == fields, "reconciliation record fields")
        require(all(type(record[k]) is str and record[k] for k in ("record_id", "author_id", "version", "rationale")), "reconciliation provenance required")
        require(record["resolution"] in {"resolved", "coding_unresolved"} and _utc(record["timestamp_UTC"]) <= cutoff, "reconciliation after cutoff/invalid resolution")
        require(sidecar["source_record"]["record_sha256"] in record["prior_record_hashes"], "missing initial raw decision")
        require({sidecar["source_key"]["manifest_sha256"], frame_hash} <= set(record["frozen_input_hashes"]), "reconciliation input drift")
        literal_ids = {x["proposition_id"] for x in sidecar["raw_propositions"]}
        for key in ("initial_decision_refs", "supporting_source_refs", "counterevidence_source_refs"):
            _refs(record[key], documents, literal_ids, required=key != "counterevidence_source_refs")
        _refs([record["adjudicator_initial_ref"]], documents, literal_ids, required=True)
        require(type(record["retained_alternatives"]) is list and type(record["changes"]) is list, "explicit reconciliation alternatives/changes")
        summary_changes = [x for x in record["changes"] if x.get("field") == "summary.state"]
        require(len(summary_changes) <= 1, "repeated summary correction")
        for change in record["changes"]:
            require(set(change) == {"field", "old", "new", "reason"} and type(change["reason"]) is str and change["reason"], "explicit old/new correction required")
        if summary_changes:
            change = summary_changes[0]
            require(change["old"] == state and change["new"] == reference["expression_state"], "summary correction source mismatch")
            state = change["new"]
        # Semantic reinterpretations need their own reviewed input envelope.
        # Keep them pending here instead of treating a resolution string as proof.
        pending = record["resolution"] != "resolved" or any(x["field"] != "summary.state" for x in record["changes"])
    require(state == reference["expression_state"], "unrecorded source summary replacement")
    return state, pending


def _supplied_view(sidecar, auxiliary, supplied, projection, p1, inputs, mechanical_row, documents, output_pin):
    fields = {"source_key", "target_binding", "summary_rule", "scope_comparability", "mapping_decisions", "reference_admission", "reconciliation_ledger", "analysis_cutoff", "positive_scope_record_pin"}
    require(set(supplied) == fields and supplied["source_key"] == sidecar["source_key"], "supplied view fields/source key")
    require(sidecar["qa"]["disposition"] != "unavailable", "failed raw import cannot supply a reference")
    reference, complete = _binding_check(sidecar, auxiliary, supplied, projection, p1, inputs, mechanical_row, documents)
    unresolved, established = _mapping_check(sidecar, auxiliary, supplied, projection, reference, documents, inputs)
    rule = supplied["summary_rule"]
    require(set(rule) == {"schema_sha256", "rubric_sha256", "aggregation_rule_id", "declared_scope"}, "summary rule fields")
    require(type(rule["aggregation_rule_id"]) is str and rule["aggregation_rule_id"], "declared aggregation rule required")
    require(rule["schema_sha256"] == sidecar["summary_rule"]["schema_sha256"] and rule["rubric_sha256"] == sidecar["summary_rule"]["rubric_sha256"], "summary rule source drift")
    scope = rule["declared_scope"]
    require(type(scope) is dict and set(scope) == {"provenance_record_pin", "exclusive_operational_summary"}, "source-backed summary scope required")
    scope_record = _provenance_pin(scope["provenance_record_pin"], documents)
    require(scope_record["source_key"] == sidecar["source_key"] and scope_record["aggregation_rule_id"] == rule["aggregation_rule_id"] and scope_record["scope_comparability"] == supplied["scope_comparability"] and scope_record["exclusive_operational_summary"] is scope["exclusive_operational_summary"], "summary scope decision mismatch")
    state, pending = _reconciliation(sidecar, supplied, reference, documents, inputs["expected_frame_sha256"])
    does_not_undermine = False
    if supplied["positive_scope_record_pin"] is not None:
        record = _provenance_pin(supplied["positive_scope_record_pin"], documents)
        require(record["source_key"] == sidecar["source_key"] and record["mechanical_output_sha256"] == output_pin["sha256"], "positive-scope source drift")
        require(record["established_assertion_ids"] == established and record["unresolved_fields"] == unresolved, "positive-scope candidates changed")
        require(type(record["unresolved_does_not_undermine_Q"]) is bool, "explicit positive-scope decision required")
        require(all(type(record[k]) is str and record[k] for k in ("author_id", "version")), "positive-scope author/version required")
        _refs(record["supporting_source_refs"], documents, {p["proposition_id"] for p in sidecar["raw_propositions"]}, required=True)
        does_not_undermine = record["unresolved_does_not_undermine_Q"]
    eligible = complete and all(projection["coverage"].get(k) is True for k in ("readable", "history_eligible", "annotation_valid", "reference_available"))
    # This adapter's historical packet cannot acquire main-frame membership.
    if inputs["frame"]["analysis_scope"] != "synthetic_fixture" and not sidecar["main_frame_eligible"]:
        eligible = False
        unresolved = unresolved + ["historical_main_frame_ineligible"]
    qa = reconcile_value(projection["value"], state, supplied["scope_comparability"], exclusive_summary_rule=scope["exclusive_operational_summary"], complete_reference=eligible and not pending, unresolved_fields=unresolved, established_Q=bool(established), unresolved_does_not_undermine_Q=does_not_undermine)
    result = copy.deepcopy(sidecar)
    for key in ("target_binding", "summary_rule", "scope_comparability", "mapping_decisions", "reference_admission", "reconciliation_ledger", "analysis_cutoff"):
        result[key] = copy.deepcopy(supplied[key])
    result["mechanical_candidate"] = {"value": projection["value"], "source_output_pin": output_pin, "reason": projection["reason"]}
    result["qa"] = {"complete": eligible, "unmapped_fields": unresolved, "reconciliation_record_id": supplied["reconciliation_ledger"][0]["record_id"] if supplied["reconciliation_ledger"] else None, **qa}
    return result


def reconcile_assigned_p1(p1, inputs, history, supplied_views, *, decision_artifacts):
    """Run unchanged V3 checks, then retain all frozen mass through separate QA.

    The historical baseline has no inputs for this operation. A caller must
    separately supply a real frozen frame (or explicitly synthetic fixture),
    native bindings, reference envelopes and source-backed mapping decisions.
    Failed or omitted mappings leave their assigned endpoints null.
    """
    p1 = verified_p1(p1.__file__)
    source_before = canonical(history)
    require(history == ingest_history(history["source_packet"], expected_seal_sha256=history["seal_sha256"]), "historical sidecar/source drift")
    require(set(inputs) == {"frame", "corpus", "bindings", "adjudications", "artifacts", "expected_frame_sha256"}, "P1 input fields")
    mechanical = p1.analyze(**inputs)
    body = canonical(mechanical)
    output_pin = {"bytes": len(body), "sha256": sha(body), "encoding": "canonical JSON UTF-8"}
    raw = {canonical(x["source_key"]): x for x in history["raw_provenance"]}
    base = {canonical(x["source_key"]): x for x in history["records"]}
    batch_errors = []
    try:
        documents = _evidence_documents(decision_artifacts)
        supplied = unique(supplied_views, lambda x: canonical(x["source_key"]), "supplied source view")
        require(set(supplied) <= set(base), "foreign supplied source view")
    except (SourceError, KeyError, TypeError, ValueError) as exc:
        # The raw population and frozen P1 input have already been verified.
        # An invalid/ambiguous supplied batch cannot erase their denominator
        # or select a preferred survivor; retain all endpoints unavailable.
        documents, supplied = {}, {}
        batch_errors.append({"stage": "supplied_batch", "reason": str(exc)})
    projected = {(x["assignment_id"], x["awareness_type"]): x for x in mechanical["assignment_view"]}
    selected, records, import_ledger = {}, [], []
    target_keys = set()
    for source_key, initial in base.items():
        result = copy.deepcopy(initial)
        errors, target = copy.deepcopy(batch_errors), None
        if errors:
            result["qa"].update(disposition="unavailable", value=None, complete=False)
        value = supplied.get(source_key)
        if value is not None:
            try:
                typ = TYPE_MAP[initial["source_key"]["source_type"]]
                require(typ is not None, "audience view remains secondary")
                binding = value["target_binding"]
                require(type(binding) is dict, "target binding must be an object")
                require(type(binding["assignment_id"]) is str and binding["assignment_id"], "target assignment ID must be a nonempty string")
                target = (binding["assignment_id"], typ, initial["source_key"]["view"])
                require(target not in target_keys, "target slot reused by multiple raw views")
                target_keys.add(target)
                row = projected[target[:2]]
                projection = row[target[2]]
                result["mechanical_candidate"] = {"value": projection["value"], "source_output_pin": output_pin, "reason": projection["reason"]}
                result = _supplied_view(result, raw[source_key], value, projection, p1, inputs, row, documents, output_pin)
                result["assignment_weight"] = {"assignment_id": target[0], "frame_sha256": inputs["expected_frame_sha256"], "numerator": row["weight"]["numerator"], "denominator": row["weight"]["denominator"]}
                selected[target] = result
            except (SourceError, KeyError, TypeError, IndexError, StopIteration, ValueError) as exc:
                errors.append({"stage": "supplied_reference_reconciliation", "reason": str(exc)})
                result["qa"].update(disposition="unavailable", value=None, complete=False)
                # A duplicate poisons the target rather than choosing a survivor.
                if target in selected:
                    selected[target]["qa"].update(disposition="unavailable", value=None, complete=False)
                elif target is not None and target[:2] in projected:
                    selected[target] = result
        records.append(result)
        import_ledger.append({"source_key": initial["source_key"], "target_key": list(target) if target else None, "errors": errors, "status": result["qa"]["disposition"]})
    # Duplicate target discovery can invalidate an earlier record. Derive all
    # ledger statuses after selection, and retain an error on every collider.
    collisions = {}
    for entry in import_ledger:
        if entry["target_key"] is not None:
            collisions.setdefault(tuple(entry["target_key"]), []).append(entry)
    for result, entry in zip(records, import_ledger):
        target = entry["target_key"]
        if target is not None and len(collisions[tuple(target)]) > 1:
            result["qa"].update(disposition="unavailable", value=None, complete=False)
            entry["errors"].append({"stage": "supplied_reference_reconciliation", "reason": "target slot reused by multiple raw views"})
        entry["status"] = result["qa"]["disposition"]
    rows = []
    for original in mechanical["assignment_view"]:
        row = {k: copy.deepcopy(v) for k, v in original.items() if k not in ("E_public", "R_ASK", "P1_bounds")}
        for view in p1.VIEWS:
            sidecar = selected.get((row["assignment_id"], row["awareness_type"], view))
            row[view] = {"value": None if sidecar is None else sidecar["qa"]["value"], "disposition": "unavailable" if sidecar is None else sidecar["qa"]["disposition"], "source_key": None if sidecar is None else sidecar["source_key"], "mechanical_candidate": copy.deepcopy(original[view])}
        row["P1_bounds"] = p1.ARITHMETIC.paired_bounds(row["E_public"]["value"], row["R_ASK"]["value"])
        rows.append(row)
    tables = {}
    for typ in p1.TYPES:
        assigned = [row for row in rows if row["awareness_type"] == typ]
        fractions = [Fraction(x["weight"]["numerator"], x["weight"]["denominator"]) for x in assigned]
        require(sum(fractions) == 1, "reconciled assignment mass changed")
        bounds = p1.ARITHMETIC.weighted_bounds([{"weight": row["weight"]["value"], "E": row["E_public"]["value"], "R": row["R_ASK"]["value"]} for row in assigned])
        exact = [sum((weight * row["P1_bounds"][i] for weight, row in zip(fractions, assigned)), Fraction()) for i in (0, 1)]
        tables["P1_" + typ] = {"assigned_rows": len(assigned), "assigned_mass": p1.fraction_record(sum(fractions)), "logical_bounds": bounds, "exact_logical_bounds": [p1.fraction_record(x) for x in exact], "rows": assigned}
    require(source_before == canonical(history), "raw source was modified")
    return {"schema": "sigres-p1-reconciled-assigned-v1", "scope": inputs["frame"]["analysis_scope"], "mechanical_output": mechanical, "mechanical_output_pin": output_pin, "records": records, "assignment_ledger": import_ledger, "assignment_view": rows, "p1_tables": tables, "frame_sha256": inputs["expected_frame_sha256"], "P1_executions": 1, "empirical_main_estimate": None, "runtime_authority": False, "new_labels": 0, "new_human_readings": 0}
