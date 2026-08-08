#!/usr/bin/env python3
"""Merge compatible deterministic Horde candidate-generation shards."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
from typing import Iterable

if __package__:
    from .generate_horde_book_candidates import sha256_file
else:
    from generate_horde_book_candidates import sha256_file  # type: ignore


COMMON_SETTING_KEYS = {
    "nodes",
    "multipv",
    "root_move_policy",
    "best_move_weight",
    "score_cap",
    "candidate_gap",
    "min_ply",
    "max_ply",
    "prefix_share",
}


def read_jsonl(path: Path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def balanced_unique(records: Iterable[dict[str, object]]) -> tuple[list[dict[str, object]], int]:
    unique: list[dict[str, object]] = []
    seen: set[str] = set()
    duplicates = 0
    for record in records:
        key = str(record["canonical_fen"])
        if key in seen:
            duplicates += 1
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
    return [record for record in unique if id(record) in keep_ids], duplicates


def enforce_prefix_cap(
    records: list[dict[str, object]], prefix_share: float
) -> tuple[list[dict[str, object]], int, int, int]:
    constrained = records
    prefix_trimmed = 0
    balance_trimmed = 0
    while True:
        prefix_cap = max(1, math.ceil(len(constrained) * prefix_share))
        seen_families: Counter[str] = Counter()
        filtered: list[dict[str, object]] = []
        for record in constrained:
            family = str(record["prefix_family"])
            if seen_families[family] >= prefix_cap:
                prefix_trimmed += 1
                continue
            seen_families[family] += 1
            filtered.append(record)

        rebalanced, duplicates = balanced_unique(filtered)
        assert duplicates == 0
        balance_trimmed += len(filtered) - len(rebalanced)
        if rebalanced == constrained:
            return constrained, prefix_trimmed, balance_trimmed, prefix_cap
        constrained = rebalanced


def load_shard(directory: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "HORDE_BOOK_CANDIDATE_GENERATION_V1":
        raise ValueError(f"{directory}: unsupported shard schema")

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


def common_recipe(manifest: dict[str, object]) -> dict[str, object]:
    settings = manifest["settings"]
    return {key: settings[key] for key in sorted(COMMON_SETTING_KEYS)}


def generate(shards: list[Path], output_dir: Path) -> dict[str, object]:
    if len(shards) < 2:
        raise ValueError("at least two shards are required")
    output_dir.mkdir(parents=True, exist_ok=True)
    epd_path = output_dir / "candidates.epd"
    trace_path = output_dir / "traces.jsonl"
    manifest_path = output_dir / "manifest.json"
    for path in (epd_path, trace_path, manifest_path):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path}")

    loaded = [load_shard(shard) for shard in shards]
    authority = loaded[0][0]
    recipe = common_recipe(authority)
    for manifest, _ in loaded[1:]:
        if manifest["engine_sha256"] != authority["engine_sha256"]:
            raise ValueError("shards use different engine hashes")
        if manifest["network_sha256"] != authority["network_sha256"]:
            raise ValueError("shards use different network hashes")
        if common_recipe(manifest) != recipe:
            raise ValueError("shards use different generation recipes")

    decorated: list[dict[str, object]] = []
    for shard_index, ((manifest, records), directory) in enumerate(zip(loaded, shards)):
        for record in records:
            decorated.append(
                {
                    **record,
                    "source_shard": shard_index,
                    "source_seed": manifest["settings"]["seed"],
                    "source_manifest_sha256": manifest["_manifest_sha256"],
                    "source_directory": str(directory.resolve()),
                }
            )

    merged, duplicate_count = balanced_unique(decorated)
    initial_balance_trimmed = len(decorated) - duplicate_count - len(merged)
    merged, prefix_trimmed, extra_balance_trimmed, prefix_cap = enforce_prefix_cap(
        merged, float(recipe["prefix_share"])
    )
    balance_trimmed = initial_balance_trimmed + extra_balance_trimmed
    side_counts = Counter(str(record["side_to_move"]) for record in merged)
    prefix_counts = Counter(str(record["prefix_family"]) for record in merged)
    assert max(prefix_counts.values(), default=0) <= prefix_cap

    with epd_path.open("w", encoding="ascii", newline="\n") as handle:
        for record in merged:
            handle.write(str(record["fen"]) + ";\n")
    with trace_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in merged:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    manifest = {
        "schema": "HORDE_BOOK_CANDIDATE_MERGE_V1",
        "engine_sha256": authority["engine_sha256"],
        "network_sha256": authority["network_sha256"],
        "recipe": recipe,
        "inputs": [
            {
                "directory": str(directory.resolve()),
                "manifest": manifest["_manifest_path"],
                "manifest_sha256": manifest["_manifest_sha256"],
                "seed": manifest["settings"]["seed"],
                "records": manifest["counts"]["records"],
            }
            for (manifest, _), directory in zip(loaded, shards)
        ],
        "counts": {
            "input_records": sum(len(records) for _, records in loaded),
            "canonical_duplicates_removed": duplicate_count,
            "prefix_trimmed": prefix_trimmed,
            "balance_trimmed": balance_trimmed,
            "records": len(merged),
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
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = generate(args.shard, args.output_dir)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
