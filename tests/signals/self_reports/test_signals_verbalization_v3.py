import base64
import copy
import hashlib
import json
import os
import stat
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import signals.self_reports.verbalization_v3 as v


ANNOTATION_ID = "host-unit-contract-only"
ASSIGNMENT = {"annotation_id": ANNOTATION_ID, "input_id": "unit", "seed": 123, "request_sha256": "b" * 64}


def source(texts=(("quoted text",),), *, actor_truncated=False):
    messages, source_map = [], []
    counts = {"readable_parts": 0, "nonempty_parts": 0, "empty_parts": 0, "unavailable_parts": 0}
    for message_index, texts_in_message in enumerate(texts):
        parts = []
        missing = 0
        for part_index, text in enumerate(texts_in_message):
            readable = isinstance(text, str)
            source_map.append({"message_index": message_index, "part_index": part_index,
                               "generated_message_index": message_index * 2, "field": "reasoning",
                               "content_block_index": None, "readable": readable})
            counts["readable_parts" if readable else "unavailable_parts"] += 1
            if readable:
                parts.append({"part_index": part_index, "text": text})
                counts["nonempty_parts" if text.strip() else "empty_parts"] += 1
            else:
                missing += 1
        messages.append({"message_index": message_index, "parts": parts, "unavailable_parts": missing})
    status = "readable" if counts["nonempty_parts"] else "empty" if counts["readable_parts"] else "unavailable"
    projection = {"record": {"initial_context": [], "tools": [], "reasoning_messages": messages,
                             "coverage": {**counts, "status": status, "assistant_messages": len(messages),
                                          "partially_unavailable": bool(counts["readable_parts"] and counts["unavailable_parts"]) }},
                  "source_map": source_map, "cutoff": 2 * len(messages), "cutoff_name": "retrospective"}
    return v.bind_source(projection, source_identity={"input_sha256": "a" * 64}, actor_truncated=actor_truncated)


def quote(text="quoted text", *, message=0, part=0, occurrence=None):
    return {"message_index": message, "part_index": part, "quote": text, "occurrence": occurrence}


def witness(indices=(0,), *, polarity="affirmed", temporality="current_inference"):
    return {"polarity": polarity, "temporality": temporality, "quotes": list(indices)}


def annotation():
    return {"annotation_id": ANNOTATION_ID, "quotes": [],
            "concepts": {name: {"status": "complete", "relevant": [], "excluded": None} for name in v.CONCEPTS}}


def relevant_annotation(quotes=None, witnesses=None, *, concept=v.CONCEPTS[0]):
    value = annotation()
    value["quotes"] = [quote()] if quotes is None else quotes
    value["concepts"][concept]["relevant"] = [witness()] if witnesses is None else witnesses
    return value


def secondary_annotation():
    value = annotation()
    value["quotes"] = [quote()]
    value["concepts"][v.CONCEPTS[0]]["excluded"] = {"reason": "quoted_material", "quotes": [0]}
    return value


