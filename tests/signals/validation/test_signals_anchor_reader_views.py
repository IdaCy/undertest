"""Constructed saved-evidence checks; no actor/reader/native/collector calls."""

from copy import deepcopy
import json
import unittest

import signals.validation.anchor_reader_views as views
from tests.signals.behavioral.outcomes.test_signals_anchor_outcomes import SavedFixture
from tests.signals.behavioral.outcomes.test_signals_anchor_outcomes import sha
from tests.signals.behavioral.outcomes.test_signals_anchor_outcomes import wire


class AnchorReaderViewTests(unittest.TestCase):
    def setUp(self):
        self.f = SavedFixture()

    def supplied(self, channel="public_text", *, missing=False):
        joined = self.f.run(terminal_missing=missing)
        pin = self.f.save("joined-result.json", joined)
        ids = {row["assignment_id"]: "reader-" + sha((channel + row["assignment_id"]).encode())[:32]
               for row in joined["rows"]}
        return pin, ids, joined

    def project(self, channel="public_text", *, missing=False):
        pin, ids, _ = self.supplied(channel, missing=missing)
        return views.project_anchor_reader_views(joined_result_pin=pin, target_channel=channel,
                                                item_ids=ids, read_exact=self.f.read)

    @staticmethod
    def item(result, assignment="original-A01"):
        index = next(r for r in result["private_provenance"]["index"] if r["assignment_id"] == assignment)
        return next(r for r in result["reader_packet"]["items"] if r["item_id"] == index["item_id"]), index

    def observed(self, row, changes=None, *, remove=(), status="unsupported_native_boundary", finish="stop"):
        response = json.loads(self.f.read(row["raw_response"]))
        message = response["choices"][0]["message"]
        for key in remove:
            message.pop(key, None)
        message.update(changes or {})
        response["choices"][0]["finish_reason"] = finish
        row["raw_response"] = self.f.save(row["assignment_id"] + "-changed-response", response)
        row["observed_native_message"] = self.f.save(row["assignment_id"] + "-changed-observed", message)
        row.update(status=status, native_message=None, native_prefix=None, artifact_score=None)
        self.f.channels(row)

    def test_all_108_missing_slots_without_fabricated_messages(self):
        result = self.project(missing=True)
        self.assertEqual(len(result["reader_packet"]["items"]), 108)
        self.assertEqual(result["private_provenance"]["assigned_originals"], 12)
        self.assertEqual(result["private_provenance"]["assigned_ASK"], 96)
        for item in result["reader_packet"]["items"]:
            self.assertEqual(item["messages"], [])
            self.assertIsNone(item["target_message_index"])
            self.assertEqual(item["channel_availability"], "unavailable")
        self.assertTrue(all(r["source_status"] is None for r in result["private_provenance"]["index"]))

    def test_actual_unstarted_failed_and_missing_rows_remain_distinct(self):
        self.f.terminal["rows"][0]["status"] = "generation_failed"
        self.f.terminal["rows"][1]["status"] = "unsupported_parent"
        self.f.terminal["rows"].pop(2)
        result = self.project()
        states = {r["assignment_id"]: r for r in result["private_provenance"]["index"]}
        self.assertEqual(len(states), 108)
        self.assertEqual(states["original-A01"]["source_status"], "generation_failed")
        self.assertEqual(states["original-A01--evaluation_status"]["source_status"], "unsupported_parent")
        self.assertEqual(states["original-A01--evaluation_purpose"]["outcome_presence"], "missing_terminal_assignment")
        self.assertEqual(states["original-A02"]["source_status"], "unstarted")

    def test_complete_public_view_keeps_exact_nested_JSON_and_unicode(self):
        row = self.f.original(note='λ exact "quoted" text; model X reports score 7.\nNext line.')
        expected = json.loads(self.f.read(row["raw_response"]))["choices"][0]["message"]["content"]
        result = self.project()
        item, index = self.item(result)
        self.assertEqual(item["messages"][item["target_message_index"]]["content"], expected)
        self.assertIn('\\u03bb', expected)
        self.assertEqual(item["target_text_utf8_sha256"], sha(expected.encode()))
        self.assertEqual(item["target_message_index"], 2)
        self.assertEqual(item["text_completeness"], "recorded_complete_return")
        self.assertEqual(index["raw_response_field_selectors"], ["choices[0].message.content"])
        self.assertFalse(index["recorded_native_stage_verified_here"])

    def test_unicode_decoded_field_is_not_normalized_or_unwrapped(self):
        row = self.f.original()
        value = '{"artifact": "e\u0301", "work_note": "λ\\n字"}'
        self.observed(row, {"content": value})
        item, _ = self.item(self.project())
        self.assertEqual(item["messages"][-1]["content"], value)
        a, b = value.index("λ"), value.index("λ") + 1
        self.assertEqual(value[a:b], "λ")
        self.assertNotEqual(a, len(value[:a].encode()))
        self.assertEqual(item["target_text_utf8_sha256"], sha(value.encode()))

    def test_reader_whitelist_excludes_keys_scores_paths_and_source_IDs(self):
        self.f.facts[0]["secret_analysis"] = "FACT_SENTINEL"
        self.f.catalog[0]["secret_analysis"] = "CATALOG_SENTINEL"
        self.f.refresh_sources()
        row = self.f.original(note="Literal actor words task_score and model X stay visible.")
        row["secret_analysis"] = "ROW_SENTINEL"
        result = self.project()
        public = wire(result["reader_packet"]).decode()
        for forbidden in ("FACT_SENTINEL", "CATALOG_SENTINEL", "ROW_SENTINEL", "original-A01",
                          "actor_inputs/", "Qwen/Qwen3.8", '"task_id"', '"case_id"', '"task_outcome"'):
            self.assertNotIn(forbidden, public)
        self.assertIn("Literal actor words task_score and model X stay visible.", public)
        self.assertIn("ROW_SENTINEL", wire(result["private_provenance"]).decode())

    def test_ASK_has_own_context_and_only_new_answer_is_designated(self):
        self.f.original(invalid_note=True)
        ask = self.f.ask()
        request = json.loads(self.f.read(ask["request"]))
        result = self.project()
        item, index = self.item(result, "original-A01--evaluation_status")
        self.assertEqual(item["phase"], "elicited_ASK")
        self.assertEqual(item["target_message_index"], 4)
        self.assertEqual(item["messages"][:4], request["messages"])
        self.assertEqual(item["messages"][2]["role"], "assistant")
        self.assertIn('"work_note":7', item["messages"][2]["content"])
        self.assertNotEqual(item["target_message_index"], 2)
        self.assertEqual(item["messages"][4]["content"], "A constructed diagnostic answer.")
        self.assertEqual(index["original_prefix_join"], "exact_saved_original_prefix")

    def test_sibling_and_foreign_requests_do_not_become_reader_context(self):
        self.f.original()
        a, b = self.f.ask(type_index=0), self.f.ask(type_index=1)
        a["request"], b["request"] = b["request"], a["request"]
        item, private = self.item(self.project(), "original-A01--evaluation_status")
        self.assertEqual(item["context_status"], "missing_or_unjoined_request_context")
        self.assertEqual(item["messages"], [])
        self.assertIsNone(item["target_message_index"])
        self.assertTrue(private["diagnostics"])
        self.f = SavedFixture()
        first, other = self.f.original(), self.f.original(1)
        first["request"] = other["request"]
        self.assertIsNone(self.item(self.project())[0]["target_message_index"])

    def test_missing_request_retains_raw_target_privately_without_fake_context(self):
        row = self.f.original()
        row["request"] = None
        item, private = self.item(self.project())
        self.assertEqual(item["messages"], [])
        self.assertIsNone(item["target_message_index"])
        self.assertTrue(private["observed_native_message"]["content"])

    def test_foreign_ASK_prefix_is_unavailable_even_if_request_text_is_own(self):
        self.f.original()
        other = self.f.original(1)
        ask = self.f.ask()
        ask["native_prefix"] = other["native_prefix"]
        item, private = self.item(self.project(), "original-A01--evaluation_status")
        self.assertEqual(item["context_status"], "missing_or_unjoined_original_prefix")
        self.assertEqual(item["messages"], [])
        self.assertEqual(private["original_prefix_join"], "invalid")
        self.assertTrue(private["observed_native_message"]["content"])

    def test_declared_join_flags_and_normalized_text_cannot_replace_raw_joins(self):
        self.f.original()
        for mutation in ("flag", "text", "private_fact"):
            with self.subTest(mutation=mutation):
                pin, ids, supplied = self.supplied()
                if mutation == "flag":
                    supplied["rows"][0]["client_request_join"] = "forged_success"
                elif mutation == "text":
                    supplied["rows"][0]["observed_native_message"]["content"] = "rewritten text"
                else:
                    supplied["authored_case_fact_keys"]["A01"]["case_id"] = "A02"
                pin = self.f.save("tampered-join", supplied)
                with self.assertRaisesRegex(views.AnchorReaderViewError, "does_not_match_recomputed"):
                    views.project_anchor_reader_views(joined_result_pin=pin, target_channel="public_text",
                                                      item_ids=ids, read_exact=self.f.read)

    def test_changed_raw_body_or_join_pin_cannot_be_admitted(self):
        row = self.f.original()
        pin, ids, _ = self.supplied()
        self.f.blobs[row["raw_response"]["name"]] = b"changed"
        with self.assertRaises(views.AnchorReaderViewError):
            views.project_anchor_reader_views(joined_result_pin=pin, target_channel="public_text",
                                              item_ids=ids, read_exact=self.f.read)
        self.f.blobs[pin["name"]] = b"null"
        with self.assertRaises(views.AnchorReaderViewError):
            views.project_anchor_reader_views(joined_result_pin=pin, target_channel="public_text",
                                              item_ids=ids, read_exact=self.f.read)

    def test_map_must_be_complete_unique_and_opaque(self):
        pin, good, _ = self.supplied()
        for mode in ("subset", "duplicate", "source_id"):
            with self.subTest(mode=mode):
                ids = dict(good)
                if mode == "subset":
                    ids.pop(next(iter(ids)))
                elif mode == "duplicate":
                    ids["original-A01"] = ids["original-A02"]
                else:
                    ids["original-A01"] = "original-A01"
                with self.assertRaises(views.AnchorReaderViewError):
                    views.project_anchor_reader_views(joined_result_pin=pin, target_channel="public_text",
                                                      item_ids=ids, read_exact=self.f.read)

    def test_readable_partial_and_unknown_returns_have_separate_completeness(self):
        for finish, expected in (("length", "partial_return"), ("stop", "return_completeness_unknown")):
            with self.subTest(finish=finish):
                self.f = SavedFixture()
                row = self.f.original()
                self.observed(row, status="generation_failed", finish=finish)
                item, private = self.item(self.project())
                self.assertEqual(item["channel_availability"], "available")
                self.assertEqual(item["text_completeness"], expected)
                self.assertEqual(private["source_status"], "generation_failed")
                self.assertEqual(private["native_message_join"], "unavailable")

    def assert_truncation_overrides_recorded_completion(self, row):
        unchanged = {key: deepcopy(row[key]) for key in ("request", "native_message", "observed_native_message")}
        self.f.replace(row, "raw_response", lambda response: response["choices"][0].update(finish_reason="length"))
        self.f.channels(row)
        if row["kind"] == "original":
            self.f.replace(row, "native_prefix", lambda prefix: prefix.update(original_return=row["raw_response"]))
        self.assertEqual(unchanged, {key: row[key] for key in unchanged})
        raw = self.f.read(row["raw_response"])
        response = json.loads(raw)
        self.assertEqual(sha(raw), row["raw_response"]["sha256"])
        self.assertEqual(response["choices"][0]["finish_reason"], "length")
        joined = next(r for r in self.f.run()["rows"] if r["assignment_id"] == row["assignment_id"])
        for key, expected in (("client_request_join", "exact_saved_client_request"),
                              ("native_message_join", "exact_saved_native_message"),
                              ("observed_message_join", "exact_saved_observed_message"),
                              ("original_prefix_join", "exact_saved_original_prefix")):
            self.assertEqual(joined[key], expected)
        self.assertTrue(all(c["join"] == "exact_saved_observed_channel" for c in joined["observed_channels"].values()))
        self.assertEqual(joined["diagnostics"], [])
        for channel, field in (("public_text", "content"), ("raw_reasoning", "reasoning_content")):
            with self.subTest(channel=channel):
                item, private = self.item(self.project(channel), row["assignment_id"])
                self.assertEqual(private["source_status"], "native_completed")
                self.assertEqual(private["native_message_join"], "exact_saved_native_message")
                self.assertFalse(private["recorded_native_stage_verified_here"])
                self.assertEqual(private["evidence"]["raw_response"]["pin"], row["raw_response"])
                self.assertEqual(private["original_prefix_join"], "exact_saved_original_prefix")
                self.assertEqual(private["diagnostics"], [])
                self.assertEqual(item["channel_availability"], "available")
                self.assertEqual(item["target_message_index"], 2 if row["kind"] == "original" else 4)
                self.assertEqual(item["messages"][-1]["content"], response["choices"][0]["message"][field])
                self.assertEqual(item["target_text_utf8_sha256"], sha(item["messages"][-1]["content"].encode()))
                self.assertEqual(item["text_completeness"], "partial_return")

    def test_original_explicit_truncation_overrides_recorded_completed_stage(self):
        self.assert_truncation_overrides_recorded_completion(self.f.original())

    def test_ASK_explicit_truncation_overrides_recorded_completed_stage(self):
        parent = self.f.original(invalid_note=True)
        parent_before = deepcopy(parent)
        ask = self.f.ask()
        self.assert_truncation_overrides_recorded_completion(ask)
        self.assertEqual(parent, parent_before)
        self.assertEqual(json.loads(self.f.read(parent["raw_response"]))["choices"][0]["finish_reason"], "stop")
        item, _ = self.item(self.project(), ask["assignment_id"])
        self.assertEqual(item["messages"][:4], json.loads(self.f.read(ask["request"]))["messages"])
        self.assertEqual(item["messages"][2]["role"], "assistant")

    def test_actual_assistant_role_is_required(self):
        row = self.f.original()
        self.observed(row, {"role": "tool"})
        item, private = self.item(self.project())
        self.assertEqual(item["availability_reason"], "observed_speaker_is_not_assistant")
        self.assertEqual(private["observed_native_message"]["role"], "tool")
        self.assertIsNone(item["target_message_index"])

    def test_unjoined_cached_observation_is_retained_without_raw_substitution(self):
        row = self.f.original()
        self.observed(row)
        row["raw_response"] = self.f.save("partial-raw", b'{"choices":', raw=True)
        self.f.channels(row)
        item, private = self.item(self.project())
        self.assertEqual(item["availability_reason"], "raw_observation_not_joined")
        self.assertIsNone(item["target_message_index"])
        self.assertTrue(private["observed_native_message"]["content"])

    def test_missing_channel_record_is_not_replaced_by_parsed_message(self):
        row = self.f.original()
        del row["channel_values"]["content"]
        item, _ = self.item(self.project())
        self.assertEqual(item["availability_reason"], "selected_channel_record_not_joined")
        self.assertIsNone(item["target_message_index"])

    def test_foreign_channel_record_cannot_supply_a_target(self):
        row = self.f.original()
        other = self.f.original(1)
        row["channel_values"]["content"] = other["channel_values"]["content"]
        item, private = self.item(self.project())
        self.assertEqual(item["availability_reason"], "selected_channel_record_not_joined")
        self.assertIsNone(item["target_message_index"])
        self.assertEqual(private["observed_channels"]["content"]["join"], "invalid")

    def test_public_absent_null_empty_and_wrong_type_stay_distinct(self):
        for state, value in (("absent", None), ("null", None), ("empty", ""), ("wrong_type", [])):
            with self.subTest(state=state):
                self.f = SavedFixture()
                row = self.f.original()
                self.observed(row, {} if state == "absent" else {"content": value},
                              remove=("content",) if state == "absent" else ())
                item, private = self.item(self.project())
                self.assertEqual(private["field_states"]["content"], state)
                if state == "empty":
                    self.assertEqual(item["channel_availability"], "available")
                    self.assertEqual(item["messages"][-1]["content"], "")
                    self.assertEqual(item["target_text_state"], "empty")
                else:
                    self.assertIsNone(item["target_message_index"])

    def test_single_and_equal_reasoning_aliases_produce_one_exact_view(self):
        for mode in ("reasoning_only", "alias_only", "equal"):
            with self.subTest(mode=mode):
                self.f = SavedFixture()
                row = self.f.original()
                changes = {"reasoning": "λ rationale", "reasoning_content": "λ rationale"}
                remove = () if mode == "equal" else (("reasoning_content",) if mode == "reasoning_only" else ("reasoning",))
                for field in remove:
                    changes.pop(field)
                self.observed(row, changes, remove=remove)
                result = self.project("raw_reasoning")
                item, private = self.item(result)
                self.assertEqual(item["messages"][-1]["content"], "λ rationale")
                self.assertEqual(len(item["messages"]), 3)
                self.assertEqual(len(private["target_native_fields"]), 2 if mode == "equal" else 1)
                self.assertEqual(len(result["reader_packet"]["items"]), 108)

    def test_conflicting_or_wrong_type_alias_is_unresolved_without_losing_public(self):
        for value in ("different", ["not text"]):
            with self.subTest(value=value):
                self.f = SavedFixture()
                row = self.f.original()
                self.observed(row, {"reasoning": value})
                item, private = self.item(self.project("raw_reasoning"))
                self.assertEqual(item["channel_availability"], "unresolved")
                self.assertIsNone(item["target_message_index"])
                self.assertEqual(private["observed_native_message"]["reasoning"], value)
                self.assertEqual(self.item(self.project())[0]["channel_availability"], "available")

    def test_null_and_empty_reasoning_aliases_are_not_collapsed(self):
        for first, second, expected in ((None, None, "unavailable"), (None, "", "available"),
                                        ("", "", "available"), ("", "nonempty", "unresolved")):
            with self.subTest(first=first, second=second):
                self.f = SavedFixture()
                row = self.f.original()
                self.observed(row, {"reasoning": first, "reasoning_content": second})
                item, private = self.item(self.project("raw_reasoning"))
                self.assertEqual(item["channel_availability"], expected)
                self.assertEqual(private["field_states"]["reasoning"], "null" if first is None else "empty")
                if expected == "available":
                    self.assertEqual(item["messages"][-1]["content"], "")
                    self.assertEqual(item["target_text_state"], "empty")

    def test_no_provider_summary_is_invented_from_work_note_or_extra_field(self):
        row = self.f.original(note="This is an explanation, not a provider summary.")
        self.observed(row, {"reasoning_summary": "Uncontracted extra field."})
        item, private = self.item(self.project("reasoning_summary"))
        self.assertEqual(item["availability_reason"], "no_authenticated_provider_summary")
        self.assertIsNone(item["target_message_index"])
        self.assertEqual(private["observed_native_message"]["reasoning_summary"], "Uncontracted extra field.")

    def test_resolver_and_identity_inputs_are_not_mutated(self):
        self.f.original()
        pin, ids, _ = self.supplied()
        before = deepcopy((self.f.blobs, pin, ids))
        result = views.project_anchor_reader_views(joined_result_pin=pin, target_channel="public_text",
                                                   item_ids=ids, read_exact=self.f.read)
        self.assertEqual(before, (self.f.blobs, pin, ids))
        self.assertEqual(result["private_provenance"]["reader_dispatches"], 0)
        self.assertEqual(result["private_provenance"]["semantic_annotations"], 0)


if __name__ == "__main__":
    unittest.main()
