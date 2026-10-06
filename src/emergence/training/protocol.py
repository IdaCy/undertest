from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from emergence.training.math_reward import COMPLETE_FINISH_REASONS
from emergence.training.math_reward import SCORER_VERSION
from emergence.training.math_reward import TRUNCATED_FINISH_REASONS


PROMPT_CONTRACT_ID = "exact_scalar_messages_development_v1"
GRPO_POLICY_ID = "grpo_fixed_group_preserve_development_v1"
QUALIFICATION_STATUS = "proposed_unqualified"
ANSWER_FORMAT_INSTRUCTION = (
    "Solve the problem. You may show your reasoning. End your response with exactly "
    "one final line of the form Final answer: <answer>. Replace <answer> with one "
    "exact scalar: an ASCII integer, a terminating decimal with digits after the "
    "decimal point, or a fraction written as a/b with integer numerator and nonzero "
    "integer denominator. Use an ASCII minus sign for negative numbers. Do not use "
    "scientific notation. Do not write any other final-answer label or any boxed "
    "expressions in your response. Do not place units, extra values, mathematical "
    "expressions, or punctuation after the scalar."
)
_GRPO_RULES = {
    "policy_id": GRPO_POLICY_ID,
    "qualification_status": QUALIFICATION_STATUS,
    "scorer_version": SCORER_VERSION,
    "membership": "predeclared_sample_ids_exactly_once_same_task_group_policy",
    "missing_or_unscorable": "stop_and_preserve_no_update_no_retry",
    "missing_reward": "never_impute",
    "model_invalid_or_truncated": "retain_original_zero_reward_in_fixed_group",
    "all_equal_finite": "valid_zero_variance_preserve_fixed_group",
    "response_dependent_resampling": "forbidden",
    "optimizer_and_kl": "not_implemented_or_qualified_here",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def grpo_policy_evidence() -> dict[str, Any]:
    return {**_GRPO_RULES, "policy_sha256": _sha256(_json(_GRPO_RULES))}


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


@dataclass(frozen=True)
class RenderedScalarPrompt:
    task_id: str
    source_text: str
    source_text_sha256: str
    instruction_sha256: str
    messages_sha256: str
    contract_id: str = PROMPT_CONTRACT_ID
    scorer_version: str = SCORER_VERSION
    qualification_status: str = QUALIFICATION_STATUS
    tokenization_status: str = "unqualified_no_tokenizer_applied"

    def model_messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": ANSWER_FORMAT_INSTRUCTION},
            {"role": "user", "content": self.source_text},
        ]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "messages": self.model_messages()}


def render_scalar_prompt(task: Mapping[str, Any]) -> RenderedScalarPrompt:
    """Whitelist raw source text; this does not apply or qualify a chat template."""
    if not isinstance(task, Mapping):
        raise ValueError("task must be a mapping")
    task_id = _required_string(task.get("task_id"), "task_id")
    text = _required_string(task.get("source_text"), "source_text")
    digest = _sha256(text)
    if task.get("source_text_sha256") != digest:
        raise ValueError("source_text_sha256 must match the unchanged source text")
    messages = [
        {"role": "system", "content": ANSWER_FORMAT_INSTRUCTION},
        {"role": "user", "content": text},
    ]
    return RenderedScalarPrompt(task_id, text, digest, _sha256(ANSWER_FORMAT_INSTRUCTION), _sha256(_json(messages)))


def _preserve_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"__nonfinite_float_evidence__": repr(value)}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("rollout evidence keys must be strings")
        return {key: _preserve_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_preserve_json(item) for item in value]
    raise ValueError("rollout evidence must contain JSON-compatible values")


@dataclass(frozen=True)
class GRPOGroupDecision:
    group_id: str
    task_id: str
    policy_version: str
    expected_sample_ids: tuple[str, ...]
    observed_sample_ids: tuple[Any, ...]
    preserved_rollouts: tuple[Any, ...]
    blockers: tuple[str, ...]
    action: str
    update_permitted_by_group_policy: bool
    zero_variance: bool | None
    reward_mean: float | None
    reward_variance: float | None
    rewards: tuple[float, ...] | None
    evidence_sha256: str
    policy_id: str = GRPO_POLICY_ID
    qualification_status: str = QUALIFICATION_STATUS
    retry_permitted: bool = False
    resampling_permitted: bool = False
    evidence_encoding: str = "json_with_explicit_nonfinite_float_tags_v1"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "group_policy": grpo_policy_evidence()}


