#!/usr/bin/env python3
"""v1's cost of what examples/bench.rs measures, on the same positions:
legal_moves, key_of and the pure half of ingest.expand (child FEN, key and
terminal status per legal move), with v1's memo caches cleared before each
measurement (a writer meets new positions almost always).

  python v1_bench.py --logic atomicdb/logic.py golden.jsonl [max]
"""

import argparse
import importlib.util
import json
import time

ap = argparse.ArgumentParser()
ap.add_argument('--logic', required=True)
ap.add_argument('file')
ap.add_argument('max', nargs='?', type=int, default=5000)
args = ap.parse_args()
spec = importlib.util.spec_from_file_location('v1_logic', args.logic)
logic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logic)

fens = []
with open(args.file) as fh:
    for line in fh:
        fens.append(json.loads(line)['canon'])
        if len(fens) >= args.max:
            break


def expansion(fen):
    out = []
    for uci in logic.legal_moves(fen):
        child = logic.canonical_fen(logic.apply_move(fen, uci))
        out.append((uci, child, logic.key_of(child), logic.terminal_status(child)))
    return out


print(f'{len(fens)} positions; pyffish {logic.pf.info()}')
for name, fn in (('legal_moves', logic.legal_moves), ('key_of', logic.key_of),
                 ('expansion', expansion)):
    logic.clear_caches()
    t = time.perf_counter()
    for fen in fens:
        fn(fen)
    dt = time.perf_counter() - t
    print(f'{name:>12}: {dt / len(fens) * 1e6:9.2f} us/position (1 thread, cold memo)')
