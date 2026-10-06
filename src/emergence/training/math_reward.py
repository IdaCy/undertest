from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from fractions import Fraction
from typing import Literal


SCORER_VERSION = "exact-scalar-v1"
MAX_RESPONSE_CHARS = 131_072
MAX_ANSWER_CHARS = 2_048
MAX_SCALAR_DIGITS = 256
MAX_BRACE_DEPTH = 8
COMPLETE_FINISH_REASONS = frozenset({"stop", "eos", "end_turn"})
TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens", "max_length"})

ParseStatus = Literal["valid", "invalid", "unsupported"]
RewardStatus = Literal[
    "correct", "incorrect", "invalid", "truncated", "unscorable", "scorer_error"
]

_INTEGER = r"[+-]?[0-9]+"
_DECIMAL_RE = re.compile(r"[+-]?(?:[0-9]+\.[0-9]+|\.[0-9]+)")
_INTEGER_RE = re.compile(_INTEGER)
_SLASH_RE = re.compile(rf"({_INTEGER})\s*/\s*({_INTEGER})")
_FRAC_RE = re.compile(
    rf"([+-]?)\\(?:dfrac|tfrac|frac)\s*\{{\s*({_INTEGER})\s*\}}\s*\{{\s*({_INTEGER})\s*\}}"
)
_FINAL_RE = re.compile(r"^[ \t]*Final answer[ \t]*:", re.IGNORECASE | re.MULTILINE)
_BOX_RE = re.compile(r"(?<!\\)\\boxed\s*\{")
_SCIENCE_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)[eE][+-]?[0-9]+")
_ARITHMETIC_RE = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)"
    r"(?:\s*[+*/^\-]\s*[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+))+"
)
_MATH_WRAPPERS = (("$$", "$$"), ("$", "$"), (r"\(", r"\)"), (r"\[", r"\]"))


@dataclass(frozen=True)
class ScalarParse:
    status: ParseStatus
    reason: str
    canonical: str | None = None
    numerator: int | None = None
    denominator: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MathReward:
    status: RewardStatus
    reward: float | None
    reason: str
    correctness: Literal["correct", "incorrect", "unscored"] = "unscored"
    validity: Literal["valid", "invalid", "unscorable", "not_checked"] = "not_checked"
    parse_status: Literal["valid", "invalid", "unsupported", "not_checked"] = "not_checked"
    transport_status: str = "ok"
    finish_reason: str | None = None
    extracted_answer: str | None = None
    normalized_prediction: str | None = None
    normalized_gold: str | None = None
    scorer_version: str = SCORER_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _Extraction:
    status: ParseStatus
    reason: str
    answer: str | None = None


def _fraction_result(numerator: int, denominator: int) -> ScalarParse:
    if denominator == 0:
        return ScalarParse("invalid", "zero_denominator")
    value = Fraction(numerator, denominator)
    canonical = str(value.numerator)
    if value.denominator != 1:
        canonical += f"/{value.denominator}"
    return ScalarParse("valid", "exact_scalar", canonical, value.numerator, value.denominator)


def _brace_group(text: str, opening: int) -> tuple[str | None, int, str | None]:
    depth = 0
    for offset in range(opening, len(text)):
        if text[offset] == "{":
            depth += 1
            if depth > MAX_BRACE_DEPTH:
                return None, offset, "nesting_resource_limit"
        elif text[offset] == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1 : offset], offset + 1, None
    return None, len(text), "unclosed_box"


def _unwrap_math(text: str) -> tuple[str | None, str | None]:
    for opening, closing in _MATH_WRAPPERS:
        if text.startswith(opening):
            if len(text) < len(opening) + len(closing) or not text.endswith(closing):
                return None, "unmatched_math_wrapper"
            return text[len(opening) : -len(closing)].strip(), None
    if text.endswith(("$", r"\)", r"\]")):
        return None, "unmatched_math_wrapper"
    return text, None


def parse_scalar_answer(text: str) -> ScalarParse:
    """Parse a whole exact scalar; unsupported representations are never coerced."""
    if not isinstance(text, str):
        return ScalarParse("invalid", "answer_not_string")
    if len(text) > MAX_ANSWER_CHARS:
        return ScalarParse("unsupported", "answer_resource_limit")
    scalar, error = _unwrap_math(text.strip())
    if error:
        return ScalarParse("invalid", error)
    if not scalar:
        return ScalarParse("invalid", "empty_answer")
    if scalar.startswith(r"\boxed"):
        if scalar.count(r"\boxed") != 1:
            return ScalarParse("invalid", "multiple_boxes")
        match = _BOX_RE.match(scalar)
        if not match:
            return ScalarParse("invalid", "malformed_box")
        scalar_body, end, error = _brace_group(scalar, match.end() - 1)
        if error:
            status = "unsupported" if error == "nesting_resource_limit" else "invalid"
            return ScalarParse(status, error)
        if scalar[end:].strip():
            return ScalarParse("unsupported", "box_trailing_content")
        scalar = scalar_body.strip()
        if not scalar:
            return ScalarParse("invalid", "empty_answer")
    if sum(character in "0123456789" for character in scalar) > MAX_SCALAR_DIGITS:
        return ScalarParse("unsupported", "digit_resource_limit")
    if _INTEGER_RE.fullmatch(scalar):
        return _fraction_result(int(scalar), 1)
    if _DECIMAL_RE.fullmatch(scalar):
        sign = -1 if scalar.startswith("-") else 1
        unsigned = scalar.lstrip("+-")
        whole, fraction = unsigned.split(".")
        return _fraction_result(sign * int((whole or "0") + fraction), 10 ** len(fraction))
    match = _SLASH_RE.fullmatch(scalar)
    if match:
        return _fraction_result(int(match[1]), int(match[2]))
    match = _FRAC_RE.fullmatch(scalar)
    if match:
        sign = -1 if match[1] == "-" else 1
        return _fraction_result(sign * int(match[2]), int(match[3]))
    if _SCIENCE_RE.fullmatch(scalar):
        return ScalarParse("unsupported", "scientific_notation_unsupported")
    if _ARITHMETIC_RE.fullmatch(scalar):
        return ScalarParse("unsupported", "scalar_expression_unsupported")
    if re.fullmatch(r"[+\-0-9.\s/]+", scalar):
        return ScalarParse("invalid", "malformed_scalar")
    if re.fullmatch(r"[+\-0-9.eE]+", scalar) and any(c in scalar for c in "eE"):
        return ScalarParse("invalid", "malformed_exponent")
    if re.match(r"^[+-]\s*[+-]", scalar):
        return ScalarParse("invalid", "malformed_sign")
    if re.search(r"\\(?:dfrac|tfrac|frac)", scalar) and scalar.count("{") != scalar.count("}"):
        return ScalarParse("invalid", "malformed_fraction")
    return ScalarParse("unsupported", "unsupported_answer_type")


