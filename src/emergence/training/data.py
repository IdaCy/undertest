"""Development-only, un-tokenized construction of matched training records.

Source inputs are dictionaries with source_uri, source_revision, source_row_id,
source_text, source_usage="training_allowed", license, and private_gold. A source
revision must be an immutable hexadecimal digest. Source permission is a caller
attestation, not a license or contamination audit performed by this module.

Candidates have candidate_id, task_id, response, substantive (a bool), and score.
The score dictionary follows MathReward.to_dict(): status, reward, correctness,
validity, parse_status, finish_reason, transport_status, scorer_version, and reason.
Additional candidate provenance is retained in the private construction ledger.

No corpus downloads, model calls, tokenization, or training take place here.
Exact normalization and supplied cluster links do not establish near-duplicate
or cross-source evaluation decontamination. Outputs remain development material.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any


SCHEMA_VERSION = "matched_data_development_v1"
SELECTION_RULE_VERSION = "sha256_seeded_strict_pair_v1"
SPLITS = ("development", "train", "capability_holdout", "reserve")
SCORE_STATUSES = (
    "correct", "incorrect", "invalid", "truncated", "unscorable", "scorer_error"
)
COMPLETE_FINISH_REASONS = frozenset({"stop", "eos", "end_turn"})
_REVISION = re.compile(r"(?:[0-9a-fA-F]{40}|(?:sha256:)?[0-9a-fA-F]{64})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _copy(value: Any) -> Any:
    return json.loads(_json(value))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _seed(seed: Any) -> int:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    return seed


def _weight(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("example_weight must be a finite positive number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError("example_weight must be a finite positive number")
    return float(value)


def normalized_source_text(text: str) -> str:
    """NFC and whitespace normalization only; case and math symbols survive."""
    return " ".join(unicodedata.normalize("NFC", _string(text, "source_text")).split())


def normalize_source_task(record: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist source fields and keep private answers out of model messages."""
    if not isinstance(record, Mapping):
        raise ValueError("source record must be a mapping")
    source_uri = _string(record.get("source_uri"), "source_uri")
    revision = _string(record.get("source_revision"), "source_revision")
    if not _REVISION.fullmatch(revision):
        raise ValueError("source_revision must be an immutable 40/64-hex digest")
    row_id = _string(record.get("source_row_id"), "source_row_id")
    text = _string(record.get("source_text"), "source_text")
    license_name = _string(record.get("license"), "license")
    if record.get("source_usage") != "training_allowed":
        raise ValueError("source_usage must explicitly permit training; evaluation-only sources are excluded")
    gold = record.get("private_gold")
    if not isinstance(gold, Mapping) or not gold:
        raise ValueError("private_gold must be a nonempty mapping")
    identity = {"source_uri": source_uri, "source_revision": revision, "source_row_id": row_id}
    task = {
        "schema_version": SCHEMA_VERSION,
        "construction_status": "development_only",
        "task_id": "task_" + _hash(_json(identity)),
        **identity,
        "source_usage": "training_allowed",
        "source_text": text,
        "source_text_sha256": _hash(text),
        "normalized_text_sha256": _hash(normalized_source_text(text)),
        "license": license_name,
        "component_terms": _copy(record.get("component_terms", [])),
        "topic": _copy(record.get("topic")),
        "difficulty": _copy(record.get("difficulty")),
        "example_weight": _weight(record.get("example_weight", 1.0)),
        "messages": [{"role": "user", "content": text}],
        "private_gold": _copy(dict(gold)),
    }
    if "source_row_sha256" in record:
        digest = record["source_row_sha256"]
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ValueError("source_row_sha256 must be a lowercase SHA-256 digest")
        task["source_row_sha256"] = digest
    return task


