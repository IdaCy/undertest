import copy
import json
import unittest

from emergence.training.data import assign_task_splits
from emergence.training.data import derive_matched_records
from emergence.training.data import normalize_source_task
from emergence.training.data import normalized_source_text
from emergence.training.data import validate_split_integrity


def source(row_id, text=None, **extra):
    return normalize_source_task({
        "source_uri": "https://example.invalid/source",
        "source_revision": "a" * 40,
        "source_row_id": str(row_id),
        "source_text": text or f"Compute {row_id} + 1.",
        "source_usage": "training_allowed",
        "license": "test-fixture-only",
        "private_gold": {"answer": "secret gold", "scorer_args": {"mode": "exact"}},
        **extra,
    })


def partition(tasks, **extra):
    return assign_task_splits(tasks, seed=7, split_weights={"development": 1}, **extra)


def candidate(task, suffix, status, *, response=None, substantive=True, **extra):
    complete = status in {"correct", "incorrect"}
    score = {
        "status": status,
        "reward": 1.0 if status == "correct" else (0.0 if status in {"incorrect", "invalid", "truncated"} else None),
        "correctness": status if complete else "unscored",
        "validity": "valid" if complete else "not_checked",
        "parse_status": "valid" if complete else "not_checked",
        "finish_reason": "length" if status == "truncated" else "stop",
        "transport_status": "error" if status == "scorer_error" else "ok",
        "scorer_version": "fixture_scalar_v1",
        "reason": f"fixture {status}",
        "normalized_gold": "secret gold",
    }
    return {
        "candidate_id": f"{task['task_id']}_{suffix}",
        "task_id": task["task_id"],
        "response": response if response is not None else f"response {suffix} for {status}",
        "substantive": substantive,
        "score": score,
        **extra,
    }


def derive(tasks, candidates, **kwargs):
    return derive_matched_records(tasks, candidates, seed=12, reference_id="pinned-base-plus-initial-adapter", **kwargs)


class SourceTaskTests(unittest.TestCase):
    def test_private_metadata_is_never_serialized_into_prompt(self):
        task = source("one", "What is 1 + 1?", topic="private topic", difficulty="private difficulty", selected=True)
        self.assertEqual(task["messages"], [{"role": "user", "content": "What is 1 + 1?"}])
        self.assertNotIn("selected", task)
        self.assertEqual(task["private_gold"]["answer"], "secret gold")

    def test_moving_source_references_and_evaluation_only_sources_fail(self):
        for revision in ("main", "latest", "v1.0", "a" * 39):
            with self.subTest(revision=revision), self.assertRaisesRegex(ValueError, "immutable"):
                source("one", source_revision=revision)
        with self.assertRaisesRegex(ValueError, "evaluation-only"):
            source("one", source_usage="evaluation_only")

    def test_math_unicode_is_not_compatibility_folded(self):
        self.assertNotEqual(normalized_source_text("x²"), normalized_source_text("x2"))
        self.assertEqual(normalized_source_text(" a\r\n b  "), "a b")


class SplitTests(unittest.TestCase):
    def test_exact_and_known_derivations_form_transitive_cluster_before_split(self):
        one = source("one", "Compute   x + y.")
        duplicate = source("duplicate", "Compute x + y.", source_uri="https://example.invalid/other")
        translation = source("translation", "Calculez x + y.")
        distinct = source("distinct")
        records = [one, duplicate, translation, distinct]
        links = {duplicate["task_id"]: "known-source-problem", translation["task_id"]: "known-source-problem"}
        args = dict(seed=18, split_weights={"development": 1, "train": 8, "capability_holdout": 1}, known_clusters=links)
        assigned = assign_task_splits(records, **args)
        self.assertEqual(assigned, assign_task_splits(list(reversed(records)), **args))
        grouped = {task["task_id"]: task for task in assigned}
        cluster_ids = {grouped[task["task_id"]]["dependency_cluster_id"] for task in (one, duplicate, translation)}
        split_ids = {grouped[task["task_id"]]["split"] for task in (one, duplicate, translation)}
        self.assertEqual(len(cluster_ids), 1)
        self.assertEqual(len(split_ids), 1)
        aliases = [grouped[task["task_id"]]["exact_duplicate_of"] for task in (one, duplicate)]
        self.assertEqual(aliases.count(None), 1)
        self.assertNotIn(grouped[distinct["task_id"]]["dependency_cluster_id"], cluster_ids)

    def test_cross_split_content_known_derivations_and_clusters_rejected(self):
        for kind in ("content", "known_derivation", "cluster"):
            with self.subTest(kind=kind):
                if kind == "content":
                    tasks = partition([source("one", "Same problem"), source("two", "Same  problem")])
                    tasks[1]["dependency_cluster_id"] = "forged-independent-cluster"
                else:
                    tasks = partition([source("one"), source("two")])
                    if kind == "known_derivation":
                        for task in tasks:
                            task["known_derivation_labels"] = ["known-shared-source"]
                    else:
                        tasks[1]["dependency_cluster_id"] = tasks[0]["dependency_cluster_id"]
                tasks[1]["split"] = "train"
                with self.assertRaisesRegex(ValueError, "cross-split"):
                    validate_split_integrity(tasks)

    def test_invalid_splits_cluster_links_and_duplicate_ids_rejected(self):
        task = source("one")
        with self.assertRaisesRegex(ValueError, "duplicate task_id"):
            partition([task, task])
        with self.assertRaisesRegex(ValueError, "unknown task_id"):
            partition([task], known_clusters={"absent": "cluster"})
        with self.assertRaisesRegex(ValueError, "unknown split"):
            assign_task_splits([task], seed=1, split_weights={"awareness_eval": 1})
        tasks = partition([task])
        tasks[0]["split"] = "validation"
        with self.assertRaisesRegex(ValueError, "invalid split"):
            validate_split_integrity(tasks)

    def test_exact_duplicate_answers_require_adjudication_if_gold_differs(self):
        tasks = [source("one", "Same task", private_gold={"answer": "2"}), source("two", "Same task", private_gold={"answer": "3"})]
        with self.assertRaisesRegex(ValueError, "conflicting private_gold"):
            partition(tasks)

    def test_changed_prompt_or_source_text_is_rejected(self):
        for field in ("source_text", "messages"):
            with self.subTest(field=field):
                tasks = partition([source("one")])
                tasks[0][field] = "secret gold" if field == "source_text" else [{"role": "user", "content": "secret gold"}]
                with self.assertRaisesRegex(ValueError, "inconsistent"):
                    derive(tasks, [])