def _extract_answer(response: str) -> _Extraction:
    markers = list(_FINAL_RE.finditer(response))
    box_count = response.count(r"\boxed")
    if box_count > 1:
        return _Extraction("invalid", "multiple_boxes")
    if len(markers) > 1:
        return _Extraction("invalid", "multiple_final_answer_markers")
    if box_count:
        match = _BOX_RE.search(response)
        if not match:
            return _Extraction("invalid", "malformed_box")
        answer, end, error = _brace_group(response, match.end() - 1)
        if error:
            status = "unsupported" if error == "nesting_resource_limit" else "invalid"
            return _Extraction(status, error)
        suffix = response[end:].strip()
        prefix = response[: match.start()].rstrip()
        envelope_start = match.start()
        wrapped = False
        for opening, closing in _MATH_WRAPPERS:
            if prefix.endswith(opening):
                if suffix not in {closing, closing + "."}:
                    return _Extraction("invalid", "unmatched_math_wrapper")
                envelope_start = len(prefix) - len(opening)
                wrapped = True
                break
        if not wrapped and suffix not in {"", "."}:
            return _Extraction("unsupported", "box_trailing_content")
        if markers and (
            markers[0].end() > envelope_start
            or response[markers[0].end() : envelope_start].strip()
        ):
            return _Extraction("invalid", "conflicting_answer_markers")
        return _Extraction("valid", "terminal_box", answer)
    if markers:
        answer = response[markers[0].end() :].strip()
        if "\n" in answer or "\r" in answer:
            return _Extraction("invalid", "multiline_final_answer")
        return _Extraction("valid", "final_answer_marker", answer)
    return _Extraction("invalid", "missing_final_answer_marker")


def score_math_response(
    response: str,
    gold_answer: str,
    *,
    finish_reason: str | None = None,
    transport_status: str = "ok",
) -> MathReward:
    """Score terminal boxed or 'Final answer:' scalars with affirmative completion metadata."""
    metadata = {
        "finish_reason": finish_reason if isinstance(finish_reason, str) else None,
        "transport_status": transport_status if isinstance(transport_status, str) else "invalid",
    }
    if not isinstance(transport_status, str) or (
        finish_reason is not None and not isinstance(finish_reason, str)
    ):
        return MathReward("scorer_error", None, "metadata_type_error", **metadata)
    if transport_status != "ok":
        return MathReward("scorer_error", None, "transport_failure", **metadata)
    if not isinstance(response, str):
        return MathReward("scorer_error", None, "response_not_string", **metadata)
    try:
        gold = parse_scalar_answer(gold_answer)
        if gold.status != "valid":
            return MathReward(
                "unscorable", None, f"gold_{gold.reason}", validity="unscorable", **metadata
            )
        metadata["normalized_gold"] = gold.canonical
        if finish_reason in TRUNCATED_FINISH_REASONS:
            return MathReward("truncated", 0.0, "generation_truncated", validity="invalid", **metadata)
        if finish_reason not in COMPLETE_FINISH_REASONS:
            reason = "finish_reason_missing" if finish_reason is None else "finish_reason_unsupported"
            return MathReward("unscorable", None, reason, validity="unscorable", **metadata)
        if len(response) > MAX_RESPONSE_CHARS:
            return MathReward("unscorable", None, "response_resource_limit", validity="unscorable", **metadata)
        extraction = _extract_answer(response)
        if extraction.status != "valid":
            status = "invalid" if extraction.status == "invalid" else "unscorable"
            return MathReward(
                status,
                0.0 if status == "invalid" else None,
                extraction.reason,
                validity="invalid" if status == "invalid" else "unscorable",
                parse_status=extraction.status,
                **metadata,
            )
        parsed = parse_scalar_answer(extraction.answer)
        metadata["extracted_answer"] = extraction.answer
        if parsed.status != "valid":
            status = "invalid" if parsed.status == "invalid" else "unscorable"
            return MathReward(
                status,
                0.0 if status == "invalid" else None,
                parsed.reason,
                validity="invalid" if status == "invalid" else "unscorable",
                parse_status=parsed.status,
                **metadata,
            )
        equal = (parsed.numerator, parsed.denominator) == (gold.numerator, gold.denominator)
        status = "correct" if equal else "incorrect"
        return MathReward(
            status,
            1.0 if equal else 0.0,
            "exact_match" if equal else "exact_mismatch",
            correctness=status,
            validity="valid",
            parse_status="valid",
            normalized_prediction=parsed.canonical,
            **metadata,
        )
    except Exception as exc:
        return MathReward("scorer_error", None, f"internal_exception:{type(exc).__name__}", **metadata)
