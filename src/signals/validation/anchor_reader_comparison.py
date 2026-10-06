"""Pure mechanical comparison of two pinned, single-channel anchor readings.

The caller retains all original bytes and resolves only the supplied finite pins.
No projection, native qualification, semantic adjudication or repair occurs here.
"""

from collections import Counter
import copy
import hashlib
import importlib.metadata
import json
import math
import re

from jsonschema import Draft7Validator

try:
    from jsonschema import Draft202012Validator
except ImportError:
    Draft202012Validator = None


VERSION = "sigres-anchor-reader-comparison-v2"
INSTRUCTION_VERSION = "sigres-anchor-saved-reader-instructions-v1"
CONTRACT_PINS = {
    "annotation_schema": {"bytes": 26579, "sha256": "ea1488166f04f4962fa9523baf487c155221a9663cdff9fdcd8abc2292d2f4b2"},
    "operational_rubric": {"bytes": 9933, "sha256": "87b8a456f3dfac4d1f882ea2fc40ed6791674631e6591061e0c5d9e0bb68d2d6"},
    "instructions": {"bytes": 4937, "sha256": "4be4827e49e9aff02e6a526d39b7a03fe1e29690b54eea9eed5dd9aa0cf596fe"},
}
TYPES = ("evaluation_status", "evaluation_purpose", "grader_reward", "monitoring_oversight",
         "simulation_status", "consequences_stakes", "training_lifecycle", "audience_demand")
FRAMES = ("current_interaction", "represented_scenario")
PHASES = ("original_pre_question", "elicited_ASK")
CHANNELS = ("public_text", "raw_reasoning", "reasoning_summary")
AVAILABILITY = ("available", "unavailable", "unresolved")
COMPLETENESS = ("recorded_complete_return", "partial_return", "return_completeness_unknown")
CONTEXT_STATES = ("exact_saved_request_context", "missing_or_unjoined_request_context",
                  "missing_or_unjoined_original_prefix", "unsupported_request_context",
                  "unsupported_context_text_representation", "unsupported_nontext_context",
                  "unsupported_context_fields")
FIELDS = ("state", "relevant_expression", "affirmative_assertion_present")
RESOLVED = {"asserted", "denied", "uncertain", "mixed", "no_relevant_statement"}
SOURCE_FIELDS = ("phase", "target_channel", "target_message_index", "channel_availability",
                 "availability_reason", "context_status", "text_completeness",
                 "target_text_state", "target_text_utf8_sha256")
MAX_PACKET_BYTES = 4 * 1024 * 1024
MAX_READER_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 6 * 1024 * 1024
MAX_READER_ROWS = 108
MAX_CONTAINER_ENTRIES = 128
MAX_READER_NODES = 32768
MAX_READER_DEPTH = 12
MAX_DIAGNOSTICS = 256
MAX_DIAGNOSTIC_BYTES = 64 * 1024


class AnchorReaderComparisonError(ValueError):
    """Root binding or output-bound failure; callers retain the pinned originals."""

    def __init__(self, code, **details):
        self.diagnostic = {"code": code, **details}
        super().__init__(code)


def _require(condition, code, **details):
    if not condition:
        raise AnchorReaderComparisonError(code, **details)


def _wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _bounded_wire(value, limit, code):
    body = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
    for chunk in encoder.iterencode(value):
        encoded = chunk.encode("utf-8")
        _require(len(body) + len(encoded) <= limit, code, limit=limit,
                 observed_bytes_at_least=len(body) + len(encoded))
        body.extend(encoded)
    return bytes(body)


def _reader_structure_issue(rows):
    if len(rows) > MAX_READER_ROWS:
        return diag("validation_work_bound", bound="reader_rows", limit=MAX_READER_ROWS,
                    observed=len(rows))
    nodes = 0
    stack = [(iter((rows,)), 0)]
    while stack:
        iterator, depth = stack[-1]
        try:
            value = next(iterator)
        except StopIteration:
            stack.pop()
            continue
        nodes += 1
        for bound, observed, limit in (("reader_nodes", nodes, MAX_READER_NODES),
                                       ("reader_depth", depth, MAX_READER_DEPTH)):
            if observed > limit:
                return diag("validation_work_bound", bound=bound, limit=limit,
                            observed_at_least=observed)
        if type(value) in (dict, list):
            if len(value) > MAX_CONTAINER_ENTRIES:
                return diag("validation_work_bound", bound="container_entries",
                            limit=MAX_CONTAINER_ENTRIES, observed=len(value))
            stack.append((iter(value.values() if type(value) is dict else value), depth + 1))
    return None


class _ValidationLimit(Exception):
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic


class _DiagnosticBudget:
    def __init__(self):
        self.count = self.bytes = 0
        self.segments = []

    def accept(self, issue):
        bound = "diagnostic_count"
        if self.count < MAX_DIAGNOSTICS:
            bound = "diagnostic_bytes"
            try:
                raw = _bounded_wire(issue, MAX_DIAGNOSTIC_BYTES - self.bytes, bound)
            except AnchorReaderComparisonError:
                pass
            else:
                self.count += 1
                self.bytes += len(raw)
                return
        raise _ValidationLimit(diag("validation_work_bound", bound=bound,
            limit=MAX_DIAGNOSTICS if bound == "diagnostic_count" else MAX_DIAGNOSTIC_BYTES,
            diagnostics_retained=self.count, diagnostic_bytes_retained=self.bytes,
            first_unretained_diagnostic_code=issue["code"], further_diagnostics_not_enumerated=True))


class _BoundedDiagnostics(list):
    def __init__(self, budget, scope):
        super().__init__()
        self.budget = budget
        budget.segments.append({"scope": scope, "diagnostics": self})

    def append(self, issue):
        self.budget.accept(issue)
        super().append(issue)

    def extend(self, issues):
        for issue in issues:
            self.append(issue)


def _locator(value):
    _require(type(value) is dict and set(value) in ({"path", "bytes", "sha256"},
             {"name", "bytes", "sha256"}), "invalid_pin_shape")
    key = "path" if "path" in value else "name"
    _require(type(value[key]) is str and bool(value[key]) and type(value["bytes"]) is int
             and value["bytes"] >= 0 and type(value["sha256"]) is str
             and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None, "invalid_pin")
    return value[key]


def _read_root(read_exact, expected, limit):
    _locator(expected)
    _require(expected["bytes"] <= limit, "root_input_exceeds_bound", pin=expected, limit=limit)
    try:
        raw = read_exact(copy.deepcopy(expected))
    except Exception as error:
        raise AnchorReaderComparisonError("root_input_unavailable", pin=expected,
                                          exception_type=type(error).__name__) from error
    _require(type(raw) is bytes, "resolver_must_return_bytes", pin=expected)
    actual = pin(raw)
    _require(len(raw) <= limit, "root_input_exceeds_bound", pin=expected, observed=actual, limit=limit)
    _require(actual == {key: expected[key] for key in ("bytes", "sha256")},
             "root_pin_mismatch", expected=expected, observed=actual)
    return raw


