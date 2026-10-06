"""Join already audited observations to a fixed assignment roster.

This is downstream accounting, not a custody verifier, scorer, or dispatcher.
The caller must authenticate the roster and audit each observation's identity,
custody and termination before supplying it. Uncertain observations are retained
as unknown even when a provisional scorer supplied a Boolean value.
"""

from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Literal


ENDPOINTS = ("segmented", "raw", "conservative")
TERMINATIONS = {"returned", "cancelled", "failed", "length_truncated", "unknown"}


@dataclass(frozen=True)
class Assignment:
    physical_id: str
    checkpoint_id: str
    sample_id: str
    prompt_sha256: str
    seed: int
    sample_kind: str
    coefficient: Fraction | None


@dataclass(frozen=True)
class Observation:
    physical_id: str
    checkpoint_id: str
    sample_id: str
    prompt_sha256: str
    seed: int
    attempt_id: str
    custody: Literal["accepted", "uncertain"]
    termination: str
    segmented: bool | None
    raw: bool | None
    conservative: bool | None


@dataclass(frozen=True)
class Disposition:
    assignment: Assignment
    attempt_id: str | None
    status: str
    segmented: bool | None
    raw: bool | None
    conservative: bool | None


def reconcile(
    assignments: Iterable[Assignment], observations: Iterable[Observation]
) -> tuple[Disposition, ...]:
    """Preserve every assignment and refuse duplicate or mismatched evidence.

    Accepted custody is an upstream input assertion, not proved here. The only
    observations eligible for known endpoints have both accepted custody and
    a returned, nontruncated termination. Each endpoint keeps its own missingness.
    """
    ordered = tuple(assignments)
    if not ordered:
        raise ValueError("The fixed assignment roster cannot be empty")
    by_id = {}
    for assignment in ordered:
        if not isinstance(assignment, Assignment):
            raise ValueError("Expected an Assignment")
        if not assignment.physical_id or assignment.physical_id in by_id:
            raise ValueError("Empty or duplicate physical assignment")
        if assignment.physical_id != assignment.checkpoint_id + "::" + assignment.sample_id:
            raise ValueError("Physical identity differs from checkpoint/sample")
        if type(assignment.seed) is not int or not 0 <= assignment.seed < 2**31:
            raise ValueError("Invalid assignment seed")
        if assignment.sample_kind == "main":
            if not isinstance(assignment.coefficient, Fraction):
                raise ValueError("Main assignment requires an exact coefficient")
        elif assignment.coefficient is not None:
            raise ValueError("Controls cannot carry a main contrast coefficient")
        by_id[assignment.physical_id] = assignment

    seen = {}
    attempt_ids = set()
    for observation in observations:
        if not isinstance(observation, Observation):
            raise ValueError("Expected an Observation")
        key = observation.physical_id
        if key not in by_id or key in seen:
            raise ValueError("Outside-roster or repeated physical observation")
        if not observation.attempt_id or observation.attempt_id in attempt_ids:
            raise ValueError("Empty or reused attempt identity")
        assignment = by_id[key]
        for field in ("checkpoint_id", "sample_id", "prompt_sha256", "seed"):
            value = getattr(observation, field)
            if type(value) is not type(getattr(assignment, field)) or value != getattr(assignment, field):
                raise ValueError("Response-to-assignment mismatch: " + field)
        if observation.custody not in ("accepted", "uncertain"):
            raise ValueError("Unknown custody disposition")
        if observation.termination not in TERMINATIONS:
            raise ValueError("Unknown termination disposition")
        for endpoint in ENDPOINTS:
            value = getattr(observation, endpoint)
            if value is not None and type(value) is not bool:
                raise ValueError("Endpoint must be Boolean or unknown")
        seen[key] = observation
        attempt_ids.add(observation.attempt_id)

    result = []
    for assignment in ordered:
        observation = seen.get(assignment.physical_id)
        if observation is None:
            result.append(Disposition(assignment, None, "no_record", None, None, None))
            continue
        known_allowed = observation.custody == "accepted" and observation.termination == "returned"
        status = observation.termination if observation.custody == "accepted" else "uncertain_custody"
        endpoints = [getattr(observation, name) if known_allowed else None for name in ENDPOINTS]
        result.append(Disposition(assignment, observation.attempt_id, status, *endpoints))
    return tuple(result)


def identification_bounds(
    assignments: Iterable[Assignment], observations: Iterable[Observation], endpoint: str
) -> dict:
    """Reconcile the full fixed roster before computing signed bounds.

    Supply the authenticated assignment roster, including absent stages and
    cells. Do not construct it from the observations. These are identification
    bounds, not a confidence interval or proof of the upstream custody audit.
    """
    if endpoint not in ENDPOINTS:
        raise ValueError("Unknown endpoint")
    lower = upper = Fraction(0)
    known = unknown = 0
    for row in reconcile(assignments, observations):
        if row.assignment.sample_kind != "main":
            continue
        weight = row.assignment.coefficient
        if not isinstance(weight, Fraction):
            raise ValueError("Main assignment requires an exact coefficient")
        value = getattr(row, endpoint)
        if value is not None and type(value) is not bool:
            raise ValueError("Endpoint must be Boolean or unknown")
        if value is None:
            unknown += 1
            lower += min(weight, 0)
            upper += max(weight, 0)
        else:
            known += 1
            lower += weight * value
            upper += weight * value
    if known + unknown == 0:
        raise ValueError("A main contrast requires main assignments")
    return {"known_main": known, "unknown_main": unknown, "lower": lower,
            "upper": upper, "width": upper - lower}
