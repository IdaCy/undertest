"""Literal parser for the fixed layout/label diagnostic protocol."""
import json
from signals.validation.recognition_measurement.categories import require

def strict_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result
    def finite_only(value):
        raise ValueError("nonfinite_json")
    return json.loads(text, object_pairs_hook=unique, parse_constant=finite_only)

def literal_answer(text):
    value = strict_json(text)
    require(isinstance(value, dict) and set(value) == {"answer", "reason"}, "literal_shape")
    require(value["answer"] in ("yes", "no", "insufficient_evidence"), "literal_answer_value")
    require(isinstance(value["reason"], str) and bool(value["reason"].strip()), "literal_reason_value")
    return {"literal_answer": value["answer"], "literal_reason": value["reason"],
            "affirmation": value["answer"] == "yes", "structural_status": "valid"}
