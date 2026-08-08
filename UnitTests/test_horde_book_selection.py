import json
from pathlib import Path
import tempfile
import unittest

from Scripts import select_horde_book_candidates as MODULE


def record(name, gap, score=0, side="white"):
    return {
        "canonical_fen": f"{name}/8/8/8/8/8/8/8 w - - 0",
        "top_two_gap": gap,
        "best_score": score,
        "side_to_move": side,
    }


class HordeBookSelectionTests(unittest.TestCase):
    def test_selects_only_records_at_or_below_gap(self):
        records = [record("zero", 0), record("one", 1), record("five", 5)]
        selected = MODULE.select_by_gap(records, 1)
        self.assertEqual(
            [entry["top_two_gap"] for entry in selected], [0, 1]
        )

    def test_gap_zero_is_an_exact_zero_selection(self):
        records = [record("zero", 0), record("one", 1)]
        selected = MODULE.select_by_gap(records, 0)
        self.assertEqual([entry["top_two_gap"] for entry in selected], [0])

    def test_rejects_missing_or_negative_gaps(self):
        with self.assertRaisesRegex(ValueError, "missing top_two_gap"):
            MODULE.select_by_gap([{"canonical_fen": "missing"}], 0)
        with self.assertRaisesRegex(ValueError, "non-negative"):
            MODULE.select_by_gap([record("negative", -1)], 0)
        with self.assertRaisesRegex(ValueError, "non-negative"):
            MODULE.select_by_gap([record("zero", 0)], -1)

    def test_selects_white_relative_evaluation_band(self):
        records = [
            record("white-low", 0, 79, "white"),
            record("white-in", 0, 80, "white"),
            record("black-in", 0, -200, "black"),
            record("black-high", 0, -201, "black"),
        ]
        selected = MODULE.select_by_white_eval(records, 80, 200)
        self.assertEqual(
            [entry["canonical_fen"].split("/")[0] for entry in selected],
            ["white-in", "black-in"],
        )

    def test_external_white_evaluation_overrides_generation_score(self):
        outside = record("outside", 0, 100, "white")
        outside["selection_white_eval"] = 79
        inside = record("inside", 0, -500, "black")
        inside["selection_white_eval"] = 120
        selected = MODULE.select_by_white_eval([outside, inside], 80, 200)
        self.assertEqual(
            [entry["canonical_fen"].split("/")[0] for entry in selected],
            ["inside"],
        )

    def test_loads_verified_external_evaluation_screen(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            trace = directory / "screening.jsonl"
            canonical = "8/8/8/8/8/8/8/8 b - - 0"
            trace.write_text(
                json.dumps(
                    {
                        "fen": canonical + " 1",
                        "canonical_fen": canonical,
                        "roots": [{"score": -123, "score_kind": "cp"}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            manifest = {
                "schema": "HORDE_BOOK_MULTIPV_SCREEN_V1",
                "source_sha256": "source-hash",
                "counts": {"canonical_sources": 1},
                "outputs": {
                    "traces": {
                        "path": trace.name,
                        "sha256": MODULE.sha256_file(trace),
                    }
                },
            }
            (directory / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            _, scores = MODULE.load_evaluation_screen(directory, "source-hash")
            self.assertEqual(scores, {canonical: 123})

            with self.assertRaisesRegex(ValueError, "source hash mismatch"):
                MODULE.load_evaluation_screen(directory, "different-source")

    def test_rejects_invalid_white_evaluation_records(self):
        with self.assertRaisesRegex(ValueError, "min_eval"):
            MODULE.select_by_white_eval([record("zero", 0)], 1, 0)
        with self.assertRaisesRegex(ValueError, "missing best_score"):
            MODULE.select_by_white_eval([{"side_to_move": "white"}], 0, 1)
        with self.assertRaisesRegex(ValueError, "invalid side_to_move"):
            MODULE.select_by_white_eval([record("zero", 0, 0, "other")], 0, 1)

    def test_excludes_normalized_referee_positions(self):
        records = [record("zero", 0), record("one", 0)]
        key = MODULE.position_key("zero/8/8/8/8/8/8/8 w - h6 17 42")
        selected, excluded = MODULE.exclude_positions(records, {key})
        self.assertEqual(excluded, 1)
        self.assertEqual(len(selected), 1)
        self.assertTrue(str(selected[0]["canonical_fen"]).startswith("one/"))

    def test_excludes_all_normalized_position_aliases(self):
        records = [
            {
                "canonical_fen": "same/8/8/8/8/8/8/8 w - h6 0",
                "top_two_gap": 0,
            },
            {
                "canonical_fen": "same/8/8/8/8/8/8/8 w - - 4",
                "top_two_gap": 0,
            },
        ]
        key = MODULE.position_key("same/8/8/8/8/8/8/8 w - - 0")
        selected, excluded = MODULE.exclude_positions(records, {key})
        self.assertEqual(selected, [])
        self.assertEqual(excluded, 2)


if __name__ == "__main__":
    unittest.main()
