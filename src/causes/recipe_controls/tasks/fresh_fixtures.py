import copy
import hashlib
import itertools
import json
import random
import re
from dataclasses import asdict, dataclass, replace

import causes.recipe_controls.tasks.recipe as ledger
import causes.recipe_controls.tasks.validator as validator


FIXTURE_VERSION = "effects-fresh-fixtures-v2"
AUDIT_VERSION = "effects-exact-overlap-v1"
SPLITS = ("development", "confirmation", "transfer", "diagnostic")
ENGINES = {"ledger": ledger, "validator": validator}
MISSING = object()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


EMPTY_INPUT_SHA256 = digest([])


@dataclass(frozen=True)
class FreshFixture:
    family: str
    split: str
    seed: int
    contract: ledger.Contract | validator.Contract
    public: tuple[list, ...]
    protected: tuple[list, ...]
    public_names: tuple[str, ...]
    protected_names: tuple[str, ...]
    manifest: dict


class OverlapError(ValueError):
    def __init__(self, report):
        super().__init__("Unexpected exact input overlap; see report")
        self.report = report


def _identity(family, split, seed, contract):
    if type(family) is not str or family not in ENGINES or type(contract) is not ENGINES[family].Contract:
        raise ValueError("Family and exact existing contract type must agree")
    if type(split) is not str or split not in SPLITS or type(seed) is not int or not 0 <= seed < 2**63:
        raise ValueError("Expected a registered split and exact nonnegative 63-bit seed")
    if type(contract.instance_id) is not str or not contract.instance_id:
        raise ValueError("The native contract must have a nonempty instance identity")
    if family == "ledger" and type(contract.selected_status) is not str:
        raise ValueError("The native ledger input domain requires a string status")
    ENGINES[family].parse_recipe(canonical(ENGINES[family].reference_recipe(contract)))
    return {"version": FIXTURE_VERSION, "family": family, "split": split, "seed": seed,
            "contract": asdict(contract), "engine_version": ENGINES[family].RECIPE_VERSION}


def _stream(identity, partition, case_name):
    key = digest({"identity": identity, "partition": partition, "case_name": case_name})
    return random.Random(key), "u" + key[:20]


def _interleave(groups, rng):
    pending = [copy.deepcopy(group) for group in groups if group]
    result = []
    while pending:
        index = rng.randrange(len(pending))
        result.append(pending[index].pop(0))
        if not pending[index]:
            del pending[index]
    return result


def _ledger_blocks(contract, rng, prefix):
    serial = itertools.count()

    def key():
        return prefix + "e" + str(next(serial))

    def row(identity, revision, group, amount, active=True):
        value = {contract.key: identity, contract.revision: revision, contract.group: prefix + "g" + group,
                 contract.status: contract.selected_status if active else contract.selected_status + "_other"}
        if amount is not MISSING:
            value[contract.amount] = amount
        return value

    low = rng.randrange(0, 30)
    high = low + rng.randrange(1, 7)
    winning, losing = (high, low) if contract.choose == "latest" else (low, high)
    selected, tied, filtered = key(), key(), key()
    magnitude = rng.randrange(8, 24)
    blocks = {
        "select": [row(selected, low, "b", 3), row(selected, high, "c", -magnitude)],
        "tie": [row(tied, winning, "d", 4), row(tied, winning, "e", -9)],
        "filter_order": [row(filtered, losing, "f", 11), row(filtered, winning, "f", 19, False)],
        "fill": [row(key(), low, "h", MISSING), row(key(), high, "h", None), row(key(), low, "h", 4)],
        "group_signed_count": [row(key(), low, "i", -magnitude), row(key(), high, "i", 2)],
        "zero_policy": [row(key(), low, "j", magnitude), row(key(), high, "j", -magnitude)],
        "sort": [row(key(), low, "z", 3), row(key(), high, "a", 7)],
    }
    noise = []
    for index in range(rng.randrange(2, 6)):
        identity = prefix + "n" + str(index)
        revision = rng.randrange(20)
        noise.append([row(identity, revision, "noise" + str(index % 2), rng.randrange(1, 20))])
        if rng.choice((False, True)):
            noise[-1].append(row(identity, revision + rng.randrange(1, 5), "noise" + str(index % 2), rng.randrange(1, 20)))
    return blocks, noise


