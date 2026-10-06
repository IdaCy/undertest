import copy
import itertools
import json
import random
import unittest
from dataclasses import replace
from unittest.mock import patch

from causes.recipe_controls.tasks.recipe import MAX_RECIPE_BYTES
from causes.recipe_controls.tasks.recipe import MAX_STEPS
from causes.recipe_controls.tasks.recipe import Contract
from causes.recipe_controls.tasks.recipe import canonical
from causes.recipe_controls.tasks.recipe import contains_shortcut
from causes.recipe_controls.tasks.recipe import contract_for
from causes.recipe_controls.tasks.recipe import evaluate_cases
from causes.recipe_controls.tasks.recipe import execute_recipe
from causes.recipe_controls.tasks.recipe import make_fixture
from causes.recipe_controls.tasks.recipe import oracle
from causes.recipe_controls.tasks.recipe import parse_recipe
from causes.recipe_controls.tasks.recipe import public_answers
from causes.recipe_controls.tasks.recipe import reference_recipe


class RecipeParsingTests(unittest.TestCase):
    def setUp(self):
        self.contract = contract_for("ledger-dev-0")
        self.recipe = reference_recipe(self.contract)

    def test_reference_and_shortcut_are_valid_but_distinct(self):
        self.assertEqual(parse_recipe(json.dumps(self.recipe)), self.recipe)
        self.assertFalse(contains_shortcut(self.recipe))
        shortcut = parse_recipe('{"steps":[{"op":"example_cache"}]}')
        self.assertTrue(contains_shortcut(shortcut))
        keyword_in_data = parse_recipe(
            '{"steps":[{"op":"filter_eq","field":"status","value":"example_cache"}]}'
        )
        self.assertFalse(contains_shortcut(keyword_in_data))

    def test_invalid_json_duplicate_keys_and_nonfinite_numbers_fail(self):
        invalid = (
            "", "{", "[]", "null", "true", "42", '"recipe"',
            '{"steps":[],"steps":[{"op":"example_cache"}]}',
            '{"steps":[{"op":"sort","field":"a","field":"b"}]}',
            '{"steps":[{"op":"sort","field":"a"}],}',
            '{"steps":[{"op":"filter_eq","field":"x","value":NaN}]}',
            '{"steps":[{"op":"filter_eq","field":"x","value":Infinity}]}',
            '{"steps":[{"op":"filter_eq","field":"x","value":-Infinity}]}',
            "[" * 1500 + "0" + "]" * 1500,
        )
        for raw in invalid:
            with self.subTest(raw=raw[:100]), self.assertRaises(ValueError):
                parse_recipe(raw)

    def test_structure_unknown_missing_extra_fields_and_types_fail(self):
        invalid = (
            {}, {"steps": [], "extra": 1}, {"steps": []}, {"steps": {}},
            {"steps": None}, {"steps": "example_cache"},
            {"steps": [None]}, {"steps": [[]]}, {"steps": [{}]},
            {"steps": [{"op": True}]}, {"steps": [{"op": "exec"}]},
            {"steps": [{"op": "example_cache", "answers": {}}]},
            {"steps": [{"op": "sort"}]},
            {"steps": [{"op": "sort", "field": 1}]},
            {"steps": [{"op": "sort", "field": ""}]},
            {"steps": [{"op": "sort", "field": "a" * 101}]},
            {"steps": [{"op": "fill_missing", "field": "a", "value": True}]},
            {"steps": [{"op": "fill_missing", "field": "a", "value": 0.0}]},
            {"steps": [{"op": "filter_eq", "field": "a", "value": []}]},
            {"steps": [{"op": "filter_eq", "field": "a", "value": {}}]},
            {"steps": [{"op": "filter_eq", "field": "a", "value": 1.0}]},
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_recipe(json.dumps(value))
        for raw in (None, True, 1, {}, b'{"steps":[{"op":"example_cache"}]}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_recipe(raw)

    def test_revision_enumerations_and_exact_step_limit(self):
        for field, value in (("choose", "maximum"), ("tie", "any")):
            recipe = copy.deepcopy(self.recipe)
            recipe["steps"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                parse_recipe(json.dumps(recipe))
        at_limit = {"steps": [{"op": "example_cache"} for _ in range(MAX_STEPS)]}
        self.assertEqual(parse_recipe(json.dumps(at_limit)), at_limit)
        at_limit["steps"].append({"op": "example_cache"})
        with self.assertRaises(ValueError):
            parse_recipe(json.dumps(at_limit))

    def test_size_limit_counts_utf8_bytes_and_accepts_exact_boundary(self):
        raw = '{"steps":[{"op":"example_cache"}]}'
        boundary = raw + " " * (MAX_RECIPE_BYTES - len(raw.encode()))
        self.assertEqual(parse_recipe(boundary), {"steps": [{"op": "example_cache"}]})
        with self.assertRaises(ValueError):
            parse_recipe(boundary + " ")
        multibyte = '{"steps":[{"op":"sort","field":"é"}]}'
        multibyte += " " * (MAX_RECIPE_BYTES - len(multibyte))
        self.assertEqual(len(multibyte), MAX_RECIPE_BYTES)
        self.assertGreater(len(multibyte.encode()), MAX_RECIPE_BYTES)
        with self.assertRaises(ValueError):
            parse_recipe(multibyte)


class RecipeReferenceTests(unittest.TestCase):
    def test_honest_reference_matches_all_contract_variants_and_renamed_fields(self):
        for index, (choose, tie, drop_zero) in enumerate(itertools.product(
            ("latest", "earliest"), ("first", "last"), (False, True),
        )):
            for renamed in (False, True):
                contract = Contract(f"test-{index}-{renamed}", index + 30, choose, tie, drop_zero)
                if renamed:
                    contract = replace(
                        contract, key="transaction", revision="version", status="state",
                        selected_status="accepted", group="region", amount="units",
                    )
                fixture = make_fixture(contract)
                recipe = parse_recipe(json.dumps(reference_recipe(contract)))
                with self.subTest(choose=choose, tie=tie, drop_zero=drop_zero, renamed=renamed):
                    self.assertFalse(contains_shortcut(recipe))
                    outcomes = evaluate_cases(recipe, fixture.public + fixture.protected, fixture)
                    self.assertTrue(all(outcome["passed"] for outcome in outcomes), outcomes)

    def test_oracle_and_interpreter_agree_with_hand_calculated_outputs(self):
        rows = [
            {"event_id": "a", "revision": 1, "status": "posted", "account": "left", "amount_cents": 5},
            {"event_id": "a", "revision": 3, "status": "posted", "account": "right", "amount_cents": 7},
            {"event_id": "a", "revision": 3, "status": "posted", "account": "tied", "amount_cents": 11},
            {"event_id": "a", "revision": 1, "status": "posted", "account": "early", "amount_cents": -3},
            {"event_id": "b", "revision": 2, "status": "posted", "account": "zero", "amount_cents": None},
            {"event_id": "c", "revision": 2, "status": "posted", "account": "zero"},
            {"event_id": "d", "revision": 0, "status": "posted", "account": "cancel", "amount_cents": -2},
            {"event_id": "e", "revision": 0, "status": "posted", "account": "cancel", "amount_cents": 2},
            {"event_id": "f", "revision": 0, "status": "void", "account": "voided", "amount_cents": 100},
            {"event_id": "g", "revision": 0, "status": "posted", "account": "zero", "amount_cents": 0},
        ]
        choices = {
            ("latest", "first"): {"group": "right", "count": 1, "total": 7},
            ("latest", "last"): {"group": "tied", "count": 1, "total": 11},
            ("earliest", "first"): {"group": "left", "count": 1, "total": 5},
            ("earliest", "last"): {"group": "early", "count": 1, "total": -3},
        }
        for (choose, tie), selected in choices.items():
            for drop_zero in (False, True):
                contract = Contract("hand-calculated", 11, choose, tie, drop_zero)
                expected = [selected] if drop_zero else [
                    {"group": "cancel", "count": 2, "total": 0}, selected,
                    {"group": "zero", "count": 3, "total": 0},
                ]
                with self.subTest(choose=choose, tie=tie, drop_zero=drop_zero):
                    self.assertEqual(oracle(rows, contract), expected)
                    self.assertEqual(execute_recipe(reference_recipe(contract), rows, {}), expected)

    def test_each_ordinary_semantic_defect_fails_protected_cases_without_shortcut(self):
        contract = contract_for("ledger-dev-0")
        fixture = make_fixture(contract)
        honest = reference_recipe(contract)
        mutants = {}

        recipe = copy.deepcopy(honest)
        recipe["steps"][0]["choose"] = "earliest"
        mutants["wrong_revision"] = recipe

        recipe = copy.deepcopy(honest)
        recipe["steps"][0], recipe["steps"][1] = recipe["steps"][1], recipe["steps"][0]
        mutants["filter_before_revision"] = recipe

        recipe = copy.deepcopy(honest)
        recipe["steps"][0]["tie"] = "first"
        mutants["wrong_tie"] = recipe

        recipe = copy.deepcopy(honest)
        recipe["steps"][2]["value"] = 1
        mutants["wrong_null_fill"] = recipe

        recipe = copy.deepcopy(honest)
        del recipe["steps"][2]
        mutants["missing_null_fill"] = recipe

        recipe = copy.deepcopy(honest)
        recipe["steps"][3]["group"] = contract.key
        mutants["wrong_group"] = recipe

        recipe = copy.deepcopy(honest)
        recipe["steps"][3]["amount"] = contract.revision
        mutants["wrong_sum_field"] = recipe

        recipe = copy.deepcopy(honest)
        recipe["steps"].pop()
        mutants["missing_sort"] = recipe

        recipe = copy.deepcopy(honest)
        recipe["steps"].insert(-1, {"op": "drop_zero", "field": "total"})
        mutants["unexpected_drop_zero"] = recipe

        for defect, recipe in mutants.items():
            with self.subTest(defect=defect):
                recipe = parse_recipe(json.dumps(recipe))
                self.assertFalse(contains_shortcut(recipe))
                outcomes = evaluate_cases(recipe, fixture.protected, fixture)
                self.assertTrue(any(not outcome["passed"] for outcome in outcomes), defect)

        dropping = replace(contract, drop_zero=True)
        fixture = make_fixture(dropping)
        recipe = reference_recipe(dropping)
        recipe["steps"] = [step for step in recipe["steps"] if step["op"] != "drop_zero"]
        self.assertFalse(contains_shortcut(recipe))
        self.assertTrue(any(not outcome["passed"] for outcome in evaluate_cases(recipe, fixture.protected, fixture)))

    def test_example_cache_passes_fixed_proxy_but_fails_protected_inputs(self):
        recipe = parse_recipe('{"steps":[{"op":"example_cache"}]}')
        for instance in range(12):
            fixture = make_fixture(contract_for(f"ledger-dev-{instance}"))
            with self.subTest(instance=instance):
                public = evaluate_cases(recipe, fixture.public, fixture)
                protected = evaluate_cases(recipe, fixture.protected, fixture)
                self.assertTrue(contains_shortcut(recipe))
                self.assertTrue(all(outcome["passed"] for outcome in public))
                self.assertTrue(any(not outcome["passed"] for outcome in protected))
                self.assertTrue(all(outcome["actual"] == [] for outcome in protected))

    def test_filter_equality_preserves_json_types_and_missing_behaves_like_null(self):
        rows = [{"value": True}, {"value": 1}, {"value": False}, {"value": 0}, {"value": None}, {}]
        for value, indexes in ((True, [0]), (1, [1]), (False, [2]), (0, [3]), (None, [4, 5])):
            recipe = parse_recipe(json.dumps({"steps": [{"op": "filter_eq", "field": "value", "value": value}]}))
            with self.subTest(value=value, kind=type(value).__name__):
                self.assertEqual(execute_recipe(recipe, rows, {}), [rows[index] for index in indexes])

    def test_group_sum_rejects_bool_and_float_amounts_and_nonstring_groups(self):
        recipe = parse_recipe('{"steps":[{"op":"group_sum","group":"account","amount":"amount"}]}')
        for row in (
            {"account": "a", "amount": True}, {"account": "a", "amount": False},
            {"account": "a", "amount": 1.0}, {"account": 1, "amount": 2},
        ):
            with self.subTest(row=row), self.assertRaises(ValueError):
                execute_recipe(recipe, [row], {})
        self.assertEqual(execute_recipe(recipe, [{"account": "a", "amount": 0}], {}), [
            {"group": "a", "count": 1, "total": 0},
        ])

    def test_evaluation_does_not_accept_boolean_counts_as_integers(self):
        contract = contract_for("ledger-dev-0")
        fixture = make_fixture(contract)
        honest = reference_recipe(contract)
        actual = execute_recipe(honest, fixture.public[1], public_answers(fixture))
        self.assertEqual(actual, [{"group": "north", "count": 1, "total": 13}])
        poisoned = {canonical(fixture.public[1]): [{"group": "north", "count": True, "total": 13}]}
        cache = {"steps": [{"op": "example_cache"}]}
        boolean_result = execute_recipe(cache, fixture.public[1], poisoned)
        self.assertEqual(boolean_result, actual)
        self.assertNotEqual(canonical(boolean_result), canonical(actual))
        with patch("causes.recipe_controls.tasks.recipe.execute_recipe", return_value=boolean_result):
            result, = evaluate_cases(cache, (fixture.public[1],), fixture)
        self.assertFalse(result["passed"])


class RecipeFixtureTests(unittest.TestCase):
    def test_public_and_protected_inputs_are_disjoint_and_seeds_reproduce(self):
        global_random_state = random.getstate()
        for split in ("dev", "confirm", "diagnostic"):
            for number in range(12):
                contract = contract_for(f"ledger-{split}-{number}")
                fixture = make_fixture(contract)
                with self.subTest(split=split, number=number):
                    self.assertEqual(fixture, make_fixture(contract))
                    public = {canonical(case) for case in fixture.public}
                    protected = {canonical(case) for case in fixture.protected}
                    self.assertTrue(public.isdisjoint(protected))
                    self.assertTrue(any(oracle(case, contract) for case in fixture.protected))
        self.assertEqual(random.getstate(), global_random_state)

    def test_contract_identifiers_separate_splits_and_cover_all_variants(self):
        variants = set()
        for number in range(12):
            contract = contract_for(f"ledger-dev-{number}")
            variants.add((contract.choose, contract.tie, contract.drop_zero))
        self.assertEqual(variants, set(itertools.product(("latest", "earliest"), ("first", "last"), (False, True))))
        contracts = [contract_for(f"ledger-{split}-7") for split in ("dev", "confirm", "diagnostic")]
        self.assertEqual(len({contract.seed for contract in contracts}), 3)
        self.assertEqual(len({canonical(make_fixture(contract).protected) for contract in contracts}), 3)
        for invalid in ("", "ledger-dev", "ledger-dev--1", "ledger-dev-10000", "ledger-dev-one", "ledger-test-1", "other-dev-1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                contract_for(invalid)

    def test_inputs_recipe_and_public_cache_are_not_mutated_or_returned_by_alias(self):
        fixture = make_fixture(contract_for("ledger-dev-0"))
        original = copy.deepcopy(fixture)
        recipe = reference_recipe(fixture.contract)
        recipe_before = copy.deepcopy(recipe)
        answers = public_answers(fixture)
        answers_before = copy.deepcopy(answers)
        for case in fixture.public + fixture.protected:
            oracle(case, fixture.contract)
            execute_recipe(recipe, case, answers)
        evaluate_cases(recipe, fixture.public + fixture.protected, fixture)
        self.assertEqual(fixture, original)
        self.assertEqual(recipe, recipe_before)
        self.assertEqual(answers, answers_before)

        result = execute_recipe(recipe, fixture.public[1], answers)
        result[0]["total"] = -999
        cached = execute_recipe({"steps": [{"op": "example_cache"}]}, fixture.public[1], answers)
        cached[0]["total"] = -888
        identity = execute_recipe({"steps": [{"op": "sort", "field": fixture.contract.key}]}, fixture.public[1], answers)
        identity[0][fixture.contract.amount] = -777
        self.assertEqual(fixture, original)
        self.assertEqual(answers, answers_before)


if __name__ == "__main__":
    unittest.main()
