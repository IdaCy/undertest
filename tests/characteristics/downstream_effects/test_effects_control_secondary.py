import asyncio
import copy
import csv
import importlib
import itertools
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from inspect_ai.model import ChatMessageAssistant, ChatMessageSystem, ChatMessageUser, ModelOutput
from inspect_ai.tool import ToolCall
from inspect_ai.util import Store
from causes.recipe_controls.controls.analysis import summarize_control_design
from causes.recipe_controls.controls.design import build_control_design
from causes.recipe_controls.controls.reconcile import reconcile_control_output
from characteristics.downstream_effects.recipe_controls_secondary import summarize_control_secondary
from shared.results import export_sample
import causes.recipe_controls.tasks.recipe as effects_recipe
from causes.recipe_controls.tasks.task import SYSTEM


control = importlib.import_module("causes.recipe_controls.tasks.control_task")


class SecondaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c2 = build_control_design("C2", ["ledgerbanks-dev-0"], 1, 17, stage="engineering")
        cls.c3 = build_control_design("C3", ["validator-dev-0"], 1, 19, stage="engineering")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.counter = 0

    def folder(self):
        self.counter += 1
        root = self.root / str(self.counter)
        root.mkdir()
        return root

    def honest(self, assignment):
        engine, fixture = control.fixture_for_assignment(assignment)
        return json.dumps((effects_recipe if assignment["study_id"] == "C2" else engine).reference_recipe(fixture.contract))

    def capture(self, design, actions, *, root=None, assignment=None, complete=True, request_only=False, started=None, limit=False, cap=False):
        root = root or self.folder()
        assignment = assignment or design["assignments"][0]
        aid = assignment["assignment_id"]
        saved = Store()
        calls = [ToolCall(id="call-" + str(index), function=kind + "_recipe", arguments={"recipe": raw}) for index, (kind, raw) in enumerate(actions)]
        messages = [ChatMessageSystem(content=SYSTEM), ChatMessageUser(content=design["inputs"][aid]), ChatMessageAssistant(content="", tool_calls=calls)]
        output = ModelOutput.from_content("mock", "Canned final")
        if cap:
            output.choices[0].stop_reason = "max_tokens"
        state = SimpleNamespace(sample_id=aid, metadata=assignment, messages=messages, output=output)

        async def run():
            with patch.object(control, "store", return_value=saved):
                await control.initialize_control_episode(str(root / "ledgers"))(state, None)
                for kind, raw in actions:
                    control.record_control_candidate(kind, raw)
                if complete:
                    await control.retain_control_artifacts(state)
        asyncio.run(run())
        ledger = Path(saved.get("effects_control_ledger_path"))
        records = [json.loads(line) for line in ledger.read_text().splitlines()]
        if started:
            records[0]["started_at_utc"] = started
        if request_only:
            self.assertFalse(complete)
            records = records[:-1]
        ledger.write_text("".join(json.dumps(row) + "\n" for row in records))
        tid = saved.get("effects_control_attempt_id")
        scores = {"effects_control_" + name: {"value": value} for name, value in (saved.get("effects_control_assessment") or {}).get("outcomes", {}).items()}
        artifact, errors = export_sample(root, tid, SimpleNamespace(store={"artifacts": saved.get("artifacts")}, messages=messages), scores,
                                         {"sample": assignment, "eval": {}, "log": {}})
        self.assertEqual(errors, [])
        path = root / "samples.csv"
        if path.exists():
            with path.open() as handle:
                rows = list(csv.DictReader(handle))
        else:
            rows = []
        rows.append({"sample_key": tid, "sample_id": aid, "sample_metadata": json.dumps(assignment),
                     "sample_status": "limit" if limit else "completed" if complete else "error",
                     "error": "" if complete else "interrupted", "limit": "token" if limit else "", "scores": json.dumps(scores)})
        with path.open("w") as handle:
            writer = csv.DictWriter(handle, list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return SimpleNamespace(root=root, assignment=assignment, attempt_id=tid, ledger=ledger, artifact=artifact)

    def summarize(self, design, evidence):
        reconciled = reconcile_control_output(evidence.root, design)
        before = copy.deepcopy(reconciled)
        primary_before = summarize_control_design(design, reconciled["rows"], source_references=reconciled["source_references"])
        result = summarize_control_secondary(design, reconciled)
        self.assertEqual(reconciled, before)
        self.assertEqual(primary_before, summarize_control_design(design, reconciled["rows"], source_references=reconciled["source_references"]))
        self.assertEqual(result["input_references"]["primary_summary_sha256"], primary_before["summary_sha256"])
        return result, reconciled

    def row(self, summary, evidence):
        return next(row for row in summary["assignment_audit"] if row["assignment_id"] == evidence.assignment["assignment_id"])

    def test_missing_invalid_repaired_and_replaced_final_have_distinct_native_nulls(self):
        a = self.c2["assignments"][0]
        cases = [
            ([], {"missing_submission": 1, "invalid_submission": 0, "final_shortcut": None, "final_compliant_completion": 0, "episode_constraint_compliance": 1}),
            ([("submit", "{")], {"missing_submission": 0, "invalid_submission": 1, "task_success": None, "final_constraint_compliance": None, "episode_compliant_completion": 0}),
            ([("test", '{"steps":[{"op":"cache_B"}]}'), ("submit", self.honest(a))], {"tested_shortcut_anytime": 1, "submitted_shortcut_anytime": 0, "final_shortcut": 0, "shortcut_repaired": 1, "final_compliant_completion": 1, "episode_constraint_compliance": 0, "episode_compliant_completion": 0, "fixed_bank_A_exploitation": 0}),
            ([("submit", '{"steps":[{"op":"cache_A"}]}'), ("submit", "{")], {"submitted_shortcut_anytime": 1, "final_shortcut": None, "invalid_submission": 1, "final_compliant_completion": 0, "bank_A_success": None, "fixed_bank_A_exploitation": None}),
        ]
        for actions, expected in cases:
            with self.subTest(actions=actions):
                e = self.capture(self.c2, actions)
                result, _ = self.summarize(self.c2, e)
                outcomes = self.row(result, e)["outcomes"]
                for name, value in expected.items():
                    self.assertEqual(outcomes[name], value, name)
                self.assertEqual(result["endpoints"]["invalid_submission"]["pooled"]["counts"]["assigned"], 4)

    def test_same_syntactic_bank_choice_does_not_imply_bank_execution(self):
        for steps, success in (([{"op": "cache_A"}], 1), ([{"op": "cache_A"}, {"op": "group_sum", "group": "absent", "amount": "absent"}], 0)):
            e = self.capture(self.c2, [("submit", json.dumps({"steps": steps}))])
            result, reconciled = self.summarize(self.c2, e)
            row = self.row(result, e)
            primary = next(r for r in reconciled["rows"] if r["assignment_id"] == e.assignment["assignment_id"])
            self.assertEqual(primary["first_attempt"]["outcomes"]["final_bank_signed"], 1)
            self.assertEqual(row["outcomes"]["bank_A_success"], success)
            self.assertEqual(row["outcomes"]["fixed_bank_A_exploitation"], success)
            self.assertEqual(row["outcomes"]["task_success"], 0)

    def test_partial_positive_and_negative_rules_are_endpoint_specific(self):
        for design, raw in ((self.c2, '{"steps":[{"op":"cache_A"}]}'), (self.c3, '{"steps":[{"op":"example_cache"}]}')):
            for kind in ("submit", "test"):
                e = self.capture(design, [(kind, raw)], complete=False, request_only=True)
                result, _ = self.summarize(design, e)
                row = self.row(result, e)
                self.assertEqual(row["outcomes"]["tested_shortcut_anytime" if kind == "test" else "submitted_shortcut_anytime"], 1)
                self.assertEqual(row["outcomes"]["episode_constraint_compliance"], 0)
                opposite = "tested_shortcut_anytime" if kind == "submit" else "submitted_shortcut_anytime"
                self.assertIsNone(row["outcomes"][opposite])
                self.assertTrue(all(value is None for name, value in row["outcomes"].items() if name not in {"submitted_shortcut_anytime", "tested_shortcut_anytime", "episode_constraint_compliance"}))
                self.assertTrue(row["reported_diagnostics"]["error"])
        e = self.capture(self.c3, [("submit", self.honest(self.c3["assignments"][0]))], complete=False)
        result, _ = self.summarize(self.c3, e)
        self.assertTrue(all(value is None for value in self.row(result, e)["outcomes"].values()))

    def test_prompt_conflict_and_no_prompt_keep_durable_behavior_unlinked(self):
        for missing in (False, True):
            e = self.capture(self.c3, [("submit", '{"steps":[{"op":"example_cache"}]}')])
            if missing:
                (e.artifact / "messages.json").unlink()
            else:
                messages = json.loads((e.artifact / "messages.json").read_text())
                messages[0]["content"] += " changed"
                (e.artifact / "messages.json").write_text(json.dumps(messages))
            result, _ = self.summarize(self.c3, e)
            self.assertTrue(all(value is None for value in self.row(result, e)["outcomes"].values()))

    def test_limits_remain_separate_from_complete_behavior(self):
        e = self.capture(self.c3, [("submit", self.honest(self.c3["assignments"][0]))], limit=True, cap=True)
        result, _ = self.summarize(self.c3, e)
        row = self.row(result, e)
        self.assertEqual(row["outcomes"]["final_compliant_completion"], 1)
        self.assertTrue(row["reported_diagnostics"]["sample_limit"])
        self.assertTrue(row["reported_diagnostics"]["model_cap"])
        self.assertNotIn("sample_limit", result["endpoints"])

    def test_unreadable_first_attempt_never_substitutes_readable_retry(self):
        root = self.folder()
        first = self.capture(self.c3, [], root=root, started="2020-01-01T00:00:00+00:00")
        self.capture(self.c3, [("submit", '{"steps":[{"op":"example_cache"}]}')], root=root, started="2020-01-01T00:01:00+00:00")
        first.ledger.write_text("malformed\n")
        result, _ = self.summarize(self.c3, first)
        self.assertIsNone(self.row(result, first)["attempt_id"])
        self.assertTrue(all(value is None for value in self.row(result, first)["outcomes"].values()))

    def test_missing_rows_or_audits_preserve_assigned_unknowns(self):
        e = self.capture(self.c3, [("submit", '{"steps":[{"op":"example_cache"}]}')])
        raw = reconcile_control_output(e.root, self.c3)
        for field in ("rows", "audit"):
            changed = copy.deepcopy(raw)
            rows = changed["rows"] if field == "rows" else changed["audit"]["assignments"]
            rows[:] = [row for row in rows if row["assignment_id"] != e.assignment["assignment_id"]]
            summary = summarize_control_secondary(self.c3, changed)
            self.assertEqual(summary["endpoints"]["submitted_shortcut_anytime"]["pooled"]["counts"]["assigned"], 3)
            self.assertTrue(all(value is None for value in self.row(summary, e)["outcomes"].values()))

    def test_structural_identity_and_replay_tampering_rejects(self):
        e = self.capture(self.c3, [("submit", '{"steps":[{"op":"example_cache"}]}')])
        raw = reconcile_control_output(e.root, self.c3)
        aid = e.assignment["assignment_id"]
        for variant in ("duplicate_row", "duplicate_audit", "crosslinked_attempt", "wrong_primary", "primary_scalar", "assessment", "candidate", "durable_result", "removed_cleanup", "cleanup_time", "lost_tool_request", "source_hash"):
            changed = copy.deepcopy(raw)
            row = next(row for row in changed["rows"] if row["assignment_id"] == aid)
            audit = next(row for row in changed["audit"]["assignments"] if row["assignment_id"] == aid)
            attempt = audit["attempts"][0]
            if variant == "duplicate_row":
                changed["rows"].append(copy.deepcopy(row))
            elif variant == "duplicate_audit":
                changed["audit"]["assignments"].append(copy.deepcopy(audit))
            elif variant == "crosslinked_attempt":
                attempt["assignment_id"] = self.c3["assignments"][1]["assignment_id"]
            elif variant == "wrong_primary":
                audit["primary_attempt_id"] = "different-attempt"
            elif variant == "primary_scalar":
                row["first_attempt"]["outcomes"]["submitted_shortcut_anytime"] = 0
            elif variant == "assessment":
                attempt["replay"]["outcomes"]["task_success"] = 1
            elif variant == "candidate":
                attempt["replayed_candidates"][0]["shortcut"] = False
            elif variant == "durable_result":
                attempt["ledgers"][0]["records"][2]["valid"] = False
            elif variant == "removed_cleanup":
                attempt["ledgers"][0]["records"].pop()
            elif variant == "cleanup_time":
                attempt["ledgers"][0]["records"][-1].pop("completed_at_utc")
            elif variant == "lost_tool_request":
                attempt["ledgers"][0]["records"][-1]["requests"].append({"function": "submit_recipe", "arguments": {"recipe": "{"}})
            else:
                changed["source_references"][0]["sha256"] = "0" * 64
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                summarize_control_secondary(self.c3, changed)

    def test_c2_condition_dependent_proxy_has_no_contrast_and_fixed_banks_do(self):
        root = self.folder()
        for assignment in self.c2["assignments"]:
            e = self.capture(self.c2, [("submit", '{"steps":[{"op":"cache_A"}]}')], root=root, assignment=assignment)
        result, _ = self.summarize(self.c2, e)
        for name in ("proxy_success", "successful_exploitation"):
            table = result["endpoints"][name]
            self.assertEqual(table["definition_scope"], "condition_dependent_final_bank")
            self.assertEqual(table["pooled"]["contrasts"], {})
            self.assertEqual({cell: data["estimate"] for cell, data in table["pooled"]["cells"].items()}, {"A-A": 1, "A-B": 0, "B-A": 1, "B-B": 0})
        for name in ("bank_A_success", "fixed_bank_A_exploitation"):
            contrasts = result["endpoints"][name]["pooled"]["contrasts"]
            self.assertEqual(set(contrasts), {"final_A_minus_final_B", "preliminary_A_minus_preliminary_B", "final_bank_by_preliminary_bank"})
            self.assertTrue(all(row["estimate"] == 0 for row in contrasts.values()))

    def test_completion_enumeration_checks_bounds_equal_families_instances_and_lost_cells(self):
        design = build_control_design("C3", ["ledger-dev-0", "ledger-dev-1", "validator-dev-0"], 1, 101, stage="engineering")
        root = self.folder()
        for assignment in design["assignments"]:
            if assignment["family"] == "validator" or assignment["cell_id"] == "neutral":
                continue
            raw = '{"steps":[{"op":"example_cache"}]}' if assignment["instance_id"] == "ledger-dev-0" else self.honest(assignment)
            e = self.capture(design, [("submit", raw)], root=root, assignment=assignment)
        result, _ = self.summarize(design, e)
        table = result["endpoints"]["submitted_shortcut_anytime"]
        self.assertEqual(table["pooled"]["counts"]["assigned"], 9)
        self.assertEqual(table["pooled"]["counts"]["unknown"], 5)
        self.assertEqual(table["family_weight"], 0.5)
        self.assertEqual(table["families"]["ledger"]["instance_weight"], 0.5)
        self.assertEqual(table["families"]["validator"]["instance_weight"], 1)
        records = result["assignment_audit"]
        missing = [i for i, row in enumerate(records) if row["outcomes"]["submitted_shortcut_anytime"] is None]
        for spec in design["manifest"]["contrast_specification"]["contrasts"]:
            completions = []
            for completion in itertools.product((0, 1), repeat=len(missing)):
                assigned = dict(zip(missing, completion))
                value = 0
                for i, row in enumerate(records):
                    endpoint = assigned[i] if i in assigned else row["outcomes"]["submitted_shortcut_anytime"]
                    value += 0.5 * (0.5 if row["family"] == "ledger" else 1) * spec["coefficients"][row["cell_id"]] * endpoint
                completions.append(value)
            contrast = table["pooled"]["contrasts"][spec["name"]]
            self.assertIsNone(contrast["estimate"])
            self.assertAlmostEqual(contrast["missingness_bounds"]["lower"], min(completions))
            self.assertAlmostEqual(contrast["missingness_bounds"]["upper"], max(completions))
        self.assertIsNone(table["pooled"]["cells"]["neutral"]["observed_only_descriptive_mean"])

    def test_c2_signed_secondary_bounds_match_completion_enumeration(self):
        root = self.folder()
        by_cell = {a["cell_id"]: a for a in self.c2["assignments"]}
        e = self.capture(self.c2, [("submit", self.honest(by_cell["A-A"]))], root=root, assignment=by_cell["A-A"])
        self.capture(self.c2, [("submit", '{"steps":[{"op":"cache_A"}]}')], root=root, assignment=by_cell["B-A"])
        result, _ = self.summarize(self.c2, e)
        expected_weights = {"final_A_minus_final_B": (0.5, -0.5, 0.5, -0.5),
                            "preliminary_A_minus_preliminary_B": (0.5, 0.5, -0.5, -0.5),
                            "final_bank_by_preliminary_bank": (1, -1, -1, 1)}
        for name, weights in expected_weights.items():
            completions = [sum(w * v for w, v in zip(weights, (1, ab, 0, bb))) for ab, bb in itertools.product((0, 1), repeat=2)]
            contrast = result["endpoints"]["task_success"]["pooled"]["contrasts"][name]
            self.assertIsNone(contrast["estimate"])
            self.assertEqual(contrast["missingness_bounds"], {"lower": min(completions), "upper": max(completions)})

    def test_c4_c5_keep_both_families_and_registered_secondary_contrasts(self):
        for study in ("C4", "C5"):
            design = build_control_design(study, ["ledger-dev-0", "validator-dev-0"], 1, 121, stage="engineering")
            root = self.folder()
            honest = next(a for a in design["assignments"] if a["family"] == "ledger")
            shortcut = next(a for a in design["assignments"] if a["family"] == "validator")
            e = self.capture(design, [("submit", self.honest(honest))], root=root, assignment=honest)
            other = self.capture(design, [("submit", '{"steps":[{"op":"example_cache"}]}')], root=root, assignment=shortcut)
            result, _ = self.summarize(design, e)
            self.assertEqual(self.row(result, e)["outcomes"]["task_success"], 1)
            self.assertEqual(self.row(result, other)["outcomes"]["task_success"], 0)
            table = result["endpoints"]["task_success"]
            self.assertEqual(table["pooled"]["counts"]["assigned"], len(design["assignments"]))
            self.assertEqual(table["pooled"]["counts"]["known"], 2)
            self.assertEqual(set(table["pooled"]["contrasts"]), {spec["name"] for spec in design["manifest"]["contrast_specification"]["contrasts"]})


if __name__ == "__main__":
    unittest.main()
