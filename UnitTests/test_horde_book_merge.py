import unittest

from Scripts import merge_horde_book_shards as MODULE


def record(name, side, family="a2a3"):
    return {
        "canonical_fen": name,
        "side_to_move": side,
        "prefix_family": family,
    }


class HordeBookMergeTests(unittest.TestCase):
    def test_deduplicates_and_restores_side_balance(self):
        records = [
            record("w1", "white"),
            record("b1", "black"),
            record("w1", "white"),
            record("w2", "white"),
        ]
        merged, duplicates = MODULE.balanced_unique(records)
        self.assertEqual(duplicates, 1)
        self.assertEqual([entry["canonical_fen"] for entry in merged], ["w1", "b1"])

    def test_preserves_order_when_already_balanced(self):
        records = [record("w1", "white"), record("b1", "black", "b2b3")]
        merged, duplicates = MODULE.balanced_unique(records)
        self.assertEqual(duplicates, 0)
        self.assertEqual(merged, records)

    def test_enforces_prefix_cap_and_rebalances(self):
        records = [
            record("w1", "white", "hot"),
            record("b1", "black", "hot"),
            record("w2", "white", "hot"),
            record("b2", "black", "cold"),
        ]
        merged, prefix_trimmed, balance_trimmed, cap = MODULE.enforce_prefix_cap(
            records, 0.25
        )
        self.assertEqual(cap, 1)
        self.assertEqual(prefix_trimmed, 2)
        self.assertEqual(balance_trimmed, 0)
        self.assertEqual([entry["canonical_fen"] for entry in merged], ["w1", "b2"])


if __name__ == "__main__":
    unittest.main()
