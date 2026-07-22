#!/usr/bin/env python3
"""Worker de AtomicDB: pide lotes de analisis, corre Atomic-Stockfish y
devuelve MultiPV. Independiente del client.py de OpenBench (deliberado:
cero riesgo sobre SPRT/DATAGEN). Uso minimo (el motor se descarga solo):

  python atomicdb_worker.py -U user -P pass -S https://servidor -T 8

--engine ruta/binario permite usar una build propia en su lugar.
"""

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import requests

# Bump this integer for every published change to this worker.  It is the
# downgrade/replay guard used by the self-updater; do not reuse a build number.
ATOMICDB_WORKER_UPDATE_PROTOCOL = 1
ATOMICDB_WORKER_BUILD = 2026072201
WORKER_UPDATE_INTERVAL_SECONDS = 30 * 60
WORKER_UPDATE_CONNECT_TIMEOUT_SECONDS = 15
WORKER_UPDATE_READ_TIMEOUT_SECONDS = 30
WORKER_UPDATE_TOTAL_TIMEOUT_SECONDS = 60
WORKER_UPDATE_MAX_BYTES = 1024 * 1024
SUBMIT_CONNECT_TIMEOUT_SECONDS = 15
SUBMIT_READ_TIMEOUT_SECONDS = 600
SUBMIT_RETRY_INITIAL_SECONDS = 15
SUBMIT_RETRY_MAX_SECONDS = 300


class WorkerUpdateError(Exception):
    pass


def _worker_build(source):
    """Validate worker structure and read its build without executing it."""
    try:
        text = source.decode('utf-8', errors='strict')
        tree = ast.parse(text)
    except (UnicodeError, SyntaxError) as exc:
        raise WorkerUpdateError(f'invalid worker source: {exc}') from exc

    names = ('ATOMICDB_WORKER_UPDATE_PROTOCOL', 'ATOMICDB_WORKER_BUILD')
    values = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        assigned = [target.id for target in node.targets
                    if isinstance(target, ast.Name) and target.id in names]
        if not assigned:
            continue
        if len(node.targets) != 1 or len(assigned) != 1:
            raise WorkerUpdateError(
                'worker version assignments must be simple')
        name = assigned[0]
        if name in values:
            raise WorkerUpdateError(f'duplicate worker assignment: {name}')
        if (not isinstance(node.value, ast.Constant)
                or not isinstance(node.value.value, int)
                or isinstance(node.value.value, bool)):
            raise WorkerUpdateError(
                f'worker assignment is not an integer: {name}')
        values[name] = node.value.value

    protocol = values.get('ATOMICDB_WORKER_UPDATE_PROTOCOL')
    build = values.get('ATOMICDB_WORKER_BUILD')
    if protocol != ATOMICDB_WORKER_UPDATE_PROTOCOL:
        raise WorkerUpdateError('unsupported or missing update protocol')
    if build is None:
        raise WorkerUpdateError('missing worker build')

    functions = {node.name for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
    required_functions = {
        'main', '_install_worker_update', '_submit_until_definitive'}
    if not required_functions.issubset(functions) or 'Engine' not in classes:
        raise WorkerUpdateError('worker source is missing required structure')

    has_main_guard = any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == '__name__'
        and len(node.test.ops) == 1
        and isinstance(node.test.ops[0], ast.Eq)
        and len(node.test.comparators) == 1
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == '__main__'
        and any(isinstance(child, ast.Expr)
                and isinstance(child.value, ast.Call)
                and isinstance(child.value.func, ast.Name)
                and child.value.func.id == 'main'
                for child in node.body)
        for node in tree.body)
    if not has_main_guard:
        raise WorkerUpdateError('worker source is missing its main guard')
    return build


