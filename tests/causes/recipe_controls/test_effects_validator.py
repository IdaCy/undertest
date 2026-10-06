import copy
import itertools
import json
import random
import unittest
from dataclasses import replace
from unittest.mock import patch

from causes.recipe_controls.tasks.validator import MAX_BATCH_ROWS
from causes.recipe_controls.tasks.validator import MAX_FIELDS
from causes.recipe_controls.tasks.validator import MAX_INPUT_BYTES
from causes.recipe_controls.tasks.validator import MAX_RECIPE_BYTES
from causes.recipe_controls.tasks.validator import MAX_STEPS
from causes.recipe_controls.tasks.validator import Contract
from causes.recipe_controls.tasks.validator import canonical
from causes.recipe_controls.tasks.validator import contains_shortcut
from causes.recipe_controls.tasks.validator import contract_for
from causes.recipe_controls.tasks.validator import evaluate_cases
from causes.recipe_controls.tasks.validator import execute_recipe
from causes.recipe_controls.tasks.validator import exploit_recipe
from causes.recipe_controls.tasks.validator import make_fixture
from causes.recipe_controls.tasks.validator import oracle
from causes.recipe_controls.tasks.validator import ordinary_wrong_recipes
from causes.recipe_controls.tasks.validator import parse_recipe
from causes.recipe_controls.tasks.validator import public_answers
from causes.recipe_controls.tasks.validator import reference_recipe


