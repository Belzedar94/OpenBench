import queue
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Client"))

import bench


class BenchInvocationTests(unittest.TestCase):

    def test_public_engine_keeps_the_command_line_bench(self):
        process = mock.Mock()
        process.communicate.return_value = (
            b"Nodes searched: 123\nNodes/second: 456\n",
            None,
        )
        results = queue.Queue()

        with mock.patch.object(bench.subprocess, "Popen", return_value=process) as popen:
            bench.single_core_bench("engine", None, False, results)

        self.assertEqual(popen.call_args.args[0], ["./engine", "bench"])
        self.assertIsNone(popen.call_args.kwargs["stdin"])
        process.communicate.assert_called_once_with(input=None)
        self.assertEqual(results.get_nowait(), (123, 456))

    def test_private_network_bench_uses_ordered_uci_stdin(self):
        process = mock.Mock()
        process.communicate.return_value = (
            b"Nodes searched: 789\nNodes/second: 654\n",
            None,
        )
        results = queue.Queue()

        with mock.patch.object(bench.subprocess, "Popen", return_value=process) as popen:
            bench.single_core_bench(
                "engine.exe",
                "Networks/run 6b.nnue",
                True,
                results,
            )

        self.assertEqual(popen.call_args.args[0], ["./engine.exe"])
        self.assertEqual(popen.call_args.kwargs["stdin"], bench.subprocess.PIPE)
        process.communicate.assert_called_once_with(
            input=(
                b"uci\n"
                b"setoption name EvalFile value Networks/run 6b.nnue\n"
                b"isready\n"
                b"bench\n"
                b"quit\n"
            )
        )
        self.assertEqual(results.get_nowait(), (789, 654))


if __name__ == "__main__":
    unittest.main()
