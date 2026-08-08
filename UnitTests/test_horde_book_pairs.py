from pathlib import Path
import tempfile
import unittest

from Scripts import analyze_horde_book_pairs as MODULE


def game(round_number, white, black, result, fen, termination=None):
    termination_tag = "" if termination is None else f'[Termination "{termination}"]\n'
    return f'''[Event "?"]
[Round "{round_number}"]
[White "{white}"]
[Black "{black}"]
[Result "{result}"]
[FEN "{fen}"]
{termination_tag}

1... a6 {{0.00 1/1 1 1}} *

'''


class HordeBookPairTests(unittest.TestCase):
    def test_pair_metrics_and_canonical_reuse(self):
        fen1 = "8/8/8/8/8/8/P7/4k3 b - - 0 1"
        fen2 = "8/8/8/8/8/8/1P6/4k3 b - - 0 1"
        pgn = (
            game(1, "engine-dev", "engine-base", "0-1", fen1)
            + game(2, "engine-base", "engine-dev", "0-1", fen1)
            + game(3, "engine-dev", "engine-base", "1-0", fen2)
            + game(4, "engine-base", "engine-dev", "0-1", fen2)
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.pgn"
            path.write_text(pgn, encoding="utf-8")
            payload = MODULE.analyze([path])

        self.assertEqual(payload["complete_pairs"], 2)
        self.assertEqual(payload["pentanomial"], [0, 0, 1, 0, 1])
        self.assertEqual(payload["pair_metrics"]["middle_pair_rate"], 0.5)
        self.assertEqual(payload["pair_metrics"]["black_black_rate"], 0.5)
        self.assertEqual(payload["pair_metrics"]["assignment_decisive_rate"], 0.5)
        self.assertEqual(payload["pair_metrics"]["squared_pair_displacement"], 0.5)
        self.assertEqual(payload["opening_reuse"]["canonical_openings"], 2)
        self.assertEqual(set(payload["opening_side_strata"]), {"black"})
        self.assertEqual(payload["opening_side_strata"]["black"]["pairs"], 2)
        self.assertEqual(
            payload["opening_side_strata"]["black"]["pair_metrics"],
            payload["pair_metrics"],
        )

    def test_stratifies_pairs_by_opening_side_to_move(self):
        black_fen = "8/8/8/8/8/8/P7/4k3 b - - 0 1"
        white_fen = "8/8/8/8/8/8/1P6/4k3 w - - 0 1"
        pgn = (
            game(1, "engine-dev", "engine-base", "0-1", black_fen)
            + game(2, "engine-base", "engine-dev", "0-1", black_fen)
            + game(3, "engine-dev", "engine-base", "1-0", white_fen)
            + game(4, "engine-base", "engine-dev", "1-0", white_fen)
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.pgn"
            path.write_text(pgn, encoding="utf-8")
            payload = MODULE.analyze([path])

        self.assertEqual(set(payload["opening_side_strata"]), {"white", "black"})
        self.assertEqual(
            payload["opening_side_strata"]["black"]["pair_metrics"]["black_black_rate"],
            1.0,
        )
        self.assertEqual(
            payload["opening_side_strata"]["white"]["pair_metrics"]["white_white_rate"],
            1.0,
        )

    def test_rejects_unpaired_openings(self):
        fen1 = "8/8/8/8/8/8/P7/4k3 b - - 0 1"
        fen2 = "8/8/8/8/8/8/1P6/4k3 b - - 0 1"
        pgn = game(1, "engine-dev", "engine-base", "0-1", fen1) + game(
            2, "engine-base", "engine-dev", "0-1", fen2
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.pgn"
            path.write_text(pgn, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "different openings"):
                MODULE.analyze([path])

    def test_reports_recovered_time_forfeits(self):
        fen = "8/8/8/8/8/8/P7/4k3 b - - 0 1"
        pgn = game(
            1, "engine-dev", "engine-base", "0-1", fen, "time forfeit"
        ) + game(2, "engine-base", "engine-dev", "0-1", fen)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.pgn"
            path.write_text(pgn, encoding="utf-8")
            payload = MODULE.analyze([path])

        self.assertEqual(payload["abnormal_terminations"], 1)
        self.assertEqual(payload["terminations"], {"normal": 1, "time forfeit": 1})
        self.assertEqual(payload["abnormal_games"][0]["termination"], "time forfeit")


if __name__ == "__main__":
    unittest.main()
