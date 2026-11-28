#!/usr/bin/env python3
"""Probe latency of python-chess (v1's prober) over the positions of a
tb_golden.py file, warm (each position probed twice, the second timed).

  python tb_time.py --tables "DIR[;DIR]" golden.jsonl [max]
"""

import argparse
import collections
import json
import time

import chess.syzygy
import chess.variant

ap = argparse.ArgumentParser()
ap.add_argument('--tables', required=True)
ap.add_argument('file')
ap.add_argument('max', nargs='?', type=int, default=20000)
args = ap.parse_args()
paths = [p for p in args.tables.split(';') if p]
tb = chess.syzygy.open_tablebase(paths[0], VariantBoard=chess.variant.AtomicBoard)
for p in paths[1:]:
    tb.add_directory(p)
fens = []
with open(args.file) as fh:
    for line in fh:
        rec = json.loads(line)
        if rec['terminal'] is None:
            fens.append((rec['men'], rec['fen']))
        if len(fens) >= args.max:
            break
stats = collections.defaultdict(lambda: [0, 0.0])
for men, fen in fens:
    try:
        tb.probe_wdl(chess.variant.AtomicBoard(fen))
    except Exception:
        pass
    t = time.perf_counter()
    try:
        tb.probe_wdl(chess.variant.AtomicBoard(fen))
    except Exception:
        pass
    dt = time.perf_counter() - t
    stats[men][0] += 1
    stats[men][1] += dt
for men in sorted(stats):
    n, s = stats[men]
    print(f'{men} men: {n} probes, {s / n * 1e6:.1f} us/probe (python-chess, warm)')