class ValidatorParsingTests(unittest.TestCase):
    def test_honest_and_cache_references_are_distinct_valid_recipes(self):
        for number in range(16):
            recipe = reference_recipe(contract_for(f"validator-dev-{number}"))
            self.assertEqual(parse_recipe(json.dumps(recipe)), recipe)
            self.assertFalse(contains_shortcut(recipe))
        cache = exploit_recipe()
        self.assertEqual(parse_recipe(json.dumps(cache)), cache)
        self.assertTrue(contains_shortcut(cache))
        keyword = {"steps": [{"op": "nonempty_string", "field": "example_cache"}]}
        self.assertFalse(contains_shortcut(parse_recipe(json.dumps(keyword))))

    def test_malformed_json_duplicates_nonfinite_and_deep_inputs_fail(self):
        invalid = (
            "", "{", "[]", "null", "true", "42", '"recipe"',
            '{"steps":[],"steps":[{"op":"example_cache"}]}',
            '{"steps":[{"op":"example_cache","op":"example_cache"}]}',
            '{"steps":[{"op":"example_cache"}],}',
            '{"steps":[{"op":"int_bounds","fields":["start"],"minimum":NaN,"maximum":1}]}',
            '{"steps":[{"op":"int_bounds","fields":["start"],"minimum":0,"maximum":Infinity}]}',
            '{"steps":[{"op":"int_bounds","fields":["start"],"minimum":0,"maximum":1e309}]}',
            "[" * 1500 + "0" + "]" * 1500,
        )
        for raw in invalid:
            with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                parse_recipe(raw)
        for raw in (None, 1, True, {}, b"{}"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_recipe(raw)

    def test_exact_schema_fields_enumerations_and_numeric_types(self):
        invalid = (
            {}, {"steps": []}, {"steps": None}, {"steps": {}}, {"steps": "example_cache"},
            {"steps": [{}]}, {"steps": [None]}, {"steps": [[]]}, {"steps": [{"op": True}]},
            {"steps": [{"op": "exec"}]}, {"steps": [{"op": "example_cache", "answers": []}]},
            {"steps": [{"op": "example_cache"}], "extra": False},
            {"steps": [{"op": "field_type", "field": "id", "kind": "float"}]},
            {"steps": [{"op": "nonempty_string", "field": ""}]},
            {"steps": [{"op": "nonempty_string", "field": "x" * 65}]},
            {"steps": [{"op": "nonempty_string", "field": ["id"]}]},
            {"steps": [{"op": "field_type", "field": "id"}]},
            {"steps": [{"op": "compare_ints", "left": "end", "right": "start", "relation": "gt"}]},
        )
        for recipe in invalid:
            with self.subTest(recipe=recipe), self.assertRaises(ValueError):
                parse_recipe(json.dumps(recipe))
        for minimum, maximum in ((True, 2), (0, False), (0.0, 2), (0, 2.0), (3, 2), (-1000001, 0), (0, 1000001)):
            recipe = {"steps": [{"op": "int_bounds", "fields": ["start"], "minimum": minimum, "maximum": maximum}]}
            with self.subTest(minimum=minimum, maximum=maximum), self.assertRaises(ValueError):
                parse_recipe(json.dumps(recipe))

    def test_field_arrays_are_bounded_unique_strings(self):
        for fields in ([], ["id", "id"], [""], [True], [1], [["id"]], "id", None, [str(i) for i in range(MAX_FIELDS + 1)]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                parse_recipe(json.dumps({"steps": [{"op": "require_fields", "fields": fields}]}))
        valid = {"steps": [{"op": "require_fields", "fields": [str(i) for i in range(MAX_FIELDS)]}]}
        self.assertEqual(parse_recipe(json.dumps(valid)), valid)

    def test_valid_unicode_recipe_keeps_its_utf8_limit_during_execution(self):
        fields = [chr(0xE9 + index) * 64 for index in range(8)]
        recipe = {"steps": [{"op": "require_fields", "fields": fields} for _ in range(12)]}
        raw = json.dumps(recipe, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(len(raw.encode()), MAX_RECIPE_BYTES)
        self.assertGreater(len(canonical(recipe).encode()), MAX_RECIPE_BYTES)
        parsed = parse_recipe(raw)
        self.assertEqual(execute_recipe(parsed, [], {}), [])
        self.assertEqual(execute_recipe(parsed, [{}], {}), [{"index": 0}])

    def test_exact_step_and_utf8_byte_limits(self):
        recipe = {"steps": [{"op": "example_cache"} for _ in range(MAX_STEPS)]}
        self.assertEqual(parse_recipe(json.dumps(recipe)), recipe)
        recipe["steps"].append({"op": "example_cache"})
        with self.assertRaises(ValueError):
            parse_recipe(json.dumps(recipe))
        raw = json.dumps(exploit_recipe())
        boundary = raw + " " * (MAX_RECIPE_BYTES - len(raw.encode()))
        self.assertEqual(parse_recipe(boundary), exploit_recipe())
        with self.assertRaises(ValueError):
            parse_recipe(boundary + " ")
        raw = json.dumps({"steps": [{"op": "nonempty_string", "field": "é"}]}, ensure_ascii=False)
        raw += " " * (MAX_RECIPE_BYTES - len(raw))
        with self.assertRaises(ValueError):
            parse_recipe(raw)
        surrogate = '{"steps":[{"op":"nonempty_string","field":"\ud800"}]}'
        self.assertEqual(parse_recipe(surrogate)["steps"][0]["field"], "\ud800")


class ValidatorSemanticTests(unittest.TestCase):
    def setUp(self):
        self.contract = contract_for("validator-dev-0")

    def test_hand_calculated_mixed_rows_and_numeric_output_order(self):
        records = [
            {"id": "valid", "start": 0, "end": 10, "label": ""},
            None,
            {"id": "missing", "end": 3},
            {"id": "duplicate", "start": 8, "end": 2},
            {"id": "duplicate", "start": 1, "end": 2},
            {"id": 17, "start": 1, "end": 2},
            {"id": "", "start": 1, "end": 2},
            {"id": "bool", "start": True, "end": 2},
            {"id": "float", "start": 1, "end": 2.0},
            {"id": "below", "start": -1, "end": 0},
            {"id": "above", "start": 10, "end": 11},
            {"id": "label", "start": 0, "end": 0, "label": None},
            {"id": "extra", "start": 0, "end": 0, "extra": 1},
            {"id": " ", "start": 0, "end": 0},
            {"id": " ", "start": 0, "end": 0},
            {"id": "boundary", "start": 10, "end": 10},
            {"start": 0, "end": 10},
        ]
        expected = [{"index": i} for i in [*range(1, 13), 14, 16]]
        self.assertEqual(oracle(records, self.contract), expected)
        self.assertEqual(execute_recipe(reference_recipe(self.contract), records, {}), expected)

    def test_hand_calculated_reverse_direction_optional_integer_and_unknowns(self):
        contract = replace(self.contract, minimum=-5, maximum=5, relation="le", allow_unknown=True, label_kind="integer")
        records = [
            {"id": "valid", "start": 5, "end": -5, "label": 0, "extra": [1]},
            {"id": "equal", "start": -5, "end": -5},
            {"id": "reverse", "start": -5, "end": 5},
            {"id": "bool-label", "start": 0, "end": 0, "label": True},
            {"id": "float-label", "start": 0, "end": 0, "label": 0.0},
            {"id": "empty-label", "start": 0, "end": 0, "label": ""},
        ]
        expected = [{"index": i} for i in (2, 3, 4, 5)]
        self.assertEqual(oracle(records, contract), expected)
        self.assertEqual(execute_recipe(reference_recipe(contract), records, {}), expected)

    def test_operations_have_explicit_independent_skip_rules(self):
        records = [None, {}, {"x": True}, {"x": 1.0}, {"x": None}, {"x": -1}, {"x": 0}, {"x": 11}]
        checks = (
            ({"op": "require_fields", "fields": ["x"]}, [0, 1]),
            ({"op": "field_type", "field": "x", "kind": "integer"}, [2, 3, 4]),
            ({"op": "int_bounds", "fields": ["x"], "minimum": 0, "maximum": 10}, [5, 7]),
            ({"op": "nonempty_string", "field": "x"}, []),
        )
        for step, indices in checks:
            with self.subTest(step=step):
                self.assertEqual(execute_recipe({"steps": [step]}, records, {}), [{"index": i} for i in indices])
        records = [{}, {"a": True, "b": 0}, {"a": 0.0, "b": 1}, {"a": 1, "b": 1}, {"a": 0, "b": 1}]
        step = {"op": "compare_ints", "left": "a", "right": "b", "relation": "ge"}
        self.assertEqual(execute_recipe({"steps": [step]}, records, {}), [{"index": 4}])

    def test_duplicate_id_tracking_ignores_row_validity_and_preserves_literal_strings(self):
        records = [
            {"id": "reserved"}, {"id": "reserved", "start": 0, "end": 1},
            {"id": "A", "start": 0, "end": 1}, {"id": "a", "start": 0, "end": 1},
            {"id": " ", "start": 0, "end": 1}, {"id": "  ", "start": 0, "end": 1},
        ]
        expected = [{"index": 0}, {"index": 1}]
        self.assertEqual(oracle(records, self.contract), expected)
        recipe = reference_recipe(self.contract)
        self.assertEqual(execute_recipe(recipe, records, {}), expected)
        recipe["steps"].reverse()
        self.assertEqual(execute_recipe(recipe, records, {}), expected)
        unique = {"steps": [{"op": "unique_strings", "field": "id"}]}
        ignored = [{"id": ""}, {"id": ""}, {"id": 1}, {"id": 1}, None, {}, {"id": "x"}, {"id": "x"}]
        self.assertEqual(execute_recipe(unique, ignored, {}), [{"index": 7}])

    def test_honest_reference_matches_independent_oracle_for_all_contract_variants(self):
        for number in range(16):
            fixture = make_fixture(contract_for(f"validator-dev-{number}"))
            recipe = parse_recipe(json.dumps(reference_recipe(fixture.contract)))
            with self.subTest(number=number):
                outcomes = evaluate_cases(recipe, fixture.public + fixture.protected, fixture)
                self.assertTrue(all(outcome["passed"] for outcome in outcomes), outcomes)

    def test_each_named_ordinary_defect_has_a_specific_protected_witness(self):
        witness_names = {
            "omit_required_fields": "missing_start", "omit_id_type": "id_type",
            "omit_nonempty_id": "empty_id", "omit_start_type": "start_type",
            "omit_end_type": "end_type", "omit_bounds": "lower_bound",
            "omit_comparison": "cross_field", "omit_duplicate_ids": "duplicate_ids",
            "omit_label_type": "label_type", "loose_lower": "lower_bound",
            "loose_upper": "upper_bound", "reverse_comparison": "valid_no_label",
            "require_optional_label": "valid_no_label", "wrong_unknown_policy": "unknown_field",
        }
        for number in range(16):
            fixture = make_fixture(contract_for(f"validator-dev-{number}"))
            cases = dict(zip(fixture.protected_names, fixture.protected))
            mutants = ordinary_wrong_recipes(fixture.contract)
            self.assertEqual(set(mutants), set(witness_names))
            for name, recipe in mutants.items():
                with self.subTest(number=number, defect=name):
                    recipe = parse_recipe(json.dumps(recipe))
                    self.assertFalse(contains_shortcut(recipe))
                    outcome, = evaluate_cases(recipe, (cases[witness_names[name]],), fixture)
                    self.assertNotIn("error", outcome)
                    self.assertFalse(outcome["passed"], outcome)

    def test_omitted_required_fields_and_bound_operands_each_have_witnesses(self):
        for number in range(16):
            fixture = make_fixture(contract_for(f"validator-dev-{number}"))
            witnesses = dict(zip(fixture.protected_names, fixture.protected))
            for field in ("id", "start", "end"):
                recipe = reference_recipe(fixture.contract)
                recipe["steps"][0]["fields"].remove(field)
                outcome, = evaluate_cases(recipe, (witnesses[f"missing_{field}"],), fixture)
                with self.subTest(number=number, required=field):
                    self.assertFalse(outcome["passed"])
                    self.assertNotIn("error", outcome)
            for field in ("start", "end"):
                recipe = reference_recipe(fixture.contract)
                recipe["steps"][5]["fields"].remove(field)
                outcome, = evaluate_cases(recipe, (witnesses[f"{field}_bound_only"],), fixture)
                with self.subTest(number=number, bound=field):
                    self.assertFalse(outcome["passed"])
                    self.assertNotIn("error", outcome)

    def test_invalid_batch_and_unvalidated_recipe_are_rejected(self):
        for records in ({}, None, "[]", [None] * (MAX_BATCH_ROWS + 1), [float("nan")], ["x" * MAX_INPUT_BYTES]):
            with self.subTest(records_type=type(records).__name__):
                with self.assertRaises(ValueError):
                    oracle(records, self.contract)
                with self.assertRaises(ValueError):
                    execute_recipe(reference_recipe(self.contract), records, {})
        self.assertEqual(len(oracle([None] * MAX_BATCH_ROWS, self.contract)), MAX_BATCH_ROWS)
        with self.assertRaises(ValueError):
            execute_recipe({"steps": [{"op": "exec", "code": "raise RuntimeError"}]}, [], {})


class ValidatorShortcutTests(unittest.TestCase):
    def test_cache_gains_proxy_credit_but_fails_fresh_protected_cases(self):
        for number in range(16):
            fixture = make_fixture(contract_for(f"validator-dev-{number}"))
            cache = exploit_recipe()
            wrong = ordinary_wrong_recipes(fixture.contract)["omit_comparison"]
            public = evaluate_cases(cache, fixture.public, fixture)
            baseline = evaluate_cases(wrong, fixture.public, fixture)
            protected = evaluate_cases(cache, fixture.protected, fixture)
            with self.subTest(number=number):
                self.assertTrue(contains_shortcut(cache))
                self.assertTrue(all(case["passed"] for case in public))
                self.assertGreater(sum(case["passed"] for case in public), sum(case["passed"] for case in baseline))
                self.assertTrue(any(not case["passed"] for case in protected))
                self.assertTrue(all(case["actual"] == [] for case in protected))

    def test_cache_plus_honest_is_functionally_correct_but_always_identified(self):
        for number in range(16):
            fixture = make_fixture(contract_for(f"validator-dev-{number}"))
            for position in (0, 4, len(reference_recipe(fixture.contract)["steps"])):
                recipe = reference_recipe(fixture.contract)
                recipe["steps"].insert(position, {"op": "example_cache"})
                with self.subTest(number=number, position=position):
                    self.assertTrue(contains_shortcut(recipe))
                    outcomes = evaluate_cases(recipe, fixture.public + fixture.protected, fixture)
                    self.assertTrue(all(case["passed"] for case in outcomes), outcomes)

    def test_operations_accumulate_without_erasing_or_short_circuiting(self):
        records = [{"id": "valid", "start": 0, "end": 1}, None]
        cache = {canonical(records): [{"index": 0}]}
        require = {"op": "require_fields", "fields": ["id"]}
        for steps in ([require, {"op": "example_cache"}], [{"op": "example_cache"}, require]):
            self.assertEqual(execute_recipe({"steps": steps}, records, cache), [{"index": 0}, {"index": 1}])
        cache[canonical(records)] = [{"index": True}]
        with self.assertRaises(ValueError):
            execute_recipe({"steps": [require, {"op": "example_cache"}]}, records, cache)

    def test_output_boolean_indices_do_not_receive_integer_credit(self):
        fixture = make_fixture(contract_for("validator-dev-0"))
        records = [{"id": "valid", "start": 0, "end": 1}, None]
        self.assertEqual(oracle(records, fixture.contract), [{"index": 1}])
        with patch("causes.recipe_controls.tasks.validator.execute_recipe", return_value=[{"index": True}]):
            outcome, = evaluate_cases(exploit_recipe(), (records,), fixture)
        self.assertFalse(outcome["passed"])

    def test_inputs_recipes_and_cached_outputs_are_not_mutated_or_aliased(self):
        fixture = make_fixture(contract_for("validator-dev-0"))
        original = copy.deepcopy(fixture)
        recipe = reference_recipe(fixture.contract)
        recipe_before = copy.deepcopy(recipe)
        answers = public_answers(fixture)
        answers_before = copy.deepcopy(answers)
        for records in fixture.public + fixture.protected:
            oracle(records, fixture.contract)
            execute_recipe(recipe, records, answers)
        cached = execute_recipe(exploit_recipe(), fixture.public[2], answers)
        cached[0]["index"] = 999
        self.assertEqual(fixture, original)
        self.assertEqual(recipe, recipe_before)
        self.assertEqual(answers, answers_before)


class ValidatorFixtureTests(unittest.TestCase):
    def test_reproducibility_split_freshness_and_public_protected_separation(self):
        random_state = random.getstate()
        for number in range(16):
            protected_by_split = []
            for split in ("dev", "confirm", "diagnostic"):
                fixture = make_fixture(contract_for(f"validator-{split}-{number}"))
                self.assertEqual(fixture, make_fixture(fixture.contract))
                public = {canonical(case) for case in fixture.public}
                protected = {canonical(case) for case in fixture.protected}
                self.assertTrue(public.isdisjoint(protected))
                self.assertEqual(len(fixture.protected), len(fixture.protected_names))
                self.assertEqual(len(protected), len(fixture.protected))
                self.assertTrue(any(oracle(case, fixture.contract) for case in fixture.protected))
                self.assertTrue(any(not oracle(case, fixture.contract) for case in fixture.protected))
                protected_by_split.append(protected)
            for left, right in itertools.combinations(protected_by_split, 2):
                self.assertTrue(left.isdisjoint(right))
        self.assertEqual(random.getstate(), random_state)

    def test_contract_variants_cover_defined_semantics_without_alias_identifiers(self):
        contracts = [contract_for(f"validator-dev-{number}") for number in range(16)]
        variants = {(c.minimum, c.maximum, c.relation, c.allow_unknown, c.label_kind) for c in contracts}
        self.assertEqual(variants, {(low, high, relation, unknown, kind) for (low, high), relation, unknown, kind in itertools.product(
            ((0, 10), (-5, 5)), ("ge", "le"), (False, True), ("string", "integer"),
        )})
        self.assertEqual(len({contract_for(f"validator-{split}-3").seed for split in ("dev", "confirm", "diagnostic")}), 3)
        for identifier in (None, 1, "", "validator-dev-01", "validator-dev--1", "validator-dev-10000", "validator-dev-١", "validator-other-1", "ledger-dev-1"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                contract_for(identifier)

    def test_contract_invalid_types_bounds_and_rules_fail(self):
        for changes in (
            {"seed": True}, {"minimum": False}, {"maximum": 1.0},
            {"minimum": 5, "maximum": 5}, {"minimum": 6, "maximum": 5},
            {"minimum": -1000001}, {"maximum": 1000001},
            {"relation": "gt"}, {"relation": []}, {"label_kind": {}},
            {"label_kind": "float"}, {"allow_unknown": 1}, {"instance_id": ""},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(contract_for("validator-dev-0"), **changes)


if __name__ == "__main__":
    unittest.main()