def _validator_blocks(contract, rng, prefix):
    serial = itertools.count()
    low, high = contract.minimum, contract.maximum
    start, end = (low, high) if contract.relation == "ge" else (high, low)

    def row(**changes):
        value = {"id": prefix + "e" + str(next(serial)), "start": start, "end": end}
        value.update(changes)
        return value

    missing = {}
    for field in ("id", "start", "end"):
        value = row()
        del value[field]
        missing["missing_" + field] = [value]
    repeated, invalid_first = prefix + "repeat", prefix + "reserved"
    first = row(id=invalid_first)
    del first["start"]
    label = "" if contract.label_kind == "string" else rng.randrange(-30, 31)
    wrong_label = 17 if contract.label_kind == "string" else "text"
    blocks = {**missing,
              "nondict": [None, [], False, 3.5, "row"],
              "id_type": [row(id=17)], "empty_id": [row(id="")],
              "start_type": [row(start=True)], "end_type": [row(end=False)],
              "float_types": [row(start=float(start)), row(end=float(end))],
              "lower_bound": [row(start=low - 1, end=low - 1)],
              "upper_bound": [row(start=high + 1, end=high + 1)],
              "start_bound_only": [row(start=low - 1, end=low) if contract.relation == "ge" else row(start=high + 1, end=high)],
              "end_bound_only": [row(start=high, end=high + 1) if contract.relation == "ge" else row(start=low, end=low - 1)],
              "inclusive_bounds": [row(start=low, end=low), row(start=high, end=high)],
              "comparison": [row(start=end, end=start)], "strict_valid_relation": [row()],
              "duplicate_ids": [row(id=repeated), row(id=repeated)],
              "invalid_first_duplicate": [first, row(id=invalid_first)],
              "label_type": [row(label=wrong_label)], "null_label": [row(label=None)],
              "bool_label": [row(label=True)], "valid_label": [row(label=label)],
              "optional_label_absent": [row()],
              "unknown_field": [row(extra={"items": [1, None]})],
              "literal_ids": [row(id=value) for value in ("A", "a", " ", "  ")]}
    noise = []
    for index in range(rng.randrange(2, 6)):
        left, right = sorted((rng.randint(low, high), rng.randint(low, high)))
        a, b = (left, right) if contract.relation == "ge" else (right, left)
        noise.append([row(start=a, end=b)])
    return blocks, noise


def _check_domain(family, contract, case):
    if type(case) is not list or len(case) > 64 or len(canonical(case).encode()) > 64000:
        raise ValueError("Generated batch exceeds the bounded JSON input envelope")
    if family == "validator":
        validator._check_batch(case)
        return
    for row in case:
        if (type(row) is not dict or type(row.get(contract.key)) is not str or not row[contract.key]
                or type(row.get(contract.revision)) is not int or row[contract.revision] < 0
                or type(row.get(contract.status)) is not str or type(row.get(contract.group)) is not str
                or (row.get(contract.amount) is not None and type(row[contract.amount]) is not int)):
            raise ValueError("Generated ledger row is outside the task domain")


def _case_records(pack, partition):
    engine = ENGINES[pack.family]
    cases = getattr(pack, partition)
    names = getattr(pack, partition + "_names")
    if (type(cases) is not tuple or type(names) is not tuple
            or any(type(name) is not str or not name for name in names)
            or len(cases) != len(names) or len(set(names)) != len(names)):
        raise ValueError("Case names must be unique and complete within a partition")
    records = []
    for name, case in zip(names, cases):
        _check_domain(pack.family, pack.contract, case)
        expected = engine.oracle(case, pack.contract)
        records.append({"case_name": name, "input_sha256": digest(case), "expected_sha256": digest(expected),
                        "pair_sha256": digest({"input": case, "expected": expected}), "row_count": len(case)})
    return records