def _download_worker(server):
    """Fetch the worker only from a fixed path on the exact HTTPS origin."""
    origin = urlparse(server)
    if (origin.scheme != 'https' or not origin.hostname
            or origin.username or origin.password):
        raise WorkerUpdateError(
            'auto-update requires a credential-free HTTPS -S URL')
    url = server.rstrip('/') + '/atomicdb/engines/atomicdb_worker.py'
    # The query prevents an intermediary from replaying an old cached worker.
    url += f'?current_build={ATOMICDB_WORKER_BUILD}&_={int(time.time())}'
    response = None
    try:
        response = requests.get(
            url,
            timeout=(WORKER_UPDATE_CONNECT_TIMEOUT_SECONDS,
                     WORKER_UPDATE_READ_TIMEOUT_SECONDS),
            stream=True,
            allow_redirects=False,
            headers={
                'Cache-Control': 'no-cache',
                'Accept-Encoding': 'identity',
            },
        )
        if 300 <= response.status_code < 400:
            raise WorkerUpdateError('worker download redirected')
        response.raise_for_status()
        final = urlparse(response.url)
        if ((final.scheme, final.hostname, final.port)
                != (origin.scheme, origin.hostname, origin.port)):
            raise WorkerUpdateError('worker download changed origin')
        encoding = response.headers.get('Content-Encoding', 'identity').lower()
        if encoding != 'identity':
            raise WorkerUpdateError('worker download used unexpected encoding')
        declared = response.headers.get('Content-Length')
        if declared is not None:
            try:
                declared = int(declared)
            except ValueError as exc:
                raise WorkerUpdateError('invalid worker Content-Length') from exc
            if declared < 1 or declared > WORKER_UPDATE_MAX_BYTES:
                raise WorkerUpdateError('worker download has an invalid size')
        chunks = []
        received = 0
        deadline = time.monotonic() + WORKER_UPDATE_TOTAL_TIMEOUT_SECONDS
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if time.monotonic() > deadline:
                raise WorkerUpdateError('worker download exceeded its deadline')
            if not chunk:
                continue
            received += len(chunk)
            if received > WORKER_UPDATE_MAX_BYTES:
                raise WorkerUpdateError('worker download exceeds the size limit')
            chunks.append(chunk)
        if declared is not None and received != declared:
            raise WorkerUpdateError('worker download length mismatch')
        if received == 0:
            raise WorkerUpdateError('worker download is empty')
        return b''.join(chunks)
    except requests.RequestException as exc:
        raise WorkerUpdateError(f'worker download failed: {exc}') from exc
    finally:
        if response is not None:
            response.close()


@contextmanager
def _worker_update_lock(script):
    """Best-effort cross-platform lock for workers sharing one script."""
    lock_path = script.with_name(script.name + '.update.lock')
    handle = open(lock_path, 'a+b')
    locked = False
    try:
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        locked = True
        yield True
    finally:
        if locked:
            try:
                if os.name == 'nt':
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _atomic_write(path, content, mode):
    """Write and fsync a sibling temp, then atomically replace path."""
    fd, temp_name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp',
                                     dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        if (hashlib.sha256(temp.read_bytes()).digest()
                != hashlib.sha256(content).digest()):
            raise WorkerUpdateError('staged worker hash mismatch')
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _install_worker_update(server, script_path=None):
    """Install a newer official worker. Return True when restart is needed.

    All errors are fail-open: the currently running, known-good worker remains
    in memory and callers may continue processing tasks.
    """
    script = Path(script_path or os.path.abspath(__file__))
    try:
        candidate = _download_worker(server)
        remote_build = _worker_build(candidate)
        candidate.decode('utf-8', errors='strict')
        compile(candidate, str(script), 'exec')
        if remote_build < ATOMICDB_WORKER_BUILD:
            raise WorkerUpdateError(
                f'refusing worker downgrade {ATOMICDB_WORKER_BUILD}->{remote_build}')
        if remote_build == ATOMICDB_WORKER_BUILD:
            return False
        if script.is_symlink() or not script.is_file():
            raise WorkerUpdateError('worker script is not a regular file')
        with _worker_update_lock(script) as acquired:
            if not acquired:
                return False
            current = script.read_bytes()
            disk_build = _worker_build(current)
            if current == candidate:
                # Another worker installed it while this process kept running.
                return True
            if disk_build > remote_build:
                raise WorkerUpdateError(
                    f'refusing worker downgrade {disk_build}->{remote_build}')
            if disk_build == remote_build:
                raise WorkerUpdateError(
                    f'worker build {remote_build} was reused with different bytes')
            mode = stat.S_IMODE(script.stat().st_mode)
            backup = script.with_name(script.name + '.previous')
            _atomic_write(backup, current, mode)
            _atomic_write(script, candidate, mode)
            if script.read_bytes() != candidate:
                raise WorkerUpdateError('installed worker verification failed')
        print(f'AtomicDB worker update: build {ATOMICDB_WORKER_BUILD} -> '
              f'{remote_build}; restarting between batches', flush=True)
        return True
    # Updating is optional infrastructure: a bug, permission problem or odd
    # HTTP response here must never stop a known-good analysis worker.
    except Exception as exc:
        print(f'AtomicDB worker auto-update skipped: {exc}', flush=True)
        return False


