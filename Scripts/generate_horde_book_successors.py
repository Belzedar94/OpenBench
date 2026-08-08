#!/usr/bin/env python3
"""Advance canonical Horde EPD records by one natural engine move."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
from typing import Iterator

import chess
import chess.variant

if __package__:
    from .generate_horde_book_candidates import (
        RootMove,
        UciEngine,
        canonical_fen,
        choose_move,
        sha256_file,
    )
else:
    from generate_horde_book_candidates import (  # type: ignore
        RootMove,
        UciEngine,
        canonical_fen,
        choose_move,
        sha256_file,
    )


def read_epd(path: Path) -> Iterator[str]:
    with path.open(encoding="ascii") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            fen = line.split(";", 1)[0].strip()
            fields = fen.split()
            if len(fields) != 6:
                raise ValueError(f"{path}:{line_number}: expected a six-field FEN")
            yield fen


def advance_fen(fen: str, selected: RootMove) -> str:
    board = chess.variant.HordeBoard(fen)
    legal_moves = {board.uci(move) for move in board.legal_moves}
    if selected.move not in legal_moves:
        raise ValueError(f"selected move {selected.move} is illegal in {fen}")
    board.push_uci(selected.move)
    return board.fen(en_passant="fen")


def generate(
    source: Path,
    engine: Path,
    network: Path,
    output_dir: Path,
    seed: int,
    nodes: int,
    multipv: int,
    score_cap: int,
    plies: int,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    epd_path = output_dir / "successors.epd"
    trace_path = output_dir / "successors.jsonl"
    manifest_path = output_dir / "manifest.json"
    for path in (epd_path, trace_path, manifest_path):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path}")

    source_seen: set[str] = set()
    successor_seen: set[str] = set()
    records: list[dict[str, object]] = []
    skipped_wrong_side = 0
    skipped_terminal = 0
    skipped_duplicate_successor = 0

    with UciEngine(engine, network, nodes, multipv) as uci:
        for source_ordinal, fen in enumerate(read_epd(source)):
            source_key = canonical_fen(fen)
            if source_key in source_seen:
                continue
            source_seen.add(source_key)

            board = chess.variant.HordeBoard(fen)
            if board.turn != chess.BLACK:
                skipped_wrong_side += 1
                continue
            if board.is_game_over(claim_draw=True) or board.legal_moves.count() < 2:
                skipped_terminal += 1
                continue

            uci.new_trajectory()
            successor = fen
            steps: list[dict[str, object]] = []
            for ply in range(plies):
                roots = uci.analyze_fen(successor)
                if len(roots) < 2:
                    break
                selected = choose_move(roots, score_cap, seed, source_ordinal, ply)
                next_fen = advance_fen(successor, selected)
                steps.append(
                    {
                        "ply": ply,
                        "fen": successor,
                        "selected": selected.__dict__,
                        "roots": [root.__dict__ for root in roots],
                    }
                )
                successor = next_fen
            if len(steps) != plies:
                skipped_terminal += 1
                continue
            successor_key = canonical_fen(successor)
            if successor_key in successor_seen:
                skipped_duplicate_successor += 1
                continue
            successor_seen.add(successor_key)
            records.append(
                {
                    "source_ordinal": source_ordinal,
                    "source_fen": fen,
                    "source_canonical_fen": source_key,
                    "successor_fen": successor,
                    "successor_canonical_fen": successor_key,
                    "plies": plies,
                    "steps": steps,
                }
            )

    with epd_path.open("w", encoding="ascii", newline="\n") as handle:
        for record in records:
            handle.write(str(record["successor_fen"]) + ";\n")
    with trace_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    manifest = {
        "schema": "HORDE_BOOK_NATURAL_SUCCESSORS_V1",
        "source": str(source.resolve()),
        "source_sha256": sha256_file(source),
        "engine": str(engine.resolve()),
        "engine_sha256": sha256_file(engine),
        "network": str(network.resolve()),
        "network_sha256": sha256_file(network),
        "python": platform.python_version(),
        "python_chess": chess.__version__,
        "prng": "SHA256 counter draws",
        "settings": {
            "seed": seed,
            "nodes": nodes,
            "multipv": multipv,
            "best_move_weight": 0.75,
            "score_cap": score_cap,
            "plies": plies,
            "required_source_side": "black",
            "successor_side": "white",
        },
        "counts": {
            "source_records": sum(1 for _ in read_epd(source)),
            "canonical_sources": len(source_seen),
            "successors": len(records),
            "canonical_successors": len(successor_seen),
            "skipped_wrong_side": skipped_wrong_side,
            "skipped_terminal": skipped_terminal,
            "skipped_duplicate_successor": skipped_duplicate_successor,
        },
        "outputs": {
            "epd": {"path": epd_path.name, "sha256": sha256_file(epd_path)},
            "traces": {"path": trace_path.name, "sha256": sha256_file(trace_path)},
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--network", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260808)
    parser.add_argument("--nodes", type=int, default=1000)
    parser.add_argument("--multipv", type=int, default=4)
    parser.add_argument("--score-cap", type=int, default=50)
    parser.add_argument("--plies", type=int, default=1)
    args = parser.parse_args()

    if not 1 <= args.nodes <= 10_000_000:
        parser.error("--nodes must be between 1 and 10000000")
    if not 2 <= args.multipv <= 16:
        parser.error("--multipv must be between 2 and 16")
    if not 0 <= args.score_cap <= 100_000:
        parser.error("--score-cap must be between 0 and 100000")
    if not 1 <= args.plies <= 63 or args.plies % 2 == 0:
        parser.error("--plies must be an odd number between 1 and 63")

    manifest = generate(
        source=args.source,
        engine=args.engine,
        network=args.network,
        output_dir=args.output_dir,
        seed=args.seed,
        nodes=args.nodes,
        multipv=args.multipv,
        score_cap=args.score_cap,
        plies=args.plies,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