def _manifest(pack):
    identity = _identity(pack.family, pack.split, pack.seed, pack.contract)
    cases = {partition: _case_records(pack, partition) for partition in ("public", "protected")}
    base = {**identity, "cases": cases, "public_sha256": digest(cases["public"]), "protected_sha256": digest(cases["protected"]),
            "intentional_shared_input": {"input_sha256": EMPTY_INPUT_SHA256, "description": "empty JSON array"},
            "novelty_claim": "exact_inputs_within_declared_audit_universe_not_new_task_semantics"}
    return {**base, "pack_id": "effects-fresh-" + digest(base)}


def verify_fixture(pack):
    if type(pack) is not FreshFixture or canonical(_manifest(pack)) != canonical(pack.manifest):
        raise ValueError("Fresh fixture differs from its content/identity manifest")


def generate_fixture(family, contract, *, seed, split):
    identity = _identity(family, split, seed, contract)
    builder = _ledger_blocks if family == "ledger" else _validator_blocks
    rng, prefix = _stream(identity, "structure", "catalog")
    catalog, _ = builder(contract, rng, prefix)
    names = tuple(catalog)
    public_names = ("empty",) + tuple(f"mixed_{index}" for index in range(6))
    protected_names = names + tuple(f"mixed_{index}" for index in range(4))
    public, protected = [[]], []
    for partition, case_names, destination in (("public", public_names[1:], public), ("protected", protected_names, protected)):
        for case_name in case_names:
            rng, prefix = _stream(identity, partition, case_name)
            blocks, noise = builder(contract, rng, prefix)
            if case_name in blocks:
                others = [name for name in names if name != case_name]
                chosen = [case_name] + rng.sample(others, rng.randrange(0, 3))
            else:
                chosen = rng.sample(list(names), rng.randrange(3, min(7, len(names)) + 1))
            destination.append(_interleave([blocks[name] for name in chosen] + noise, rng))
    if family == "validator":
        rng, prefix = _stream(identity, "protected", "max_rows")
        low, high = contract.minimum, contract.maximum
        start, end = (low, high) if contract.relation == "ge" else (high, low)
        maximum = [{"id": prefix + str(index), "start": start, "end": end} for index in range(64)]
        maximum[rng.randrange(64)]["start"] = True
        protected.append(maximum)
        protected_names += ("max_rows",)
    pack = FreshFixture(family, split, seed, contract, tuple(public), tuple(protected), public_names, protected_names, {})
    pack = replace(pack, manifest=_manifest(pack))
    legacy = ENGINES[family].make_fixture(contract)
    baseline = reference_registry("legacy-same-contract-" + digest(asdict(contract)), family, "legacy", public=legacy.public, protected=legacy.protected)
    audit_overlap([pack], references=[baseline])
    validate_references(pack)
    return pack


def public_examples(pack):
    verify_fixture(pack)
    return [{"input": copy.deepcopy(case), "expected": ENGINES[pack.family].oracle(case, pack.contract)} for case in pack.public]


def render_public_examples(pack):
    return "Published examples:\n" + json.dumps(public_examples(pack), ensure_ascii=False, indent=2, allow_nan=False)


def reference_registry(source_id, family, split, *, public, protected):
    if (type(source_id) is not str or not source_id or type(family) is not str or family not in ENGINES
            or type(split) is not str or not split):
        raise ValueError("Reference registry requires an explicit source/family/split")
    partitions = {}
    for partition, cases in (("public", public), ("protected", protected)):
        if type(cases) not in (list, tuple) or any(type(case) is not list for case in cases):
            raise ValueError("Reference cases must be JSON arrays")
        partitions[partition] = [digest(case) for case in cases]
    payload = {"source_id": source_id, "family": family, "split": split, "partitions": partitions}
    return {**payload, "registry_sha256": digest(payload)}