def raw(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def codes(items):
    return {item["code"] for item in items}


class V3HostContractTests(unittest.TestCase):
    def parse(self, value=None, given_source=None):
        return v.parse_annotation(raw(annotation() if value is None else value), source() if given_source is None else given_source, ASSIGNMENT)

    def test_all_sixteen_candidate_v2_clarification_cases(self):
        cases = {"valid_secondary": (secondary_annotation(), True, True, "pending")}
        for name in ("nonmatching_secondary", "out_of_range_secondary", "negative_secondary", "missing_occurrence_secondary",
                     "wrong_type_secondary", "unused_wrong_type_quote", "missing_top_level_quotes", "unknown_top_level_key",
                     "missing_concept", "invalid_secondary_reason_enum", "schema_valid_unused_nonmatching_quote",
                     "negative_primary_index", "nonnegative_out_of_range_primary_index"):
            value = secondary_annotation()
            schema, structural, whole = False, False, False
            if name == "nonmatching_secondary":
                value["quotes"][0]["quote"] = "absent"
                schema, structural = True, True
            elif name in {"out_of_range_secondary", "negative_secondary"}:
                value["concepts"][v.CONCEPTS[0]]["excluded"]["quotes"] = [999 if name == "out_of_range_secondary" else -1]
                schema = structural = name == "out_of_range_secondary"
            elif name == "missing_occurrence_secondary":
                del value["quotes"][0]["occurrence"]
            elif name in {"wrong_type_secondary", "unused_wrong_type_quote"}:
                value["quotes"][0]["quote"] = 7
                if name == "unused_wrong_type_quote":
                    value["concepts"][v.CONCEPTS[0]]["excluded"] = None
            elif name == "missing_top_level_quotes":
                del value["quotes"]
            elif name == "unknown_top_level_key":
                value["extra"] = "unused"
            elif name == "missing_concept":
                del value["concepts"]["simulation_recognition"]
            elif name == "invalid_secondary_reason_enum":
                value["concepts"][v.CONCEPTS[0]]["excluded"]["reason"] = "unknown_reason"
            elif name == "schema_valid_unused_nonmatching_quote":
                value["quotes"][0]["quote"] = "absent"
                value["concepts"][v.CONCEPTS[0]]["excluded"] = None
                schema, structural = True, True
            elif name in {"negative_primary_index", "nonnegative_out_of_range_primary_index"}:
                value["concepts"][v.CONCEPTS[0]]["relevant"] = [witness([-1 if name == "negative_primary_index" else 999])]
                schema = name == "nonnegative_out_of_range_primary_index"
            cases[name] = (value, schema, structural, whole)
        self.assertEqual(len(cases), 14)
        for name, (value, schema, structural, whole) in cases.items():
            with self.subTest(name=name):
                result = self.parse(value)
                self.assertEqual(result["global_schema_valid"], schema)
                self.assertEqual(result["primary_structural_evaluable"], structural)
                self.assertEqual(result["whole_contract_valid"], whole)
                self.assertEqual(result["primary_fitness_gate"], "pending" if structural else "fail")
                self.assertEqual(result["primary_semantic_valid"], "pending" if structural else "not_evaluated")
                self.assertTrue(all(row["category"] == "unresolved" for row in result["strict_evidence_table"].values()))
        baseline = raw(secondary_annotation())
        for name, data in (("malformed_json", baseline[:-1]), ("duplicate_top_level_key", b'{"annotation_id":"duplicate",' + baseline[1:])):
            with self.subTest(name=name):
                result = v.parse_annotation(data, source(), ASSIGNMENT)
                self.assertFalse(result["strict_json_parse_valid"])
                self.assertIsNone(result["global_schema_valid"])
                self.assertFalse(result["primary_structural_evaluable"])
                self.assertEqual(result["primary_fitness_gate"], "fail")

    def test_schema_rejects_types_enums_cardinalities_everywhere(self):
        changes = [
            lambda x: x.update(annotation_id=""),
            lambda x: x.update(quotes={}),
            lambda x: x.update(concepts=[]),
            lambda x: x["quotes"][0].update(message_index=True),
            lambda x: x["quotes"][0].update(part_index=0.5),
            lambda x: x["quotes"][0].update(occurrence=False),
            lambda x: x["quotes"][0].update(quote=""),
            lambda x: x["quotes"][0].update(offset=0),
            lambda x: x["concepts"][v.CONCEPTS[0]].update(status="incomplete"),
            lambda x: x["concepts"][v.CONCEPTS[0]].update(relevant=[witness()] * 4),
            lambda x: x["concepts"][v.CONCEPTS[0]]["relevant"][0].update(quotes=[]),
            lambda x: x["concepts"][v.CONCEPTS[0]]["relevant"][0].update(quotes=[True]),
            lambda x: x["concepts"][v.CONCEPTS[0]]["relevant"][0].update(polarity="positive"),
            lambda x: x["concepts"][v.CONCEPTS[0]]["relevant"][0].update(temporality="past_report"),
            lambda x: x["concepts"][v.CONCEPTS[0]].update(excluded=[]),
        ]
        for number, change in enumerate(changes):
            with self.subTest(number=number):
                value = relevant_annotation()
                change(value)
                result = self.parse(value)
                self.assertFalse(result["global_schema_valid"])
                self.assertFalse(result["primary_structural_evaluable"])
                self.assertEqual(result["quote_evidence"], [])

    def test_integral_json_numbers_follow_schema_without_rewriting_raw(self):
        value = relevant_annotation([quote(message=0.0, part=0.0)], [witness([0.0])])
        result = self.parse(value)
        self.assertTrue(result["global_schema_valid"])
        self.assertTrue(result["primary_structural_evaluable"])
        self.assertEqual(result["parsed_annotation"]["quotes"][0]["message_index"].token, "0.0")
        self.assertEqual(base64.b64decode(result["raw_annotation"]["base64"]), raw(value))
        exported = v.strict_json(v.projection_json(result))
        self.assertEqual(exported["parsed_annotation"]["quotes"][0]["message_index"].token, "0.0")

    def test_strict_json_rejects_nonfinite_duplicate_nested_keys_and_extra_text(self):
        for data in (b'{"x":NaN}', b'{"x":Infinity}', b'{"x":{"a":1,"a":2}}', raw(annotation()) + b'{}', b'\xff'):
            with self.subTest(data=data):
                result = v.parse_annotation(data, source(), ASSIGNMENT)
                self.assertFalse(result["strict_json_parse_valid"])
                self.assertEqual(base64.b64decode(result["raw_annotation"]["base64"]), data)

    def test_fractional_indices_cannot_become_integers_through_float_rounding(self):
        data = raw(relevant_annotation())
        for token in (b"1e-1000", b"1.00000000000000000000000001", b"-1e-1000"):
            with self.subTest(token=token):
                changed = data.replace(b'"message_index":0', b'"message_index":' + token)
                result = v.parse_annotation(changed, source(), ASSIGNMENT)
                self.assertTrue(result["strict_json_parse_valid"])
                self.assertFalse(result["global_schema_valid"])
                self.assertFalse(result["primary_structural_evaluable"])
                self.assertEqual(base64.b64decode(result["raw_annotation"]["base64"]), changed)

    def test_huge_integral_exponents_are_schema_valid_and_checked_before_conversion(self):
        for token in (b"1e1000", b"1e1000000000", b"9" * 5000):
            with self.subTest(token=token[:32]):
                secondary = raw(secondary_annotation()).replace(b'"quotes":[0]', b'"quotes":[' + token + b']')
                result = v.parse_annotation(secondary, source(), ASSIGNMENT)
                self.assertTrue(result["strict_json_parse_valid"])
                self.assertTrue(result["global_schema_valid"])
                self.assertTrue(result["primary_structural_evaluable"])
                self.assertIn("quotation_index_out_of_range", codes(result["secondary_diagnostics"]))
                self.assertEqual(result["primary_fitness_gate"], "pending")
                self.assertIn(token.decode(), v.projection_json(result))
                primary = raw(relevant_annotation()).replace(b'"quotes":[0]', b'"quotes":[' + token + b']')
                result = v.parse_annotation(primary, source(), ASSIGNMENT)
                self.assertTrue(result["global_schema_valid"])
                self.assertFalse(result["primary_structural_evaluable"])
                unused = raw(annotation()).replace(b'"quotes":[]', b'"quotes":[{"message_index":' + token + b',"part_index":0,"quote":"quoted text","occurrence":null}]', 1)
                result = v.parse_annotation(unused, source(), ASSIGNMENT)
                self.assertTrue(result["global_schema_valid"])
                self.assertTrue(result["primary_structural_evaluable"])
                self.assertIn("unavailable_or_nonexistent_part", codes(result["format_diagnostics"]))

    def test_unrepresentable_numbers_report_unknown_decoding_not_schema_invalidity(self):
        data = raw(secondary_annotation()).replace(b'"quotes":[0]', b'"quotes":[1e999999999999999999999999999999]')
        result = v.parse_annotation(data, source(), ASSIGNMENT)
        self.assertIsNone(result["strict_json_parse_valid"])
        self.assertIsNone(result["global_schema_valid"])
        self.assertEqual(result["completion"]["state"], "unsupported_decoding")
        self.assertIn("annotation_numeric_decode_unsupported", codes(result["errors"]))
        self.assertEqual(base64.b64decode(result["raw_annotation"]["base64"]), data)
        envelope = {"choices": [{"message": {"role": "assistant", "content": data.decode()}, "finish_reason": "stop"}]}
        with tempfile.TemporaryDirectory() as directory:
            result = v.parse_http_response(raw(envelope), source(), ASSIGNMENT, archive_path=Path(directory) / "raw.http")
            self.assertEqual(result["completion"]["state"], "unsupported_decoding")
            self.assertIsNone(result["global_schema_valid"])
            self.assertEqual(result["primary_fitness_gate"], "fail")

    def test_decoder_depth_limit_does_not_claim_invalid_json_or_schema(self):
        given_source = source()
        with patch.object(v.json, "loads", side_effect=RecursionError("decoder depth limit")):
            result = v.parse_annotation(b"[]", given_source, ASSIGNMENT)
        self.assertIsNone(result["strict_json_parse_valid"])
        self.assertIsNone(result["global_schema_valid"])
        self.assertEqual(result["completion"]["state"], "unsupported_decoding")
        self.assertIn("annotation_depth_decode_unsupported", codes(result["errors"]))

    def test_decoded_nested_schema_invalid_value_can_still_be_archived(self):
        data = b"[" * 200 + b"0" + b"]" * 200
        result = v.parse_annotation(data, source(), ASSIGNMENT)
        self.assertTrue(result["strict_json_parse_valid"])
        self.assertFalse(result["global_schema_valid"])
        saved = v.projection_json(result)
        self.assertIn(data.decode(), saved)
        self.assertEqual(base64.b64decode(result["raw_annotation"]["base64"]), data)

    def test_occurrences_unicode_overlaps_and_no_normalization(self):
        given_source = source((("🙂 é aaa\nend",),))
        value = relevant_annotation([quote("aa", occurrence=1)])
        result = self.parse(value, given_source)
        evidence = result["quote_evidence"][0]
        self.assertEqual((evidence["start"], evidence["end"], evidence["selected_occurrence"], evidence["match_count"]), (5, 7, 1, 2))
        self.assertEqual(evidence["content_sha256"], hashlib.sha256("🙂 é aaa\nend".encode()).hexdigest())
        self.assertEqual(evidence["source_location"]["generated_message_index"], 0)
        for evidence_quote in (quote("aa"), quote("aa", occurrence=2), quote("é", occurrence=0), quote("é"), quote("aaa end")):
            with self.subTest(quote=evidence_quote):
                result = self.parse(relevant_annotation([evidence_quote]), given_source)
                self.assertFalse(result["primary_structural_evaluable"])
        unique = self.parse(relevant_annotation([quote("é")]), given_source)["quote_evidence"][0]
        self.assertEqual((unique["selected_occurrence"], unique["raw"]["occurrence"], unique["match_count"]), (0, None, 1))

    def test_bundles_link_full_source_in_location_not_numeric_index_order(self):
        given_source = source((("alpha", "beta"), ("gamma",)))
        value = relevant_annotation([quote("gamma", message=1), quote("alpha"), quote("beta", part=1)], [witness([1, 2, 0])])
        result = self.parse(value, given_source)
        bundle = result["concepts"][v.CONCEPTS[0]]["relevant"][0]
        self.assertTrue(result["primary_structural_evaluable"])
        self.assertEqual(bundle["quote_indices"], [1, 2, 0])
        self.assertEqual([row["raw"]["quote"] for row in bundle["evidence"]], ["alpha", "beta", "gamma"])
        value["concepts"][v.CONCEPTS[0]]["relevant"][0]["quotes"] = [0, 1, 2]
        result = self.parse(value, given_source)
        self.assertIn("quotation_source_order", codes(result["errors"]))

    def test_overlap_equal_start_and_repeated_reference_have_no_invented_failure(self):
        given_source = source((("abcdef",),))
        for quotes, indices in (([quote("abcd"), quote("cdef")], [0, 1]),
                                ([quote("abcd"), quote("ab")], [0, 1]),
                                ([quote("abcd")], [0, 0])):
            with self.subTest(indices=indices, quotes=quotes):
                result = self.parse(relevant_annotation(quotes, [witness(indices)]), given_source)
                self.assertTrue(result["primary_structural_evaluable"])
                self.assertTrue(result["concepts"][v.CONCEPTS[0]]["relevant"][0]["observations"])
        reversed_result = self.parse(relevant_annotation([quote("cdef"), quote("abcd")], [witness([0, 1])]), given_source)
        self.assertFalse(reversed_result["primary_structural_evaluable"])

    def test_duplicate_and_unused_quote_rows_are_format_diagnostics(self):
        value = relevant_annotation([quote(), quote()], [witness([0, 1])])
        result = self.parse(value)
        self.assertTrue(result["primary_structural_evaluable"])
        self.assertIn("duplicate_quotation_entry", codes(result["format_diagnostics"]))
        self.assertFalse(result["whole_contract_valid"])
        value["quotes"].append(quote("absent"))
        result = self.parse(value)
        self.assertIn("unused_quotation", codes(result["format_diagnostics"]))
        self.assertIn("literal_not_found", codes(result["format_diagnostics"]))
        self.assertTrue(result["primary_structural_evaluable"])

    def test_bad_shared_quote_is_primary_even_if_used_as_secondary(self):
        value = relevant_annotation([quote("absent")])
        value["concepts"][v.CONCEPTS[1]]["excluded"] = {"reason": "quoted_material", "quotes": [0]}
        result = self.parse(value)
        self.assertFalse(result["primary_structural_evaluable"])
        self.assertEqual({use["kind"] for use in result["quote_evidence"][0]["uses"]}, {"primary", "secondary"})
        self.assertIn("literal_not_found", codes(result["errors"]))

    def test_temporal_polarity_and_identity_dynamic_failures(self):
        variants = [relevant_annotation(witnesses=[witness(), witness()]),
                    relevant_annotation(witnesses=[witness(polarity="denied"), witness()]),
                    relevant_annotation(concept="prohibited_shortcut_plan")]
        wrong_id = relevant_annotation()
        wrong_id["annotation_id"] = "wrong"
        variants.append(wrong_id)
        variants.append(relevant_annotation([quote(message=999)]))
        for value in variants:
            with self.subTest(value=value):
                result = self.parse(value)
                self.assertTrue(result["global_schema_valid"])
                self.assertFalse(result["primary_structural_evaluable"])
                self.assertEqual(result["primary_semantic_valid"], "not_evaluated")
                self.assertEqual(result["concepts"][v.CONCEPTS[0]]["category"], "unresolved")

    def test_raw_categories_retain_all_polarities_without_semantic_acceptance(self):
        for polarities, expected in (([], "no_relevant_statement"), (["affirmed"], "asserted"), (["denied"], "denied"),
                                     (["uncertain"], "uncertain"), (["affirmed", "denied"], "mixed"),
                                     (["affirmed", "denied", "uncertain"], "mixed")):
            with self.subTest(polarities=polarities):
                value = relevant_annotation(quotes=[quote()] if polarities else [], witnesses=[witness(polarity=p) for p in polarities])
                result = self.parse(value)
                self.assertEqual(result["concepts"][v.CONCEPTS[0]]["category"], expected)
                row = result["strict_evidence_table"][v.CONCEPTS[0]]
                self.assertEqual(row["category"], "unresolved")
                self.assertEqual(result["primary_fitness_gate"], "pending")
                if not polarities:
                    self.assertIn("explicit_full_source_absence_review", row["required_primary_review_checks"])

    def test_ambiguous_and_unable_keep_definite_evidence_without_absence(self):
        for status in ("referent_ambiguous", "coder_unable"):
            for with_evidence in (False, True):
                with self.subTest(status=status, with_evidence=with_evidence):
                    value = relevant_annotation() if with_evidence else annotation()
                    value["concepts"][v.CONCEPTS[0]]["status"] = status
                    result = self.parse(value)
                    row = result["concepts"][v.CONCEPTS[0]]
                    self.assertEqual(row["category"], "unresolved")
                    self.assertEqual(row["observed_polarities"], ["affirmed"] if with_evidence else [])
                    self.assertEqual(result["primary_fitness_gate"], "pending")
                    self.assertIn(status, result["strict_evidence_table"][v.CONCEPTS[0]]["reason_codes"])

    def test_unresolved_on_host_designated_unambiguous_screen_fails_fitness(self):
        for status in ("referent_ambiguous", "coder_unable"):
            value = relevant_annotation()
            value["concepts"][v.CONCEPTS[0]]["status"] = status
            result = v.parse_annotation(raw(value), source(), ASSIGNMENT, unambiguous_screen_item=True)
            self.assertTrue(result["primary_structural_evaluable"])
            self.assertEqual(result["primary_semantic_valid"], "unresolved")
            self.assertEqual(result["primary_fitness_gate"], "fail")
            self.assertEqual(result["concepts"][v.CONCEPTS[0]]["observed_polarities"], ["affirmed"])

    def test_secondary_source_order_error_does_not_block_primary_review(self):
        value = secondary_annotation()
        value["quotes"] = [quote("alpha"), quote("beta")]
        value["concepts"][v.CONCEPTS[0]]["excluded"]["quotes"] = [1, 0]
        result = self.parse(value, source((("alpha beta",),)))
        self.assertTrue(result["primary_structural_evaluable"])
        self.assertIn("quotation_source_order", codes(result["secondary_diagnostics"]))
        self.assertFalse(result["whole_contract_valid"])

    def test_ineligible_excluded_witness_is_secondary_and_cannot_imply_absence(self):
        for status, relevant in (("coder_unable", []), ("complete", [witness()])):
            value = secondary_annotation()
            value["concepts"][v.CONCEPTS[0]].update(status=status, relevant=relevant)
            result = self.parse(value)
            self.assertTrue(result["primary_structural_evaluable"])
            self.assertIn("ineligible_excluded_witness", codes(result["secondary_diagnostics"]))
            self.assertFalse(result["whole_contract_valid"])
            self.assertNotEqual(result["concepts"][v.CONCEPTS[0]]["category"], "no_relevant_statement")

    def test_empty_unavailable_partial_and_actor_truncation_are_distinct(self):
        empty = source((("",),))
        parsed = self.parse(given_source=empty)
        self.assertTrue(parsed["coverage"]["empty_observed_text"])
        self.assertEqual(parsed["concepts"][v.CONCEPTS[0]]["category"], "no_relevant_statement")
        with self.assertRaises(ValueError):
            v.uncalled_assignment(empty, ASSIGNMENT, reason="unavailable")
        absent = source(((None,),))
        result = v.uncalled_assignment(absent, ASSIGNMENT, reason="unavailable")
        self.assertEqual(result["completion"]["state"], "uncalled_unavailable")
        self.assertTrue(all(row["category"] == "unavailable" for row in result["strict_evidence_table"].values()))
        for given_source in (source((("quoted text", None),)), source(actor_truncated=True)):
            result = self.parse(relevant_annotation(), given_source)
            self.assertTrue(result["primary_structural_evaluable"])
            self.assertIn("full_cutoff_coverage_unresolved", result["strict_evidence_table"][v.CONCEPTS[0]]["reason_codes"])
        for reason in ("admission_failure", "annotation_call_failure", "not_dispatched"):
            result = v.uncalled_assignment(source(), ASSIGNMENT, reason=reason)
            self.assertEqual(result["completion"]["state"], "uncalled_" + reason)
            self.assertEqual(result["primary_fitness_gate"], "fail")

    def test_source_binding_rejects_omission_mutation_or_coverage_mismatch(self):
        original = source((("alpha", "beta"),))
        modified = copy.deepcopy(original)
        modified["projection"]["record"]["reasoning_messages"][0]["parts"].pop()
        with self.assertRaises(ValueError):
            self.parse(given_source=modified)
        projection = copy.deepcopy(original["projection"])
        projection["record"]["coverage"]["readable_parts"] = 3
        with self.assertRaises(ValueError):
            v.bind_source(projection, source_identity={"input_id": "unit"})
        projection = copy.deepcopy(original["projection"])
        projection["record"]["reasoning_messages"][0]["parts"].reverse()
        with self.assertRaises(ValueError):
            v.bind_source(projection, source_identity={"input_id": "unit"})

    def test_http_archive_precedes_decoding_and_is_write_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.http"
            data = b'\xffbad http'
            result = v.parse_http_response(data, source(), ASSIGNMENT, archive_path=path)
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(result["raw_http"]["sha256"], hashlib.sha256(data).hexdigest())
            self.assertTrue(result["completion"]["annotation_call_failure"])
            with self.assertRaises(ValueError):
                v.parse_http_response(b'{}', source(), ASSIGNMENT, archive_path=path)

    def test_archive_create_and_reuse_sync_file_then_parent_before_decode(self):
        given_source = source()
        envelope = {"choices": [{"message": {"role": "assistant", "content": raw(annotation()).decode()}, "finish_reason": "stop"}]}
        body = raw(envelope)
        real_fsync, real_decode = os.fsync, v.strict_json
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "raw.http"
                if existing:
                    path.write_bytes(body)
                events = []

                def traced_fsync(fd):
                    kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
                    self.assertEqual(path.read_bytes(), body)
                    events.append(kind + "_fsync")
                    return real_fsync(fd)

                def traced_decode(data):
                    events.append("decode")
                    return real_decode(data)

                with patch.object(v.os, "fsync", traced_fsync), patch.object(v, "strict_json", traced_decode):
                    result = v.parse_http_response(body, given_source, ASSIGNMENT, archive_path=path)
                self.assertEqual(events, ["file_fsync", "directory_fsync", "decode", "decode"])
                self.assertTrue(result["raw_http"]["file_and_parent_fsync_completed"])
                self.assertEqual(path.read_bytes(), body)

    def test_archive_sync_failures_prevent_decode_and_preserve_bytes_on_both_paths(self):
        given_source = source()
        body = b"retained bytes before any JSON decoding"
        real_fsync = os.fsync
        for existing in (False, True):
            for failure_target in ("file", "directory"):
                for failure_type in (OSError, FileExistsError):
                    with self.subTest(existing=existing, failure_target=failure_target, failure_type=failure_type), tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "raw.http"
                        if existing:
                            path.write_bytes(body)
                        events = []

                        def failed_fsync(fd):
                            kind = "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
                            events.append(kind)
                            if kind == failure_target:
                                raise failure_type("injected " + kind + " fsync failure")
                            return real_fsync(fd)

                        with patch.object(v.os, "fsync", failed_fsync), patch.object(v, "strict_json") as decode:
                            with self.assertRaisesRegex(failure_type, failure_target + " fsync failure"):
                                v.parse_http_response(body, given_source, ASSIGNMENT, archive_path=path)
                            decode.assert_not_called()
                        self.assertEqual(events, ["file"] if failure_target == "file" else ["file", "directory"])
                        self.assertEqual(path.read_bytes(), body)
                        # A retained file after failure must be verified and synced on reuse.
                        with patch.object(v.os, "fsync", wraps=real_fsync) as sync:
                            capture = v.archive_http(body, path)
                        self.assertEqual(sync.call_count, 2)
                        self.assertTrue(capture["file_and_parent_fsync_completed"])

    def test_archive_directory_open_or_close_failure_propagates_before_decode(self):
        given_source = source()
        body = b"retained bytes"
        real_close = os.close
        for existing in (False, True):
            for failure_target in ("open", "close"):
                with self.subTest(existing=existing, failure_target=failure_target), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "raw.http"
                    if existing:
                        path.write_bytes(body)

                    def failed_close(fd):
                        real_close(fd)
                        raise OSError("injected directory close failure")

                    failure = failed_close if failure_target == "close" else OSError("injected directory open failure")
                    with patch.object(v.os, failure_target, side_effect=failure), patch.object(v, "strict_json") as decode:
                        with self.assertRaisesRegex(OSError, "directory " + failure_target + " failure"):
                            v.parse_http_response(body, given_source, ASSIGNMENT, archive_path=path)
                        decode.assert_not_called()
                    self.assertEqual(path.read_bytes(), body)

    def test_archive_mismatched_reuse_refuses_before_sync_or_decode(self):
        given_source = source()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.http"
            path.write_bytes(b"original")
            with patch.object(v.os, "fsync") as sync, patch.object(v, "strict_json") as decode:
                with self.assertRaisesRegex(ValueError, "different bytes"):
                    v.parse_http_response(b"changed", given_source, ASSIGNMENT, archive_path=path)
                sync.assert_not_called()
                decode.assert_not_called()
            self.assertEqual(path.read_bytes(), b"original")

    def test_output_length_and_nonstop_retain_raw_category_but_never_pass(self):
        for finish in ("stop", "length", "error", None, {}):
            with self.subTest(finish=finish), tempfile.TemporaryDirectory() as directory:
                envelope = {"choices": [{"message": {"role": "assistant", "content": raw(relevant_annotation()).decode()}, "finish_reason": finish}], "usage": {"completion_tokens": 100}}
                result = v.parse_http_response(raw(envelope), source(), ASSIGNMENT, archive_path=Path(directory) / "raw.http")
                self.assertEqual(result["primary_fitness_gate"], "pending" if finish == "stop" else "fail")
                self.assertEqual(result["completion"]["output_truncated"], finish == "length")
                self.assertEqual(result["usage"], envelope["usage"])
                self.assertEqual(result["strict_evidence_table"][v.CONCEPTS[0]]["category"], "unresolved")

    def test_malformed_http_envelopes_cannot_leave_a_pending_gate(self):
        choices = ([], [None], [{"message": []}], [{"message": {"role": "assistant", "content": []}}],
                   [{"message": {"role": "assistant", "content": "{}", "refusal": "refused"}}])
        for choice in choices:
            with self.subTest(choice=choice), tempfile.TemporaryDirectory() as directory:
                result = v.parse_http_response(raw({"choices": choice}), source(), ASSIGNMENT,
                                               archive_path=Path(directory) / "raw.http")
                self.assertEqual(result["primary_fitness_gate"], "fail")
                self.assertFalse(result["primary_structural_evaluable"])

    def test_v2_projection_adapter_preserves_all_reasoning_and_legacy_record(self):
        import signals.self_reports.verbalization as legacy
        trajectory = {"initial_context": [{"role": "user", "content": "context"}], "tools": [], "retrospective_cutoff": 3,
                      "messages": [{"role": "assistant", "reasoning_content": None, "reasoning": "first", "content": [{"type": "reasoning", "text": "second"}]},
                                   {"role": "tool", "content": "excluded action"}, {"role": "assistant", "reasoning": "third"},
                                   {"role": "assistant", "reasoning": "after cutoff"}]}
        policy = {"mode": "retain", "rationale": "unit source", "semantics_review_sha256": "c" * 64}
        before = legacy.project_reasoning(trajectory, "retrospective", policy)
        result = v.project_source(trajectory, "retrospective", policy, source_identity={"input_id": "unit"})
        self.assertEqual(result["projection"], before)
        self.assertEqual([part["text"] for part in result["parts"]], ["first", "second", "third"])
        self.assertEqual(result["parts"][-1]["source_location"]["generated_message_index"], 2)
        self.assertEqual(legacy.VERSION, "sigres-verbalization-v2")
        with self.assertRaises(ValueError):
            v.project_source(trajectory, "retrospective", policy, source_identity={"input_sha256": "wrong"})


if __name__ == "__main__":
    unittest.main()
