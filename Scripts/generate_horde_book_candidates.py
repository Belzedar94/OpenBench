#!/usr/bin/env python3
"""Generate natural, reproducible Horde opening candidates with one trace each."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import platform
import re
import subprocess
from typing import Iterator

import chess
import chess.variant


INFO_RE = re.compile(
    r"^info depth (?P<depth>\d+) .*\bmultipv (?P<multipv>\d+) "
    r".*\bscore (?P<kind>cp|mate) "
    r"(?P<score>-?\d+).*\bpv (?P<move>[a-h][1-8][a-h][1-8][qrbn]?)\b"
)
MATE_SCORE = 100_000


@dataclass(frozen=True)
class RootMove:
    multipv: int
    move: str
    score: int
    score_kind: str
    depth: int = 0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def draw_u64(seed: int, trajectory: int, ply: int, purpose: str) -> int:
    payload = f"HORDE_BOOK_V2|{seed}|{trajectory}|{ply}|{purpose}".encode("ascii")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def normalize_score(kind: str, score: int) -> int:
    if kind == "cp":
        return score
    return (MATE_SCORE - min(abs(score), MATE_SCORE - 1)) * (1 if score > 0 else -1)


def parse_root_move(line: str) -> RootMove | None:
    match = INFO_RE.match(line)
    if not match:
        return None
    kind = match.group("kind")
    raw_score = int(match.group("score"))
    return RootMove(
        multipv=int(match.group("multipv")),
        move=match.group("move"),
        score=normalize_score(kind, raw_score),
        score_kind=kind,
        depth=int(match.group("depth")),
    )


def choose_move(
    roots: list[RootMove], score_cap: int, seed: int, trajectory: int, ply: int
) -> RootMove:
    if not roots:
        raise ValueError("cannot choose from an empty root list")
    roots = sorted(roots, key=lambda root: (-root.score, root.multipv, root.move))
    best = roots[0]
    allowed = [root for root in roots if best.score - root.score <= score_cap]
    if len(allowed) == 1:
        return best
    if draw_u64(seed, trajectory, ply, "best-weight") % 4 != 0:
        return best
    alternatives = allowed[1:]
    index = draw_u64(seed, trajectory, ply, "alternative") % len(alternatives)
    return alternatives[index]


def select_complete_frame(output: list[str]) -> list[RootMove]:
    frames: dict[int, dict[int, RootMove]] = {}
    for line in output:
        root = parse_root_move(line)
        if root:
            frames.setdefault(root.depth, {})[root.multipv] = root
    for depth in sorted(frames, reverse=True):
        ordered = sorted(
            frames[depth].values(),
            key=lambda root: (-root.score, root.multipv, root.move),
        )
        distinct: list[RootMove] = []
        seen_moves: set[str] = set()
        for root in ordered:
            if root.move in seen_moves:
                continue
            seen_moves.add(root.move)
            distinct.append(root)
        if len(distinct) >= 2:
            return distinct
    return []


class UciEngine:
    def __init__(self, engine: Path, network: Path, nodes: int, multipv: int):
        self.engine = engine
        self.network = network
        self.nodes = nodes
        self.multipv = multipv
        self.process: subprocess.Popen[str] | None = None

    def __enter__(self) -> "UciEngine":
        self.process = subprocess.Popen(
            [str(self.engine)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.send("uci")
        uci = self.read_until("uciok")
        if not any(line.startswith("id name Horde-Stockfish") for line in uci):
            raise RuntimeError("candidate generator requires Horde-Stockfish")
        self.send("setoption name Threads value 1")
        self.send("setoption name Hash value 16")
        self.send(f"setoption name MultiPV value {self.multipv}")
        self.send(f"setoption name EvalFile value {self.network}")
        self.send("isready")
        self.read_until("readyok")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            try:
                self.send("quit")
                self.process.wait(timeout=10)
            except Exception:
                self.process.kill()
                self.process.wait()

    def send(self, command: str) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def read_until(self, marker: str) -> list[str]:
        assert self.process is not None and self.process.stdout is not None
        output: list[str] = []
        while True:
            line = self.process.stdout.readline()
            if line == "":
                raise RuntimeError(f"engine exited before {marker!r}:\n" + "\n".join(output[-50:]))
            line = line.rstrip("\r\n")
            output.append(line)
            if marker in line:
                return output

    def new_trajectory(self) -> None:
        self.send("setoption name Clear Hash")
        self.send("isready")
        self.read_until("readyok")

    def analyze(self, moves: list[str]) -> list[RootMove]:
        command = "position startpos"
        if moves:
            command += " moves " + " ".join(moves)
        return self.analyze_command(command)

    def analyze_fen(self, fen: str) -> list[RootMove]:
        return self.analyze_command(f"position fen {fen}")

    def analyze_command(self, command: str) -> list[RootMove]:
        self.send(command)
        self.send(f"go nodes {self.nodes}")
        output = self.read_until("bestmove ")
        return select_complete_frame(output)


def canonical_fen(fen: str) -> str:
    fields = fen.split()
    if len(fields) != 6:
        raise ValueError(f"invalid six-field FEN: {fen!r}")
    return " ".join(fields[:5])


def prefix_family(moves: list[str], plies: int = 4) -> str:
    return " ".join(moves[:plies])


def eligible_board(board: chess.variant.HordeBoard) -> bool:
    return (
        not board.is_game_over(claim_draw=True)
        and board.legal_moves.count() >= 2
        and not board.is_repetition(2)
    )


def generate(
    engine: Path,
    network: Path,
    output_dir: Path,
    count: int,
    seed: int,
    nodes: int,
    multipv: int,
    score_cap: int,
    candidate_gap: int,
    min_ply: int,
    max_ply: int,
    max_attempts: int,
    prefix_share: float,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    epd_path = output_dir / "candidates.epd"
    trace_path = output_dir / "traces.jsonl"
    manifest_path = output_dir / "manifest.json"
    for path in (epd_path, trace_path, manifest_path):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path}")

    engine_sha = sha256_file(engine)
    network_sha = sha256_file(network)
    prefix_cap = max(1, math.ceil(count * prefix_share))
    seen_fens: set[str] = set()
    prefixes: Counter[str] = Counter()
    records: list[dict[str, object]] = []

    with UciEngine(engine, network, nodes, multipv) as uci:
        for trajectory in range(max_attempts):
            if len(records) >= count:
                break
            uci.new_trajectory()
            board = chess.variant.HordeBoard()
            moves: list[str] = []
            candidate_roots: list[dict[str, object]] = []
            analyses: list[dict[str, object]] = []

            for ply in range(max_ply + 1):
                if not eligible_board(board):
                    break
                roots = uci.analyze(moves)
                if len(roots) < 2:
                    break
                best, second = roots[:2]
                gap = best.score - second.score
                snapshot = {
                    "ply": ply,
                    "fen": board.fen(en_passant="fen"),
                    "side_to_move": "white" if board.turn == chess.WHITE else "black",
                    "top_two_gap": gap,
                    "best_score": best.score,
                    "roots": [root.__dict__ for root in roots],
                }
                analyses.append(snapshot)
                if (
                    min_ply <= ply <= max_ply
                    and gap <= candidate_gap
                    and best.score_kind != "mate"
                    and second.score_kind != "mate"
                ):
                    candidate_roots.append(snapshot)
                if ply == max_ply:
                    break

                selected = choose_move(roots, score_cap, seed, trajectory, ply)
                if selected.move not in {board.uci(move) for move in board.legal_moves}:
                    raise RuntimeError(f"engine returned illegal move {selected.move} at ply {ply}")
                board.push_uci(selected.move)
                moves.append(selected.move)

            desired_side = "white" if len(records) % 2 == 0 else "black"
            candidates = [root for root in candidate_roots if root["side_to_move"] == desired_side]
            if not candidates:
                continue
            candidate = min(
                candidates,
                key=lambda root: (
                    int(root["top_two_gap"]),
                    abs(int(root["best_score"])),
                    hashlib.sha256(str(root["fen"]).encode("ascii")).hexdigest(),
                ),
            )
            fen_key = canonical_fen(str(candidate["fen"]))
            family_moves = moves[: int(candidate["ply"])]
            family = prefix_family(family_moves)
            if fen_key in seen_fens or prefixes[family] >= prefix_cap:
                continue

            seen_fens.add(fen_key)
            prefixes[family] += 1
            records.append(
                {
                    "trajectory_id": trajectory,
                    "fen": candidate["fen"],
                    "canonical_fen": fen_key,
                    "ply": candidate["ply"],
                    "side_to_move": candidate["side_to_move"],
                    "top_two_gap": candidate["top_two_gap"],
                    "best_score": candidate["best_score"],
                    "prefix_family": family,
                    "moves_to_candidate": family_moves,
                    "complete_move_trace": moves,
                    "analyses": analyses,
                }
            )

    if len(records) != count:
        raise RuntimeError(f"generated {len(records)} candidates, expected {count}")

    with epd_path.open("w", encoding="ascii", newline="\n") as handle:
        for record in records:
            handle.write(str(record["fen"]) + ";\n")
    with trace_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    manifest = {
        "schema": "HORDE_BOOK_CANDIDATE_GENERATION_V1",
        "engine": str(engine.resolve()),
        "engine_sha256": engine_sha,
        "network": str(network.resolve()),
        "network_sha256": network_sha,
        "python": platform.python_version(),
        "python_chess": chess.__version__,
        "prng": "SHA256 counter draws",
        "settings": {
            "count": count,
            "seed": seed,
            "nodes": nodes,
            "multipv": multipv,
            "root_move_policy": "distinct",
            "best_move_weight": 0.75,
            "score_cap": score_cap,
            "candidate_gap": candidate_gap,
            "min_ply": min_ply,
            "max_ply": max_ply,
            "max_attempts": max_attempts,
            "prefix_share": prefix_share,
            "prefix_cap": prefix_cap,
        },
        "outputs": {
            "epd": {"path": epd_path.name, "sha256": sha256_file(epd_path)},
            "traces": {"path": trace_path.name, "sha256": sha256_file(trace_path)},
        },
        "counts": {
            "records": len(records),
            "white_to_move": sum(record["side_to_move"] == "white" for record in records),
            "black_to_move": sum(record["side_to_move"] == "black" for record in records),
            "canonical_unique": len(seen_fens),
            "prefix_families": len(prefixes),
            "max_prefix_count": max(prefixes.values(), default=0),
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--network", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--count", type=int, default=256)
    parser.add_argument("--seed", type=int, default=20260807)
    parser.add_argument("--nodes", type=int, default=2000)
    parser.add_argument("--multipv", type=int, default=4)
    parser.add_argument("--score-cap", type=int, default=50)
    parser.add_argument("--candidate-gap", type=int, default=80)
    parser.add_argument("--min-ply", type=int, default=8)
    parser.add_argument("--max-ply", type=int, default=40)
    parser.add_argument("--max-attempts", type=int, default=20000)
    parser.add_argument("--prefix-share", type=float, default=0.01)
    args = parser.parse_args()

    if not 1 <= args.count <= 100_000:
        parser.error("--count must be between 1 and 100000")
    if not 1 <= args.nodes <= 10_000_000:
        parser.error("--nodes must be between 1 and 10000000")
    if not 2 <= args.multipv <= 16:
        parser.error("--multipv must be between 2 and 16")
    if not 0 <= args.min_ply <= args.max_ply <= 128:
        parser.error("plies must satisfy 0 <= min-ply <= max-ply <= 128")
    if not 0 < args.prefix_share <= 1:
        parser.error("--prefix-share must be in (0, 1]")

    manifest = generate(
        engine=args.engine.resolve(),
        network=args.network.resolve(),
        output_dir=args.output_dir.resolve(),
        count=args.count,
        seed=args.seed,
        nodes=args.nodes,
        multipv=args.multipv,
        score_cap=args.score_cap,
        candidate_gap=args.candidate_gap,
        min_ply=args.min_ply,
        max_ply=args.max_ply,
        max_attempts=args.max_attempts,
        prefix_share=args.prefix_share,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
