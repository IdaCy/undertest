import copy
import hashlib
import json
from collections import defaultdict

from signals.reasoning.context_annotations.combined import construct_payload
from signals.reasoning.context_annotations.schema import AXES
from signals.reasoning.context_annotations.schema import validate_construct_coding


ALIGNMENT_VERSION = "effects-construct-alignment-v1"
ADJUDICATION_VERSION = "effects-construct-adjudication-accounting-v1"
RELATIONS = ("same_mention", "split_merge", "distinct_mentions", "first_only", "second_only", "unresolved")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _require(value, message):
    if not value:
        raise ValueError(message)


def _fields(value, names, label):
    _require(type(value) is dict and set(value) == set(names), "Unexpected " + label + " fields")


def _span_key(span):
    return (span["segment_id"], span["start"], span["end"])


def _evidence_key(mention):
    return (mention["target"], tuple(sorted(_span_key(span) for span in mention["spans"])))


def _overlap(left, right):
    return any(a["segment_id"] == b["segment_id"] and max(a["start"], b["start"]) < min(a["end"], b["end"])
               for a in left["spans"] for b in right["spans"])


def _edge(left, right, relation):
    return {
        "first": left["mention_id"], "second": right["mention_id"], "relation": relation,
        "axis_agreement": {axis: left[axis] == right[axis] for axis in AXES},
        "anchor_agreement": _span_key(left["spans"][left["anchor_span_index"]]) == _span_key(right["spans"][right["anchor_span_index"]]),
        "evidence_order_agreement": [_span_key(span) for span in left["spans"]] == [_span_key(span) for span in right["spans"]],
    }


def align_construct_codings(item, first, second):
    payload = construct_payload(item)
    first = validate_construct_coding(payload, first)
    second = validate_construct_coding(payload, second)
    sides = {"first": {m["mention_id"]: m for m in first["mentions"]},
             "second": {m["mention_id"]: m for m in second["mentions"]}}
    adjacency = {(side, identity): set() for side, mentions in sides.items() for identity in mentions}
    edges, cross_target = [], []
    keys = {side: defaultdict(list) for side in sides}
    for side, mentions in sides.items():
        for identity in sorted(mentions):
            keys[side][_evidence_key(mentions[identity])].append(identity)
    for left_id in sorted(sides["first"]):
        left = sides["first"][left_id]
        for right_id in sorted(sides["second"]):
            right = sides["second"][right_id]
            if left["target"] != right["target"]:
                if _overlap(left, right):
                    cross_target.append({"first": left_id, "second": right_id,
                                         "first_target": left["target"], "second_target": right["target"]})
                continue
            exact = _evidence_key(left) == _evidence_key(right)
            if exact or _overlap(left, right):
                edges.append(_edge(left, right, "exact_evidence" if exact else "potential_overlap"))
                adjacency[("first", left_id)].add(("second", right_id))
                adjacency[("second", right_id)].add(("first", left_id))
    groups, remaining = [], set(adjacency)
    while remaining:
        pending, members = [min(remaining)], set()
        while pending:
            node = pending.pop()
            if node not in members:
                members.add(node)
                pending.extend(adjacency[node] - members)
        remaining.difference_update(members)
        left_ids = sorted(identity for side, identity in members if side == "first")
        right_ids = sorted(identity for side, identity in members if side == "second")
        target = sides[min(members)[0]][min(members)[1]]["target"]
        groups.append({"group_id": _digest([target, left_ids, right_ids]), "target": target,
                       "first": left_ids, "second": right_ids,
                       "shape": "first_only" if not right_ids else "second_only" if not left_ids
                       else "one_to_one" if len(left_ids) == len(right_ids) == 1 else "many_to_many_or_split_merge_candidate",
                       "edges": [copy.deepcopy(edge) for edge in edges if edge["first"] in left_ids and edge["second"] in right_ids]})
    exact_pairs, ambiguous_keys = [], []
    for key in sorted(keys["first"].keys() & keys["second"].keys()):
        left_ids, right_ids = keys["first"][key], keys["second"][key]
        if len(left_ids) == len(right_ids) == 1:
            exact_pairs.append(_edge(sides["first"][left_ids[0]], sides["second"][right_ids[0]], "exact_evidence"))
        else:
            ambiguous_keys.append({"target": key[0], "first": list(left_ids), "second": list(right_ids),
                                   "status": "multiple_mentions_share_exact_evidence_no_unique_pair"})
    result = {
        "alignment_version": ALIGNMENT_VERSION, "annotation_id": payload["annotation_id"],
        "input_sha256": {"payload": _digest(payload), "first": _digest(first), "second": _digest(second)},
        "input_mention_counts": {side: len(mentions) for side, mentions in sides.items()},
        "groups": sorted(groups, key=lambda group: (group["target"], group["first"], group["second"])),
        "unique_exact_evidence_candidates": exact_pairs, "ambiguous_exact_evidence_keys": ambiguous_keys,
        "cross_target_overlaps": cross_target,
        "semantics": "original_inventory_relations_not_statement_equivalence_or_agreement_estimates",
        "limitations": ["requires_two_structurally_valid_codings_no_failed_slot_repair",
                        "shared_context_can_connect_distinct_mentions", "shared_omissions_not_detectable_from_two_inventories",
                        "exact_evidence_does_not_validate_axes_anchor_or_context_order",
                        "no_semantic_adjudication_or_source_authentication"],
    }
    result["alignment_sha256"] = _digest(result)
    return result


