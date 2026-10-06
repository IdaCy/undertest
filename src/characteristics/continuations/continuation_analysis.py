"""Fixed-parent summaries from independently validated continuation records."""

from collections import Counter

from signals.validation.calibration import categorical_within_case_disagreement


ARMS = ("original", "new-1", "new-2", "new-3")
COMPARISONS = ("historical_label", "final_artifact", "accepted_tool_sequence",
               "first_request_bytes", "first_response_bytes")
STATUSES = {"completed", "generation_limit", "call_limit", "token_limit",
            "error", "interrupted", "uncalled", "unknown", "invalid_evidence"}
COUNTERS = ("prompt_tokens", "completion_tokens", "completed_calls")


def _nullable_bool(value):
    return value is None or type(value) is bool


def _validate(rows):
    if not isinstance(rows, list) or len(rows) != 32:
        raise ValueError("Retain all 32 assigned branches")
    seen = set()
    for row in rows:
        fields = {"branch_id", "parent_id", "family", "case_index", "arm",
                  "historical_label", "episode_status", "complete_history",
                  "verified_completed_episode", "functional_correct",
                  "constraint_compliant", "parent_comparisons", "new_usage"}
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError("Invalid normalized continuation fields")
        family, index, arm = row["family"], row["case_index"], row["arm"]
        if (family not in ("ledger", "validator") or type(index) is not int
                or not 0 <= index < 4 or arm not in ARMS):
            raise ValueError("Branch falls outside the fixed design")
        parent = f"{family}-development-{index:03d}-r000"
        branch = parent + "--" + arm
        if row["parent_id"] != parent or row["branch_id"] != branch or branch in seen:
            raise ValueError("Inconsistent or duplicate branch identity")
        seen.add(branch)
        if row["episode_status"] not in STATUSES:
            raise ValueError("Unknown episode disposition")
        if any(type(row[key]) is not bool for key in ("complete_history", "verified_completed_episode")):
            raise ValueError("History verification flags must be booleans")
        if any(not _nullable_bool(row[key]) for key in ("historical_label", "functional_correct", "constraint_compliant")):
            raise ValueError("Outcomes must be booleans or explicit missing values")
        verified = row["verified_completed_episode"]
        if verified and (not row["complete_history"] or row["episode_status"] != "completed"):
            raise ValueError("Verified completion needs a complete normally terminated history")
        if row["historical_label"] is False and not verified:
            raise ValueError("Historical negatives require verified completed episodes")
        comparisons = row["parent_comparisons"]
        if not isinstance(comparisons, dict) or set(comparisons) != set(COMPARISONS):
            raise ValueError("Every comparison must remain represented")
        if any(not _nullable_bool(value) for value in comparisons.values()):
            raise ValueError("Comparisons must be booleans or explicit missing values")
        if row["historical_label"] is None and comparisons["historical_label"] is not None:
            raise ValueError("An unknown historical label cannot supply a label comparison")
        usage = row["new_usage"]
        if not isinstance(usage, dict) or set(usage) != set(COUNTERS):
            raise ValueError("Supply inherited-excluded branch usage counters")
        for value in usage.values():
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Usage must be a nonnegative integer or missing")
        if usage["completed_calls"] is not None and usage["completed_calls"] > 5:
            raise ValueError("A branch has at most five new calls")
        if row["episode_status"] in {"uncalled", "invalid_evidence"}:
            if (any(row[key] is not None for key in ("historical_label", "functional_correct", "constraint_compliant"))
                    or row["complete_history"] or verified
                    or any(value is not None for value in comparisons.values())):
                raise ValueError("Uncalled or invalid evidence cannot supply accepted outcomes")
        if row["episode_status"] == "uncalled" and any(value is not None for value in usage.values()):
            raise ValueError("Uncalled branches have missing usage, not synthetic observations")


def _binary(values):
    assigned = len(values)
    positive = sum(value is True for value in values)
    negative = sum(value is False for value in values)
    unknown = assigned - positive - negative
    return {"assigned": assigned, "true": positive, "false": negative, "unknown": unknown,
            "available_rate": positive / (positive + negative) if positive + negative else None,
            "all_assigned_lower": positive / assigned if assigned else None,
            "all_assigned_upper": (positive + unknown) / assigned if assigned else None}


def _scope(rows):
    return {
        "assigned": len(rows), "parents": len({row["parent_id"] for row in rows}),
        "status_counts": dict(Counter(row["episode_status"] for row in rows)),
        "complete_histories": sum(row["complete_history"] for row in rows),
        "verified_completed_episodes": sum(row["verified_completed_episode"] for row in rows),
        "outcomes": {key: _binary([row[key] for row in rows]) for key in
                     ("historical_label", "functional_correct", "constraint_compliant")},
        "parent_comparisons": {key: _binary([row["parent_comparisons"][key] for row in rows]) for key in COMPARISONS},
        "new_usage": {key: {"known_branches": sum(row["new_usage"][key] is not None for row in rows),
                            "missing_branches": sum(row["new_usage"][key] is None for row in rows),
                            "sum_over_known_branches": sum(row["new_usage"][key] for row in rows if row["new_usage"][key] is not None)}
                      for key in COUNTERS},
    }


def _disagreement(rows):
    records = [{"assignment_id": row["branch_id"], "case_id": row["parent_id"],
                "family": row["family"], "category": None if row["historical_label"] is None
                else "event" if row["historical_label"] else "no_event"} for row in rows]
    return categorical_within_case_disagreement(records, ("no_event", "event"))


def summarize_continuations(rows):
    """Summarize the fixed eight-parent unchanged-only design.

    Callers must bind the reviewed producer, host replay, labels, canonical
    comparisons and inherited-excluded usage before constructing these rows.
    Raw response equality is forensic and includes provider IDs and timestamps;
    it does not stand in for semantic or behavioral equality.
    """
    _validate(rows)
    new = [row for row in rows if row["arm"] != "original"]
    original = [row for row in rows if row["arm"] == "original"]
    families = {}
    for family in ("ledger", "validator"):
        chosen = [row for row in rows if row["family"] == family]
        selected_new = [row for row in chosen if row["arm"] != "original"]
        families[family] = {"new": _scope(selected_new),
                            "original": _scope([row for row in chosen if row["arm"] == "original"]),
                            "new_seed_disagreement": _disagreement(selected_new)}
    return {
        "schema_version": "sigres-unchanged-continuation-summary-v1",
        "population": "Eight fixed development parents;24 new-seed branches and8 original-schedule controls",
        "weighting": "Equal parents and equal three new branches per parent; three dependent pairs per parent",
        "uncertainty": "Finite assigned-parent missingness bounds; no sampling intervals or independent-pair inference",
        "endpoint": "historical_completion_dependent_accepted_shortcut",
        "finite_certificate_status": "unsupported_continuation",
        "all_assigned": _scope(rows), "new": _scope(new), "original": _scope(original),
        "new_seed_disagreement": _disagreement(new), "families": families,
        "parents": {parent: {"new": _scope([row for row in new if row["parent_id"] == parent]),
                              "original": _scope([row for row in original if row["parent_id"] == parent])}
                    for parent in sorted({row["parent_id"] for row in rows})},
    }
