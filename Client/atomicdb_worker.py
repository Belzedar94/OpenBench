#!/usr/bin/env python3
"""Worker de AtomicDB: pide lotes de analisis, corre Atomic-Stockfish y
devuelve MultiPV. Independiente del client.py de OpenBench (deliberado:
cero riesgo sobre SPRT/DATAGEN). Uso minimo (el motor se descarga solo):

  python atomicdb_worker.py -U user -P pass -S https://servidor -T 8

--jobs K corre K analisis a la vez repartiendo -T entre ellos.

--engine ruta/binario permite usar una build propia en su lugar.

El motor descargado se renueva sin parar el worker: cada 15 min se relee el
manifest y, si hay un binario nuevo, se descarga junto al actual, se comprueba
y cada analisis lo toma entre tareas (--no-engine-update lo desactiva).
"""

import argparse
import ast
import gzip
import hashlib
import json
import os
import queue
import random
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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Bump this integer for every published change to this worker.  It is the
# downgrade/replay guard used by the self-updater; do not reuse a build number.
#
# Protocol 2 changes how a self-update restarts on Windows (see
# ``_restart_updated_worker``).  A protocol-1 worker restarts with os.execv,
# which on Windows passes the command line unquoted: with a space in its
# Python, script or arguments the old process exits and the new one cannot
# start.  Such a worker refuses any file that does not speak its protocol,
# so it never installs this build and never takes that path because of it.
ATOMICDB_WORKER_UPDATE_PROTOCOL = 2
ATOMICDB_WORKER_BUILD = 2026102001
WORKER_UPDATE_INTERVAL_SECONDS = 30 * 60
# Windows restart: how long the new worker must stay up before this one
# hands over and exits.  Failures of the restart itself (a mangled command
# line, an import error) show up well within it.
WORKER_RESTART_GRACE_SECONDS = 5
# Engine hot swap: how often a running worker re-reads the engine manifest
# (jittered by +-20 % so a fleet does not download a new binary in the same
# second), and how long a job whose switch failed waits before retrying.
ENGINE_UPDATE_INTERVAL_SECONDS = 15 * 60
ENGINE_UPDATE_MIN_SECONDS = 60
ENGINE_UPDATE_JITTER = 0.2
# A published binary that failed here is retried at the next check, then
# after 4, 16... intervals, never less often than once a day: a file the
# server serves wrong, or that an antivirus keeps removing, must not be
# downloaded again every quarter of an hour by the whole fleet.
ENGINE_UPDATE_MAX_BACKOFF_SECONDS = 24 * 3600
ENGINE_SWITCH_RETRY_SECONDS = 15 * 60
# A job whose hot-swapped engine fails this many searches in a row takes it
# back: every job returns to the engine that ran before, and that sha is not
# offered again until another binary is published.
ENGINE_FAILURE_LIMIT = 3
ENGINE_DOWNLOAD_READ_TIMEOUT_SECONDS = 600
ENGINE_PROBE_TIMEOUT_SECONDS = 120
ENGINE_PROBE_NODES = 20000
HTTP_CONNECT_TIMEOUT_SECONDS = 3
HTTP_CONNECT_RETRIES = 2
WORKER_UPDATE_CONNECT_TIMEOUT_SECONDS = HTTP_CONNECT_TIMEOUT_SECONDS
WORKER_UPDATE_READ_TIMEOUT_SECONDS = 30
WORKER_UPDATE_TOTAL_TIMEOUT_SECONDS = 60
WORKER_UPDATE_MAX_BYTES = 1024 * 1024
SUBMIT_CONNECT_TIMEOUT_SECONDS = HTTP_CONNECT_TIMEOUT_SECONDS
SUBMIT_READ_TIMEOUT_SECONDS = 600
SUBMIT_RETRY_INITIAL_SECONDS = 15
SUBMIT_RETRY_MAX_SECONDS = 300
LEASE_CONNECT_TIMEOUT_SECONDS = HTTP_CONNECT_TIMEOUT_SECONDS
LEASE_READ_TIMEOUT_SECONDS = 60
LEASE_RETRY_INITIAL_SECONDS = 5
LEASE_RETRY_MAX_SECONDS = 60
HEARTBEAT_INTERVAL_SECONDS = 60
HEARTBEAT_PROGRESS_GRACE_SECONDS = 5 * 60
MAX_REPORTED_NPS = 10 ** 12
# Fallos de motor SEGUIDOS tras los que el hilo SOLVE se apaga solo.  Se
# reintenta un par de veces por si el fallo era transitorio (memoria, fichero
# bloqueado, disco lleno) y se abandona: un binario sin el comando ``solve``
# no aprende a resolverlo esperando, y seguir pidiendo tareas solo las pasea
# por un arriendo de veinte minutos para devolverlas intactas.
SOLVER_ENGINE_FAILURE_LIMIT = 3

# Each HTTP-calling thread keeps its own Session.  This reuses a healthy TLS
# connection without sharing requests.Session concurrently between threads.
_HTTP_THREAD_LOCAL = threading.local()


def _http_session():
    session = getattr(_HTTP_THREAD_LOCAL, 'session', None)
    if session is None:
        session = requests.Session()
        # A POST is safe to retry only while connecting: no request has reached
        # the server yet.  Read/status retries remain with the protocol loops,
        # which preserve lease nonces and immutable submit payloads.
        retry = Retry(
            total=HTTP_CONNECT_RETRIES,
            connect=HTTP_CONNECT_RETRIES,
            read=0,
            redirect=0,
            status=0,
            other=0,
            allowed_methods=None,
            backoff_factor=0.25,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=1,
                              pool_maxsize=1)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        _HTTP_THREAD_LOCAL.session = session
    return session


class WorkerUpdateError(Exception):
    pass


class EngineUpdateError(Exception):
    """A published engine could not be installed; the running one stays."""


class EngineExited(Exception):
    """The engine process ended in the middle of a search."""


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
        response = _http_session().get(
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
    script = Path(script_path or __file__).resolve()
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


def _restart_updated_worker(script_path=None, once=False):
    """Exec the installed worker; keep running old code if exec itself fails.

    POSIX replaces the process image with ``os.execv``.  Windows has no exec:
    its C runtime emulates one by starting a new process and exiting, and it
    joins the arguments WITHOUT quoting them, so a Python, a script or an
    argument with a space reached the new process in pieces -- the old
    worker was already gone and the new one could not even open its script.
    There ``_spawn_replacement`` starts the new worker itself and this one
    exits only once the new one is running.  ``once`` is ``--once``: the run
    is a single pass that the new worker then does in full.

    The validated new file stays on disk and ``.previous`` remains available
    for manual recovery. Automatic rollback here would race with another
    process that already restarted successfully from the shared script.
    """
    script = Path(script_path or __file__).resolve()
    argv = [sys.executable, str(script), *sys.argv[1:]]
    if os.name == 'nt':
        return _spawn_replacement(argv, once=once)
    try:
        os.execv(sys.executable, argv)
    except OSError as exc:
        print(f'AtomicDB worker restart failed: {exc}; the validated update '
              'is installed and will load on the next start', flush=True)
        return False
    return True


def _inheritable_stdio(stream):
    """What a replacement process should get for ``stream``.

    Its own descriptor, so a console or a redirected log keeps receiving the
    output.  Without one (pythonw.exe, some services) the child gets DEVNULL:
    left to its defaults it would be handed a dead pipe and die on its first
    print.
    """
    try:
        if stream is not None:
            stream.flush()
            return stream.fileno()
    except (AttributeError, OSError, ValueError):
        pass
    return subprocess.DEVNULL


def _spawn_replacement(argv, grace=WORKER_RESTART_GRACE_SECONDS,
                       exit_process=os._exit, once=False):
    """Windows restart: start ``argv`` as a new worker, then leave.

    The list form of ``subprocess`` quotes every argument, spaces included.
    This process exits (``os._exit``: every job ends at once, exactly as an
    exec would end them) only after the new one has stayed up for ``grace``
    seconds; a new worker that ends sooner -- a bad command line, a missing
    module, job threads that all crashed (which still exits 0) -- leaves this
    one running the old build, as a failed exec does.  With ``once`` the run
    is a single pass: this process waits for the new worker's pass and ends
    with its exit code, and a pass already finished cleanly within the grace
    counts as done.
    """
    try:
        child = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL,
            stdout=_inheritable_stdio(sys.stdout),
            stderr=_inheritable_stdio(sys.stderr))
    except OSError as exc:
        print(f'AtomicDB worker restart failed: {exc}; the validated update '
              'is installed and will load on the next start', flush=True)
        return False
    try:
        code = child.wait(timeout=grace)
        handed_over = once and code == 0
    except subprocess.TimeoutExpired:
        print(f'AtomicDB worker restarted as process {child.pid}', flush=True)
        code = child.wait() if once else 0
        handed_over = True
    if handed_over:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (AttributeError, OSError, ValueError):
                pass
        # A Windows status (0xC000013A after Ctrl+C) does not fit the C int
        # of os._exit; its two's complement exits with the same status.
        exit_process(code - 2 ** 32 if code >= 2 ** 31 else code)
        return True     # reached only when a test replaces exit_process
    print(f'AtomicDB worker restart failed: the new process exited with code '
          f'{code}; this one keeps running the previous build. The validated '
          'update is installed and will load on the next start', flush=True)
    return False


