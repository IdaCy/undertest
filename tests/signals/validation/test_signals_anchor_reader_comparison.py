"""Constructed comparisons only; exact private contract files are caller-bound."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import signals.validation.anchor_reader_comparison as comparison
import signals.validation.anchor_reader_views as views
from tests.signals.behavioral.outcomes.test_signals_anchor_outcomes import SavedFixture


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


AUTO = object()


class ReaderFixture:
    def __init__(self, contracts, channel="public_text", content='é🧪 literal \\n {"artifact":"not parsed"}'):
        self.blobs = {name: raw for name, raw in contracts.items()}
        self.contract_pins = {name: self.save(name, raw) for name, raw in contracts.items()}
        self.channel = channel
        self.calls = []
        self.executions = []
        self.packet = {"contract": deepcopy(views.READER_CONTRACT), "target_channel": channel, "items": []}
        for index in range(108):
            phase = comparison.PHASES[0] if index < 12 else comparison.PHASES[1]
            context = [{"role": "system", "content": "CONSTRUCTED fixture."},
                       {"role": "user", "content": "CONSTRUCTED original task."}]
            if phase == comparison.PHASES[1]:
                context += [{"role": "assistant", "content": "Inherited " + content},
                            {"role": "user", "content": "CONSTRUCTED question about pre-question context."}]
            self.packet["items"].append({
                "item_id": "reader-" + sha((channel + '/CONSTRUCTED/' + str(index)).encode())[:32],
                "phase": phase, "target_channel": channel, "target_message_index": len(context),
                "channel_availability": "available", "availability_reason": "exact_saved_assistant_field",
                "context_status": "exact_saved_request_context", "text_completeness": "recorded_complete_return",
                "target_text_state": "empty" if not content else "text", "target_text_utf8_sha256": sha(content.encode()),
                "messages": context + [{"role": "assistant", "content": content}]})
        self.packet["items"].sort(key=lambda item: item["item_id"])

    def save(self, name, raw):
        self.blobs[name] = raw
        return {"name": name, "bytes": len(raw), "sha256": sha(raw)}

    def read(self, pin):
        name = pin.get("name", pin.get("path"))
        self.calls.append(name)
        if name not in self.blobs:
            raise FileNotFoundError(name)
        return self.blobs[name]

    @staticmethod
    def rows(packet, reader):
        result = []
        for item in packet["items"]:
            source = item["channel_availability"]
            state = "no_relevant_statement" if source == "available" else "coding_unresolved" if source == "unresolved" else "unavailable"
            result.append({"item_id": item["item_id"], "reader_id": reader, "human_reader": False,
                "reading_status": "complete" if source == "available" else state,
                "propositions": [], "unresolved_alternatives": [] if source == "available" else [item["availability_reason"]],
                "type_summaries": {kind: {frame: {"state": state,
                    "relevant_expression": False if source == "available" else None,
                    "affirmative_assertion_present": False if source == "available" else None,
                    "proposition_ids": []} for frame in comparison.FRAMES} for kind in comparison.TYPES}})
        return result

    def placeholder(self, index, availability, *, context=True):
        item = self.packet["items"][index]
        item["messages"] = item["messages"][:-1] if context else []
        item.update(target_message_index=None, channel_availability=availability,
                    target_text_state="not_supplied", target_text_utf8_sha256=None,
                    availability_reason="reasoning_alias_text_conflict" if availability == "unresolved" else "content_null")
        if not context:
            item.update(context_status="missing_or_unjoined_request_context", availability_reason="missing_or_unjoined_request_context")

    @staticmethod
    def expression(row, item, *, state="asserted", polarity="affirmed", affirmative=True):
        content = item["messages"][item["target_message_index"]]["content"]
        quote = content[:2]
        row["propositions"] = [{"proposition_id": "CONSTRUCTED-p1", "type": "evaluation_status",
            "frame": "current_interaction", "referent": "CONSTRUCTED target", "time": "CONSTRUCTED time",
            "conditions": "CONSTRUCTED mechanical fixture; semantic qualification is not tested.",
            "endorsement": "actor_endorsed", "polarity": polarity, "certainty": "certain",
            "analyst_paraphrase": "CONSTRUCTED mechanical fixture.",
            "evidence": {"message_index": item["target_message_index"], "channel": item["target_channel"],
                "start_codepoint": 0, "end_codepoint": 2, "start_utf8_byte": 0,
                "end_utf8_byte": len(quote.encode()), "quote": quote}}]
        row["type_summaries"]["evaluation_status"]["current_interaction"] = {
            "state": state, "relevant_expression": True, "affirmative_assertion_present": affirmative,
            "proposition_ids": ["CONSTRUCTED-p1"]}

    def run(self, *, a=AUTO, b=AUTO, a_pin=AUTO, b_pin=AUTO, packet_pin=AUTO, **changes):
        a = self.rows(self.packet, "CONSTRUCTED-a") if a is AUTO else a
        b = self.rows(self.packet, "CONSTRUCTED-b") if b is AUTO else b
        args = dict(packet_pin=self.save("packet", wire(self.packet)) if packet_pin is AUTO else packet_pin,
            expected_channel=self.channel, contract_pins=deepcopy(self.contract_pins),
            reader_a_pin=self.save("a", wire(a)) if a_pin is AUTO else a_pin, reader_a_id="CONSTRUCTED-a",
            reader_b_pin=self.save("b", wire(b)) if b_pin is AUTO else b_pin, reader_b_id="CONSTRUCTED-b")
        args.update(changes)
        original_args, original_blobs = deepcopy(args), dict(self.blobs)
        execution = {"inputs": deepcopy(args), "fixture": "CONSTRUCTED_NOT_READINGS"}
        self.executions.append(execution)
        try:
            result = comparison.compare_anchor_readers(**args, read_exact=self.read)
            execution["output_pin"] = comparison.pin(comparison.comparison_bytes(result))
            execution["slot_counts"] = result["slot_status_counts"]
            return result
        except comparison.AnchorReaderComparisonError as error:
            execution["root_diagnostic"] = error.diagnostic
            raise
        finally:
            assert args == original_args and self.blobs == original_blobs


class AnchorReaderComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binding = os.environ.get("SIGRES2_READER_CONTRACT_BINDING")
        if not binding:
            raise unittest.SkipTest("explicit pinned private contract fixture binding required")
        pins = json.loads(Path(binding).read_text())
        cls.contracts = {}
        for name, expected in comparison.CONTRACT_PINS.items():
            raw = Path(pins[name]["path"]).read_bytes()
            assert comparison.pin(raw) == expected
            cls.contracts[name] = raw
        cls.schema = comparison.strict_json(cls.contracts["annotation_schema"])
        cls.validator, cls.validator_info = comparison.make_validator(cls.schema)

    def setUp(self):
        self.f = ReaderFixture(self.contracts)
        self.artifacts = {}
        self.measurements = {}

    def assert_denominators(self, result):
        self.assertEqual(result["assigned_reading_slots"], 216)
        self.assertEqual(result["assigned_paired_cells"], 1728)
        counts = result["counts"]
        self.assertEqual(counts["all"]["assigned_cells"], 1728)
        self.assertEqual(counts["by_type_group"]["primary"]["assigned_cells"], 1512)
        self.assertEqual(counts["by_type_group"]["audience_control"]["assigned_cells"], 216)
        self.assertEqual([counts["by_frame"][frame]["assigned_cells"] for frame in comparison.FRAMES], [864, 864])
        self.assertEqual([counts["by_phase"][phase]["assigned_cells"] for phase in comparison.PHASES], [192, 1536])
        self.assertEqual(sum(group["cells"] for group in counts["all"]["strata"].values()), 1728)

    def test_full_108_pair_and_serialization(self):
        result = self.f.run()
        self.assert_denominators(result)
        self.assertEqual(result["slot_status_counts"], {"valid": 216, "invalid": 0, "unresolved": 0, "unavailable": 0})
        self.assertFalse(result["exact_disagreements"])
        self.assertEqual(result["counts"]["all"]["strata"]["both_resolved"]["exact_equal"], dict.fromkeys(comparison.FIELDS, 1728))
        self.artifacts = {"packet.json": self.f.blobs["packet"], "reader_a.json": self.f.blobs["a"],
                          "reader_b.json": self.f.blobs["b"], "comparison.json": comparison.comparison_bytes(result)}
        self.measurements = {name: len(raw) for name, raw in self.artifacts.items()}

    def test_108_large_literal_fields_and_serialization(self):
        self.f = ReaderFixture(self.contracts, content="é🧪" + "C" * 8192)
        a, b = [self.f.rows(self.f.packet, ident) for ident in ("CONSTRUCTED-a", "CONSTRUCTED-b")]
        for item, left, right in zip(self.f.packet["items"], a, b):
            self.f.expression(left, item)
            self.f.expression(right, item)
        result = self.f.run(a=a, b=b)
        self.assert_denominators(result)
        self.assertEqual(result["slot_status_counts"]["valid"], 216)
        self.assertFalse(result["exact_disagreements"])
        self.artifacts = {"packet.json": self.f.blobs["packet"], "reader_a.json": self.f.blobs["a"],
                          "reader_b.json": self.f.blobs["b"], "comparison.json": comparison.comparison_bytes(result)}
        self.measurements = {name: len(raw) for name, raw in self.artifacts.items()}

    def test_actual_exporter_packet_shape_public_raw_summary_and_missing(self):
        fixture = SavedFixture()
        fixture.original(note="CONSTRUCTED exporter compatibility é🧪")
        fixture.ask()
        joined = fixture.run()
        joined_pin = fixture.save("constructed-joined", joined)
        all_ids = set()
        for channel in comparison.CHANNELS:
            ids = {row["assignment_id"]: "reader-" + sha((channel + row["assignment_id"]).encode())[:32]
                   for row in joined["rows"]}
            self.assertFalse(set(ids.values()) & all_ids)
            all_ids.update(ids.values())
            exported = views.project_anchor_reader_views(joined_result_pin=joined_pin, target_channel=channel,
                                                         item_ids=ids, read_exact=fixture.read)
            self.f = ReaderFixture(self.contracts, channel)
            self.f.packet = exported["reader_packet"]
            result = self.f.run()
            self.assert_denominators(result)
            self.assertEqual(result["slot_status_counts"]["invalid"], 0)
            self.assertEqual(result["target_channel"], channel)
            self.assertEqual(result["slot_status_counts"]["unavailable"], 216 if channel == "reasoning_summary" else 212)
            private_pin = self.f.save("private-envelope", wire(exported))
            with self.assertRaisesRegex(comparison.AnchorReaderComparisonError, "selected_reader_packet_only"):
                self.f.run(packet_pin=private_pin)
        missing = fixture.run(terminal_missing=True)
        missing_pin = fixture.save("constructed-missing-joined", missing)
        ids = {row["assignment_id"]: "reader-" + sha(row["assignment_id"].encode())[:32] for row in missing["rows"]}
        self.f = ReaderFixture(self.contracts)
        self.f.packet = views.project_anchor_reader_views(joined_result_pin=missing_pin, target_channel="public_text",
            item_ids=ids, read_exact=fixture.read)["reader_packet"]
        result = self.f.run()
        self.assert_denominators(result)
        self.assertEqual(result["slot_status_counts"]["unavailable"], 216)

    def test_unavailable_unresolved_empty_and_completeness_partitions(self):
        self.f = ReaderFixture(self.contracts, "raw_reasoning")
        self.f.placeholder(0, "unresolved")
        self.f.placeholder(1, "unavailable", context=False)
        item = self.f.packet["items"][2]
        item["messages"][-1]["content"] = ""
        item.update(target_text_state="empty", target_text_utf8_sha256=sha(b""))
        self.f.packet["items"][3]["text_completeness"] = "partial_return"
        self.f.packet["items"][4]["text_completeness"] = "return_completeness_unknown"
        result = self.f.run()
        self.assert_denominators(result)
        self.assertEqual(result["slot_status_counts"], {"valid": 212, "invalid": 0, "unresolved": 2, "unavailable": 2})
        self.assertEqual(result["counts"]["all"]["strata"]["both_resolved"]["cells"], 1696)
        for state in ("unresolved", "unavailable"):
            self.assertEqual(result["counts"]["all"]["strata"]["both_" + state]["cells"], 16)
        for completeness in ("partial_return", "return_completeness_unknown"):
            group = result["counts"]["by_source_text_completeness"][completeness]
            self.assertEqual(group["assigned_cells"], 16)
            self.assertEqual(group["strata"]["both_resolved"]["cells"], 16)
        for index in range(5):
            self.assertEqual(result["slots"][index]["source"], {key: self.f.packet["items"][index][key] for key in comparison.SOURCE_FIELDS})

    def test_no_target_cannot_be_recoded_from_context(self):
        self.f = ReaderFixture(self.contracts, "raw_reasoning")
        before = deepcopy(self.f.packet["items"][0])
        self.f.placeholder(0, "unresolved")
        row = self.f.rows(self.f.packet, "CONSTRUCTED-a")[0]
        row["reading_status"] = "unavailable"
        issues = comparison.validate_row(row, self.f.packet["items"][0], "CONSTRUCTED-a", self.validator)
        self.assertIn("no_target_source_reading_status_mismatch", {entry["code"] for entry in issues})
        self.f.expression(row, before)
        issues = comparison.validate_row(row, self.f.packet["items"][0], "CONSTRUCTED-a", self.validator)
        self.assertIn("no_target_requires_empty_propositions", {entry["code"] for entry in issues})

    def test_missing_pin_absent_file_null_and_malformed_are_distinct(self):
        variants = [(None, "missing_pin", "unavailable"),
                    ({"name": "absent", "bytes": 0, "sha256": sha(b"")}, "missing_file", "unavailable")]
        for name, raw, status in (("null", b"null", "null_document"), ("broken", b"[", "malformed_json"),
                                  ("wrong", b"{}", "invalid_document_type"),
                                  ("duplicates", b'{"a":1,"a":2}', "duplicate_json_key"),
                                  ("inf", b"[1e999]", "malformed_json"),
                                  ("surrogate", b'["\\ud800"]', "invalid_utf8_or_unicode_scalar")):
            variants.append((self.f.save(name, raw), status, "invalid"))
        for bound, status, scientific in variants:
            with self.subTest(status=status):
                result = self.f.run(b_pin=bound)
                self.assert_denominators(result)
                self.assertEqual(result["inputs"][1]["file_status"], status)
                self.assertTrue(all(slot["scientific_status"] == scientific for slot in result["slots"][108:]))
                self.assertEqual(result["counts"]["all"]["strata"]["input_not_comparable"]["cells"], 1728)

    def test_missing_null_duplicate_foreign_reordered_and_extra_rows(self):
        base = self.f.rows(self.f.packet, "CONSTRUCTED-b")
        cases = []
        cases.append((base[:-1], 16, "missing_row"))
        rows = deepcopy(base); rows[0] = None; cases.append((rows, 16, "null_row"))
        rows = deepcopy(base); rows[1] = deepcopy(rows[0]); cases.append((rows, 32, "duplicate"))
        rows = deepcopy(base); rows[0]["item_id"] = "reader-" + "f" * 32; cases.append((rows, 16, "foreign"))
        rows = deepcopy(base); rows[0], rows[1] = rows[1], rows[0]; cases.append((rows, 32, "reordered"))
        cases.append((deepcopy(base) + [None], 1728, "extra"))
        for rows, affected, name in cases:
            with self.subTest(name=name):
                result = self.f.run(b=rows)
                self.assert_denominators(result)
                self.assertEqual(result["counts"]["all"]["strata"]["input_not_comparable"]["cells"], affected)
                if name == "duplicate":
                    self.assertEqual(result["slots"][108]["matching_item_row_indices"], [0, 1])
                if name == "missing_row":
                    self.assertEqual(result["slots"][-1]["return_status"], name)
                if name == "null_row":
                    self.assertEqual(result["slots"][108]["return_status"], name)

    def test_exact_disagreement_accounting_keeps_propositions_unchanged(self):
        a, b = [self.f.rows(self.f.packet, reader) for reader in ("CONSTRUCTED-a", "CONSTRUCTED-b")]
        self.f.expression(a[0], self.f.packet["items"][0])
        self.f.expression(b[0], self.f.packet["items"][0], state="denied", polarity="denied", affirmative=False)
        b[1]["type_summaries"]["audience_demand"]["represented_scenario"] = {
            "state": "coding_unresolved", "relevant_expression": None,
            "affirmative_assertion_present": None, "proposition_ids": []}
        originals = deepcopy((a, b))
        result = self.f.run(a=a, b=b)
        self.assertEqual(len(result["exact_disagreements"]), 2)
        self.assertEqual(result["counts"]["all"]["strata"]["both_resolved"]["exact_different"],
                         {"state": 1, "relevant_expression": 0, "affirmative_assertion_present": 1})
        self.assertEqual([slot["annotation"] for slot in result["slots"][:108]], originals[0])
        self.assertEqual([slot["annotation"] for slot in result["slots"][108:]], originals[1])

    def test_ASK_target_only_and_literal_unicode_offsets(self):
        index = next(n for n, item in enumerate(self.f.packet["items"]) if item["phase"] == comparison.PHASES[1])
        item = self.f.packet["items"][index]
        row = self.f.rows(self.f.packet, "CONSTRUCTED-a")[index]
        self.f.expression(row, item)
        self.assertFalse(comparison.validate_row(row, item, "CONSTRUCTED-a", self.validator))
        row["propositions"][0]["evidence"]["message_index"] = 2
        self.assertIn("wrong_designated_source", {d["code"] for d in comparison.validate_row(row, item, "CONSTRUCTED-a", self.validator)})
        row["propositions"][0]["evidence"].update(message_index=4, end_utf8_byte=2)
        self.assertIn("invalid_utf8_boundaries", {d["code"] for d in comparison.validate_row(row, item, "CONSTRUCTED-a", self.validator)})

    def test_structural_span_endpoint_and_reference_failures(self):
        item = self.f.packet["items"][0]
        row = self.f.rows(self.f.packet, "CONSTRUCTED-a")[0]
        self.f.expression(row, item)
        mutations = [
            (lambda r: r.update(reader_id="CONSTRUCTED-other"), "identity_mismatch"),
            (lambda r: r.update(human_reader=True), "identity_mismatch"),
            (lambda r: r.pop("unresolved_alternatives"), "schema_invalid"),
            (lambda r: r["propositions"][0]["evidence"].update(start_codepoint=0.0), "strict_integer_required"),
            (lambda r: r["propositions"][0]["evidence"].update(message_index=True), "strict_integer_required"),
            (lambda r: r["propositions"][0]["evidence"].update(channel="raw_reasoning"), "wrong_designated_source"),
            (lambda r: r["propositions"][0]["evidence"].update(end_codepoint=99999), "invalid_codepoint_boundaries"),
            (lambda r: r["propositions"][0]["evidence"].update(quote="wrong"), "literal_quote_mismatch"),
            (lambda r: r["propositions"].append(deepcopy(r["propositions"][0])), "duplicate_proposition_id"),
            (lambda r: r["propositions"][0].update(frame="represented_scenario"), "summary_reference_type_or_frame_mismatch"),
            (lambda r: r["type_summaries"]["evaluation_status"]["current_interaction"].update(proposition_ids=["absent"]), "summary_reference_not_unique"),
            (lambda r: r["type_summaries"]["evaluation_status"]["current_interaction"].update(relevant_expression=False), "state_relevant_endpoint_mismatch"),
            (lambda r: r["type_summaries"]["evaluation_status"]["current_interaction"].update(affirmative_assertion_present=False), "state_affirmative_endpoint_mismatch"),
        ]
        for mutate, expected in mutations:
            with self.subTest(expected=expected):
                changed = deepcopy(row); mutate(changed)
                self.assertIn(expected, {d["code"] for d in comparison.validate_row(changed, item, "CONSTRUCTED-a", self.validator)})
        row["type_summaries"]["evaluation_status"]["current_interaction"].update(state="mixed", affirmative_assertion_present=False)
        self.assertFalse(comparison.validate_row(row, item, "CONSTRUCTED-a", self.validator))

    def test_packet_shape_pins_phases_order_channel_and_target_binding(self):
        base = deepcopy(self.f.packet)
        mutations = [
            lambda p: p["items"].pop(),
            lambda p: p.update(target_channel="raw_reasoning"),
            lambda p: p["items"][0].update(target_channel="raw_reasoning"),
            lambda p: p["items"][0].update(item_id="foreign"),
            lambda p: p["items"][1].update(item_id=p["items"][0]["item_id"]),
            lambda p: p["items"].reverse(),
            lambda p: p["items"][0].update(phase="original"),
            lambda p: p["items"][0].update(target_message_index=float(p["items"][0]["target_message_index"])),
            lambda p: p["items"][0]["messages"][-1].update(role="user"),
            lambda p: p["items"][0].update(target_text_utf8_sha256="0" * 64),
            lambda p: p["items"][0].update(target_text_state="empty"),
            lambda p: p["contract"].update(instruction_version="other"),
        ]
        for number, mutate in enumerate(mutations):
            with self.subTest(number=number):
                self.f.packet = deepcopy(base); mutate(self.f.packet)
                with self.assertRaises(comparison.AnchorReaderComparisonError):
                    self.f.run()
        self.f.packet = base
        wrong = self.f.save("packet-wrong-pin", wire(base)); wrong["sha256"] = "0" * 64
        with self.assertRaisesRegex(comparison.AnchorReaderComparisonError, "root_pin_mismatch"):
            self.f.run(packet_pin=wrong)
        contracts = deepcopy(self.f.contract_pins); contracts["instructions"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(comparison.AnchorReaderComparisonError, "unsupported_contract_pin"):
            self.f.run(contract_pins=contracts)

    def test_explicit_size_limits_reject_before_JSON_expansion(self):
        # Small patched ceilings exercise the real order without emitting huge junk fixtures.
        self.f.save("oversized", b"not-json" * 40)
        bound = {"name": "oversized", "bytes": 1, "sha256": "0" * 64}
        with patch.object(comparison, "MAX_PACKET_BYTES", 100):
            with self.assertRaisesRegex(comparison.AnchorReaderComparisonError, "root_input_exceeds_bound"):
                self.f.run(packet_pin=bound)
        with patch.object(comparison, "MAX_READER_BYTES", 100):
            result = self.f.run(b_pin=bound)
            self.assertEqual(result["inputs"][1]["file_status"], "input_exceeds_bound")
            self.assertEqual(result["counts"]["all"]["strata"]["input_not_comparable"]["cells"], 1728)
        with patch.object(comparison, "MAX_RESULT_BYTES", 100):
            with self.assertRaisesRegex(comparison.AnchorReaderComparisonError, "comparison_output_exceeds_bound"):
                self.f.run()

    def test_long_expected_quotes_keep_bounded_diagnostics(self):
        item = self.f.packet["items"][0]
        content = "Z" * 65536
        item["messages"][-1]["content"] = content
        item["target_text_utf8_sha256"] = sha(content.encode())
        b = self.f.rows(self.f.packet, "CONSTRUCTED-b")
        self.f.expression(b[0], item)
        template = b[0]["propositions"][0]
        template["evidence"].update(end_codepoint=len(content), end_utf8_byte=len(content), quote="wrong")
        b[0]["propositions"] = [{**deepcopy(template), "proposition_id": f"p{number:03}"} for number in range(96)]
        b[0]["type_summaries"]["evaluation_status"]["current_interaction"]["proposition_ids"] = ["p000"]
        result = self.f.run(b=b)
        self.assert_denominators(result)
        issues = result["slots"][108]["diagnostics"]
        self.assertEqual(len(issues), 96)
        self.assertTrue(all(issue["code"] == "literal_quote_mismatch"
                            and issue["expected_codepoints"] == len(content)
                            and issue["expected_utf8_sha256"] == sha(content.encode()) for issue in issues))
        body = comparison.comparison_bytes(result)
        self.assertNotIn(b"Z" * 1024, body)
        self.assertEqual(result["slots"][108]["annotation"], b[0])
        self.measurements = {"literal_target_bytes": len(content), "mismatches": len(issues),
                             "compact_result_bytes": len(body), "diagnostic_bytes": len(wire(issues))}

    def test_guarded_schema_compatibility_and_reader_identity(self):
        schema = deepcopy(self.schema)
        comparison.make_validator(schema)
        self.assertEqual(schema, self.schema)
        for name, value in (("$ref", "https://example.invalid/do-not-fetch"), ("minItems", 1)):
            changed = deepcopy(schema); changed["properties"]["propositions"][name] = value
            with self.assertRaises(ValueError):
                comparison.make_validator(changed)
        with self.assertRaisesRegex(comparison.AnchorReaderComparisonError, "distinct_reader_identities"):
            self.f.run(reader_b_id="CONSTRUCTED-a")

    def assert_work_limited(self, result, bound, disposition):
        self.assert_denominators(result)
        loaded = result["inputs"][1]
        self.assertEqual(loaded["file_status"], "validation_work_bound")
        self.assertEqual(loaded["validation_disposition"], disposition)
        self.assertIs(loaded["validation_complete"], False)
        self.assertEqual(loaded["observed_pin"], comparison.pin(self.f.blobs["b"]))
        self.assertEqual(loaded["expected_pin"], self.f.executions[-1]["inputs"]["reader_b_pin"])
        self.assertEqual(loaded["diagnostics"][-1]["bound"], bound)
        self.assertTrue(all(slot["validation_complete"] is False for slot in result["slots"][108:]))
        self.assertTrue(all(cell["b"]["value"] is None for cell in result["cells"]))
        self.assertEqual(result["counts"]["all"]["strata"]["input_not_comparable"]["cells"], 1728)

    def test_structural_work_admission_precedes_schema_enumeration(self):
        baseline = self.f.rows(self.f.packet, "CONSTRUCTED-b")
        cases = [(baseline + [{}], "reader_rows")]
        for field, value, bound in (
            ("propositions", [{}] * 129, "container_entries"),
            ("unresolved_alternatives", [[list(range(128)) for _ in range(128)] for _ in range(2)], "reader_nodes"),
            ("unresolved_alternatives", {str(index): None for index in range(129)}, "container_entries")):
            rows = deepcopy(baseline); rows[0][field] = value; cases.append((rows, bound))
        nested = None
        for _ in range(13):
            nested = [nested]
        rows = deepcopy(baseline); rows[0]["unresolved_alternatives"] = nested
        cases.append((rows, "reader_depth"))
        for rows, bound in cases:
            with self.subTest(bound=bound):
                # Only the valid A return can reach schema validation.
                real = comparison.validate_row
                def check(row, item, reader, validator, _budget=None):
                    self.assertEqual(reader, "CONSTRUCTED-a")
                    return real(row, item, reader, validator, _budget)
                with patch.object(comparison, "validate_row", side_effect=check):
                    result = self.f.run(b=rows)
                self.assert_work_limited(result, bound, "admission_work_bound")
                self.assertEqual([slot["annotation"] for slot in result["slots"][108:]], rows[:108])
        # The structural depth boundary itself is admitted; schema validity is separate.
        nested = None
        for _ in range(comparison.MAX_READER_DEPTH):
            nested = [nested]
        self.assertIsNone(comparison._reader_structure_issue(nested))
        self.assertEqual(comparison._reader_structure_issue([nested])["bound"], "reader_depth")

    def test_diagnostic_count_and_bytes_are_explicitly_incomplete(self):
        for props, bound, retained in (([{}] * 96, "diagnostic_count", 256),
                                      (["X" * 40000, "Y" * 40000], "diagnostic_bytes", 1)):
            with self.subTest(bound=bound):
                rows = self.f.rows(self.f.packet, "CONSTRUCTED-b")
                rows[0]["propositions"] = props
                result = self.f.run(b=rows)
                self.assert_work_limited(result, bound, "diagnostic_work_bound")
                loaded = result["inputs"][1]
                issue = loaded["diagnostics"][-1]
                self.assertEqual(issue["diagnostics_retained"], retained)
                self.assertTrue(issue["further_diagnostics_not_enumerated"])
                diagnostics = [d for segment in loaded["retained_validation_diagnostics"] for d in segment["diagnostics"]]
                self.assertEqual(len(diagnostics), retained)
                self.assertLessEqual(sum(len(wire(d)) for d in diagnostics), comparison.MAX_DIAGNOSTIC_BYTES)
                self.assertEqual([s["annotation"] for s in result["slots"][108:]], rows)
                self.measurements[bound] = {"diagnostics_retained": retained,
                    "diagnostic_bytes": sum(len(wire(d)) for d in diagnostics),
                    "result_bytes": len(comparison.comparison_bytes(result))}

    def test_work_bound_preserves_null_missing_and_source_states(self):
        self.f = ReaderFixture(self.contracts, "raw_reasoning")
        self.f.placeholder(0, "unresolved")
        self.f.placeholder(1, "unavailable", context=False)
        rows = self.f.rows(self.f.packet, "CONSTRUCTED-b")[:-1]
        rows[0] = None
        rows[2]["propositions"] = [{}] * 129
        result = self.f.run(b=rows)
        self.assert_work_limited(result, "container_entries", "admission_work_bound")
        self.assertEqual(result["slots"][108]["return_status"], "null_row")
        self.assertEqual(result["slots"][108]["scientific_status"], "invalid")
        self.assertEqual(result["slots"][-1]["return_status"], "missing_row")
        self.assertEqual(result["slots"][-1]["scientific_status"], "unavailable")
        self.assertEqual([s["source"]["channel_availability"] for s in result["slots"][108:110]],
                         ["unresolved", "unavailable"])
        self.assertEqual([s["scientific_status"] for s in result["slots"][:2]], ["unresolved", "unavailable"])
        # A huge duplicate-key error is malformed supplied input, never a missing row.
        raw = ('{"' + 'X' * 70000 + '":0,"' + 'X' * 70000 + '":1}').encode()
        result = self.f.run(b_pin=self.f.save("b", raw))
        self.assert_work_limited(result, "diagnostic_bytes", "diagnostic_work_bound")
        self.assertEqual(result["inputs"][1]["pre_limit_file_status"], "duplicate_json_key")
        self.assertTrue(all(s["return_status"] == "duplicate_json_key" and s["scientific_status"] == "invalid"
                            for s in result["slots"][108:]))

    def test_incremental_output_bound_and_exact_encoding(self):
        value = {"CONSTRUCTED": ["é🧪", {"z": None, "a": False}]}
        expected = wire(value)
        with patch.object(comparison, "MAX_RESULT_BYTES", len(expected)):
            self.assertEqual(comparison.comparison_bytes(value), expected)
        with patch.object(comparison, "MAX_RESULT_BYTES", len(expected) - 1):
            with self.assertRaises(comparison.AnchorReaderComparisonError) as caught:
                comparison.comparison_bytes(value)
            self.assertEqual(caught.exception.diagnostic["observed_bytes_at_least"], len(expected))
        # About 20 MiB if expanded; the serializer stops at the unchanged 6 MiB cap.
        with self.assertRaises(comparison.AnchorReaderComparisonError) as caught:
            comparison.comparison_bytes({"CONSTRUCTED": ["X" * 1000] * 20000})
        self.assertEqual(caught.exception.diagnostic["code"], "comparison_output_exceeds_bound")
        self.assertLessEqual(caught.exception.diagnostic["observed_bytes_at_least"], comparison.MAX_RESULT_BYTES + 1024)


if __name__ == "__main__":
    unittest.main()
