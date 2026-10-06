"""Prospective pure adapter. Qualification is conditional on externally trusted inputs.

No file/network access, provenance authentication, source adoption or MAIN authority.
"""
from fractions import Fraction
import hashlib
import json


_UNDEFINED = ("invalid_structural", "unavailable_unclassified", "unknown_original_gate")
_COMMON_STATES = ("observed_score", "missing_defined_score", "invalid_structural",
                  "unavailable_unclassified")
_POLICIES = {
    "P1_acquisition": (_COMMON_STATES, "not_applicable"),
    "P2_functional_handoff": (
        _COMMON_STATES + ("unknown_original_gate", "declared_failed_work_gate"),
        "failed_original_zero_for_valid_functional_arms_v1"),
}
_CERT_FIELDS = frozenset((
    "schema_version", "id", "endpoint", "source_id", "source_sha256", "domain_sha256",
    "scale", "allowed_states", "gate_policy", "shared_across_comparisons",
    "outcome_independent", "zero_lower_bound", "positive_scale",
    "adopted_for_MAIN", "certified_main_source_groups"))
_CONTEXT_FIELDS = frozenset((
    "original_id", "source_assignment_id", "source_id", "checkpoint_id",
    "registered_condition_id", "branch_record_id", "arm_role", "original_gate",
    "functional_validity"))
_JOIN_FIELDS = ("original_id", "source_assignment_id", "source_id", "checkpoint_id",
                "registered_condition_id", "original_gate")
_AUTH_BOUNDARY = "external_owner_manifest_verification_required"


def rational(value):
    """Exact integers, decimal/rational strings and Fraction only; no bool/float."""
    if type(value) not in (str, int, Fraction):
        raise ValueError("exact rational required")
    return Fraction(value)


def _text(value):
    return type(value) is str and bool(value.strip())


def _digest(value):
    return (type(value) is str and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _certificate(value):
    """Validate syntax/policy, not truth or authority; snapshot exact identity."""
    if type(value) is not dict or set(value) != _CERT_FIELDS:
        return None, "unqualified_certificate"
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not all(_text(value[k]) for k in ("id", "source_id", "endpoint"))
            or value["endpoint"] not in _POLICIES
            or not all(_digest(value[k]) for k in ("source_sha256", "domain_sha256"))):
        return None, "unqualified_certificate"
    states, gate_policy = _POLICIES[value["endpoint"]]
    if (type(value["allowed_states"]) is not list
            or any(type(s) is not str for s in value["allowed_states"])
            or tuple(value["allowed_states"]) != states
            or type(value["gate_policy"]) is not str or value["gate_policy"] != gate_policy):
        return None, "unqualified_endpoint_policy"
    if (not all(value[k] is True for k in
                ("shared_across_comparisons", "outcome_independent", "zero_lower_bound"))
            or type(value["positive_scale"]) is not bool
            or value["adopted_for_MAIN"] is not False
            or type(value["certified_main_source_groups"]) is not int
            or value["certified_main_source_groups"] != 0
            or type(value["scale"]) is not str):
        return None, "unqualified_certificate"
    try:
        scale = rational(value["scale"])
    except (ValueError, ZeroDivisionError, OverflowError):
        return None, "invalid_certificate_scale"
    if scale < 0:
        return None, "invalid_negative_scale"
    if str(scale) != value["scale"]:
        return None, "noncanonical_certificate_scale"
    if value["positive_scale"] is not (scale > 0):
        return None, "inconsistent_scale_attestation"
    certificate = dict(value)
    certificate["allowed_states"] = list(value["allowed_states"])
    status = "prospectively_inapplicable_zero_scale" if scale == 0 else None
    return certificate, status


def _context(value, source_id):
    if (type(value) is not dict or set(value) != _CONTEXT_FIELDS
            or not all(_text(v) for v in value.values())
            or value["arm_role"] not in ("full", "fresh")
            or value["original_gate"] not in ("succeeded", "failed", "unknown")
            or value["functional_validity"] not in ("valid", "invalid", "unknown")):
        return None, "malformed_pair_context"
    if value["source_id"] != source_id:
        return None, "context_source_mismatch"
    return dict(value), None


