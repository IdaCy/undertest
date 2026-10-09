"""Pure checks for retained technical qualification evidence.

These checks neither execute work nor grant admission. A finite dispatcher's
exhaustion event can precede its last completion: it says there is no more work
to admit, while already claimed work may still be running. Closure therefore
comes from the complete claim/final/success join, not the event alone. Callers
must separately verify retained result custody and current ownership/resources.
"""

import hashlib
import json


def _canonical_sha(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def qualify_finite_phase_journal(raw, ids, jobs, config, summaries):
    """Validate a complete successful finite phase against frozen inputs.

    ``summaries`` maps each assigned ID to its independently verified summary
    SHA256. Technical failures remain failures requiring separate review; they
    are never converted into valid measurements by this qualification check.
    Journal ordering uses append order, without comparing clocks across hosts.
    """
    if not ids or len(set(ids)) != len(ids) or len(jobs) != len(ids):
        raise ValueError("unique_nonempty_frozen_phase_required")
    if [job["allocation"]["run_id"] for job in jobs] != ids or set(summaries) != set(ids):
        raise ValueError("frozen_phase_identity_mismatch")
    if not raw or not raw.endswith(b"\n"):
        raise ValueError("whole_complete_journal_required")
    rows = [json.loads(line) for line in raw.splitlines()]
    claims = [(i, row) for i, row in enumerate(rows) if row["event"] == "claimed"]
    finals = [(i, row) for i, row in enumerate(rows) if row["event"] == "episode_finished"]
    successes = [(i, row) for i, row in enumerate(rows) if row["event"] == "route_success"]
    if [row["run_id"] for _, row in claims] != ids:
        raise ValueError("whole_phase_claim_final_identity")
    for events in (finals, successes):
        if len(events) != len(ids) or {row["run_id"] for _, row in events} != set(ids):
            raise ValueError("whole_phase_terminal_identity")
    for (_, claim), job in zip(claims, jobs):
        if claim["job_sha256"] != _canonical_sha(job) or claim["config_sha256"] != _canonical_sha(config):
            raise ValueError("claim_frozen_binding")
    by_final = {row["run_id"]: (i, row) for i, row in finals}
    by_success = {row["run_id"]: i for i, row in successes}
    for claim_index, claim in claims:
        run_id = claim["run_id"]
        final_index, final = by_final[run_id]
        if final.get("technical_failure") is not False or final.get("replayed") is not False or final["summary_sha256"] != summaries[run_id]:
            raise ValueError("terminal_failure_or_summary_mismatch")
        if not claim_index < final_index < by_success[run_id]:
            raise ValueError("claim_final_success_order")
    forbidden = {
        "route_failure", "route_quarantined", "route_requalified",
        "unresolved_prior_attempt", "unresolved_current_attempt",
        "existing_completed_summary", "extension_block_created", "controller_error",
    }
    if any(row["event"] in forbidden for row in rows):
        raise ValueError("technical_or_extra_evidence_requires_separate_reconciliation")
    last_claim = claims[-1][0]
    exhausted = [i for i, row in enumerate(rows) if row["event"] == "waiting_for_operator_pause_no_eligible_extensions"]
    if not exhausted or min(exhausted) <= last_claim:
        raise ValueError("finite_exhaustion_after_last_claim_required")
    first_exhausted = min(exhausted)
    last_final = max(i for i, _ in finals)
    return {
        "claimed": len(claims), "finals": len(finals), "route_successes": len(successes),
        "lifetime_route_failures": 0, "quarantines": 0, "all_frozen_claims_in_order": True,
        "exhaustion": {
            "first_event_index": first_exhausted,
            "at": rows[first_exhausted].get("at"),
            "last_claim_index": last_claim, "last_final_index": last_final,
            "preceded_last_final": first_exhausted < last_final,
            "unfinished_at_first_event": sum(i > first_exhausted for i, _ in finals),
        },
    }