def _submit_until_definitive(server, payload, task_id):
    """Keep one immutable result until the server answers definitively."""
    failures = 0
    transient_statuses = {408, 425, 429}
    while True:
        try:
            response = _http_session().post(
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
            _http_session().post(
                server + '/atomicdb/api/heartbeat', data=payload,
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
        response = _http_session().post(
            server + '/atomicdb/api/lease',
            data=dict(auth, lease_session=lease_session),
            timeout=(LEASE_CONNECT_TIMEOUT_SECONDS,
                     LEASE_READ_TIMEOUT_SECONDS))
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
        # False when the engine closed its output before answering: it died.
        self.ready = self._wait('readyok')

    def _send(self, line):
        self.p.stdin.write(line + '\n')
        self.p.stdin.flush()

    def _wait(self, token):
        """Read up to a line starting with ``token``: True, or False at EOF."""
        while True:
            line = self.p.stdout.readline()
            if not line:
                return False
            if line.startswith(token):
                return True

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
                # Output ended without ``bestmove``: the process died (a
                # crash, a kill, Ctrl+C on the shared console).  What came
                # before is a truncated search, never a result to submit.
                try:
                    code = self.p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    code = 'unknown'
                raise EngineExited('the engine stopped in the middle of a '
                                   f'search (exit code {code})')
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


# ---------------------------------------------------------------------------
# Tablebases for every worker, not just the closures
# ---------------------------------------------------------------------------
#
# Analysis that cannot see a tablebase re-searches endgames that were solved
# decades ago, and it does it on every visit.  The 3-4-5 atomic set is 1.3 GB,
# which is worth asking a contributor to store once and never worth asking
# them to fetch by hand -- so the worker fetches it itself, verifies it, and
# gets out of the way if the mirror is down.
#
# Three rules shape everything below:
#
#   * an explicit --syzygy is never touched.  Our own T2 box points at a full
#     six-man set; auto-fetching over that would be a downgrade.
#   * the fetch is idempotent and resumable, because it runs on EVERY start,
#     including the restart after a self-update.  A worker that re-downloads
#     1.3 GB whenever it updates is a worker nobody keeps running.
#   * a dead mirror never stops the fleet.  Failure means starting without
#     tablebases and saying so, not exiting.
SYZYGY_SET = 'atomic-syzygy-345'
SYZYGY_BASE_PATH = '/atomicdb/engines/syzygy-345/'
SYZYGY_DIR_NAME = 'syzygy-345'
SYZYGY_FETCH_ATTEMPTS = 3
SYZYGY_MANIFEST_TIMEOUT_SECONDS = 60
SYZYGY_FILE_CONNECT_TIMEOUT_SECONDS = 15
SYZYGY_FILE_READ_TIMEOUT_SECONDS = 600
# Anti-bomb ceilings on a remote description of what to write to disk.
SYZYGY_MAX_FILES = 4096
SYZYGY_MAX_FILE_BYTES = 512 * 1024 * 1024
_SYZYGY_HASH_CHUNK = 1 << 20


class SyzygyManifestError(Exception):
    """A manifest that is malformed, oversized, or not the set we asked for."""


def parse_syzygy_manifest(payload):
    """Validate the published vocabulary and return the file list.

    The format is fixed by the distribution point:

        {"set": "atomic-syzygy-345",
         "files": [{"name": ..., "bytes": ..., "sha256": ...}, ...]}

    Everything here is a remote instruction to write files on a contributor's
    disk, so it is checked rather than trusted -- most of all ``name``, which
    becomes a path.  A manifest that says ``../../.ssh/authorized_keys`` is
    the reason this function exists.
    """
    if not isinstance(payload, dict):
        raise SyzygyManifestError('manifest is not an object')
    if payload.get('set') != SYZYGY_SET:
        raise SyzygyManifestError(
            f'manifest declares set {payload.get("set")!r}, not {SYZYGY_SET!r}')
    files = payload.get('files')
    if not isinstance(files, list) or not files:
        raise SyzygyManifestError('manifest has no files')
    if len(files) > SYZYGY_MAX_FILES:
        raise SyzygyManifestError('manifest exceeds the file-count limit')

    seen = set()
    entries = []
    for index, raw in enumerate(files):
        if not isinstance(raw, dict):
            raise SyzygyManifestError(f'file {index} is not an object')
        name = raw.get('name')
        if not isinstance(name, str) or not name:
            raise SyzygyManifestError(f'file {index} has no name')
        # A name is a leaf, always. No directories, no traversal, no drives.
        if (name in ('.', '..') or '/' in name or '\\' in name
                or name.startswith('.') or ':' in name
                or Path(name).name != name):
            raise SyzygyManifestError(f'unsafe file name {name!r}')
        if name in seen:
            raise SyzygyManifestError(f'duplicate file name {name!r}')
        seen.add(name)
        size = raw.get('bytes')
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise SyzygyManifestError(f'{name}: bad byte count')
        if size > SYZYGY_MAX_FILE_BYTES:
            raise SyzygyManifestError(f'{name}: exceeds the per-file limit')
        digest = raw.get('sha256')
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in '0123456789abcdefABCDEF' for c in digest)):
            raise SyzygyManifestError(f'{name}: bad sha256')
        entries.append({'name': name, 'bytes': size,
                        'sha256': digest.lower()})
    return entries


def _sha256_of_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(_SYZYGY_HASH_CHUNK), b''):
            digest.update(block)
    return digest.hexdigest()


def syzygy_file_state(directory, entry):
    """``ok`` / ``missing`` / ``wrong-size`` / ``wrong-hash``.

    Size is checked before the hash on purpose: it is a stat instead of a read,
    and it catches the overwhelmingly common failure (a truncated download)
    without paying to hash a gigabyte to find out.
    """
    path = Path(directory) / entry['name']
    try:
        actual = path.stat().st_size
    except OSError:
        return 'missing'
    if actual != entry['bytes']:
        return 'wrong-size'
    if _sha256_of_file(path) != entry['sha256']:
        return 'wrong-hash'
    return 'ok'


def syzygy_dir(script_path=None):
    """Beside the SCRIPT, never the working directory.

    A worker is started from wherever the contributor happens to be, and a
    1.3 GB set that lands in a different place each time is a set downloaded
    many times.
    """
    base = Path(script_path or __file__).resolve().parent
    return base / SYZYGY_DIR_NAME


