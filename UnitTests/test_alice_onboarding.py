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
ENGINE_COMMIT = "627888f33f6d1bc8191e507d8ea413c9c536eba3"
AUDIT_PRESETS = {
    "VSTC Adjudication Audit": ("2.0+0.02", "Hash=16"),
    "STC Adjudication Audit": ("10.0+0.1", "Hash=32"),
    "LTC Adjudication Audit": ("30.0+0.3", "Hash=128"),
}


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


class ScriptedEngine:

    def __init__(self, name, searches):
        self.name = name
        self.spec = SimpleNamespace(
            tc=uci_pair_runner.TimeControl.from_settings({"depth": "1"})
        )
        self.searches = iter(searches)

    def search(self, _position, _go, _budget, _grace):
        return next(self.searches)


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
        self.assertEqual(self.engine["worker_max_concurrency"], 8)

        expected = {
            "VSTC": ("2.0+0.02", 64),
            "STC": ("10.0+0.1", 32),
            "LTC": ("30.0+0.3", 8),
        }
        self.assertEqual(
            set(self.engine["test_presets"]) - {"default"},
            set(expected) | set(AUDIT_PRESETS),
        )
        for name, (time_control, workload_size) in expected.items():
            preset = self.engine["test_presets"][name]
            self.assertEqual(preset["both_time_control"], time_control, name)
            self.assertEqual(preset["workload_size"], workload_size, name)
            self.assertIn("Threads=1", preset["both_options"], name)
            self.assertIn('"Use NNUE=true"', preset["both_options"], name)

    def test_adjudication_audits_are_exactly_two_hundred_pairs(self):
        for name, (time_control, hash_option) in AUDIT_PRESETS.items():
            preset = self.engine["test_presets"][name]
            self.assertEqual(preset["both_time_control"], time_control, name)
            self.assertIn("Threads=1", preset["both_options"], name)
            self.assertIn(hash_option, preset["both_options"], name)
            self.assertIn('"Use NNUE=true"', preset["both_options"], name)
            self.assertEqual(preset["test_max_games"], 400, name)
            self.assertEqual(preset["test_max_games"] // 2, 200, name)
            self.assertEqual(preset["upload_pgns"], "VERBOSE", name)
            self.assertEqual(preset["workload_size"], 1, name)
            self.assertEqual(
                preset["win_adj"],
                "movecount=4 score=800 shadow=true",
                name,
            )
            self.assertEqual(
                preset["draw_adj"],
                "movenumber=40 movecount=8 score=10 shadow=true",
                name,
            )

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

    def test_shadow_flag_is_parsed_from_adjudication_settings(self):
        cfg = uci_pair_runner.parse_cli([
            "-engine", "cmd=dev", "name=Alice-dev", "depth=1",
            "-engine", "cmd=base", "name=Alice-base", "depth=1",
            "-openings", "file=alice.epd", "format=epd",
            "-resign", "movecount=4", "score=800", "shadow=true",
            "-draw", "movenumber=40", "movecount=8", "score=10",
            "shadow=true",
        ])
        self.assertTrue(cfg.shadow_adjudication)
        self.assertEqual(cfg.resign, {"movecount": 4, "score": 800})
        self.assertEqual(
            cfg.draw, {"movenumber": 40, "movecount": 8, "score": 10}
        )

    def test_shadow_adjudication_continues_and_detects_an_inversion(self):
        white = ScriptedEngine("Alice-dev", [
            (
                "e2e4",
                {
                    "cp": -900,
                    "raw_mate": None,
                    "depth": 1,
                    "seldepth": 1,
                    "nodes": 1,
                },
                1.0,
            ),
        ])
        black = ScriptedEngine("Alice-base", [
            (
                "(none)",
                {
                    "cp": -uci_pair_runner.MATE_ISH,
                    "raw_mate": -1,
                    "depth": 1,
                    "seldepth": 1,
                    "nodes": 1,
                },
                1.0,
            ),
        ])
        cfg = SimpleNamespace(
            max_plies=8,
            fixed_budget_s=1.0,
            stall_grace_s=1.0,
            stall_draw_cp=800,
            resign={"movecount": 1, "score": 800},
            draw=None,
            adj_cp=0,
            adj_plies=4,
            shadow_adjudication=True,
        )

        outcome = uci_pair_runner.play_game(
            white,
            black,
            "8/8/8/8/8/8/4P3/4K2k w - - 0 1",
            cfg,
        )

        self.assertEqual(outcome.result, "1-0")
        self.assertEqual(
            outcome.shadow, {"result": "0-1", "ply": 1, "kind": "resign"}
        )
        self.assertTrue(outcome.shadow_inversion)
        self.assertEqual(outcome.plies, 1)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shadow.pgn"
            uci_pair_runner.write_pgn(
                path,
                1,
                white.name,
                black.name,
                "8/8/8/8/8/8/4P3/4K2k w - - 0 1",
                outcome,
                "2.0+0.02",
                "alice",
            )
            pgn = path.read_text(encoding="ascii")
        self.assertIn('[ShadowAdjudication "0-1 at ply 1 by resign"]', pgn)
        self.assertIn('[ShadowInversion "true"]', pgn)


if __name__ == "__main__":
    unittest.main()
