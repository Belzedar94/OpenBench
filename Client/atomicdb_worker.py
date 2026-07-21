#!/usr/bin/env python3
"""Worker de AtomicDB: pide lotes de analisis, corre Atomic-Stockfish y
devuelve MultiPV. Independiente del client.py de OpenBench (deliberado:
cero riesgo sobre SPRT/DATAGEN). Uso:

  python atomicdb_worker.py -U user -P pass -S https://servidor \
      --engine ruta/Atomic-Stockfish.exe -T 8 [--once]
"""

import argparse
import json
import sys
import time

import requests

sys.path.insert(0, '..')
from atomicdb.engine import Engine  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-U', required=True)
    ap.add_argument('-P', required=True)
    ap.add_argument('-S', required=True)
    ap.add_argument('--engine', required=True)
    ap.add_argument('-T', type=int, default=4)
    ap.add_argument('--hash', type=int, default=512)
    ap.add_argument('--once', action='store_true')
    a = ap.parse_args()

    auth = {'username': a.U, 'password': a.P, 'machine': f'{a.U}-atomicdb'}
    eng = Engine(a.engine, threads=a.T, hash_mb=a.hash)
    print(f'AtomicDB worker: {a.engine} T={a.T} -> {a.S}', flush=True)

    while True:
        try:
            r = requests.post(a.S + '/atomicdb/api/lease', data=auth, timeout=60)
            tasks = r.json().get('tasks', [])
        except Exception as e:
            print(f'lease error: {e}; reintento en 30s', flush=True)
            time.sleep(30)
            continue
        if not tasks:
            print('sin tareas; espero 60s', flush=True)
            if a.once:
                break
            time.sleep(60)
            continue
        for t in tasks:
            t0 = time.time()
            lines = eng.analyse(t['fen'], t['budget_nodes'], t['multipv'])
            try:
                rr = requests.post(a.S + '/atomicdb/api/submit', data={
                    **auth, 'task_id': t['id'], 'lines': json.dumps(lines),
                }, timeout=120)
                s = rr.json().get('summary', rr.json())
            except Exception as e:
                s = f'submit error: {e}'
            print(f"task {t['id']} ({t['budget_nodes']}n, "
                  f"{time.time()-t0:.1f}s) -> {s}", flush=True)
        if a.once:
            break
    eng.close()


if __name__ == '__main__':
    main()