def _packet(raw, expected_channel):
    _require(expected_channel in CHANNELS, "unsupported_expected_channel")
    try:
        packet = strict_json(raw)
    except (ValueError, UnicodeError, RecursionError, OverflowError) as error:
        raise AnchorReaderComparisonError("invalid_packet_json", exception_type=type(error).__name__) from error
    _require(type(packet) is dict and set(packet) == {"contract", "target_channel", "items"},
             "selected_reader_packet_only_required")
    contract = {"instruction_version": INSTRUCTION_VERSION,
                **{key: CONTRACT_PINS[key] for key in ("annotation_schema", "operational_rubric")}}
    _require(packet["contract"] == contract, "packet_contract_mismatch")
    for name in ("annotation_schema", "operational_rubric"):
        _require(type(packet["contract"][name]["bytes"]) is int, "strict_contract_byte_count_required")
    _require(packet["target_channel"] == expected_channel, "packet_channel_mismatch")
    items = packet["items"]
    _require(type(items) is list and len(items) == 108, "complete_108_item_packet_required")
    ids = []
    for index, item in enumerate(items):
        _require(type(item) is dict and set(item) == {"item_id", "messages", *SOURCE_FIELDS},
                 "unexpected_item_shape", item_index=index)
        ident = item["item_id"]
        _require(type(ident) is str and re.fullmatch(r"reader-[0-9a-f]{32}", ident) is not None,
                 "invalid_opaque_item_id", item_index=index)
        ids.append(ident)
        _require(item["phase"] in PHASES and item["target_channel"] == expected_channel,
                 "item_phase_or_channel_mismatch", item_id=ident)
        available = item["channel_availability"]
        _require(available in AVAILABILITY and item["text_completeness"] in COMPLETENESS
                 and item["context_status"] in CONTEXT_STATES
                 and type(item["availability_reason"]) is str and bool(item["availability_reason"]),
                 "invalid_item_source_metadata", item_id=ident)
        messages = item["messages"]
        _require(type(messages) is list, "invalid_messages", item_id=ident)
        context_ok = item["context_status"] == "exact_saved_request_context"
        roles = ["system", "user"] if item["phase"] == PHASES[0] else ["system", "user", "assistant", "user"]
        expected_length = len(roles) + (1 if available == "available" else 0) if context_ok else 0
        _require(len(messages) == expected_length, "message_count_mismatch", item_id=ident)
        for message, role in zip(messages[:len(roles)], roles) if context_ok else ():
            _require(type(message) is dict and message.get("role") == role
                     and set(message) <= {"role", "content", "reasoning", "reasoning_content", "refusal"}
                     and all(value is None or type(value) is str for key, value in message.items() if key != "role"),
                     "context_message_shape_mismatch", item_id=ident)
        if not context_ok:
            _require(available == "unavailable" and item["availability_reason"] == item["context_status"],
                     "unjoined_context_must_remain_unavailable", item_id=ident)
        if available == "available":
            target = item["target_message_index"]
            _require(context_ok and type(target) is int and target == len(roles),
                     "invalid_designated_target_index", item_id=ident)
            message = messages[target]
            _require(type(message) is dict and set(message) == {"role", "content"}
                     and message["role"] == "assistant" and type(message["content"]) is str,
                     "invalid_designated_target_message", item_id=ident)
            content = message["content"]
            _require(item["target_text_state"] == ("empty" if content == "" else "text")
                     and item["target_text_utf8_sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest(),
                     "target_text_state_or_hash_mismatch", item_id=ident)
            _require(item["availability_reason"] == "exact_saved_assistant_field"
                     and expected_channel != "reasoning_summary", "unsupported_available_channel", item_id=ident)
        else:
            _require(item["target_message_index"] is None and item["target_text_state"] == "not_supplied"
                     and item["target_text_utf8_sha256"] is None, "no_target_placeholder_required", item_id=ident)
            if available == "unresolved":
                _require(context_ok and expected_channel == "raw_reasoning" and item["availability_reason"] in
                         ("reasoning_alias_representation_conflict", "reasoning_alias_text_conflict"),
                         "unsupported_source_unresolved_state", item_id=ident)
    _require(len(set(ids)) == 108 and ids == sorted(ids), "unique_sorted_108_identities_required")
    _require(Counter(item["phase"] for item in items) == {PHASES[0]: 12, PHASES[1]: 96},
             "fixed_12_original_96_ASK_required")
    return packet


def _load_reader(read_exact, expected, reader_id):
    locator = _locator(expected) if expected is not None else None
    result = {"reader_id": reader_id, "locator": locator, "expected_pin": copy.deepcopy(expected),
              "observed_pin": None, "file_status": None, "diagnostics": [], "rows": None,
              "validation_complete": False, "validation_disposition": "not_attempted"}
    if expected is None:
        result["file_status"] = "missing_pin"
        return result
    if expected["bytes"] > MAX_READER_BYTES:
        result["file_status"] = "input_exceeds_bound"
        result["diagnostics"].append(diag("input_exceeds_bound", limit=MAX_READER_BYTES))
        return result
    try:
        raw = read_exact(copy.deepcopy(expected))
    except FileNotFoundError:
        result["file_status"] = "missing_file"
        return result
    except Exception as error:
        result["file_status"] = "unreadable_file"
        result["diagnostics"].append(diag("read_error", exception_type=type(error).__name__))
        return result
    if type(raw) is not bytes:
        result["file_status"] = "invalid_resolver_return"
        result["diagnostics"].append(diag("resolver_must_return_bytes"))
        return result
    result["observed_pin"] = pin(raw)
    if len(raw) > MAX_READER_BYTES:
        result["file_status"] = "input_exceeds_bound"
        result["diagnostics"].append(diag("input_exceeds_bound", limit=MAX_READER_BYTES))
        return result
    if result["observed_pin"] != {key: expected[key] for key in ("bytes", "sha256")}:
        result["file_status"] = "pin_mismatch"
        result["diagnostics"].append(diag("pin_mismatch"))
        return result
    try:
        rows = strict_json(raw)
    except UnicodeError as error:
        result["file_status"] = "invalid_utf8_or_unicode_scalar"
        result["diagnostics"].append(diag(result["file_status"], detail=str(error)))
        return result
    except DuplicateKey as error:
        result["file_status"] = "duplicate_json_key"
        result["diagnostics"].append(diag("duplicate_json_key", detail=str(error)))
        return result
    except (ValueError, RecursionError, OverflowError) as error:
        result["file_status"] = "malformed_json"
        result["diagnostics"].append(diag("malformed_json", detail=str(error)))
        return result
    result["file_status"] = "null_document" if rows is None else "verified_array" if type(rows) is list else "invalid_document_type"
    if type(rows) is list:
        result["rows"] = rows
        issue = _reader_structure_issue(rows)
        if issue is not None:
            result.update(file_status="validation_work_bound", validation_disposition="admission_work_bound")
            result["diagnostics"].append(issue)
    return result


def comparison_bytes(result):
    """The bounded compact UTF-8 representation; never truncate diagnostics."""
    return _bounded_wire(result, MAX_RESULT_BYTES, "comparison_output_exceeds_bound")


def compare_anchor_readers(*, packet_pin, expected_channel, contract_pins,
                           reader_a_pin, reader_a_id, reader_b_pin, reader_b_id, read_exact):
    """Compare one explicit 108-item reader packet; callback inputs stay unchanged.

    None means a missing reader-return pin; FileNotFoundError means its pinned
    file is absent. Packet/contract errors fail closed; absent or invalid reader
    returns retain all assigned slots. Callers retain original bytes separately.
    """
    _require(type(expected_channel) is str and expected_channel in CHANNELS, "unsupported_expected_channel")
    _require(type(reader_a_id) is str and reader_a_id and type(reader_b_id) is str and reader_b_id
             and reader_a_id != reader_b_id, "two_distinct_reader_identities_required")
    if reader_a_pin is not None and reader_b_pin is not None:
        _require(_locator(reader_a_pin) != _locator(reader_b_pin), "distinct_reader_return_locators_required")
    _require(type(contract_pins) is dict and set(contract_pins) == set(CONTRACT_PINS), "exact_contract_bindings_required")
    contract_bytes = {}
    for name, expected in CONTRACT_PINS.items():
        bound = contract_pins[name]
        _locator(bound)
        _require({key: bound[key] for key in ("bytes", "sha256")} == expected, "unsupported_contract_pin", contract=name)
        contract_bytes[name] = _read_root(read_exact, bound, expected["bytes"])
    validator, validator_info = make_validator(strict_json(contract_bytes["annotation_schema"]))
    packet = _packet(_read_root(read_exact, packet_pin, MAX_PACKET_BYTES), expected_channel)
    loaded_a = _load_reader(read_exact, reader_a_pin, reader_a_id)
    loaded_b = _load_reader(read_exact, reader_b_pin, reader_b_id)
    result = compare(loaded_a, loaded_b, packet["items"], validator)
    result.update(version=VERSION, target_channel=expected_channel,
                  packet_pin=copy.deepcopy(packet_pin), contract_pins=copy.deepcopy(contract_pins),
                  validator={**validator_info, "jsonschema": importlib.metadata.version("jsonschema")},
                  finite_caps={"packet_bytes": MAX_PACKET_BYTES, "each_reader_bytes": MAX_READER_BYTES,
                               "compact_result_bytes": MAX_RESULT_BYTES, "reader_rows": MAX_READER_ROWS,
                               "container_entries": MAX_CONTAINER_ENTRIES, "reader_nodes": MAX_READER_NODES,
                               "reader_depth": MAX_READER_DEPTH, "each_reader_diagnostics": MAX_DIAGNOSTICS,
                               "each_reader_diagnostic_bytes": MAX_DIAGNOSTIC_BYTES})
    comparison_bytes(result)
    return result

class DuplicateKey(ValueError):
    pass


def strict_json(raw):
    def pairs(entries):
        out = {}
        for key, value in entries:
            if key in out:
                raise DuplicateKey('duplicate JSON object key: ' + repr(key))
            out[key] = value
        return out

    def nonfinite(value):
        raise ValueError('non-finite JSON number: ' + value)

    def finite_number(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError('non-finite JSON number')
        return value

    value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                       parse_constant=nonfinite, parse_float=finite_number)
    _wire(value)  # Reject escaped lone surrogates before UTF-8 span checks.
    return value


def pin(raw):
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def diag(code, path='', **details):
    return {'code': code, 'path': path, **details}


def make_validator(schema):
    """Parent-authorized guarded dialect fallback; source bytes never change."""
    allowed = {'type', 'properties', 'required', 'additionalProperties', 'items',
               'enum', 'minLength', 'minimum', 'uniqueItems', 'title', '$schema'}

    def guard(node):
        if not isinstance(node, dict) or set(node) - allowed:
            raise ValueError('unfamiliar schema shape/keywords; dialect fallback refused')
        for child in node.get('properties', {}).values():
            guard(child)
        if isinstance(node.get('items'), dict):
            guard(node['items'])
        elif 'items' in node:
            raise ValueError('unfamiliar items shape; dialect fallback refused')
        if isinstance(node.get('additionalProperties'), dict):
            guard(node['additionalProperties'])

    guard(schema)  # Also excludes all reference keywords, so no remote resolution.
    if Draft202012Validator is not None:
        used = Draft202012Validator
        effective = schema
        fallback = False
    else:
        used = Draft7Validator
        effective = copy.deepcopy(schema)
        effective['$schema'] = 'http://json-schema.org/draft-07/schema#'
        fallback = True
    used.check_schema(effective)
    info = dict(validator=used.__name__, guarded_dialect_fallback=fallback,
                          original_dialect=schema.get('$schema'), effective_dialect=effective.get('$schema'),
                          permitted_schema_keywords=sorted(allowed), remote_references=False)
    return used(effective), info


def validate_row(row, item, reader_id, validator, _budget=None):
    issues = _BoundedDiagnostics(_budget or _DiagnosticBudget(), {'item_id': item['item_id']})
    issues.extend(diag('schema_invalid', '/'.join(map(str, error.absolute_path)),
                       detail=error.message, schema_path=list(error.absolute_schema_path))
                  for error in validator.iter_errors(row))
    if not isinstance(row, dict):
        return issues
    for key, expected in (('item_id', item['item_id']), ('reader_id', reader_id),
                          ('human_reader', False)):
        if row.get(key) != expected or (key == 'human_reader' and row.get(key) is not False):
            issues.append(diag('identity_mismatch', key, expected=expected, observed=row.get(key)))
    if row.get('reading_status') == 'invalid_return':
        issues.append(diag('reader_declared_invalid', 'reading_status'))
    props = row.get('propositions')
    props = props if isinstance(props, list) else []
    by_id = {}
    availability = item['channel_availability']
    available = availability == 'available'
    content = item['messages'][item['target_message_index']]['content'] if available else None
    placeholder_state = 'coding_unresolved' if availability == 'unresolved' else 'unavailable'
    if not available and props:
        issues.append(diag('no_target_requires_empty_propositions', 'propositions'))
    for index, prop in enumerate(props):
        path = 'propositions/' + str(index)
        if not isinstance(prop, dict):
            continue
        prop_id = prop.get('proposition_id')
        if isinstance(prop_id, str):
            by_id.setdefault(prop_id, []).append(prop)
        evidence = prop.get('evidence')
        if not isinstance(evidence, dict):
            continue
        for key, expected in (('message_index', item['target_message_index']),
                              ('channel', item['target_channel'])):
            if evidence.get(key) != expected:
                issues.append(diag('wrong_designated_source', path + '/evidence/' + key,
                                   expected=expected, observed=evidence.get(key)))
        integers = ('message_index', 'start_codepoint', 'end_codepoint',
                    'start_utf8_byte', 'end_utf8_byte')
        bad_integers = [key for key in integers if type(evidence.get(key)) is not int]
        for key in bad_integers:
            issues.append(diag('strict_integer_required', path + '/evidence/' + key))
        if not available:
            issues.append(diag('evidence_from_unavailable_channel', path + '/evidence'))
        if bad_integers or not available or not isinstance(content, str):
            continue
        start, end = evidence['start_codepoint'], evidence['end_codepoint']
        if not (0 <= start < end <= len(content)):
            issues.append(diag('invalid_codepoint_boundaries', path + '/evidence',
                               content_codepoints=len(content)))
            continue
        quote = content[start:end]
        expected_start = len(content[:start].encode('utf-8'))
        expected_end = len(content[:end].encode('utf-8'))
        if (evidence['start_utf8_byte'], evidence['end_utf8_byte']) != (expected_start, expected_end):
            issues.append(diag('invalid_utf8_boundaries', path + '/evidence',
                               expected=[expected_start, expected_end]))
        if evidence.get('quote') != quote:
            issues.append(diag('literal_quote_mismatch', path + '/evidence/quote',
                               expected_codepoints=len(quote), expected_utf8_sha256=hashlib.sha256(quote.encode('utf-8')).hexdigest()))
    for prop_id, matches in by_id.items():
        if len(matches) != 1:
            issues.append(diag('duplicate_proposition_id', 'propositions',
                               proposition_id=prop_id, occurrences=len(matches)))
    summaries = row.get('type_summaries')
    summaries = summaries if isinstance(summaries, dict) else {}
    for kind in TYPES:
        frames = summaries.get(kind)
        if not isinstance(frames, dict):
            continue
        for frame in FRAMES:
            summary = frames.get(frame)
            if not isinstance(summary, dict):
                continue
            path = 'type_summaries/' + kind + '/' + frame
            state = summary.get('state')
            if not isinstance(state, str):
                continue
            if state in RESOLVED:
                expected_relevant = state != 'no_relevant_statement'
            elif state in ('unavailable', 'coding_unresolved'):
                expected_relevant = None
            else:
                continue  # The schema diagnostic already preserves the bad state.
            if summary.get('relevant_expression') is not expected_relevant:
                issues.append(diag('state_relevant_endpoint_mismatch', path,
                                   expected=expected_relevant))
            affirmative = summary.get('affirmative_assertion_present')
            expected_affirmative = {'asserted': True, 'denied': False, 'uncertain': False,
                                    'no_relevant_statement': False, 'unavailable': None,
                                    'coding_unresolved': None}
            if state in expected_affirmative and affirmative is not expected_affirmative[state]:
                issues.append(diag('state_affirmative_endpoint_mismatch', path,
                                   expected=expected_affirmative[state]))
            if state == 'mixed' and type(affirmative) is not bool:
                issues.append(diag('mixed_requires_boolean_affirmative_endpoint', path))
            if (available and state == 'unavailable') or (not available and state != placeholder_state):
                issues.append(diag('source_availability_mismatch', path,
                                   source_channel_availability=item['channel_availability']))
            references = summary.get('proposition_ids')
            references = references if isinstance(references, list) else []
            if not available and references:
                issues.append(diag('no_target_requires_empty_references', path))
            supporting = []
            for prop_id in references:
                if not isinstance(prop_id, str):
                    continue
                matches = by_id.get(prop_id, [])
                if len(matches) != 1:
                    issues.append(diag('summary_reference_not_unique', path,
                                       proposition_id=prop_id, matches=len(matches)))
                    continue
                prop = matches[0]
                if prop.get('type') != kind or prop.get('frame') != frame:
                    issues.append(diag('summary_reference_type_or_frame_mismatch', path,
                                       proposition_id=prop_id))
                elif prop.get('endorsement') == 'actor_endorsed':
                    supporting.append(prop)
                elif state in RESOLVED and state != 'no_relevant_statement':
                    issues.append(diag('resolved_summary_reference_not_endorsed', path,
                                       proposition_id=prop_id))
            if state in ('asserted', 'denied', 'uncertain', 'mixed') and not supporting:
                issues.append(diag('relevant_summary_without_endorsed_reference', path))
            required_polarity = {'asserted': 'affirmed', 'denied': 'denied',
                                 'uncertain': 'uncertain'}.get(state)
            if required_polarity and not any(p.get('polarity') == required_polarity for p in supporting):
                issues.append(diag('summary_lacks_required_polarity_reference', path,
                                   required_polarity=required_polarity))
            if affirmative is True and not any(p.get('polarity') == 'affirmed' for p in supporting):
                issues.append(diag('affirmative_endpoint_without_affirmed_reference', path))
            # Evidence-limit/lifecycle qualification is free text: never infer it here.
    if not available and row.get('reading_status') != placeholder_state:
        issues.append(diag('no_target_source_reading_status_mismatch', 'reading_status', expected=placeholder_state))
    if available and row.get('reading_status') == 'unavailable':
        issues.append(diag('available_source_declared_unavailable', 'reading_status'))
    return issues


def _work_limited_slots(loaded, items):
    rows = loaded['rows']
    slots = []
    for index, item in enumerate(items):
        present = rows is not None and index < len(rows)
        row = rows[index] if present else None
        missing = rows is not None and not present
        slots.append({'source_index': index, 'item_id': item['item_id'], 'reader_id': loaded['reader_id'],
            'phase': item['phase'], 'source': {key: item[key] for key in SOURCE_FIELDS},
            'file_status': loaded['file_status'], 'matching_item_row_indices': None,
            'row_index': index if present else None, 'mechanical_status': 'unavailable' if missing else 'invalid',
            'scientific_status': 'unavailable' if missing else 'invalid',
            'return_status': ('null_row' if row is None else 'returned_row') if present else
                             'missing_row' if missing else loaded['pre_limit_file_status'],
            'declared_reading_status': row.get('reading_status') if isinstance(row, dict) else None,
            'validation_complete': False, 'diagnostics': [diag('validation_not_completed',
                disposition=loaded['validation_disposition'], identity_index_not_completed=True)],
            'annotation': row})
    return slots


def make_slots(loaded, items, validator):
    if loaded['file_status'] == 'validation_work_bound':
        return _work_limited_slots(loaded, items)
    budget = _DiagnosticBudget()
    original_diagnostics = loaded['diagnostics']
    loaded['diagnostics'] = _BoundedDiagnostics(budget, {'input': loaded['reader_id']})
    try:
        loaded['diagnostics'].extend(original_diagnostics)
        slots = _make_slots(loaded, items, validator, budget)
    except _ValidationLimit as error:
        loaded.update(pre_limit_file_status=loaded['file_status'], file_status='validation_work_bound', validation_complete=False,
                      validation_disposition='diagnostic_work_bound')
        loaded['diagnostics'] = [*loaded['diagnostics'], error.diagnostic]
        loaded['retained_validation_diagnostics'] = budget.segments[1:]
        return _work_limited_slots(loaded, items)
    if loaded['rows'] is not None:
        loaded.update(validation_complete=True, validation_disposition='completed')
    return slots


def _make_slots(loaded, items, validator, budget):
    rows = loaded['rows']
    indices = {}
    known = {item['item_id'] for item in items}
    if rows is not None:
        for index, row in enumerate(rows):
            if isinstance(row, dict) and isinstance(row.get('item_id'), str):
                indices.setdefault(row['item_id'], []).append(index)
                if row['item_id'] not in known:
                    loaded['diagnostics'].append(diag('foreign_item_id', str(index), item_id=row['item_id']))
            if index >= len(items):
                loaded['diagnostics'].append(diag('extra_unassigned_row', str(index)))
                loaded['diagnostics'].extend(diag('extra_row_schema_invalid', str(index),
                    annotation_path=list(error.absolute_path), detail=error.message)
                    for error in validator.iter_errors(row))
        if len(rows) != len(items):
            loaded['diagnostics'].append(diag('row_count_mismatch', expected=len(items), observed=len(rows)))
        for item_id, positions in indices.items():
            if len(positions) > 1:
                loaded['diagnostics'].append(diag('duplicate_item_id', item_id=item_id, row_indices=positions))
    slots = []
    for index, item in enumerate(items):
        slot = {'source_index': index, 'item_id': item['item_id'], 'reader_id': loaded['reader_id'],
                'phase': item['phase'], 'source': {key: item[key] for key in SOURCE_FIELDS},
                'file_status': loaded['file_status'],
                'matching_item_row_indices': indices.get(item['item_id'], []),
                'row_index': index if rows is not None and index < len(rows) else None,
                'mechanical_status': None, 'scientific_status': None, 'return_status': None,
                'declared_reading_status': None, 'diagnostics': [], 'annotation': None,
                'validation_complete': rows is not None and index < len(rows)}
        if rows is None:
            slot['return_status'] = loaded['file_status']
            absent = loaded['file_status'] in ('missing_file', 'missing_pin')
            slot['mechanical_status'] = 'unavailable' if absent else 'invalid'
            slot['scientific_status'] = 'unavailable' if absent else 'invalid'
            slot['diagnostics'].append(diag(loaded['file_status']))
        elif index >= len(rows):
            slot.update(return_status='missing_row', mechanical_status='unavailable', scientific_status='unavailable')
            slot['diagnostics'].append(diag('missing_row'))
        else:
            row = rows[index]
            slot['annotation'] = row  # Exact parsed annotation; never repair or reconcile.
            slot['return_status'] = 'null_row' if row is None else 'returned_row'
            slot['diagnostics'] = validate_row(row, item, loaded['reader_id'], validator, budget)
            if row is None:
                slot['diagnostics'].append(diag('null_row'))
            if isinstance(row, dict):
                slot['declared_reading_status'] = row.get('reading_status')
                if isinstance(row.get('item_id'), str) and len(indices.get(row['item_id'], [])) > 1:
                    slot['diagnostics'].append(diag('duplicate_item_id', row_indices=indices[row['item_id']]))
            if len(indices.get(item['item_id'], [])) != 1:
                slot['diagnostics'].append(diag('expected_item_identity_missing_or_duplicate',
                                               row_indices=indices.get(item['item_id'], [])))
            if len(rows) > len(items):
                slot['diagnostics'].append(diag('extra_rows_prevent_return_assignment_acceptance'))
            slot['mechanical_status'] = 'invalid' if slot['diagnostics'] else 'valid'
            if slot['mechanical_status'] == 'invalid':
                slot['scientific_status'] = 'invalid'
            else:
                states = [row['type_summaries'][kind][frame]['state'] for kind in TYPES for frame in FRAMES]
                slot['scientific_status'] = ('unavailable' if row['reading_status'] == 'unavailable' else
                    'unresolved' if row['reading_status'] == 'coding_unresolved' or 'coding_unresolved' in states else 'valid')
        slots.append(slot)
    return slots


def cell_side(slot, kind, frame):
    if slot['mechanical_status'] != 'valid':
        return {'status': 'input_' + slot['mechanical_status'], 'return_status': slot['return_status'],
                'declared_reading_status': slot['declared_reading_status'], 'value': None}
    value = slot['annotation']['type_summaries'][kind][frame]
    state = value['state']
    # Preserve cell-level resolution; separately retain the reader's whole-row status.
    status = 'resolved' if state in RESOLVED else 'unresolved' if state == 'coding_unresolved' else 'unavailable'
    return {'status': status, 'return_status': slot['return_status'],
            'declared_reading_status': slot['declared_reading_status'],
            'value': {field: value[field] for field in FIELDS}}


def count_cells(cells):
    counts = {'assigned_cells': len(cells), 'status_pairs': {}, 'strata': {}}
    for cell in cells:
        pair = cell['a']['status'] + '__' + cell['b']['status']
        counts['status_pairs'][pair] = counts['status_pairs'].get(pair, 0) + 1
        group = counts['strata'].setdefault(cell['comparison_stratum'],
            {'cells': 0, 'state_pairs': {}, 'endpoint_pairs': {field: {} for field in FIELDS[1:]},
             'exact_equal': {field: 0 for field in FIELDS}, 'exact_different': {field: 0 for field in FIELDS}})
        group['cells'] += 1
        if cell['exact_equal'] is None:
            continue
        for field in FIELDS:
            equal = cell['exact_equal'][field]
            group['exact_equal' if equal else 'exact_different'][field] += 1
            pair = json.dumps([cell['a']['value'][field], cell['b']['value'][field]], separators=(',', ':'))
            pairs = group['state_pairs'] if field == 'state' else group['endpoint_pairs'][field]
            pairs[pair] = pairs.get(pair, 0) + 1
    return counts


def compare(loaded_a, loaded_b, items, validator):
    slots_a = make_slots(loaded_a, items, validator)
    slots_b = make_slots(loaded_b, items, validator)
    cells = []
    disagreements = []
    for index, item in enumerate(items):
        for kind in TYPES:
            for frame in FRAMES:
                a, b = cell_side(slots_a[index], kind, frame), cell_side(slots_b[index], kind, frame)
                comparable = a['value'] is not None and b['value'] is not None
                if not comparable:
                    stratum = 'input_not_comparable'
                elif a['status'] == b['status']:
                    stratum = 'both_' + a['status']
                else:
                    stratum = 'different_availability_or_resolution'
                cell = {'source_index': index, 'item_id': item['item_id'], 'type': kind,
                        'type_group': 'audience_control' if kind == 'audience_demand' else 'primary',
                        'frame': frame, 'phase': item['phase'],
                        'source': {key: item[key] for key in SOURCE_FIELDS},
                        'a': a, 'b': b, 'comparison_stratum': stratum,
                        'exact_equal': {field: a['value'][field] == b['value'][field] for field in FIELDS} if comparable else None}
                cells.append(cell)
                if comparable and not all(cell['exact_equal'].values()):
                    disagreements.append(cell)
    summary = {'all': count_cells(cells), 'by_type_group': {}, 'by_type': {}, 'by_frame': {},
               'by_phase': {}, 'by_type_group_frame_phase': {}}
    for dimension, field, values in (
            ('by_type_group', 'type_group', ('primary', 'audience_control')),
            ('by_type', 'type', TYPES), ('by_frame', 'frame', FRAMES),
            ('by_phase', 'phase', PHASES)):
        summary[dimension] = {value: count_cells([cell for cell in cells if cell[field] == value]) for value in values}
    for group in ('primary', 'audience_control'):
        for frame in FRAMES:
            for phase in PHASES:
                summary['by_type_group_frame_phase']['/'.join((group, frame, phase))] = count_cells(
                    [c for c in cells if (c['type_group'], c['frame'], c['phase']) == (group, frame, phase)])
    for field, values in (('channel_availability', AVAILABILITY), ('text_completeness', COMPLETENESS),
                          ('context_status', CONTEXT_STATES), ('target_channel', (items[0]['target_channel'],))):
        summary['by_source_' + field] = {value: count_cells([c for c in cells if c['source'][field] == value])
                                                 for value in values}
    summary['by_phase_completeness'] = {phase + '/' + completeness: count_cells(
        [c for c in cells if c['phase'] == phase and c['source']['text_completeness'] == completeness])
        for phase in PHASES for completeness in COMPLETENESS}
    slots = slots_a + slots_b
    return {'status': 'mechanical_descriptive_comparison_semantic_review_pending',
            'assigned_reading_slots': len(slots), 'assigned_paired_cells': len(cells),
            'slot_status_counts': {state: sum(slot['scientific_status'] == state for slot in slots)
                                   for state in ('valid', 'invalid', 'unresolved', 'unavailable')},
            'source_assignment_counts': {field: dict(Counter(item[field] for item in items))
                                        for field in ('phase', 'target_channel', 'channel_availability',
                                                      'text_completeness', 'context_status')},
            'inputs': [{key: value for key, value in loaded.items() if key != 'rows'}
                       for loaded in (loaded_a, loaded_b)],
            'slots': slots, 'counts': summary, 'cells': cells, 'exact_disagreements': disagreements,
            'limitations': [
                'Mechanical validity is not semantic validity, factual truth, or instrument certification.',
                'Initial annotations and raw bytes are retained; no reconciliation or author-key comparison.',
                'Mixed affirmative endpoints are not derived from free-text evidence-limit or lifecycle qualification.',
                'Whole-row reading_status and per-cell resolution are both retained; resolved cells are compared by their declared cell states.',
                'Availability and coding-unresolved equality remain outside substantive resolved-state agreement.',
                'Original responses, ASK answers and channels remain linked; no prevalence, reliability, detector accuracy, independent-cluster inference, kappa, p-values or confidence intervals.',
                'Partial and completeness-unknown negatives describe the supplied target string only; recorded completion is collector attribution, not native qualification.',
                'Matching states/endpoints do not establish identical propositions, evidence boundaries, interpretations or independent semantic judgments.',
                'Callers retain all pinned raw bytes, including invalid returns; this pure function does not write or repair them.',
                'Work bounds are operational admission limits, not schema or scientific judgments. A work-limited return is not compared; assigned parsed rows and raw pins remain available, and diagnostic enumeration is explicitly incomplete.',
                'Readers are model-assisted, not human references or claimed independent models.',
            ]}
