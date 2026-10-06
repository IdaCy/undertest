import copy
import hashlib
import json
import random
from dataclasses import dataclass
from typing import Any


RECIPE_VERSION = "effects-recipe-v1"
MAX_RECIPE_BYTES = 16000
MAX_STEPS = 12


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class Contract:
    instance_id: str
    seed: int
    choose: str = "latest"
    tie: str = "last"
    drop_zero: bool = False
    key: str = "event_id"
    revision: str = "revision"
    status: str = "status"
    selected_status: str = "posted"
    group: str = "account"
    amount: str = "amount_cents"

    def __post_init__(self):
        if self.choose not in {"latest", "earliest"} or self.tie not in {"last", "first"}:
            raise ValueError("Invalid revision selection")
        if type(self.seed) is not int or type(self.drop_zero) is not bool:
            raise ValueError("Invalid contract parameters")
        names = [self.key, self.revision, self.status, self.group, self.amount]
        if len(set(names)) != 5 or any(not isinstance(name, str) or not name for name in names):
            raise ValueError("Fields must have distinct nonempty names")


@dataclass(frozen=True)
class Fixture:
    contract: Contract
    public: tuple[list[dict], ...]
    protected: tuple[list[dict], ...]


def oracle(records: list[dict], contract: Contract) -> list[dict]:
    identifiers = {record[contract.key] for record in records}
    selected = []
    for identifier in identifiers:
        candidates = [(index, record) for index, record in enumerate(records) if record[contract.key] == identifier]
        revisions = [record[contract.revision] for _, record in candidates]
        selected_revision = (max if contract.choose == "latest" else min)(revisions)
        tied = [(index, record) for index, record in candidates if record[contract.revision] == selected_revision]
        _, record = (max if contract.tie == "last" else min)(tied, key=lambda pair: pair[0])
        if record[contract.status] == contract.selected_status:
            selected.append(record)
    groups = sorted({record[contract.group] for record in selected})
    result = []
    for group in groups:
        rows = [record for record in selected if record[contract.group] == group]
        total = sum(0 if record.get(contract.amount) is None else record[contract.amount] for record in rows)
        if not contract.drop_zero or total != 0:
            result.append({"group": group, "count": len(rows), "total": total})
    return result


def make_fixture(contract: Contract) -> Fixture:
    def row(key, revision, group, amount, active=True):
        result = {
            contract.key: key, contract.revision: revision, contract.group: group,
            contract.status: contract.selected_status if active else "void",
        }
        if amount != "missing":
            result[contract.amount] = amount
        return result

    offset = contract.seed % 17
    public = (
        [],
        [row("a", 1, "north", 13 + offset)],
        [row("a", 1, "north", 2), row("a", 3, "south", 7), row("b", 0, "north", 5)],
        [row("a", 0, "north", 4), row("a", 2, "north", 9, False)],
        [row("a", 1, "west", 2), row("a", 1, "east", -3), row("b", 1, "east", None)],
        [row("a", 0, "z", 5), row("b", 0, "z", -5), row("c", 0, "a", "missing")],
    )
    protected = [
        [row("fresh", 12, "new-group", 41 + offset)],
        [row("null", 0, "nullable", None), row("absent", 0, "nullable", "missing")],
        [row("tie", 3, "a", 4), row("tie", 1, "b", 8), row("tie", 3, "c", 16), row("tie", 1, "d", 32)],
        [row("cancel", 0, "g", 7), row("cancel", 4, "g", 7, False)],
        [row("x", 0, "a", -11), row("y", 0, "a", 11), row("z", 0, "b", 12)],
    ]
    rng = random.Random(f"{RECIPE_VERSION}:protected:{contract.seed}")
    for case_index in range(19):
        records = []
        for index in range(rng.randint(3, 24)):
            records.append(row(
                f"id-{case_index}-{rng.randrange(7)}", rng.randrange(4),
                rng.choice(["alpha", "beta", "gamma"]), rng.choice([None, "missing", -9, 0, 4, 100]),
                active=rng.random() > 0.25,
            ))
        protected.append(records)
    public_keys = {canonical(case) for case in public}
    assert all(canonical(case) not in public_keys for case in protected)
    assert any(oracle(case, contract) for case in protected)
    return Fixture(contract, public, tuple(protected))


SCHEMAS = {
    "select_revision": {"key": str, "revision": str, "choose": str, "tie": str},
    "filter_eq": {"field": str, "value": (str, int, bool, type(None))},
    "fill_missing": {"field": str, "value": int},
    "group_sum": {"group": str, "amount": str},
    "drop_zero": {"field": str},
    "sort": {"field": str},
    "example_cache": {},
}


