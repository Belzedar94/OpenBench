#!/usr/bin/env python3
"""Worker de AtomicDB: pide lotes de analisis, corre Atomic-Stockfish y
devuelve MultiPV. Independiente del client.py de OpenBench (deliberado:
cero riesgo sobre SPRT/DATAGEN). Uso minimo (el motor se descarga solo):

  python atomicdb_worker.py -U user -P pass -S https://servidor -T 8

--jobs K corre K analisis a la vez repartiendo -T entre ellos.

--engine ruta/binario permite usar una build propia en su lugar.
"""

import argparse
import ast
import gzip
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import requests

# Bump this integer for every published change to this worker.  It is the
# downgrade/replay guard used by the self-updater; do not reuse a build number.
ATOMICDB_WORKER_UPDATE_PROTOCOL = 1
ATOMICDB_WORKER_BUILD = 2026072806
WORKER_UPDATE_INTERVAL_SECONDS = 30 * 60
WORKER_UPDATE_CONNECT_TIMEOUT_SECONDS = 15
WORKER_UPDATE_READ_TIMEOUT_SECONDS = 30
WORKER_UPDATE_TOTAL_TIMEOUT_SECONDS = 60
WORKER_UPDATE_MAX_BYTES = 1024 * 1024
SUBMIT_CONNECT_TIMEOUT_SECONDS = 15
SUBMIT_READ_TIMEOUT_SECONDS = 600
SUBMIT_RETRY_INITIAL_SECONDS = 15
SUBMIT_RETRY_MAX_SECONDS = 300
HEARTBEAT_INTERVAL_SECONDS = 60
HEARTBEAT_PROGRESS_GRACE_SECONDS = 5 * 60
MAX_REPORTED_NPS = 10 ** 12


class WorkerUpdateError(Exception):
    pass


class LeaseRequestError(Exception):
    """Lease outcome whose commit status is known or indeterminate."""

    def __init__(self, message, ambiguous=True):
        super().__init__(message)
        self.ambiguous = ambiguous