def _validated_tasks(tasks: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    exact_gold: dict[str, str] = {}
    for task in tasks:
        if not isinstance(task, Mapping):
            raise ValueError("task must be a mapping")
        normalized = normalize_source_task(task)
        for key in ("task_id", "source_text_sha256", "normalized_text_sha256", "messages"):
            if task.get(key) != normalized[key]:
                raise ValueError(f"task has stale or inconsistent {key}")
        if task["task_id"] in seen:
            raise ValueError(f"duplicate task_id: {task['task_id']}")
        seen.add(task["task_id"])
        content_hash = task["normalized_text_sha256"]
        gold_identity = _json(task["private_gold"])
        if content_hash in exact_gold and exact_gold[content_hash] != gold_identity:
            raise ValueError("identical normalized tasks have conflicting private_gold; adjudication required")
        exact_gold[content_hash] = gold_identity
        result.append(_copy(dict(task)))
    return sorted(result, key=lambda item: item["task_id"])


def assign_task_splits(
    tasks: Iterable[Mapping[str, Any]],
    *,
    seed: int,
    split_weights: Mapping[str, float],
    known_clusters: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Union exact matches and explicit derivation links before seeded splitting.

    known_clusters maps task IDs to adjudicated shared derivation labels. Exact
    duplicate rows are retained as provenance aliases; exact_duplicate_of names
    their canonical source-row representative. Split weights give probabilities,
    not exact counts. The corpus and cluster map must be frozen before generation.
    """
    _seed(seed)
    result = _validated_tasks(tasks)
    if not isinstance(split_weights, Mapping) or not split_weights:
        raise ValueError("split_weights must be a nonempty mapping")
    if set(split_weights) - set(SPLITS):
        raise ValueError("unknown split in split_weights")
    weights = [(name, _weight(split_weights[name])) for name in SPLITS if name in split_weights]
    total = sum(weight for _, weight in weights)
    if not math.isfinite(total):
        raise ValueError("sum of split_weights must be finite")
    known_clusters = dict(known_clusters or {})
    ids = {task["task_id"] for task in result}
    if set(known_clusters) - ids:
        raise ValueError("known_clusters contains an unknown task_id")
    for label in known_clusters.values():
        _string(label, "known cluster label")
    parent = {task_id: task_id for task_id in ids}

    def find(task_id: str) -> str:
        while parent[task_id] != task_id:
            parent[task_id] = parent[parent[task_id]]
            task_id = parent[task_id]
        return task_id

    def union(left: str, right: str) -> None:
        left, right = find(left), find(right)
        parent[max(left, right)] = min(left, right)

    exact_groups: dict[str, list[str]] = defaultdict(list)
    known_groups: dict[str, list[str]] = defaultdict(list)
    for task in result:
        task_id = task["task_id"]
        exact_groups[task["normalized_text_sha256"]].append(task_id)
        if task_id in known_clusters:
            known_groups[known_clusters[task_id]].append(task_id)
    for members in [*exact_groups.values(), *known_groups.values()]:
        for task_id in members[1:]:
            union(members[0], task_id)
    components: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in result:
        components[find(task["task_id"])].append(task)
    for members in components.values():
        content_hashes = sorted({task["normalized_text_sha256"] for task in members})
        cluster_id = "cluster_" + _hash(_json(content_hashes))
        labels = sorted({known_clusters[task["task_id"]] for task in members if task["task_id"] in known_clusters})
        draw = int(_hash(_json([seed, "split", cluster_id])), 16) / 2**256
        cumulative = 0.0
        split = weights[-1][0]
        for name, weight in weights:
            cumulative += weight / total
            if draw < cumulative:
                split = name
                break
        for task in members:
            representative = min(exact_groups[task["normalized_text_sha256"]])
            task["dependency_cluster_id"] = cluster_id
            task["known_derivation_labels"] = labels
            task["split"] = split
            task["split_seed"] = seed
            task["exact_duplicate_of"] = None if task["task_id"] == representative else representative
    validate_split_integrity(result)
    return result


def validate_split_integrity(tasks: Iterable[Mapping[str, Any]]) -> None:
    """Reject cross-split exact content, supplied derivations, or cluster IDs."""
    records = _validated_tasks(tasks)
    partitions: dict[tuple[str, str], str] = {}
    task_map = {task["task_id"]: task for task in records}
    for task in records:
        split = task.get("split")
        if split not in SPLITS:
            raise ValueError(f"invalid split: {split!r}")
        cluster = _string(task.get("dependency_cluster_id"), "dependency_cluster_id")
        labels = task.get("known_derivation_labels", [])
        if not isinstance(labels, list):
            raise ValueError("known_derivation_labels must be a list")
        for label in labels:
            _string(label, "known derivation label")
        keys = [("content", task["normalized_text_sha256"]), ("cluster", cluster)]
        keys.extend(("known_derivation", label) for label in labels)
        for key in keys:
            if key in partitions and partitions[key] != split:
                raise ValueError(f"cross-split {key[0]} overlap")
            partitions[key] = split
        duplicate_of = task.get("exact_duplicate_of")
        if duplicate_of is not None:
            representative = task_map.get(duplicate_of)
            if representative is None or duplicate_of == task["task_id"]:
                raise ValueError("invalid exact_duplicate_of reference")
            if representative["normalized_text_sha256"] != task["normalized_text_sha256"]:
                raise ValueError("exact_duplicate_of must reference identical normalized content")
            if representative.get("exact_duplicate_of") is not None:
                raise ValueError("exact_duplicate_of must reference the canonical representative")


def _candidate_exclusion(candidate: Mapping[str, Any]) -> str | None:
    score = candidate.get("score")
    if not isinstance(score, Mapping):
        raise ValueError("candidate score must be a mapping")
    status = score.get("status")
    if status not in SCORE_STATUSES:
        raise ValueError(f"unknown score status: {status!r}")
    _string(score.get("scorer_version"), "scorer_version")
    _string(score.get("reason"), "score reason")
    if status not in {"correct", "incorrect"}:
        return status
    expected_reward = 1.0 if status == "correct" else 0.0
    if isinstance(score.get("reward"), bool) or score.get("reward") != expected_reward:
        raise ValueError(f"inconsistent reward for {status} candidate")
    if score.get("correctness") != status:
        raise ValueError("inconsistent score correctness")
    if score.get("validity") != "valid" or score.get("parse_status") != "valid":
        raise ValueError("correct/incorrect candidates require valid parsing")
    if score.get("transport_status") != "ok":
        raise ValueError("correct/incorrect candidates require successful transport")
    if score.get("finish_reason") not in COMPLETE_FINISH_REASONS:
        raise ValueError("correct/incorrect candidates require confirmed complete generation")
    if not candidate["response"].strip():
        raise ValueError("correct/incorrect candidates require a nonempty response")
    if status == "incorrect" and not candidate["substantive"]:
        return "non_substantive_incorrect"
    return None


def derive_matched_records(
    tasks: Iterable[Mapping[str, Any]],
    candidates: Iterable[Mapping[str, Any]],
    *,
    seed: int,
    reference_id: str,
    split: str = "development",
    group_size: int = 4,
) -> dict[str, Any]:
    """Return aligned SFT, DPO, RL rows plus complete private inclusion ledgers.

    response is the unchanged full assistant text, including any emitted reasoning.
    Candidates must name the canonical row for exact-duplicate source content;
    candidates on aliases are rejected before any yield denominator is computed.
    This module does not qualify model templates, token masks, scorer correctness,
    substantive-error judgments, or audit readiness for scientific training.
    """
    _seed(seed)
    _string(reference_id, "reference_id")
    if split not in SPLITS:
        raise ValueError(f"invalid split: {split!r}")
    if split not in {"development", "train"}:
        raise ValueError("capability_holdout and reserve are protected from training-record derivation")
    if isinstance(group_size, bool) or not isinstance(group_size, int) or group_size < 2:
        raise ValueError("group_size must be an integer of at least two")
    records = _validated_tasks(tasks)
    validate_split_integrity(records)
    task_map = {task["task_id"]: task for task in records}
    representatives: dict[str, str] = {}
    for task in records:
        representatives.setdefault(task["normalized_text_sha256"], task["task_id"])
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    candidate_ids = set()
    candidate_ledger = []
    scorer_versions = set()
    response_groups: dict[str, str] = {}
    response_decisions: dict[tuple[str, str], str] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ValueError("candidate must be a mapping")
        row = _copy(dict(candidate))
        candidate_id = _string(row.get("candidate_id"), "candidate_id")
        if candidate_id in candidate_ids:
            raise ValueError(f"duplicate candidate_id: {candidate_id}")
        candidate_ids.add(candidate_id)
        task_id = _string(row.get("task_id"), "candidate task_id")
        if task_id not in task_map:
            raise ValueError(f"candidate references unknown task_id: {task_id}")
        if not isinstance(row.get("response"), str):
            raise ValueError("candidate response must be a string, including empty failed outputs")
        if not isinstance(row.get("substantive"), bool):
            raise ValueError("candidate substantive must be an explicit boolean")
        task = task_map[task_id]
        if task_id != representatives[task["normalized_text_sha256"]]:
            raise ValueError("candidate must reference the canonical exact-duplicate task row")
        if "split" in row and row["split"] != task["split"]:
            raise ValueError("candidate split does not match its task")
        if "normalized_text_sha256" in row and row["normalized_text_sha256"] != task["normalized_text_sha256"]:
            raise ValueError("candidate content hash does not match its task")
        if "source_text_sha256" in row and row["source_text_sha256"] != task["source_text_sha256"]:
            raise ValueError("candidate source text hash does not match its task")
        if "response_group_id" in row:
            group = _string(row["response_group_id"], "response_group_id")
            if group in response_groups and response_groups[group] != task["split"]:
                raise ValueError("cross-split response group overlap")
            response_groups[group] = task["split"]
        exclusion = _candidate_exclusion(row)
        scorer_versions.add(row["score"]["scorer_version"])
        row["response_sha256"] = _hash(row["response"])
        if row["score"]["status"] in {"correct", "incorrect"}:
            key = (task_id, row["response_sha256"])
            if key in response_decisions and response_decisions[key] != row["score"]["status"]:
                raise ValueError("identical task/response text has contradictory correctness scores")
            response_decisions[key] = row["score"]["status"]
        row["eligible_for_pair"] = exclusion is None
        row["exclusion_reason"] = exclusion
        row["selected_as"] = None
        candidate_ledger.append(row)
        by_task[task_id].append(row)
    if len(scorer_versions) > 1:
        raise ValueError("candidate bank mixes scorer versions")

    sft, dpo, rl, task_ledger = [], [], [], []
    for task in records:
        task_id = task["task_id"]
        rows = by_task[task_id]
        correct = [row for row in rows if row["eligible_for_pair"] and row["score"]["status"] == "correct"]
        incorrect = [row for row in rows if row["eligible_for_pair"] and row["score"]["status"] == "incorrect"]
        statuses = Counter(row["score"]["status"] for row in rows)
        if correct and incorrect:
            group_status = "mixed_reward"
        elif correct:
            group_status = "correct_only"
        elif incorrect:
            group_status = "incorrect_only"
        else:
            group_status = "no_eligible_scorable_candidates"
        ledger = {
            "task_id": task_id,
            "dependency_cluster_id": task["dependency_cluster_id"],
            "split": task["split"],
            "topic": _copy(task.get("topic")),
            "difficulty": _copy(task.get("difficulty")),
            "candidate_count": len(rows),
            "score_status_counts": {status: statuses[status] for status in SCORE_STATUSES},
            "eligible_correct_count": len(correct),
            "eligible_incorrect_count": len(incorrect),
            "group_status": group_status,
            "retained": False,
            "exclusion_reason": None,
        }
        task_ledger.append(ledger)
        if task["split"] != split:
            ledger["exclusion_reason"] = "outside_requested_split"
        elif task_id != representatives[task["normalized_text_sha256"]]:
            ledger["exclusion_reason"] = "exact_duplicate_task"
        elif not rows:
            ledger["exclusion_reason"] = "no_candidates"
        elif not correct:
            ledger["exclusion_reason"] = "no_correct_candidate"
        elif not incorrect:
            ledger["exclusion_reason"] = "no_substantive_incorrect_candidate"
        if ledger["exclusion_reason"] is not None:
            continue

        def select(pool: list[dict[str, Any]], role: str) -> dict[str, Any]:
            return min(pool, key=lambda row: (_hash(_json([seed, role, task_id, row["candidate_id"]])), row["candidate_id"]))

        chosen, rejected = select(correct, "chosen"), select(incorrect, "rejected")
        chosen["selected_as"], rejected["selected_as"] = "chosen", "rejected"
        ledger.update(retained=True, chosen_candidate_id=chosen["candidate_id"], rejected_candidate_id=rejected["candidate_id"])
        common = {
            "schema_version": SCHEMA_VERSION,
            "construction_status": "development_only",
            "tokenization_status": "unqualified",
            "task_id": task_id,
            "dependency_cluster_id": task["dependency_cluster_id"],
            "split": split,
            "example_weight": _weight(task["example_weight"]),
            "selection_rule_version": SELECTION_RULE_VERSION,
            "selection_seed": seed,
        }
        prompt = _copy(task["messages"])
        sft.append({
            **common,
            "messages": prompt + [{"role": "assistant", "content": chosen["response"]}],
            "chosen_candidate_id": chosen["candidate_id"],
            "response": chosen["response"],
            "response_sha256": chosen["response_sha256"],
            "loss_mask_policy": "generated_assistant_tokens_only_pending_tokenizer_validation",
        })
        dpo.append({
            **common,
            "messages": _copy(prompt),
            "chosen_candidate_id": chosen["candidate_id"],
            "rejected_candidate_id": rejected["candidate_id"],
            "chosen": chosen["response"],
            "rejected": rejected["response"],
            "chosen_sha256": chosen["response_sha256"],
            "rejected_sha256": rejected["response_sha256"],
            "preference_reason": "strict_correct_over_substantive_incorrect",
            "score_gap": 1.0,
            "scorer_version": chosen["score"]["scorer_version"],
            "reference_id": reference_id,
        })
        rl.append({
            **common,
            "messages": _copy(prompt),
            "private_gold": _copy(task["private_gold"]),
            "scorer_version": chosen["score"]["scorer_version"],
            "group_size": group_size,
        })
    eligible_population = [row for row in task_ledger if row["split"] == split and row["exclusion_reason"] != "exact_duplicate_task"]
    screened_population = [row for row in eligible_population if row["candidate_count"] > 0]
    retained = len(sft)
    return {
        "schema_version": SCHEMA_VERSION,
        "construction_status": "development_only",
        "selection_rule_version": SELECTION_RULE_VERSION,
        "selection_seed": seed,
        "split": split,
        "reference_id": reference_id,
        "sft": sft,
        "dpo": dpo,
        "rl": rl,
        "task_ledger": task_ledger,
        "candidate_ledger": sorted(candidate_ledger, key=lambda row: row["candidate_id"]),
        "summary": {
            "input_tasks": len(records),
            "input_candidates": len(candidate_ledger),
            "requested_split_unique_tasks": len(eligible_population),
            "screened_unique_tasks": len(screened_population),
            "unscreened_unique_tasks": len(eligible_population) - len(screened_population),
            "retained_tasks": retained,
            "pairable_fraction": retained / len(screened_population) if screened_population else None,
            "retained_fraction_of_requested_tasks": retained / len(eligible_population) if eligible_population else None,
            "task_exclusion_counts": dict(sorted(Counter(row["exclusion_reason"] for row in task_ledger if not row["retained"]).items())),
            "candidate_exclusion_counts": dict(sorted(Counter(row["exclusion_reason"] for row in candidate_ledger if row["exclusion_reason"] is not None).items())),
            "score_status_counts": {status: sum(row["score"]["status"] == status for row in candidate_ledger) for status in SCORE_STATUSES},
        },
        "unresolved_limitations": [
            "source_permissions_are_caller_attested",
            "manual_near_duplicate_and_cross_source_overlap_audit_pending",
            "scorer_and_substantive_error_audit_pending",
            "actual_model_template_tokenizer_and_loss_mask_validation_pending",
            "not_approved_for_scientific_training",
        ],
    }