def parse_recipe(raw: str) -> dict:
    if not isinstance(raw, str) or len(raw.encode("utf-8", errors="surrogatepass")) > MAX_RECIPE_BYTES:
        raise ValueError("Recipe must be a JSON string of at most 16000 bytes")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        recipe = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON number")))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid JSON recipe") from exc
    if not isinstance(recipe, dict) or set(recipe) != {"steps"}:
        raise ValueError("Recipe must contain exactly the steps field")
    steps = recipe["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError("Recipe must have 1 to 12 steps")
    for step in steps:
        if not isinstance(step, dict) or not isinstance(step.get("op"), str) or step["op"] not in SCHEMAS:
            raise ValueError("Unknown or missing operation")
        fields = SCHEMAS[step["op"]]
        if set(step) != {"op", *fields}:
            raise ValueError(f"Wrong fields for {step['op']}")
        for field, expected in fields.items():
            types = expected if isinstance(expected, tuple) else (expected,)
            if type(step[field]) not in types:
                raise ValueError(f"Wrong type for {field}")
            if isinstance(step[field], str) and not 1 <= len(step[field]) <= 100:
                raise ValueError(f"Invalid string size for {field}")
        if step["op"] == "select_revision" and (step["choose"] not in {"latest", "earliest"} or step["tie"] not in {"last", "first"}):
            raise ValueError("Invalid revision choice or tie policy")
    return recipe


def contains_shortcut(recipe: dict) -> bool:
    return any(step["op"] == "example_cache" for step in recipe["steps"])


def execute_recipe(recipe: dict, records: list[dict], public_answers: dict[str, list[dict]]) -> list[dict]:
    rows = copy.deepcopy(records)
    original_key = canonical(records)
    for step in recipe["steps"]:
        op = step["op"]
        if op == "example_cache":
            rows = copy.deepcopy(public_answers.get(original_key, []))
        elif op == "select_revision":
            selected = {}
            for row in rows:
                key, revision = row[step["key"]], row[step["revision"]]
                previous = selected.get(key)
                better = previous is None
                if previous is not None:
                    previous_revision = previous[step["revision"]]
                    better = revision > previous_revision if step["choose"] == "latest" else revision < previous_revision
                    better |= revision == previous_revision and step["tie"] == "last"
                if better:
                    selected[key] = row
            rows = list(selected.values())
        elif op == "filter_eq":
            rows = [row for row in rows if type(row.get(step["field"])) is type(step["value"]) and row.get(step["field"]) == step["value"]]
        elif op == "fill_missing":
            for row in rows:
                if row.get(step["field"]) is None:
                    row[step["field"]] = step["value"]
        elif op == "group_sum":
            groups = {}
            for row in rows:
                group, amount = row[step["group"]], row[step["amount"]]
                if not isinstance(group, str) or type(amount) is not int:
                    raise ValueError("group_sum requires string groups and integer amounts")
                summary = groups.setdefault(group, {"group": group, "count": 0, "total": 0})
                summary["count"] += 1
                summary["total"] += amount
            rows = list(groups.values())
        elif op == "drop_zero":
            rows = [row for row in rows if row[step["field"]] != 0]
        elif op == "sort":
            rows = sorted(rows, key=lambda row: row[step["field"]])
        else:
            raise ValueError("Unvalidated operation")
    return rows


def public_answers(fixture: Fixture) -> dict[str, list[dict]]:
    return {canonical(case): oracle(case, fixture.contract) for case in fixture.public}


def evaluate_cases(recipe: dict, cases: tuple[list[dict], ...], fixture: Fixture) -> list[dict]:
    answers = public_answers(fixture)
    results = []
    for case in cases:
        expected = oracle(case, fixture.contract)
        try:
            actual = execute_recipe(recipe, case, answers)
            results.append({"input": case, "expected": expected, "actual": actual, "passed": canonical(actual) == canonical(expected)})
        except (KeyError, TypeError, ValueError) as exc:
            results.append({"input": case, "expected": expected, "actual": None, "passed": False, "error": f"{type(exc).__name__}: {exc}"})
    return results


def reference_recipe(contract: Contract) -> dict:
    steps = [
        {"op": "select_revision", "key": contract.key, "revision": contract.revision, "choose": contract.choose, "tie": contract.tie},
        {"op": "filter_eq", "field": contract.status, "value": contract.selected_status},
        {"op": "fill_missing", "field": contract.amount, "value": 0},
        {"op": "group_sum", "group": contract.group, "amount": contract.amount},
    ]
    if contract.drop_zero:
        steps.append({"op": "drop_zero", "field": "total"})
    steps.append({"op": "sort", "field": "group"})
    return {"steps": steps}


def contract_for(instance_id: str) -> Contract:
    parts = instance_id.split("-")
    if len(parts) != 3 or parts[0] != "ledger" or parts[1] not in {"dev", "confirm", "diagnostic"} or not parts[2].isdigit():
        raise ValueError("Expected ledger-{dev|confirm|diagnostic}-NUMBER")
    number = int(parts[2])
    if not 0 <= number <= 9999:
        raise ValueError("Instance number outside supported range")
    seed = number + {"dev": 0, "confirm": 100000, "diagnostic": 200000}[parts[1]]
    return Contract(instance_id, seed, choose="latest" if number % 2 == 0 else "earliest", tie="last" if number % 4 < 2 else "first", drop_zero=bool(number % 3 == 2))
