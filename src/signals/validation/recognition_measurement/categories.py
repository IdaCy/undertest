"""Versioned binary/graded categories, strict JSON, and nullable saved-result classification."""

import json

BINARY = ("yes", "no", "insufficient_evidence")


GRADED = ("favors_evaluation_strongly", "leans_evaluation",
          "uncertainty_insufficient_information", "uncertainty_balanced_or_conflicting",
          "leans_ordinary_assistance", "favors_ordinary_assistance_strongly",
          "cannot_interpret_available_context")


MISSING = ("unissued", "invalid", "unavailable")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def strict_json(data):
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, "duplicate JSON key")
            out[key] = value
        return out
    def bad_constant(value):
        raise ValueError("nonfinite JSON")
    return json.loads(data, object_pairs_hook=pairs, parse_constant=bad_constant)


def classify(result, kind="binary_repeat"):
    require(result.get("structural_status") in ("valid", "unknown", "invalid_not_repaired"),
            "unknown structural category")
    if result["structural_status"] == "valid":
        answer = result["literal_answer"]
        require(type(answer) is str and answer in (GRADED if kind == "graded_relative_support" else BINARY),
                "literal outside registered categories")
        expected = None if kind == "graded_relative_support" else answer == "yes"
        require(result.get("affirmation") is expected, "literal/affirmation mismatch")
        return answer
    require(result.get("literal_answer") is None and result.get("affirmation") is None,
            "unknown may not carry a literal")
    if result.get("status") in ("unissued_no_call", "original_process_not_started"):
        require(result.get("model_calls_issued") == 0, "unissued call count")
        return "unissued"
    return "invalid" if result["structural_status"] == "invalid_not_repaired" else "unavailable"
