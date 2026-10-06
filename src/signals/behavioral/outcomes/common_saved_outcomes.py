"""Pure analyst-only joins for the exact common DEV producer's saved journal.

The caller supplies pinned bytes and independently accepted custody. This module
does not certify custody, issuer authority, original waits, native qualification
or release. Optional evidence is an explicit selection, never a complete scan.
No runtime producer, scorer, model or filesystem interface is invoked.
"""

from copy import deepcopy
import hashlib
import json
import re


VERSION = "sigres-common-saved-outcomes-v3"
PRODUCER_VERSION = "sigres-native-common-qwen-bridge-v1"
STATIC_SHA256 = "62976831b796956e92255e1a88295627748ea795f50170a67bafb46d6d5231d6"
COHORT_IDENTITY = (27672, "6b8ae2fcb19b5259255abfce3b19848b1f876d0a037681cee3b798d970fd7104")
ASSIGNMENT_IDENTITY = (82275, "8334b888ace38b5f86d21ffd75f92f8d6ab4944f148a87b474429b21caeed79d")
SOURCE_IDENTITIES = {
    "native_common": (22610, "4ea54e3e7ac51101e91095600111a279ec857124e7a7c4a22c8f3736319ea067"),
    "controller": (26718, "c7e0fb39a8ffb7fa0ef0a5c11fd25b0946b91118fbcc034192365dca82beed44"),
    "bridge": (24543, "9d4718f7b1e181201e73d5b540409328fa5489ba3b646fe7cbe240fe968f918a"),
    "runtime_entry": (17958, "d52e5985c842aeee27ff7c799d548459b1eba5ae524607d670ac7926c2c993ff"),
    "closure": (14112, "793158e11dd5c205524b93d1f9d33898c0a0684da5e2bc6cb848556a175e945f"),
    "task_capture": (28760, "4bf7a4d459599ed5b3ffe1159946c09ee50589ae1c41585c2c9e1dba7f27c786"),
    "artifact_wire": (20916, "5790f72f36cd022cb71aa713af5c810ab8ea843c8474f0a4e089f11d03c1df4e"),
    "checker_protocol": (19051, "3266bc81cf9a4a56778fe3ad3271d7684e06fdc00733b3f8df776b91c0176cdf"),
    "physical_runtime": (24070, "dda423e41751e9ed06e12d3ee45a6d0cb5fa381dd87f5447fa1f42a4749d2134"),
    "planning_native_history": (20867, "199d424bafa5b0fef81d10322001fe0da3e3de2763691c50a05ace23d75bf3be"),
}
_IDENTITY_FIELDS = ("assignment_id", "parent_assignment_id", "block", "actor_slot", "unit_id", "kind",
                    "generation_call_ceiling", "output_token_ceiling", "pair_order_index", "timing_pair_id",
                    "timing_contract_sha256")


class CommonSavedOutcomeError(ValueError):
    """Required pins or population/authority/terminal identities are ambiguous."""


def _need(condition, reason):
    if not condition:
        raise CommonSavedOutcomeError(reason)


