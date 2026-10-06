import copy
import json
import unittest
from collections import Counter

from causes.recipe_controls.controls.diagnostics import build_diagnostic_plan
from causes.recipe_controls.controls.diagnostics import score_diagnostic_response
from causes.recipe_controls.controls.diagnostics import validate_diagnostic_plan
import causes.recipe_controls.tasks.banks as banks
import causes.recipe_controls.tasks.recipe as ledger


class DiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = build_diagnostic_plan(2026093005)

    def literal(self, study):
        return next(s for s in self.plan["slots"] if s["study_id"] == study and s["kind"] == "literal_notice")

    def score(self, slot, value, **kwargs):
        return score_diagnostic_response(self.plan, slot["slot_id"], value, **kwargs)

    def test_population_and_seed_only_reorders(self):
        counts = Counter((s["study_id"], s["kind"]) for s in self.plan["slots"])
        self.assertEqual(counts, {("C2", "literal_notice"): 16, ("C2", "interface_expression"): 6,
                                  ("C3", "literal_notice"): 6, ("C4", "literal_notice"): 6, ("C5", "literal_notice"): 12})
        other = build_diagnostic_plan(2026093006)
        self.assertNotEqual(other["manifest"]["slot_ids"], self.plan["manifest"]["slot_ids"])
        self.assertEqual(set(other["manifest"]["slot_ids"]), set(self.plan["manifest"]["slot_ids"]))

    def test_every_answer_key_against_exact_plan(self):
        for slot in self.plan["slots"]:
            if slot["kind"] == "literal_notice":
                with self.subTest(slot=slot["slot_id"]):
                    self.assertTrue(self.score(slot, json.dumps(slot["expected_answer"]))["passed"])

    def test_mapping_does_not_change_displayed_bank_answers(self):
        for cell in ("A-A", "A-B", "B-A", "B-B"):
            slots = [s for s in self.plan["slots"] if s["study_id"] == "C2" and s["cell_id"] == cell and s["kind"] == "literal_notice"]
            self.assertEqual(len(slots), 4)
            for slot in slots:
                self.assertEqual(slot["expected_answer"]["preliminary_scored_bank"], cell[0])
                self.assertEqual(slot["expected_answer"]["final_scored_bank"], cell[-1])

    def test_response_alternatives_can_be_structurally_valid_and_wrong(self):
        for study, field, wrong in (("C4", "scoring_examples", "protected_only"),
                                    ("C5", "actual_host_setting_from_notice", "simulated")):
            slot = self.literal(study)
            answer = copy.deepcopy(slot["expected_answer"])
            answer[field] = wrong
            result = self.score(slot, json.dumps(answer))
            self.assertEqual(result["status"], "literal_incorrect")
            self.assertFalse(result["field_correct"][field])

    def test_duplicate_keys_extra_keys_numeric_booleans_and_json_prose_rejected(self):
        slot = self.literal("C2")
        raw = json.dumps(slot["expected_answer"])
        variants = [raw[:-1] + ',"common_examples_scored":false}', "```json\n" + raw + "\n```", raw + " trailing"]
        for change in ({"cache_A_permitted": 0}, {"extra": False}, {"public_tested_banks": ["A", "A"]}):
            value = dict(slot["expected_answer"])
            value.update(change)
            variants.append(json.dumps(value))
        for raw in variants:
            self.assertEqual(self.score(slot, raw)["status"], "invalid_json_or_schema")

    def test_tested_bank_set_order_and_wrong_subset(self):
        slot = self.literal("C2")
        answer = dict(slot["expected_answer"])
        answer["public_tested_banks"] = ["B", "A"]
        self.assertTrue(self.score(slot, json.dumps(answer))["passed"])
        answer["public_tested_banks"] = ["A"]
        self.assertEqual(self.score(slot, json.dumps(answer))["status"], "literal_incorrect")

    def test_general_and_both_induced_cache_references(self):
        for slot in self.plan["slots"]:
            if slot["kind"] != "interface_expression":
                continue
            recipe = (ledger.reference_recipe(banks.contract_for(slot["instance_id"])) if slot["expression"] == "general"
                      else {"steps": [{"op": slot["expression"]}]})
            result = self.score(slot, json.dumps(recipe))
            self.assertTrue(result["passed"], result)
            self.assertFalse(result["induced_expression_is_spontaneous_gaming"])

    def test_wrong_expression_composition_and_general_failure_are_retained(self):
        for slot in self.plan["slots"]:
            if slot["kind"] != "interface_expression":
                continue
            wrong = {"steps": [{"op": "cache_B" if slot["expression"] == "cache_A" else "cache_A"}]}
            self.assertEqual(self.score(slot, json.dumps(wrong))["status"], "expression_incorrect")
        slot = next(s for s in self.plan["slots"] if s["expression"] == "cache_B")
        composed = {"steps": [{"op": "cache_A"}, {"op": "cache_B"}]}
        self.assertEqual(self.score(slot, json.dumps(composed))["status"], "expression_incorrect")

    def test_cap_error_and_missing_do_not_pass_even_with_answer(self):
        slot = self.literal("C3")
        for status in ("output_limit", "error", "unavailable"):
            result = self.score(slot, json.dumps(slot["expected_answer"]), response_status=status)
            self.assertIsNone(result["passed"])
            self.assertEqual(result["status"], status)
            self.assertFalse(result["behavioral_assignment_filter"])
        self.assertEqual(self.score(slot, None)["status"], "missing_final_content")

    def test_plan_mutation_and_cross_slot_rejected(self):
        for mutation in ("request", "answer", "source", "order"):
            plan = copy.deepcopy(self.plan)
            if mutation == "request":
                plan["slots"][0]["messages"][0]["content"] += "changed"
            elif mutation == "answer":
                next(s for s in plan["slots"] if s["kind"] == "literal_notice")["expected_answer"] = {}
            elif mutation == "source":
                plan["manifest"]["source_sha256"] = {}
            else:
                plan["slots"].reverse()
            with self.assertRaises(ValueError):
                validate_diagnostic_plan(plan)
        with self.assertRaises(ValueError):
            score_diagnostic_response(self.plan, "unknown", "{}")

    def test_quote_is_data_and_answer_keys_are_not_sent(self):
        for slot in self.plan["slots"]:
            user = json.loads(slot["messages"][1]["content"])
            self.assertNotIn("expected_answer", user)
            self.assertNotIn("fixture_sha256", user)
            self.assertIsInstance(user["quoted_task"], str)
            self.assertEqual(len(slot["messages"]), 2)
            if slot["kind"] == "literal_notice":
                for spec in user["output_specification"]["properties"].values():
                    self.assertNotIn("const", spec)
                    if "enum" in spec:
                        self.assertGreater(len(spec["enum"]), 1)


if __name__ == "__main__":
    unittest.main()
