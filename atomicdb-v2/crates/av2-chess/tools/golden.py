#!/usr/bin/env python3
"""Write v1's answers for a set of positions, for av2-chess's equivalence check.

Runs v1's own ``atomicdb/logic.py`` (loaded by path, so Django is not needed)
on top of whatever ``pyffish`` is installed, and writes one JSON line per
position:

    {"fen": input FEN as given,
     "canon": logic.canonical_fen(fen), "key": logic.key_of(fen),
     "db_key": the key stored in production (prod mode only),
     "moves": logic.legal_moves(canon)          (movegen order),
     "raw": [pf.get_fen(canon, [m]) ...]        (counters included),
     "kids": [[logic.apply_move(canon, m), key, logic.is_zeroing(canon, m)] ...],
     "kid_terminal": [logic.terminal_status(child) ...],
     "terminal": logic.terminal_status(canon), "imm": pf.is_immediate_game_end,
     "result": pf.game_result (only for terminal positions),
     "tb6": logic.tb_applicable(canon, 6)}

Modes:
  playout  --games N --plies P --seed S   random games from the start position
           (every position met, as the movegen writes it, counters included)
  fens     --input FILE                    one FEN per line (optionally "key<TAB>fen")

  python golden.py --logic path/to/atomicdb/logic.py playout --games 2000 --seed 1 > out.jsonl
"""

import argparse
import importlib.util
import json
import random
import sys


def load_logic(path):
    spec = importlib.util.spec_from_file_location('v1_logic', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def record(logic, fen, db_key=None):
    pf = logic.pf
    canon = logic.canonical_fen(fen)
    moves = logic.legal_moves(canon)
    raw = [pf.get_fen(logic.VARIANT, canon, [m]) for m in moves]
    kids = []
    kid_terminal = []
    for m in moves:
        child = logic.apply_move(canon, m)
        kids.append([child, logic.key_of(child), logic.is_zeroing(canon, m)])
        t = logic.terminal_status(child)
        kid_terminal.append(t[0] if t else None)
    t = logic.terminal_status(canon)
    imm = pf.is_immediate_game_end(logic.VARIANT, canon, [])
    rec = {
        'fen': fen, 'canon': canon, 'key': logic.key_of(fen),
        'moves': moves, 'raw': raw, 'kids': kids, 'kid_terminal': kid_terminal,
        'terminal': t[0] if t else None, 'imm': [bool(imm[0]), int(imm[1])],
        'result': pf.game_result(logic.VARIANT, canon, []) if t else None,
        'tb6': logic.tb_applicable(canon, 6),
    }
    if db_key is not None:
        rec['db_key'] = db_key
    return rec


def playout(logic, games, plies, seed, out):
    pf = logic.pf
    rng = random.Random(seed)
    start = pf.start_fen(logic.VARIANT)
    n = 0
    for _ in range(games):
        fen = start
        for _ply in range(plies):
            out.write(json.dumps(record(logic, fen)) + '\n')
            n += 1
            moves = pf.legal_moves(logic.VARIANT, fen, [])
            if not moves or logic.terminal_status(fen):
                break
            fen = pf.get_fen(logic.VARIANT, fen, [rng.choice(moves)])
    return n


def from_file(logic, path, out):
    n = 0
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.rstrip('\n')
            if not line:
                continue
            db_key, _, fen = line.rpartition('\t')
            out.write(json.dumps(record(logic, fen, db_key or None)) + '\n')
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--logic', required=True)
    sub = ap.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('playout')
    p.add_argument('--games', type=int, default=1000)
    p.add_argument('--plies', type=int, default=300)
    p.add_argument('--seed', type=int, default=20261128)
    f = sub.add_parser('fens')
    f.add_argument('--input', required=True)
    args = ap.parse_args()
    logic = load_logic(args.logic)
    out = sys.stdout
    if args.mode == 'playout':
        n = playout(logic, args.games, args.plies, args.seed, out)
    else:
        n = from_file(logic, args.input, out)
    print(f'{n} positions; pyffish {logic.pf.version()} {logic.pf.info()}',
          file=sys.stderr)


if __name__ == '__main__':
    main()