def validate_adjudication_accounting(item, first, second, adjudication):
    payload = construct_payload(item)
    alignment = align_construct_codings(payload, first, second)
    _fields(adjudication, ("schema_version", "annotation_id", "alignment_sha256", "decisions"), "adjudication")
    _require(adjudication["schema_version"] == ADJUDICATION_VERSION and adjudication["annotation_id"] == payload["annotation_id"]
             and adjudication["alignment_sha256"] == alignment["alignment_sha256"], "Adjudication source binding mismatch")
    _require(type(adjudication["decisions"]) is list, "Explicit adjudication decision list required")
    available = {segment["segment_id"]: segment["value"] for segment in payload["segments"]
                 if segment["availability"] in ("readable", "empty")}
    expected = {"first": {m["mention_id"] for m in first["mentions"]}, "second": {m["mention_id"] for m in second["mentions"]}}
    consumed, identities = {side: set() for side in expected}, set()
    for decision in adjudication["decisions"]:
        _fields(decision, ("decision_id", "first", "second", "relation", "reason", "spans"), "decision")
        identity, relation = decision["decision_id"], decision["relation"]
        _require(type(identity) is str and bool(identity) and identity not in identities, "Unique nonempty decision identity required")
        identities.add(identity)
        _require(type(relation) is str and relation in RELATIONS, "Unknown adjudication relation")
        _require(type(decision["reason"]) is str and bool(decision["reason"].strip()), "Substantive adjudication reason required")
        for side in expected:
            values = decision[side]
            _require(type(values) is list and all(type(value) is str for value in values), "Explicit original mention IDs required")
            _require(len(values) == len(set(values)) and set(values) <= expected[side] and not (set(values) & consumed[side]),
                     "Unknown or multiply consumed original mention")
            consumed[side].update(values)
        left, right = len(decision["first"]), len(decision["second"])
        shapes = {"same_mention": left == right == 1, "split_merge": left >= 1 and right >= 1 and left + right >= 3,
                  "distinct_mentions": left >= 1 and right >= 1, "first_only": left >= 1 and right == 0,
                  "second_only": left == 0 and right >= 1, "unresolved": left + right >= 1}
        _require(shapes[relation], "Adjudication relation/cardinality mismatch")
        _require(type(decision["spans"]) is list and bool(decision["spans"]), "Exact source evidence required for each decision")
        span_keys = set()
        for span in decision["spans"]:
            _fields(span, ("segment_id", "start", "end", "quote"), "adjudication span")
            _require(type(span["segment_id"]) is str and span["segment_id"] in available, "Unknown or unavailable adjudication segment")
            value = available[span["segment_id"]]
            _require(type(span["start"]) is int and type(span["end"]) is int and 0 <= span["start"] < span["end"] <= len(value)
                     and type(span["quote"]) is str and span["quote"] == value[span["start"]:span["end"]], "Adjudication span must match exact Unicode offsets")
            key = _span_key(span)
            _require(key not in span_keys, "Duplicate adjudication source span")
            span_keys.add(key)
    _require(consumed == expected, "Adjudication must account for every original mention exactly once")
    result = {
        "accounting_version": ADJUDICATION_VERSION, "annotation_id": payload["annotation_id"],
        "alignment_sha256": alignment["alignment_sha256"], "adjudication_sha256": _digest(adjudication),
        "input_mention_counts": alignment["input_mention_counts"],
        "consumed_mention_counts": {side: len(values) for side, values in consumed.items()},
        "decision_count": len(adjudication["decisions"]),
        "relation_decision_counts": {relation: sum(d["relation"] == relation for d in adjudication["decisions"]) for relation in RELATIONS},
        "decisions": copy.deepcopy(adjudication["decisions"]),
        "semantic_validation_established": False, "original_codings_replaced": False,
        "semantics": "conditional_source_span_and_inventory_accounting_only_no_consensus_coding_or_accuracy_estimate",
    }
    result["accounting_sha256"] = _digest(result)
    return result
