#!/usr/bin/env python3
"""Select a reproducible subset from a merged Horde opening candidate pool."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

if __package__:
    from .analyze_horde_book_pairs import pair_games, read_games
    from .generate_horde_book_candidates import sha256_file
    from .merge_horde_book_shards import balanced_unique, enforce_prefix_cap, read_jsonl
else:
    from analyze_horde_book_pairs import pair_games, read_games  # type: ignore
    from generate_horde_book_candidates import sha256_file  # type: ignore
    from merge_horde_book_shards import (  # type: ignore
        balanced_unique,
        enforce_prefix_cap,
        read_jsonl,
    )


def select_by_gap(
    records: list[dict[str, object]], max_gap: int
) -> list[dict[str, object]]:
    if max_gap < 0:
        raise ValueError("max_gap must be non-negative")

    selected: list[dict[str, object]] = []
    for record in records:
        if "top_two_gap" not in record:
            raise ValueError("candidate record is missing top_two_gap")
        gap = int(record["top_two_gap"])
        if gap < 0:
            raise ValueError("top_two_gap must be non-negative")
        if gap <= max_gap:
            selected.append(record)
    return selected


def position_key(fen: str) -> str:
    """Match positions after referee normalization of EP and move clocks."""

    fields = fen.split()
    if len(fields) not in {5, 6}:
        raise ValueError(f"invalid FEN: {fen!r}")
    return " ".join(fields[:3])


def exclude_positions(
    records: list[dict[str, object]], excluded_keys: set[str]
) -> tuple[list[dict[str, object]], int]:
    kept: list[dict[str, object]] = []
    excluded = 0
    for record in records:
        key = position_key(str(record["canonical_fen"]))
        if key in excluded_keys:
            excluded += 1
        else:
            kept.append(record)
    return kept, excluded


def read_exclusion_pgns(paths: list[Path]) -> set[str]:
    if not paths:
        return set()
    games = read_games(paths)
    _, incomplete = pair_games(games)
    if incomplete:
        raise ValueError("exclusion PGNs contain incomplete color-reversed pairs")
    return {position_key(game.fen) for game in games}


def load_pool(directory: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "HORDE_BOOK_CANDIDATE_MERGE_V1":
        raise ValueError(f"{directory}: unsupported candidate-pool schema")

    epd_path = directory / str(manifest["outputs"]["epd"]["path"])
    trace_path = directory / str(manifest["outputs"]["traces"]["path"])
    if sha256_file(epd_path) != manifest["outputs"]["epd"]["sha256"]:
        raise ValueError(f"{directory}: EPD hash mismatch")
    if sha256_file(trace_path) != manifest["outputs"]["traces"]["sha256"]:
        raise ValueError(f"{directory}: trace hash mismatch")

    records = read_jsonl(trace_path)
    if len(records) != manifest["counts"]["records"]:
        raise ValueError(f"{directory}: trace record count mismatch")
    manifest["_manifest_path"] = str(manifest_path.resolve())
    manifest["_manifest_sha256"] = sha256_file(manifest_path)
    return manifest, records


def generate(
    source: Path, output_dir: Path, max_gap: int, exclude_pgns: list[Path] | None = None
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    epd_path = output_dir / "candidates.epd"
    trace_path = output_dir / "traces.jsonl"
    manifest_path = output_dir / "manifest.json"
    for path in (epd_path, trace_path, manifest_path):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path}")

    source_manifest, source_records = load_pool(source)
    gap_selected = select_by_gap(source_records, max_gap)
    exclusion_paths = exclude_pgns or []
    excluded_keys = read_exclusion_pgns(exclusion_paths)
    heldout_selected, excluded_count = exclude_positions(gap_selected, excluded_keys)
    balanced, duplicate_count = balanced_unique(heldout_selected)
    initial_balance_trimmed = len(heldout_selected) - duplicate_count - len(balanced)

    prefix_share = float(source_manifest["recipe"]["prefix_share"])
    selected, prefix_trimmed, extra_balance_trimmed, prefix_cap = enforce_prefix_cap(
        balanced, prefix_share
    )
    balance_trimmed = initial_balance_trimmed + extra_balance_trimmed
    side_counts = Counter(str(record["side_to_move"]) for record in selected)
    prefix_counts = Counter(str(record["prefix_family"]) for record in selected)

    if not selected:
        raise ValueError("selection produced an empty opening pool")
    if side_counts["white"] != side_counts["black"]:
        raise AssertionError("selection is not balanced by side to move")
    if max(prefix_counts.values(), default=0) > prefix_cap:
        raise AssertionError("selection exceeds the prefix-family cap")

    with epd_path.open("w", encoding="ascii", newline="\n") as handle:
        for record in selected:
            handle.write(str(record["fen"]) + ";\n")
    with trace_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in selected:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    manifest = {
        "schema": "HORDE_BOOK_CANDIDATE_SELECTION_V1",
        "engine_sha256": source_manifest["engine_sha256"],
        "network_sha256": source_manifest["network_sha256"],
        "recipe": source_manifest["recipe"],
        "selection": {
            "max_gap": max_gap,
            "excluded_pgns": [
                {
                    "path": str(path.resolve()),
                    "sha256": sha256_file(path),
                }
                for path in exclusion_paths
            ],
        },
        "input": {
            "directory": str(source.resolve()),
            "manifest": source_manifest["_manifest_path"],
            "manifest_sha256": source_manifest["_manifest_sha256"],
            "records": len(source_records),
        },
        "counts": {
            "input_records": len(source_records),
            "gap_filtered_out": len(source_records) - len(gap_selected),
            "heldout_positions_excluded": excluded_count,
            "canonical_duplicates_removed": duplicate_count,
            "prefix_trimmed": prefix_trimmed,
            "balance_trimmed": balance_trimmed,
            "records": len(selected),
            "white_to_move": side_counts["white"],
            "black_to_move": side_counts["black"],
            "prefix_families": len(prefix_counts),
            "prefix_cap": prefix_cap,
            "max_prefix_count": max(prefix_counts.values(), default=0),
        },
        "outputs": {
            "epd": {"path": epd_path.name, "sha256": sha256_file(epd_path)},
            "traces": {"path": trace_path.name, "sha256": sha256_file(trace_path)},
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-gap", type=int, required=True)
    parser.add_argument("--exclude-pgn", type=Path, action="append", default=[])
    args = parser.parse_args()
    manifest = generate(args.source, args.output_dir, args.max_gap, args.exclude_pgn)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
