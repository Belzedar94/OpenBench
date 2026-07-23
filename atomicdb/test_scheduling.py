import itertools
import math
import unittest

from . import scheduling


class BasePriorityTests(unittest.TestCase):

    @staticmethod
    def reference(eval_cp, regret_cp, expanded, visits):
        evaluation = abs(eval_cp) if eval_cp is not None else 0
        runits = (
            5
            if regret_cp is None or regret_cp == float("inf")
            else min(regret_cp, 3000) / 100.0
        )
        return (
            min(evaluation, 1500) / 100.0
            + (50.0 if evaluation >= 9000 else 0.0)
            + (2.0 if not expanded else 0.0)
            - 3.0 * runits
            - 1.5 * visits
        )

    def test_matches_current_ingest_formula(self):
        for eval_cp, regret_cp, expanded, visits in itertools.product(
                (None, 0, 800, -800, 8999, 9000, 9997),
                (None, float("-inf"), 0.0, 200.0, 3000.0, 4000.0,
                 float("inf")),
                (False, True),
                (0, 1, 4)):
            with self.subTest(
                    eval_cp=eval_cp,
                    regret_cp=regret_cp,
                    expanded=expanded,
                    visits=visits):
                self.assertEqual(
                    scheduling.base_priority(
                        eval_cp, regret_cp, expanded, visits),
                    self.reference(eval_cp, regret_cp, expanded, visits),
                )

    def test_disconnected_unanalysed_base_value(self):
        self.assertEqual(
            scheduling.base_priority(None, float("inf"), False, 0),
            -13.0,
        )


class TheoryBoostTests(unittest.TestCase):

    def test_tiers_and_cap(self):
        self.assertEqual(scheduling.theory_boost(["P0"]), 12.0)
        self.assertEqual(scheduling.theory_boost(["P1"]), 8.0)
        self.assertEqual(scheduling.theory_boost(["P2"]), 4.0)
        self.assertEqual(scheduling.theory_boost(["P3"]), 1.0)
        self.assertEqual(scheduling.theory_boost(["P1", "P2"]), 8.0)
        self.assertEqual(
            scheduling.theory_boost([
                ("critical", "P0"),
                ("secondary", "P1"),
                ("residual", "P3"),
            ]),
            12.0,
        )
        self.assertEqual(
            scheduling.theory_boost([("critical", "P0")], max_boost=6.0),
            6.0,
        )

    def test_duplicate_cohort_does_not_inflate_boost(self):
        cohorts = [
            ("two-knights", "P2"),
            ("two-knights", "P2"),
            ("two-knights", "P1"),
        ]
        self.assertEqual(scheduling.theory_boost(cohorts), 8.0)

    def test_multi_cohort_order_is_deterministic(self):
        cohorts = [
            scheduling.TheoryCohort("zeta", "P3"),
            scheduling.TheoryCohort("alpha", "P2"),
            scheduling.TheoryCohort("middle", "P3"),
        ]
        expected = scheduling.theory_boost(cohorts)
        for permutation in itertools.permutations(cohorts):
            self.assertEqual(scheduling.theory_boost(permutation), expected)

    def test_decay_starts_at_first_threshold_and_hits_zero_at_either_limit(self):
        self.assertEqual(scheduling.theory_decay_factor(0, 0), 1.0)
        self.assertEqual(scheduling.theory_decay_factor(3, 25), 1.0)
        self.assertEqual(scheduling.theory_decay_factor(4, 0), 0.5)
        self.assertEqual(scheduling.theory_decay_factor(0, 37.5), 0.5)
        self.assertEqual(scheduling.theory_decay_factor(4, 37.5), 0.5)
        self.assertEqual(scheduling.theory_decay_factor(5, 0), 0.0)
        self.assertEqual(scheduling.theory_decay_factor(0, 50), 0.0)
        self.assertEqual(scheduling.theory_boost(["P0"], 5, 0), 0.0)
        self.assertEqual(scheduling.theory_boost(["P0"], 0, 50), 0.0)

    def test_decay_uses_attempts_or_core_hours_whichever_is_staler(self):
        self.assertEqual(scheduling.theory_decay_factor(4, 10), 0.5)
        self.assertEqual(scheduling.theory_decay_factor(2, 30), 0.8)
        self.assertEqual(scheduling.theory_decay_factor(4, 45), 0.2)

    def test_invalid_decay_and_tier_fail_closed(self):
        with self.assertRaises(ValueError):
            scheduling.theory_decay_factor(-1, 0)
        with self.assertRaises(ValueError):
            scheduling.theory_decay_factor(0, math.inf)
        with self.assertRaises(ValueError):
            scheduling.theory_boost(["P9"])
        with self.assertRaises(ValueError):
            scheduling.theory_boost(["P0"], max_boost=math.nan)
        with self.assertRaises(ValueError):
            scheduling.theory_boost(["P0"], max_boost=13)


