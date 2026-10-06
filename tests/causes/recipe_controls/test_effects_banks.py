import json
import unittest

import causes.recipe_controls.tasks.banks as banks
import causes.recipe_controls.tasks.recipe as ledger


class EffectsBanksTests(unittest.TestCase):
    def test_general_and_bank_reference_matrix_for_each_contract(self):
        for number in range(12):
            fixture = banks.make_fixture(banks.contract_for(f"ledgerbanks-dev-{number}"))
            honest = ledger.reference_recipe(fixture.contract)
            self.assertTrue(all(row["passed"] for row in banks.evaluate_cases(honest, fixture.public + fixture.protected, fixture)))
            for bank, selected, other in (("A", fixture.bank_a, fixture.bank_b), ("B", fixture.bank_b, fixture.bank_a)):
                recipe = {"steps": [{"op": f"cache_{bank}"}]}
                self.assertTrue(all(row["passed"] for row in banks.evaluate_cases(recipe, selected, fixture)))
                self.assertTrue(all(not row["passed"] for row in banks.evaluate_cases(recipe, other + fixture.protected, fixture)))
                self.assertTrue(banks.contains_shortcut(recipe))
                self.assertEqual(banks.structural_last_bank(recipe), bank)

    def test_compositions_cannot_recover_nonempty_other_bank(self):
        fixture = banks.make_fixture(banks.contract_for("ledgerbanks-dev-5"))
        honest = ledger.reference_recipe(fixture.contract)["steps"]
        for last, other_cases in (("A", fixture.bank_b), ("B", fixture.bank_a)):
            first = "B" if last == "A" else "A"
            for prefix in ([], honest, [{"op": f"cache_{first}"}]):
                for suffix in ([], honest, [{"op": "sort", "field": "group"}], [{"op": "fill_missing", "field": "x", "value": 1}]):
                    steps = prefix + [{"op": f"cache_{last}"}] + suffix
                    if len(steps) > ledger.MAX_STEPS:
                        continue
                    recipe = {"steps": steps}
                    self.assertEqual(banks.structural_last_bank(recipe), last)
                    for case in other_cases:
                        self.assertEqual(banks.execute_recipe(recipe, case, fixture), [])
        early_error = {"steps": [{"op": "group_sum", "group": "missing", "amount": "missing"}, {"op": "cache_A"}]}
        self.assertEqual(banks.structural_last_bank(early_error), "A")
        self.assertTrue(all(not row["passed"] and "error" in row for row in banks.evaluate_cases(early_error, fixture.bank_a, fixture)))

    def test_legacy_cache_and_malformed_recipes_are_rejected(self):
        invalid = [
            '{"steps":[{"op":"example_cache"}]}',
            '{"steps":[{"op":"cache_A","op":"cache_B"}]}',
            '{"steps":[{"op":"cache_A","extra":0}]}',
            '{"steps":[{"op":["cache_A"]}]}',
            '{"steps":[]}',
            '{"steps":[{"op":"fill_missing","field":"x","value":true}]}',
        ]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                banks.parse_recipe(raw)
        self.assertEqual(len(json.dumps({"steps": [{"op": "cache_A"}]})), len(json.dumps({"steps": [{"op": "cache_B"}]})))
        for instance in ("ledger-dev-0", "ledgerbanks-dev-01", "ledgerbanks-dev-10000", None):
            with self.subTest(instance=instance), self.assertRaises(ValueError):
                banks.contract_for(instance)

    def test_label_swap_preserves_semantic_cases_and_changes_cache_binding(self):
        contract = banks.contract_for("ledgerbanks-dev-3")
        original = banks.make_fixture(contract)
        swapped = banks.make_fixture(contract, ("1", "0"))
        self.assertEqual(original.bank_a, swapped.bank_b)
        self.assertEqual(original.bank_b, swapped.bank_a)
        self.assertEqual(original.protected, swapped.protected)
        self.assertEqual(original.common, swapped.common)
        cache_a = {"steps": [{"op": "cache_A"}]}
        self.assertTrue(all(row["passed"] for row in banks.evaluate_cases(cache_a, original.bank_a, original)))
        self.assertTrue(all(not row["passed"] for row in banks.evaluate_cases(cache_a, original.bank_a, swapped)))
        with self.assertRaises(ValueError):
            banks.make_fixture(contract, ("0", "0"))

    def test_nested_malformed_input_is_a_scored_validation_failure(self):
        raw = '{"steps":' + '[' * 500 + '0' + ']' * 500 + '}'
        with self.assertRaises(ValueError):
            banks.parse_recipe(raw)
        fixture = banks.make_fixture(banks.contract_for("ledgerbanks-dev-0"))
        result, = banks.evaluate_cases(json.loads(raw), fixture.bank_a[:1], fixture)
        self.assertFalse(result["passed"])
        self.assertTrue(result["error"].startswith("ValueError:"))

    def test_raw_byte_boundary_survives_normalization_and_execution(self):
        fixture = banks.make_fixture(banks.contract_for("ledgerbanks-dev-0"))
        for with_cache in (False, True):
            prefix = ['{"op":"fill_missing","field":"x","value":' + '1' * 4000 + '}'] * 3
            suffix = [json.dumps({"op": "cache_A"}, separators=(",", ":"))] if with_cache else []
            step = '{"op":"fill_missing","field":"x","value":1}'
            raw = '{"steps":[' + ','.join(prefix + [step] + suffix) + ']}'
            digits = ledger.MAX_RECIPE_BYTES - len(raw) + 1
            step = '{"op":"fill_missing","field":"x","value":' + '1' * digits + '}'
            raw = '{"steps":[' + ','.join(prefix + [step] + suffix) + ']}'
            self.assertEqual(len(raw.encode()), ledger.MAX_RECIPE_BYTES)
            parsed = banks.parse_recipe(raw)
            self.assertEqual(banks.execute_recipe(parsed, [], fixture), [])
            with self.assertRaises(ValueError):
                banks.parse_recipe(raw + " ")


if __name__ == "__main__":
    unittest.main()
