#!/usr/bin/env python3

import concurrent.futures
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Client"))

import worker


class FakeStdout:

    def __init__(self, lines, on_eof=None):
        self.lines = iter([line.encode("utf-8") for line in lines] + [b""])
        self.on_eof = on_eof

    def readline(self):
        line = next(self.lines)
        if not line and self.on_eof:
            self.on_eof()
        return line


class FakeProcess:

    def __init__(self, lines, returncode, on_eof=None):
        self.stdout = FakeStdout(lines, on_eof)
        self.returncode = returncode

    def wait(self):
        return self.returncode


def routing_config(
    book,
    dev_stagger_ms=0,
    base_stagger_ms=0,
    dev_engine="Atomic-Stockfish",
    base_engine="Fairy-Stockfish-Atomic-Baseline",
    variant_contract=None,
):
    test = {
        "type": "TEST",
        "book": {"name": book},
        "dev": {
            "engine": dev_engine,
            "cutechess_launch_stagger_ms": dev_stagger_ms,
        },
        "base": {
            "engine": base_engine,
            "cutechess_launch_stagger_ms": base_stagger_ms,
        },
    }
    if variant_contract is not None:
        test["variant_contract"] = variant_contract
        test["book"]["variant_contract"] = variant_contract
        test["dev"]["variant_contract"] = variant_contract
        test["base"]["variant_contract"] = variant_contract
    return SimpleNamespace(workload={"test": test})


class HordeVariantRoutingTests(unittest.TestCase):

    def test_horde_book_routes_to_native_cutechess(self):
        config = routing_config(
            "HORDE_openings.epd",
            dev_engine="Horde-Stockfish",
            base_engine="Fairy-Stockfish-Hordetest-Baseline",
        )
        with self.assertRaisesRegex(
            worker.VariantRoutingError, "require variant_contract"
        ):
            worker.variant_routing(config)

    def test_both_horde_engine_names_route_without_a_book_token(self):
        for engine in (
            "Horde-Stockfish",
            "Fairy-Stockfish-Hordetest-Baseline",
        ):
            with self.subTest(engine=engine):
                config = routing_config(
                    "None", dev_engine=engine, base_engine=engine
                )
                with self.assertRaisesRegex(
                    worker.VariantRoutingError, "require variant_contract"
                ):
                    worker.variant_routing(config)

    def test_explicit_horde_contract_routes_without_name_inference(self):
        config = routing_config(
            "openings.epd",
            dev_engine="PrivateDev",
            base_engine="PrivateBase",
            variant_contract="LICHESS_HORDE_V1",
        )
        self.assertEqual(worker.variant_routing(config), ("cutechess", "horde"))

    def test_horde_contract_rejects_an_atomic_book(self):
        config = routing_config(
            "ATOMIC_openings.epd",
            dev_engine="Horde-Stockfish",
            base_engine="Fairy-Stockfish-Hordetest-Baseline",
            variant_contract="LICHESS_HORDE_V1",
        )
        with self.assertRaisesRegex(
            worker.VariantRoutingError, "conflicts with inferred route"
        ):
            worker.variant_routing(config)

    def test_conflicting_side_contracts_are_rejected(self):
        config = routing_config("HORDE_openings.epd")
        config.workload["test"]["dev"]["variant_contract"] = "LICHESS_HORDE_V1"
        config.workload["test"]["base"]["variant_contract"] = "ATOMIC_V1"
        with self.assertRaisesRegex(
            worker.VariantRoutingError, "conflicting variant contracts"
        ):
            worker.variant_routing(config)

    def test_unknown_workload_preserves_standard_fallback(self):
        config = routing_config(
            "openings.epd", dev_engine="UnknownDev", base_engine="UnknownBase"
        )
        self.assertEqual(
            worker.variant_routing(config), ("cutechess", "standard")
        )


