"""Driver UCI minimo para el analisis MultiPV de una posicion.
Usado por el runner local (M1) y por el worker distribuido (M2)."""

import re
import subprocess


class Engine:
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
        """Devuelve lines=[{'move','eval_cp','mate','pv'}] perspectiva blanca.
        Sin ucinewgame a proposito: la TT sobrevive entre tareas, asi que las
        revisitas y los vecinos del mismo lote arrancan en caliente."""
        self._send(f'setoption name MultiPV value {multipv}')
        self._send(f'position fen {fen}')
        go = f'go nodes {nodes}'
        if searchmoves:
            go += ' searchmoves ' + ' '.join(searchmoves)
        self._send(go)
        best, lines = {}, {}
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
                    # scores TB del motor (fuera de escala de mate): clamp para
                    # que prioricen (banda >=9000) sin fingir distancia de mate
                    val = max(-9_500, min(9_500, val))
                    entry['eval_cp'] = val if stm_white else -val
                else:
                    entry['mate'] = val if stm_white else -val
                    entry['eval_cp'] = (10_000 - abs(val)) * (1 if entry['mate'] > 0 else -1)
                lines[idx] = entry
        return [lines[i] for i in sorted(lines)]

    def close(self):
        try:
            self._send('quit')
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()
