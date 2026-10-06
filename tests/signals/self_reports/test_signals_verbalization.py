import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from shared.runs import write_json
import signals.self_reports.verbalization as v
from signals.validation.controls.runner import ChatEndpoint
from signals.validation.controls.runner import digest


POLICY = {"mode": "retain", "rationale": "Retain the public identifier without changing task semantics.",
          "semantics_review_sha256": "a" * 64}
RUBRIC = "Synthetic test rubric. Categorize emitted language only."


def trajectory(reasoning="I may be evaluated."):
    return {"initial_context": [{"role": "user", "content": '{"case_id":"PUBLIC_HASH","task":"initial task"}',
                                 "assignment_id": "HIDDEN_INITIAL_METADATA"}],
            "tools": [], "messages": [{"role": "assistant", "reasoning_content": reasoning,
                "content": "HIDDEN_PUBLIC_ACTION", "tool_calls": [{"arguments": "HIDDEN_TOOL_CALL"}],
                "labels": "HIDDEN_LABEL"}], "retrospective_cutoff": 1, "status": "completed",
            "assignment": {"assignment_id": "HIDDEN_ASSIGNMENT"}, "labels": "HIDDEN_RESULT",
            "model_monitor_probability": "HIDDEN_PROBABILITY"}


def annotation(identifier, spans=None):
    concepts = {name: {"spans": []} for name in v.CONCEPTS}
    for name, items in (spans or {}).items():
        concepts[name]["spans"] = items
    return {"annotation_id": identifier, "concepts": concepts}


def response(value, finish="stop"):
    return {"choices": [{"message": {"role": "assistant", "content": json.dumps(value)}, "finish_reason": finish}]}


def span(quote="I may be evaluated.", *, polarity="uncertain", temporality="current_inference", message=0, part=0, occurrence=None):
    return {"message_index": message, "part_index": part, "quote": quote, "occurrence": occurrence,
            "polarity": polarity, "temporality": temporality}


def make_plan(inputs, sources=None):
    plan = {"schema_version": 1, "status": "frozen", "stage": "development", "actor_snapshot": {"snapshot": "fixture"},
            "controls_sha256": "b" * 64, "rubric_sha256": hashlib.sha256(RUBRIC.encode()).hexdigest(),
            "prompt_sha256": v.prompt_hash(RUBRIC), "source_sha256": sources or {},
            "model": "scripted/model", "model_revision": "scripted-revision", "service_identity_sha256": "c" * 64,
            "generation": copy.deepcopy(v.GENERATION), "public_hash_policy": copy.deepcopy(POLICY),
            "selection_sha256": "d" * 64, "adjudication_sha256": "e" * 64,
            "seed_scheme": "externally_assigned_distinct_v1", "seed_table_sha256": None,
            "allocation_sha256": "f" * 64, "assignments": []}
    for (kind, input_id), source in inputs.items():
        for coder in range(2 if kind == "control" else 1):
            task = {"annotation_id": uuid4().hex, "input_kind": kind, "input_id": input_id,
                    "input_sha256": digest(source) if source is not None else None, "coder_id": f"coder-{coder}",
                    "group": "control" if kind == "control" else "first_pass", "seed": len(plan["assignments"]),
                    "cutoff_name": "retrospective", "request_sha256": None}
            if source is not None and source.get("retrospective_cutoff") is not None:
                projection = v.project_reasoning(source, "retrospective", POLICY)
                if projection["record"]["coverage"]["status"] != "unavailable":
                    request = v.annotation_request(projection, annotation_id=task["annotation_id"], model=plan["model"], seed=task["seed"], rubric=RUBRIC)
                    task["request_sha256"] = digest(request)
            plan["assignments"].append(task)
    plan["seed_table_sha256"] = digest([{key: task[key] for key in ("annotation_id", "coder_id", "seed")} for task in plan["assignments"]])
    return plan


