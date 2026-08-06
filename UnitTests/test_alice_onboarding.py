#!/usr/bin/env python3

import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Client"))

import uci_pair_runner
import worker


BOOK_SHA256 = "BCD89D9FC3EA81FEB95932EB64D6B6F15AD25CC04CDCC9E0440F097CFFB8CCF6"
NETWORK = "alice_run2rl_e40_l09.nnue"
ENGINE_COMMIT = "b8a37122d7f674d3c75ebd1bb362ae01b670e32e"


def load_json(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def routing_config(book, engine="Alice-Stockfish"):
    return SimpleNamespace(
        workload={
            "test": {
                "book": {"name": book},
                "dev": {"engine": engine},
                "base": {"engine": engine},
            }
        }
    )


class AliceOnboardingTests(unittest.TestCase):

    def setUp(self):
        self.general = load_json("Config/config.json")
        self.engine = load_json("Engines/Alice-Stockfish.json")
        self.book = load_json("Books/ALICE_openings.epd.json")

    def test_engine_and_book_are_registered_in_priority_order(self):
        engines = self.general["engines"]
        self.assertLess(engines.index("Spell-Stockfish"), engines.index("Alice-Stockfish"))
        self.assertLess(engines.index("Atomic-Stockfish"), engines.index("Alice-Stockfish"))
        self.assertIn("ALICE_openings.epd", self.general["books"])

    def test_book_is_pinned_to_the_public_asset(self):
        self.assertEqual(self.book["sha"].upper(), BOOK_SHA256)
        self.assertEqual(self.book["raw_sha"].upper(), BOOK_SHA256)
        self.assertEqual(
            self.book["source"],
            "https://github.com/Belzedar94/Alice-Stockfish/releases/download/"
            "openbench-assets-v1/ALICE_openings.epd.zip",
        )

    def test_engine_build_and_presets_are_frozen(self):
        defaults = self.engine["test_presets"]["default"]
        self.assertEqual(self.engine["source"], "https://github.com/Belzedar94/Alice-Stockfish")
        self.assertEqual(self.engine["build"]["cpuflags"], [])
        self.assertEqual(set(self.engine["build"]["systems"]), {"Windows", "Linux"})
        self.assertEqual(defaults["both_bench"], 162582)
        self.assertEqual(defaults["both_network"], NETWORK)
        self.assertEqual(defaults["base_branch"], ENGINE_COMMIT)
        self.assertEqual(
            self.engine["tune_presets"]["default"]["dev_branch"], ENGINE_COMMIT
        )
        self.assertEqual(defaults["book_name"], "ALICE_openings.epd")
        self.assertEqual(defaults["priority"], -10)
        self.assertEqual(defaults["win_adj"], "movecount=4 score=800")
        self.assertEqual(defaults["draw_adj"], "movenumber=40 movecount=8 score=10")
        self.assertEqual(self.engine["datagen_presets"], {"default": {}})

        expected = {
            "VSTC": ("2.0+0.02", 64),
            "STC": ("10.0+0.1", 32),
            "LTC": ("30.0+0.3", 8),
        }
        self.assertEqual(
            set(self.engine["test_presets"]) - {"default"}, set(expected)
        )
        for name, (time_control, workload_size) in expected.items():
            preset = self.engine["test_presets"][name]
            self.assertEqual(preset["both_time_control"], time_control, name)
            self.assertEqual(preset["workload_size"], workload_size, name)
            self.assertIn("Threads=1", preset["both_options"], name)
            self.assertIn('"Use NNUE=true"', preset["both_options"], name)

    def test_book_and_engine_fallback_route_to_the_pair_runner(self):
        self.assertEqual(
            worker.variant_routing(routing_config("ALICE_openings.epd")),
            ("uci-pair-runner", "alice"),
        )
        self.assertEqual(
            worker.variant_routing(routing_config("None")),
            ("uci-pair-runner", "alice"),
        )

    def test_pgn_records_the_alice_variant(self):
        outcome = uci_pair_runner.Outcome("1/2-1/2", "Draw by stalemate")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alice.pgn"
            uci_pair_runner.write_pgn(
                path,
                1,
                "Alice-dev",
                "Alice-base",
                "8/8/8/8/8/8/6K1/7k w - - 0 1",
                outcome,
                "2.0+0.02",
                "alice",
            )
            pgn = path.read_text(encoding="ascii")
        self.assertIn('[Variant "alice"]', pgn)


if __name__ == "__main__":
    unittest.main()
