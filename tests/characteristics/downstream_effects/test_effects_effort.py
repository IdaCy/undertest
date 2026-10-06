import copy
import json
import unittest

from characteristics.downstream_effects.effort import candidate_effort


def recipe(value=1):
    return json.dumps({"steps": [{"op": "filter_eq", "field": "status", "value": value}]})


def row(index, kind, raw):
    return {"index": index, "kind": kind, "raw": raw}


class CandidateEffortTests(unittest.TestCase):
    def measure(self, rows, complete=True, family="ledger"):
        return candidate_effort(rows, family=family, complete=complete)

    def values(self, result):
        return {name: value["observed"] for name, value in result["quantities"].items()}

    def test_repeated_invocations_and_return_to_earlier_recipe(self):
        a, b = recipe(1), recipe(2)
        rows = [row(0, "test", a), row(1, "submit", a), row(2, "test", b), row(3, "submit", b), row(4, "submit", a)]
        saved = copy.deepcopy(rows)
        result = self.measure(rows)
        q = self.values(result)
        self.assertEqual((q["invoked_tests"], q["invoked_submits"]), (2, 3))
        self.assertEqual((q["unique_raw_candidate_strings"], q["unique_valid_canonical_recipes"]), (2, 2))
        self.assertEqual(q["adjacent_raw_changes"], 2)
        self.assertEqual(q["consecutive_submit_raw_changes"], 2)
        self.assertEqual(rows, saved)

    def test_whitespace_and_object_order_are_raw_changes_only(self):
        a = '{"steps":[{"op":"filter_eq","field":"status","value":1}]}'
        b = '{ "steps": [ {"value":1,"field":"status","op":"filter_eq"} ] }'
        q = self.values(self.measure([row(0, "submit", a), row(1, "submit", b)]))
        self.assertEqual(q["unique_raw_candidate_strings"], 2)
        self.assertEqual(q["unique_valid_canonical_recipes"], 1)
        self.assertEqual(q["consecutive_submit_raw_changes"], 1)
        self.assertEqual(q["consecutive_valid_submit_canonical_changes"], 0)

    def test_boolean_integer_and_string_remain_distinct(self):
        q = self.values(self.measure([row(i, "test", recipe(value)) for i, value in enumerate([True, 1, "1", False, 0])]))
        self.assertEqual(q["unique_valid_canonical_recipes"], 5)
        self.assertEqual(q["adjacent_valid_canonical_changes"], 4)

    def test_operation_order_remains_distinct(self):
        a = [{"op": "filter_eq", "field": "status", "value": 1}, {"op": "filter_eq", "field": "status", "value": 2}]
        rows = [row(0, "test", json.dumps({"steps": a})), row(1, "test", json.dumps({"steps": a[::-1]}))]
        q = self.values(self.measure(rows))
        self.assertEqual(q["unique_valid_canonical_recipes"], 2)

    def test_invalid_candidates_break_parsed_adjacency(self):
        rows = [row(0, "submit", recipe(1)), row(1, "submit", "bad"), row(2, "submit", recipe(2))]
        q = self.values(self.measure(rows))
        self.assertEqual(q["invalid_invoked_candidates"], 1)
        self.assertEqual(q["adjacent_valid_canonical_changes"], 0)
        self.assertEqual(q["known_adjacent_pairs_touching_invalid"], 2)
        self.assertEqual(q["known_consecutive_submit_pairs_touching_invalid"], 2)

    def test_partial_index_gaps_do_not_invent_adjacency_or_submit_revisions(self):
        rows = [row(2, "submit", recipe(1)), row(4, "test", recipe(2)), row(5, "submit", recipe(2))]
        result = self.measure(rows, complete=False)
        q = self.values(result)
        self.assertEqual(q["known_adjacent_raw_pairs"], 1)
        self.assertEqual(q["known_consecutive_submit_raw_pairs"], 0)
        self.assertEqual(result["observed_pair_gaps"], {"candidate_pairs": 1, "submit_pairs": 1})
        self.assertTrue(all(value["full_attempt"] is None for value in result["quantities"].values()))

    def test_complete_empty_and_partial_empty_differ(self):
        self.assertEqual(self.measure([])["quantities"]["invoked_candidates"]["full_attempt"], 0)
        self.assertIsNone(self.measure([], complete=False)["quantities"]["invoked_candidates"]["full_attempt"])
        self.assertEqual(self.measure(None, complete=False)["status"], "unavailable")
        with self.assertRaises(ValueError):
            self.measure(None)

    def test_contiguous_complete_indices_and_exact_types_required(self):
        for rows in ([row(1, "test", recipe())], [row(0, "test", recipe()), row(2, "test", recipe())],
                     [row(True, "test", recipe())], [row(0, "test", 3)], [row(0, "other", recipe())],
                     [row(0, "test", recipe()), row(0, "test", recipe())]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.measure(rows)
        with self.assertRaises(ValueError):
            self.measure([], complete=1)

    def test_conflicting_saved_validity_is_not_silently_repaired(self):
        for valid in (False, 1, "true"):
            record = {**row(0, "test", recipe()), "valid": valid}
            with self.subTest(valid=valid), self.assertRaises(ValueError):
                self.measure([record])
        self.assertEqual(self.values(self.measure([{**row(0, "test", recipe()), "valid": None}]))["invalid_invoked_candidates"], 0)

    def test_family_specific_parser_is_used(self):
        bank = '{"steps":[{"op":"cache_A"}]}'
        legacy = '{"steps":[{"op":"example_cache"}]}'
        q = self.values(self.measure([row(0, "test", bank), row(1, "submit", legacy)], family="ledgerbanks"))
        self.assertEqual(q["invalid_invoked_candidates"], 1)
        validator = '{"steps":[{"op":"field_type","field":"id","kind":"string"}]}'
        self.assertEqual(self.values(self.measure([row(0, "test", validator)], family="validator"))["invalid_invoked_candidates"], 0)


if __name__ == "__main__":
    unittest.main()
