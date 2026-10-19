"""Which engine builds AtomicDB takes analysis from.

The worker reports the sha256 of the binary it searched with (``engine_sha``,
see ``views.api_submit``).  That is telemetry from an untrusted client: it
cannot stop someone determined to lie, and it is not meant to.  What it stops
is the honest accident -- a worker pointed at the wrong binary with
``--engine``.  In October an engine that was not playing atomic chess fed 188
positions before anyone noticed, and every one of them had to be searched
again.

The builds accepted are the reference binaries the server publishes itself,
every ``Atomic-Stockfish-*`` file in the engines directory (the current ones
and the previous ones still kept there), plus any sha256 listed under
``extra_engines`` in the policy file.  Retiring a build is deleting its file.

The gate is OFF unless the policy file says ``"mode": "enforce"``.  A missing
or unreadable policy file leaves it off, so shipping this module changes
nothing until someone writes that file::

    {"mode": "enforce",
     "extra_engines": {"<sha256>": "who built it and why it is trusted"}}

Tablebase results are never gated: the worker probes the tables itself, the
engine is not involved, and the server checks those closures on its own.
"""
import json
import os

from django.conf import settings

DEFAULT_POLICY_FILE = '/etc/openbench/atomicdb-engine-policy.json'
DEFAULT_ENGINES_DIR = '/var/www/atomicdb-engines'
REFERENCE_PREFIX = 'Atomic-Stockfish-'

REJECTION_DETAIL = (
    'AtomicDB only takes analysis from the reference engine. Start the worker '
    'without --engine and it downloads the reference build; to get your own '
    'build accepted, ask on the Belzedar Labs Discord.')

# (path, size, mtime_ns) -> sha256, so a binary is hashed once per process
# and again only when its file changes.
_digests = {}
# path -> ((size, mtime_ns), parsed policy)
_policies = {}


def _policy_path():
    return str(getattr(settings, 'ATOMICDB_ENGINE_POLICY_FILE',
                       DEFAULT_POLICY_FILE) or '')


def _engines_dir():
    return str(getattr(settings, 'ATOMICDB_ENGINES_DIR',
                       DEFAULT_ENGINES_DIR) or '')


def _is_sha256(value):
    return (isinstance(value, str) and len(value) == 64
            and all(ch in '0123456789abcdef' for ch in value))


def load_policy():
    """``{'mode': 'off'|'enforce', 'extra_engines': {sha: note}}``.

    Anything odd about the file (absent, unreadable, not JSON, an unknown
    mode) reads as ``off``.  A typo in the policy must never start rejecting
    the whole fleet.
    """
    off = {'mode': 'off', 'extra_engines': {}}
    path = _policy_path()
    if not path:
        return off
    try:
        stat = os.stat(path)
    except OSError:
        return off
    stamp = (stat.st_size, stat.st_mtime_ns)
    cached = _policies.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        with open(path, encoding='utf-8') as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return off
    policy = dict(off)
    if isinstance(raw, dict):
        if raw.get('mode') == 'enforce':
            policy['mode'] = 'enforce'
        extra = raw.get('extra_engines')
        if isinstance(extra, dict):
            policy['extra_engines'] = {
                sha.lower(): str(note) for sha, note in extra.items()
                if _is_sha256(str(sha).lower())}
    _policies[path] = (stamp, policy)
    return policy


def _file_digest(path, stat):
    import hashlib

    key = (path, stat.st_size, stat.st_mtime_ns)
    digest = _digests.get(key)
    if digest is None:
        hasher = hashlib.sha256()
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        _digests[key] = digest
    return digest


def reference_engines():
    """``{sha256: file name}`` of every reference binary being served."""
    found = {}
    root = _engines_dir()
    if not root:
        return found
    try:
        entries = list(os.scandir(root))
    except OSError:
        return found
    for entry in entries:
        if not entry.name.startswith(REFERENCE_PREFIX):
            continue
        try:
            if not entry.is_file():
                continue
            found[_file_digest(entry.path, entry.stat())] = entry.name
        except OSError:
            continue
    return found


def accepted_engines(policy=None):
    """``{sha256: description}`` of every build the gate lets through."""
    policy = policy or load_policy()
    accepted = {sha: f'reference: {name}'
                for sha, name in reference_engines().items()}
    for sha, note in policy['extra_engines'].items():
        accepted.setdefault(sha, f'extra: {note}')
    return accepted


def enforcing():
    return load_policy()['mode'] == 'enforce'


def rejects(engine_sha, tablebase=False):
    """True when the gate is on and this result has to be refused.

    Cheap while the gate is off: nothing is hashed unless it is enforcing.
    With no reference binary to be found (directory moved, permissions) the
    gate lets everything through: refusing the whole fleet over a path is
    worse than the accident the gate exists for.
    """
    if tablebase:
        return False
    policy = load_policy()
    if policy['mode'] != 'enforce':
        return False
    if not reference_engines():
        return False
    return engine_sha not in accepted_engines(policy)