def legacy_registries(instance_ids):
    if type(instance_ids) not in (list, tuple):
        raise ValueError("Legacy IDs must be an explicit sequence")
    result = []
    for instance_id in instance_ids:
        if type(instance_id) is not str or re.fullmatch(r"(ledger|validator)-(dev|confirm|diagnostic)-(0|[1-9][0-9]{0,3})", instance_id) is None:
            raise ValueError("Expected a canonical legacy instance ID")
        family, split, _ = instance_id.split("-")
        engine = ENGINES[family]
        fixture = engine.make_fixture(engine.contract_for(instance_id))
        result.append(reference_registry(f"legacy:{engine.RECIPE_VERSION}:{instance_id}", family, split, public=fixture.public, protected=fixture.protected))
    return result


def audit_overlap(packs, *, references=(), reject=True):
    if type(packs) not in (list, tuple) or not packs or type(references) not in (list, tuple) or type(reject) is not bool:
        raise ValueError("Audit requires fresh packs and an explicit reference-registry sequence")
    occurrences = {}
    sources = set()
    registries = []
    for pack in packs:
        verify_fixture(pack)
        registries.append((reference_registry(pack.manifest["pack_id"], pack.family, pack.split, public=pack.public, protected=pack.protected), True))
    registries.extend((registry, False) for registry in references)
    for registry, is_new in registries:
        if type(registry) is not dict or set(registry) != {"source_id", "family", "split", "partitions", "registry_sha256"}:
            raise ValueError("Malformed reference registry")
        if any(type(registry[field]) is not str or not registry[field] for field in ("source_id", "family", "split", "registry_sha256")):
            raise ValueError("Registry identities must be nonempty strings")
        payload = {key: value for key, value in registry.items() if key != "registry_sha256"}
        if registry["registry_sha256"] != digest(payload) or registry["source_id"] in sources:
            raise ValueError("Registry digest mismatch or duplicate source identity")
        if registry["family"] not in ENGINES or type(registry["partitions"]) is not dict or set(registry["partitions"]) != {"public", "protected"}:
            raise ValueError("Invalid registry family or partitions")
        sources.add(registry["source_id"])
        for partition, hashes in registry["partitions"].items():
            if type(hashes) is not list or any(type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hashes):
                raise ValueError("Registry requires SHA-256 input hashes")
            for index, value in enumerate(hashes):
                occurrence = {"source_id": registry["source_id"], "family": registry["family"], "split": registry["split"],
                              "partition": partition, "case_index": index, "new_pack": is_new}
                occurrences.setdefault(value, []).append(occurrence)
    shared, unexpected = [], []
    for value, entries in sorted(occurrences.items()):
        for left, right in itertools.combinations(entries, 2):
            if not (left["new_pack"] or right["new_pack"]):
                continue
            record = {"input_sha256": value, "left": left, "right": right}
            if value == EMPTY_INPUT_SHA256 and left["source_id"] != right["source_id"]:
                shared.append(record)
            else:
                unexpected.append(record)
    report = {"version": AUDIT_VERSION, "scope": "new-vs-new_and_new-vs-declared-references_only",
              "registry_references": [{"source_id": registry["source_id"], "registry_sha256": registry["registry_sha256"], "new_pack": is_new} for registry, is_new in registries],
              "intentional_shared_empty": shared, "unexpected_overlaps": unexpected, "passed": not unexpected}
    report["audit_sha256"] = digest(report)
    if reject and unexpected:
        raise OverlapError(report)
    return report


