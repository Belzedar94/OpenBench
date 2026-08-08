import unittest

from Scripts import screen_horde_book_candidates as MODULE
from Scripts.generate_horde_book_candidates import RootMove


def root(multipv, score, kind="cp"):
    return RootMove(multipv=multipv, move="a1a2", score=score, score_kind=kind, depth=8)


class HordeBookScreenTests(unittest.TestCase):
    def test_accepts_narrow_finite_roots(self):
        self.assertEqual(MODULE.screen_reason([root(1, 30), root(2, 20)], 15, 400), "accepted")

    def test_rejects_wide_gap_and_large_score(self):
        self.assertEqual(
            MODULE.screen_reason([root(1, 30), root(2, 10)], 15, 400),
            "wide_top_two_gap",
        )
        self.assertEqual(
            MODULE.screen_reason([root(1, 401), root(2, 400)], 15, 400),
            "large_absolute_score",
        )

    def test_rejects_mate_or_incomplete_frame(self):
        self.assertEqual(MODULE.screen_reason([root(1, 30)], 15, 400), "incomplete_multipv")
        self.assertEqual(
            MODULE.screen_reason([root(1, 99999, "mate"), root(2, 10)], 15, 400),
            "mate_score",
        )


if __name__ == "__main__":
    unittest.main()
