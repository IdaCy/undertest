"""Pure audit selection/triage helpers; never admit data or alter a split."""
import hashlib
import heapq
import json
import re
import unicodedata

SEED = "dtrai-source-audit-v1-20261001"
CORE_QUOTAS = {"train": 150, "development": 25, "capability_holdout": 25}
RISK_QUOTAS = {"multiple_outputs": 6, "rounding_or_approximation": 6, "units_or_percentage": 4,
               "missing_context_or_diagram": 6, "answer_hint_or_solution": 6, "proof_or_domain_contract": 4}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def rank(cluster, pool, purpose="core"):
    return sha(canonical([SEED, purpose, pool, cluster]))


def select_core(records, quotas=None):
    """Return fixed bottom-hash samples, independent of input order/model results.

    records contains one row per exact cluster, with representative_source_row_id,
    normalized_text_sha256, candidate_pool and scalar_parser_eligible. Existing
    pool labels are preserved. The caller must verify complete input hashes/counts.
    """
    quotas = CORE_QUOTAS if quotas is None else quotas
    if set(quotas) - set(CORE_QUOTAS) or any(type(n) is not int or not 0 <= n <= 256 for n in quotas.values()):
        raise ValueError("invalid finite audit quotas")
    heaps = {pool: [] for pool in quotas}
    seen, counts = set(), {pool: 0 for pool in quotas}
    for row in records:
        cluster, pool = row["normalized_text_sha256"], row["candidate_pool"]
        if not isinstance(cluster, str) or not re.fullmatch("[0-9a-f]{64}", cluster):
            raise ValueError("invalid cluster identity")
        if cluster in seen:
            raise ValueError("duplicate cluster identity; resolve input ledger before sampling")
        seen.add(cluster)
        if pool not in quotas or row["scalar_parser_eligible"] is not True:
            continue
        identity = row["representative_source_row_id"]
        if not isinstance(identity, str) or not re.fullmatch(r"train:[0-9]+", identity):
            raise ValueError("invalid source-row identity")
        counts[pool] += 1
        if quotas[pool] == 0:
            continue
        key = rank(cluster, pool)
        item = (-int(key, 16), cluster, identity)
        heapq.heappush(heaps[pool], item)
        if len(heaps[pool]) > quotas[pool]:
            heapq.heappop(heaps[pool])
    selected = []
    for pool, items in heaps.items():
        for _, cluster, identity in sorted(items, key=lambda item: rank(item[1], pool)):
            selected.append({"audit_case_id": "gold-" + rank(cluster, pool)[:24], "source_row_id": identity,
                             "normalized_text_sha256": cluster, "pool": pool, "selection_rank_sha256": rank(cluster, pool)})
    return {"selected": selected, "available": counts,
            "shortfalls": {pool: max(0, quota - counts[pool]) for pool, quota in quotas.items()},
            "status": "fixed_audit_selection_not_source_admission"}


def risk_flags(question):
    """Diagnostic flags only: a match is neither a rejection nor proven leakage."""
    if not isinstance(question, str):
        raise ValueError("question must be text")
    text = unicodedata.normalize("NFC", question).lower()
    patterns = {
        "multiple_outputs": r"\(a\).*\(b\)|\(i\).*\(ii\)|find both|find all|all possible values|ordered pair",
        "rounding_or_approximation": r"round(?:ed|ing)?|nearest|approximate|decimal places|significant figures|numerically",
        "units_or_percentage": r"percent|percentage|\bunits?\b|\b(?:cm|km|meters?|metres?|seconds?|degrees?)\b",
        "missing_context_or_diagram": r"diagram|figure|shown below|shown above|following table|attached|https?://|<img|!\[",
        "answer_hint_or_solution": r"(?:correct|final) answer\s*[:=]|\bsolution\s*:|\bhint\s*:|the answer is",
        "proof_or_domain_contract": r"\bprove\b|show that|justify|undefined|does not exist|no solution|complex roots",
    }
    return [name for name in RISK_QUOTAS if re.search(patterns[name], text, re.DOTALL)]


def overlap_fingerprints(text):
    """Candidate-retrieval keys; only the exact key represents exact text.

    Presentation/numeric-template keys may merge distinct math and require review.
    In particular they are never independent-component or automatic-split labels.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("overlap text must be nonempty")
    exact = " ".join(unicodedata.normalize("NFC", text).split())
    presentation = re.sub(r"\\(?:left|right)\b", "", exact)
    presentation = presentation.replace(r"\dfrac", r"\frac").replace(r"\tfrac", r"\frac")
    presentation = presentation.replace(r"\!", "").replace("$", "")
    presentation = re.sub(r"\s+", "", presentation)
    template = re.sub(r"\d+(?:\.\d+)?|\.\d+", "<number>", presentation)
    return {"exact_normalized_sha256": sha(exact.encode()),
            "presentation_candidate_sha256": sha(presentation.encode()),
            "numeric_template_candidate_sha256": sha(template.encode()),
            "classification": "retrieval_only_no_automatic_merge_or_exclusion"}
