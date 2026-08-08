#!/usr/bin/env python3
"""Measure paired-game information supplied by a Horde opening book."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Iterable, Iterator


HEADER_RE = re.compile(r'^\[([^ ]+) "(.*)"\]$')
VALID_RESULTS = {"1-0", "0-1", "1/2-1/2"}


@dataclass(frozen=True)
class Game:
    source: str
    ordinal: int
    white: str
    black: str
    result: str
    fen: str
    termination: str


def iter_headers(path: Path) -> Iterator[dict[str, str]]:
    """Yield PGN header dictionaries without parsing expensive movetext."""

    headers: dict[str, str] = {}
    movetext_started = False

    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\r\n")
            match = HEADER_RE.fullmatch(line)
            if match:
                key, value = match.groups()
                if key == "Event" and headers and movetext_started:
                    yield headers
                    headers = {}
                    movetext_started = False
                headers[key] = value
            elif headers and line.strip():
                movetext_started = True

    if headers:
        yield headers


def read_games(paths: Iterable[Path]) -> list[Game]:
    games: list[Game] = []
    required = {"White", "Black", "Result", "FEN"}

    for path in paths:
        for ordinal, headers in enumerate(iter_headers(path), start=1):
            missing = required - headers.keys()
            if missing:
                raise ValueError(
                    f"{path}: game {ordinal} is missing headers {sorted(missing)}"
                )
            if headers["Result"] not in VALID_RESULTS:
                raise ValueError(
                    f"{path}: game {ordinal} has unsupported result {headers['Result']!r}"
                )
            games.append(
                Game(
                    source=str(path.resolve()),
                    ordinal=ordinal,
                    white=headers["White"],
                    black=headers["Black"],
                    result=headers["Result"],
                    fen=headers["FEN"],
                    termination=headers.get("Termination", "").strip(),
                )
            )

    return games


def canonical_fen(fen: str) -> str:
    """Keep every rule-relevant field while ignoring the full-move number."""

    fields = fen.split()
    if len(fields) != 6:
        raise ValueError(f"invalid six-field FEN: {fen!r}")
    return " ".join(fields[:5])


def engine_score(game: Game, engine: str) -> float:
    if engine not in {game.white, game.black}:
        raise ValueError(f"engine {engine!r} is absent from game {game.ordinal}")
    if game.result == "1/2-1/2":
        return 0.5
    winner = game.white if game.result == "1-0" else game.black
    return float(winner == engine)


def winner_color(game: Game) -> str:
    if game.result == "1/2-1/2":
        return "draw"
    return "white" if game.result == "1-0" else "black"


def opening_side(game: Game) -> str:
    fields = game.fen.split()
    if len(fields) != 6 or fields[1] not in {"w", "b"}:
        raise ValueError(f"invalid six-field FEN: {game.fen!r}")
    return "white" if fields[1] == "w" else "black"


def summarize_pairs(
    pair_scores: list[float], color_outcomes: Counter[str]
) -> dict[str, object]:
    pair_count = len(pair_scores)
    assignment_decisive = sum(score != 1.0 for score in pair_scores)
    squared_displacement = sum((score - 1.0) ** 2 for score in pair_scores)
    penta = Counter(pair_scores)

    return {
        "pairs": pair_count,
        "pentanomial": [penta[score] for score in (0.0, 0.5, 1.0, 1.5, 2.0)],
        "pair_metrics": {
            "assignment_decisive_rate": assignment_decisive / pair_count if pair_count else 0.0,
            "middle_pair_rate": 1.0 - assignment_decisive / pair_count if pair_count else 0.0,
            "squared_pair_displacement": squared_displacement / pair_count if pair_count else 0.0,
            "black_black_rate": color_outcomes["black_black"] / pair_count if pair_count else 0.0,
            "white_white_rate": color_outcomes["white_white"] / pair_count if pair_count else 0.0,
            "draw_draw_rate": color_outcomes["draw_draw"] / pair_count if pair_count else 0.0,
            "mixed_rate": color_outcomes["mixed"] / pair_count if pair_count else 0.0,
        },
    }


def resolve_engines(
    games: list[Game], dev_name: str | None, base_name: str | None
) -> tuple[str, str]:
    names = sorted({name for game in games for name in (game.white, game.black)})
    if dev_name or base_name:
        if not dev_name or not base_name:
            raise ValueError("--dev-name and --base-name must be supplied together")
        if set(names) != {dev_name, base_name}:
            raise ValueError(
                f"PGNs contain engines {names}, not exactly {dev_name!r} and {base_name!r}"
            )
        return dev_name, base_name

    if len(names) != 2:
        raise ValueError(f"expected exactly two engine names, found {names}")

    dev_candidates = [name for name in names if name.casefold().endswith("dev")]
    if len(dev_candidates) == 1:
        dev = dev_candidates[0]
        return dev, next(name for name in names if name != dev)
    return names[0], names[1]


def pair_games(games: list[Game]) -> tuple[list[tuple[Game, Game]], list[Game]]:
    by_source: dict[str, list[Game]] = defaultdict(list)
    for game in games:
        by_source[game.source].append(game)

    pairs: list[tuple[Game, Game]] = []
    incomplete: list[Game] = []
    for source_games in by_source.values():
        source_games.sort(key=lambda game: game.ordinal)
        for offset in range(0, len(source_games), 2):
            block = source_games[offset : offset + 2]
            if len(block) == 1:
                incomplete.extend(block)
                continue
            first, second = block
            if canonical_fen(first.fen) != canonical_fen(second.fen):
                raise ValueError(
                    f"{first.source}: games {first.ordinal}/{second.ordinal} use different openings"
                )
            if first.white != second.black or first.black != second.white:
                raise ValueError(
                    f"{first.source}: games {first.ordinal}/{second.ordinal} do not swap engines"
                )
            pairs.append((first, second))

    return pairs, incomplete


def analyze(
    paths: list[Path], dev_name: str | None = None, base_name: str | None = None
) -> dict[str, object]:
    games = read_games(paths)
    if not games:
        raise ValueError("no games found")
    dev, base = resolve_engines(games, dev_name, base_name)
    pairs, incomplete = pair_games(games)

    color_outcomes = Counter()
    side_pair_scores: dict[str, list[float]] = defaultdict(list)
    side_color_outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    result_counts = Counter(game.result for game in games)
    termination_counts = Counter(game.termination or "normal" for game in games)
    abnormal_games = [
        {
            "source": game.source,
            "ordinal": game.ordinal,
            "termination": game.termination,
        }
        for game in games
        if game.termination.casefold() not in {"", "normal"}
    ]
    fen_counts = Counter()
    fen_pair_scores: dict[str, list[float]] = defaultdict(list)
    pair_scores: list[float] = []

    for first, second in pairs:
        score = engine_score(first, dev) + engine_score(second, dev)
        pair_scores.append(score)
        side = opening_side(first)
        side_pair_scores[side].append(score)

        colors = (winner_color(first), winner_color(second))
        if colors == ("black", "black"):
            color_key = "black_black"
        elif colors == ("white", "white"):
            color_key = "white_white"
        elif colors == ("draw", "draw"):
            color_key = "draw_draw"
        else:
            color_key = "mixed"
        color_outcomes[color_key] += 1
        side_color_outcomes[side][color_key] += 1

        fen = canonical_fen(first.fen)
        fen_counts[fen] += 1
        fen_pair_scores[fen].append(score)

    pair_count = len(pair_scores)
    pair_summary = summarize_pairs(pair_scores, color_outcomes)
    black_wins = sum(game.result == "0-1" for game in games)
    white_wins = sum(game.result == "1-0" for game in games)

    most_reused = [
        {
            "fen": fen,
            "pairs": count,
            "middle_pairs": sum(score == 1.0 for score in fen_pair_scores[fen]),
        }
        for fen, count in sorted(fen_counts.items(), key=lambda item: (-item[1], item[0]))[:20]
    ]

    return {
        "schema": "HORDE_BOOK_PAIR_INFORMATION_V1",
        "inputs": [str(path.resolve()) for path in paths],
        "engines": {"dev": dev, "base": base},
        "games": len(games),
        "complete_pairs": pair_count,
        "incomplete_games": len(incomplete),
        "game_results": dict(sorted(result_counts.items())),
        "terminations": dict(sorted(termination_counts.items())),
        "abnormal_terminations": len(abnormal_games),
        "abnormal_games": abnormal_games,
        "color_rates": {
            "black_win": black_wins / len(games),
            "white_win": white_wins / len(games),
            "draw": result_counts["1/2-1/2"] / len(games),
        },
        "pentanomial": pair_summary["pentanomial"],
        "pair_metrics": pair_summary["pair_metrics"],
        "opening_side_strata": {
            side: summarize_pairs(side_pair_scores[side], side_color_outcomes[side])
            for side in ("white", "black")
            if side_pair_scores[side]
        },
        "opening_reuse": {
            "canonical_openings": len(fen_counts),
            "pairs_per_canonical_opening": pair_count / len(fen_counts) if fen_counts else 0.0,
            "max_pairs_for_one_opening": max(fen_counts.values(), default=0),
            "most_reused": most_reused,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pgn", type=Path, nargs="+")
    parser.add_argument("--dev-name")
    parser.add_argument("--base-name")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    payload = analyze(args.pgn, args.dev_name, args.base_name)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
