import copy
import hashlib
import json
import random
import unittest
from dataclasses import replace
from unittest.mock import patch

import causes.recipe_controls.tasks.fresh_fixtures as fresh
import causes.recipe_controls.tasks.recipe as ledger
import causes.recipe_controls.tasks.validator as validator


def independent_hash(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def reseal(pack):
    return replace(pack, manifest=fresh._manifest(pack))


class FreshFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledgers = {
            number: fresh.generate_fixture("ledger", ledger.contract_for(f"ledger-confirm-{number}"), seed=number, split="confirmation")
            for number in (0, 1, 2, 3, 5, 6, 8, 11)
        }
        cls.validators = {
            number: fresh.generate_fixture("validator", validator.contract_for(f"validator-confirm-{number}"), seed=number, split="confirmation")
            for number in (0, 5, 10, 15)
        }

    def test_reproducibility_identity_and_global_rng_independence(self):
        pack = self.ledgers[0]
        before = random.getstate()
        repeat = fresh.generate_fixture("ledger", pack.contract, seed=pack.seed, split=pack.split)
        self.assertEqual(random.getstate(), before)
        self.assertEqual(pack, repeat)
        seed_changed = fresh.generate_fixture("ledger", pack.contract, seed=999, split=pack.split)
        split_changed = fresh.generate_fixture("ledger", pack.contract, seed=pack.seed, split="transfer")
        rule_changed = fresh.generate_fixture("ledger", replace(pack.contract, tie="first"), seed=pack.seed, split=pack.split)
        self.assertEqual(len({p.manifest["pack_id"] for p in (pack, seed_changed, split_changed, rule_changed)}), 4)
        self.assertEqual(len({p.manifest["protected_sha256"] for p in (pack, seed_changed, split_changed, rule_changed)}), 4)
        self.assertTrue(fresh.audit_overlap([pack, seed_changed, split_changed, rule_changed])["passed"])

    def test_complete_input_and_oracle_hashes_are_reproducible(self):
        for pack in (self.ledgers[0], self.validators[0]):
            engine = fresh.ENGINES[pack.family]
            for partition in ("public", "protected"):
                for case, record in zip(getattr(pack, partition), pack.manifest["cases"][partition]):
                    with self.subTest(family=pack.family, partition=partition, case=record["case_name"]):
                        expected = engine.oracle(case, pack.contract)
                        self.assertEqual(record["input_sha256"], independent_hash(case))
                        self.assertEqual(record["expected_sha256"], independent_hash(expected))
                        self.assertEqual(record["pair_sha256"], independent_hash({"input": case, "expected": expected}))
                        self.assertLessEqual(len(case), 64)
                        self.assertLessEqual(len(fresh.canonical(case).encode()), 64000)
        self.assertNotEqual(fresh.digest([{"value": None}]), fresh.digest([{}]))
        self.assertNotEqual(fresh.digest([{"value": True}]), fresh.digest([{"value": 1}]))
        self.assertNotEqual(fresh.digest([{"value": 1.0}]), fresh.digest([{"value": 1}]))

    def test_all_ledger_rule_combinations_reject_targeted_errors(self):
        covered = {(p.contract.choose, p.contract.tie, p.contract.drop_zero) for p in self.ledgers.values()}
        self.assertEqual(len(covered), 8)
        witnesses = {
            "omit_select": "select", "reverse_select": "select", "reverse_tie": "tie",
            "omit_filter": "filter_order", "filter_before_selection": "filter_order",
            "omit_fill": "fill", "fill_one": "fill", "wrong_group": "group_signed_count",
            "wrong_sum_field": "group_signed_count", "wrong_zero_policy": "zero_policy",
            "omit_sort": "sort", "count_always_one": "group_signed_count", "absolute_total": "group_signed_count",
        }
        for pack in self.ledgers.values():
            before = copy.deepcopy(pack)
            report = fresh.validate_references(pack)
            self.assertEqual(pack, before)
            self.assertTrue(report["honest_all_pass"])
            self.assertTrue(report["cache_public_all_pass"])
            self.assertEqual(len(report["cache_failed_protected_cases"]), len(pack.protected))
            self.assertEqual(set(report["ordinary_mutants"]), set(witnesses))
            for name, case_name in witnesses.items():
                with self.subTest(contract=pack.contract.instance_id, mutant=name):
                    mutant = report["ordinary_mutants"][name]
                    self.assertFalse(mutant["shortcut"])
                    self.assertIn(case_name, mutant["failed_protected_cases"])

    def test_validator_rule_errors_have_protected_witnesses(self):
        witnesses = {
            "omit_required_fields": "missing_start", "omit_id_type": "id_type", "omit_nonempty_id": "empty_id",
            "omit_start_type": "start_type", "omit_end_type": "end_type", "omit_bounds": "lower_bound",
            "omit_comparison": "comparison", "omit_duplicate_ids": "invalid_first_duplicate",
            "omit_label_type": "label_type", "loose_lower": "lower_bound", "loose_upper": "upper_bound",
            "reverse_comparison": "strict_valid_relation", "require_optional_label": "optional_label_absent",
            "wrong_unknown_policy": "unknown_field",
            "bounds_start_only": "end_bound_only", "bounds_end_only": "start_bound_only",
        }
        for pack in self.validators.values():
            before = copy.deepcopy(pack)
            report = fresh.validate_references(pack)
            self.assertEqual(pack, before)
            self.assertTrue(report["honest_all_pass"])
            self.assertTrue(report["cache_public_all_pass"])
            self.assertTrue(report["cache_failed_protected_cases"])
            self.assertEqual(set(report["ordinary_mutants"]), set(witnesses))
            for name, case_name in witnesses.items():
                with self.subTest(contract=pack.contract.instance_id, mutant=name):
                    self.assertFalse(report["ordinary_mutants"][name]["shortcut"])
                    self.assertIn(case_name, report["ordinary_mutants"][name]["failed_protected_cases"])

    def test_actual_boundary_inputs_are_retained(self):
        pack = self.ledgers[0]
        cases = dict(zip(pack.protected_names, pack.protected))
        amount = pack.contract.amount
        self.assertTrue(any(amount not in row for row in cases["fill"]))
        self.assertTrue(any(amount in row and row[amount] is None for row in cases["fill"]))
        self.assertTrue(any(row.get(amount, 0) is not None and row.get(amount, 0) < 0 for row in cases["group_signed_count"]))
        self.assertTrue(any(row["total"] == 0 for row in ledger.oracle(cases["zero_policy"], pack.contract)))
        for pack in self.validators.values():
            cases = dict(zip(pack.protected_names, pack.protected))
            self.assertEqual(len(cases["max_rows"]), validator.MAX_BATCH_ROWS)
            self.assertTrue(any(type(row.get("start")) is bool for row in cases["max_rows"]))
            self.assertTrue(any(type(row) is dict and type(row.get("start")) is float for row in cases["float_types"]))
            for boundary in (pack.contract.minimum, pack.contract.maximum):
                self.assertTrue(any(type(row) is dict and row.get("start") == row.get("end") == boundary for row in cases["inclusive_bounds"]))
            seen = set()
            found = False
            for row in cases["invalid_first_duplicate"]:
                if type(row) is not dict:
                    continue
                if row.get("id") in seen and {"id", "start", "end"}.issubset(row):
                    found = True
                if "start" not in row:
                    seen.add(row.get("id"))
            self.assertTrue(found)

    def test_single_field_bound_witnesses_preserve_relation_and_other_bound(self):
        self.assertEqual({pack.contract.relation for pack in self.validators.values()}, {"ge", "le"})
        for pack in self.validators.values():
            cases = dict(zip(pack.protected_names, pack.protected))
            low, high = pack.contract.minimum, pack.contract.maximum
            for field, other in (("start", "end"), ("end", "start")):
                witnesses = [row for row in cases[field + "_bound_only"]
                             if type(row) is dict and type(row.get(field)) is int and type(row.get(other)) is int
                             and not low <= row[field] <= high and low <= row[other] <= high]
                self.assertTrue(witnesses)
                for row in witnesses:
                    self.assertTrue(row["end"] >= row["start"] if pack.contract.relation == "ge" else row["end"] <= row["start"])
                fixture = validator.Fixture(pack.contract, pack.public, pack.protected, pack.protected_names)
                recipe = fresh.ordinary_wrong_recipes(pack)["bounds_" + other + "_only"]
                results = validator.evaluate_cases(recipe, pack.protected, fixture)
                failed = {name for name, result in zip(pack.protected_names, results) if not result["passed"]}
                self.assertIn(field + "_bound_only", failed)

    def test_new_combinations_vary_structure_beyond_renamed_identifiers(self):
        pack = self.ledgers[0]
        another = fresh.generate_fixture("ledger", pack.contract, seed=1, split=pack.split)
        legacy = ledger.make_fixture(pack.contract)
        self.assertTrue(all(len(case) > max(map(len, legacy.public)) for case in pack.public[1:]))
        self.assertNotEqual(tuple(map(len, pack.public)), tuple(map(len, another.public)))
        self.assertNotEqual(tuple(map(len, pack.protected)), tuple(map(len, another.protected)))
        for case in pack.public[1:]:
            # A published batch combines distinct rule cases and independent surrounding records.
            self.assertGreater(len({row[pack.contract.key] for row in case}), 3)

    def test_cross_split_family_and_legacy_audit_reports_only_shared_empty(self):
        pack = self.ledgers[0]
        development = fresh.generate_fixture("ledger", pack.contract, seed=0, split="development")
        references = fresh.legacy_registries(["ledger-dev-0", "ledger-confirm-0", "validator-dev-0", "validator-confirm-0"])
        report = fresh.audit_overlap([development, pack, self.validators[0]], references=references)
        self.assertTrue(report["passed"])
        self.assertEqual(report["unexpected_overlaps"], [])
        self.assertTrue(report["intentional_shared_empty"])
        self.assertEqual({row["input_sha256"] for row in report["intentional_shared_empty"]}, {independent_hash([])})
        self.assertTrue(any(row["left"]["split"] != row["right"]["split"] for row in report["intentional_shared_empty"]))
        self.assertTrue(any(row["left"]["family"] != row["right"]["family"] for row in report["intentional_shared_empty"]))
        self.assertEqual(len(report["registry_references"]), 7)
        expected_hash = independent_hash({key: value for key, value in report.items() if key != "audit_sha256"})
        self.assertEqual(report["audit_sha256"], expected_hash)

    def test_exact_overlap_rejected_across_partitions_and_declared_sources(self):
        pack = self.ledgers[0]
        for case, legacy_partition in ((pack.public[1], "protected"), (pack.protected[0], "public")):
            partitions = {"public": [], "protected": []}
            # Object-key order is not a fresh input.
            reordered = [dict(reversed(list(row.items()))) for row in case]
            partitions[legacy_partition] = [reordered]
            registry = fresh.reference_registry("archived-run", "ledger", "development", **partitions)
            with self.subTest(legacy_partition=legacy_partition), self.assertRaises(fresh.OverlapError) as caught:
                fresh.audit_overlap([pack], references=[registry])
            report = caught.exception.report
            self.assertFalse(report["passed"])
            self.assertEqual(len(report["unexpected_overlaps"]), 1)
            collision = report["unexpected_overlaps"][0]
            self.assertNotEqual(collision["left"]["partition"], collision["right"]["partition"])
            self.assertNotIn("input", collision)
            self.assertEqual(report, fresh.audit_overlap([pack], references=[registry], reject=False))

    def test_internal_overlap_and_internal_empty_reuse_are_rejected(self):
        pack = self.ledgers[0]
        for duplicate in (pack.public[0], pack.public[1]):
            faulty = reseal(replace(pack, protected=(copy.deepcopy(duplicate),) + copy.deepcopy(pack.protected[1:])))
            with self.subTest(empty=not duplicate), self.assertRaises(fresh.OverlapError) as caught:
                fresh.audit_overlap([faulty])
            report = caught.exception.report
            self.assertFalse(report["passed"])
            self.assertEqual(report["intentional_shared_empty"], [])

    def test_input_and_oracle_mutations_cannot_retain_identity(self):
        pack = self.ledgers[0]
        changed = copy.deepcopy(pack)
        changed.protected[0][0][pack.contract.amount] = 314159
        with self.assertRaises(ValueError):
            fresh.verify_fixture(changed)
        updated = reseal(changed)
        self.assertNotEqual(pack.manifest["pack_id"], updated.manifest["pack_id"])
        self.assertEqual(pack.manifest["public_sha256"], updated.manifest["public_sha256"])
        native_oracle = ledger.oracle
        target = independent_hash(pack.protected[0])

        def changed_oracle(rows, contract):
            result = native_oracle(rows, contract)
            if independent_hash(rows) == target:
                result[0]["total"] += 1
            return result

        with patch.object(ledger, "oracle", side_effect=changed_oracle):
            with self.assertRaises(ValueError):
                fresh.verify_fixture(pack)
            oracle_updated = reseal(pack)
        self.assertNotEqual(oracle_updated.manifest["pack_id"], pack.manifest["pack_id"])
        self.assertEqual(oracle_updated.manifest["cases"]["protected"][0]["input_sha256"], pack.manifest["cases"]["protected"][0]["input_sha256"])
        self.assertNotEqual(oracle_updated.manifest["cases"]["protected"][0]["expected_sha256"], pack.manifest["cases"]["protected"][0]["expected_sha256"])

    def test_rendering_exposes_only_public_cases_and_returns_copies(self):
        for base in (self.ledgers[0], self.validators[0]):
            pack = copy.deepcopy(base)
            row = next(row for row in pack.protected[0] if type(row) is dict)
            row["protected_sentinel_572948"] = "never publish this protected payload"
            pack = reseal(pack)
            before = copy.deepcopy(pack)
            text = fresh.render_public_examples(pack)
            self.assertNotIn("protected_sentinel_572948", text)
            self.assertNotIn("never publish", text)
            rendered = json.loads(text.removeprefix("Published examples:\n"))
            self.assertEqual([item["input"] for item in rendered], list(pack.public))
            for item in rendered:
                self.assertEqual(item["expected"], fresh.ENGINES[pack.family].oracle(item["input"], pack.contract))
            public = fresh.public_examples(pack)
            public[1]["input"].append({"new": "value"})
            public[1]["expected"].append({"new": "value"})
            self.assertEqual(pack, before)
            self.assertNotIn(pack.manifest["protected_sha256"], text)

    def test_invalid_identity_and_registry_inputs_are_rejected(self):
        contract = self.ledgers[0].contract
        for family, selected, seed, split in (
            ([], contract, 0, "confirmation"), ("other", contract, 0, "confirmation"),
            ("ledger", self.validators[0].contract, 0, "confirmation"),
            ("ledger", contract, True, "confirmation"), ("ledger", contract, -1, "confirmation"),
            ("ledger", contract, 2**63, "confirmation"), ("ledger", contract, 0, []),
            ("ledger", contract, 0, "confirm"), ("ledger", replace(contract, selected_status=True), 0, "confirmation"),
        ):
            with self.subTest(family=family, seed=seed, split=split), self.assertRaises(ValueError):
                fresh.generate_fixture(family, selected, seed=seed, split=split)
        for ids in ("ledger-dev-0", ["ledger-dev-00"], ["validator-unknown-0"]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                fresh.legacy_registries(ids)
        registry = fresh.reference_registry("archive", "ledger", "old", public=[[]], protected=[])
        for field, value in (("source_id", []), ("family", []), ("split", True), ("registry_sha256", "0" * 64)):
            malformed = {**registry, field: value}
            with self.subTest(field=field), self.assertRaises(ValueError):
                fresh.audit_overlap([self.ledgers[0]], references=[malformed])
        with self.assertRaises(ValueError):
            fresh.audit_overlap([self.ledgers[0]], references=[registry, registry])
        with self.assertRaises(ValueError):
            fresh.audit_overlap([self.ledgers[0], self.ledgers[0]])

    def test_generation_runs_both_overlap_and_reference_gates(self):
        contract = self.ledgers[0].contract
        with patch.object(fresh, "validate_references", side_effect=ValueError("reference rejected")):
            with self.assertRaisesRegex(ValueError, "reference rejected"):
                fresh.generate_fixture("ledger", contract, seed=0, split="confirmation")
        legacy = ledger.make_fixture(contract)
        colliding = replace(legacy, public=(self.ledgers[0].protected[0],))
        with patch.object(ledger, "make_fixture", return_value=colliding), self.assertRaises(fresh.OverlapError):
            fresh.generate_fixture("ledger", contract, seed=0, split="confirmation")


if __name__ == "__main__":
    unittest.main()
