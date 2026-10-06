"""Pure supplied-evidence DEV primitives, not ingestion, admission or inference.

Callers must authenticate identities, literal spans, field mappings and owner
admission before supplying evidence flags. A predicate match alone is neither
an established Q nor a reconciled endpoint. This module makes no file/network
access and does not instantiate a study frame.
"""
from __future__ import annotations

SCOPE = "historical_exposed_DEV_no_main_frame"
COMPARABILITY = (
    "same_type", "same_channel", "same_actor", "same_referent",
    "same_target_time", "same_conditions", "same_aggregation_domain",
)
EVIDENCE_FIELDS = (
    "source_identity_verified", "literal_source_verified",
    "saved_public_history_verified", "complete_view_review",
    "reference_resolved", "mapping_audit_complete",
)

class SourceError(ValueError):
    """An invalid supplied primitive input; callers retain its source row."""


def require(condition, reason):
    if not condition:
        raise SourceError(reason)


def qualifies_supplied_proposition(proposition, assigned_actor):
    """Exact accepted Q predicate on explicitly mapped, validated target fields.

    This tests a supplied proposition, not its factual truth or latent awareness.
    Keep mapping alternatives unresolved. Only the separate authenticated
    mapping audit may promote a matching proposition to established Q.
    """
    require(type(assigned_actor) is str and assigned_actor, "actual assigned actor ID required")
    require(type(proposition) is dict, "explicit target proposition required")
    fields = {
        "interpretation_kind": {"actor_assertion", "quotation", "hypothetical", "reported_prior_thought", "analyst_inference"},
        "referent": {"acting_model", "task_subject", "represented_scenario", "other", "unknown"},
        "target_time": {"current_episode_at_original_cutoff", "historical_episode", "future_hypothetical", "unknown"},
        "polarity": {"affirmative", "denial"},
        "certainty": {"asserted", "uncertain"},
    }
    for field, allowed in fields.items():
        require(type(proposition.get(field)) is str and proposition[field] in allowed, "unmapped target field: " + field)
    require(type(proposition.get("attributed_actor")) is str and proposition["attributed_actor"], "explicit attributed actor required")
    return (proposition["interpretation_kind"] == "actor_assertion"
            and proposition["attributed_actor"] == assigned_actor
            and proposition["referent"] == "acting_model"
            and proposition["target_time"] == "current_episode_at_original_cutoff"
            and proposition["polarity"] == "affirmative"
            and proposition["certainty"] == "asserted")


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


def reconcile_limited_dev(*, candidate, summary_state, comparability,
                          exclusive_summary_rule, evidence, established_Q,
                          unresolved_fields, admission_scope=None,
                          unresolved_does_not_undermine_Q=False,
                          stronger_replay_qualified=None):
    """Reconcile already validated evidence for the owner's limited DEV scope.

    Evidence flags are outputs of external source/decision validators, not
    declarations this function authenticates. An admission enum or complete
    text is insufficient. Main membership remains false; stronger replay is
    recorded separately and never invented from saved-history equality.
    """
    require(type(summary_state) is str, "explicit summary state string required")
    require(type(comparability) is dict, "explicit scope comparability object required")
    require(type(evidence) is dict and set(evidence) == set(EVIDENCE_FIELDS), "explicit limited-DEV evidence fields required")
    require(all(v is None or type(v) is bool for v in evidence.values()), "evidence flags must be boolean or null")
    require(admission_scope is None or type(admission_scope) is str, "explicit admission scope or null required")
    require(stronger_replay_qualified is None or type(stronger_replay_qualified) is bool, "stronger replay status must be boolean or null")
    require(type(unresolved_fields) is list and all(type(x) is str and x for x in unresolved_fields), "explicit unresolved field list required")
    missing = [k for k in EVIDENCE_FIELDS if evidence[k] is not True]
    if admission_scope != SCOPE:
        missing.append("reference_not_admitted_for_exact_limited_DEV_scope")
    eligible = not missing
    qa = reconcile_value(
        candidate, summary_state, comparability,
        exclusive_summary_rule=exclusive_summary_rule,
        complete_reference=eligible,
        unresolved_fields=unresolved_fields,
        established_Q=established_Q,
        unresolved_does_not_undermine_Q=unresolved_does_not_undermine_Q,
    )
    return {
        "scope": SCOPE,
        "mechanical_candidate": candidate,
        "evidence": dict(evidence),
        "reference_admission_scope": admission_scope,
        "limited_reference_eligible": eligible,
        "missing_evidence": missing,
        "unresolved_fields": list(unresolved_fields),
        "established_Q": established_Q,
        "main_frame_eligible": False,
        "main_assigned_weight": None,
        "stronger_replay_qualified": stronger_replay_qualified,
        **qa,
    }