class VerbalizationTests(unittest.TestCase):
    def setUp(self):
        self.identifier = uuid4().hex
        self.projection = v.project_reasoning(trajectory(), "retrospective", POLICY)

    def parse(self, items=None, projection=None):
        return v.parse_annotation(response(annotation(self.identifier, items)), projection or self.projection, self.identifier)

    def test_reasoning_only_blinding_and_explicit_public_hash_retention(self):
        source = trajectory()
        source["messages"].append({"role": "assistant", "reasoning": "HIDDEN_FUTURE"})
        projection = v.project_reasoning(source, "retrospective", POLICY)
        request = v.annotation_request(projection, annotation_id=self.identifier, model="test", seed=2, rubric=RUBRIC)
        text = json.dumps(request)
        self.assertNotIn("HIDDEN_", text)
        self.assertIn("PUBLIC_HASH", text)
        self.assertNotIn("source_map", text)
        self.assertNotIn("cutoff", json.loads(request["messages"][1]["content"])["record"])
        self.assertEqual(request["max_tokens"], 2048)
        self.assertEqual(request["chat_template_kwargs"], {"enable_thinking": False})
        for policy in ({}, {**POLICY, "mode": "redact"}, {**POLICY, "rationale": ""}):
            with self.assertRaises(ValueError):
                v.project_reasoning(source, "retrospective", policy)

    def test_source_map_is_auditable_without_exposing_tool_positions(self):
        source = trajectory()
        source["messages"] = [
            {"role": "tool", "content": "HIDDEN_TOOL_RESULT"},
            {"role": "assistant", "reasoning_content": None, "reasoning": "first", "content": [
                {"type": "text", "text": "HIDDEN_TEXT"}, {"type": "reasoning", "text": "second"},
                {"type": "reasoning", "text": "HIDDEN_REDACTED", "redacted": True}]},
        ]
        source["retrospective_cutoff"] = 2
        projected = v.project_reasoning(source, "retrospective", POLICY)
        self.assertEqual(projected["record"]["reasoning_messages"], [{"message_index": 0, "parts": [
            {"part_index": 1, "text": "first"}, {"part_index": 2, "text": "second"}], "unavailable_parts": 2}])
        self.assertTrue(projected["record"]["coverage"]["partially_unavailable"])
        mapping = projected["source_map"][2]
        self.assertEqual((mapping["generated_message_index"], mapping["content_block_index"], mapping["field"]), (1, 1, "text"))

    def test_empty_missing_redacted_and_partial_coverage_are_distinct(self):
        for value, expected in ((None, "unavailable"), ("", "empty"), (" \n", "empty"), ("fragment", "readable")):
            with self.subTest(value=value):
                projection = v.project_reasoning(trajectory(value), "retrospective", POLICY)
                parsed = self.parse(projection=projection)
                self.assertEqual(parsed["coverage"]["status"], expected)
                category = "unavailable" if expected == "unavailable" else "no_relevant_statement"
                self.assertTrue(all(item["category"] == category for item in parsed["concepts"].values()))
        source = trajectory("")
        source["messages"].append({"role": "assistant", "content": "public"})
        source["retrospective_cutoff"] = 2
        projected = v.project_reasoning(source, "retrospective", POLICY)
        self.assertEqual(projected["record"]["coverage"]["status"], "empty")
        self.assertTrue(projected["record"]["coverage"]["partially_unavailable"])

    def test_all_categories_come_from_validated_relevant_spans(self):
        source = trajectory("yes no maybe")
        projected = v.project_reasoning(source, "retrospective", POLICY)
        for polarities, expected in ((["affirmed"], "asserted"), (["denied"], "denied"), (["uncertain"], "uncertain"),
                                     (["affirmed", "denied"], "mixed"), (["affirmed", "uncertain"], "mixed"),
                                     (["denied", "uncertain"], "mixed"), ([], "no_relevant_statement")):
            spans = [span(quote, polarity=polarity) for quote, polarity in zip(("yes", "no", "maybe"), polarities)]
            parsed = self.parse({v.CONCEPTS[0]: spans}, projected)
            self.assertEqual(parsed["concepts"][v.CONCEPTS[0]]["category"], expected)

    def test_quotes_examples_counterfactuals_and_past_reports_never_assert_current_concepts(self):
        for temporality in ("quoted_material", "example", "counterfactual", "past_report"):
            parsed = self.parse({name: [span(polarity="affirmed", temporality=temporality)] for name in v.CONCEPTS})
            for item in parsed["concepts"].values():
                self.assertEqual(item["category"], "no_relevant_statement")
                self.assertFalse(item["spans"][0]["relevant"])
        parsed = self.parse({name: [span(polarity="affirmed")] for name in v.CONCEPTS})
        for name in v.CONCEPTS[-2:]:
            self.assertEqual(parsed["concepts"][name]["category"], "no_relevant_statement")
        parsed = self.parse({name: [span(polarity="denied", temporality="future_plan")] for name in v.CONCEPTS[-2:]})
        self.assertTrue(all(parsed["concepts"][name]["category"] == "denied" for name in v.CONCEPTS[-2:]))

    def test_offsets_are_literal_unicode_half_open_and_overlapping_occurrences_work(self):
        projected = v.project_reasoning(trajectory("🙂 é aaa\nend"), "retrospective", POLICY)
        parsed = self.parse({v.CONCEPTS[0]: [span("aa", occurrence=1)]}, projected)
        result = parsed["concepts"][v.CONCEPTS[0]]["spans"][0]
        self.assertEqual((result["start"], result["end"]), (5, 7))
        self.assertEqual("🙂 é aaa\nend"[result["start"]:result["end"]], result["quote"])
        for evidence in (span("aa"), span("aa", occurrence=2), span("é aaa", occurrence=True), span("é")):
            with self.assertRaises(ValueError):
                self.parse({v.CONCEPTS[0]: [evidence]}, projected)

    def test_context_actions_fabrication_bad_indices_and_duplicates_are_rejected(self):
        bad = [span("initial task"), span("HIDDEN_PUBLIC_ACTION"), span("fabricated"), span(part=2),
               span(message=True), span(temporality="present"), span(polarity="positive"), span(" ")]
        for evidence in bad:
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                self.parse({v.CONCEPTS[0]: [evidence]})
        with self.assertRaises(ValueError):
            self.parse({v.CONCEPTS[0]: [span(), span(polarity="affirmed")]})

    def test_strict_schema_no_model_categories_or_silent_repairs(self):
        value = annotation(self.identifier)
        variants = []
        changed = copy.deepcopy(value)
        changed["concepts"][v.CONCEPTS[0]]["category"] = "asserted"
        variants.append(changed)
        changed = copy.deepcopy(value)
        del changed["concepts"][v.CONCEPTS[-1]]
        variants.append(changed)
        variants.append({**value, "annotation_id": uuid4().hex})
        for changed in variants:
            with self.assertRaises(ValueError):
                v.parse_annotation(response(changed), self.projection, self.identifier)
        for text in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', '```json\n{}\n```'):
            with self.assertRaises(ValueError):
                v.strict_json(text)
        for finish in ("length", "tool_calls", None):
            with self.assertRaises(ValueError):
                v.parse_annotation(response(value, finish), self.projection, self.identifier)

    def test_bounded_evidence_keeps_polarities_but_rejects_repetition_and_mixed_exclusions(self):
        projected = v.project_reasoning(trajectory("affirm deny uncertain repeat excluded"), "retrospective", POLICY)
        valid = [span("affirm", polarity="affirmed"), span("deny", polarity="denied"), span("uncertain")]
        self.assertEqual(self.parse({v.CONCEPTS[0]: valid}, projected)["concepts"][v.CONCEPTS[0]]["category"], "mixed")
        for items in (valid + [span("repeat", polarity="affirmed")], list(reversed(valid)),
                      [valid[0], span("excluded", temporality="quoted_material")],
                      [span("affirm", temporality="example"), span("excluded", temporality="quoted_material")]):
            with self.assertRaises(ValueError):
                self.parse({v.CONCEPTS[0]: items}, projected)
        quote = "I do not think " + "the deliberately detailed target proposition " * 40 + "applies here."
        long_projection = v.project_reasoning(trajectory(quote), "retrospective", POLICY)
        parsed = self.parse({v.CONCEPTS[0]: [span(quote, polarity="denied")]}, long_projection)
        self.assertEqual(parsed["concepts"][v.CONCEPTS[0]]["spans"][0]["quote"], quote)

    def test_frozen_plan_seeds_input_prompt_and_source_mutations_reject(self):
        inputs = {("actor", "a"): trajectory(), ("control", "c"): trajectory("constructed")}
        plan = make_plan(inputs)
        kwargs = {"actor_snapshot": plan["actor_snapshot"], "controls_sha256": plan["controls_sha256"], "rubric": RUBRIC, "sources": {}}
        v.validate_plan(plan, **kwargs)
        mutations = [lambda p: p["generation"].update(temperature=0), lambda p: p.update(stage="confirmation"),
                     lambda p: p.update(source_sha256={"extra": "0" * 64}),
                     lambda p: p["assignments"][1].update(seed=p["assignments"][0]["seed"]),
                     lambda p: p["assignments"][0].update(annotation_id="original-assignment-id"),
                     lambda p: p["assignments"].pop(), lambda p: p.update(prompt_sha256="0" * 64)]
        for mutate in mutations:
            changed = copy.deepcopy(plan)
            mutate(changed)
            with self.assertRaises(ValueError):
                v.validate_plan(changed, **kwargs)
        task = plan["assignments"][0]
        v.prepare_assignment(task, plan, inputs, RUBRIC)
        with self.assertRaises(ValueError):
            v.prepare_assignment({**task, "request_sha256": "0" * 64}, plan, inputs, RUBRIC)


class VerbalizationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.run_root = Path(self.temporary.name) / "run"
        self.inputs = {("actor", "actor-id"): trajectory()}
        self.plan = make_plan(self.inputs)
        self.task = self.plan["assignments"][0]
        self.prepared = v.prepare_assignment(self.task, self.plan, self.inputs, RUBRIC)
        self.output = self.run_root / "annotations" / self.task["annotation_id"]
        run_id = uuid4().hex
        self.ledger = v.ConsumptionLedger(self.run_root, {self.task["annotation_id"]: self.prepared},
                                          digest(self.plan), run_id, create=True)
        self.kwargs = {"prepared": self.prepared, "plan_sha256": digest(self.plan), "run_id": run_id,
                       "output": self.output, "ledger": self.ledger}

    def test_preview_then_one_call_then_validated_resume_without_retry(self):
        self.assertEqual(v.run_assignment(**self.kwargs)["status"], "pending")
        calls = []
        def call(request):
            calls.append(request)
            return response(annotation(self.task["annotation_id"]))
        result = v.run_assignment(**self.kwargs, execute=True, call=call, execution_receipt={"fixture": True}, call_kind="injected_test")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(v.run_assignment(**self.kwargs, execute=True, call=call), result)
        self.assertEqual(len(calls), 1)
        changed = json.loads((self.output / "annotation.json").read_text())
        changed["annotation"]["concepts"][v.CONCEPTS[0]]["category"] = "asserted"
        write_json(self.output / "annotation.json", changed)
        with self.assertRaises(ValueError):
            v.run_assignment(**self.kwargs)

    def test_malformed_response_is_retained_failed_slot_never_retried(self):
        calls = []
        def call(request):
            calls.append(request)
            return response({"invalid": "retained"})
        result = v.run_assignment(**self.kwargs, execute=True, call=call, execution_receipt={}, call_kind="injected_test")
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["annotation"])
        self.assertEqual(result["categories"], {name: None for name in v.CONCEPTS})
        self.assertTrue((self.output / "response.json").exists())
        self.assertEqual(v.run_assignment(**self.kwargs, execute=True, call=call), result)
        self.assertEqual(len(calls), 1)

    def test_interrupted_call_consumes_slot_even_if_no_response_was_saved(self):
        def interrupted(_):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            v.run_assignment(**self.kwargs, execute=True, call=interrupted, execution_receipt={}, call_kind="injected_test")
        result = v.run_assignment(**self.kwargs, execute=True, call=lambda _: self.fail("Unexpected retry"))
        self.assertEqual(result["status"], "interrupted")
        self.assertIsNone(result["annotation"])

    def test_missing_reasoning_and_missing_trajectory_accounted_without_call(self):
        for index, source in enumerate((trajectory(None), None)):
            inputs = {("actor", "actor-id"): source}
            plan = make_plan(inputs)
            prepared = v.prepare_assignment(plan["assignments"][0], plan, inputs, RUBRIC)
            identifier = prepared["task"]["annotation_id"]
            ledger = v.ConsumptionLedger(self.run_root / str(index), {identifier: prepared}, digest(plan), self.kwargs["run_id"], create=True)
            result = v.run_assignment(prepared=prepared, plan_sha256=digest(plan), run_id=self.kwargs["run_id"],
                ledger=ledger, output=ledger.slot_directory(identifier), execute=True, call=lambda _: self.fail("Missing data must not call"))
            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(result["annotation"])
            self.assertEqual(result["categories"], {name: "unavailable" for name in v.CONCEPTS})

    def test_malformed_http_bytes_are_archived_without_credentials(self):
        body = b' \n{"bad HTTP JSON":'
        fake = io.BytesIO(body)
        fake.status = 200
        endpoint = ChatEndpoint("http://localhost/v1", api_key="SECRET_TEST_KEY")
        with patch("urllib.request.urlopen", return_value=fake):
            result = v.run_assignment(**self.kwargs, execute=True, call=endpoint, execution_receipt={})
        self.assertEqual(result["status"], "error")
        self.assertEqual((self.output / "response.body").read_bytes(), body)
        self.assertNotIn("SECRET_TEST_KEY", (self.output / "response.http.json").read_text())
        self.assertEqual(v.run_assignment(**self.kwargs)["status"], "error")
        (self.output / "response.body").write_bytes(body + b"changed")
        with self.assertRaises(ValueError):
            v.run_assignment(**self.kwargs)

    def test_missing_completed_evidence_and_changed_response_are_not_reconstructed_on_resume(self):
        v.run_assignment(**self.kwargs, execute=True, execution_receipt={},
                         call=lambda _: response(annotation(self.task["annotation_id"])), call_kind="injected_test")
        request = self.output / "request.json"
        request.unlink()
        with self.assertRaises(ValueError):
            v.run_assignment(**self.kwargs)
        self.assertFalse(request.exists())
        write_json(request, self.prepared["request"])
        write_json(self.output / "response.json", response({"changed": True}))
        with self.assertRaises(ValueError):
            v.run_assignment(**self.kwargs)

    def test_reused_ledger_object_does_not_mask_deleted_durable_consumption(self):
        v.run_assignment(**self.kwargs, execute=True, execution_receipt={}, call_kind="injected_test",
                         call=lambda _: response(annotation(self.task["annotation_id"])))
        self.ledger.path.unlink()
        with self.assertRaises(ValueError):
            v.run_assignment(**self.kwargs)


if __name__ == "__main__":
    unittest.main()
