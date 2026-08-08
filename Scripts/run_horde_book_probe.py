#!/usr/bin/env python3
"""Run and validate a reproducible node-limited Horde book-sensitivity probe."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

if __package__:
    from .analyze_horde_book_pairs import analyze
    from .generate_horde_book_candidates import sha256_file
else:
    from analyze_horde_book_pairs import analyze  # type: ignore
    from generate_horde_book_candidates import sha256_file  # type: ignore


def build_command(
    referee: Path,
    engine: Path,
    network: Path,
    book: Path,
    pgn: Path,
    label: str,
    seed: int,
    games: int,
    strong_nodes: int,
    weak_nodes: int,
    concurrency: int,
) -> list[str]:
    engine_dir = engine.resolve().parent
    shared_engine = [
        f"cmd={engine.resolve()}",
        f"dir={engine_dir}",
        "proto=uci",
        "tc=inf",
        "timemargin=250",
        f"option.EvalFile={network.resolve()}",
        "option.Threads=1",
        "option.Hash=32",
    ]
    return [
        str(referee.resolve()),
        "-repeat",
        "-recover",
        "-variant",
        "horde",
        "-concurrency",
        str(concurrency),
        "-games",
        str(games),
        "-maxmoves",
        "0",
        "-ratinginterval",
        str(max(20, games // 4)),
        "-outcomeinterval",
        str(max(20, games // 4)),
        "-engine",
        *shared_engine,
        "name=probe-strong",
        f"nodes={strong_nodes}",
        "-engine",
        *shared_engine,
        "name=probe-weak",
        f"nodes={weak_nodes}",
        "-openings",
        f"file={book.resolve()}",
        "format=epd",
        "order=random",
        "start=1",
        "-srand",
        str(seed),
        "-pgnout",
        str(pgn.resolve()),
        "-event",
        label,
        "-site",
        "local",
    ]


def valid_probe(returncode: int, analysis: dict[str, object], expected_games: int) -> bool:
    return (
        returncode == 0
        and analysis["games"] == expected_games
        and analysis["incomplete_games"] == 0
        and analysis["abnormal_terminations"] == 0
        and analysis["complete_pairs"] * 2 == expected_games
    )


def run(args: argparse.Namespace) -> tuple[dict[str, object], bool]:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    pgn_path = args.output_dir / "games.pgn"
    log_path = args.output_dir / "referee.log"
    manifest_path = args.output_dir / "manifest.json"
    for path in (pgn_path, log_path, manifest_path):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path}")

    command = build_command(
        referee=args.referee,
        engine=args.engine,
        network=args.network,
        book=args.book,
        pgn=pgn_path,
        label=args.label,
        seed=args.seed,
        games=args.games,
        strong_nodes=args.strong_nodes,
        weak_nodes=args.weak_nodes,
        concurrency=args.concurrency,
    )
    with log_path.open("w", encoding="utf-8", newline="\n") as log:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            if not args.quiet:
                sys.stdout.write(line)
                sys.stdout.flush()
            log.write(line)
            log.flush()
        returncode = process.wait()

    analysis_error: str | None = None
    try:
        analysis = analyze([pgn_path], "probe-strong", "probe-weak")
    except Exception as error:
        analysis = {}
        analysis_error = f"{type(error).__name__}: {error}"
    valid = analysis_error is None and valid_probe(returncode, analysis, args.games)
    manifest = {
        "schema": "HORDE_BOOK_SENSITIVITY_PROBE_V1",
        "status": "valid" if valid else "invalid",
        "returncode": returncode,
        "analysis_error": analysis_error,
        "settings": {
            "label": args.label,
            "seed": args.seed,
            "games": args.games,
            "pairs": args.games // 2,
            "strong_nodes": args.strong_nodes,
            "weak_nodes": args.weak_nodes,
            "concurrency": args.concurrency,
            "variant": "horde",
            "repeat": True,
            "recover": True,
            "maxmoves": 0,
        },
        "artifacts": {
            "referee": {
                "path": str(args.referee.resolve()),
                "sha256": sha256_file(args.referee),
            },
            "engine": {
                "path": str(args.engine.resolve()),
                "sha256": sha256_file(args.engine),
            },
            "network": {
                "path": str(args.network.resolve()),
                "sha256": sha256_file(args.network),
            },
            "book": {
                "path": str(args.book.resolve()),
                "sha256": sha256_file(args.book),
            },
            "pgn": {
                "path": str(pgn_path.resolve()),
                "sha256": sha256_file(pgn_path) if pgn_path.exists() else None,
            },
            "log": {"path": str(log_path.resolve()), "sha256": sha256_file(log_path)},
        },
        "command": command,
        "analysis": analysis,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest, valid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--referee", type=Path, required=True)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--network", type=Path, required=True)
    parser.add_argument("--book", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--label", default="Horde book sensitivity probe")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--games", type=int, default=80)
    parser.add_argument("--strong-nodes", type=int, default=50_000)
    parser.add_argument("--weak-nodes", type=int, default=40_000)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.games < 2 or args.games % 2:
        parser.error("--games must be a positive even number")
    if not 1 <= args.weak_nodes < args.strong_nodes <= 100_000_000:
        parser.error("nodes must satisfy 1 <= weak-nodes < strong-nodes <= 100000000")
    if not 1 <= args.concurrency <= 256:
        parser.error("--concurrency must be between 1 and 256")

    manifest, valid = run(args)
    print(json.dumps({"status": manifest["status"], "manifest": str(args.output_dir / "manifest.json")}))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
