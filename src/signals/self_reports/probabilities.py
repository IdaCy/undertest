from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation


PROBABILITY_PARSER_VERSION = "source-probability-json-v1"
MAX_FINAL_CHARS = 32_768
COMPLETE_FINISH_REASONS = frozenset({"stop", "eos", "end_turn"})
TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens", "max_length"})


@dataclass(frozen=True)
class SourceProbability:
    status: str
    reason: str
    probability: float | None = None
    parser_version: str = PROBABILITY_PARSER_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("nonfinite_json_constant")


def parse_source_probability(
    final_content: str,
    *,
    finish_reason: str | None,
    transport_status: str = "ok",
) -> SourceProbability:
    """Parse the final answer channel; reasoning text is retained separately by callers."""
    if not isinstance(transport_status, str) or transport_status != "ok":
        return SourceProbability("request_error", "transport_failure")
    if not isinstance(finish_reason, str):
        return SourceProbability("unscorable", "finish_reason_missing_or_invalid")
    if finish_reason in TRUNCATED_FINISH_REASONS:
        return SourceProbability("truncated", "completion_truncated")
    if finish_reason not in COMPLETE_FINISH_REASONS:
        return SourceProbability("unscorable", "finish_reason_unsupported")
    if not isinstance(final_content, str):
        return SourceProbability("invalid", "final_content_not_string")
    if len(final_content) > MAX_FINAL_CHARS:
        return SourceProbability("invalid", "final_content_limit")
    try:
        value = json.loads(
            final_content,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=Decimal,
        )
    except (ValueError, RecursionError, InvalidOperation):
        return SourceProbability("invalid", "invalid_json")
    if not isinstance(value, dict) or set(value) != {"p_eval_source"}:
        return SourceProbability("invalid", "wrong_object_shape")
    probability = value["p_eval_source"]
    if isinstance(probability, bool) or not isinstance(probability, (int, Decimal)):
        return SourceProbability("invalid", "probability_not_number")
    if isinstance(probability, Decimal) and not probability.is_finite():
        return SourceProbability("invalid", "probability_out_of_range")
    if not 0 <= probability <= 1:
        return SourceProbability("invalid", "probability_out_of_range")
    return SourceProbability("valid", "strict_probability_object", float(probability))
