#!/usr/bin/env python3
"""v1's tablebase answers (python-chess, as atomicdb/tb.py probes) for random
atomic endgames, for av2-tb's comparison with shakmaty-syzygy.

Positions: random walks from real positions (--seeds), or random material and placement with 3..MAX men, kept only when the
Fairy-Stockfish movegen (pyffish, through v1's logic.py) says the position is
valid and not terminal, plus every child of each (one ply: captures shrink
the material, some children are terminal).  Each line:

    {"fen": canonical FEN, "men": n, "terminal": logic.terminal_status or null,
     "wdl": tb.probe_wdl or null, "err": exception class or null,
     "dtz": probe_dtz or null, "opp_check": side not to move in check}

  python tb_golden.py --logic atomicdb/logic.py --tables DIR[;DIR] --count N --men 3,4,5 --seed S
"""

import argparse
import importlib.util
import json
import random
import sys

import chess
import chess.syzygy
import chess.variant

PIECES = 'QRBNP'


def load_logic(path):
    spec = importlib.util.spec_from_file_location('v1_logic', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def random_fen(rng, men):
    squares = rng.sample(range(64), men)
    board = [None] * 64
    board[squares[0]] = 'K'
    board[squares[1]] = 'k'
    for sq in squares[2:]:
        p = rng.choice(PIECES)
        if p == 'P' and sq // 8 in (0, 7):
            p = rng.choice('QRBN')
        board[sq] = p if rng.random() < 0.5 else p.lower()
    rows = []
    for rank in range(7, -1, -1):
        row, empty = '', 0
        for file in range(8):
            p = board[rank * 8 + file]
            if p is None:
                empty += 1
            else:
                if empty:
                    row += str(empty)
                    empty = 0
                row += p
        if empty:
            row += str(empty)
        rows.append(row)
    return '/'.join(rows) + (' w' if rng.random() < 0.5 else ' b') + ' - - 0 1'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--logic', required=True)
    ap.add_argument('--tables', required=True)
    ap.add_argument('--count', type=int, default=1000)
    ap.add_argument('--men', default='3,4,5')
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--max-pieces', type=int, default=6)
    ap.add_argument('--no-children', action='store_true')
    ap.add_argument('--seeds', help='TSV of real positions (FEN in column 2): '
                    'random walks from each instead of random placement')
    ap.add_argument('--walks', type=int, default=8)
    ap.add_argument('--depth', type=int, default=12)
    ap.add_argument('--keep-illegal', action='store_true',
                    help='keep roots where the side not to move is in check')
    args = ap.parse_args()
    logic = load_logic(args.logic)
    pf = logic.pf
    paths = [p for p in args.tables.split(';') if p]
    tb = chess.syzygy.open_tablebase(paths[0], VariantBoard=chess.variant.AtomicBoard)
    for p in paths[1:]:
        tb.add_directory(p)
    rng = random.Random(args.seed)
    men_choices = [int(m) for m in args.men.split(',')]
    seen = set()
    written = 0

    def emit(fen):
        nonlocal written
        if fen in seen:
            return
        seen.add(fen)
        parts = fen.split()
        if not (parts[2] == '-' and parts[3] == '-'
                and logic.piece_count(fen) <= args.max_pieces):
            return
        t = logic.terminal_status(fen)
        opp = bool(chess.variant.AtomicBoard(fen).status()
                   & chess.STATUS_OPPOSITE_CHECK)
        rec = {'fen': fen, 'men': logic.piece_count(fen),
               'terminal': t[0] if t else None, 'wdl': None, 'err': None,
               'dtz': None, 'opp_check': opp}
        try:
            rec['wdl'] = tb.probe_wdl(chess.variant.AtomicBoard(fen))
        except Exception as exc:  # tb.probe_wdl returns None on any exception
            rec['err'] = type(exc).__name__
        try:
            rec['dtz'] = tb.probe_dtz(chess.variant.AtomicBoard(fen))
        except Exception:
            pass
        print(json.dumps(rec))
        written += 1

    if args.seeds:
        roots = 0
        for line in open(args.seeds, encoding='utf-8'):
            seed = logic.canonical_fen(line.rstrip('\n').split('\t')[1])
            roots += 1
            emit(seed)
            for _ in range(args.walks):
                fen = seed
                for _ply in range(args.depth):
                    moves = logic.legal_moves(fen)
                    if not moves or logic.terminal_status(fen):
                        break
                    fen = logic.apply_move(fen, rng.choice(moves))
                    emit(fen)
        print(f'{roots} seeds, {written} positions', file=sys.stderr)
        return

    roots = 0
    while roots < args.count:
        fen = random_fen(rng, rng.choice(men_choices))
        if pf.validate_fen(fen, logic.VARIANT) != 1:
            continue
        fen = logic.canonical_fen(fen)
        if logic.terminal_status(fen) is not None:
            continue
        # No legal move leaves the mover's own king in check, so such a root
        # is not a position of the game (python-chess: opposite check).
        if not args.keep_illegal and (chess.variant.AtomicBoard(fen).status()
                                      & chess.STATUS_OPPOSITE_CHECK):
            continue
        roots += 1
        emit(fen)
        if not args.no_children:
            for uci in logic.legal_moves(fen):
                emit(logic.apply_move(fen, uci))
    print(f'{roots} roots, {written} positions', file=sys.stderr)


if __name__ == '__main__':
    main()
