import queue
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Client"))

import genfens


class GenfensInvocationTests(unittest.TestCase):

    def test_public_engine_keeps_the_command_line_contract(self):
        invocation = genfens.genfens_command_builder(
            "engine", None, False, 8, "None", "plies=6", 94
        )
        self.assertEqual(
            invocation,
            (["./engine", "genfens 8 seed 94 book None plies=6", "quit"], None),
        )

    def test_private_engine_uses_ordered_uci_stdin(self):
        invocation = genfens.genfens_command_builder(
            "engine.exe",
            "Networks/run 6b.nnue",
            True,
            8,
            "Books/horde.epd",
            "minplies=3 maxplies=4",
            94,
        )
        self.assertEqual(invocation[0], ["./engine.exe"])
        self.assertEqual(
            invocation[1],
            (
                b"setoption name EvalFile value Networks/run 6b.nnue\n"
                b"isready\n"
                b"genfens 8 seed 94 book Books/horde.epd "
                b"minplies=3 maxplies=4\n"
                b"quit\n"
            ),
        )

    def test_private_stream_is_written_before_output_is_collected(self):
        process = mock.Mock()
        process.stdout.readline.side_effect = [
            b"readyok\n",
            b"info string genfens 4k3/8/8/8/8/8/P7/8 w - - 0 1\n",
            b"",
        ]
        results = queue.Queue()
        invocation = (["./engine"], b"isready\ngenfens 1 seed 1 book None\nquit\n")

        with mock.patch.object(genfens.subprocess, "Popen", return_value=process) as popen:
            genfens.genfens_single_threaded(invocation, results)

        self.assertEqual(popen.call_args.args[0], ["./engine"])
        self.assertEqual(popen.call_args.kwargs["stdin"], genfens.subprocess.PIPE)
        process.stdin.write.assert_called_once_with(invocation[1])
        process.stdin.close.assert_called_once_with()
        process.wait.assert_called_once_with()
        self.assertEqual(
            results.get_nowait(),
            "4k3/8/8/8/8/8/P7/8 w - - 0 1",
        )


if __name__ == "__main__":
    unittest.main()
