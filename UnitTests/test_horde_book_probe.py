from pathlib import Path
import unittest

from Scripts import run_horde_book_probe as MODULE


class HordeBookProbeTests(unittest.TestCase):
    def test_command_keeps_paths_as_single_arguments(self):
        engine = Path("C:/Engine Dir/stockfish.exe")
        book = Path("C:/Books/horde.epd")
        command = MODULE.build_command(
            referee=Path("C:/Program Files/cutechess.exe"),
            engine=engine,
            network=Path("C:/Nets/run6b.nnue"),
            book=book,
            pgn=Path("C:/Output/games.pgn"),
            label="probe label",
            seed=7,
            games=80,
            strong_nodes=50_000,
            weak_nodes=40_000,
            concurrency=2,
        )
        self.assertIn(f"cmd={engine.resolve()}", command)
        self.assertIn(f"file={book.resolve()}", command)
        self.assertIn("probe label", command)

    def test_validation_requires_exact_complete_pairs(self):
        valid = {"games": 80, "incomplete_games": 0, "complete_pairs": 40}
        self.assertTrue(MODULE.valid_probe(0, valid, 80))
        self.assertFalse(MODULE.valid_probe(1, valid, 80))
        self.assertFalse(
            MODULE.valid_probe(
                0, {"games": 78, "incomplete_games": 0, "complete_pairs": 39}, 80
            )
        )


if __name__ == "__main__":
    unittest.main()