def normalize(raw, state, certificate, context=None):
    """Check supplied exact score/state; preserve raw evidence and missingness.

    P2 needs context before any value can qualify. Its truth must be established
    outside this function against retained assignments and branch provenance.
    """
    supplied = certificate if type(certificate) is dict else {}
    result = {"raw": raw, "input_state": state, "normalized": None,
              "completion_interval": None, "qualified": False,
              "status": "unqualified_certificate", "certificate": None,
              "certificate_id": supplied.get("id"), "scale": supplied.get("scale"),
              "certificate_fingerprint": None,
              "context": dict(context) if type(context) is dict else context,
              "authentication": _AUTH_BOUNDARY, "MAIN_qualified": False,
              "task_success_inferred": False}
    cert, error = _certificate(certificate)
    if cert is not None:
        result["certificate"] = cert
        result["certificate_fingerprint"] = hashlib.sha256(
            json.dumps(cert, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if error is not None:
        result["status"] = error
        return result
    if type(state) is not str:
        result["status"] = "malformed_score_state"
        return result
    if state not in cert["allowed_states"]:
        result["status"] = "state_not_allowed_for_endpoint"
        return result
    if state in _UNDEFINED:
        result["status"] = state
        return result
    if cert["endpoint"] == "P2_functional_handoff":
        ctx, error = _context(context, cert["source_id"])
        if error is not None:
            result["status"] = error
            return result
        result["context"] = ctx
        if ctx["functional_validity"] != "valid":
            result["status"] = ("invalid_functional_arm" if ctx["functional_validity"] == "invalid"
                                else "unknown_functional_validity")
            return result
        if ctx["original_gate"] == "unknown":
            result["status"] = "unknown_original_gate"
            return result
        if ctx["original_gate"] == "failed" and state != "declared_failed_work_gate":
            result["status"] = "failed_original_requires_declared_gate"
            return result
        if state == "declared_failed_work_gate" and ctx["original_gate"] != "failed":
            result["status"] = "inconsistent_original_gate"
            return result
    elif context is not None:
        result["status"] = "unexpected_pair_context_for_P1"
        return result
    if state == "missing_defined_score":
        if raw is not None:
            result["status"] = "inconsistent_missing_state"
            return result
        result.update(status=state, qualified=True, completion_interval=["0", "1"])
        return result
    try:
        score = rational(raw)
    except (ValueError, ZeroDivisionError, OverflowError):
        result["status"] = "invalid_numeric_score"
        return result
    scale = rational(cert["scale"])
    if not 0 <= score <= scale:
        result["status"] = "outside_certified_support"
        return result
    if state == "declared_failed_work_gate" and score != 0:
        result["status"] = "inconsistent_gate_score"
        return result
    value = str(score / scale)
    result.update(status=state, qualified=True, normalized=value,
                  completion_interval=[value, value])
    return result


def _exact_equal(left, right):
    # bool is a subclass of int: ordinary dictionary equality can silently
    # accept 1 for True or 0 for False. Cached records need exact field types.
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        return (left.keys() == right.keys()
                and all(_exact_equal(left[k], right[k]) for k in left))
    if type(left) is list:
        return len(left) == len(right) and all(_exact_equal(a, b) for a, b in zip(left, right))
    return left == right


def _checked_record(record):
    """Recompute output; reject edited, incomplete or fabricated cached results."""
    if type(record) is not dict or not all(k in record for k in
                                            ("raw", "input_state", "certificate", "context")):
        return None, "malformed_record"
    expected = normalize(record["raw"], record["input_state"],
                         record["certificate"], record["context"])
    if not expected["qualified"]:
        return None, "unqualified_record"
    # Once recomputation qualifies, raw/certificate/context have exact supported
    # types. A cached record must match every field, not merely a qualified flag.
    try:
        same = _exact_equal(record, expected)
    except Exception:
        same = False
    if not same:
        return None, "inconsistent_record"
    return expected, None


def paired(full, fresh):
    """P2 full-minus-fresh; context checks remain conditional on trusted inputs.

    This cannot authenticate IDs, source certificates, original gates, valid-arm
    judgments or role labels; the owner must bind them to retained provenance.
    """
    result = {"full": full, "fresh": fresh, "difference": None,
              "completion_interval": None, "qualified": False,
              "status": "unqualified_pair", "authentication": _AUTH_BOUNDARY,
              "MAIN_qualified": False}
    left, error = _checked_record(full)
    if error:
        result["status"] = "full_" + error
        return result
    right, error = _checked_record(fresh)
    if error:
        result["status"] = "fresh_" + error
        return result
    if (left["certificate_fingerprint"] != right["certificate_fingerprint"]
            or left["certificate"] != right["certificate"]):
        result["status"] = "unmatched_certificate_identity"
        return result
    if left["certificate"]["endpoint"] != "P2_functional_handoff":
        result["status"] = "endpoint_not_full_fresh_pair"
        return result
    lc, rc = left["context"], right["context"]
    mismatch = [k for k in _JOIN_FIELDS if lc[k] != rc[k]]
    if mismatch:
        result.update(status="pair_context_mismatch", mismatched_fields=mismatch)
        return result
    if lc["arm_role"] != "full" or rc["arm_role"] != "fresh":
        result["status"] = "incorrect_full_fresh_roles"
        return result
    if lc["branch_record_id"] == rc["branch_record_id"]:
        result["status"] = "reused_branch_record"
        return result
    fl, fu = map(rational, left["completion_interval"])
    rl, ru = map(rational, right["completion_interval"])
    result.update(qualified=True, status="paired_conditional_on_trusted_inputs",
                  completion_interval=[str(fl - ru), str(fu - rl)])
    if left["normalized"] is not None and right["normalized"] is not None:
        result["difference"] = str(rational(left["normalized"]) - rational(right["normalized"]))
    return result
