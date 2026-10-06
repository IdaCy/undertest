import hashlib
import json

import causes.recipe_controls.tasks.banks as effects_banks
import causes.recipe_controls.tasks.recipe as effects_recipe
import causes.recipe_controls.tasks.validator as effects_validator


VERSION = "effects-candidate-effort-v1"
PARSERS = {"ledger": effects_recipe, "validator": effects_validator, "ledgerbanks": effects_banks}


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _quantity(value, complete):
    return {"observed": value, "full_attempt": value if complete else None,
            "coverage": "complete_recorded_invocations" if complete else "observed_lower_bound"}


def candidate_effort(candidates, *, family, complete):
    if family not in PARSERS or type(complete) is not bool:
        raise ValueError("A supported recipe family and explicit completeness boolean are required")
    if candidates is None:
        if complete:
            raise ValueError("Unavailable candidates cannot have complete coverage")
        return {"version": VERSION, "family": family, "status": "unavailable", "rows": [],
                "quantities": None, "complete": False}
    if type(candidates) is not list:
        raise ValueError("Candidates must be an ordered list or unavailable")
    engine = PARSERS[family]
    rows = []
    prior_index = -1
    for record in candidates:
        if (type(record) is not dict or type(record.get("index")) is not int
                or record["index"] <= prior_index or record.get("kind") not in {"test", "submit"}
                or type(record.get("raw")) is not str):
            raise ValueError("Candidate identity/order/kind/raw string is invalid")
        index = record["index"]
        if complete and index != prior_index + 1:
            raise ValueError("Complete candidate coverage requires contiguous indices from zero")
        prior_index = index
        try:
            parsed = engine.parse_recipe(record["raw"])
        except ValueError:
            parsed_text = None
        else:
            parsed_text = _canonical(parsed)
        valid = parsed_text is not None
        if record.get("valid") is not None and (type(record["valid"]) is not bool or record["valid"] != valid):
            raise ValueError("Saved candidate validity conflicts with exact parser replay")
        rows.append({"index": index, "kind": record["kind"], "raw": record["raw"],
                     "raw_sha256": _sha(record["raw"]), "valid": valid,
                     "canonical_recipe": parsed_text,
                     "canonical_recipe_sha256": _sha(parsed_text) if valid else None})

    raw_pairs = parsed_pairs = raw_changes = parsed_changes = invalid_pairs = gap_pairs = 0
    for left, right in zip(rows, rows[1:]):
        if right["index"] != left["index"] + 1:
            gap_pairs += 1
            continue
        raw_pairs += 1
        raw_changes += left["raw"] != right["raw"]
        if left["valid"] and right["valid"]:
            parsed_pairs += 1
            parsed_changes += left["canonical_recipe"] != right["canonical_recipe"]
        else:
            invalid_pairs += 1

    submit_raw_pairs = submit_parsed_pairs = submit_raw_changes = submit_parsed_changes = 0
    submit_invalid_pairs = submit_gap_pairs = 0
    previous_submit_position = None
    for position, row in enumerate(rows):
        if row["kind"] != "submit":
            continue
        if previous_submit_position is not None:
            previous = rows[previous_submit_position]
            if row["index"] - previous["index"] != position - previous_submit_position:
                submit_gap_pairs += 1
            else:
                submit_raw_pairs += 1
                submit_raw_changes += previous["raw"] != row["raw"]
                if previous["valid"] and row["valid"]:
                    submit_parsed_pairs += 1
                    submit_parsed_changes += previous["canonical_recipe"] != row["canonical_recipe"]
                else:
                    submit_invalid_pairs += 1
        previous_submit_position = position

    counts = {
        "invoked_candidates": len(rows),
        "invoked_tests": sum(row["kind"] == "test" for row in rows),
        "invoked_submits": sum(row["kind"] == "submit" for row in rows),
        "invalid_invoked_candidates": sum(not row["valid"] for row in rows),
        "unique_raw_candidate_strings": len({row["raw"] for row in rows}),
        "unique_valid_canonical_recipes": len({row["canonical_recipe"] for row in rows if row["valid"]}),
        "known_adjacent_raw_pairs": raw_pairs,
        "adjacent_raw_changes": raw_changes,
        "known_adjacent_valid_recipe_pairs": parsed_pairs,
        "adjacent_valid_canonical_changes": parsed_changes,
        "known_adjacent_pairs_touching_invalid": invalid_pairs,
        "known_consecutive_submit_raw_pairs": submit_raw_pairs,
        "consecutive_submit_raw_changes": submit_raw_changes,
        "known_consecutive_valid_submit_pairs": submit_parsed_pairs,
        "consecutive_valid_submit_canonical_changes": submit_parsed_changes,
        "known_consecutive_submit_pairs_touching_invalid": submit_invalid_pairs,
    }
    return {"version": VERSION, "family": family, "parser_version": engine.RECIPE_VERSION,
            "status": "measured", "complete": complete, "rows": rows,
            "quantities": {key: _quantity(value, complete) for key, value in counts.items()},
            "observed_pair_gaps": {"candidate_pairs": gap_pairs, "submit_pairs": submit_gap_pairs},
            "representation": "Sorted object keys; operation list order and scalar JSON types retained; only valid recipes canonicalized.",
            "scope": "Recorded invocations from one independently verified first attempt; caller must establish source identity and completeness.",
            "limitations": ["Recipe identity is syntactic, not semantic equivalence.",
                            "Invalid candidates interrupt parsed comparisons; missing indices interrupt adjacency.",
                            "Outer malformed calls, tool errors, model calls, usage and latency require separate evidence.",
                            "These counts do not measure cognition, awareness or psychological effort."]}
