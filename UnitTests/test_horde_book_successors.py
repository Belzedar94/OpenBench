from pathlib import Path
import tempfile
import unittest

from Scripts import generate_horde_book_successors as MODULE
from Scripts.generate_horde_book_candidates import RootMove


class HordeBookSuccessorTests(unittest.TestCase):
    def test_reads_epd_and_ignores_operations(self):
        fen = "8/8/8/8/8/8/PP6/4k3 b - - 0 1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.epd"
            path.write_text(f"# comment\n{fen}; bm e1e2;\n\n", encoding="ascii")
            self.assertEqual(list(MODULE.read_epd(path)), [fen])

    def test_advances_black_source_to_white_successor(self):
        fen = "8/8/8/8/8/8/PP6/4k3 b - - 0 1"
        selected = RootMove(multipv=1, move="e1e2", score=0, score_kind="cp", depth=1)
        successor = MODULE.advance_fen(fen, selected)
        self.assertEqual(successor.split()[1], "w")
        self.assertEqual(successor.split()[4], "1")

    def test_rejects_illegal_successor_move(self):
        fen = "8/8/8/8/8/8/PP6/4k3 b - - 0 1"
        selected = RootMove(multipv=1, move="e1e8", score=0, score_kind="cp", depth=1)
        with self.assertRaisesRegex(ValueError, "illegal"):
            MODULE.advance_fen(fen, selected)


if __name__ == "__main__":
    unittest.main()
