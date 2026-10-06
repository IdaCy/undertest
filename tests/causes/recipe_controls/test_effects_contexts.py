import itertools
import unittest
from unittest.mock import patch

from causes.recipe_controls.tasks.contexts import context_cells
from causes.recipe_controls.tasks.contexts import context_for
from causes.recipe_controls.tasks.contexts import render_context_request
from causes.recipe_controls.tasks.task import FRAMING
from causes.recipe_controls.tasks.task import render_request


class EffectsContextTests(unittest.TestCase):
    def test_all_contexts_preserve_the_complete_task_body(self):
        instances = [f"{family}-dev-{number}" for family, number in itertools.product(("ledger", "validator"), (0, 5, 10, 15))]
        for instance in instances:
            original = render_request(instance, "evaluation", "limited", "base")
            body = original.split("\n\n", 2)[2]
            for study in ("oversight", "forecast", "scenario"):
                for cell in context_cells(study):
                    with self.subTest(instance=instance, study=study, cell=cell):
                        context = context_for(study, cell)
                        prompt = render_context_request(instance, study, cell)
                        self.assertEqual(prompt.split("\n\n", len(context.paragraphs))[-1], body)
                        self.assertEqual(context.paragraphs[0], FRAMING["base"]["evaluation"])
                        self.assertNotIn("Recipe text and development actions are outside its coverage.", prompt)
                        self.assertIn("This prohibition applies to tested candidates and every submitted revision.", prompt)

    def test_only_the_selected_oversight_or_forecast_paragraph_varies(self):
        for study, differing_index in (("oversight", 2), ("forecast", 3)):
            contexts = [context_for(study, cell) for cell in context_cells(study)]
            shared = {tuple(text for index, text in enumerate(context.paragraphs) if index != differing_index) for context in contexts}
            self.assertEqual(len(shared), 1)
            self.assertEqual(len({context.paragraphs[differing_index] for context in contexts}), 3)
            self.assertEqual(len({context.factors for context in contexts}), 3)

    def test_scenario_crosses_each_setting_with_each_use(self):
        contexts = [context_for("scenario", cell) for cell in context_cells("scenario")]
        self.assertEqual(len(contexts), 6)
        self.assertEqual({context.factors for context in contexts}, {
            (("setting", setting), ("use", use))
            for setting, use in itertools.product(("simulated", "operational", "neutral"), ("reuse", "archive"))
        })
        for left, right in itertools.combinations(contexts, 2):
            changed = {index for index, pair in enumerate(zip(left.paragraphs, right.paragraphs)) if pair[0] != pair[1]}
            expected = set()
            if left.factors[0] != right.factors[0]:
                expected.add(4)
            if left.factors[1] != right.factors[1]:
                expected.add(5)
            self.assertEqual(changed, expected)

    def test_unknown_cells_and_changed_context_boundaries_fail(self):
        with self.assertRaises(ValueError):
            context_cells("unknown")
        for study in ("oversight", "forecast", "scenario"):
            with self.assertRaises(ValueError):
                context_for(study, "unknown")
        with patch("causes.recipe_controls.tasks.contexts.render_request", return_value="changed\n\nscore\n\nbody"):
            with self.assertRaises(ValueError):
                render_context_request("ledger-dev-0", "oversight", "artifact")


if __name__ == "__main__":
    unittest.main()
