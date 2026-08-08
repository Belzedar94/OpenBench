#!/usr/bin/env python3
"""Select a reproducible subset from a merged Horde opening candidate pool."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

if __package__:
    from .analyze_horde_book_pairs import pair_games, read_games
    from .generate_horde_book_candidates import sha256_file
    from .merge_horde_book_shards import enforce_prefix_cap, read_jsonl
else:
    from analyze_horde_book_pairs import pair_games, read_games  # type: ignore
    from generate_horde_book_candidates import sha256_file  # type: ignore
    from merge_horde_book_shards import (  # type: ignore
        enforce_prefix_cap,
        read_jsonl,
    )


V3_EVALUATION_SCREEN_SETTINGS = {
    "threads": 1,
    "hash_mib": 16,
    "nodes": 20_000,
    "multipv": 2,
    "max_gap": 100_000,
    "max_abs_score": 100_000,
}


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


def white_relative_eval(record: dict[str, object]) -> int:
    if "selection_white_eval" in record:
        return int(record["selection_white_eval"])
    if "best_score" not in record:
        raise ValueError("candidate record is missing best_score")
    side_to_move = str(record.get("side_to_move", ""))
    if side_to_move not in {"white", "black"}:
        raise ValueError("candidate record has invalid side_to_move")
    score = int(record["best_score"])
    return score if side_to_move == "white" else -score


def select_by_white_eval(
    records: list[dict[str, object]], min_eval: int, max_eval: int
) -> list[dict[str, object]]:
    if min_eval > max_eval:
        raise ValueError("min_eval must not exceed max_eval")
    return [
        record
        for record in records
        if min_eval <= white_relative_eval(record) <= max_eval
    ]


def position_key(fen: str) -> str:
    """Match positions after referee normalization of EP and move clocks."""

    fields = fen.split()
    if len(fields) not in {5, 6}:
        raise ValueError(f"invalid FEN: {fen!r}")
    return " ".join(fields[:3])


def deterministic_record_key(record: dict[str, object]) -> str:
    """Order candidates independently of shard, seed, and input order."""

    stable_identity = {
        "position_key": position_key(str(record["canonical_fen"])),
        "canonical_fen": str(record["canonical_fen"]),
        "fen": str(record["fen"]),
        "prefix_family": str(record["prefix_family"]),
        "side_to_move": str(record["side_to_move"]),
    }
    payload = json.dumps(stable_identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def operationally_unique_and_balanced(
    records: list[dict[str, object]],
) -> tuple[list[dict[str, object]], int, int]:
    """Deduplicate on the referee key, then balance in frozen hash order."""

    ordered = sorted(records, key=deterministic_record_key)
    unique: list[dict[str, object]] = []
    seen: set[str] = set()
    duplicate_count = 0
    for record in ordered:
        key = position_key(str(record["canonical_fen"]))
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        unique.append(record)

    by_side = {
        side: [record for record in unique if record["side_to_move"] == side]
        for side in ("white", "black")
    }
    target = min(len(by_side["white"]), len(by_side["black"]))
    keep_ids = {
        id(record)
        for side in ("white", "black")
        for record in by_side[side][:target]
    }
    selected = [record for record in unique if id(record) in keep_ids]
    balance_trimmed = len(unique) - len(selected)
    return selected, duplicate_count, balance_trimmed


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


def load_evaluation_screen(
    directory: Path,
    expected_source_sha256: str,
    expected_engine_sha256: str,
    expected_network_sha256: str,
) -> tuple[dict[str, object], dict[str, int]]:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "HORDE_BOOK_MULTIPV_SCREEN_V2":
        raise ValueError(f"{directory}: unsupported evaluation-screen schema")
    if manifest.get("source_sha256") != expected_source_sha256:
        raise ValueError(f"{directory}: evaluation-screen source hash mismatch")
    if manifest.get("engine_sha256") != expected_engine_sha256:
        raise ValueError(f"{directory}: evaluation-screen engine hash mismatch")
    if manifest.get("network_sha256") != expected_network_sha256:
        raise ValueError(f"{directory}: evaluation-screen network hash mismatch")
    if manifest.get("settings") != V3_EVALUATION_SCREEN_SETTINGS:
        raise ValueError(f"{directory}: evaluation-screen settings mismatch")

    trace_path = directory / str(manifest["outputs"]["traces"]["path"])
    if sha256_file(trace_path) != manifest["outputs"]["traces"]["sha256"]:
        raise ValueError(f"{directory}: evaluation-screen trace hash mismatch")

    records = read_jsonl(trace_path)
    if len(records) != manifest["counts"]["canonical_sources"]:
        raise ValueError(f"{directory}: evaluation-screen trace record count mismatch")

    scores: dict[str, int] = {}
    for record in records:
        roots = record.get("roots")
        if not isinstance(roots, list) or not roots:
            raise ValueError(f"{directory}: evaluation-screen record has no root score")
        best = roots[0]
        if not isinstance(best, dict) or best.get("score_kind") != "cp":
            raise ValueError(f"{directory}: evaluation-screen record has a non-cp score")
        fen = str(record["fen"])
        fields = fen.split()
        if len(fields) != 6 or fields[1] not in {"w", "b"}:
            raise ValueError(f"{directory}: evaluation-screen record has invalid FEN")
        score = int(best["score"])
        white_score = score if fields[1] == "w" else -score
        key = str(record["canonical_fen"])
        if key in scores:
            raise ValueError(f"{directory}: duplicate evaluation-screen position")
        scores[key] = white_score

    manifest["_manifest_path"] = str(manifest_path.resolve())
    manifest["_manifest_sha256"] = sha256_file(manifest_path)
    return manifest, scores


def generate(
    source: Path,
    output_dir: Path,
    max_gap: int,
    exclude_pgns: list[Path] | None = None,
    min_white_eval: int | None = None,
    max_white_eval: int | None = None,
    evaluation_screen: Path | None = None,
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
    screen_manifest: dict[str, object] | None = None
    if evaluation_screen is not None:
        screen_manifest, screen_scores = load_evaluation_screen(
            evaluation_screen,
            str(source_manifest["outputs"]["epd"]["sha256"]),
            str(source_manifest["engine_sha256"]),
            str(source_manifest["network_sha256"]),
        )
        scored_records: list[dict[str, object]] = []
        for record in gap_selected:
            key = str(record["canonical_fen"])
            if key not in screen_scores:
                raise ValueError(f"{evaluation_screen}: missing evaluation for {key}")
            scored = dict(record)
            scored["selection_white_eval"] = screen_scores[key]
            scored_records.append(scored)
        gap_selected = scored_records
    if (min_white_eval is None) != (max_white_eval is None):
        raise ValueError("white evaluation bounds must be supplied together")
    eval_selected = (
        select_by_white_eval(gap_selected, min_white_eval, max_white_eval)
        if min_white_eval is not None and max_white_eval is not None
        else gap_selected
    )
    exclusion_paths = exclude_pgns or []
    excluded_keys = read_exclusion_pgns(exclusion_paths)
    heldout_selected, excluded_count = exclude_positions(eval_selected, excluded_keys)
    balanced, duplicate_count, initial_balance_trimmed = (
        operationally_unique_and_balanced(heldout_selected)
    )

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
        "schema": "HORDE_BOOK_CANDIDATE_SELECTION_V2",
        "engine_sha256": source_manifest["engine_sha256"],
        "network_sha256": source_manifest["network_sha256"],
        "recipe": source_manifest["recipe"],
        "selection": {
            "max_gap": max_gap,
            "min_white_eval": min_white_eval,
            "max_white_eval": max_white_eval,
            "operational_dedup_key": "board_side_castling",
            "ordering": "sha256_stable_position_v1",
            "evaluation_screen": (
                {
                    "directory": str(evaluation_screen.resolve()),
                    "manifest": screen_manifest["_manifest_path"],
                    "manifest_sha256": screen_manifest["_manifest_sha256"],
                    "engine_sha256": screen_manifest["engine_sha256"],
                    "network_sha256": screen_manifest["network_sha256"],
                    "settings": screen_manifest["settings"],
                }
                if evaluation_screen is not None and screen_manifest is not None
                else None
            ),
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
            "white_eval_filtered_out": len(gap_selected) - len(eval_selected),
            "heldout_positions_excluded": excluded_count,
            "operational_duplicates_removed": duplicate_count,
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
    parser.add_argument("--min-white-eval", type=int)
    parser.add_argument("--max-white-eval", type=int)
    parser.add_argument("--evaluation-screen", type=Path)
    parser.add_argument("--exclude-pgn", type=Path, action="append", default=[])
    args = parser.parse_args()
    if (args.min_white_eval is None) != (args.max_white_eval is None):
        parser.error("--min-white-eval and --max-white-eval must be supplied together")
    manifest = generate(
        args.source,
        args.output_dir,
        args.max_gap,
        args.exclude_pgn,
        args.min_white_eval,
        args.max_white_eval,
        args.evaluation_screen,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
