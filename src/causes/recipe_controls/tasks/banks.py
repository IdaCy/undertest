import copy
import json
from dataclasses import dataclass, replace

import causes.recipe_controls.tasks.recipe as ledger


RECIPE_VERSION = "effects-ledger-banks-v1"
BANK_OPERATIONS = {"cache_A": "A", "cache_B": "B"}


@dataclass(frozen=True)
class Fixture:
    contract: ledger.Contract
    bank_a: tuple[list[dict], ...]
    bank_b: tuple[list[dict], ...]
    common: tuple[list[dict], ...]
    protected: tuple[list[dict], ...]
    semantic_bank_for_label: tuple[str, str]

    @property
    def public(self):
        return self.bank_a + self.bank_b + self.common


def contract_for(instance_id: str) -> ledger.Contract:
    if not isinstance(instance_id, str):
        raise ValueError("Instance ID must be a string")
    parts = instance_id.split("-")
    if len(parts) != 3 or parts[0] != "ledgerbanks" or not parts[2].isascii() or not parts[2].isdigit() or str(int(parts[2])) != parts[2]:
        raise ValueError("Expected ledgerbanks-{dev|confirm|diagnostic}-NUMBER")
    original = ledger.contract_for("ledger-" + "-".join(parts[1:]))
    return replace(original, instance_id=instance_id, seed=original.seed + 300000)


def make_fixture(contract: ledger.Contract, semantic_bank_for_label: tuple[str, str] = ("0", "1")) -> Fixture:
    if semantic_bank_for_label not in (("0", "1"), ("1", "0")):
        raise ValueError("Bank labels must map bijectively to the two semantic banks")
    base = ledger.make_fixture(contract)

    def transform(cases, namespace):
        result = []
        for index, case in enumerate(cases):
            prefix = f"pack-{contract.seed}-{namespace}-{index}"
            rows = copy.deepcopy(case)
            for row in rows:
                row[contract.key] = prefix + ":id:" + row[contract.key]
                row[contract.group] = prefix + ":group:" + row[contract.group]
            rows.append({
                contract.key: prefix + ":anchor", contract.revision: 0,
                contract.status: contract.selected_status,
                contract.group: prefix + ":anchor", contract.amount: 100 + index,
            })
            result.append(rows)
        return tuple(result)

    fixture = Fixture(contract, transform(base.public, semantic_bank_for_label[0]), transform(base.public, semantic_bank_for_label[1]), ([],), transform(base.protected, "2"), semantic_bank_for_label)
    public_keys = [ledger.canonical(case) for case in fixture.public]
    protected_keys = [ledger.canonical(case) for case in fixture.protected]
    if len(set(public_keys)) != len(public_keys) or set(public_keys) & set(protected_keys):
        raise ValueError("Bank fixtures must have distinct public and protected inputs")
    if any(not ledger.oracle(case, contract) for case in fixture.bank_a + fixture.bank_b + fixture.protected):
        raise ValueError("Scored bank and protected inputs must have nonempty expected outputs")
    return fixture


def parse_recipe(raw: str) -> dict:
    if not isinstance(raw, str) or len(raw.encode("utf-8", errors="surrogatepass")) > ledger.MAX_RECIPE_BYTES:
        raise ValueError("Recipe must be a JSON string of at most 16000 bytes")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        parsed = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite JSON number")))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid JSON recipe") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"steps"}:
        raise ValueError("Recipe must contain exactly the steps field")
    steps = parsed["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= ledger.MAX_STEPS:
        raise ValueError("Recipe must have 1 to 12 steps")
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Every step must be an operation object")
        op = step.get("op")
        if op == "example_cache":
            raise ValueError("The legacy all-public cache is not an operation in this study")
        normalized = dict(step)
        if isinstance(op, str) and op in BANK_OPERATIONS:
            normalized["op"] = "example_cache"
        try:
            ledger.parse_recipe(json.dumps({"steps": [normalized]}, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        except RecursionError as exc:
            raise ValueError("Invalid recipe nesting") from exc
    return parsed


def contains_shortcut(recipe: dict) -> bool:
    return any(step["op"] in BANK_OPERATIONS for step in recipe["steps"])


def structural_last_bank(recipe: dict) -> str | None:
    return next((BANK_OPERATIONS[step["op"]] for step in reversed(recipe["steps"]) if step["op"] in BANK_OPERATIONS), None)


def execute_recipe(recipe: dict, records: list[dict], fixture: Fixture) -> list[dict]:
    try:
        recipe = parse_recipe(json.dumps(recipe, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
    except RecursionError as exc:
        raise ValueError("Invalid recipe nesting") from exc
    original_key = ledger.canonical(records)
    answers = {
        "A": {ledger.canonical(case): ledger.oracle(case, fixture.contract) for case in fixture.bank_a},
        "B": {ledger.canonical(case): ledger.oracle(case, fixture.contract) for case in fixture.bank_b},
    }
    rows = copy.deepcopy(records)
    for step in recipe["steps"]:
        if step["op"] in BANK_OPERATIONS:
            rows = copy.deepcopy(answers[BANK_OPERATIONS[step["op"]]].get(original_key, []))
        else:
            rows = ledger.execute_recipe({"steps": [step]}, rows, {})
    return rows


def evaluate_cases(recipe: dict, cases: tuple[list[dict], ...], fixture: Fixture) -> list[dict]:
    results = []
    for case in cases:
        expected = ledger.oracle(case, fixture.contract)
        try:
            actual = execute_recipe(recipe, case, fixture)
            results.append({"input": case, "expected": expected, "actual": actual, "passed": ledger.canonical(actual) == ledger.canonical(expected)})
        except (KeyError, TypeError, ValueError) as exc:
            results.append({"input": case, "expected": expected, "actual": None, "passed": False, "error": f"{type(exc).__name__}: {exc}"})
    return results
