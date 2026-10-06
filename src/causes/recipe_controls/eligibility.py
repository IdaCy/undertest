import copy
import hashlib
import io
import json
import os
import stat
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath


DERIVATION_VERSION = "effects-family-exception-candidate-v3"
CONDITION_FIELDS = ("assignment_id", "instance_id", "family", "framing", "grading",
                    "rollout", "prompt_variant", "condition", "recipe_version", "rubric_version")
ASSIGNMENT_FIELDS = ("assignment_id", "instance_id", "framing", "grading", "rollout", "prompt_variant")
RUN_FIELDS = ("run_id", "config_id", "model_revision", "section", "run_type")
FAMILY_ABSENCE = [{"source": "assignments.json", "fields": ["family"]}]
INDEX_ISSUES = ["assignment_metadata_absent", "assignment_metadata_complete_false",
                "attempt_metadata_absent", "attempt_metadata_complete_false"]
REQUIRED_REFERENCES = ("manifest", "assignments", "inputs", "source_archive", "raw_eval", "csv",
                       "export_metadata", "export_identity", "export_messages", "canonical_messages",
                       "reconciliation", "evidence_index", "projection_owner", "source_contract",
                       "derivation_module", "derivation_driver", "protocol", "actual_owner_resolution")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("nonfinite JSON constant: " + value)

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def checked_bytes(path, expected=None, root=None):
    path = Path(path).absolute()
    if ".." in path.parts:
        raise ValueError("parent traversal in source path")
    if root is not None and not path.is_relative_to(Path(root).absolute()):
        raise ValueError("source outside declared root")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("symlink in source path")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("source is not a regular file")
        raw = stream.read()
        after = os.fstat(stream.fileno())
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, key) != getattr(after, key) for key in fields) or len(raw) != before.st_size:
        raise ValueError("source changed while reading")
    reference = {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if expected is not None and any(reference[key] != expected.get(key) for key in reference):
        raise ValueError("source byte identity mismatch")
    return reference, raw


def verify_source_archive(raw, expected_hashes):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(expected_hashes):
            raise ValueError("source archive member inventory mismatch")
        result = {}
        for name in names:
            path = PurePosixPath(name)
            member = archive.getinfo(name)
            if path.is_absolute() or ".." in path.parts or member.is_dir() or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("unsafe source archive member")
            content = archive.read(name)
            if hashlib.sha256(content).hexdigest() != expected_hashes[name]:
                raise ValueError("source archive member digest mismatch: " + name)
            result[name] = content
        return result


def durable_inventory(entries):
    attempts = {}
    for entry in entries:
        raw = entry["raw"]
        ref = entry["reference"]
        if len(raw) != ref["bytes"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError("durable reference mismatch")
        records = [strict_json(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
        if not records or any(type(record) is not dict for record in records):
            raise ValueError("unreadable durable records")
        starts = [record for record in records if record.get("phase") == "start"]
        if len(starts) != 1 or records[0] != starts[0]:
            raise ValueError("rejected durable header")
        start = starts[0]
        aid, attempt = start.get("assignment_id"), start.get("attempt_id")
        if not all(type(value) is str and value for value in (aid, attempt)):
            raise ValueError("missing durable identity")
        try:
            stamp = datetime.fromisoformat(start["started_at_utc"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("missing durable start time") from exc
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("durable timestamp lacks timezone")
        if stamp.utcoffset().total_seconds() != 0:
            raise ValueError("durable UTC timestamp has nonzero offset")
        if any(record.get("phase") not in ("start", "request", "result", "cleanup") for record in records):
            raise ValueError("unrecognized durable record phase")
        if attempt in {value["attempt_id"] for values in attempts.values() for value in values}:
            raise ValueError("duplicate durable attempt identity")
        attempts.setdefault(aid, []).append({"attempt_id": attempt, "started_at": start["started_at_utc"],
                                            "timestamp": stamp, "records": records, "reference": ref})
    for values in attempts.values():
        if len({value["timestamp"] for value in values}) != len(values):
            raise ValueError("unknown first durable attempt order")
        values.sort(key=lambda value: value["timestamp"])
    return attempts


def unavailable_population(population, reason, references):
    identities = [row["assignment_id"] for row in population]
    if not identities or any(type(identity) is not str or not identity for identity in identities) or len(set(identities)) != len(identities):
        raise ValueError("unavailable population needs fixed unique assignment identities")
    return [{"version": DERIVATION_VERSION, "assignment_id": identity, "schedule_ordinal": index,
             "qualification_disposition": "unavailable", "treatment_linkage_eligible": False,
             "review_resolution": None, "native_flags_rewritten": False,
             "refusal_reasons": [reason], "references": copy.deepcopy(references)}
            for index, identity in enumerate(identities)]


def _same(left, right):
    return type(left) is type(right) and (all(key in right and _same(value, right[key]) for key, value in left.items())
            and len(left) == len(right) if type(left) is dict else
            all(_same(a, b) for a, b in zip(left, right)) and len(left) == len(right) if type(left) is list else left == right)


def _recorded_surfaces(evidence, fields, require):
    expected = evidence["expected_metadata"]
    primary = evidence["durable_attempts"][0]
    attempt_id = primary["attempt_id"]
    ledger_path = evidence["original_run_path"] + "/ledgers/" + expected["assignment_id"] + "-" + attempt_id + ".jsonl"
    identity = {**expected, "attempt_id": attempt_id, "ledger_path": ledger_path}
    served, provider = evidence["served_model"], evidence["expected_run"]["model"]

    def structured(value, label):
        if type(value) is not dict:
            require(False, label + ":structured_object")
            return
        fields(value, identity, (), label)
        for key in ("metadata", "identity", "sample", "state", "start", "assessment", "result"):
            if key in value and value[key] is not None:
                structured(value[key], label + ":" + key)

    def message(value, label):
        fields(value, identity, (), label)
        if value.get("model") is not None:
            require(_same(value["model"], served), label + ":served_model")

    def output(value, label, model=served):
        fields(value, {"model": model}, ("model",), label)
        for index, choice in enumerate(value.get("choices", [])):
            message(choice["message"], f"{label}:choice:{index}")

    def store(value, label):
        fields(value, {**identity, "effects_instance_id": expected["instance_id"],
                      "effects_attempt_id": attempt_id, "effects_ledger_path": ledger_path}, (), label)
        if value.get("effects_assessment") is not None:
            structured(value["effects_assessment"], label + ":assessment")
        artifacts = value.get("artifacts", {})
        if "effects/identity.json" in artifacts:
            saved = strict_json(artifacts["effects/identity.json"])
            fields(saved, identity, ("assignment_id", "attempt_id"), label + ":serialized_identity")

    sample = evidence["sample"]
    initialized = [event for event in sample["events"] if event["event"] == "sample_init"]
    require(len(initialized) == 1, "recorded_surface:exactly_one_sample_init")
    for index, event in enumerate(initialized):
        label = f"recorded_surface:init:{index}"
        for side in ("sample", "state"):
            fields(event[side]["metadata"], expected, CONDITION_FIELDS, label + ":" + side + ":metadata")
            structured(event[side], label + ":" + side)
        fields(event["sample"], {"id": expected["assignment_id"]}, ("id", "input"), label + ":sample_identity")
        require(_same(event["sample"]["input"], evidence["input_pair"]["user"]), label + ":sample_input")
        output(event["state"]["output"], label + ":initial_output", provider)
        require(event["state"]["output"].get("choices") == [], label + ":initial_output_has_no_generation")
        store(event["state"].get("store", {}), label + ":initial_store")
        for position, item in enumerate(event["state"].get("messages", [])):
            message(item, f"{label}:initial_message:{position}")
    model_index = 0
    current_state_model = provider
    for event_index, event in enumerate(sample["events"]):
        label = f"recorded_surface:event:{event_index}"
        if type(event.get("metadata")) is dict:
            structured(event["metadata"], label + ":event_metadata")
        if event["event"] == "model":
            call = event["call"]
            fields(call["request"], {"model": served}, ("model",), label + ":call_request")
            require(_same(call["request"], evidence["model_requests"][model_index]), label + ":raw_request_copy_agrees")
            output(call["response"], label + ":call_response")
            for position, item in enumerate(event["input"]):
                message(item, f"{label}:model_input:{position}")
            model_index += 1
        elif event["event"] in ("state", "store"):
            for change_index, change in enumerate(event["changes"]):
                location = change["path"]
                parts = [part.replace("~1", "/").replace("~0", "~") for part in location.split("/")[1:]]
                here = f"{label}:change:{change_index}:{location}"
                value = change.get("value")
                replaced = change.get("replaced")
                if event["event"] == "state" and parts == ["output", "model"]:
                    if replaced is not None:
                        require(_same(replaced, current_state_model), here + ":historical_model")
                    require(change["op"] in ("add", "replace") and _same(value, served), here + ":new_served_model")
                    current_state_model = served
                elif event["event"] == "state" and parts[:1] == ["output"]:
                    for version, payload in (("value", value), ("replaced", replaced)):
                        if type(payload) is dict:
                            if len(parts) == 1:
                                output(payload, here + ":" + version, current_state_model if version == "replaced" else served)
                            elif parts[1:2] == ["choices"] and len(parts) == 3:
                                message(payload["message"], here + ":" + version + ":choice_message")
                            elif parts[1:2] == ["choices"] and parts[-1:] == ["message"]:
                                message(payload, here + ":" + version + ":message")
                    if parts[-2:] == ["message", "model"]:
                        require(value is None or _same(value, served), here + ":message_model")
                elif event["event"] == "state" and parts[:1] == ["messages"]:
                    for version, payload in (("value", value), ("replaced", replaced)):
                        if len(parts) == 2 and type(payload) is dict:
                            message(payload, here + ":" + version + ":message")
                        elif parts[-1:] == ["model"] and payload is not None:
                            require(_same(payload, served), here + ":" + version + ":message_model")
                elif event["event"] == "state" and parts[:1] == ["metadata"]:
                    for version, payload in (("value", value), ("replaced", replaced)):
                        if len(parts) == 1 and type(payload) is dict:
                            structured(payload, here + ":" + version + ":metadata")
                        elif len(parts) == 2 and parts[1] in identity and payload is not None:
                            require(_same(payload, identity[parts[1]]), here + ":" + version + ":condition")
                else:
                    store_parts = parts[1:] if event["event"] == "state" and parts[:1] == ["store"] else parts if event["event"] == "store" else []
                    for version, payload in (("value", value), ("replaced", replaced)):
                        if not store_parts or payload is None:
                            continue
                        field = store_parts[0]
                        if field in ("effects_instance_id", "effects_attempt_id", "effects_ledger_path") and len(store_parts) == 1:
                            store({field: payload}, here + ":" + version)
                        elif field == "effects_assessment" and len(store_parts) == 1:
                            structured(payload, here + ":" + version + ":assessment")
                        elif field == "artifacts" and len(store_parts) == 1:
                            store({"artifacts": payload}, here + ":" + version + ":artifacts")
                        elif store_parts == ["artifacts", "effects/identity.json"]:
                            store({"artifacts": {"effects/identity.json": payload}}, here + ":" + version + ":identity")
    for index, record in enumerate(primary["records"]):
        if "model_output" in record:
            output(record["model_output"], f"recorded_surface:durable:{index}:model_output")
    for index, native in enumerate(evidence["reconciled"]["attempts"]):
        for position, source in enumerate(native.get("metadata_sources", [])):
            require(source.get("present") is True, f"recorded_surface:reconciled:{index}:metadata_source:{position}:present")
            structured(source["value"], f"recorded_surface:reconciled:{index}:metadata_source:{position}")
        if native.get("replayed_observed_submission") is not None:
            structured(native["replayed_observed_submission"], f"recorded_surface:reconciled:{index}:replayed_submission")
        for position, csv_row in enumerate(native.get("samples", [])):
            fields(csv_row, {**identity, "sample_id": expected["assignment_id"]}, ("sample_id", "condition"), f"recorded_surface:reconciled:{index}:csv:{position}")
            fields(strict_json(csv_row["sample_metadata"]), expected, CONDITION_FIELDS, f"recorded_surface:reconciled:{index}:csv:{position}:metadata")


def derive_family_exception(evidence):
    reasons, checks = [], []

    def require(condition, code):
        checks.append(code)
        if condition is not True:
            reasons.append(code)

    def fields(actual, expected, required, label):
        if type(actual) is not dict:
            require(False, label + ":missing_object")
            return
        for key in required:
            require(key in actual, label + ":required:" + key)
        for key, value in actual.items():
            if key in expected:
                require(_same(value, expected[key]), label + ":agree:" + key)

    result = {"version": DERIVATION_VERSION, "assignment_id": evidence.get("assignment_id"),
              "qualification_disposition": "unavailable", "treatment_linkage_eligible": False,
              "review_resolution": None, "native_flags_rewritten": False,
              "required_acceptance": "Separate exact candidate manifest and three-perspective owner review resolution; no acceptance API in this derivation.",
              "references": copy.deepcopy(evidence.get("references", {})), "refusal_reasons": reasons}
    try:
        expected = evidence["expected_metadata"]
        require(type(expected) is dict and set(expected) == set(CONDITION_FIELDS), "expected_metadata_schema")
        require(type(expected.get("rollout")) is int and expected["rollout"] >= 0, "expected_rollout_type")
        require(all(type(expected.get(key)) is str and expected[key] for key in CONDITION_FIELDS if key != "rollout"), "expected_string_types")
        require(expected["assignment_id"] == evidence["assignment_id"], "expected_assignment_identity")
        require(expected["family"] == evidence["canonical_family"], "canonical_instance_family")
        require(expected["condition"] == expected["framing"] + "-" + expected["grading"], "derived_condition")
        assignment = evidence["assignment"]
        fields(assignment, expected, ASSIGNMENT_FIELDS, "assignment")
        require("family" not in assignment, "sole_exception_requires_absent_family")
        require(set(assignment) == set(ASSIGNMENT_FIELDS), "frozen_assignment_field_set")
        require(_same(assignment, evidence["reproduced_assignment"]), "reproduced_schedule_row")
        references = evidence["references"]
        for name in REQUIRED_REFERENCES:
            ref = references.get(name)
            require(type(ref) is dict and type(ref.get("path")) is str and Path(ref["path"]).is_absolute()
                    and type(ref.get("bytes")) is int and ref["bytes"] >= 0
                    and type(ref.get("sha256")) is str and len(ref["sha256"]) == 64
                    and all(char in "0123456789abcdef" for char in ref["sha256"]), "source_reference:" + name)
        require(evidence["global_issues"] == [], "global_source_and_inventory_checks")
        run = evidence["expected_run"]
        fields(evidence["manifest"], run, (*RUN_FIELDS, "model"), "manifest_run")
        require(evidence["manifest"]["schedule_sha256"] == evidence["reproduced_schedule_sha256"], "reproduced_schedule_digest")
        require(_same(evidence["input_pair"], evidence["rendered_input_pair"]), "original_rendered_inputs")
        require(digest(evidence["input_pair"]) == evidence["manifest"]["input_sha256"][expected["assignment_id"]], "saved_input_digest")
        sample, raw_eval, csv_row = evidence["sample"], evidence["raw_eval_identity"], evidence["csv_row"]
        def output_identity(output, label):
            fields(output, {"model": evidence["served_model"]}, ("model",), label)
            for index, choice in enumerate(output.get("choices", [])):
                message = choice["message"]
                if message.get("model") is not None:
                    require(message["model"] == evidence["served_model"], f"{label}:choice:{index}:message_model")

        output_identity(sample["output"], "raw_sample_output")
        for index, message in enumerate(sample["messages"]):
            if message.get("model") is not None:
                require(message["model"] == evidence["served_model"], f"raw_message:{index}:model")
        model_events = [event for event in sample["events"] if event["event"] == "model"]
        require(len(model_events) == len(evidence["model_requests"]), "raw_model_event_inventory")
        for index, event in enumerate(model_events):
            fields(event, {"model": run["model"]}, ("model", "output"), f"raw_model_event:{index}")
            output_identity(event["output"], f"raw_model_event:{index}:output")
        fields(raw_eval, {**run, **evidence["expected_inspect_identity"]},
               ("model", "inspect_run_id", "eval_id", "task_id", "eval_created"), "raw_eval_identity")
        for label, values in (("raw_eval_metadata", raw_eval["metadata"]), ("raw_log_metadata", raw_eval["log_metadata"]),
                              ("export_eval_metadata", evidence["export_metadata"]["eval"]),
                              ("export_log_metadata", evidence["export_metadata"]["log"]),
                              ("csv_eval_metadata", strict_json(csv_row["eval_metadata"])),
                              ("csv_log_metadata", strict_json(csv_row["log_metadata"]))):
            require(set(values) == {"undertest"}, label + ":wrapper")
            fields(values.get("undertest"), run, RUN_FIELDS, label)
            require(set(values.get("undertest", {})) == set(RUN_FIELDS), label + ":field_set")
        fields(csv_row, {**run, **evidence["expected_inspect_identity"], "sample_id": expected["assignment_id"],
                         "condition": expected["condition"], "epoch": str(sample["epoch"])},
               (*RUN_FIELDS, "model", "sample_id", "condition", "epoch", "inspect_run_id", "eval_id", "task_id", "eval_created"), "csv")
        require(sample["id"] == expected["assignment_id"] and type(sample["id"]) is str, "raw_sample_identity")
        require(type(sample.get("uuid")) is str and bool(sample["uuid"]), "raw_sample_uuid")
        require(type(sample["epoch"]) is int and sample["epoch"] == 1, "raw_sample_first_epoch")
        for label, values in (("raw_sample_metadata", sample["metadata"]),
                              ("csv_sample_metadata", strict_json(csv_row["sample_metadata"])),
                              ("export_sample_metadata", evidence["export_metadata"]["sample"])):
            fields(values, expected, CONDITION_FIELDS, label)
            require(set(values) == set(expected), label + ":field_set")
        require(sample["input"] == evidence["input_pair"]["user"], "raw_sample_original_user_input")
        messages = sample["messages"]
        for index, role in enumerate(("system", "user")):
            require(messages[index]["role"] == role and messages[index]["content"] == evidence["input_pair"][role], "raw_transcript_original_" + role)
        expected_messages = (json.dumps(messages, ensure_ascii=False, indent=2) + "\n").encode()
        for label in ("export_messages", "canonical_messages"):
            require(evidence[label] == expected_messages, label + ":matches_raw_transcript_bytes")
            require(references[label]["bytes"] == len(expected_messages)
                    and references[label]["sha256"] == hashlib.sha256(expected_messages).hexdigest(), label + ":reference_matches_bytes")
        require(type(evidence["model_requests"]) is list and bool(evidence["model_requests"]), "recorded_model_request_evidence")
        for index, request in enumerate(evidence["model_requests"]):
            require(request["model"] == evidence["served_model"], f"model_request:{index}:model")
            for position, role in enumerate(("system", "user")):
                message = request["messages"][position]
                require(message["role"] == role and message["content"] in (
                    evidence["input_pair"][role], [{"type": "text", "text": evidence["input_pair"][role]}]), f"model_request:{index}:original_{role}")
        attempts = evidence["durable_attempts"]
        require(len(attempts) == 1, "current_exception_release_single_durable_attempt")
        primary = attempts[0]
        attempt_id = primary["attempt_id"]
        records = primary["records"]
        require(type(records) is list and bool(records) and records[0].get("phase") == "start"
                and sum(record.get("phase") == "start" for record in records) == 1
                and all(record.get("phase") in ("start", "request", "result", "cleanup") for record in records), "durable_header_and_phase_structure")
        stamp = datetime.fromisoformat(primary["started_at"])
        require(stamp.tzinfo is not None and stamp.utcoffset() is not None and stamp.utcoffset().total_seconds() == 0
                and records[0]["started_at_utc"] == primary["started_at"], "durable_start_time_explicit_utc")
        ledger_path = evidence["original_run_path"] + "/ledgers/" + expected["assignment_id"] + "-" + attempt_id + ".jsonl"
        identity = {**expected, "attempt_id": attempt_id, "ledger_path": ledger_path}
        for label, value in (("manifest_present_fields", evidence["manifest"]), ("raw_sample_present_fields", sample),
                             ("export_metadata_present_fields", evidence["export_metadata"]),
                             ("raw_eval_present_fields", raw_eval), ("csv_present_fields", csv_row)):
            fields(value, {**identity, **run}, (), label)
        fields(primary["records"][0], identity, ("assignment_id", "attempt_id"), "durable_header")
        fields(primary["records"][0]["metadata"], identity, CONDITION_FIELDS, "durable_header_metadata")
        require(set(primary["records"][0]["metadata"]) == set(expected), "durable_header_metadata:field_set")
        require(Path(primary["reference"]["path"]).name == Path(ledger_path).name, "durable_filename_identity")
        for index, record in enumerate(primary["records"]):
            fields(record, identity, (), f"durable_record:{index}")
            for nested in ("metadata", "identity", "assessment"):
                if nested in record:
                    fields(record[nested], identity, (), f"durable_record:{index}:{nested}")
        store = sample["store"]
        fields(store, {**identity, "effects_instance_id": expected["instance_id"],
                       "effects_attempt_id": attempt_id, "effects_ledger_path": ledger_path},
               ("effects_instance_id", "effects_attempt_id", "effects_ledger_path", "artifacts"), "raw_store")
        if "effects_assessment" in store and store["effects_assessment"] is not None:
            fields(store["effects_assessment"], identity, (), "raw_store_assessment")
            if type(store["effects_assessment"]) is dict and "result" in store["effects_assessment"]:
                fields(store["effects_assessment"]["result"], identity, (), "raw_store_assessment_result")
        fields(evidence["export_identity"], identity, ("assignment_id", "attempt_id", "ledger_path"), "export_identity")
        require(set(evidence["export_identity"]) == {"assignment_id", "attempt_id", "ledger_path"}, "export_identity:field_set")
        stored_identity = strict_json(store["artifacts"]["effects/identity.json"])
        require(_same(stored_identity, evidence["export_identity"]), "raw_store_artifact_identity")
        require(csv_row["artifacts"] == evidence["original_run_path"] + "/artifacts/" + csv_row["sample_key"], "csv_artifact_path")
        require(csv_row["log"] == evidence["original_run_path"] + "/logs/" + Path(references["raw_eval"]["path"]).name, "csv_log_path")
        require(csv_row["artifact_export_errors"] == "[]" and csv_row["artifact_error"] == "", "csv_export_errors_absent")
        require(evidence["export_errors"] == [], "export_errors_absent")
        reconciled, indexed, projected = evidence["reconciled"], evidence["indexed"], evidence["projected_owner"]
        fields(reconciled, identity, ASSIGNMENT_FIELDS, "reconciliation")
        require(reconciled["primary_order_verified"] is True and reconciled["primary_attempt_id"] == attempt_id, "native_first_attempt_agrees")
        require(len(reconciled["attempts"]) == 1, "reconciled_single_attempt_inventory")
        native_attempt = reconciled["attempts"][0]
        require(native_attempt["attempt_id"] == attempt_id and native_attempt["started"] is True
                and native_attempt["started_at"] == primary["started_at"]
                and native_attempt["order_source"] == "started_at", "durable_first_order_reproduced")
        require(native_attempt["ledger_integrity"] is True and native_attempt["errors"] == [], "native_ledger_integrity")
        for label, value in (("reconciliation", reconciled), ("reconciled_attempt", native_attempt),
                             ("index_assignment", indexed["assignment_provenance"]),
                             ("index_attempt", indexed["primary_evidence"]["preserved_attempt_fields"])):
            require(value["metadata_integrity"] is True and value["metadata_complete"] is False
                    and value["metadata_conflicts"] == [] and value["metadata_absent"] == FAMILY_ABSENCE, label + ":only_family_absence")
        fields(indexed["condition_metadata"], expected, ASSIGNMENT_FIELDS, "index_conditions")
        require(indexed["assignment_id"] == expected["assignment_id"] and indexed["primary_attempt_id"] == attempt_id, "index_identity")
        require(indexed["condition_attribution_trusted"] is False and indexed["primary_evidence"]["condition_attribution_trusted"] is False, "original_index_flags_false")
        require(indexed["primary_evidence"]["verified_first_attempt"] is True and indexed["primary_evidence"]["attempt_id"] == attempt_id, "index_first_attempt")
        require(sorted(indexed["primary_evidence"]["attribution_issues"]) == INDEX_ISSUES, "only_enumerated_index_issues")
        require(indexed["unattributed_attempts"] == [], "no_nonprimary_index_attempts")
        require(projected["assignment_id"] == expected["assignment_id"] and projected["verified_attempt_id"] == attempt_id, "projection_identity")
        require(_same(projected["original_index_row"], indexed), "projected_original_index_exact")
        require(projected["native_linkage_eligible"] is False and projected["family_eligibility_override_applied"] is False, "original_projection_flags_false")
        require(projected["selection_issues"] == [], "canonical_selection_available")
        canonical_ref = projected["canonical_source"]
        require(canonical_ref["status"] == "readable" and all(canonical_ref[key] == references["canonical_messages"][key]
                    for key in ("path", "bytes", "sha256")), "canonical_selected_source_exact")
        require(len(indexed["primary_evidence"]["snapshots"]) == 1, "current_exception_release_single_snapshot")
        snapshot = indexed["primary_evidence"]["snapshots"][0]
        require(snapshot["transcript"]["status"] == "available" and snapshot["transcript"]["schema_issues"] == [], "no_transcript_schema_corruption")
        require(snapshot["sources"]["messages.json"] == canonical_ref, "indexed_canonical_source_exact")
        _recorded_surfaces(evidence, fields, require)
        result.update(annotation_id=projected["annotation_id"], attempt_id=attempt_id,
                      derived_conditions=copy.deepcopy(expected), sample_uuid=sample["uuid"],
                      first_attempt={"policy": "only archived durable attempt; timestamp required; no filesystem-time fallback", "started_at": primary["started_at"], "reference": copy.deepcopy(primary["reference"])},
                      canonical_transcript_sha256=hashlib.sha256(expected_messages).hexdigest(),
                      original_native_flags={"metadata_integrity": copy.deepcopy(reconciled.get("metadata_integrity")),
                                             "metadata_complete": copy.deepcopy(reconciled.get("metadata_complete")),
                                             "condition_attribution_trusted": copy.deepcopy(indexed.get("condition_attribution_trusted")),
                                             "native_linkage_eligible": copy.deepcopy(projected.get("native_linkage_eligible")),
                                             "family_eligibility_override_applied": copy.deepcopy(projected.get("family_eligibility_override_applied"))},
                      model_run_identity=copy.deepcopy(run), input_sha256=digest(evidence["input_pair"]))
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, UnicodeError) as exc:
        reasons.append("unavailable_required_evidence:" + type(exc).__name__ + ":" + str(exc))
    result["checks"] = checks
    result["refusal_reasons"] = sorted(set(reasons))
    if not reasons:
        result["qualification_disposition"] = "qualified_candidate_pending_independent_review"
    return result