def _score_blockers(score: Any) -> list[str]:
    if not isinstance(score, Mapping):
        return ["score_missing_or_not_mapping"]
    blockers = []
    if score.get("scorer_version") != SCORER_VERSION:
        blockers.append("scorer_version_mismatch")
    if not isinstance(score.get("reason"), str) or not score["reason"].strip():
        blockers.append("score_reason_missing")
    raw_status = score.get("status")
    status = raw_status if isinstance(raw_status, str) else None
    if status in {"unscorable", "scorer_error"}:
        blockers.append("score_" + status)
    elif status not in {"correct", "incorrect", "invalid", "truncated"}:
        blockers.append("score_status_unknown")
    reward = score.get("reward")
    if type(reward) not in (int, float) or (isinstance(reward, float) and not math.isfinite(reward)):
        blockers.append("reward_missing_or_nonfinite")
    elif reward != (1.0 if status == "correct" else 0.0):
        blockers.append("reward_inconsistent_with_status")
    if score.get("transport_status") != "ok":
        blockers.append("transport_not_ok")
    finish = score.get("finish_reason")
    if status == "truncated":
        if not isinstance(finish, str) or finish not in TRUNCATED_FINISH_REASONS:
            blockers.append("truncation_metadata_inconsistent")
    elif not isinstance(finish, str) or finish not in COMPLETE_FINISH_REASONS:
        blockers.append("completion_metadata_missing_or_inconsistent")
    expected_correctness = status if status in {"correct", "incorrect"} else "unscored"
    if score.get("correctness") != expected_correctness:
        blockers.append("correctness_inconsistent")
    if status in {"correct", "incorrect"}:
        if score.get("validity") != "valid" or score.get("parse_status") != "valid":
            blockers.append("validity_or_parse_inconsistent")
    elif status == "invalid":
        if score.get("validity") != "invalid" or score.get("parse_status") != "invalid":
            blockers.append("invalid_metadata_inconsistent")
    elif status == "truncated":
        if score.get("validity") != "invalid" or score.get("parse_status") != "not_checked":
            blockers.append("truncated_metadata_inconsistent")
    return blockers


def assess_grpo_group(
    *,
    group_id: str,
    task_id: str,
    policy_version: str,
    expected_sample_ids: Sequence[str],
    rollouts: Sequence[Mapping[str, Any]],
) -> GRPOGroupDecision:
    """Gate one predeclared group without changing membership or reward semantics."""
    group_id = _required_string(group_id, "group_id")
    task_id = _required_string(task_id, "task_id")
    policy_version = _required_string(policy_version, "policy_version")
    if not isinstance(expected_sample_ids, (tuple, list)) or len(expected_sample_ids) < 2:
        raise ValueError("expected_sample_ids must be a predeclared list/tuple of at least two IDs")
    expected = tuple(_required_string(item, "expected sample_id") for item in expected_sample_ids)
    if len(set(expected)) != len(expected):
        raise ValueError("expected_sample_ids must be unique")
    if not isinstance(rollouts, (tuple, list)):
        raise ValueError("rollouts must be a finite list/tuple")
    preserved = tuple(_preserve_json(row) for row in rollouts)
    blockers = []
    observed = []
    rewards_by_id = {}
    seen = set()
    if len(rollouts) != len(expected):
        blockers.append("group_size_mismatch")
    for index, row in enumerate(rollouts):
        prefix = f"member[{index}]:"
        if not isinstance(row, Mapping):
            observed.append(None)
            blockers.append(prefix + "rollout_not_mapping")
            continue
        sample_id = row.get("sample_id")
        observed.append(_preserve_json(sample_id))
        member_blockers = []
        if not isinstance(sample_id, str) or not sample_id.strip():
            member_blockers.append("sample_id_missing")
        else:
            if sample_id in seen:
                member_blockers.append("duplicate_sample_id")
            seen.add(sample_id)
            if sample_id not in expected:
                member_blockers.append("unexpected_sample_id")
        for name, value in (("task_id", task_id), ("group_id", group_id), ("policy_version", policy_version)):
            if row.get(name) != value:
                member_blockers.append(name + "_mismatch")
        if not isinstance(row.get("response"), str):
            member_blockers.append("response_not_string")
        elif not row["response"].strip() and isinstance(row.get("score"), Mapping):
            if row["score"].get("status") in ("correct", "incorrect"):
                member_blockers.append("empty_response_with_scored_answer")
        member_blockers.extend(_score_blockers(row.get("score")))
        if not member_blockers:
            rewards_by_id[sample_id] = float(row["score"]["reward"])
        blockers.extend(prefix + reason for reason in member_blockers)
    blockers.extend("missing_sample_id:" + sample_id for sample_id in expected if sample_id not in seen)
    rewards = None if blockers else tuple(rewards_by_id[sample_id] for sample_id in expected)
    mean = None if rewards is None else sum(rewards) / len(rewards)
    variance = None if rewards is None else sum((reward - mean) ** 2 for reward in rewards) / len(rewards)
    zero_variance = None if variance is None else variance == 0.0
    action = "stop_and_preserve" if blockers else ("valid_zero_variance" if zero_variance else "valid_mixed_reward")
    evidence = {
        "group_id": group_id,
        "task_id": task_id,
        "policy_version": policy_version,
        "expected_sample_ids": expected,
        "preserved_rollouts": preserved,
        "group_policy": grpo_policy_evidence(),
    }
    return GRPOGroupDecision(
        group_id, task_id, policy_version, expected, tuple(observed), preserved,
        tuple(blockers), action, not bool(blockers), zero_variance, mean, variance,
        rewards, _sha256(_json(evidence)),
    )