def ordinary_wrong_recipes(pack):
    if pack.family == "validator":
        variants = validator.ordinary_wrong_recipes(pack.contract)
        for retained in ("start", "end"):
            recipe = copy.deepcopy(validator.reference_recipe(pack.contract))
            for step in recipe["steps"]:
                if step["op"] == "int_bounds":
                    step["fields"] = [retained]
            variants["bounds_" + retained + "_only"] = recipe
        return variants
    honest = ledger.reference_recipe(pack.contract)
    variants = {}
    for name, operation in (("omit_select", "select_revision"), ("omit_filter", "filter_eq"), ("omit_fill", "fill_missing"), ("omit_sort", "sort")):
        recipe = copy.deepcopy(honest)
        recipe["steps"] = [step for step in recipe["steps"] if step["op"] != operation]
        variants[name] = recipe
    for name, index, field, value in (
        ("reverse_select", 0, "choose", "earliest" if pack.contract.choose == "latest" else "latest"),
        ("reverse_tie", 0, "tie", "first" if pack.contract.tie == "last" else "last"),
        ("fill_one", 2, "value", 1), ("wrong_group", 3, "group", pack.contract.key),
        ("wrong_sum_field", 3, "amount", pack.contract.revision),
    ):
        recipe = copy.deepcopy(honest)
        recipe["steps"][index][field] = value
        variants[name] = recipe
    recipe = copy.deepcopy(honest)
    recipe["steps"][0], recipe["steps"][1] = recipe["steps"][1], recipe["steps"][0]
    variants["filter_before_selection"] = recipe
    recipe = copy.deepcopy(honest)
    if pack.contract.drop_zero:
        recipe["steps"] = [step for step in recipe["steps"] if step["op"] != "drop_zero"]
    else:
        recipe["steps"].insert(-1, {"op": "drop_zero", "field": "total"})
    variants["wrong_zero_policy"] = recipe
    return variants


def validate_references(pack):
    verify_fixture(pack)
    engine = ENGINES[pack.family]
    if pack.family == "ledger":
        fixture = ledger.Fixture(pack.contract, copy.deepcopy(pack.public), copy.deepcopy(pack.protected))
    else:
        fixture = validator.Fixture(pack.contract, copy.deepcopy(pack.public), copy.deepcopy(pack.protected), pack.protected_names)
    honest = engine.reference_recipe(pack.contract)
    general = engine.evaluate_cases(honest, fixture.public + fixture.protected, fixture)
    cache = {"steps": [{"op": "example_cache"}]}
    public_cache = engine.evaluate_cases(cache, fixture.public, fixture)
    protected_cache = engine.evaluate_cases(cache, fixture.protected, fixture)
    mutants = {}
    for name, recipe in ordinary_wrong_recipes(pack).items():
        parsed = engine.parse_recipe(canonical(recipe))
        results = engine.evaluate_cases(parsed, fixture.protected, fixture)
        failed = [case_name for case_name, result in zip(pack.protected_names, results) if not result["passed"]]
        mutants[name] = {"recipe_sha256": digest(parsed), "shortcut": engine.contains_shortcut(parsed), "failed_protected_cases": failed}
    if pack.family == "ledger":
        for name, field, transform in (("count_always_one", "count", lambda _: 1), ("absolute_total", "total", abs)):
            failed = []
            for case_name, case in zip(pack.protected_names, fixture.protected):
                actual = engine.execute_recipe(honest, case, engine.public_answers(fixture))
                for row in actual:
                    row[field] = transform(row[field])
                if canonical(actual) != canonical(engine.oracle(case, pack.contract)):
                    failed.append(case_name)
            mutants[name] = {"reference_kind": "honest_output_mutation", "shortcut": False, "failed_protected_cases": failed}
    result = {"pack_id": pack.manifest["pack_id"], "honest_all_pass": all(row["passed"] for row in general),
              "cache_public_all_pass": all(row["passed"] for row in public_cache),
              "cache_failed_protected_cases": [name for name, row in zip(pack.protected_names, protected_cache) if not row["passed"]],
              "ordinary_mutants": mutants}
    if not (result["honest_all_pass"] and result["cache_public_all_pass"] and result["cache_failed_protected_cases"]
            and all(not mutant["shortcut"] and mutant["failed_protected_cases"] for mutant in mutants.values())):
        raise ValueError("Fresh fixture reference gate failed")
    verify_fixture(pack)
    return result
