#!/usr/bin/env python3

import json
import hashlib
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

    def search(self, _position, _go, _budget, _grace, *_strict):
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
            "-variant", "alice",
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

    def acceptance_args(self, directory):
        return [
            "--acceptance-mode",
            "-repeat",
            "-variant", "alice",
            "-concurrency", "1",
            "-games", "2",
            "-engine", "cmd=dev", "name=Alice-dev", "proto=uci",
            "tc=2+0.02", "timemargin=0", "option.Threads=1",
            "option.Hash=512",
            "-engine", "cmd=base", "name=Alice-base", "proto=uci",
            "tc=2+0.02", "timemargin=0", "option.Threads=1",
            "option.Hash=512",
            "-openings", "file=alice.epd", "format=epd",
            "-pgnout", str(Path(directory) / "games.pgn"),
            "--result-jsonl", str(Path(directory) / "pair.jsonl"),
            "--pair-ordinal", "17",
        ]

    def test_acceptance_cli_is_exact_and_rejects_policy_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = uci_pair_runner.parse_cli(self.acceptance_args(directory))
            self.assertTrue(cfg.acceptance_mode)
            self.assertEqual(cfg.variant, "alice")
            self.assertEqual(cfg.games, 2)
            self.assertEqual(cfg.concurrency, 1)
            self.assertEqual(cfg.pair_ordinal, 17)
            for spec in cfg.specs:
                self.assertEqual(spec.options["Threads"], "1")
                self.assertEqual(spec.options["Hash"], "512")
                self.assertNotIn("UCI_Variant", spec.options)

            invalid_suffixes = [
                ["--unknown-setting", "value"],
                ["-resign", "movecount=4", "score=800"],
                ["--stall-draw-cp", "800"],
            ]
            for suffix in invalid_suffixes:
                with self.subTest(suffix=suffix), self.assertRaises(SystemExit):
                    uci_pair_runner.parse_cli(
                        self.acceptance_args(directory) + suffix
                    )

    def test_acceptance_terminal_record_is_authoritative(self):
        white = ScriptedEngine("Alice-dev", [
            (
                "(none)",
                {
                    "cp": -uci_pair_runner.MATE_ISH,
                    "raw_mate": -1,
                    "depth": 1,
                    "seldepth": 1,
                    "nodes": 1,
                    "terminal": {"result": "0-1", "reason": "checkmate"},
                },
                1.0,
            ),
        ])
        black = ScriptedEngine("Alice-base", [])
        cfg = SimpleNamespace(
            acceptance_mode=True,
            max_plies=8,
            fixed_budget_s=1.0,
            stall_grace_s=1.0,
            stall_draw_cp=800,
            resign=None,
            draw=None,
            adj_cp=0,
            adj_plies=4,
            shadow_adjudication=False,
        )
        outcome = uci_pair_runner.play_game(
            white,
            black,
            "8/8/8/8/8/8/4k3/4K3 w - - 0 1",
            cfg,
        )
        self.assertEqual(outcome.result, "0-1")
        self.assertEqual(outcome.outcome_class, "SCORABLE_NATURAL")
        self.assertEqual(outcome.reason, "Black mates")

    def test_acceptance_ambiguous_terminal_is_not_scorable(self):
        white = ScriptedEngine("Alice-dev", [
            (
                "(none)",
                {
                    "cp": 0,
                    "raw_mate": None,
                    "depth": 1,
                    "seldepth": 1,
                    "nodes": 1,
                    "terminal": None,
                },
                1.0,
            ),
        ])
        black = ScriptedEngine("Alice-base", [])
        cfg = SimpleNamespace(
            acceptance_mode=True,
            max_plies=8,
            fixed_budget_s=1.0,
            stall_grace_s=1.0,
            stall_draw_cp=800,
            resign=None,
            draw=None,
            adj_cp=0,
            adj_plies=4,
            shadow_adjudication=False,
        )
        outcome = uci_pair_runner.play_game(
            white,
            black,
            "8/8/8/8/8/8/4k3/4K3 w - - 0 1",
            cfg,
        )
        self.assertEqual(outcome.outcome_class, "PROTOCOL_ABORT")
        self.assertEqual(outcome.failure_code, "missing-terminal-record")

    def test_alice_shadow_audit_uses_the_strict_terminal_protocol(self):
        white = ScriptedEngine("Alice-dev", [
            (
                "(none)",
                {
                    "cp": 0,
                    "raw_mate": None,
                    "depth": 1,
                    "seldepth": 1,
                    "nodes": 1,
                    "terminal": None,
                },
                1.0,
            ),
        ])
        black = ScriptedEngine("Alice-base", [])
        cfg = SimpleNamespace(
            acceptance_mode=False,
            variant="alice",
            max_plies=8,
            fixed_budget_s=1.0,
            stall_grace_s=1.0,
            stall_draw_cp=800,
            resign=None,
            draw=None,
            adj_cp=0,
            adj_plies=4,
            shadow_adjudication=True,
        )
        outcome = uci_pair_runner.play_game(
            white,
            black,
            "8/8/8/8/8/8/4k3/4K3 w - - 0 1",
            cfg,
        )
        self.assertEqual(outcome.outcome_class, "PROTOCOL_ABORT")
        self.assertEqual(
            uci_pair_runner.shadow_audit_failure(outcome),
            "PROTOCOL_ABORT missing-terminal-record",
        )

    def test_shadow_inversion_or_abort_invalidates_the_pair(self):
        clean = uci_pair_runner.Outcome("1/2-1/2", "Draw by rule")
        self.assertIsNone(uci_pair_runner.shadow_audit_failure(clean))
        clean.shadow_inversion = True
        self.assertEqual(
            uci_pair_runner.shadow_audit_failure(clean),
            "SHADOW_INVERSION shadow-inversion",
        )
        failed = uci_pair_runner.Outcome("0-1", "White disconnects")
        failed.outcome_class = "OPERATIONAL_ABORT"
        failed.failure_code = "engine-died"
        self.assertEqual(
            uci_pair_runner.shadow_audit_failure(failed),
            "OPERATIONAL_ABORT engine-died",
        )

    def test_pgn_error_reports_the_machine_failure_class(self):
        headers = [
            '[Variant "alice"]',
            '[OutcomeClass "PROTOCOL_ABORT"]',
            '[FailureCode "missing-terminal-record"]',
            '[Termination "abandoned"]',
        ]
        self.assertEqual(
            worker.PGNHelper.get_error_reason(headers),
            "Alice PROTOCOL_ABORT: missing-terminal-record",
        )

    def test_other_variants_keep_their_plain_termination_reasons(self):
        # spell-chess shares the pair runner, so an engine that dies also
        # produces an OperationalAbort outcome and these headers. Its error
        # reports must stay exactly what the server has always received.
        for termination, expected in [
            ("abandoned", "Disconnect"),
            ("stalled connection", "Stalled"),
            ("illegal move", "Illegal Move"),
        ]:
            headers = [
                '[Variant "spell-chess"]',
                '[OutcomeClass "OPERATIONAL_ABORT"]',
                '[FailureCode "engine-died"]',
                '[Termination "%s"]' % termination,
            ]
            self.assertEqual(
                worker.PGNHelper.get_error_reason(headers), expected
            )

    def test_other_variants_ignore_a_shadow_inversion_header(self):
        headers = [
            '[Variant "spell-chess"]',
            '[ShadowInversion "true"]',
            '[Termination "abandoned"]',
        ]
        self.assertEqual(
            worker.PGNHelper.get_error_reason(headers), "Disconnect"
        )

    def test_machine_pair_excludes_an_anomalous_complete_pair(self):
        natural = uci_pair_runner.Outcome("1/2-1/2", "Draw by rule")
        natural.root_fen = "8/8/8/8/8/8/4k3/4K3 w - - 0 1"
        failed = uci_pair_runner.Outcome(
            "0-1", "White disconnects", "abandoned", restart=True
        )
        failed.root_fen = natural.root_fen
        failed.outcome_class = "OPERATIONAL_ABORT"
        failed.failure_code = "engine-died"
        failed.failure_stage = "search"
        games = {
            1: {"game_no": 1, "dev_score": 0, "outcome": failed},
            2: {"game_no": 2, "dev_score": 1, "outcome": natural},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pair.jsonl"
            path.touch()
            uci_pair_runner.write_machine_pair(path, 23, games)
            record = json.loads(path.read_text(encoding="ascii"))
        evidence_sha = record.pop("evidence_sha256")
        self.assertEqual(
            evidence_sha,
            hashlib.sha256(uci_pair_runner._canonical_json_bytes(record)).hexdigest(),
        )
        self.assertEqual(record["ordinal"], 23)
        self.assertEqual(
            record["game_classes"],
            ["OPERATIONAL_ABORT", "SCORABLE_NATURAL"],
        )
        self.assertEqual(record["game_scores"], [None, 0.5])
        self.assertNotEqual(record["game_scores"], [0.0, 0.5])


if __name__ == "__main__":
    unittest.main()
