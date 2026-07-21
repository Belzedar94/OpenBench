#!/usr/bin/env python3
"""Worker de AtomicDB: pide lotes de analisis, corre Atomic-Stockfish y
devuelve MultiPV. Independiente del client.py de OpenBench (deliberado:
cero riesgo sobre SPRT/DATAGEN). Uso minimo (el motor se descarga solo):

  python atomicdb_worker.py -U user -P pass -S https://servidor -T 8

--engine ruta/binario permite usar una build propia en su lugar.
"""

import argparse
import json
import re
import subprocess
import sys
import time

import requests


class Engine:
    """Driver UCI minimo, embebido para que este archivo sea autocontenido."""

    def __init__(self, path, threads=1, hash_mb=256, syzygy=''):
        self.p = subprocess.Popen([path], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True, bufsize=1)
        self._send('uci')
        self._wait('uciok')
        self._send(f'setoption name Threads value {threads}')
        self._send(f'setoption name Hash value {hash_mb}')
        if syzygy:
            self._send(f'setoption name SyzygyPath value {syzygy}')
        self._send('isready')
        self._wait('readyok')

    def _send(self, line):
        self.p.stdin.write(line + '\n')
        self.p.stdin.flush()

    def _wait(self, token):
        while True:
            line = self.p.stdout.readline()
            if not line or line.startswith(token):
                return

    def analyse(self, fen, nodes, multipv, searchmoves=None):
        """Devuelve lines=[{'move','eval_cp','mate','pv','raw'}] White-POV.
        Sin ucinewgame a proposito: la TT sobrevive entre tareas.
        searchmoves restringe la raiz a las jugadas vivas (no pegajoso:
        aplica solo a este go, verificado empiricamente)."""
        self._send(f'setoption name MultiPV value {multipv}')
        self._send(f'position fen {fen}')
        go = f'go nodes {nodes}'
        if searchmoves:
            go += ' searchmoves ' + ' '.join(searchmoves)
        self._send(go)
        lines = {}
        stm_white = fen.split()[1] == 'w'
        while True:
            line = self.p.stdout.readline()
            if not line:
                break
            if line.startswith('bestmove'):
                break
            m = re.match(
                r'info depth \d+ seldepth \d+ multipv (\d+) score '
                r'(cp|mate) (-?\d+) .*? pv (.+)', line)
            if m:
                idx = int(m.group(1))
                kind, val = m.group(2), int(m.group(3))
                pv = m.group(4).split()
                entry = {'move': pv[0], 'pv': pv,
                         'eval_cp': None, 'mate': None,
                         'raw': line.strip()}
                if kind == 'cp':
                    # scores TB fuera de escala de mate: clamp para priorizar
                    # (banda >=9000) sin fingir distancia de mate
                    val = max(-9_500, min(9_500, val))
                    entry['eval_cp'] = val if stm_white else -val
                else:
                    entry['mate'] = val if stm_white else -val
                    entry['eval_cp'] = (10_000 - abs(val)) * (
                        1 if entry['mate'] > 0 else -1)
                lines[idx] = entry
        return [lines[i] for i in sorted(lines)]

    def close(self):
        try:
            self._send('quit')
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


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
            'threads': a.T, 'hash': a.hash, 'tb': '1' if tb else '0',
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
            lines = eng.analyse(t['fen'], t['budget_nodes'], t['multipv'],
                                t.get('searchmoves'))
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
