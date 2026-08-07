#!/usr/bin/env python3

import argparse
import pathlib
import re
import subprocess
import tempfile


SELECTORS = [
    "results:Horde black win",
    "results:Horde draw stalemate",
    "results:Horde white win",
    "results:Horde mate precedes fifty moves",
    "results:Horde black stalemate",
    "results:Horde simple pawn fortress",
    "results:Horde king-only fortress",
    "results:Horde all-moves fortress",
    "results:Horde forced fortress",
    "results:Horde breakable fortress",
    "results:Horde mobile after king move",
    "results:Horde mobile after piece move",
    "results:Horde last piece is capturable",
    "results:Horde fifty moves",
    "hordeMaterialCorpus",
    "hordeRuleContract",
    "perft:horde canonical start d1",
    "perft:horde canonical start d2",
    "perft:horde canonical start d3",
    "perft:horde canonical start d4",
    "perft:horde open flank d1",
    "perft:horde open flank d2",
    "perft:horde open flank d3",
    "perft:horde open flank d4",
    "perft:horde en passant d1",
    "perft:horde en passant d2",
    "perft:horde en passant d3",
    "perft:horde en passant d4",
    "perft:horde advanced d4",
    "perft:horde advanced ep d4",
    "perft:horde v2 startpos",
    "perft:horde dunsany startpos",
    "perft:horde3",
]

EXPECTED_TOTALS = re.compile(
    r"Totals:\s+35 passed,\s+0 failed,\s+0 skipped,\s+0 blacklisted"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("test_binary", type=pathlib.Path)
    args = parser.parse_args()

    binary = args.test_binary.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="horde-referee-") as temporary:
        report = pathlib.Path(temporary) / "horde-tests.txt"
        command = [str(binary), *SELECTORS, "-o", f"{report},txt"]
        completed = subprocess.run(command, check=False)
        output = report.read_text(encoding="utf-8", errors="replace")
        print(output, end="")

    if completed.returncode != 0:
        raise SystemExit(
            f"Horde referee tests exited with status {completed.returncode}"
        )
    if not EXPECTED_TOTALS.search(output):
        raise SystemExit("Horde referee test total did not match 35/0/0/0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
