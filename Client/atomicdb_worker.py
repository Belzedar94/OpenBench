#!/usr/bin/env python3
"""Worker de AtomicDB: pide lotes de analisis, corre Atomic-Stockfish y
devuelve MultiPV. Independiente del client.py de OpenBench (deliberado:
cero riesgo sobre SPRT/DATAGEN). Uso minimo (el motor se descarga solo):

  python atomicdb_worker.py -U user -P pass -S https://servidor -T 8

--engine ruta/binario permite usar una build propia en su lugar.
"""

import argparse
import json
import sys
import time

import requests

sys.path.insert(0, '..')
from atomicdb.engine import Engine  # noqa: E402


def _fetch_verified(server, entry, dest):
    """Descarga un archivo del manifest con sha256 verificado; reutiliza la
    copia local si ya coincide."""
    import hashlib
    import os
    import stat

    def _ok():
        with open(dest, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest() == entry['sha256']

    if not (os.path.exists(dest) and _ok()):
        print(f"downloading {entry['file']} ({entry['size_mb']} MB)...",
              flush=True)
        r = requests.get(server + '/atomicdb/engines/' + entry['file'],
                         timeout=600)
        r.raise_for_status()
        if hashlib.sha256(r.content).hexdigest() != entry['sha256']:
            sys.exit(f"download of {entry['file']} failed the sha256 check")
        with open(dest, 'wb') as f:
            f.write(r.content)
        if os.name != 'nt':
            os.chmod(dest, os.stat(dest).st_mode | stat.S_IEXEC)


def provision_engine(server):
    """Descarga el motor de referencia (y su red NNUE si el binario no la
    lleva embebida), todo sha256-verificado."""
    import os
    import platform

    key = f'{platform.system().lower()}-{platform.machine().lower()}'
    key = {'windows-amd64': 'windows-x86_64',
           'linux-amd64': 'linux-x86_64'}.get(key, key)
    man = requests.get(server + '/atomicdb/engines/manifest.json',
                       timeout=60).json()
    if key not in man['binaries']:
        sys.exit(f'no prebuilt engine for {key}: pass --engine with your own '
                 f'build (prebuilts: {", ".join(sorted(man["binaries"]))})')
    b = man['binaries'][key]
    os.makedirs('Engines', exist_ok=True)
    dest = os.path.join('Engines', b['file'])
    _fetch_verified(server, b, dest)
    if b.get('needs_net') and 'net' in man:
        # la red va al cwd del worker: el default EvalFile del motor la
        # resuelve desde ahi
        _fetch_verified(server, man['net'], man['net']['file'])
    print(f'engine ready: {dest}', flush=True)
    return dest


def probe_tb(tb, fen):
    if tb is None:
        return None
    parts = fen.split()
    if parts[2] != '-' or parts[3] != '-':
        return None
    if sum(ch.isalpha() for ch in parts[0]) > 6:
        return None
    try:
        import chess.variant
        board = chess.variant.AtomicBoard(fen)
        return tb.probe_wdl(board)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-U', required=True)
    ap.add_argument('-P', required=True)
    ap.add_argument('-S', required=True)
    ap.add_argument('--engine', default='',
                    help='binario propio (opcional; sin el se descarga el de referencia)')
    ap.add_argument('-T', type=int, default=4)
    ap.add_argument('--hash', type=int, default=512)
    ap.add_argument('--syzygy', default='', help='dirs de TB atomicas separados por ;')
    ap.add_argument('--once', action='store_true')
    a = ap.parse_args()

    tb = None
    if a.syzygy:
        import chess.syzygy
        import chess.variant
        dirs = [d for d in a.syzygy.split(';') if d]
        tb = chess.syzygy.open_tablebase(dirs[0],
                                         VariantBoard=chess.variant.AtomicBoard)
        for d in dirs[1:]:
            tb.add_directory(d)
        print(f'syzygy: {len(dirs)} dirs', flush=True)

    import platform
    if not a.engine:
        a.engine = provision_engine(a.S)
    auth = {'username': a.U, 'password': a.P, 'machine': f'{a.U}-atomicdb',
            'threads': a.T, 'hash': a.hash,
            'os': f'{platform.system()} {platform.release()}'}
    eng = Engine(a.engine, threads=a.T, hash_mb=a.hash, syzygy=a.syzygy)
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
            wdl = probe_tb(tb, t['fen'])
            if wdl is not None:
                try:
                    rr = requests.post(a.S + '/atomicdb/api/submit', data={
                        **auth, 'task_id': t['id'], 'lines': '[]',
                        'elapsed': f'{time.time() - t0:.2f}',
                        'tb_wdl': wdl}, timeout=60)
                    print(f"task {t['id']} TB wdl={wdl} -> "
                          f"{rr.json().get('summary')}", flush=True)
                except Exception as e:
                    print(f'tb submit error: {e}', flush=True)
                continue
            lines = eng.analyse(t['fen'], t['budget_nodes'], t['multipv'])
            try:
                rr = requests.post(a.S + '/atomicdb/api/submit', data={
                    **auth, 'task_id': t['id'], 'lines': json.dumps(lines),
                    'elapsed': f'{time.time() - t0:.2f}',
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
