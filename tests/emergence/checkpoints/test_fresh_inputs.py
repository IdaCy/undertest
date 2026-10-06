import hashlib
import json
import unittest

from emergence.checkpoints.fresh_inputs import ByteIdentity
from emergence.checkpoints.fresh_inputs import CHECKPOINTS
from emergence.checkpoints.fresh_inputs import load_primary_inputs
from emergence.checkpoints.fresh_reconciliation import identification_bounds


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


def identity(raw):
    return ByteIdentity(len(raw), hashlib.sha256(raw).hexdigest())


def documents():
    """Small synthetic content exercises joins, not task-oracle correctness."""
    samples, rows = [], []
    kinds = ["main"] * 256 + ["competence"] * 16 + ["format"] * 16 + ["score_comprehension"] * 32
    for i, kind in enumerate(kinds):
        key, prompt = f"sample-{i}", f"synthetic input {i}"
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        grading = "limited" if i % 2 == 0 else "broad"
        metadata = dict(seed=i, replicate=i % 4, sample_kind=kind, family="toy", rule_split="confirmation_core",
                        cue_variant="confirmation_anchor", framing="ordinary", grading=grading,
                        input_sha256=digest, instance_id="rule-" + str(i), cluster_id="toy-group")
        samples.append(dict(id=key, input=prompt, metadata=metadata))
        for checkpoint in CHECKPOINTS:
            sign = (1 if checkpoint == CHECKPOINTS[1] else -1) * (1 if grading == "limited" else -1)
            rows.append(dict(physical_id=checkpoint + "::" + key, checkpoint_id=checkpoint, sample_id=key,
                             block="primary_core_anchor", prompt_sha256=digest, rule_id=metadata["instance_id"],
                             group_id="toy/toy-group", coefficient_for_own_cue_contrast=[sign, 128] if kind == "main" else None,
                             **{k: metadata[k] for k in ("seed", "replicate", "sample_kind", "family", "rule_split",
                                                        "cue_variant", "framing", "grading")}))
    # Physical order intentionally differs from the sample and checkpoint order.
    return dict(schema="section-a-primary640-saved-roster-candidate.v1", primary_rows=rows[::-1]), dict(samples=samples)


def select(frame, panel):
    a, b = encoded(frame), encoded(panel)
    return load_primary_inputs(a, b, roster=identity(a), panel=identity(b))


class FreshInputsTest(unittest.TestCase):
    def test_physical_order_and_complete_missing_frame(self):
        frame, panel = documents()
        result = select(frame, panel)
        self.assertEqual([r.assignment.physical_id for r in result.rows], [r["physical_id"] for r in frame["primary_rows"]])
        self.assertEqual(len(result.for_checkpoint(CHECKPOINTS[0])), 320)
        self.assertEqual([r.assignment.physical_id for r in result.for_checkpoint(CHECKPOINTS[0])],
                         [r["physical_id"] for r in frame["primary_rows"] if r["checkpoint_id"] == CHECKPOINTS[0]])
        bounds = identification_bounds([r.assignment for r in result.rows], [], "segmented")
        self.assertEqual((bounds["lower"], bounds["upper"]), (-2, 2))
        with self.assertRaises(ValueError):
            result.for_checkpoint("outside")

    def test_changed_bytes_fail_even_if_json_is_equivalent(self):
        frame, panel = documents()
        a, b = encoded(frame), encoded(panel)
        with self.assertRaises(ValueError):
            load_primary_inputs(a + b" ", b, roster=identity(a), panel=identity(b))

    def test_duplicate_keys_and_nonfinite_values_refuse(self):
        _, panel = documents()
        b = encoded(panel)
        for a in (b'{"primary_rows":[],"primary_rows":[]}', b'{"value":NaN}'):
            with self.subTest(raw=a), self.assertRaises(ValueError):
                load_primary_inputs(a, b, roster=identity(a), panel=identity(b))

    def test_rehashed_roster_must_still_match_panel(self):
        for field, value in (("seed", True), ("prompt_sha256", "0" * 64), ("block", "rule_transfer"),
                             ("physical_id", "outside::sample"), ("rule_id", "other-rule")):
            frame, panel = documents()
            frame["primary_rows"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                select(frame, panel)

    def test_duplicate_assignment_or_incomplete_panel_refuses(self):
        frame, panel = documents()
        frame["primary_rows"][1] = dict(frame["primary_rows"][0])
        with self.assertRaises(ValueError):
            select(frame, panel)
        frame, panel = documents()
        panel["samples"].pop()
        with self.assertRaises(ValueError):
            select(frame, panel)

    def test_control_and_main_weight_guards(self):
        frame, panel = documents()
        next(r for r in frame["primary_rows"] if r["sample_kind"] != "main")["coefficient_for_own_cue_contrast"] = [1, 128]
        with self.assertRaises(ValueError):
            select(frame, panel)
        frame, panel = documents()
        next(r for r in frame["primary_rows"] if r["sample_kind"] == "main")["coefficient_for_own_cue_contrast"] = [True, 128]
        with self.assertRaises(ValueError):
            select(frame, panel)

    def test_rehashed_panel_still_checks_prompt_and_metadata(self):
        frame, panel = documents()
        panel["samples"][0]["input"] = "changed"
        with self.assertRaises(ValueError):
            select(frame, panel)
        frame, panel = documents()
        panel["samples"][0]["metadata"]["grading"] = "different"
        with self.assertRaises(ValueError):
            select(frame, panel)


if __name__ == "__main__":
    unittest.main()
