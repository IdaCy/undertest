"""Constructed saved-source fixtures only; no collector, native or actor calls."""

from copy import deepcopy
import hashlib
import json
import unittest

import signals.behavioral.outcomes.anchor_outcomes as adapter


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(body):
    return hashlib.sha256(body).hexdigest()


class SavedFixture:
    def __init__(self):
        self.blobs = {}
        self.source_pins = {}
        self.members = {}
        for name in ("collector", "frame_builder", *adapter.PACKET_SOURCES):
            path = adapter.PACKET_SOURCES.get(name, name + ".py")
            pin = self.save(path, b"# constructed source identity fixture: " + name.encode(), raw=True, source=True)
            self.source_pins[name] = pin
            if name in adapter.PACKET_SOURCES:
                self.members[path] = pin
        self.catalog, self.facts, self.assignments = [], [], []
        for index in range(12):
            case_id, task_id = f"A{index + 1:02d}", f"task-{index % 3}"
            text = f"Task {index}: naïve λ evidence; optional ordinary note."
            quote = "naïve λ evidence"
            a, b = text.index(quote), text.index(quote) + len(quote)
            messages = [{"role": "system", "content": "Complete the task."},
                        {"role": "user", "content": text}]
            actor_input = {"messages": messages, "response_schema": {"type": "object"}}
            path = f"actor_inputs/{case_id}.json"
            pin = self.save(path, actor_input, source=True, pretty=True)
            self.members[path] = pin
            prefix_hash = sha(wire(messages))
            self.catalog.append({"case_id": case_id, "task_id": task_id,
                                 "family": f"family-{index % 3}", "structure": f"structure-{index % 3}",
                                 "dependency_group": "one_constructed_programme",
                                 "input_path": path, "input_bytes": pin["bytes"],
                                 "input_file_sha256": pin["sha256"], "initial_prefix_sha256": prefix_hash,
                                 "split": "exposed_constructed_development", "confirmation_eligible": False})
            self.facts.append({"case_id": case_id, "task_id": task_id,
                               "initial_prefix_sha256": prefix_hash,
                               "actor_input_sha256": sha(wire(actor_input)),
                               "evidence": [{"evidence_id": case_id + "-context", "source": path,
                                             "source_sha256": pin["sha256"], "message_index": 1,
                                             "role": "user", "channel": "public_text", "quote": quote,
                                             "start_codepoint": a, "end_codepoint": b,
                                             "start_utf8_byte": len(text[:a].encode()),
                                             "end_utf8_byte": len(text[:b].encode()),
                                             "delivery_status": "proposed_unrendered", "actor_access_observed": False}],
                               "planned_current_interaction_facts": {"evaluation_status": {"value": True,
                                   "source_evidence_ids": ["plan-purpose", case_id + "-context"],
                                   "fact_status": "authored_plan_fact", "latent_awareness_key": None}},
                               "represented_scenario_facts": {"training_lifecycle": {"value": True,
                                   "frame": "represented_scenario", "latent_awareness_key": None}},
                               "visible_contract_notices": ["absence of a promise is narrow"],
                               "actual_current_research_ref": "researcher/current_research_facts.json"})
            parent = "original-" + case_id
            self.assignments.append({"assignment_id": parent, "case_id": case_id, "kind": "original",
                                     "parent_assignment_id": None, "messages": messages,
                                     "seed": index * 9, "task_id": task_id, "scorer_case_index": index,
                                     "input_pin": pin, "initial_prefix_sha256": prefix_hash})
            for offset, typ in enumerate(adapter.TYPES, 1):
                question = "Question about " + typ
                self.assignments.append({"assignment_id": parent + "--" + typ, "case_id": case_id,
                                         "kind": "ask", "parent_assignment_id": parent,
                                         "question": question, "question_sha256": sha(question.encode()),
                                         "question_type": typ, "seed": index * 9 + offset})
        self.current = {"original_model_episodes": 0, "actor_access_to_researcher_keys": False,
                        "human_readings": 0, "constructed_fixture": True}
        self.dictionary = [{"evidence_id": "plan-purpose", "actor_visible": False,
                            "claim": "An authored plan is not an empirical fact."},
                           {"evidence_id": "outer-interface", "actor_visible": True,
                            "selector": "messages[1] and response_schema"}]
        self.frame = {"schema": "sigres2-anchor-literal-frame-v1", "originals": 12,
                      "independent_forks": 96, "rows": self.assignments,
                      "status": "SOURCE_ONLY_UNISSUED", "confirmation_eligible": False}
        self.terminal = {"frame_sha256": None, "rows": [self.empty(a) for a in self.assignments],
                         "status": "source_collection_halted", "halt_reason": "constructed_fixture",
                         "human_readings": 0, "awareness_labels": 0, "confirmation_eligible": False}
        self.refresh_sources()

    def save(self, name, value, *, source=False, raw=False, pretty=False):
        body = value if raw else ((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
                                  if pretty else wire(value))
        self.blobs[name] = body
        return {"path" if source else "name": name, "bytes": len(body), "sha256": sha(body)}

    def read(self, pin):
        return self.blobs[pin.get("path", pin.get("name"))]

    def refresh_sources(self):
        for name, value in (("case_catalog", self.catalog), ("case_fact_keys", self.facts),
                            ("current_research_facts", self.current), ("evidence_dictionary", self.dictionary)):
            path = "researcher/" + name + ".json"
            self.members[path] = self.save(path, value, source=True, pretty=True)
        self.manifest_pin = self.save("packet_manifest.json", {"files": list(self.members.values())}, source=True)
        self.frame["source_manifest_sha256"] = self.manifest_pin["sha256"]
        self.frame_pin = self.save("frame.json", self.frame, source=True)
        self.terminal["frame_sha256"] = sha(wire(self.frame))

    @staticmethod
    def empty(assignment):
        return {**{k: assignment[k] for k in ("assignment_id", "case_id", "kind", "parent_assignment_id")},
                "status": "unstarted", "reason": "not_reached", "generation_invoked": False,
                "count_invoked": False, "raw_response": None, "native_prefix": None,
                "artifact_score": None, "channels": None, "channel_values": {}, "usage": None}

    def original(self, index=0, *, correct=True, invalid_note=False, note="ordinary explanation"):
        assignment = self.assignments[index * 9]
        row = self.terminal["rows"][index * 9]
        artifact = '{"answer":' + ("1" if correct else "2") + "}"
        content = '{"artifact":' + artifact + ',"work_note":' + ("7" if invalid_note else json.dumps(note)) + "}"
        self.native(row, assignment, assignment["messages"], content)
        message = json.loads(self.read(row["native_message"]))
        prefix = {"cutoff": "original_end_before_any_interview", "assignment_id": assignment["assignment_id"],
                  "case_id": assignment["case_id"], "actor": deepcopy(adapter.ACTOR),
                  "messages": assignment["messages"] + [message], "original_request": row["request"],
                  "original_return": row["raw_response"], "usage": row["usage"], "channels": row["channels"],
                  "observed_ns": 123}
        prefix["messages_sha256"] = sha(wire(prefix["messages"]))
        row["native_prefix"] = self.save(assignment["assignment_id"] + "-prefix.json", prefix)
        score = {"case_id": assignment["case_id"], "status": "invalid_return" if invalid_note else "received",
                 "raw_hex": content.encode().hex(), "raw_bytes": len(content.encode()),
                 "raw_sha256": sha(content.encode()), "work_note": None if invalid_note else note,
                 "task_score": None if invalid_note else {"status": "scored", "correct": correct, "diagnostics": []},
                 "spontaneous_expression": None, "recognition_score": None, "performs_model_call": False,
                 "transport_provenance": "not_assessed_by_pure_scorer"}
        if not invalid_note:
            a, b = content.index(artifact), content.index(artifact) + len(artifact)
            score.update(artifact_text=artifact, artifact_sha256=sha(artifact.encode()),
                         artifact_spans={"artifact_codepoints": [a, b], "artifact_utf8_bytes": [a, b]})
        row["artifact_score"] = self.save(assignment["assignment_id"] + "-score.json", score)
        return row

    def native(self, row, assignment, messages, content):
        ident = assignment["assignment_id"]
        request = {**deepcopy(adapter.GENERATION), "model": adapter.MODEL, "messages": deepcopy(messages),
                   "tools": None, "tool_choice": "none", "parallel_tool_calls": False, "max_tokens": 4096,
                   "stream": False, "n": 1, "seed": assignment["seed"]}
        message = {"role": "assistant", "content": content, "reasoning_content": "A constructed rationale."}
        response = {"object": "chat.completion", "model": adapter.MODEL, "id": "fixture-" + ident,
                    "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        row.update(status="native_completed", reason=None, generation_invoked=True, count_invoked=True,
                   request=self.save(ident + "-request.json", request),
                   raw_response=self.save(ident + "-response.json", response),
                   native_message=self.save(ident + "-message.json", message),
                   observed_native_message=self.save(ident + "-observed.json", message),
                   channels={"complete_native_boundary_admitted": True}, usage={"input": 1, "output": 1})
        self.channels(row)

    def channels(self, row):
        message = json.loads(self.read(row["observed_native_message"]))
        for field in ("content", "reasoning", "reasoning_content"):
            record = {"field": field, "present": field in message,
                      "json_type": type(message[field]).__name__ if field in message else "absent",
                      "source_message": row["observed_native_message"], "source_response": row["raw_response"]}
            if field in message:
                record["value"] = message[field]
            row["channel_values"][field] = self.save(row["assignment_id"] + "-channel-" + field, record)

    def ask(self, parent_index=0, type_index=0):
        index = parent_index * 9 + type_index + 1
        assignment, row = self.assignments[index], self.terminal["rows"][index]
        parent = self.terminal["rows"][parent_index * 9]
        prefix = json.loads(self.read(parent["native_prefix"]))
        self.native(row, assignment, prefix["messages"] + [{"role": "user", "content": assignment["question"]}],
                    "A constructed diagnostic answer.")
        row["native_prefix"] = deepcopy(parent["native_prefix"])
        return row

    def replace(self, row, field, transform):
        old = row[field]
        obj = json.loads(self.read(old))
        transform(obj)
        row[field] = self.save(old.get("name", old.get("path")), obj, source="path" in old)

    def run(self, *, terminal_missing=False):
        pin = None if terminal_missing else self.save("terminal.json", self.terminal)
        return adapter.join_anchor_saved_outcomes(frame_pin=self.frame_pin, terminal_pin=pin,
            packet_manifest_pin=self.manifest_pin, source_pins=self.source_pins, read_exact=self.read)


class AnchorOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.f = SavedFixture()

    def test_fixed_population_and_exact_three_input_hashes(self):
        self.f.original()
        self.f.ask()
        result = self.f.run()
        self.assertEqual(len(result["rows"]), 108)
        self.assertEqual(result["counts"]["assigned_originals"], 12)
        self.assertEqual(result["counts"]["underlying_task_contexts"], 3)
        self.assertIsNone(result["counts"]["independent_clusters"])
        first, ask = result["rows"][:2]
        self.assertEqual(first["task_outcome"]["task_score"]["correct"], True)
        self.assertEqual(ask["original_prefix_join"], "exact_saved_original_prefix")
        self.assertEqual(ask["task_outcome"]["status"], "not_applicable_ASK")
        key, cat = self.f.facts[0], self.f.catalog[0]
        self.assertEqual(len({cat["input_file_sha256"], key["actor_input_sha256"], key["initial_prefix_sha256"]}), 3)
        self.assertEqual(result["rows"][0]["diagnostics"], [])

    def test_authored_facts_remain_literal_and_request_evidence_is_narrow(self):
        self.f.original()
        result = self.f.run()
        self.assertEqual(result["authored_case_fact_keys"]["A01"], self.f.facts[0])
        self.assertEqual(result["authored_current_research_facts"], self.f.current)
        self.assertEqual(result["authored_evidence_dictionary"], self.f.dictionary)
        delivery = result["rows"][0]["fact_delivery"][0]
        self.assertFalse(delivery["source_evidence"]["actor_access_observed"])
        self.assertEqual(delivery["client_request_span"], "present_in_exact_saved_client_request")
        for key in ("server_render_verified", "actor_access_verified", "fact_truth_verified"):
            self.assertIsNone(delivery[key])
        self.assertTrue(result["analyst_only_not_reader_input"])
        self.assertNotIn("reader_input", result)
        for key in ("recognition_scores", "latent_awareness", "Study2_qualified_parents"):
            self.assertIsNone(result[key])

    def test_invalid_note_score_does_not_block_ASK_parent(self):
        self.f.original(invalid_note=True)
        self.f.ask()
        result = self.f.run()
        self.assertIsNone(result["rows"][0]["task_outcome"]["task_score"])
        self.assertEqual(result["rows"][0]["task_outcome"]["status"], "exact_saved_score_join")
        self.assertEqual(result["rows"][1]["native_message_join"], "exact_saved_native_message")
        self.assertEqual(result["rows"][1]["original_prefix_join"], "exact_saved_original_prefix")

    def test_missing_score_and_callback_failure_preserve_ASK(self):
        for mode in ("missing", "unavailable"):
            with self.subTest(mode=mode):
                self.f = SavedFixture()
                row = self.f.original()
                row["artifact_score"] = None if mode == "missing" else self.f.save(
                    "unavailable-score.json", {"status": "unavailable", "reason": "ValueError", "task_score": None})
                self.f.ask()
                result = self.f.run()
                self.assertIsNone(result["rows"][0]["task_outcome"]["task_score"])
                self.assertEqual(result["rows"][1]["original_prefix_join"], "exact_saved_original_prefix")

    def test_valid_wrong_artifact_is_not_missing_or_unaware(self):
        self.f.original(correct=False)
        result = self.f.run()
        score = result["rows"][0]["task_outcome"]["task_score"]
        self.assertEqual(score, {"status": "scored", "correct": False, "diagnostics": []})
        self.assertIsNone(result["rows"][0]["recognition_outcome"])

    def test_received_envelope_with_invalid_artifact_stays_null_not_false(self):
        row = self.f.original()
        invalid = {"status": "invalid_result", "correct": None, "diagnostics": ["artifact_schema"]}
        self.f.replace(row, "artifact_score", lambda s: s.update(task_score=invalid))
        outcome = self.f.run()["rows"][0]["task_outcome"]
        self.assertEqual(outcome["status"], "exact_saved_score_join")
        self.assertEqual(outcome["task_score"], invalid)

    def test_distinct_valid_notes_do_not_change_task_endpoint(self):
        results = []
        for note in ("Nothing about awareness.", "An explicit awareness claim."):
            fixture = SavedFixture()
            fixture.original(note=note)
            results.append(fixture.run()["rows"][0]["task_outcome"]["task_score"])
        self.assertEqual(*results)

    def test_missing_terminal_assignments_are_unknown_not_invented_unstarted(self):
        self.f.original()
        self.f.terminal["rows"] = self.f.terminal["rows"][:1]
        result = self.f.run()
        self.assertEqual(len(result["rows"]), 108)
        self.assertEqual(result["counts"]["missing_terminal_assignments"], 107)
        self.assertIsNone(result["rows"][1]["source_status"])
        self.assertEqual(result["rows"][1]["outcome_presence"], "missing_terminal_assignment")
        self.assertEqual(self.f.run(terminal_missing=True)["counts"]["missing_terminal_assignments"], 108)

    def test_supplied_null_or_nonobject_terminal_is_invalid_not_absent(self):
        for value in (None, False, 0, "terminal", [], {}):
            with self.subTest(terminal=value):
                self.f.terminal = value
                with self.assertRaisesRegex(adapter.AnchorOutcomeError, "terminal_frame_or_shape"):
                    self.f.run()

    def test_duplicate_foreign_and_wrong_case_terminal_fail_unambiguously(self):
        for mutation in ("duplicate", "foreign", "wrong_case"):
            with self.subTest(mutation=mutation):
                self.f = SavedFixture()
                if mutation == "duplicate":
                    self.f.terminal["rows"].append(deepcopy(self.f.terminal["rows"][0]))
                elif mutation == "foreign":
                    self.f.terminal["rows"][0]["assignment_id"] = "foreign"
                else:
                    self.f.terminal["rows"][0]["case_id"] = "A02"
                with self.assertRaises(adapter.AnchorOutcomeError):
                    self.f.run()

    def test_bad_frame_parent_and_canonical_input_identity_fail(self):
        for mutation in ("duplicate", "parent", "actor_hash", "scorer_case"):
            with self.subTest(mutation=mutation):
                self.f = SavedFixture()
                if mutation == "duplicate":
                    self.f.assignments[1]["assignment_id"] = self.f.assignments[0]["assignment_id"]
                elif mutation == "parent":
                    self.f.assignments[1]["parent_assignment_id"] = self.f.assignments[9]["assignment_id"]
                elif mutation == "actor_hash":
                    self.f.facts[0]["actor_input_sha256"] = self.f.catalog[0]["input_file_sha256"]
                else:
                    self.f.assignments[0]["scorer_case_index"] = 1
                self.f.refresh_sources()
                with self.assertRaises(adapter.AnchorOutcomeError):
                    self.f.run()

    def test_swapped_sibling_requests_and_foreign_parent_prefix_are_diagnostics(self):
        self.f.original()
        ask1, ask2 = self.f.ask(type_index=0), self.f.ask(type_index=1)
        ask1["request"], ask2["request"] = ask2["request"], ask1["request"]
        result = self.f.run()
        self.assertEqual(result["rows"][1]["client_request_join"], "invalid")
        self.assertEqual(result["rows"][2]["client_request_join"], "invalid")
        self.f = SavedFixture()
        self.f.original()
        other = self.f.original(1)
        ask = self.f.ask()
        ask["native_prefix"] = other["native_prefix"]
        result = self.f.run()
        self.assertEqual(result["rows"][1]["original_prefix_join"], "invalid")

    def test_missing_parent_and_sibling_answer_cannot_create_own_history(self):
        self.f.original()
        ask = self.f.ask()
        self.f.replace(ask, "request", lambda r: r["messages"].insert(-1,
                           {"role": "assistant", "content": "Sibling ASK answer."}))
        self.assertEqual(self.f.run()["rows"][1]["client_request_join"], "invalid")
        self.f.terminal["rows"][0] = self.f.empty(self.f.assignments[0])
        result = self.f.run()
        self.assertEqual(result["rows"][1]["original_prefix_join"], "invalid")

    def test_score_foreign_case_and_response_hash_substitution_are_not_scores(self):
        for mutation in ("case", "response_hash", "span"):
            with self.subTest(mutation=mutation):
                self.f = SavedFixture()
                row = self.f.original()
                if mutation == "case":
                    change = lambda s: s.update(case_id="A02")
                elif mutation == "response_hash":
                    change = lambda s: s.update(raw_sha256=row["raw_response"]["sha256"])
                else:
                    change = lambda s: s["artifact_spans"].update(artifact_utf8_bytes=[0, 1])
                self.f.replace(row, "artifact_score", change)
                outcome = self.f.run()["rows"][0]["task_outcome"]
                self.assertEqual(outcome["status"], "invalid_saved_score_evidence")
                self.assertIsNone(outcome["task_score"])
                self.assertIsNotNone(outcome["score_record"])

    def test_corrupt_pinned_bytes_and_malformed_raw_JSON_stay_distinct(self):
        row = self.f.original()
        self.f.blobs[row["raw_response"]["name"]] = b"tampered"
        result = self.f.run()
        self.assertTrue(any(d["reason"] == "saved_bytes_pin_mismatch" for d in result["rows"][0]["diagnostics"]))
        self.f = SavedFixture()
        row = self.f.original()
        row["raw_response"] = self.f.save("malformed.bin", b"{partial", raw=True)
        result = self.f.run()
        self.assertEqual(result["rows"][0]["evidence"]["raw_response"]["status"], "bytes_verified")
        self.assertIsNone(result["rows"][0]["task_outcome"]["task_score"])

    def test_null_score_record_and_non_UTF8_scalar_stay_invalid_not_missing(self):
        row = self.f.original()
        row["artifact_score"] = self.f.save("null-score", None)
        saved = self.f.run()["rows"][0]
        self.assertEqual(saved["evidence"]["artifact_score"]["status"], "invalid_json_record")
        self.assertIn("invalid_record", saved["evidence"]["artifact_score"])
        self.assertIsNone(saved["task_outcome"]["task_score"])
        row["raw_response"] = self.f.save("bad-unicode", b'{"content":"\\ud800"}', raw=True)
        saved = self.f.run()["rows"][0]
        self.assertTrue(any(d["reason"] == "invalid_json:UnicodeEncodeError" for d in saved["diagnostics"]))

    def test_truncated_observed_text_is_retained_without_native_or_score_admission(self):
        row = self.f.original()
        self.f.replace(row, "raw_response", lambda r: r["choices"][0].update(finish_reason="length"))
        row.update(status="generation_failed", reason="native_usage_count_or_limit_mismatch",
                   native_message=None, native_prefix=None, artifact_score=None)
        self.f.channels(row)
        result = self.f.run()
        saved = result["rows"][0]
        self.assertEqual(saved["source_outcome"], row)
        self.assertEqual(saved["evidence"]["observed_native_message"]["status"], "json_verified")
        self.assertEqual(saved["native_message_join"], "unavailable")
        self.assertIsNone(saved["task_outcome"]["task_score"])
        self.assertEqual(saved["observed_channels"]["content"]["join"], "exact_saved_observed_channel")
        self.assertTrue(saved["observed_channels"]["content"]["record"]["value"])

    def test_channel_cross_response_and_corrupt_pin_are_not_observed_joins(self):
        row = self.f.original()
        other = self.f.original(1)
        row["channel_values"]["content"] = other["channel_values"]["content"]
        result = self.f.run()["rows"][0]
        self.assertEqual(result["observed_channels"]["content"]["join"], "invalid")
        row["channel_values"]["content"] = {"name": "missing", "bytes": 0, "sha256": sha(b"")}
        result = self.f.run()["rows"][0]
        self.assertEqual(result["observed_channels"]["content"]["join"], "unavailable")
        self.assertTrue(any(d["reason"] == "unavailable_saved_bytes:KeyError" for d in result["diagnostics"]))

    def test_saved_request_does_not_invent_invocation_or_render(self):
        row = self.f.original()
        row.update(status="unstarted", generation_invoked=False, count_invoked=False,
                   native_message=None, native_prefix=None, raw_response=None,
                   observed_native_message=None, channel_values={}, artifact_score=None)
        result = self.f.run()
        self.assertEqual(result["counts"]["recorded_generation_invoked"], 0)
        self.assertEqual(result["counts"]["recorded_count_invoked"], 0)
        self.assertEqual(result["rows"][0]["client_request_join"], "exact_saved_client_request")
        self.assertIsNone(result["rows"][0]["fact_delivery"][0]["actor_access_verified"])

    def test_request_must_match_producer_serialization_not_just_JSON_equality(self):
        for mode in ("pretty", "boolean_seed"):
            with self.subTest(mode=mode):
                self.f = SavedFixture()
                row = self.f.original()
                request = json.loads(self.f.read(row["request"]))
                if mode == "boolean_seed":
                    request["seed"] = False  # False == 0 in Python, different wire.
                row["request"] = self.f.save("altered-request", request, pretty=mode == "pretty")
                result = self.f.run()["rows"][0]
                self.assertEqual(result["client_request_join"], "invalid")
                self.assertIsNone(result["task_outcome"]["task_score"])

    def test_empty_content_is_a_recorded_native_stage_not_requalified(self):
        row = self.f.original()
        self.f.replace(row, "raw_response", lambda r: r["choices"][0]["message"].update(content=""))
        for field in ("native_message", "observed_native_message"):
            self.f.replace(row, field, lambda m: m.update(content=""))
        message = json.loads(self.f.read(row["native_message"]))
        def update_prefix(prefix):
            prefix["messages"][-1] = message
            prefix["messages_sha256"] = sha(wire(prefix["messages"]))
            prefix["original_return"] = row["raw_response"]
        self.f.replace(row, "native_prefix", update_prefix)
        row["artifact_score"] = None
        self.f.channels(row)
        result = self.f.run()
        self.assertEqual(result["rows"][0]["native_message_join"], "exact_saved_native_message")
        self.assertFalse(result["recorded_native_stage_verified_here"])
        self.assertEqual(result["rows"][0]["observed_channels"]["content"]["record"]["value"], "")

    def test_missing_public_content_score_is_preserved_without_a_negative(self):
        row = self.f.original()
        self.f.replace(row, "raw_response", lambda r: r["choices"][0]["message"].update(content=None))
        self.f.replace(row, "native_message", lambda m: m.update(content=None))
        row["artifact_score"] = self.f.save("missing-public-score", {
            "case_id": "A01", "status": "missing_return", "raw_hex": None,
            "raw_bytes": None, "raw_sha256": None, "task_score": None})
        outcome = self.f.run()["rows"][0]["task_outcome"]
        self.assertEqual(outcome["status"], "saved_missing_public_content_not_content_joined")
        self.assertIsNone(outcome["task_score"])
        self.assertEqual(outcome["score_record"]["status"], "missing_return")

    def test_unicode_span_byte_offsets_and_fact_case_are_checked(self):
        self.f.original()
        e = self.f.facts[0]["evidence"][0]
        self.assertNotEqual(e["end_codepoint"], e["end_utf8_byte"])
        e["end_utf8_byte"] = e["end_codepoint"]
        self.f.refresh_sources()
        result = self.f.run()
        self.assertEqual(result["rows"][0]["fact_delivery"][0]["client_request_span"], "invalid_evidence")
        self.f = SavedFixture()
        self.f.original()
        self.f.facts[0]["evidence"][0]["source"] = "actor_inputs/A02.json"
        self.f.refresh_sources()
        self.assertEqual(self.f.run()["rows"][0]["fact_delivery"][0]["client_request_span"], "invalid_evidence")

    def test_all_supplied_dependency_pins_are_checked_inside_adapter(self):
        for name in ("collector", "panel", "frame_builder"):
            with self.subTest(name=name):
                self.f = SavedFixture()
                self.f.blobs[self.f.source_pins[name]["path"]] += b"changed"
                with self.assertRaises(adapter.AnchorOutcomeError):
                    self.f.run()

    def test_native_message_alias_and_original_prefix_swaps_are_invalid(self):
        self.f.original()
        other = self.f.original(1)
        self.f.terminal["rows"][0]["native_prefix"] = other["native_prefix"]
        self.assertEqual(self.f.run()["rows"][0]["original_prefix_join"], "invalid")
        self.f = SavedFixture()
        row = self.f.original()
        self.f.replace(row, "native_message", lambda m: m.update(reasoning="Conflicting reasoning."))
        self.assertEqual(self.f.run()["rows"][0]["native_message_join"], "invalid")

    def test_input_objects_are_not_mutated_and_no_source_key_enters_request(self):
        self.f.original()
        before = deepcopy((self.f.frame, self.f.terminal, self.f.facts))
        result = self.f.run()
        self.assertEqual(before, (self.f.frame, self.f.terminal, self.f.facts))
        request = json.loads(self.f.read(self.f.terminal["rows"][0]["request"]))
        self.assertNotIn("response_schema", request)
        self.assertNotIn("planned_current_interaction_facts", wire(request).decode())
        self.assertFalse(result["source_adoption_verified_here"])


if __name__ == "__main__":
    unittest.main()
