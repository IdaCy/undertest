from collections import Counter
from pathlib import Path


def audit_samples(rows: list[dict], expected_ids: list[str]) -> dict:
    latest = [row for row in rows if row.get("latest_attempt") and "sample_id" in row]
    counts = Counter(str(row["sample_id"]) for row in latest)
    expected = set(expected_ids)
    observed = set(counts)
    failures = []
    for row in latest:
        reasons = []
        if row.get("sample_status") != "completed":
            reasons.append(row.get("sample_status", "unknown_status"))
        if row.get("error"):
            reasons.append("sample_error")
        if not row.get("score.development_outcomes.value"):
            reasons.append("missing_scores")
        if row.get("artifact_export_errors") not in (None, "", "[]"):
            reasons.append("artifact_export_error")
        path = Path(row.get("artifacts", "")) / "development"
        if not all((path / name).is_file() for name in ("evidence.json", "outcomes.json", "model_output.json")):
            reasons.append("missing_evidence")
        if reasons:
            failures.append({"sample_id": str(row["sample_id"]), "reasons": reasons})
    missing = sorted(expected - observed)
    unexpected = sorted(observed - expected)
    duplicates = sorted(key for key, count in counts.items() if count != 1)
    return {
        "schema_version": 1,
        "complete": not (missing or unexpected or duplicates or failures),
        "expected_count": len(expected_ids), "latest_count": len(latest),
        "missing_ids": missing, "unexpected_ids": unexpected,
        "duplicate_ids": duplicates, "failed_samples": failures,
        "interpretation": "Execution and evidence completeness only; malformed actions and generation truncation remain recorded scientific outcomes, not automatic retry criteria.",
    }
