"""Select frozen primary-study inputs in physical roster order.

The caller supplies trusted byte identities for the already validated study
roster and panel. This join does not allocate calls, validate model/tokenizer
identity, reconstruct the task oracles, or establish result custody.
"""

from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import re

from emergence.checkpoints.fresh_reconciliation import Assignment


CHECKPOINTS = ("olmo3_7b_think_sft", "olmo3_7b_think_released")
KINDS = {"main": 512, "competence": 32, "format": 32, "score_comprehension": 64}
MAX_DOCUMENT_BYTES = 8 * 1024**2


@dataclass(frozen=True)
class ByteIdentity:
    size: int
    sha256: str


@dataclass(frozen=True)
class SelectedInput:
    assignment: Assignment
    prompt: str
    metadata_json: bytes


@dataclass(frozen=True)
class PrimaryInputs:
    roster: ByteIdentity
    panel: ByteIdentity
    rows: tuple[SelectedInput, ...]

    def for_checkpoint(self, checkpoint_id: str) -> tuple[SelectedInput, ...]:
        if checkpoint_id not in CHECKPOINTS:
            raise ValueError("Checkpoint is outside the fixed primary study")
        return tuple(row for row in self.rows if row.assignment.checkpoint_id == checkpoint_id)


def _require(value, message):
    if not value:
        raise ValueError(message)


def _document(raw: bytes, identity: ByteIdentity) -> dict:
    _require(type(identity) is ByteIdentity, "Expected a trusted byte identity")
    _require(type(identity.size) is int and 0 < identity.size <= MAX_DOCUMENT_BYTES,
             "Document size is outside the input bound")
    _require(type(identity.sha256) is str and re.fullmatch(r"[0-9a-f]{64}", identity.sha256),
             "Invalid expected digest")
    _require(type(raw) is bytes and len(raw) == identity.size
             and hashlib.sha256(raw).hexdigest() == identity.sha256,
             "Frozen input bytes differ")

    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError("Nonfinite JSON input")

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
    _require(type(value) is dict, "Input document must be an object")
    return value


def load_primary_inputs(
    roster_raw: bytes, panel_raw: bytes, *, roster: ByteIdentity, panel: ByteIdentity
) -> PrimaryInputs:
    """Join exact saved bytes without rebuilding or reordering a subset panel."""
    frame = _document(roster_raw, roster)
    content = _document(panel_raw, panel)
    _require(frame.get("schema") == "section-a-primary640-saved-roster-candidate.v1",
             "Unsupported primary roster schema")
    assigned = frame.get("primary_rows")
    samples = content.get("samples")
    _require(type(assigned) is list and len(assigned) == 640, "Complete primary roster required")
    _require(type(samples) is list and len(samples) == 320, "Complete primary panel required")
    by_id = {}
    for sample in samples:
        _require(type(sample) is dict, "Invalid panel sample")
        key = sample.get("id")
        _require(type(key) is str and key and key not in by_id, "Duplicate or invalid sample ID")
        prompt, metadata = sample.get("input"), sample.get("metadata")
        _require(type(prompt) is str and prompt and type(metadata) is dict, "Invalid sample content")
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        _require(metadata.get("input_sha256") == digest, "Panel prompt identity differs")
        metadata_json = json.dumps(metadata, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        by_id[key] = (prompt, metadata, digest, metadata_json)

    rows, seen, used_samples = [], set(), set()
    for row in assigned:
        _require(type(row) is dict, "Invalid physical assignment")
        checkpoint, sample_id = row.get("checkpoint_id"), row.get("sample_id")
        _require(type(checkpoint) is str and checkpoint in CHECKPOINTS
                 and type(sample_id) is str and sample_id in by_id, "Unknown assigned input")
        key = row.get("physical_id")
        _require(type(key) is str and key == checkpoint + "::" + sample_id and key not in seen,
                 "Duplicate or mismatched physical identity")
        _require(row.get("block") == "primary_core_anchor", "Deferred block entered primary roster")
        prompt, metadata, digest, metadata_json = by_id[sample_id]
        for name in ("seed", "replicate", "sample_kind", "family", "rule_split", "cue_variant", "framing", "grading"):
            _require(name in row and name in metadata and type(row[name]) is type(metadata[name])
                     and row[name] == metadata[name], "Roster/panel metadata differs: " + name)
        _require(type(row["seed"]) is int and 0 <= row["seed"] < 2**31, "Invalid generation seed")
        _require(type(row["replicate"]) is int and row["replicate"] >= 0, "Invalid rotation index")
        _require(row.get("prompt_sha256") == digest and row.get("rule_id") == metadata.get("instance_id")
                 and row.get("group_id") == row["family"] + "/" + metadata["cluster_id"],
                 "Roster/panel prompt or rule identity differs")
        kind = row["sample_kind"]
        _require(kind in KINDS, "Unknown sample kind")
        coefficient = row.get("coefficient_for_own_cue_contrast")
        if kind == "main":
            _require(row["grading"] in ("limited", "broad"), "Invalid main grading condition")
            sign = (1 if checkpoint == CHECKPOINTS[1] else -1) * (1 if row["grading"] == "limited" else -1)
            _require(type(coefficient) is list and len(coefficient) == 2
                     and all(type(v) is int for v in coefficient) and coefficient == [sign, 128],
                     "Main contrast weight differs from the primary design")
            weight = Fraction(sign, 128)
        else:
            _require(coefficient is None, "A control cannot carry a main contrast weight")
            weight = None
        assignment = Assignment(key, checkpoint, sample_id, digest, row["seed"], kind, weight)
        rows.append(SelectedInput(assignment, prompt, metadata_json))
        seen.add(key)
        used_samples.add(sample_id)
    _require(used_samples == set(by_id), "Unassigned sample in primary panel")
    _require(Counter(row.assignment.sample_kind for row in rows) == KINDS, "Primary sample counts differ")
    for checkpoint in CHECKPOINTS:
        _require(Counter(row.assignment.sample_kind for row in rows if row.assignment.checkpoint_id == checkpoint)
                 == {kind: count // 2 for kind, count in KINDS.items()}, "Checkpoint frame is incomplete")
    return PrimaryInputs(roster, panel, tuple(rows))