def _wire(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _pin(pin):
    _need(type(pin) is dict and set(pin) == {"path", "bytes", "sha256"}
          and type(pin["path"]) is str and bool(pin["path"])
          and type(pin["bytes"]) is int and pin["bytes"] >= 0
          and type(pin["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", pin["sha256"]), "invalid_pin")
    return pin


def _read(pin, read_exact):
    _pin(pin)
    try:
        body = read_exact(deepcopy(pin))
    except Exception as exc:
        raise CommonSavedOutcomeError("saved_bytes_unavailable:" + type(exc).__name__) from exc
    _need(type(body) is bytes and len(body) == pin["bytes"] and _sha(body) == pin["sha256"], "saved_byte_pin_mismatch")
    return body


def _json(body):
    def pairs(items):
        value = {}
        for key, item in items:
            _need(key not in value, "duplicate_json_key")
            value[key] = item
        return value
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=pairs)
        _wire(value)  # Reject nonfinite values and escaped lone surrogates.
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise CommonSavedOutcomeError("invalid_saved_json:" + type(exc).__name__) from exc


def _relative(path):
    return (type(path) is str and bool(path) and not path.startswith("/")
            and "\\" not in path and all(p not in ("", ".", "..") for p in path.split("/")))


def _inventory(value):
    _need(type(value) is dict and set(value) == {"artifacts", "control"}, "original_inventory_shape")
    members = {}
    for tree, record in value.items():
        _need(type(record) is dict and type(record.get("files")) is list
              and type(record.get("directories")) is list and type(record.get("bytes")) is int,
              "inventory_tree_shape")
        total = 0
        for item in record["files"]:
            _pin(item)
            _need(_relative(item["path"]), "inventory_member_path")
            path = tree + "/" + item["path"]
            _need(path not in members, "duplicate_inventory_member")
            members[path] = {**item, "path": path}
            total += item["bytes"]
        _need(total == record["bytes"], "inventory_byte_total")
    return members


def _identity(row, assignments):
    _need(type(row) is dict and row.get("assignment_id") in assignments, "foreign_assignment")
    original = assignments[row["assignment_id"]]
    _need(all(_wire(row.get(k)) == _wire(original.get(k)) for k in _IDENTITY_FIELDS), "assignment_identity_drift")
    _need(type(row.get("status")) is str, "missing_recorded_status")
    return original


def _pair_identity(pair, pairs, runtime):
    _need(type(pair) is dict and pair.get("timing_pair_id") in pairs, "foreign_pair")
    expected = pairs[pair["timing_pair_id"]]
    _need(all(_wire(pair.get(k)) == _wire(expected[k]) for k in expected), "pair_identity_drift")
    _need(pair.get("operation_id") == runtime["operation_id"]
          and pair.get("clock_domain_id") == runtime["clock_domain_id"], "pair_operation_drift")


def _seed(assignment, index):
    return int.from_bytes(hashlib.sha256(_wire(["sigres-native-common-generation-seed-v1", assignment, index])).digest()[:8], "big") % (2**31)


def join_common_saved_outcomes(*, inventory_pin, binding_pin, authority_pin, assignment_record_pin,
                              cohort_pin, assignment_source_pin, source_pins, read_exact,
                              controller_pin=None, final_payload_pin=None, evidence_pins=()):
    """Join the fixed 48 source rows and separate issued Qwen 24-row scope.

    inventory_pin is the producer's original_inventory.json, not custody success.
    Captured pins use paths relative to that inventory's two trees, including
    the artifacts/ or control/ prefix. Required captured records must share a
    journal directory. Sources/cohort/assignment bytes may have other locators.
    evidence_pins explicitly selects optional numbered journal or qwenb records;
    omitted evidence remains unexamined, never inferred absent in the inventory.

    Required malformed evidence, foreign/duplicate terminal identities and a
    supplied null/nonobject terminal raise. Invalid optional journal joins are
    retained diagnostically with their exact input pins and original records.
    Competing optional records are unresolved, including identical duplicates:
    no single call/return/POST is selected from their argument order. Optional
    records are displayed by pin path; this ordering conveys no causal priority.
    A missing required assignment block cannot be synthesized after startup
    failure. read_exact returns whole files; this API does not stream large JSON.
    """
    try:
        return _join(inventory_pin, binding_pin, authority_pin, assignment_record_pin, cohort_pin,
                     assignment_source_pin, source_pins, read_exact, controller_pin, final_payload_pin, evidence_pins)
    except CommonSavedOutcomeError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, UnicodeError) as exc:
        raise CommonSavedOutcomeError("invalid_saved_shape:" + type(exc).__name__) from exc