def _restart_updated_worker(script_path=None):
    """Exec the installed worker; keep running old code if exec itself fails.

    The validated new file stays on disk and ``.previous`` remains available
    for manual recovery. Automatic rollback here would race with another
    process that already restarted successfully from the shared script.
    """
    script = Path(script_path or os.path.abspath(__file__))
    try:
        os.execv(sys.executable,
                 [sys.executable, str(script), *sys.argv[1:]])
    except OSError as exc:
        print(f'AtomicDB worker restart failed: {exc}; the validated update '
              'is installed and will load on the next start', flush=True)
        return False
    return True


def _submit_until_definitive(server, payload, task_id):
    """Keep one immutable result until the server answers definitively."""
    failures = 0
    transient_statuses = {408, 425, 429}
    while True:
        try:
            response = requests.post(
                server + '/atomicdb/api/submit', data=payload,
                timeout=(SUBMIT_CONNECT_TIMEOUT_SECONDS,
                         SUBMIT_READ_TIMEOUT_SECONDS))
            if (response.status_code >= 500
                    or response.status_code in transient_statuses):
                raise requests.RequestException(
                    f'transient HTTP {response.status_code}')
            if 200 <= response.status_code < 300:
                try:
                    body = response.json()
                except Exception as exc:
                    raise requests.RequestException(
                        'non-JSON success response') from exc
                if not isinstance(body, dict) or not body.get('ok'):
                    raise requests.RequestException(
                        'success response did not acknowledge the submit')
            elif not 400 <= response.status_code < 500:
                raise requests.RequestException(
                    f'indeterminate HTTP {response.status_code}')
            return response
        except requests.RequestException as exc:
            failures += 1
            delay = min(
                SUBMIT_RETRY_INITIAL_SECONDS
                * (2 ** min(failures - 1, 5)),
                SUBMIT_RETRY_MAX_SECONDS)
            print(f'task {task_id} submit transport error: {exc}; '
                  f'retrying the same result in {delay}s', flush=True)
            time.sleep(delay)


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
    ap.add_argument('--no-auto-update', action='store_true',
                    help='no actualizar este archivo desde el servidor oficial')
    a = ap.parse_args()

    if not a.no_auto_update and _install_worker_update(a.S):
        _restart_updated_worker()

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
    machine = f'{a.U}-{platform.node() or "worker"}-atomicdb'[:64]
    auth = {'username': a.U, 'password': a.P, 'machine': machine,
            'threads': a.T, 'hash': a.hash, 'tb': '1' if tb else '0',
            'os': f'{platform.system()} {platform.release()}'}
    eng = Engine(a.engine, threads=a.T, hash_mb=a.hash, syzygy=a.syzygy)
    print(f'AtomicDB worker: {a.engine} T={a.T} -> {a.S}', flush=True)
    next_update_check = time.monotonic() + WORKER_UPDATE_INTERVAL_SECONDS

    while True:
        # This is deliberately the only periodic update checkpoint: the prior
        # leased batch has been fully analysed and definitively submitted.
        if (not a.no_auto_update
                and time.monotonic() >= next_update_check):
            next_update_check = time.monotonic() + WORKER_UPDATE_INTERVAL_SECONDS
            if _install_worker_update(a.S):
                eng.close()
                if not _restart_updated_worker():
                    eng = Engine(a.engine, threads=a.T, hash_mb=a.hash,
                                 syzygy=a.syzygy)
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
                    rr = _submit_until_definitive(a.S, {
                        **auth, 'task_id': t['id'], 'lines': '[]',
                        'elapsed': f'{time.time() - t0:.2f}',
                        'tb_wdl': wdl}, t['id'])
                    print(f"task {t['id']} TB wdl={wdl} -> "
                          f"{rr.json().get('summary')}", flush=True)
                except Exception as e:
                    print(f'tb submit error: {e}', flush=True)
                continue
            try:
                lines = eng.analyse(t['fen'], t['budget_nodes'],
                                    t['multipv'], t.get('searchmoves'))
            except Exception as e:
                print(f'engine failure: {e}; reiniciando motor', flush=True)
                try:
                    eng.close()
                except Exception:
                    pass
                eng = Engine(a.engine, threads=a.T, hash_mb=a.hash,
                             syzygy=a.syzygy)
                continue   # la tarea vuelve sola al caducar su lease
            searched = 0
            for ln in lines:
                m = re.search(r' nodes (\d+)', ln.get('raw', ''))
                if m:
                    searched = max(searched, int(m.group(1)))
            try:
                rr = _submit_until_definitive(a.S, {
                    **auth, 'task_id': t['id'], 'lines': json.dumps(lines),
                    'elapsed': f'{time.time() - t0:.2f}',
                    'nodes': searched,
                }, t['id'])
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
