import copy
import unittest

from signals.validation.controls.run import validate_freeze
from signals.reasoning.run_monitor import validate_confirmation_monitor
from signals.reasoning.monitor import prompt_hash


class ConfirmationFreezeTests(unittest.TestCase):
    def test_pre_run_freeze_requires_operating_point_and_exact_monitor_hash(self):
        source = {"runner.py": "a" * 64}
        freeze = {"source_sha256": source, "monitor_sha256": prompt_hash(),
                  "protocol_sha256": "b" * 64, "operating_points_file_sha256": "c" * 64}
        validate_freeze(freeze, source)
        for value in (None, True, "", "agreed", "A" * 64, "a" * 63):
            bad = copy.deepcopy(freeze)
            bad["operating_points_file_sha256"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_freeze(bad, source)
        bad = copy.deepcopy(freeze)
        bad["monitor_sha256"] = "d" * 64
        with self.assertRaises(ValueError):
            validate_freeze(bad, source)
        with self.assertRaises(ValueError):
            validate_freeze(freeze, {"runner.py": "e" * 64})

    def test_null_freeze_is_rejected_and_monitor_plan_cannot_drift(self):
        with self.assertRaises(ValueError):
            validate_freeze(None, {})
        source = {name: "a" * 64 for name in
                  ("src/signals/reasoning/monitor.py", "src/signals/reasoning/observations.py")}
        freeze = {"source_sha256": source, "monitor_model": "fake", "monitor_model_revision": "r1",
                  "monitor_cutoffs": ["retrospective"], "monitor_sha256": prompt_hash()}
        actor = {"assignments": [{"split": "confirmation"}], "manifest": {"freeze": freeze}}
        validate_confirmation_monitor(actor, "fake", "r1", ["retrospective"], source)
        for model, revision, cutoffs, sources in (
            ("other", "r1", ["retrospective"], source),
            ("fake", "r2", ["retrospective"], source),
            ("fake", "r1", ["prospective"], source),
            ("fake", "r1", ["retrospective"], {}),
        ):
            with self.subTest(model=model, revision=revision, cutoffs=cutoffs), self.assertRaises(ValueError):
                validate_confirmation_monitor(actor, model, revision, cutoffs, sources)


if __name__ == "__main__":
    unittest.main()
