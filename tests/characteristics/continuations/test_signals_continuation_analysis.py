import copy
import itertools
import unittest

from characteristics.continuations.continuation_analysis import COMPARISONS
from characteristics.continuations.continuation_analysis import COUNTERS
from characteristics.continuations.continuation_analysis import summarize_continuations


def panel():
    return [{"branch_id": f"{family}-development-{index:03d}-r000--{arm}",
             "parent_id": f"{family}-development-{index:03d}-r000", "family": family,
             "case_index": index, "arm": arm, "historical_label": None,
             "episode_status": "uncalled", "complete_history": False,
             "verified_completed_episode": False, "functional_correct": None,
             "constraint_compliant": None, "parent_comparisons": dict.fromkeys(COMPARISONS),
             "new_usage": dict.fromkeys(COUNTERS)}
            for family in ("ledger", "validator") for index in range(4)
            for arm in ("original", "new-1", "new-2", "new-3")]


def observed(row, value):
    row.update(historical_label=value, episode_status="completed" if value is not None else "unknown",
               complete_history=value is not None, verified_completed_episode=value is not None)


class ContinuationAnalysisTests(unittest.TestCase):
    def test_all_missing_retains_design_and_sharp_binary_triangle_bound(self):
        result = summarize_continuations(panel())
        self.assertEqual(result["all_assigned"]["assigned"],32)
        self.assertEqual(result["original"]["assigned"],8)
        self.assertEqual(result["new"]["assigned"],24)
        pairs = result["new_seed_disagreement"]["pooled"]
        self.assertEqual((pairs["known_pairs"],pairs["all_pairs"]),(0,24))
        self.assertEqual((pairs["minimum_disagreements"],pairs["maximum_disagreements"]),(0,16))
        self.assertIsNone(pairs["known_disagreement_rate"])
        self.assertEqual(pairs["disagreement_upper"],2/3)
        for family in result["families"].values():
            self.assertEqual(family["new_seed_disagreement"]["pooled"]["all_pairs"],12)
            self.assertEqual(family["original"]["assigned"],4)

    def test_all_27_binary_missingness_patterns_against_explicit_pair_enumeration(self):
        for values in itertools.product((False,True,None),repeat=3):
            rows=panel()
            for offset,value in enumerate(values,1):observed(rows[offset],value)
            result=summarize_continuations(rows)["new_seed_disagreement"]["cases"][0]
            possible=[sum(a!=b for a,b in itertools.combinations(completed,2))
                      for completed in itertools.product(*[(False,True) if value is None else (value,) for value in values])]
            self.assertEqual(result["minimum_disagreements"],min(possible),values)
            self.assertEqual(result["maximum_disagreements"],max(possible),values)
            known=[value for value in values if value is not None]
            self.assertEqual(result["known_disagreements"],sum(a!=b for a,b in itertools.combinations(known,2)),values)

    def test_original_controls_never_enter_new_seed_disagreement(self):
        rows=panel()
        for row in rows:observed(row,row["arm"]=="original")
        result=summarize_continuations(rows)
        self.assertEqual(result["new_seed_disagreement"]["pooled"]["maximum_disagreements"],0)
        self.assertEqual(result["original"]["outcomes"]["historical_label"]["true"],8)
        self.assertEqual(result["new"]["outcomes"]["historical_label"]["false"],24)
        self.assertEqual(result,summarize_continuations(list(reversed(rows))))

    def test_incomplete_positive_and_known_usage_are_retained(self):
        rows=panel();row=rows[1]
        row.update(historical_label=True,episode_status="interrupted",new_usage={"prompt_tokens":17,"completion_tokens":3,"completed_calls":1})
        row["parent_comparisons"]["historical_label"]=False
        result=summarize_continuations(rows)["new"]
        self.assertEqual(result["outcomes"]["historical_label"]["true"],1)
        self.assertEqual(result["outcomes"]["historical_label"]["unknown"],23)
        self.assertEqual(result["new_usage"]["completion_tokens"],{"known_branches":1,"missing_branches":23,"sum_over_known_branches":3})
        self.assertEqual(result["parent_comparisons"]["historical_label"]["false"],1)

    def test_invalid_labels_and_denominators_refused(self):
        for mutate in [lambda rows:rows.pop(),lambda rows:rows.__setitem__(0,copy.deepcopy(rows[1])),
                       lambda rows:rows[0].update(historical_label=False,episode_status="call_limit"),
                       lambda rows:rows[0].update(historical_label=0),
                       lambda rows:rows[0].update(historical_label=True,episode_status="invalid_evidence"),
                       lambda rows:rows[0]["new_usage"].update(completed_calls=0),
                       lambda rows:rows[0].update(case_index=True),
                       lambda rows:rows[0].update(episode_status="unknown",parent_comparisons={**dict.fromkeys(COMPARISONS),"historical_label":True}),
                       lambda rows:rows[0]["parent_comparisons"].pop("first_response_bytes")]:
            rows=panel();mutate(rows)
            with self.assertRaises(ValueError):summarize_continuations(rows)


if __name__ == "__main__":
    unittest.main()
