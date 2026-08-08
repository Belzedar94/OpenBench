#!/usr/bin/env python3
"""Screen a Horde EPD pool with reproducible MultiPV root constraints."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import platform

if __package__:
    from .generate_horde_book_candidates import RootMove, UciEngine, canonical_fen, sha256_file
    from .generate_horde_book_successors import read_epd
else:
    from generate_horde_book_candidates import (  # type: ignore
        RootMove,
        UciEngine,
        canonical_fen,
        sha256_file,
    )
    from generate_horde_book_successors import read_epd  # type: ignore


def screen_reason(roots: list[RootMove], max_gap: int, max_abs_score: int) -> str:
    if len(roots) < 2:
        return "incomplete_multipv"
    best, second = roots[:2]
    if best.score_kind == "mate" or second.score_kind == "mate":
        return "mate_score"
    if best.score - second.score > max_gap:
        return "wide_top_two_gap"
    if abs(best.score) > max_abs_score:
        return "large_absolute_score"
    return "accepted"


def generate(
    source: Path,
    engine: Path,
    network: Path,
    output_dir: Path,
    nodes: int,
    multipv: int,
    max_gap: int,
    max_abs_score: int,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    epd_path = output_dir / "screened.epd"
    trace_path = output_dir / "screening.jsonl"
    manifest_path = output_dir / "manifest.json"
    for path in (epd_path, trace_path, manifest_path):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path}")

    seen: set[str] = set()
    accepted: list[str] = []
    traces: list[dict[str, object]] = []
    reasons: Counter[str] = Counter()
    side_counts: Counter[str] = Counter()

    with UciEngine(engine, network, nodes, multipv) as uci:
        for ordinal, fen in enumerate(read_epd(source)):
            key = canonical_fen(fen)
            if key in seen:
                reasons["canonical_duplicate"] += 1
                continue
            seen.add(key)
            uci.new_trajectory()
            roots = uci.analyze_fen(fen)
            reason = screen_reason(roots, max_gap, max_abs_score)
            reasons[reason] += 1
            traces.append(
                {
                    "ordinal": ordinal,
                    "fen": fen,
                    "canonical_fen": key,
                    "reason": reason,
                    "roots": [root.__dict__ for root in roots],
                }
            )
            if reason == "accepted":
                accepted.append(fen)
                side_counts["white" if fen.split()[1] == "w" else "black"] += 1

    with epd_path.open("w", encoding="ascii", newline="\n") as handle:
        for fen in accepted:
            handle.write(fen + ";\n")
    with trace_path.open("w", encoding="utf-8", newline="\n") as handle:
        for trace in traces:
            handle.write(json.dumps(trace, sort_keys=True) + "\n")

    manifest = {
        "schema": "HORDE_BOOK_MULTIPV_SCREEN_V2",
        "source": str(source.resolve()),
        "source_sha256": sha256_file(source),
        "engine": str(engine.resolve()),
        "engine_sha256": sha256_file(engine),
        "network": str(network.resolve()),
        "network_sha256": sha256_file(network),
        "python": platform.python_version(),
        "settings": {
            "threads": 1,
            "hash_mib": 16,
            "nodes": nodes,
            "multipv": multipv,
            "max_gap": max_gap,
            "max_abs_score": max_abs_score,
        },
        "counts": {
            "source_records": sum(1 for _ in read_epd(source)),
            "canonical_sources": len(seen),
            "accepted": len(accepted),
            "accepted_by_side": dict(sorted(side_counts.items())),
            "reasons": dict(sorted(reasons.items())),
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
    parser.add_argument("--nodes", type=int, default=1000)
    parser.add_argument("--multipv", type=int, default=4)
    parser.add_argument("--max-gap", type=int, default=15)
    parser.add_argument("--max-abs-score", type=int, default=400)
    args = parser.parse_args()

    if not 1 <= args.nodes <= 10_000_000:
        parser.error("--nodes must be between 1 and 10000000")
    if not 2 <= args.multipv <= 16:
        parser.error("--multipv must be between 2 and 16")
    if not 0 <= args.max_gap <= 100_000:
        parser.error("--max-gap must be between 0 and 100000")
    if not 0 <= args.max_abs_score <= 100_000:
        parser.error("--max-abs-score must be between 0 and 100000")

    manifest = generate(
        source=args.source,
        engine=args.engine,
        network=args.network,
        output_dir=args.output_dir,
        nodes=args.nodes,
        multipv=args.multipv,
        max_gap=args.max_gap,
        max_abs_score=args.max_abs_score,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