class MatchedDatasetTests(unittest.TestCase):
    def test_alias_candidates_cannot_silently_disappear_from_yield(self):
        tasks = partition([source("one", "Same problem"), source("two", "Same problem")])
        alias = next(task for task in tasks if task["exact_duplicate_of"] is not None)
        rows = [candidate(alias, "good", "correct"), candidate(alias, "bad", "incorrect")]
        with self.assertRaisesRegex(ValueError, "canonical exact-duplicate"):
            derive(tasks, rows)

    def test_same_ids_weights_and_chosen_bytes_with_complete_failure_ledger(self):
        tasks = partition([source("one", example_weight=2.5), source("two"), source("three")])
        first, second, third = tasks
        selected_text = "<think>Full reasoning stays intact.\n</think>\n\\boxed{2}  \n"
        rows = [
            candidate(first, "good", "correct", response=selected_text, generator={"revision": "b" * 40}),
            candidate(first, "bad", "incorrect"),
            candidate(first, "refusal", "incorrect", substantive=False),
            *[candidate(first, status, status) for status in ("invalid", "truncated", "unscorable", "scorer_error")],
            candidate(second, "allgood", "correct"),
        ]
        before = copy.deepcopy((tasks, rows))
        result = derive(tasks, rows)
        self.assertEqual((tasks, rows), before)
        self.assertEqual(result, derive(list(reversed(tasks)), list(reversed(rows))))
        self.assertEqual(result["summary"]["retained_tasks"], 1)
        for arm in ("sft", "dpo", "rl"):
            self.assertEqual(len(result[arm]), 1)
            self.assertEqual(result[arm][0]["task_id"], first["task_id"])
            self.assertEqual(result[arm][0]["example_weight"], first["example_weight"])
        self.assertEqual(result["sft"][0]["messages"][-1]["content"], selected_text)
        self.assertEqual(result["sft"][0]["response"], result["dpo"][0]["chosen"])
        self.assertEqual(result["sft"][0]["response_sha256"], result["dpo"][0]["chosen_sha256"])
        for arm in ("sft", "dpo", "rl"):
            prompt = result[arm][0]["messages"]
            if arm == "sft":
                prompt = prompt[:-1]
            self.assertEqual(prompt, first["messages"])
            self.assertNotIn("secret gold", json.dumps(prompt))
            self.assertNotIn("source_revision", json.dumps(prompt))
        self.assertEqual(result["rl"][0]["private_gold"], first["private_gold"])
        failures = result["summary"]["candidate_exclusion_counts"]
        self.assertEqual(set(failures), {"invalid", "truncated", "unscorable", "scorer_error", "non_substantive_incorrect"})
        self.assertEqual(len(result["candidate_ledger"]), len(rows))
        by_id = {row["task_id"]: row for row in result["task_ledger"]}
        self.assertEqual(by_id[second["task_id"]]["exclusion_reason"], "no_substantive_incorrect_candidate")
        self.assertEqual(by_id[third["task_id"]]["exclusion_reason"], "no_candidates")
        self.assertAlmostEqual(result["summary"]["pairable_fraction"], 1 / 2)
        self.assertAlmostEqual(result["summary"]["retained_fraction_of_requested_tasks"], 1 / 3)
        self.assertEqual(result["summary"]["screened_unique_tasks"], 2)
        self.assertEqual(result["summary"]["unscreened_unique_tasks"], 1)
        self.assertEqual(result["construction_status"], "development_only")

    def test_duplicate_source_rows_do_not_add_prompt_multiplicity(self):
        tasks = partition([source("one", "Same task"), source("two", "Same task")])
        canonical = next(task for task in tasks if task["exact_duplicate_of"] is None)
        rows = [candidate(canonical, outcome, outcome) for outcome in ("correct", "incorrect")]
        result = derive(tasks, rows)
        self.assertEqual(len(result["sft"]), 1)
        self.assertEqual(result["summary"]["requested_split_unique_tasks"], 1)
        self.assertEqual(result["summary"]["task_exclusion_counts"], {"exact_duplicate_task": 1})

    def test_seeded_pair_selection_is_independent_of_candidate_input_order(self):
        tasks = partition([source("one")])
        rows = [candidate(tasks[0], str(i), "correct" if i % 2 else "incorrect") for i in range(16)]
        result = derive(tasks, rows)
        self.assertEqual(result, derive(tasks, rows[::2] + rows[1::2]))
        self.assertEqual(result["dpo"][0]["score_gap"], 1.0)

    def test_duplicate_candidate_ids_unknown_tasks_and_mismatches_fail(self):
        tasks = partition([source("one")])
        good = candidate(tasks[0], "good", "correct")
        with self.assertRaisesRegex(ValueError, "duplicate candidate_id"):
            derive(tasks, [good, good])
        for updates, expected in (({"task_id": "unknown"}, "unknown task_id"), ({"split": "train"}, "split does not match"), ({"normalized_text_sha256": "f" * 64}, "content hash does not match")):
            with self.subTest(updates=updates), self.assertRaisesRegex(ValueError, expected):
                derive(tasks, [{**good, **updates}])

    def test_unknown_or_inconsistent_scores_and_mixed_scorers_fail_closed(self):
        tasks = partition([source("one")])
        good = candidate(tasks[0], "good", "correct")
        for updates in ({"status": "unknown"}, {"reward": 0.0}, {"finish_reason": "length"}, {"transport_status": "error"}, {"parse_status": "invalid"}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                derive(tasks, [{**good, "score": {**good["score"], **updates}}])
        bad = candidate(tasks[0], "bad", "incorrect")
        bad["score"]["scorer_version"] = "different_scorer"
        with self.assertRaisesRegex(ValueError, "mixes scorer versions"):
            derive(tasks, [good, bad])

    def test_contradictory_scores_for_identical_response_fail_closed(self):
        tasks = partition([source("one")])
        rows = [candidate(tasks[0], status, status, response="identical response") for status in ("correct", "incorrect")]
        with self.assertRaisesRegex(ValueError, "contradictory correctness"):
            derive(tasks, rows)

    def test_real_scalar_scorer_matches_data_contract(self):
        from emergence.training.math_reward import score_math_response

        tasks = partition([source("one", "What is 1 + 1?", private_gold={"answer": "2"})])
        responses = [r"Reasoning. \\boxed{2}", r"Reasoning. \\boxed{3}", r"Reasoning. \\boxed{2}"]
        # The fixture uses exactly one literal LaTeX command in each response.
        responses = [text.replace("\\\\", "\\") for text in responses]
        rows = []
        for index, response in enumerate(responses):
            finish = "length" if index == 2 else "stop"
            rows.append({
                "candidate_id": str(index),
                "task_id": tasks[0]["task_id"],
                "response": response,
                "substantive": True,
                "score": score_math_response(response, "2", finish_reason=finish).to_dict(),
            })
        result = derive(tasks, rows)
        self.assertEqual(len(result["dpo"]), 1)
        self.assertEqual(result["dpo"][0]["chosen"], responses[0])
        self.assertEqual(result["dpo"][0]["rejected"], responses[1])
        self.assertEqual(result["summary"]["candidate_exclusion_counts"], {"truncated": 1})

    def test_response_groups_cannot_cross_split_boundaries(self):
        tasks = partition([source("one"), source("two")])
        tasks[1]["split"] = "train"
        rows = [candidate(task, "good", "correct", response_group_id="shared-generation-group") for task in tasks]
        with self.assertRaisesRegex(ValueError, "cross-split response group"):
            derive(tasks, rows)

    def test_holdout_and_reserve_cannot_be_derived_as_training_records(self):
        tasks = partition([source("one")])
        for split in ("capability_holdout", "reserve"):
            with self.subTest(split=split), self.assertRaisesRegex(ValueError, "protected"):
                derive(tasks, [], split=split)


if __name__ == "__main__":
    unittest.main()
