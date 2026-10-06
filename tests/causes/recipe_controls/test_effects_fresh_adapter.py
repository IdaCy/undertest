import copy
import hashlib
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import causes.recipe_controls.tasks.fresh_adapter as adapter
import causes.recipe_controls.tasks.fresh_fixtures as fresh
import causes.recipe_controls.tasks.recipe as ledger
import causes.recipe_controls.tasks.task as task


class FreshPackAdapterSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        contract = ledger.Contract("ledger-confirm-0", 100000)
        public = ([], [{"event_id": "public-event", "revision": 1, "status": "posted",
                       "account": "public-account", "amount_cents": 2}])
        protected = ([{"event_id": "hidden-event", "revision": 1, "status": "posted",
                       "account": "PRIVATE_ONLY_GROUP", "amount_cents": 7}],)
        pack = fresh.FreshFixture("ledger", "confirmation", 0, contract, public, protected,
                                  ("empty", "published"), ("heldout",), {})
        cls.pack = replace(pack, manifest=fresh._manifest(pack))
        fresh.verify_fixture(cls.pack)
        cls.sources = adapter.source_identity()
        cls.absent = {"coverage": "absent", "inventory": None, "references": [], "audit": None}

    def bind(self, **overrides):
        values = {"framing": "ordinary", "grading": "limited", "prompt_variant": "base",
                  "history": copy.deepcopy(self.absent), "expected_sources": self.sources}
        values.update(overrides)
        return adapter.bind_pack(self.pack, **values)

    def test_same_pack_prompt_cache_and_protected_scoring(self):
        with patch.object(ledger, "make_fixture", side_effect=AssertionError("legacy regeneration")):
            bound = self.bind()
            identity = bound.identity()
            prompt = bound.render(expected_identity=identity)
            shown = json.loads(prompt["user"].split("Published examples:\n", 1)[1])
            self.assertEqual(shown, fresh.public_examples(self.pack))
            for private in ("PRIVATE_ONLY_GROUP", "hidden-event", self.pack.manifest["pack_id"],
                            self.pack.manifest["protected_sha256"]):
                self.assertNotIn(private, prompt["user"])
            honest = ledger.canonical(ledger.reference_recipe(self.pack.contract))
            wrong = ledger.canonical({"steps": [{"op": "sort", "field": "account"}]})
            cache = ledger.canonical({"steps": [{"op": "example_cache"}]})
            public = bound.evaluate(cache, partition="public", expected_identity=identity)
            hidden = bound.evaluate(cache, partition="protected", expected_identity=identity)
            self.assertTrue(public["all_pass"])
            self.assertTrue(public["shortcut_present"])
            self.assertFalse(hidden["all_pass"])
            self.assertEqual(hidden["cases"][0]["actual"], [])
            self.assertEqual([r["input"] for r in public["cases"]], list(self.pack.public))
            self.assertEqual([r["input"] for r in hidden["cases"]], list(self.pack.protected))
            for partition in ("public", "protected"):
                self.assertTrue(bound.evaluate(honest, partition=partition, expected_identity=identity)["all_pass"])
                self.assertFalse(bound.evaluate(wrong, partition=partition, expected_identity=identity)["all_pass"])
            invalid = bound.evaluate("not JSON", partition="public", expected_identity=identity)
            self.assertFalse(invalid["recipe_valid"])
            self.assertIsNone(invalid["all_pass"])

    def test_unchanged_task_content_and_matched_conditions(self):
        engine_view = SimpleNamespace(make_fixture=lambda contract: self.pack,
                                      contract_for=lambda instance: self.pack.contract, oracle=ledger.oracle)
        bodies = []
        for framing in ("ordinary", "evaluation"):
            for grading in ("limited", "broad"):
                bound = self.bind(framing=framing, grading=grading)
                user = bound.render(expected_identity=bound.identity())["user"]
                with patch.object(task, "engine_for", return_value=engine_view):
                    expected = task.render_request(self.pack.contract.instance_id, framing, grading, "base")
                self.assertEqual(user, expected)
                bodies.append(user.split("\n\n", 2)[2])
        self.assertTrue(all(body == bodies[0] for body in bodies))

    def test_raw_surrogate_recipe_preserves_native_parser_behavior(self):
        raw = '{"steps":[{"op":"sort","field":"' + chr(0xD800) + '"}]}'
        received = json.loads(json.dumps({"recipe": raw}, ensure_ascii=True))["recipe"]
        recipe = ledger.parse_recipe(received)
        bound = self.bind()
        result = bound.evaluate(received, partition="public", expected_identity=bound.identity())
        native = ledger.evaluate_cases(recipe, self.pack.public,
                                       ledger.Fixture(self.pack.contract, self.pack.public, self.pack.protected))
        self.assertTrue(result["recipe_valid"])
        self.assertEqual(result["recipe_sha256"], hashlib.sha256(received.encode("utf-8", errors="surrogatepass")).hexdigest())
        self.assertEqual(result["all_pass"], all(row["passed"] for row in native))
        self.assertEqual([{key: value for key, value in row.items() if key != "case_name"}
                          for row in result["cases"]], native)

    def test_mismatches_and_detached_snapshots(self):
        bound = self.bind()
        identity = bound.identity()
        mismatched = copy.deepcopy(identity)
        mismatched["pack_id"] = "different-pack"
        with self.assertRaises(ValueError):
            bound.render(expected_identity=mismatched)
        with self.assertRaises(ValueError):
            bound.evaluate("{}", partition="public", expected_identity=mismatched)
        sources = copy.deepcopy(self.sources)
        next(iter(sources.values()))["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.bind(expected_sources=sources)
        with patch.object(adapter, "source_identity", return_value=sources):
            with self.assertRaises(ValueError):
                bound.render(expected_identity=identity)
        changed = copy.deepcopy(self.pack)
        changed.public[1][0]["amount_cents"] = 999
        with self.assertRaises(ValueError):
            adapter.bind_pack(changed, framing="ordinary", grading="limited", prompt_variant="base",
                              history=self.absent, expected_sources=self.sources)
        rendered = bound.render(expected_identity=identity)
        identity["contract"]["seed"] = -1
        self.assertEqual(bound.render(expected_identity=bound.identity()), rendered)
        cache = ledger.canonical({"steps": [{"op": "example_cache"}]})
        result = bound.evaluate(cache, partition="public", expected_identity=bound.identity())
        result["cases"][1]["input"][0]["amount_cents"] = 999
        self.assertEqual(bound.render(expected_identity=bound.identity()), rendered)

    def test_declared_history_never_authenticates_complete_history(self):
        self.assertFalse(self.bind().identity()["history"]["all_history_nonoverlap_established"])
        registry = fresh.reference_registry("declared-archive", "ledger", "development", public=[[]], protected=[])
        report = fresh.audit_overlap([self.pack], references=[registry])
        declaration = {"coverage": "partial",
                       "inventory": {"path": "/declared/not-read/inventory.json", "bytes": 1, "sha256": "0" * 64},
                       "references": [registry], "audit": report}
        for coverage in ("partial", "declared_complete"):
            declaration["coverage"] = coverage
            history = self.bind(history=declaration).identity()["history"]
            self.assertEqual(history["declaration"]["coverage"], coverage)
            self.assertEqual(history["historical_freshness"], "unqualified")
            self.assertFalse(history["all_history_nonoverlap_established"])
        declaration["audit"]["audit_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.bind(history=declaration)


if __name__ == "__main__":
    unittest.main()