def _download_syzygy_file(server, entry, directory, get):
    """One file, staged and verified before it is allowed to exist."""
    url = server.rstrip('/') + SYZYGY_BASE_PATH + entry['name']
    response = get(url, stream=True,
                   timeout=(SYZYGY_FILE_CONNECT_TIMEOUT_SECONDS,
                            SYZYGY_FILE_READ_TIMEOUT_SECONDS))
    close = getattr(response, 'close', None)
    try:
        response.raise_for_status()
        _stream_syzygy_file(response, entry, directory)
    finally:
        # 292 files is 292 held connections if this is forgotten.
        if close is not None:
            close()


def _stream_syzygy_file(response, entry, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=entry['name'] + '.', suffix='.part',
                                     dir=str(directory))
    temp = Path(temp_name)
    try:
        digest = hashlib.sha256()
        written = 0
        with os.fdopen(fd, 'wb') as stream:
            for block in response.iter_content(chunk_size=_SYZYGY_HASH_CHUNK):
                if not block:
                    continue
                written += len(block)
                if written > entry['bytes']:
                    raise SyzygyManifestError(
                        f"{entry['name']}: server sent more than the manifest "
                        'declared')
                digest.update(block)
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        if written != entry['bytes']:
            raise SyzygyManifestError(
                f"{entry['name']}: got {written} bytes, expected "
                f"{entry['bytes']}")
        if digest.hexdigest() != entry['sha256']:
            raise SyzygyManifestError(f"{entry['name']}: sha256 mismatch")
        # Only now does it get its real name: a half-written tablebase must
        # never be mistaken for a good one by the next start.
        os.replace(temp, directory / entry['name'])
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def fetch_syzygy_set(server, directory=None, attempts=SYZYGY_FETCH_ATTEMPTS,
                     log=print, get=None):
    """Bring the set on disk up to the manifest. Returns the dir, or None.

    Idempotent and resumable: every pass re-checks what is on disk and fetches
    only what is missing or wrong, so a cold start, a restart after a
    self-update, and a resumed half-download all take the same path. Never
    raises -- a mirror having a bad day is not a reason to stop analysing.
    """
    get = get or _http_session().get
    directory = Path(directory) if directory is not None else syzygy_dir()

    for attempt in range(1, max(1, attempts) + 1):
        try:
            response = get(
                server.rstrip('/') + SYZYGY_BASE_PATH + 'manifest.json',
                timeout=(HTTP_CONNECT_TIMEOUT_SECONDS,
                         SYZYGY_MANIFEST_TIMEOUT_SECONDS))
            response.raise_for_status()
            entries = parse_syzygy_manifest(response.json())
        except Exception as error:
            log(f'syzygy: manifest unavailable ({error}); intento '
                f'{attempt}/{attempts}', flush=True)
            continue

        pending = [e for e in entries
                   if syzygy_file_state(directory, e) != 'ok']
        if not pending:
            log(f'syzygy: {len(entries)} files verified in {directory}',
                flush=True)
            return directory

        missing_bytes = sum(e['bytes'] for e in pending)
        log(f'syzygy: {len(pending)}/{len(entries)} files missing '
            f'({missing_bytes / (1 << 20):.0f} MB); downloading to {directory}',
            flush=True)
        failed = 0
        for entry in pending:
            try:
                _download_syzygy_file(server, entry, directory, get)
            except Exception as error:
                failed += 1
                log(f"syzygy: {entry['name']} failed ({error})", flush=True)
        if not failed:
            log(f'syzygy: full set in {directory}', flush=True)
            return directory
        log(f'syzygy: {failed} files still missing after attempt '
            f'{attempt}/{attempts}', flush=True)

    log('syzygy: could not complete the set; the worker starts WITHOUT '
        'tablebases (analysis stays correct, only slower in endgames)',
        flush=True)
    return None


def resolve_syzygy(args, server, log=print, script_path=None, get=None):
    """The SyzygyPath the engines should use, or '' for none.

    An explicit --syzygy wins outright and is returned untouched, including
    its multi-directory ';' form -- the contributor who typed it knows more
    about their disk than this function does.
    """
    if args.syzygy:
        return args.syzygy
    if getattr(args, 'no_fetch_syzygy', False):
        log('syzygy: download disabled (--no-fetch-syzygy)', flush=True)
        return ''
    fetched = fetch_syzygy_set(server, syzygy_dir(script_path), log=log,
                               get=get)
    return str(fetched) if fetched else ''


def _engine_platform_key():
    """Key of this machine's prebuilt engine in the manifest."""
    import platform

    key = f'{platform.system().lower()}-{platform.machine().lower()}'
    return {'windows-amd64': 'windows-x86_64',
            'linux-amd64': 'linux-x86_64'}.get(key, key)


def _engine_entry(entry, what):
    """A manifest file entry, checked before any of it reaches the disk.

    ``file`` becomes a path, so it must be a bare name in the target
    directory; ``sha256`` is what the download is held to.
    """
    if not isinstance(entry, dict):
        raise EngineUpdateError(f'{what}: manifest entry is not an object')
    name = entry.get('file')
    if (not isinstance(name, str) or not name or name.startswith('.')
            or '/' in name or '\\' in name or ':' in name
            or Path(name).name != name):
        raise EngineUpdateError(f'{what}: unsafe file name {name!r}')
    digest = entry.get('sha256')
    if (not isinstance(digest, str) or len(digest) != 64
            or any(c not in '0123456789abcdefABCDEF' for c in digest)):
        raise EngineUpdateError(f'{what}: bad sha256 in the manifest')
    return dict(entry, sha256=digest.lower())


def _manifest_engine(man, key):
    """(binary entry, net entry or None) for platform ``key``."""
    binaries = man.get('binaries') if isinstance(man, dict) else None
    if not isinstance(binaries, dict):
        raise EngineUpdateError('the manifest lists no binaries')
    if key not in binaries:
        raise EngineUpdateError(f'no prebuilt engine for {key}')
    binary = _engine_entry(binaries[key], key)
    net = None
    if binary.get('needs_net') and 'net' in man:
        net = _engine_entry(man['net'], 'net')
    return binary, net


def _manifest_bench(man, binary):
    """The NNUE bench signature the manifest gives this binary, or None.

    ``"bench": {"nnue": 438711}`` (a bare integer is read the same way), in
    the platform entry or else at the top level.  Absent means no bench
    gate at all; present but unreadable is refused rather than ignored,
    since the publisher meant the binary to be checked.
    """
    raw = binary.get('bench', man.get('bench'))
    if isinstance(raw, dict):
        if 'nnue' not in raw:
            return None
        raw = raw['nnue']
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise EngineUpdateError(
            f'unreadable bench signature in the manifest: {raw!r}')
    return raw


def _engine_local_name(entry):
    """Where a downloaded binary lives in ``Engines``: its published name
    plus the first 12 hex digits of its sha256.

    A local path then always holds the same bytes.  A release that reuses a
    file name gets its own file, so no binary is ever replaced under a
    process that runs it -- which POSIX allows silently, and which would let
    a restart of that process run new bytes under the old signature -- and
    the swap does not depend on how releases are named.
    """
    name, digest = entry['file'], entry['sha256'][:12]
    if name.lower().endswith('.exe'):
        return f'{name[:-4]}-{digest}{name[-4:]}'
    return f'{name}-{digest}'