class SchedulingScoreTests(unittest.TestCase):

    def test_source_rank_is_explicit(self):
        self.assertGreater(
            scheduling.source_rank("USER"),
            scheduling.source_rank("THEORY"),
        )
        self.assertGreater(
            scheduling.source_rank("THEORY"),
            scheduling.source_rank("AUTO"),
        )
        self.assertEqual(scheduling.source_rank("user"), 2)
        with self.assertRaises(ValueError):
            scheduling.source_rank("UNKNOWN")

    def test_shadow_reports_boost_but_preserves_base_selection(self):
        cohorts = [("two-knights", "P1"), ("d6-canary", "P2")]
        snapshot = list(cohorts)
        score = scheduling.score_candidate(
            eval_cp=800,
            regret_cp=200,
            expanded=True,
            visits=2,
            source="THEORY",
            cohorts=cohorts,
            shadow=True,
        )
        self.assertEqual(cohorts, snapshot)
        self.assertEqual(score.base_priority, -1.0)
        self.assertEqual(score.theory_boost, 8.0)
        self.assertEqual(score.proposed_priority, 7.0)
        self.assertEqual(score.selection_priority, score.base_priority)
        self.assertTrue(score.shadow)

    def test_non_shadow_applies_proposed_priority_only(self):
        score = scheduling.score_candidate(
            eval_cp=100,
            regret_cp=0,
            expanded=True,
            visits=0,
            source="THEORY",
            cohorts=["P2"],
            shadow=False,
        )
        self.assertEqual(score.base_priority, 1.0)
        self.assertEqual(score.proposed_priority, 5.0)
        self.assertEqual(score.selection_priority, 5.0)
        self.assertFalse(score.shadow)

    def test_scheduler_key_orders_source_then_priority_then_id(self):
        auto = scheduling.score_candidate(
            eval_cp=9997,
            regret_cp=0,
            expanded=True,
            visits=0,
            source="AUTO",
        )
        theory = scheduling.score_candidate(
            eval_cp=0,
            regret_cp=0,
            expanded=True,
            visits=0,
            source="THEORY",
        )
        user = scheduling.score_candidate(
            eval_cp=0,
            regret_cp=0,
            expanded=True,
            visits=0,
            source="USER",
        )
        candidates = [("auto", auto), ("user", user), ("theory", theory)]
        ordered = sorted(
            candidates,
            key=lambda item: scheduling.scheduler_sort_key(*item),
        )
        self.assertEqual(
            [candidate_id for candidate_id, _ in ordered],
            ["user", "theory", "auto"],
        )

        tied = [("zeta", theory), ("alpha", theory)]
        self.assertEqual(
            [
                candidate_id
                for candidate_id, _ in sorted(
                    tied,
                    key=lambda item: scheduling.scheduler_sort_key(*item),
                )
            ],
            ["alpha", "zeta"],
        )


if __name__ == "__main__":
    unittest.main()