class CurrentTaskState:
    """Thread-safe snapshot of the assignment currently making progress.

    A live Python process is not sufficient evidence that Stockfish is alive.
    Heartbeats expose the assignment only while its UCI ``nodes`` counter has
    advanced recently; after the grace period the server is free to let the
    lease expire and fence a replacement assignment with a new lease token.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._task_id = None
        self._lease_token = ''
        self._nps = 0
        self._last_nodes = -1
        self._progress_at = 0.0

    def start(self, task_id, lease_token=''):
        with self._lock:
            self._task_id = task_id
            self._lease_token = lease_token or ''
            self._nps = 0
            self._last_nodes = -1
            # Give a newly started engine one grace window to report its first
            # UCI node counter.
            self._progress_at = self._clock()

    def progress(self, nodes, elapsed):
        try:
            nodes = int(nodes)
            elapsed = float(elapsed)
        except (TypeError, ValueError, OverflowError):
            return
        if nodes < 0 or elapsed <= 0:
            return
        with self._lock:
            if self._task_id is None or nodes <= self._last_nodes:
                return
            self._last_nodes = nodes
            self._nps = min(MAX_REPORTED_NPS,
                            max(0, round(nodes / elapsed)))
            self._progress_at = self._clock()

    def clear(self):
        with self._lock:
            self._task_id = None
            self._lease_token = ''
            self._nps = 0
            self._last_nodes = -1
            self._progress_at = 0.0

    def snapshot(self):
        with self._lock:
            if (self._task_id is None
                    or self._clock() - self._progress_at
                    > HEARTBEAT_PROGRESS_GRACE_SECONDS):
                return None
            return {
                'id': self._task_id,
                'lease_token': self._lease_token,
                'nps': self._nps,
            }


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


def _heartbeat_loop(server, auth, current_task, stop):
    """Keep capacity visible during long blocking engine searches."""
    while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
        try:
            current = current_task.snapshot()
            payload = dict(auth, worker_build=ATOMICDB_WORKER_BUILD,
                           nps=current['nps'] if current else 0)
            if current:
                payload.update(task_id=current['id'],
                               lease_token=current['lease_token'])
            requests.post(server + '/atomicdb/api/heartbeat', data=payload,
                          timeout=(SUBMIT_CONNECT_TIMEOUT_SECONDS, 30)) \
                    .raise_for_status()
        except requests.RequestException as exc:
            print(f'heartbeat skipped: {exc}', flush=True)


def _request_tasks(server, auth, lease_session):
    """Request one lease while preserving replay semantics.

    Transport failures, 5xx responses and malformed 2xx bodies may all occur
    after the assignment committed, so their nonce must be retried unchanged.
    A valid 2xx payload or a definitive 4xx response ends the logical request.
    """
    try:
        response = requests.post(
            server + '/atomicdb/api/lease',
            data=dict(auth, lease_session=lease_session), timeout=60)
    except requests.RequestException as exc:
        raise LeaseRequestError(str(exc), ambiguous=True) from exc
    status = response.status_code
    if 400 <= status < 500:
        raise LeaseRequestError(f'HTTP {status}', ambiguous=False)
    if not 200 <= status < 300:
        raise LeaseRequestError(f'HTTP {status}', ambiguous=True)
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        raise LeaseRequestError(f'invalid JSON: {exc}', ambiguous=True) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get('tasks'), list):
        raise LeaseRequestError('invalid lease response schema', ambiguous=True)
    return payload['tasks']


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

    def analyse(self, fen, nodes, multipv, searchmoves=None, progress=None):
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
        started = time.monotonic()
        stm_white = fen.split()[1] == 'w'
        while True:
            line = self.p.stdout.readline()
            if not line:
                break
            if line.startswith('bestmove'):
                break
            if progress:
                found_nodes = re.search(r' nodes (\d+)', line)
                if found_nodes:
                    progress(int(found_nodes.group(1)),
                             max(time.monotonic() - started, 1e-6))
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


def _file_sha256(path):
    """sha256 de un fichero, o '' si no se puede leer.

    Telemetria de PROCEDENCIA, opcional por diseno: el servidor la acepta si
    llega y no la echa de menos si no.  Una eval no puede envenenar la verdad
    del arbol (solo los cierres exactos lo hacen), pero un build roto puede
    redirigir el selector durante meses; con el sha del binario y el de la red
    ese sesgo es atribuible a algo concreto.
    """
    try:
        digest = hashlib.sha256()
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ''


def _net_sha256(engine_path):
    """sha256 de la red NNUE que el motor va a cargar, si es un fichero suelto.

    Se busca en el cwd del worker (que es donde ``provision_engine`` la deja)
    y junto al binario.  Una red embebida en el binario ya queda cubierta por
    el sha del propio ejecutable.
    """
    import glob
    candidates = sorted(glob.glob('*.nnue'))
    directory = os.path.dirname(os.path.abspath(engine_path))
    candidates += sorted(glob.glob(os.path.join(directory, '*.nnue')))
    for candidate in candidates:
        digest = _file_sha256(candidate)
        if digest:
            return digest
    return ''


class Solver:
    """Drives the engine's `solve` command over a fresh process each time.

    A solve job is minutes long and allocates its own proof table, so it does
    NOT share the analysis engine's process: a solver that runs out of memory
    or wedges must not take the analysis worker down with it.
    """

    def __init__(self, path, hash_mb=256):
        self.path = path
        self.hash_mb = hash_mb

    def solve(self, fen, goal, budget_nodes, movetime_ms=0):
        """Return (outcome, pn, dn, nodes, elapsed, certificate, telemetry).

        ``telemetry`` carries the fortress indicators the solver reports.  It
        is advisory scheduling information: the server stores it and never
        lets it near a verdict.
        """
        command = (f'solve {fen} goal {goal} nodes {int(budget_nodes)} '
                   f'hash {int(self.hash_mb)}')
        if movetime_ms:
            command += f' movetime {int(movetime_ms)}'
        started = time.time()
        process = subprocess.run(
            [self.path], input=command + '\nquit\n', text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        elapsed = time.time() - started

        outcome, pn, dn, nodes = 'UNKNOWN', None, None, 0
        telemetry = {}
        certificate = []
        in_certificate = False
        for line in process.stdout.splitlines():
            if in_certificate:
                certificate.append(line)
                continue
            if line.startswith('# atomicdb-proof/'):
                in_certificate = True
                certificate.append(line)
                continue
            if not line.startswith('solve '):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            name, value = parts[1], parts[2]
            if name == 'outcome':
                outcome = value
            elif name == 'pn':
                pn = None if value == 'INF' else int(value)
            elif name == 'dn':
                dn = None if value == 'INF' else int(value)
            elif name == 'nodes':
                nodes = int(value)
            elif name.startswith('fortress_'):
                try:
                    telemetry[name] = float(value)
                except ValueError:
                    pass
        text = '\n'.join(certificate) + '\n' if certificate else ''
        return outcome, pn, dn, nodes, elapsed, text, telemetry


def _solve_once(server, auth, solver, lease_session):
    """Acquire, solve and submit ONE proof task. Returns True if it did work."""
    try:
        response = requests.post(
            server + '/atomicdb/api/solve/acquire',
            data=dict(auth, lease_session=lease_session), timeout=60)
        response.raise_for_status()
        tasks = response.json().get('tasks') or []
    except (requests.RequestException, ValueError) as exc:
        print(f'solve lease error: {exc}', flush=True)
        return False
    if not tasks:
        return False

    task = tasks[0]
    outcome, pn, dn, nodes, elapsed, certificate, telemetry = solver.solve(
        task['fen'], task.get('goal', 'WHITE_WIN'), task['budget_nodes'])
    payload = {**auth, 'task_id': task['id'], 'outcome': outcome,
               'nodes': nodes, 'elapsed': f'{elapsed:.2f}',
               'lease_token': task.get('lease_token', ''), **telemetry}
    if pn is not None:
        payload['pn'] = pn
    if dn is not None:
        payload['dn'] = dn
    files = None
    if certificate:
        files = {'certificate': ('certificate.gz',
                                 gzip.compress(certificate.encode('utf-8')),
                                 'application/gzip')}
    try:
        submitted = requests.post(server + '/atomicdb/api/solve/submit',
                                  data=payload, files=files,
                                  timeout=(SUBMIT_CONNECT_TIMEOUT_SECONDS,
                                           SUBMIT_READ_TIMEOUT_SECONDS))
        summary = submitted.json().get('summary', submitted.text[:200])
    except (requests.RequestException, ValueError) as exc:
        summary = f'submit error: {exc}'
    print(f"solve task {task['id']} {outcome} ({nodes}n, {elapsed:.1f}s) "
          f'-> {summary}', flush=True)
    return True


def _solve_loop(server, auth, solver, stop_event):
    """Proof thread. The solver is single-threaded, so it runs BESIDE the
    analysis engine instead of in front of it: one core proves while the
    engine's threads keep the analysis queue (and visitor requests) moving.
    A fortress-stratum attempt can hold this thread for many minutes; that
    must never freeze the analysis loop again."""
    while not stop_event.is_set():
        did = False
        try:
            did = _solve_once(server, auth, solver,
                              secrets.token_urlsafe(24))
        except Exception as exc:  # the thread must survive anything
            print(f'solve loop error: {exc}', flush=True)
        if not did:
            stop_event.wait(60)


def probe_tb(tb, fen):
    """Return ``(wdl, dtz)``; ``dtz`` is None when the tables cannot give it.

    DTZ is what turns a tablebase win into a statement about the fifty-move
    counter, so the server can record for which entry clocks the closure
    holds.  It is strictly additive: a server that does not know the field
    ignores it, and a probe that cannot produce it still reports its WDL.
    """
    if tb is None:
        return None, None
    parts = fen.split()
    if parts[2] != '-' or parts[3] != '-':
        return None, None
    if sum(ch.isalpha() for ch in parts[0]) > 6:
        return None, None
    try:
        import chess.variant
        board = chess.variant.AtomicBoard(fen)
        wdl = tb.probe_wdl(board)
    except Exception:
        return None, None
    dtz = None
    try:
        dtz = tb.probe_dtz(board)
    except Exception:
        dtz = None
    return wdl, dtz


def _analysis_slot(slot, a, tb, auth, provenance, engine_threads, stop,
                   owns_updates):
    """One independent leaseholder: its own engine, lease and heartbeat.

    A slot is a worker in every sense the server cares about.  It has its own
    machine identity, so every rule the protocol already enforces per machine
    -- one live assignment, the fencing token, heartbeat keepalive, replay of a
    lost response -- applies per slot with nothing added to the protocol and
    nothing added to the schema.  That is the whole reason the slot lives in
    the identity rather than in a new column.
    """
    eng = Engine(a.engine, threads=engine_threads, hash_mb=a.hash,
                 syzygy=a.syzygy)
    current_task = CurrentTaskState()
    threading.Thread(
        target=_heartbeat_loop, args=(a.S, auth, current_task, stop),
        name=f'atomicdb-heartbeat-{slot}', daemon=True).start()

    next_update_check = time.monotonic() + WORKER_UPDATE_INTERVAL_SECONDS
    # Idempotency nonce for one logical lease request. Preserve it only across
    # transport/ambiguous-response failures where the server may have committed;
    # rotate only after a valid 2xx payload or a definitive 4xx rejection.
    lease_session = secrets.token_urlsafe(24)

    while not stop.is_set():
        # Only one slot drives the self-update, and it does so between batches
        # exactly as before.  A successful update re-execs the process, which
        # ends every slot at once -- their leases then expire and are recycled,
        # which is the same thing that happens when a single worker restarts.
        if (owns_updates and not a.no_auto_update
                and time.monotonic() >= next_update_check):
            next_update_check = time.monotonic() + WORKER_UPDATE_INTERVAL_SECONDS
            if _install_worker_update(a.S):
                eng.close()
                if not _restart_updated_worker():
                    eng = Engine(a.engine, threads=engine_threads,
                                 hash_mb=a.hash, syzygy=a.syzygy)
        try:
            tasks = _request_tasks(a.S, auth, lease_session)
        except LeaseRequestError as e:
            if not e.ambiguous:
                lease_session = secrets.token_urlsafe(24)
            print(f'[{slot}] lease response error: {e}; reintento en 30s',
                  flush=True)
            if stop.wait(30):
                break
            continue
        lease_session = secrets.token_urlsafe(24)
        if not tasks:
            print(f'[{slot}] sin tareas; espero 60s', flush=True)
            if a.once:
                break
            if stop.wait(60):
                break
            continue
        for t in tasks:
            current_task.start(t['id'], t.get('lease_token', ''))
            t0 = time.time()
            wdl, dtz = probe_tb(tb, t['fen'])
            if wdl is not None:
                payload = {
                    **auth, **provenance,
                    'task_id': t['id'], 'lines': '[]',
                    'elapsed': f'{time.time() - t0:.2f}',
                    'tb_wdl': wdl,
                    'lease_token': t.get('lease_token', '')}
                if dtz is not None:
                    payload['tb_dtz'] = dtz
                try:
                    rr = _submit_until_definitive(a.S, payload, t['id'])
                    print(f"[{slot}] task {t['id']} TB wdl={wdl} dtz={dtz} -> "
                          f"{rr.json().get('summary')}", flush=True)
                except Exception as e:
                    print(f'[{slot}] tb submit error: {e}', flush=True)
                current_task.clear()
                continue
            try:
                lines = eng.analyse(
                    t['fen'], t['budget_nodes'], t['multipv'],
                    t.get('searchmoves'),
                    progress=current_task.progress,
                )
            except Exception as e:
                print(f'[{slot}] engine failure: {e}; reiniciando motor',
                      flush=True)
                try:
                    eng.close()
                except Exception:
                    pass
                eng = Engine(a.engine, threads=engine_threads, hash_mb=a.hash,
                             syzygy=a.syzygy)
                current_task.clear()
                continue   # la tarea vuelve sola al caducar su lease
            searched = 0
            for ln in lines:
                m = re.search(r' nodes (\d+)', ln.get('raw', ''))
                if m:
                    searched = max(searched, int(m.group(1)))
            try:
                rr = _submit_until_definitive(a.S, {
                    **auth, **provenance,
                    'task_id': t['id'], 'lines': json.dumps(lines),
                    'elapsed': f'{time.time() - t0:.2f}',
                    'nodes': searched,
                    'lease_token': t.get('lease_token', ''),
                }, t['id'])
                s = rr.json().get('summary', rr.json())
            except Exception as e:
                s = f'submit error: {e}'
            print(f"[{slot}] task {t['id']} ({t['budget_nodes']}n, "
                  f"{time.time()-t0:.1f}s) -> {s}", flush=True)
            current_task.clear()
        if a.once:
            break
    eng.close()


def plan_threads(total, jobs, solve):
    """Split the contributor's -T promise honestly, and spend all of it.

    -T is what the machine was promised to use, IN TOTAL, and the split has to
    respect that rather than multiply it: K engines at T threads each is K*T
    cores, which is how a worker quietly takes over a box it was lent a corner
    of.  The solver, when present, takes its core off the top first, exactly as
    it did before jobs existed.

    The remainder is distributed instead of discarded.  A flat ``available //
    jobs`` looks tidy and quietly wastes up to jobs-1 cores -- -T 24 --jobs 6
    --solve would run six engines of three and leave five of the twenty-four
    idle, which is precisely the thing this flag exists to stop.  So the first
    slots get one thread more and the sum is exact.

    Returns (jobs, [threads per slot], solver_cores).  Jobs is clamped down
    when the budget cannot give every slot a whole thread, because a slot with
    less than one core is not parallelism, it is contention with bookkeeping.
    """
    total = max(1, int(total))
    jobs = max(1, int(jobs))
    solver_cores = 1 if solve else 0
    available = max(1, total - solver_cores)
    jobs = min(jobs, available)
    base, extra = divmod(available, jobs)
    return jobs, [base + (1 if slot < extra else 0)
                  for slot in range(jobs)], solver_cores


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-U', required=True)
    ap.add_argument('-P', required=True)
    ap.add_argument('-S', required=True)
    ap.add_argument('--engine', default='',
                    help='binario propio (opcional; sin el se descarga el de referencia)')
    ap.add_argument('-T', type=int, default=4)
    ap.add_argument('--jobs', type=int, default=1,
                    help='analisis concurrentes; -T se REPARTE entre ellos. '
                         'Una posicion sola escala mal con muchos hilos, asi '
                         'que K analisis de T/K hilos rinden mas que uno de T')
    ap.add_argument('--hash', type=int, default=512)
    ap.add_argument('--syzygy', default='', help='dirs de TB atomicas separados por ;')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--no-auto-update', action='store_true',
                    help='no actualizar este archivo desde el servidor oficial')
    ap.add_argument('--solve', action='store_true',
                    help='servir tambien tareas SOLVE (pruebas exhaustivas) '
                         'ademas de analisis; sin este flag el worker se '
                         'comporta exactamente igual que antes')
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

    # --once keeps the historical smoke semantics, which are single-slot.
    if a.once:
        a.jobs = 1
    jobs, slot_threads, solver_cores = plan_threads(a.T, a.jobs, a.solve)
    if jobs < a.jobs:
        print(f'aviso: --jobs {a.jobs} recortado a {jobs}: -T {a.T} no da '
              f'un hilo entero por trabajo', flush=True)
    if a.solve and a.T < 2:
        print('aviso: --solve con -T 1 comparte el unico hilo entre '
              'analisis y pruebas', flush=True)

    base = f'{a.U}-{platform.node() or "worker"}-atomicdb'
    # With a single job the identity is byte-identical to every previous
    # build, so an existing deployment sees no change at all.  With more, each
    # slot is its own leaseholder and says so.
    if jobs == 1:
        machines = [base[:64]]
    else:
        machines = [f'{base[:60]}#{slot}' for slot in range(jobs)]

    # Each slot reports ITS OWN thread count, because that is what it will
    # actually run and the capacity view should not have to guess.
    auth_base = {'username': a.U, 'password': a.P,
                 'threads': slot_threads[0], 'hash': a.hash,
                 'tb': '1' if tb else '0',
                 'os': f'{platform.system()} {platform.release()}',
                 'worker_build': ATOMICDB_WORKER_BUILD}
    # Additive provenance: an older server simply ignores these POST fields.
    provenance = {'engine_sha': _file_sha256(a.engine),
                  'net_sha': _net_sha256(a.engine)}

    stop = threading.Event()
    solver = Solver(a.engine, hash_mb=a.hash) if a.solve else None
    if solver is not None and not a.once:
        threading.Thread(
            target=_solve_loop,
            args=(a.S, dict(auth_base, machine=machines[0]), solver, stop),
            name='atomicdb-solver', daemon=True).start()

    spread = ('x'.join(str(n) for n in sorted(set(slot_threads), reverse=True))
              if len(set(slot_threads)) > 1 else str(slot_threads[0]))
    print(f'AtomicDB worker: {a.engine} T={a.T} -> {jobs} trabajos de '
          f'{spread} hilos (suma {sum(slot_threads) + solver_cores})'
          + (f' + {solver_cores} solver' if solver_cores else '')
          + f' -> {a.S}', flush=True)

    if a.once:
        if a.solve and _solve_once(a.S, dict(auth_base, machine=machines[0]),
                                   solver, secrets.token_urlsafe(24)):
            stop.set()
            return
        _analysis_slot(0, a, tb, dict(auth_base, machine=machines[0]),
                       provenance, slot_threads[0], stop, owns_updates=False)
        stop.set()
        return

    threads = []
    for slot in range(jobs):
        thread = threading.Thread(
            target=_analysis_slot,
            args=(slot, a, tb,
                  dict(auth_base, machine=machines[slot],
                       threads=slot_threads[slot]),
                  provenance, slot_threads[slot], stop, slot == 0),
            name=f'atomicdb-analysis-{slot}')
        thread.start()
        threads.append(thread)
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        stop.set()
        for thread in threads:
            thread.join(timeout=5)


if __name__ == '__main__':
    main()