def _download_verified(server, entry, dest):
    """Put one manifest file at ``dest``, sha256-verified BEFORE it gets that
    name; a local copy that already matches is reused.

    The bytes are streamed to a sibling ``.part`` file and renamed into place
    only once they match, so a half-written binary never carries a real name
    and a file in use is never written over in place.  Returns True when
    something was downloaded.  Raises EngineUpdateError on a mismatch; HTTP
    and disk errors propagate.
    """
    entry = _engine_entry(entry, str(dest))
    dest = Path(dest)
    # Partial downloads of this same file that an interrupted run left behind
    # (Ctrl+C, a crash, a restart mid-download).  An hour without a write
    # keeps clear of a download another worker sharing the folder has under
    # way.  Swept before the reuse check: the usual case is a file that was
    # completed later and is only ever reused from then on.
    for stale in dest.parent.glob(dest.name + '.*.part'):
        try:
            if time.time() - stale.stat().st_mtime > 3600:
                stale.unlink()
        except OSError:
            pass
    if dest.is_file() and _file_sha256(dest) == entry['sha256']:
        return False
    print(f"downloading {entry['file']} ({entry.get('size_mb', '?')} MB)...",
          flush=True)
    response = _http_session().get(
        server.rstrip('/') + '/atomicdb/engines/' + entry['file'],
        stream=True,
        timeout=(HTTP_CONNECT_TIMEOUT_SECONDS,
                 ENGINE_DOWNLOAD_READ_TIMEOUT_SECONDS))
    try:
        response.raise_for_status()
        fd, temp_name = tempfile.mkstemp(prefix=dest.name + '.',
                                         suffix='.part', dir=str(dest.parent))
        temp = Path(temp_name)
        try:
            digest = hashlib.sha256()
            with os.fdopen(fd, 'wb') as stream:
                for block in response.iter_content(chunk_size=1 << 20):
                    if block:
                        digest.update(block)
                        stream.write(block)
                stream.flush()
                os.fsync(stream.fileno())
            if digest.hexdigest() != entry['sha256']:
                raise EngineUpdateError(
                    f"download of {entry['file']} failed the sha256 check")
            if os.name != 'nt':
                os.chmod(temp, 0o755)
            for attempt in range(5):
                try:
                    os.replace(temp, dest)
                    break
                except OSError:
                    # Another worker sharing this folder may have put the
                    # same file there -- and started it -- in the meantime.
                    if (dest.is_file()
                            and _file_sha256(dest) == entry['sha256']):
                        return False
                    # An antivirus still scanning the fresh file holds it
                    # for a moment.
                    if attempt == 4:
                        raise
                    time.sleep(0.5 * (attempt + 1))
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
    finally:
        response.close()
    return True


def _fetch_verified(server, entry, dest):
    """Descarga un archivo del manifest con sha256 verificado; reutiliza la
    copia local si ya coincide."""
    try:
        _download_verified(server, entry, dest)
    except EngineUpdateError as exc:
        sys.exit(str(exc))


def provision_engine(server):
    """Descarga el motor de referencia (y su red NNUE si el binario no la
    lleva embebida), todo sha256-verificado."""
    return _provision_engine(server)['path']


def _provision_engine(server):
    """``provision_engine`` plus the net entry the engine loads (None when
    the binary embeds its net), so the provenance can name it exactly."""
    key = _engine_platform_key()
    man = _http_session().get(
        server + '/atomicdb/engines/manifest.json',
        timeout=(HTTP_CONNECT_TIMEOUT_SECONDS, 60)).json()
    if key not in man['binaries']:
        sys.exit(f'no prebuilt engine for {key}: pass --engine with your own '
                 f'build (prebuilts: {", ".join(sorted(man["binaries"]))})')
    try:
        b = _engine_entry(man['binaries'][key], key)
    except EngineUpdateError as exc:
        sys.exit(str(exc))
    Path('Engines').mkdir(exist_ok=True)
    dest = Path('Engines') / _engine_local_name(b)
    _fetch_verified(server, b, dest)
    net = None
    if b.get('needs_net') and 'net' in man:
        # la red va al cwd del worker: el default EvalFile del motor la
        # resuelve desde ahi
        net = man['net']
        _fetch_verified(server, net, net['file'])
    print(f'engine ready: {dest}', flush=True)
    # str en el contrato: quien llama lo pasa a subprocess y telemetria tal
    # cual llevaba anos recibiendolo; pathlib es de puertas adentro.
    return {'path': str(dest), 'net': net}


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
    candidates = sorted(Path('.').glob('*.nnue'))
    directory = Path(engine_path).resolve().parent
    candidates += sorted(directory.glob('*.nnue'))
    for candidate in candidates:
        digest = _file_sha256(candidate)
        if digest:
            return digest
    return ''


# ---------------------------------------------------------------------------
# Engine hot swap
# ---------------------------------------------------------------------------
#
# A volunteer should not have to stop a worker to get a newly published
# engine.  A background thread re-reads the manifest, installs a changed
# binary NEXT TO the one in use (the old files stay: they are the fallback),
# runs it once to prove it plays on this machine, and only then offers it.
# Each job switches its own engine process between tasks and the solver
# between proofs; the offer carries path and provenance together, so every
# result is signed with the sha of the binary that produced it.


_BENCH_NODES = re.compile(r'Nodes searched\s*:\s*(\d+)')


