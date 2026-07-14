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


def routing_config(book, dev_stagger_ms=0, base_stagger_ms=0):
    return SimpleNamespace(
        workload={
            "test": {
                "book": {"name": book},
                "dev": {
                    "engine": "Atomic-Stockfish",
                    "cutechess_launch_stagger_ms": dev_stagger_ms,
                },
                "base": {
                    "engine": "Fairy-Stockfish-Atomic-Baseline",
                    "cutechess_launch_stagger_ms": base_stagger_ms,
                },
            }
        }
    )


class CutechessLaunchStaggerTests(unittest.TestCase):

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