def _join(inventory_pin, binding_pin, authority_pin, assignment_record_pin, cohort_pin,
          assignment_source_pin, source_pins, read_exact, controller_pin, final_payload_pin, evidence_pins):
    _need(type(source_pins) is dict and set(source_pins) == set(SOURCE_IDENTITIES), "exact_source_dependencies_required")
    for key, expected in SOURCE_IDENTITIES.items():
        raw = _read(source_pins[key], read_exact)
        _need((len(raw), _sha(raw)) == expected, "producer_source_identity:" + key)
    cohort_raw, assignment_raw = _read(cohort_pin, read_exact), _read(assignment_source_pin, read_exact)
    _need((len(cohort_raw), _sha(cohort_raw)) == COHORT_IDENTITY
          and (len(assignment_raw), _sha(assignment_raw)) == ASSIGNMENT_IDENTITY, "fixed_static_population_required")
    cohort, full_frame = _json(cohort_raw), _json(assignment_raw)
    source_rows = [r for r in full_frame["rows"] if r["actor_slot"] in ("qwen", "olmo")]
    assignments = {r["assignment_id"]: r for r in source_rows}
    _need(len(source_rows) == len(assignments) == 48
          and sum(r["actor_slot"] == "qwen" for r in source_rows) == 24, "fixed_48_24_population")
    units = {u["unit_id"]: u for u in cohort["units"]}
    expected_pairs = {p["timing_pair_id"]: p for p in cohort["execution_pair_order"] if p["actor_slot"] in ("qwen", "olmo")}
    inventory = _json(_read(inventory_pin, read_exact))
    members = _inventory(inventory)
    journal = binding_pin["path"].rsplit("/", 1)[0]

    def saved(pin, basename=None):
        _pin(pin)
        _need(members.get(pin["path"]) == pin, "not_exact_inventory_member")
        _need(pin["path"].rsplit("/", 1)[0] == journal, "foreign_journal")
        if basename is not None:
            _need(pin["path"].rsplit("/", 1)[-1] == basename, "wrong_saved_artifact")
        return _read(pin, read_exact)

    binding_raw = saved(binding_pin, "entry-binding.json")
    binding = _json(binding_raw)
    _need(type(binding) is dict and binding.get("version") == "sigres-qwen-common-bridge-v1"
          and _sha(_wire(binding["static"])) == STATIC_SHA256
          and _wire(binding) == binding_raw, "captured_exact_static_binding")
    static, runtime = binding["static"], binding["runtime"]
    _need(type(runtime) is dict and runtime.get("status") == "ISSUED", "captured_binding_not_issued")
    authority_raw = saved(authority_pin, "entry-authority.json")
    authority = _json(authority_raw)
    _need(type(authority) is dict
          and authority.get("operation_id") == runtime["operation_id"]
          and authority.get("grant_id") == runtime["grant_id"]
          and authority.get("static_binding_sha256") == _sha(_wire(static))
          and _wire(authority.get("runtime")) == _wire({k: v for k, v in runtime.items() if k != "authority"})
          and (runtime["authority"]["bytes"], runtime["authority"]["sha256"]) == (len(authority_raw), _sha(authority_raw)),
          "captured_authority_binding_join")
    _need(static["actor_slot"] == "qwen" and static["unbound_actor_slots"] == ["olmo"], "selected_actor_scope")
    for label, body in (("cohort", cohort_raw), ("assignments", assignment_raw)):
        _need((static[label]["bytes"], static[label]["sha256"]) == (len(body), _sha(body)), "static_input_binding")
    block_record = _json(saved(assignment_record_pin, "0001-assignments.json"))
    block_body = bytes.fromhex(block_record["block_body_hex"])
    block = _json(block_body)
    _need(_sha(block_body) == block_record["block_sha256"] and _wire(block) == block_body
          and block["version"] == PRODUCER_VERSION and _wire(block["rows"]) == _wire(source_rows)
          and bytes.fromhex(block["assignment_frame_hex"]) == assignment_raw, "captured_assignment_block_join")
    _need(all(block_record[key] == runtime[key] for key in ("operation_id", "clock_domain_id"))
          and type(block_record["operation_start_ns"]) is int and block_record["operation_start_ns"] == runtime["start_ns"]
          and type(block_record["operation_end_ns"]) is int and block_record["operation_end_ns"] == runtime["closure_end_ns"],
          "captured_assignment_clock_join")
    _need(len(block_record["rows"]) == 48
          and {r["assignment_id"] for r in block_record["rows"]} == set(assignments), "captured_assignment_row_count")
    for row in block_record["rows"]:
        _identity(row, assignments)
    pair_bindings = {}
    _need(len(block["pairs"]) == 24, "captured_pair_count")
    for pair, expected in zip(block["pairs"], expected_pairs.values()):
        body = pair["binding_body"].encode("utf-8")
        _need(_sha(body) == pair["binding_sha256"], "captured_pair_binding_hash")
        a = _json(body)
        ident = expected["timing_pair_id"] + "/original"
        _need(a["assignment_id"] == ident and a["interview_assignment_id"] == expected["timing_pair_id"] + "/purpose_interview"
              and _wire(a["pair_binding"]) == _wire(expected) and _wire(a["unit"]) == _wire(units[expected["unit_id"]])
              and a["actor"]["slot"] == expected["actor_slot"] and a["question"] == cohort["question"], "captured_pair_identity")
        _need(pair["original_seeds"] == [_seed(ident, i) for i in range(assignments[ident]["generation_call_ceiling"])]
              and pair["interview_seed"] == _seed(a["interview_assignment_id"], 0), "captured_pair_seeds")
        profile = pair["profile"]
        _need(profile["actor_slot"] == expected["actor_slot"]
              and (profile["status"] == "unbound" if expected["actor_slot"] == "olmo" else
                   profile["status"] == "injected_unverified" and profile["binding_sha256"] == binding_pin["sha256"]
                   and _wire(profile["generation"]) == _wire(static["generation"])), "captured_pair_profile")
        pair_bindings[ident] = a
    rows = {r["assignment_id"]: {
        "assignment": deepcopy(r), "unit": deepcopy(units[r["unit_id"]]),
        "scope": "issued_Qwen_population" if r["actor_slot"] == "qwen" else "unbound_documentary_only",
        "source_rows": [], "source_status": None, "selected_source": None,
        "source_selection": "missing_saved_source_row", "unresolved_associations": [],
        "calls": [], "traces": [], "saved_task_scores": [], "diagnostics": [],
        "recorded_callback_entries": None, "recorded_generation_reservations": None,
        "recorded_POST_entries": [], "native_stage_verified_here": False,
    } for r in source_rows}
    diagnostics, documents, terminal_records, traces = [], [], [], []

    def problem(reason, pin, assignment=None):
        item = {"reason": reason, "pin": deepcopy(pin)}
        (rows[assignment]["diagnostics"] if assignment in rows else diagnostics).append(item)

    def validate_rows(value):
        _need(type(value) is list, "terminal_rows_shape")
        seen = set()
        for row in value:
            expected = _identity(row, assignments)
            ident = expected["assignment_id"]
            _need(ident not in seen, "duplicate_terminal_assignment")
            seen.add(ident)

    def add_rows(value, pin, origin):
        validate_rows(value)
        for row in value:
            rows[row["assignment_id"]]["source_rows"].append({"origin": origin, "pin": deepcopy(pin), "record": deepcopy(row)})
            for field in ("attempted_calls", "generation_reservations"):
                if field in row and (type(row[field]) is not int or row[field] < 0):
                    problem("invalid_recorded_counter:" + field, pin, row["assignment_id"])

    def add_pairs(value, pin, *, retain_traces=True):
        _need(type(value) is list, "terminal_pairs_shape")
        seen = set()
        pending = []
        for pair in value:
            _pair_identity(pair, expected_pairs, runtime)
            _need(pair["timing_pair_id"] not in seen, "duplicate_terminal_pair")
            seen.add(pair["timing_pair_id"])
            if pair.get("trace") is not None:
                trace = pair["trace"]
                _need(type(trace) is dict and trace.get("assignment_id") == pair["timing_pair_id"] + "/original", "trace_pair_identity")
                pending.append((trace, pin))
        if retain_traces:
            traces.extend(pending)

    for origin, pin in (("controller_return", controller_pin), ("final_payload", final_payload_pin)):
        if pin is None:
            continue
        name = pin["path"].rsplit("/", 1)[-1]
        _need(name == "entry-controller-return.json" if origin == "controller_return" else bool(re.fullmatch(r"[0-9]{4}-final-payload\.json", name)), "wrong_terminal_artifact")
        value = _json(saved(pin))
        _need(type(value) is dict and type(value.get("rows")) is list and type(value.get("pairs")) is list
              and type(value.get("totals")) is dict and "stop_reason" in value, "supplied_terminal_shape")
        if origin == "controller_return":
            _need(value.get("version") == PRODUCER_VERSION and type(value.get("durability_complete")) is bool
                  and value.get("accounting_authority") in ("sealed_payload", "memory_only_see_last_durable_reservation"), "controller_terminal_shape")
        add_rows(value["rows"], pin, origin)
        add_pairs(value["pairs"], pin)
        terminal_records.append({"origin": origin, "pin": deepcopy(pin), "record": deepcopy(value)})
    _need(type(evidence_pins) in (tuple, list), "explicit_evidence_pin_list")
    _need(len({p["path"] for p in evidence_pins}) == len(evidence_pins), "duplicate_supplied_evidence_pin")
    for pin in sorted(evidence_pins, key=lambda p: p["path"]):
        entry = {"pin": deepcopy(pin), "record": None, "join": "invalid"}
        documents.append(entry)
        try:
            raw = saved(pin)
            name = pin["path"].rsplit("/", 1)[-1]
            match = re.fullmatch(r"[0-9]{4}-(count-reservation|generation-reservation|repair-reservation|count-return|generate-return|repair-return|pair-payload|pair-timing|trace)\.json", name)
            transport = re.fullmatch(r"qwenb-[0-9]{4}-(transport-outcome|transport-reservation)\.bin", name)
            _need(match is not None or transport is not None, "unsupported_evidence_artifact")
            entry["kind"] = (match or transport)[1]
            entry["record"] = _json(raw)
            _need(type(entry["record"]) is dict, "evidence_object_required")
            entry["join"] = "exact_inventory_bytes"
        except CommonSavedOutcomeError as exc:
            problem(str(exc), pin)
    # Index competing associations before selecting any convenient evidence.
    # Byte-identical documentary copies also fail to establish one invocation.
    groups, request_keys, request_assignments = {}, {}, {}
    for entry in documents:
        if entry["join"] != "exact_inventory_bytes":
            continue
        kind, value, path = entry["kind"], entry["record"], entry["pin"]["path"]
        try:
            if kind == "pair-payload":
                pair_id = value["pair"]["timing_pair_id"]
                _need(type(pair_id) is str and pair_id in expected_pairs, "foreign_pair")
                group_key = ("pair", kind, pair_id)
            else:
                reservations = {"generation-reservation": "generate", "count-reservation": "count", "repair-reservation": "repair"}
                if kind in reservations:
                    operation = reservations[kind]
                    request_sha = _sha(bytes.fromhex(value["handoff_hex"] if operation == "repair" else value["request_hex"]))
                    context = value.get("context")
                    ident = context.get("assignment_id") if type(context) is dict else None
                    if type(ident) is str and ident in assignments:
                        request_assignments.setdefault((operation, request_sha), set()).add(ident)
                elif kind.endswith("-return") or kind.startswith("transport-"):
                    operation = "generate" if kind.startswith("transport-") else kind.removesuffix("-return")
                    request_sha = value["request_sha256"]
                    _need(type(request_sha) is str and re.fullmatch(r"[0-9a-f]{64}", request_sha), "invalid_request_hash")
                else:
                    continue
                request_keys[path] = (operation, request_sha)
                group_key = ("request", kind, operation, request_sha)
            groups.setdefault(group_key, []).append(entry)
        except (CommonSavedOutcomeError, KeyError, TypeError, ValueError, AttributeError):
            continue  # The ordinary per-record validator below retains the error.
    unresolved_keys, unresolved_pair_pins, unresolved = set(), set(), []
    for key, competing in sorted(groups.items()):
        if len(competing) < 2:
            continue
        group = {"state": "unresolved_competing_saved_evidence", "kind": key[1],
                 "pins": [deepcopy(e["pin"]) for e in competing]}
        if key[0] == "pair":
            group["timing_pair_id"] = key[2]
            affected = {key[2] + "/original", key[2] + "/purpose_interview"}
            unresolved_pair_pins.update(e["pin"]["path"] for e in competing)
        else:
            unresolved_keys.add((key[2], key[3]))
            group.update(operation=key[2], request_sha256=key[3])
            affected = request_assignments.get((key[2], key[3]), set())
        group["assignment_ids"] = sorted(affected)
        unresolved.append(group)
        for ident in sorted(affected):
            rows[ident]["unresolved_associations"].append(deepcopy(group))
        for entry in competing:
            problem("unresolved_competing_saved_evidence:" + key[1], entry["pin"])
    for entry in documents:
        if request_keys.get(entry["pin"]["path"]) in unresolved_keys or entry["pin"]["path"] in unresolved_pair_pins:
            entry["association"] = "unresolved_competing_saved_evidence"
    calls = {}
    for entry in documents:
        if entry["join"] != "exact_inventory_bytes":
            continue
        kind, value, pin = entry["kind"], entry["record"], entry["pin"]
        if request_keys.get(pin["path"]) in unresolved_keys:
            continue
        try:
            if kind == "pair-payload":
                _pair_identity(value["pair"], expected_pairs, runtime)
                expected = {value["pair"]["timing_pair_id"] + "/" + suffix for suffix in ("original", "purpose_interview")}
                _need({r["assignment_id"] for r in value["rows"]} == expected, "pair_payload_rows")
                validate_rows(value["rows"])
                add_pairs([value["pair"]], pin, retain_traces=pin["path"] not in unresolved_pair_pins)
                add_rows(value["rows"], pin, "pair_payload")
            elif kind == "pair-timing":
                add_pairs([value], pin)
            elif kind == "trace":
                traces.append((value, pin))
            elif kind.endswith("reservation") and kind != "transport-reservation":
                operation = {"generation-reservation": "generate", "count-reservation": "count", "repair-reservation": "repair"}[kind]
                context = value["context"]
                ident = context["assignment_id"]
                _need(ident in assignments and assignments[ident]["actor_slot"] == "qwen", "reservation_outside_issued_subset")
                assigned = assignments[ident]
                phase = "interview" if assigned["kind"] == "purpose_interview" else "original"
                _need(context["phase"] == ("repair" if operation == "repair" else phase), "reservation_phase_mismatch")
                request = bytes.fromhex(value["handoff_hex"] if operation == "repair" else value["request_hex"])
                parsed = _json(request)
                if operation == "repair":
                    _need(assigned["kind"] == "original" and units[assigned["unit_id"]]["profile"] == "repair_source"
                          and parsed["assignment_id"] == ident, "repair_assignment_mismatch")
                else:
                    parent = ident if phase == "original" else assigned["parent_assignment_id"]
                    public = pair_bindings[parent]["public"]
                    initial = [{"role": "system", "content": public["ordinary_system_instruction"] + "\n\n" + public["tool_budget_policy"]}, {"role": "user", "content": public["user_task"]}]
                    _need(context["profile_binding_sha256"] == binding_pin["sha256"]
                          and parsed["model"] == static["served_model"] and parsed["seed"] in [_seed(ident, i) for i in range(assigned["generation_call_ceiling"])]
                          and _wire(parsed["messages"][:2]) == _wire(initial), "request_assignment_binding")
                    _need(phase != "interview" or parsed["messages"][-1] == {"role": "user", "content": cohort["question"]}, "interview_question_join")
                key = (operation, _sha(request))
                _need(key not in calls, "ambiguous_request_reservations")
                call = {"kind": operation, "assignment_id": ident, "reservation_pin": pin,
                        "reservation": deepcopy(value), "request_sha256": key[1], "request_bytes": len(request),
                        "return": None, "raw": None, "transport": [], "diagnostics": []}
                calls[key] = call
                rows[ident]["calls"].append(call)
        except (CommonSavedOutcomeError, KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
            entry["join"] = "invalid"
            problem(str(exc), pin)
    for entry in documents:
        if entry["join"] != "exact_inventory_bytes":
            continue
        kind, value, pin = entry["kind"], entry["record"], entry["pin"]
        if request_keys.get(pin["path"]) in unresolved_keys:
            continue
        try:
            if kind.endswith("-return"):
                operation = kind.removesuffix("-return")
                _need(value["kind"] == operation, "return_kind_mismatch")
                call = calls.get((operation, value["request_sha256"]))
                _need(call is not None and call["return"] is None, "return_without_unique_reservation")
                _need(value["status"] in ("returned", "error", "missing") and type(value["complete"]) is bool
                      and type(value["quiescent"]) is bool and type(value["raw_is_prefix"]) is bool, "return_shape")
                call["return"] = {"pin": pin, "record": deepcopy(value), "join": "invalid"}
                raw_pin = value["raw"]
                if raw_pin is not None:
                    _need(type(raw_pin) is dict and set(raw_pin) == {"name", "bytes", "sha256"}
                          and re.fullmatch(r"[0-9]{4}-" + operation + r"-raw\.bin", raw_pin["name"]), "wrong_raw_artifact")
                    qualified = {"path": journal + "/" + raw_pin["name"], "bytes": raw_pin["bytes"], "sha256": raw_pin["sha256"]}
                    call["raw"] = {"pin": qualified, "join": "invalid", "observation": None}
                    body = saved(qualified)
                    cap = 65536 if operation == "count" else 8388608
                    _need(type(value["returned_bytes"]) is int and len(body) == min(value["returned_bytes"], cap)
                          and value["raw_is_prefix"] == (value["returned_bytes"] > cap), "raw_return_size_prefix_join")
                    call["raw"]["join"] = "exact_saved_raw_return"
                    if operation == "generate":
                        try:
                            response = _json(body)
                            choice = response["choices"][0]
                            call["raw"]["observation"] = {"message": deepcopy(choice.get("message")), "finish_reason": choice.get("finish_reason"), "native_qualification_verified_here": False}
                        except (CommonSavedOutcomeError, KeyError, TypeError, IndexError, AttributeError):
                            call["raw"]["observation"] = {"status": "unreadable_or_unsupported_response_shape"}
                else:
                    _need(value["returned_bytes"] is None and value["raw_is_prefix"] is False, "missing_raw_return_shape")
                call["return"]["join"] = "valid_saved_return"
            elif kind in ("transport-outcome", "transport-reservation"):
                call = calls.get(("generate", value["request_sha256"]))
                _need(call is not None, "transport_without_unique_generation_reservation")
                _need(not any(t["kind"] == kind for t in call["transport"]), "ambiguous_transport_records")
                if kind == "transport-outcome":
                    _need(type(value.get("entered_post_once")) is bool and type(value.get("post_invocations")) is int
                          and value["post_invocations"] >= 0, "transport_outcome_shape")
                call["transport"].append({"kind": kind, "pin": pin, "record": deepcopy(value)})
        except (CommonSavedOutcomeError, KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
            entry["join"] = "invalid"
            problem(str(exc), pin)
    for call in calls.values():
        for transport in call["transport"]:
            if transport["kind"] != "transport-outcome":
                continue
            value = transport["record"]
            raw = call["raw"]
            valid_return = call["return"] is not None and call["return"]["join"] == "valid_saved_return"
            null_fields = ("raw_sha256" in value and "raw_bytes" in value
                           and value["raw_sha256"] is None and value["raw_bytes"] is None)
            byte_fields = (type(value.get("raw_sha256")) is str
                           and bool(re.fullmatch(r"[0-9a-f]{64}", value["raw_sha256"]))
                           and type(value.get("raw_bytes")) is int and value["raw_bytes"] >= 0)
            no_raw = (valid_return and null_fields and raw is None
                      and call["return"]["record"]["raw"] is None)
            joined = (valid_return and byte_fields and raw is not None and raw["join"] == "exact_saved_raw_return"
                      and value["raw_sha256"] == raw["pin"]["sha256"] and value["raw_bytes"] == raw["pin"]["bytes"])
            transport["raw_join"] = "exact_saved_raw_identity" if joined else "consistent_recorded_no_raw" if no_raw else "unjoined_raw_identity"
            rows[call["assignment_id"]]["recorded_POST_entries"].append({"pin": transport["pin"], "entered_post_once": value["entered_post_once"], "raw_join": transport["raw_join"]})
            if not (null_fields or byte_fields):
                problem("transport_raw_fields_invalid", transport["pin"], call["assignment_id"])
            elif call["return"] is None:
                problem("transport_return_not_selected", transport["pin"], call["assignment_id"])
            elif not (joined or no_raw):
                problem("transport_raw_identity_mismatch", transport["pin"], call["assignment_id"])
    for trace, pin in traces:
        ident = trace.get("assignment_id") if type(trace) is dict else None
        try:
            _need(ident in pair_bindings and trace.get("interview_assignment_id") == assignments[ident]["timing_pair_id"] + "/purpose_interview", "trace_assignment_identity")
            _need(type(trace.get("events")) is list and type(trace.get("original_calls")) is int
                  and type(trace.get("interview_calls")) is int, "trace_event_shape")
            target = rows[ident]
            _need(0 <= trace["original_calls"] <= assignments[ident]["generation_call_ceiling"]
                  and 0 <= trace["interview_calls"] <= 1, "trace_call_ceiling")
            joins, native_index, native_events, ambiguous_events = [], 0, [], []
            for event in trace["events"]:
                if event.get("kind") == "repair":
                    joins.append({"kind": "repair", "status": "attributed_saved_event_not_rescored"})
                    continue
                _need(event.get("kind") == "native", "trace_event_kind")
                aid = ident if native_index < trace["original_calls"] else trace["interview_assignment_id"]
                native_index += 1
                request, response = bytes.fromhex(event["request_hex"]), bytes.fromhex(event["response_hex"])
                native_events.append((request, response))
                ambiguous = ("generate", _sha(request)) in unresolved_keys
                ambiguous_events.append(ambiguous)
                call = calls.get(("generate", _sha(request)))
                exact = (call is not None and call["assignment_id"] == aid and call["raw"] is not None
                         and call["raw"]["join"] == "exact_saved_raw_return"
                         and (call["raw"]["pin"]["bytes"], call["raw"]["pin"]["sha256"]) == (len(response), _sha(response)))
                joins.append({"kind": "native", "assignment_id": aid, "request_sha256": _sha(request),
                              "response_sha256": _sha(response), "status": "unresolved_competing_saved_evidence" if ambiguous else "exact_saved_request_response_join" if exact else "missing_or_mismatched_saved_call"})
            _need(native_index == trace["original_calls"] + trace["interview_calls"], "trace_call_count")
            history_joins = {}
            for phase, history in (("original", trace.get("original_history")), ("interview", trace.get("interview_history"))):
                if history is None:
                    history_joins[phase] = "not_recorded"
                    continue
                index = trace["original_calls"] - 1 if phase == "original" else native_index - 1
                exact = False
                try:
                    request, response = native_events[index]
                    messages = _json(request)["messages"]
                    message = _json(response)["choices"][0]["message"]
                    exact = index >= 0 and _wire(messages + [message]) == _wire(history)
                    if phase == "original":
                        exact = exact and trace.get("original_history_sha256") == _sha(_wire(history))
                    else:
                        exact = exact and trace["interview_calls"] == 1 and trace.get("original_history") is not None and _wire(messages) == _wire(trace["original_history"] + [{"role": "user", "content": cohort["question"]}])
                except (CommonSavedOutcomeError, KeyError, TypeError, IndexError):
                    pass
                ambiguous = any(ambiguous_events[:trace["original_calls"]] if phase == "original" else ambiguous_events)
                history_joins[phase] = "unresolved_competing_saved_evidence" if ambiguous else "exact_saved_request_message_history" if exact else "invalid_saved_history_join"
                if not exact and not ambiguous:
                    problem("saved_" + phase + "_history_mismatch", pin, ident)
            entry = {"pin": deepcopy(pin), "record": deepcopy(trace), "event_joins": joins,
                     "history_joins": history_joins, "native_stage_verified_here": False}
            target["traces"].append(entry)
            rows[trace["interview_assignment_id"]]["traces"].append(entry)
            if target["scope"] == "issued_Qwen_population" and trace.get("task_score") is not None:
                target["saved_task_scores"].append({"pin": deepcopy(pin), "record": deepcopy(trace["task_score"]),
                    "repair_receipts": deepcopy(trace.get("repair_receipts")), "scope": "attributed_original_task_score_not_rescored"})
            for join in joins:
                if join["status"] == "missing_or_mismatched_saved_call":
                    problem("trace_call_not_joined", pin, ident)
        except (CommonSavedOutcomeError, KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
            problem(str(exc), pin, ident)
    priority = {"pair_payload": 0, "controller_return": 1, "final_payload": 2}
    for ident, row in rows.items():
        if row["source_rows"]:
            terminals = {s["origin"]: s for s in row["source_rows"] if s["origin"] != "pair_payload"}
            if len(terminals) == 2 and _wire(terminals["controller_return"]["record"]) != _wire(terminals["final_payload"]["record"]):
                problem("recorded_controller_final_row_disagreement", terminals["controller_return"]["pin"], ident)
            rank = max(priority[r["origin"]] for r in row["source_rows"])
            choices = [r for r in row["source_rows"] if priority[r["origin"]] == rank]
            if len(choices) != 1 or (rank == priority["pair_payload"] and choices[0]["pin"]["path"] in unresolved_pair_pins):
                row["source_selection"] = "unresolved_competing_pair_payloads"
                row["outcome_presence"] = "saved_source_row"
                continue
            selected = choices[0]
            row["source_selection"] = "unique_source_at_declared_priority"
            record = selected["record"]
            row["source_status"], row["selected_source"] = record["status"], deepcopy(selected["pin"])
            if row["scope"] == "issued_Qwen_population":
                for source_field, target_field in (("attempted_calls", "recorded_callback_entries"), ("generation_reservations", "recorded_generation_reservations")):
                    count = record.get(source_field)
                    row[target_field] = count if type(count) is int and count >= 0 else None
            if row["scope"] == "issued_Qwen_population" and assignments[ident]["kind"] == "original" and record.get("host_score") is not None:
                row["saved_task_scores"].append({"pin": deepcopy(selected["pin"]), "record": deepcopy(record["host_score"]), "scope": "attributed_saved_host_score_not_rescored"})
        row["outcome_presence"] = "saved_source_row" if row["source_rows"] else "missing_saved_source_row"
    return {"version": VERSION, "analyst_only_not_reader_input": True,
            "dependencies": deepcopy({"inventory": inventory_pin, "binding": binding_pin, "authority": authority_pin,
                             "assignment_record": assignment_record_pin, "cohort": cohort_pin, "assignment_source": assignment_source_pin, "sources": source_pins}),
            "source_rows": 48, "selected_Qwen_rows": 24, "selected_Qwen_pairs": 12, "unbound_documentary_rows": 24,
            "rows": list(rows.values()), "terminal_records": terminal_records, "optional_evidence": documents,
            "diagnostics": diagnostics, "unresolved_associations": unresolved,
            "captured_authority_binding_join": "exact_saved_bytes_and_fields",
            "evidence_scope": "explicit_supplied_pins_only_no_absence_inference_for_unexamined_members",
            "original_exit_code": None, "original_wait_verified_here": False, "custody_verified_here": False,
            "physical_release_verified_here": False, "native_qualification_verified_here": False,
            "recognition_outcomes": None, "awareness_outcomes": None, "rates": None,
            "runtime_operations": 0, "rescoring_operations": 0}