def _probe_engine(path, timeout=ENGINE_PROBE_TIMEOUT_SECONDS,
                  nodes=ENGINE_PROBE_NODES, bench=None):
    """Run a candidate engine once, beside the working ones, and check that
    it plays: ``uci``, ``isready`` and a short search from the start position
    must all answer within ``timeout`` seconds.

    The sha256 proves the file is the published one, not that it runs HERE:
    a CPU without the instructions it was built for, or a net it cannot find
    -- which this engine reports with ``info string ERROR`` and ``bestmove
    (none)`` while staying alive.  One thread and a small hash, so the live
    searches do not notice.  With ``bench`` (the manifest's signature) the
    default ``bench`` must also report exactly that many nodes: well under a
    second, and it proves the binary searches as its build did.  Returns the
    engine's ``id name``; raises EngineUpdateError otherwise.
    """
    try:
        process = subprocess.Popen(
            [path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as exc:
        raise EngineUpdateError(f'cannot start {path}: {exc}') from exc
    lines = queue.Queue()

    def pump():
        try:
            for line in process.stdout:
                lines.put(line)
        except (OSError, ValueError):
            pass
        lines.put(None)

    reader = threading.Thread(target=pump, name='atomicdb-engine-probe',
                              daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    seen = []

    def send(command):
        process.stdin.write(command + '\n')
        process.stdin.flush()

    def expect(token):
        """The next line whose first word is ``token`` (or that matches it,
        for a compiled pattern)."""
        label = getattr(token, 'pattern', token)
        while True:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise queue.Empty
                line = lines.get(timeout=remaining)
            except queue.Empty:
                raise EngineUpdateError(
                    f'no {label!r} within {timeout:g}s') from None
            if line is None:
                raise EngineUpdateError(
                    f'it exited before {label!r}: '
                    + (' | '.join(seen[-3:]) or 'no output'))
            line = line.strip()
            if not line:
                continue
            seen.append(line[:200])
            if line.startswith('info string ERROR'):
                raise EngineUpdateError(line[:300])
            if (token.match(line) if hasattr(token, 'match')
                    else line.split()[0] == token):
                return line

    try:
        send('uci')
        expect('uciok')
        name = next((line[len('id name '):] for line in seen
                     if line.startswith('id name ')), Path(path).name)
        send('setoption name Threads value 1')
        send('setoption name Hash value 16')
        send('isready')
        expect('readyok')
        send('position startpos')
        send(f'go nodes {int(nodes)}')
        best = expect('bestmove').split()
        if len(best) < 2 or best[1] in ('(none)', '0000'):
            raise EngineUpdateError(
                'no move from the start position: ' + ' | '.join(seen[-3:]))
        if bench is not None:
            send('bench')
            found = int(_BENCH_NODES.match(expect(_BENCH_NODES)).group(1))
            if found != bench:
                raise EngineUpdateError(
                    f'bench signature {found} does not match the manifest '
                    f'({bench})')
    except (OSError, ValueError) as exc:
        raise EngineUpdateError(f'it stopped answering: {exc}') from exc
    finally:
        # quit, then end of input for a binary that only stops at EOF.
        for step in (lambda: send('quit'), process.stdin.close):
            try:
                step()
            except (OSError, ValueError):
                pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        reader.join(timeout=5)
        if not reader.is_alive():
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass
    return name


class EngineUpdater:
    """The engine every job should run, kept in step with the manifest.

    ``run`` (one daemon thread) re-reads the manifest every ``interval``
    seconds.  Only the binary decides: a net ships with the binary that
    loads it by name, so a manifest whose binary sha256 is the one in use
    needs nothing.  A different binary is downloaded beside the old files
    exactly as provisioning does, with its net when it needs one, and run
    once by ``probe``; only then does it become the next generation.  Any
    failure leaves the generation alone -- the running engine stays -- and
    that binary is tried again at the next check, then ever less often
    (ENGINE_UPDATE_MAX_BACKOFF_SECONDS); a newly published one is tried at
    once.

    The jobs and the solver read ``current()`` between tasks.  A generation
    is (number, path, provenance), assigned at once and never edited, so a
    reader cannot pair one binary with another binary's sha.  A job whose
    newly offered engine keeps failing in real searches calls ``reject``:
    the engine that ran before it comes back as the next generation.
    """

    def __init__(self, server, path, provenance,
                 interval=ENGINE_UPDATE_INTERVAL_SECONDS, probe=None,
                 directory='Engines', log=print):
        self.server = server
        self.interval = interval
        self.directory = Path(directory)
        self._probe = probe or _probe_engine
        self._log = log
        self._lock = threading.Lock()
        self._current = (0, str(path), dict(provenance))
        # binary sha256 -> (consecutive failures, monotonic time of next try)
        self._failures = {}
        # generation -> (path, provenance) of the engine it replaced
        self._before = {}
        # shas taken back after failing in use; offered again never, until
        # the manifest names some other binary
        self._rejected = set()

    def current(self):
        """(generation, engine path, provenance) the jobs should run."""
        with self._lock:
            return self._current

    def check_once(self):
        """One manifest check.  True when a new engine was offered."""
        generation, path, provenance = self.current()
        try:
            response = _http_session().get(
                self.server + '/atomicdb/engines/manifest.json',
                timeout=(HTTP_CONNECT_TIMEOUT_SECONDS, 60))
            response.raise_for_status()
            manifest = response.json()
            binary, net = _manifest_engine(manifest, _engine_platform_key())
            bench = _manifest_bench(manifest, binary)
        except Exception as exc:
            self._log(f'engine update check skipped: {exc}', flush=True)
            return False
        if binary['sha256'] == provenance.get('engine_sha'):
            return False
        if binary['sha256'] in self._rejected:
            return False
        failures, next_try = self._failures.get(binary['sha256'], (0, 0.0))
        if time.monotonic() < next_try:
            return False
        try:
            new_path = self._install(binary, net, path)
            engine_sha = _file_sha256(new_path)
            if engine_sha != binary['sha256']:
                raise EngineUpdateError(f'{new_path} changed after its check')
            name = self._probe(new_path, bench=bench)
        except Exception as exc:
            failures += 1
            delay = (0 if failures == 1 else
                     min(self.interval * 4 ** min(failures - 1, 8),
                         ENGINE_UPDATE_MAX_BACKOFF_SECONDS))
            self._failures[binary['sha256']] = (failures,
                                                time.monotonic() + delay)
            self._log(f"engine update to {binary['file']} failed: {exc}; "
                      f'still running {path}, next try in about '
                      f'{max(delay, self.interval) / 60:.0f} min', flush=True)
            return False
        self._failures.pop(binary['sha256'], None)
        new_provenance = {
            'engine_sha': engine_sha,
            'net_sha': net['sha256'] if net else _net_sha256(new_path)}
        with self._lock:
            # Numbered from what is current NOW: a job may have taken an
            # engine back while this one was downloading.
            number, before_path, before_provenance = self._current
            self._before[number + 1] = (before_path, before_provenance)
            self._current = (number + 1, new_path, new_provenance)
        checked = f', bench {bench}' if bench is not None else ''
        self._log(f'engine update: {before_path} -> {new_path} ({name}'
                  f'{checked}); every job switches to it when its current '
                  'task ends', flush=True)
        return True

    def reject(self, generation, reason):
        """Take back a newly offered engine that keeps failing in use.

        The engine that ran before it becomes the next generation -- every
        job and the solver return to it between tasks -- and its sha is not
        offered again until the manifest names another binary.  True when
        this call took it back; False when ``generation`` is no longer
        current (another job did it, or a newer one arrived) or there is
        nothing to go back to (the engine the worker started with, or one
        that is itself a taken-back return).
        """
        with self._lock:
            number, path, provenance = self._current
            if number != generation or number not in self._before:
                return False
            back_path, back_provenance = self._before.pop(number)
            self._rejected.add(provenance.get('engine_sha', ''))
            self._current = (number + 1, back_path, back_provenance)
        self._log(f'engine update: {path} taken back after {reason}; every '
                  f'job returns to {back_path}, and this binary is not '
                  'offered again until another one is published', flush=True)
        return True

    def _install(self, binary, net, running):
        self.directory.mkdir(exist_ok=True)
        dest = self.directory / _engine_local_name(binary)
        if dest.resolve() == Path(running).resolve():
            # Only if the running file no longer matches its own name.
            raise EngineUpdateError(
                f'{dest} is the running engine; it is not overwritten in use')
        _download_verified(self.server, binary, dest)
        if net is not None:
            net_dest = Path(net['file'])
            if (net_dest.is_file()
                    and _file_sha256(net_dest) != net['sha256']):
                raise EngineUpdateError(
                    f'{net_dest} exists with other content and an engine in '
                    'use may load it; the new net loads at the next start')
            _download_verified(self.server, net, net_dest)
        return str(dest)

    def run(self, stop):
        """Check until ``stop`` is set.  Never raises."""
        while not stop.wait(self.interval * (
                1 + ENGINE_UPDATE_JITTER * (2 * random.random() - 1))):
            try:
                self.check_once()
            except Exception as exc:  # the thread must survive anything
                self._log(f'engine update check failed: {exc}', flush=True)


def _switch_engine(slot, eng, old_path, new_path, threads, a):
    """Move one job to ``new_path`` between tasks: (engine, switched).

    The old process is closed first, so two hash tables of one job never
    coexist; when the new binary does not come up, the old one -- still on
    disk -- is started again and the job carries on as before.
    """
    try:
        eng.close()
    except Exception:
        pass
    try:
        new = Engine(new_path, threads=threads, hash_mb=a.hash,
                     syzygy=a.syzygy)
        if not new.ready:
            new.close()
            raise EngineUpdateError('it did not answer isready')
    except Exception as exc:
        print(f'[{slot}] engine switch to {new_path} failed: {exc}; back on '
              f'{old_path}', flush=True)
        # A moment of memory pressure or an antivirus scan must not end the
        # job; a binary that stays unstartable still ends it, loudly, as a
        # failed restart after an engine crash always has.
        for attempt in range(3):
            try:
                return Engine(old_path, threads=threads, hash_mb=a.hash,
                              syzygy=a.syzygy), False
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))
    print(f'[{slot}] engine switched to {new_path}', flush=True)
    return new, True


class SolverEngineFailure(Exception):
    """El motor no BUSCO: no hay resultado que enviar, ni siquiera UNKNOWN.

    UNKNOWN es un veredicto -- "busque hasta agotar el presupuesto y no pude
    cerrar" -- y no un cajon de sastre para "este binario no sabe resolver".
    Confundirlos es lo que quemo 64 tareas el 21-ago: el build publicado en
    ``/atomicdb/engines`` no traia el comando ``solve``, contesto ``Unknown
    command`` en 0,6 s, y el cliente convirtio ese fallo del motor en un
    resultado que el servidor acepto de buena fe y marco COMPLETED.

    La regla que impone esta excepcion: una tarea solo se consume cuando el
    solver ha buscado de verdad.  Un fallo de motor suelta el arriendo en
    silencio y deja que caduque, porque una tarea caducada se reintenta y una
    tarea enviada como UNKNOWN esta cerrada para siempre.
    """


class Solver:
    """Drives the engine's `solve` command over a fresh process each time.

    A solve job is minutes long and allocates its own proof table, so it does
    NOT share the analysis engine's process: a solver that runs out of memory
    or wedges must not take the analysis worker down with it.
    """

    def __init__(self, path, hash_mb=256, build_sha=''):
        self.path = path
        self.hash_mb = hash_mb
        # sha256 del binario que firma CADA resultado enviado.  El servidor ya
        # tenia el campo -- ``api_solve_submit`` lee ``solver_build`` y lo
        # sanea con ``_sha_field`` -- pero el cliente nunca lo rellenaba, asi
        # que las 4602 filas historicas dicen ''.  Sin esa firma la pregunta
        # "que build produjo este resultado" no se contesta desde la base de
        # datos, y por eso el fallo de hoy fue invisible ahi dentro.
        self.build_sha = build_sha or ''

    @staticmethod
    def _first_signal(process):
        """La primera linea util de la salida, para que el error DIGA algo.

        El banner del motor es siempre la primera linea y nunca es la noticia;
        lo interesante viene detras (``Unknown command: 'solve ...'``).
        """
        for stream in (process.stdout, process.stderr):
            for line in (stream or '').splitlines():
                line = line.strip()
                if line and not line.startswith('Atomic-Stockfish '):
                    return line[:300]
        return '(no output)'

    def _assert_searched(self, process, reported, engine_error, outcome, nodes):
        """Levanta si lo que volvio NO es una busqueda. No devuelve nada."""
        if process.returncode != 0:
            raise SolverEngineFailure(
                f'the engine exited with code {process.returncode}: '
                f'{self._first_signal(process)}')
        if engine_error:
            # `solve error: invalid FEN (...)`: el motor entendio el comando y
            # se nego.  Es un fallo con nombre, no un veredicto.
            raise SolverEngineFailure(
                f'the engine refused the task: {engine_error[:300]}')
        if not reported:
            # El caso del 21-ago.  Un build sin el comando no imprime una sola
            # linea `solve outcome`, asi que el parser se queda con su tupla
            # inicial y devuelve un UNKNOWN que nadie ha calculado.
            raise SolverEngineFailure(
                'the engine printed no `solve outcome` line, so this build '
                f'has no solver: {self._first_signal(process)}')
        if outcome == 'UNKNOWN' and nodes <= 0:
            # Para poder decir "no cerre" hay que haber buscado.  Cero nodos
            # es un solver que no llego a arrancar.
            raise SolverEngineFailure(
                'the engine returned UNKNOWN without searching a single node')

    def solve(self, fen, goal, budget_nodes, movetime_ms=0):
        """Return (outcome, pn, dn, nodes, elapsed, certificate, telemetry).

        ``telemetry`` carries the fortress indicators the solver reports.  It
        is advisory scheduling information: the server stores it and never
        lets it near a verdict.

        Raises ``SolverEngineFailure`` when the engine did not search at all.
        Devolver un UNKNOWN inventado seria mas comodo aqui y carisimo tres
        capas mas arriba, donde ya no se distingue de un veredicto real.
        """
        command = (f'solve {fen} goal {goal} nodes {int(budget_nodes)} '
                   f'hash {int(self.hash_mb)}')
        if movetime_ms:
            command += f' movetime {int(movetime_ms)}'
        started = time.time()
        # stderr se CAPTURA (antes iba a DEVNULL): cuando el motor falla, su
        # queja suele ser lo unico que explica por que, y tirarla obliga a
        # diagnosticar a ciegas.  Se lee aparte, nunca se parsea como salida.
        process = subprocess.run(
            [self.path], input=command + '\nquit\n', text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        elapsed = time.time() - started

        outcome, pn, dn, nodes = 'UNKNOWN', None, None, 0
        telemetry = {}
        certificate = []
        in_certificate = False
        reported = False
        engine_error = ''
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
                reported = True
            elif name == 'error:':
                engine_error = line
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
        self._assert_searched(process, reported, engine_error, outcome, nodes)
        return outcome, pn, dn, nodes, elapsed, text, telemetry


class SolveLeaseState:
    """La asignacion SOLVE viva, para que el latido sepa por quien latir.

    Deliberadamente SIN la guarda de progreso de ``CurrentTaskState``: el
    buscador de pruebas no publica contadores intermedios, asi que exigirle
    senales de avance seria condenarlo al silencio, y el silencio es
    exactamente lo que caduca el arriendo.  Aqui la evidencia de vida es que
    el hilo sigue dentro de su tarea.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._task_id = None
        self._lease_token = ''

    def start(self, task_id, lease_token=''):
        with self._lock:
            self._task_id = task_id
            self._lease_token = lease_token or ''

    def clear(self):
        with self._lock:
            self._task_id = None
            self._lease_token = ''

    def snapshot(self):
        with self._lock:
            if self._task_id is None:
                return None
            return {'id': self._task_id, 'lease_token': self._lease_token}


def _solve_heartbeat_loop(server, auth, current_task, stop):
    """Mantiene vivo el arriendo mientras la prueba corre.

    Una tarea del estrato fortaleza retiene el hilo mucho mas que los
    ``SOLVE_LEASE_MINUTES`` del servidor.  Sin este latido el arriendo caduca
    a mitad de la busqueda, otro worker se lleva la misma tarea y el resultado
    que llega despues se rechaza por ficha caducada: trabajo tirado a la
    basura por no decir "sigo aqui".
    """
    while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
        current = current_task.snapshot()
        if current is None:
            continue
        try:
            _http_session().post(
                server + '/atomicdb/api/solve/heartbeat',
                data=dict(auth, task_id=current['id'],
                          lease_token=current['lease_token']),
                timeout=(SUBMIT_CONNECT_TIMEOUT_SECONDS, 30)) \
                    .raise_for_status()
        except requests.RequestException as exc:
            print(f'solve heartbeat skipped: {exc}', flush=True)


class SolveAcquireAmbiguous(Exception):
    """El acquire fallo sin saber si el servidor llego a asignar la tarea."""


def _solve_once(server, auth, solver, lease_session, current_task=None):
    """Acquire, solve and submit ONE proof task. Returns True if it did work."""
    try:
        response = _http_session().post(
            server + '/atomicdb/api/solve/acquire',
            data=dict(auth, lease_session=lease_session),
            timeout=(HTTP_CONNECT_TIMEOUT_SECONDS, 60))
        response.raise_for_status()
        tasks = response.json().get('tasks') or []
    except (requests.RequestException, ValueError) as exc:
        print(f'solve lease error: {exc}', flush=True)
        # La peticion pudo commitear antes de romperse: quien la repita tiene
        # que hacerlo con la MISMA sesion o el replay del servidor no la
        # reconoce y la asignacion se queda huerfana.
        raise SolveAcquireAmbiguous(str(exc)) from exc
    if not tasks:
        return False

    task = tasks[0]
    if current_task is not None:
        current_task.start(task['id'], task.get('lease_token', ''))
    try:
        try:
            outcome, pn, dn, nodes, elapsed, certificate, telemetry = \
                solver.solve(task['fen'], task.get('goal', 'WHITE_WIN'),
                             task['budget_nodes'])
        except SolverEngineFailure as exc:
            # NO se envia NADA.  El arriendo se suelta al salir (el ``finally``
            # para el latido) y el servidor devuelve la tarea a PENDING cuando
            # caduque, a los ``SOLVE_LEASE_MINUTES``.  Perder una tarea por
            # caducidad cuesta veinte minutos de espera; enviarla como UNKNOWN
            # la cierra en COMPLETED para siempre.  Esa asimetria es toda la
            # razon de ser de esta rama.
            print(f"solve task {task['id']} ENGINE FAILURE, nothing submitted "
                  f'(the task returns to the queue when its lease expires): '
                  f'{exc}', flush=True)
            raise
        payload = {**auth, 'task_id': task['id'], 'outcome': outcome,
                   'nodes': nodes, 'elapsed': f'{elapsed:.2f}',
                   'lease_token': task.get('lease_token', ''), **telemetry}
        if solver.build_sha:
            # Procedencia, igual que ``provenance`` en las rutas de analisis:
            # cada resultado queda firmado por el binario que lo produjo.
            payload['solver_build'] = solver.build_sha
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
            submitted = _http_session().post(
                server + '/atomicdb/api/solve/submit',
                data=payload, files=files,
                timeout=(SUBMIT_CONNECT_TIMEOUT_SECONDS,
                         SUBMIT_READ_TIMEOUT_SECONDS))
            summary = submitted.json().get('summary', submitted.text[:200])
        except (requests.RequestException, ValueError) as exc:
            summary = f'submit error: {exc}'
    finally:
        if current_task is not None:
            current_task.clear()
    print(f"solve task {task['id']} {outcome} ({nodes}n, {elapsed:.1f}s) "
          f'-> {summary}', flush=True)
    return True


def _solve_loop(server, auth, solver, stop_event, engines=None):
    """Proof thread. The solver is single-threaded, so it runs BESIDE the
    analysis engine instead of in front of it: one core proves while the
    engine's threads keep the analysis queue (and visitor requests) moving.
    A fortress-stratum attempt can hold this thread for many minutes; that
    must never freeze the analysis loop again.

    With ``engines`` (an EngineUpdater) the solver takes a newly published
    engine between proofs.  This thread is the only one that touches
    ``solver.path`` and ``solver.build_sha``, and only between tasks, so a
    proof is always signed by the binary that ran it."""
    current_task = SolveLeaseState()
    threading.Thread(
        target=_solve_heartbeat_loop,
        args=(server, auth, current_task, stop_event),
        name='atomicdb-solve-heartbeat', daemon=True).start()

    # Nonce de una peticion LOGICA de arriendo, con la misma regla que el
    # hilo de analisis: se conserva mientras el resultado sea ambiguo (la
    # asignacion pudo commitear y perderse la respuesta) y se renueva solo
    # cuando el servidor ya ha contestado algo que cierra la peticion.
    lease_session = secrets.token_urlsafe(24)
    engine_failures = 0
    generation = 0
    proven = None      # (path, sha) of the last binary that really solved
    while not stop_event.is_set():
        if engines is not None:
            offered, path, provenance = engines.current()
            if offered != generation:
                generation = offered
                solver.path = path
                solver.build_sha = provenance.get('engine_sha', '')
                engine_failures = 0
                print(f'solve: engine switched to {path}', flush=True)
        did = False
        try:
            did = _solve_once(server, auth, solver, lease_session,
                              current_task)
            lease_session = secrets.token_urlsafe(24)
            engine_failures = 0
            if did:
                proven = (solver.path, solver.build_sha)
        except SolveAcquireAmbiguous:
            pass                       # misma sesion en el reintento
        except SolverEngineFailure:
            # Ya se ha impreso arriba con el detalle que dio el motor.  Se
            # reintenta un par de veces por si el fallo era transitorio y
            # luego se APAGA el modo SOLVE: un binario que no sabe resolver
            # no cambia de opinion, y seguir pidiendo tareas para soltarlas
            # solo pasea la cola por arriendos de veinte minutos.  El analisis
            # sigue intacto; este hilo es el unico que se retira.
            lease_session = secrets.token_urlsafe(24)
            engine_failures += 1
            if engine_failures >= SOLVER_ENGINE_FAILURE_LIMIT:
                if engines is None:
                    print(f'solve DISABLED after {engine_failures} '
                          f'consecutive engine failures: {solver.path} cannot '
                          'solve. No solve task was consumed; analysis keeps '
                          'running. Check that this build answers the `solve` '
                          'command.', flush=True)
                    return
                # A newly published binary without the solver must not stop
                # the proofs the previous one -- still on disk -- was doing.
                # Path and signature go back together; the next engine
                # published is tried again.
                if (proven is not None
                        and proven != (solver.path, solver.build_sha)):
                    print(f'solve: {solver.path} cannot solve; proofs go '
                          f'back to {proven[0]} until another engine is '
                          'published', flush=True)
                    solver.path, solver.build_sha = proven
                    engine_failures = 0
                    continue
                # Same reasoning as above, but a newly published engine may
                # well solve: wait for one instead of retiring for good.
                print(f'solve PAUSED after {engine_failures} consecutive '
                      f'engine failures: {solver.path} cannot solve. No solve '
                      'task was consumed; analysis keeps running. Proofs '
                      'resume when a new engine is published.', flush=True)
                while not stop_event.wait(60):
                    if engines.current()[0] != generation:
                        break
                continue
        except Exception as exc:  # the thread must survive anything
            print(f'solve loop error: {exc}', flush=True)
            lease_session = secrets.token_urlsafe(24)
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
                   owns_updates, engines=None):
    """One independent leaseholder: its own engine, lease and heartbeat.

    A slot is a worker in every sense the server cares about.  It has its own
    machine identity, so every rule the protocol already enforces per machine
    -- one live assignment, the fencing token, heartbeat keepalive, replay of a
    lost response -- applies per slot with nothing added to the protocol and
    nothing added to the schema.  That is the whole reason the slot lives in
    the identity rather than in a new column.

    With ``engines`` (an EngineUpdater) the slot takes a newly published
    engine between batches; ``engine_path`` and ``provenance`` always change
    together, so each result names the binary that analysed it.  The slot
    starts on the engine main() provisioned (generation 0) and reaches any
    later one only through ``_switch_engine``, which can fall back.
    """
    engine_path = a.engine
    generation = 0
    eng = Engine(engine_path, threads=engine_threads, hash_mb=a.hash,
                 syzygy=a.syzygy)
    failed_offer, engine_retry_at = None, 0.0
    engine_failures = 0        # consecutive failed searches of this engine

    def pending_offer():
        """The other engine this job should move to now, if any."""
        if engines is None:
            return None
        offered = engines.current()
        if offered[0] == generation:
            return None
        if offered[0] == failed_offer and time.monotonic() < engine_retry_at:
            return None
        return offered

    current_task = CurrentTaskState()
    threading.Thread(
        target=_heartbeat_loop, args=(a.S, auth, current_task, stop),
        name=f'atomicdb-heartbeat-{slot}', daemon=True).start()

    next_update_check = time.monotonic() + WORKER_UPDATE_INTERVAL_SECONDS
    # Idempotency nonce for one logical lease request. Preserve it only across
    # transport/ambiguous-response failures where the server may have committed;
    # rotate only after a valid 2xx payload or a definitive 4xx rejection.
    lease_session = secrets.token_urlsafe(24)
    lease_failures = 0

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
                    eng = Engine(engine_path, threads=engine_threads,
                                 hash_mb=a.hash, syzygy=a.syzygy)
        # A newly published engine is taken here, between batches: never in
        # the middle of a search.  A failed switch keeps the old engine and
        # waits before trying that same offer again.
        offered = pending_offer()
        if offered is not None:
            eng, switched = _switch_engine(slot, eng, engine_path,
                                           offered[1], engine_threads, a)
            if switched:
                generation, engine_path, provenance = offered
                engine_failures = 0
            else:
                failed_offer = offered[0]
                engine_retry_at = (time.monotonic()
                                   + ENGINE_SWITCH_RETRY_SECONDS)
        try:
            tasks = _request_tasks(a.S, auth, lease_session)
        except LeaseRequestError as e:
            if not e.ambiguous:
                lease_session = secrets.token_urlsafe(24)
            lease_failures += 1
            delay = min(LEASE_RETRY_INITIAL_SECONDS
                        * (2 ** min(lease_failures - 1, 4)),
                        LEASE_RETRY_MAX_SECONDS)
            print(f'[{slot}] lease response error: {e}; retrying in {delay}s',
                  flush=True)
            if stop.wait(delay):
                break
            continue
        lease_failures = 0
        lease_session = secrets.token_urlsafe(24)
        if not tasks:
            print(f'[{slot}] no tasks available; waiting 60s', flush=True)
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
                # Nothing is submitted: the task comes back by itself when
                # its lease expires, instead of a truncated search being
                # stored as if it were a result.
                current_task.clear()
                engine_failures += 1
                if stop.is_set():      # Ctrl+C reaches the engines too
                    print(f'[{slot}] engine failure: {e}', flush=True)
                    break
                # A newly offered engine that keeps failing real searches is
                # taken back for every job (no-op for the engine the worker
                # started with, or one that already is a return).
                if (engines is not None
                        and engine_failures >= ENGINE_FAILURE_LIMIT):
                    engines.reject(generation,
                                   f'{engine_failures} failed searches in a '
                                   f'row ({e})')
                offered = pending_offer()
                if offered is not None:
                    print(f'[{slot}] engine failure: {e}; moving to '
                          f'{offered[1]}', flush=True)
                    eng, switched = _switch_engine(
                        slot, eng, engine_path, offered[1], engine_threads, a)
                    if switched:
                        generation, engine_path, provenance = offered
                        engine_failures = 0
                    else:
                        failed_offer = offered[0]
                        engine_retry_at = (time.monotonic()
                                           + ENGINE_SWITCH_RETRY_SECONDS)
                    continue   # la tarea vuelve sola al caducar su lease
                print(f'[{slot}] engine failure: {e}; restarting the engine',
                      flush=True)
                try:
                    eng.close()
                except Exception:
                    pass
                eng = Engine(engine_path, threads=engine_threads,
                             hash_mb=a.hash, syzygy=a.syzygy)
                continue   # la tarea vuelve sola al caducar su lease
            engine_failures = 0
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
                    # Aditivo: se devuelve la restriccion CON LA QUE SE BUSCO,
                    # para que el servidor sepa que estas lineas no hablan de
                    # la posicion entera sino de lo que quedaba sin resolver.
                    # Un servidor que no la conozca la ignora.
                    'searchmoves': ' '.join(t.get('searchmoves') or ()),
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
                    help='your own binary (optional; without it the '
                         'reference build is downloaded)')
    ap.add_argument('-T', type=int, default=4)
    ap.add_argument('--jobs', type=int, default=1,
                    help='concurrent analyses; -T is SPLIT between them. '
                         'A single position scales poorly with many threads, '
                         'so K analyses of T/K threads outperform one of T')
    ap.add_argument('--hash', type=int, default=512)
    ap.add_argument('--syzygy', default='',
                    help='atomic TB dirs separated by ; (when given, it is '
                         'used as-is and nothing is downloaded)')
    ap.add_argument('--no-fetch-syzygy', action='store_true',
                    help=f'do not download the {SYZYGY_SET} set (1.3 GB) '
                         'when --syzygy was not given')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--no-auto-update', action='store_true',
                    help='do not update this file from the official server')
    ap.add_argument('--no-engine-update', action='store_true',
                    help='do not switch to a newly published engine while '
                         'running (it is still picked up at the next start)')
    ap.add_argument('--engine-update-minutes', type=float,
                    default=ENGINE_UPDATE_INTERVAL_SECONDS / 60,
                    help='minutes between checks for a newly published '
                         'engine (default %(default)g, minimum '
                         f'{ENGINE_UPDATE_MIN_SECONDS // 60})')
    ap.add_argument('--solve', action='store_true',
                    help='also serve SOLVE tasks (exhaustive proofs) besides '
                         'analysis; without this flag the worker behaves '
                         'exactly as before')
    a = ap.parse_args()

    if not a.no_auto_update and _install_worker_update(a.S):
        _restart_updated_worker(once=a.once)

    # Deliberately AFTER the self-update: a restarted worker re-enters main()
    # from the top, so the set is re-verified on every start rather than only
    # on cold ones.  Verification of an intact set is a stat and a hash, so
    # this costs seconds and downloads nothing.
    a.syzygy = resolve_syzygy(a, a.S)

    # The probing tablebase and the ADVERTISED capability are two different
    # things.  python-chess gives the worker a fast pre-probe that skips the
    # engine entirely; the engine gets SyzygyPath either way and resolves
    # endgames itself.  So a missing python-chess must degrade the pre-probe,
    # not the capability -- reporting tb=0 there would send every tablebase
    # position to some other worker for no reason.
    tb = None
    if a.syzygy:
        dirs = [d for d in a.syzygy.split(';') if d]
        try:
            import chess.syzygy
            import chess.variant
            tb = chess.syzygy.open_tablebase(
                dirs[0], VariantBoard=chess.variant.AtomicBoard)
            for d in dirs[1:]:
                tb.add_directory(d)
            print(f'syzygy: {len(dirs)} dirs, pre-probe active', flush=True)
        except Exception as error:
            print(f'syzygy: {len(dirs)} dirs for the engine; no pre-probe '
                  f'({error})', flush=True)

    import platform
    provisioned = None
    if not a.engine:
        provisioned = _provision_engine(a.S)
        a.engine = provisioned['path']

    # --once keeps the historical smoke semantics, which are single-slot.
    if a.once:
        a.jobs = 1
    jobs, slot_threads, solver_cores = plan_threads(a.T, a.jobs, a.solve)
    if jobs < a.jobs:
        print(f'note: --jobs {a.jobs} reduced to {jobs}: -T {a.T} does not '
              f'give each job a full thread', flush=True)
    if a.solve and a.T < 2:
        print('note: --solve with -T 1 shares the single thread between '
              'analysis and proofs', flush=True)

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
                 'tb': '1' if a.syzygy else '0',
                 'os': f'{platform.system()} {platform.release()}',
                 'worker_build': ATOMICDB_WORKER_BUILD}
    # Additive provenance: an older server simply ignores these POST fields.
    # A downloaded engine that loads its net from disk names exactly the net
    # the manifest gave it; the directory scan is the fallback for the rest.
    provenance = {'engine_sha': _file_sha256(a.engine),
                  'net_sha': (provisioned['net']['sha256'].lower()
                              if provisioned and provisioned['net']
                              else _net_sha256(a.engine))}

    stop = threading.Event()
    # Only the downloaded reference engine follows the manifest: a binary
    # given with --engine is the contributor's choice and stays.
    engines = None
    if provisioned is not None and not a.once and not a.no_engine_update:
        engines = EngineUpdater(
            a.S, a.engine, provenance,
            interval=min(max(ENGINE_UPDATE_MIN_SECONDS,
                             a.engine_update_minutes * 60), 24 * 3600))
        threading.Thread(target=engines.run, args=(stop,),
                         name='atomicdb-engine-update', daemon=True).start()
    solver = (Solver(a.engine, hash_mb=a.hash,
                     build_sha=provenance['engine_sha'])
              if a.solve else None)
    if solver is not None and not a.once:
        threading.Thread(
            target=_solve_loop,
            args=(a.S, dict(auth_base, machine=machines[0]), solver, stop,
                  engines),
            name='atomicdb-solver', daemon=True).start()

    spread = ('x'.join(str(n) for n in sorted(set(slot_threads), reverse=True))
              if len(set(slot_threads)) > 1 else str(slot_threads[0]))
    print(f'AtomicDB worker: {a.engine} T={a.T} -> {jobs} jobs of '
          f'{spread} threads ({sum(slot_threads) + solver_cores} in total)'
          + (f' + {solver_cores} solver' if solver_cores else '')
          + f' -> {a.S}', flush=True)
    if engines is not None:
        print(f'engine updates: a newly published engine is installed while '
              f'running (checked every {engines.interval / 60:g} min; '
              '--no-engine-update turns this off)', flush=True)

    if a.once:
        solved = False
        if a.solve:
            try:
                solved = _solve_once(a.S,
                                     dict(auth_base, machine=machines[0]),
                                     solver, secrets.token_urlsafe(24))
            except (SolveAcquireAmbiguous, SolverEngineFailure):
                # Una sola pasada no reintenta nada: aqui tanto el acquire
                # roto como el motor sin solver son "no hubo prueba", y se
                # baja al analisis.  El fallo de motor ya se ha impreso con su
                # detalle; lo que NO puede hacer es tumbar el smoke entero,
                # que existe precisamente para comprobar el resto del camino.
                solved = False
        if solved:
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
                  provenance, slot_threads[slot], stop, slot == 0, engines),
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