class TerachessVariantRoutingTests(unittest.TestCase):

    def test_book_token_and_engine_fallback_use_the_pair_runner(self):
        tagged = routing_config(
            "TERACHESS_openings_v1.epd",
            dev_engine="Terachess-Stockfish",
            base_engine="Terachess-Stockfish",
        )
        untagged = routing_config(
            "tera_openings_v1.epd",
            dev_engine="Terachess-Stockfish",
            base_engine="Terachess-Stockfish",
        )
        self.assertEqual(
            worker.variant_routing(tagged),
            ("uci-pair-runner", "terachess"),
        )
        self.assertEqual(
            worker.variant_routing(untagged),
            ("uci-pair-runner", "terachess"),
        )

    def test_pair_runner_receives_the_measured_1200_ply_cap(self):
        config = routing_config(
            "tera_openings_v1.epd",
            dev_engine="Terachess-Stockfish",
            base_engine="Terachess-Stockfish",
        )
        self.assertEqual(
            worker.Cutechess.basic_settings(config),
            "-repeat -recover -variant terachess --max-plies 1200",
        )

    def test_terachess_policy_does_not_leak_to_other_runners(self):
        spell = worker.Cutechess.basic_settings(
            routing_config(
                "spell_openings.epd",
                dev_engine="Spell-Stockfish",
                base_engine="Spell-Stockfish",
            )
        )
        atomic = worker.Cutechess.basic_settings(
            routing_config("ATOMIC_openings.epd")
        )
        self.assertNotIn("--max-plies", spell)
        self.assertNotIn("--max-plies", atomic)


class CutechessLaunchStaggerTests(unittest.TestCase):

    def test_native_runner_uses_an_explicit_worker_local_path(self):
        config = routing_config("ATOMIC_openings.epd")
        expected_name = ["cutechess-ob.exe", "cutechess-ob"][worker.IS_LINUX]

        with tempfile.TemporaryDirectory() as cwd:
            previous = os.getcwd()
            os.chdir(cwd)
            try:
                command = worker.runner_base_command(config)
                argv = worker.cutechess_command_argv(command)
            finally:
                os.chdir(previous)

        self.assertEqual(
            Path(argv[0]).resolve(),
            (Path(worker.__file__).resolve().parent / expected_name).resolve(),
        )
        self.assertTrue(Path(argv[0]).is_absolute())

    def test_atomic_staggers_copies_by_larger_requested_interval(self):
        config = routing_config("ATOMIC_openings.epd", 1000, 1500)
        self.assertEqual(
            worker.Cutechess.launch_stagger_seconds(config, 2), 3.0
        )

    def test_default_stagger_preserves_original_launch(self):
        config = routing_config("ordinary.epd")
        self.assertEqual(
            worker.Cutechess.launch_stagger_seconds(config, 2), 0.0
        )

    def test_spell_custom_runner_is_not_staggered(self):
        config = routing_config("SPELL_openings.epd", 250, 250)
        self.assertEqual(
            worker.Cutechess.launch_stagger_seconds(config, 2), 0.0
        )

    def test_abort_during_stagger_does_not_launch_a_runner(self):
        config = routing_config("ATOMIC_openings.epd", 1500, 1500)
        abort_flag = threading.Event()
        abort_flag.set()
        with mock.patch.object(worker, "Popen") as popen:
            result = worker.run_and_parse_cutechess(
                config,
                "cutechess-ob.exe -variant atomic",
                1,
                queue.Queue(),
                abort_flag,
            )
        self.assertIsNone(result)
        popen.assert_not_called()


