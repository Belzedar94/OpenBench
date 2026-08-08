import unittest

from Scripts import select_horde_book_candidates as MODULE


def record(name, gap):
    return {
        "canonical_fen": f"{name}/8/8/8/8/8/8/8 w - - 0",
        "top_two_gap": gap,
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
