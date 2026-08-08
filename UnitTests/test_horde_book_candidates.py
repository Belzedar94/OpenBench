import unittest

from Scripts.generate_horde_book_candidates import (
    RootMove,
    choose_move,
    draw_u64,
    parse_root_move,
    select_complete_frame,
)


class HordeBookCandidateTests(unittest.TestCase):
    def test_parses_cp_and_mate_root_moves(self):
        cp = parse_root_move(
            "info depth 8 seldepth 10 multipv 2 score cp -17 nodes 100 pv a2a4 a7a6"
        )
        mate = parse_root_move(
            "info depth 8 seldepth 10 multipv 1 score mate 3 nodes 100 pv b2b4"
        )
        self.assertEqual(cp, RootMove(2, "a2a4", -17, "cp", 8))
        self.assertEqual(mate.move, "b2b4")
        self.assertGreater(mate.score, 90_000)

    def test_counter_draw_is_stable_and_purpose_separated(self):
        self.assertEqual(draw_u64(7, 11, 13, "x"), draw_u64(7, 11, 13, "x"))
        self.assertNotEqual(draw_u64(7, 11, 13, "x"), draw_u64(7, 11, 13, "y"))

    def test_uses_last_depth_with_a_complete_multipv_frame(self):
        lines = [
            "info depth 7 multipv 1 score cp 20 nodes 80 pv a2a4",
            "info depth 7 multipv 2 score cp 10 nodes 80 pv b2b4",
            "info depth 8 multipv 1 score cp 5 nodes 100 pv c2c4",
        ]
        frame = select_complete_frame(lines)
        self.assertEqual([root.move for root in frame], ["a2a4", "b2b4"])
        self.assertEqual({root.depth for root in frame}, {7})

    def test_requires_distinct_root_moves_in_a_complete_frame(self):
        lines = [
            "info depth 7 multipv 1 score cp 20 nodes 80 pv a2a4",
            "info depth 7 multipv 2 score cp 10 nodes 80 pv b2b4",
            "info depth 8 multipv 1 score cp 5 nodes 100 pv c2c4",
            "info depth 8 multipv 2 score cp 5 nodes 100 pv c2c4",
        ]
        frame = select_complete_frame(lines)
        self.assertEqual([root.move for root in frame], ["a2a4", "b2b4"])
        self.assertEqual({root.depth for root in frame}, {7})

    def test_deduplicates_moves_without_discarding_the_deepest_frame(self):
        lines = [
            "info depth 8 multipv 1 score cp 5 nodes 100 pv c2c4",
            "info depth 8 multipv 2 score cp 5 nodes 100 pv c2c4",
            "info depth 8 multipv 3 score cp 4 nodes 100 pv d2d4",
        ]
        frame = select_complete_frame(lines)
        self.assertEqual([root.move for root in frame], ["c2c4", "d2d4"])

    def test_move_choice_never_exceeds_score_cap(self):
        roots = [
            RootMove(1, "a2a4", 100, "cp"),
            RootMove(2, "b2b4", 70, "cp"),
            RootMove(3, "c2c4", 10, "cp"),
        ]
        for trajectory in range(100):
            selected = choose_move(roots, 50, 1, trajectory, 0)
            self.assertIn(selected.move, {"a2a4", "b2b4"})


if __name__ == "__main__":
    unittest.main()