class CutechessStartupDiagnosticsTests(unittest.TestCase):

    @staticmethod
    def run_fake(
        lines, returncode, expected_games=None, abort_on_eof=False
    ):
        abort_flag = threading.Event()
        process = FakeProcess(
            lines,
            returncode,
            abort_flag.set if abort_on_eof else None,
        )
        config = routing_config("ATOMIC_openings.epd")
        config.workload["distribution"] = {}
        if expected_games is not None:
            config.workload["distribution"][
                "games-per-cutechess"
            ] = expected_games
        with mock.patch.object(worker, "Popen", return_value=process) as popen:
            result = worker.run_and_parse_cutechess(
                config,
                "cutechess-ob.exe -variant atomic",
                7,
                queue.Queue(),
                abort_flag,
            )
        return result, popen

    def test_stderr_is_captured_with_nonzero_exit_before_first_game(self):
        result, popen = self.run_fake(
            ["Engine initialization failed: timeout\n"], 1
        )

        self.assertEqual(popen.call_args.kwargs["stderr"], worker.STDOUT)
        self.assertEqual(result["returncode"], 1)
        self.assertEqual(result["started_games"], 0)
        self.assertEqual(result["finished_games"], 0)
        self.assertIn("before the first completed game", result["message"])
        self.assertIn("initialization failed", result["logs"])

    def test_clean_exit_without_games_is_still_a_startup_failure(self):
        result, _ = self.run_fake([], 0)
        self.assertEqual(result["returncode"], 0)
        self.assertIn("without completing a game", result["message"])

    def test_completed_games_and_clean_exit_are_not_a_runner_failure(self):
        result, _ = self.run_fake(
            [
                "Started game 1 of 2 (Atomic-dev vs Atomic-base)\n",
                "Finished game 1 (Atomic-dev vs Atomic-base): 1-0 {White mates}\n",
                "Started game 2 of 2 (Atomic-base vs Atomic-dev)\n",
                "Finished game 2 (Atomic-base vs Atomic-dev): 0-1 {Black mates}\n",
            ],
            0,
        )
        self.assertIsNone(result)

    def test_clean_partial_batch_is_reported_as_a_runner_failure(self):
        result, _ = self.run_fake(
            [
                "Started game 1 of 2 (Atomic-dev vs Atomic-base)\n",
                "Finished game 1 (Atomic-dev vs Atomic-base): 1-0 {White mates}\n",
            ],
            0,
            expected_games=2,
        )
        self.assertEqual(result["finished_games"], 1)
        self.assertIn("only 1/2 games", result["message"])

    def test_server_abort_after_completed_games_is_not_a_runner_failure(self):
        result, _ = self.run_fake(
            [
                "Started game 1 of 2 (Atomic-dev vs Atomic-base)\n",
                "Finished game 1 (Atomic-dev vs Atomic-base): 1-0 {White mates}\n",
                "Started game 2 of 2 (Atomic-base vs Atomic-dev)\n",
                "Finished game 2 (Atomic-base vs Atomic-dev): 0-1 {Black mates}\n",
            ],
            0,
            expected_games=2,
            abort_on_eof=True,
        )
        self.assertIsNone(result)

    def test_runner_failures_are_aggregated_into_one_server_event(self):
        first = concurrent.futures.Future()
        first.set_result(
            {
                "cutechess_idx": 0,
                "returncode": 1,
                "started_games": 0,
                "finished_games": 0,
                "message": "startup failed",
                "logs": "engine timeout",
            }
        )
        second = concurrent.futures.Future()
        second.set_result(None)
        config = SimpleNamespace(workload={"test": {"id": 42}})
        reporter = worker.ResultsReporter(
            config, [first, second], queue.Queue(), threading.Event()
        )

        with mock.patch.object(
            worker.ServerReporter, "report_engine_error"
        ) as report:
            count = reporter.send_runner_errors()

        self.assertEqual(count, 1)
        report.assert_called_once()
        self.assertIn("1/2 copies failed", report.call_args.args[1])
        self.assertIn("engine timeout", report.call_args.args[2])

    def test_missing_pgn_does_not_mask_pregame_failure(self):
        config = SimpleNamespace(
            workload={"test": {"id": 1}, "result": {"id": 2}}
        )
        reporter = worker.ResultsReporter(
            config, [], queue.Queue(), threading.Event()
        )

        with tempfile.TemporaryDirectory() as cwd:
            previous = os.getcwd()
            os.chdir(cwd)
            try:
                with mock.patch.object(
                    worker.PGNHelper, "slice_pgn_file"
                ) as slice_pgn:
                    reporter.send_errors(3, 1)
            finally:
                os.chdir(previous)

        slice_pgn.assert_not_called()

    def test_server_abort_does_not_block_on_or_report_pending_runner(self):
        pending = concurrent.futures.Future()
        abort_flag = threading.Event()
        abort_flag.set()
        config = SimpleNamespace(workload={"test": {"id": 42}})
        reporter = worker.ResultsReporter(
            config, [pending], queue.Queue(), abort_flag
        )

        with mock.patch.object(
            worker.ServerReporter, "report_engine_error"
        ) as report:
            count = reporter.send_runner_errors()

        self.assertEqual(count, 0)
        report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
