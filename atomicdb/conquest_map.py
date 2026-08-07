"""Versioned, read-only Atomic move-tree snapshots for AtomicDB.

The live solver graph is a DAG with possible reversible cycles.  Public web
requests must never recursively traverse that graph, so a management command
builds a deterministic display-tree projection and atomically publishes a
compact gzip JSON artifact.  This module validates that artifact and serves a
bounded hierarchical view without consulting or mutating the database.
"""

from collections import defaultdict, deque
import gzip
import hashlib
import heapq
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import zlib
from datetime import datetime, timezone as datetime_timezone

from django.conf import settings
from django.http import HttpResponse, JsonResponse

from . import logic, openings


SNAPSHOT_SCHEMA = 'atomicdb.map.snapshot.v1'
API_SCHEMA = 'atomicdb.map.v1'
ERROR_SCHEMA = 'atomicdb.map.error.v1'

DEFAULT_MARKS = 500
MAX_MARKS = 600
DEFAULT_DEPTH = 16
MAX_DEPTH = 32
MAX_WORK_ITEMS = 16
MAX_API_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
SNAPSHOT_READ_CHUNK = 64 * 1024

# ingest.DEAD is -1e9 and the live queue treats DEAD / 2 as the boundary.
# Keep this value local: snapshot construction must not invoke scheduler code.
HISTORICAL_PRIORITY_CUTOFF = -500_000_000.0

WEIGHTS = frozenset(('frontier', 'explored', 'compute'))
HEX_KEY = re.compile(r'^[0-9a-f]{64}$')
_MISSING_REGRET = 10 ** 30
_EXACT_UTILITY = 10 ** 15

# Compact aggregate vector stored once per full-snapshot node.  The public API
# expands this into named dictionaries for readability.
(
    M_POSITIONS,
    M_CLOSED,
    M_UNKNOWN,
    M_FRONTIER,
    M_HISTORICAL,
    M_NODES,
    M_SECONDS,
    M_ACTIVE,
    M_QUEUED,
    M_TRANSPOSITIONS,
    M_WHITE_WIN,
    M_BLACK_WIN,
    M_DRAW,
    M_STATUS_UNKNOWN,
    M_TB,
    M_MATE_PV,
    M_MINIMAX,
    M_TERMINAL,
    M_CLOSURE_NONE,
    M_ANDOR,
    M_ENGINE,
    M_DISPUTED,
    M_PROOF_NONE,
) = range(23)

logger = logging.getLogger(__name__)
_snapshot_cache = {'signature': None, 'snapshot': None, 'error': None}
_snapshot_cache_lock = threading.Lock()
_legacy_work_index_cache = {
    'object_id': None,
    'snapshot_id': None,
    'work_keys': None,
    'generation': 0,
    'inflight': {},
}


class SnapshotError(ValueError):
    """Published snapshot is absent, corrupt or violates the v1 contract."""


def _canonical_json(value):
    return b''.join(_canonical_chunks(value))


def _canonical_chunks(value):
    encoder = json.JSONEncoder(
        sort_keys=True, separators=(',', ':'), ensure_ascii=False,
        allow_nan=False,
    )
    for chunk in encoder.iterencode(value):
        yield chunk.encode('utf-8')


def _snapshot_digest(snapshot_without_id):
    digest = hashlib.sha256()
    for chunk in _canonical_chunks(snapshot_without_id):
        digest.update(chunk)
    return digest.hexdigest()


def seal_snapshot(snapshot_without_id):
    """Return a shallow envelope with an identity covering every other field.

    Nodes are already private to a just-built immutable artifact.  Avoiding a
    deep copy is material at the documented million-position scale.
    """
    sealed = dict(snapshot_without_id)
    metadata = dict(sealed['snapshot'])
    metadata.pop('id', None)
    sealed['snapshot'] = metadata
    digest = _snapshot_digest(sealed)
    sealed['snapshot']['id'] = digest
    return sealed


def _unsealed_copy(snapshot):
    unsealed = dict(snapshot)
    metadata = dict(unsealed.get('snapshot') or {})
    metadata.pop('id', None)
    unsealed['snapshot'] = metadata
    return unsealed


def _validate_metric_vector(metrics):
    if not isinstance(metrics, list) or len(metrics) != 23:
        raise SnapshotError('invalid aggregate metric vector')
    for index, value in enumerate(metrics):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SnapshotError('non-numeric aggregate metric')
        if not math.isfinite(value) or value < 0:
            raise SnapshotError('invalid aggregate metric')
        if index != M_SECONDS and not isinstance(value, int):
            raise SnapshotError('non-integral aggregate counter')


def validate_snapshot(snapshot):
    """Strictly authenticate and structurally validate a loaded artifact."""
    if not isinstance(snapshot, dict):
        raise SnapshotError('snapshot must be an object')
    if snapshot.get('schema') != SNAPSHOT_SCHEMA:
        raise SnapshotError('unsupported snapshot schema')
    metadata = snapshot.get('snapshot')
    nodes = snapshot.get('nodes')
    if not isinstance(metadata, dict) or not isinstance(nodes, dict):
        raise SnapshotError('missing snapshot metadata or nodes')
    digest = metadata.get('id')
    if not isinstance(digest, str) or not HEX_KEY.fullmatch(digest):
        raise SnapshotError('invalid snapshot identity')
    if _snapshot_digest(_unsealed_copy(snapshot)) != digest:
        raise SnapshotError('snapshot content hash mismatch')

    root_key = metadata.get('root_key')
    start_key = metadata.get('start_key')
    if root_key != start_key or root_key not in nodes:
        raise SnapshotError('invalid snapshot root')
    if metadata.get('positions') != len(nodes):
        raise SnapshotError('position count mismatch')
    if (not isinstance(metadata.get('edges'), int)
            or metadata['edges'] < 0
            or not isinstance(metadata.get('max_depth'), int)
            or metadata['max_depth'] < 0):
        raise SnapshotError('invalid snapshot counters')

    required = {
        'f', 's', 'c', 'p', 'e', 'b', 'd', 'r', 'u', 'a', 'i', 'x',
        'k', 'm', 'wa', 'wq', 'di', 'v', 'ex',
    }
    for key, node in nodes.items():
        if not isinstance(key, str) or not HEX_KEY.fullmatch(key):
            raise SnapshotError('invalid position key')
        if not isinstance(node, dict) or not required.issubset(node):
            raise SnapshotError('invalid snapshot node')
        if node['s'] not in ('UNKNOWN', 'WHITE_WIN', 'BLACK_WIN', 'DRAW'):
            raise SnapshotError('invalid node status')
        if node['c'] not in (None, 'TB', 'MATE_PV', 'MINIMAX', 'TERMINAL'):
            raise SnapshotError('invalid node closure')
        if node['p'] not in (None, 'ANDOR', 'ENGINE', 'DISPUTED'):
            raise SnapshotError('invalid node proof')
        if not isinstance(node['d'], int) or node['d'] < 0:
            raise SnapshotError('invalid node depth')
        if not isinstance(node['k'], list) or len(node['k']) != len(set(node['k'])):
            raise SnapshotError('invalid display children')
        if not isinstance(node['i'], int) or not isinstance(node['x'], int):
            raise SnapshotError('invalid transposition counters')
        if node['i'] < 0 or node['x'] < 0 or node['x'] > node['i']:
            raise SnapshotError('invalid transposition counters')
        if any(
            isinstance(node[field], bool)
            or not isinstance(node[field], int)
            or node[field] < 0
            for field in ('wa', 'wq')
        ):
            raise SnapshotError('invalid exact work counter')
        _validate_metric_vector(node['m'])
        metrics = node['m']
        if (node['wa'] > metrics[M_ACTIVE]
                or node['wq'] > metrics[M_QUEUED]):
            raise SnapshotError('exact work exceeds subtree work')
        if metrics[M_CLOSED] + metrics[M_UNKNOWN] != metrics[M_POSITIONS]:
            raise SnapshotError('closed/unknown aggregate mismatch')
        if sum(metrics[M_WHITE_WIN:M_STATUS_UNKNOWN + 1]) != metrics[M_POSITIONS]:
            raise SnapshotError('status aggregate mismatch')
        if sum(metrics[M_TB:M_CLOSURE_NONE + 1]) != metrics[M_POSITIONS]:
            raise SnapshotError('closure aggregate mismatch')
        if sum(metrics[M_ANDOR:M_PROOF_NONE + 1]) != metrics[M_POSITIONS]:
            raise SnapshotError('proof aggregate mismatch')
        if metrics[M_FRONTIER] > metrics[M_UNKNOWN]:
            raise SnapshotError('frontier aggregate mismatch')

    root = nodes[root_key]
    if root['r'] is not None or root['d'] != 0 or root['u'] is not None:
        raise SnapshotError('root has a display parent')
    attributed = set()
    maximum_depth = 0
    for parent_key, parent in nodes.items():
        maximum_depth = max(maximum_depth, parent['d'])
        for child_key in parent['k']:
            child = nodes.get(child_key)
            if child is None:
                raise SnapshotError('missing display child')
            if child['r'] != parent_key or child['d'] != parent['d'] + 1:
                raise SnapshotError('invalid display-tree edge')
            if child_key in attributed:
                raise SnapshotError('display child attributed more than once')
            attributed.add(child_key)
        child_positions = sum(
            nodes[child_key]['m'][M_POSITIONS]
            for child_key in parent['k']
        )
        if child_positions + 1 != parent['m'][M_POSITIONS]:
            raise SnapshotError('position aggregate is not additive')
        for metric_index, own_field, label in (
            (M_ACTIVE, 'wa', 'active'),
            (M_QUEUED, 'wq', 'queued'),
        ):
            child_work = sum(
                nodes[child_key]['m'][metric_index]
                for child_key in parent['k']
            )
            if parent[own_field] + child_work != parent['m'][metric_index]:
                raise SnapshotError(
                    f'{label} work aggregate is not additive')
    if attributed != set(nodes) - {root_key}:
        raise SnapshotError('display tree contains unattributed nodes')
    if maximum_depth != metadata['max_depth']:
        raise SnapshotError('maximum depth mismatch')
    work_keys = metadata.get('work_keys')
    if work_keys is not None:
        if not isinstance(work_keys, list):
            raise SnapshotError('invalid exact work index')
        if any(
            not isinstance(key, str) or key not in nodes
            for key in work_keys
        ):
            raise SnapshotError('invalid exact work index')
        if len(work_keys) != len(set(work_keys)):
            raise SnapshotError('invalid exact work index')
        expected_work_keys = {
            key for key, node in nodes.items()
            if node['wa'] or node['wq']
        }
        if set(work_keys) != expected_work_keys:
            raise SnapshotError('exact work index mismatch')
    return snapshot


def read_snapshot(path):
    """Read at most the documented size budget and fail closed on corruption."""
    path = Path(path)
    try:
        with gzip.open(path, 'rb') as stream:
            raw = bytearray()
            while len(raw) <= MAX_SNAPSHOT_BYTES:
                remaining = MAX_SNAPSHOT_BYTES + 1 - len(raw)
                chunk = stream.read(min(SNAPSHOT_READ_CHUNK, remaining))
                if not chunk:
                    break
                raw.extend(chunk)
    except (OSError, EOFError, zlib.error) as exc:
        raise SnapshotError('cannot read snapshot') from exc
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise SnapshotError('snapshot exceeds uncompressed size budget')
    try:
        snapshot = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError('snapshot is not strict UTF-8 JSON') from exc
    return validate_snapshot(snapshot)


def published_snapshot(path=None):
    """Load and process-cache the current immutable artifact by file identity."""
    target = Path(path or settings.ATOMICDB_MAP_SNAPSHOT_PATH)
    try:
        stat = target.stat()
    except OSError as exc:
        raise SnapshotError('snapshot is unavailable') from exc
    signature = (
        str(target.resolve()),
        getattr(stat, 'st_dev', None),
        getattr(stat, 'st_ino', None),
        getattr(stat, 'st_ctime_ns', None),
        stat.st_mtime_ns,
        stat.st_size,
    )
    with _snapshot_cache_lock:
        if _snapshot_cache['signature'] == signature:
            if _snapshot_cache['error'] is not None:
                raise SnapshotError(_snapshot_cache['error'])
            return _snapshot_cache['snapshot']
        try:
            snapshot = read_snapshot(target)
        except SnapshotError as exc:
            _snapshot_cache.update(
                signature=signature, snapshot=None, error=str(exc))
            raise
        _snapshot_cache.update(
            signature=signature, snapshot=snapshot, error=None)
        return snapshot


def reset_snapshot_cache():
    """Test/deploy hook; normal publication invalidates by file identity."""
    with _snapshot_cache_lock:
        _snapshot_cache.update(signature=None, snapshot=None, error=None)
        _legacy_work_index_cache.update(
            object_id=None,
            snapshot_id=None,
            work_keys=None,
            generation=_legacy_work_index_cache['generation'] + 1,
        )


def publish_snapshot(snapshot, path):
    """Publish a validated gzip artifact through a same-directory rename."""
    validate_snapshot(snapshot)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=str(target.parent), prefix=f'.{target.name}.',
            suffix='.tmp',
        )
        with os.fdopen(descriptor, 'wb') as fileobj:
            with gzip.GzipFile(
                    filename='', mode='wb', fileobj=fileobj,
                    compresslevel=6, mtime=0) as compressed:
                for chunk in _canonical_chunks(snapshot):
                    compressed.write(chunk)
            fileobj.flush()
            os.fsync(fileobj.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    reset_snapshot_cache()
    return target


def _normalise_position(row):
    if isinstance(row, dict):
        return {
            'key': row['key'],
            'fen': row['fen'],
            'eval_cp': row.get('eval_cp'),
            'backed_eval': row.get('backed_eval'),
            'status': row.get('status', 'UNKNOWN'),
            'closure': row.get('closure'),
            'proof': row.get('proof'),
            'best_move': row.get('best_move'),
            'depth_invested': int(row.get('depth_invested', 0) or 0),
            'nodes_invested': int(row.get('nodes_invested', 0) or 0),
            'time_invested': float(row.get('time_invested', 0.0) or 0.0),
            'visits': int(row.get('visits', 0) or 0),
            'priority': float(row.get('priority', 0.0) or 0.0),
            'expanded': bool(row.get('expanded', False)),
        }
    (
        key, fen, eval_cp, backed_eval, status, closure, proof, best_move,
        depth_invested, nodes_invested, time_invested, visits, priority,
        expanded,
    ) = row
    return {
        'key': key, 'fen': fen, 'eval_cp': eval_cp,
        'backed_eval': backed_eval, 'status': status,
        'closure': closure, 'proof': proof, 'best_move': best_move,
        'depth_invested': int(depth_invested or 0),
        'nodes_invested': int(nodes_invested or 0),
        'time_invested': float(time_invested or 0.0),
        'visits': int(visits or 0), 'priority': float(priority or 0.0),
        'expanded': bool(expanded),
    }


def _validate_position(position):
    if not isinstance(position['key'], str) or not HEX_KEY.fullmatch(
            position['key']):
        raise SnapshotError('invalid database position key')
    if not isinstance(position['fen'], str) or not position['fen']:
        raise SnapshotError('invalid database position FEN')
    if position['status'] not in ('UNKNOWN', 'WHITE_WIN', 'BLACK_WIN', 'DRAW'):
        raise SnapshotError('invalid database status')
    if position['closure'] not in (
            None, 'TB', 'MATE_PV', 'MINIMAX', 'TERMINAL'):
        raise SnapshotError('invalid database closure')
    if position['proof'] not in (None, 'ANDOR', 'ENGINE', 'DISPUTED'):
        raise SnapshotError('invalid database proof')
    for eval_field in ('eval_cp', 'backed_eval'):
        if (position[eval_field] is not None
                and (isinstance(position[eval_field], bool)
                     or not isinstance(position[eval_field], int))):
            raise SnapshotError('invalid database evaluation')
    counters = (
        position['depth_invested'], position['nodes_invested'],
        position['visits'],
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in counters):
        raise SnapshotError('invalid database counter')
    if (not math.isfinite(position['time_invested'])
            or position['time_invested'] < 0):
        raise SnapshotError('invalid database engine time')
    if not math.isfinite(position['priority']):
        raise SnapshotError('invalid database priority')


def _position_utility(position):
    status = position['status']
    if status == 'WHITE_WIN':
        return _EXACT_UTILITY
    if status == 'BLACK_WIN':
        return -_EXACT_UTILITY
    if status == 'DRAW':
        return 0
    return position['eval_cp']


def _regret(parent, child, outgoing, positions):
    selected = _position_utility(child)
    known = [
        _position_utility(positions[child_key])
        for _uci, child_key in outgoing
        if child_key in positions
        and _position_utility(positions[child_key]) is not None
    ]
    if selected is None:
        return 0 if not known else _MISSING_REGRET
    if not known:
        return 0
    best = max(known) if parent['fen'].split()[1] == 'w' else min(known)
    return abs(best - selected)


def _san_for_edge(parent_fen, move_uci):
    try:
        import pyffish as pf
        return pf.get_san('atomic', parent_fen, move_uci)
    except Exception:
        return move_uci


def _empty_metrics():
    return [0] * 23


def _own_metrics(position, active, queued, alternate_parents):
    metrics = _empty_metrics()
    historical = position['priority'] <= HISTORICAL_PRIORITY_CUTOFF
    unknown = position['status'] == 'UNKNOWN'
    metrics[M_POSITIONS] = 1
    metrics[M_CLOSED] = int(not unknown)
    metrics[M_UNKNOWN] = int(unknown)
    metrics[M_FRONTIER] = int(unknown and not historical)
    metrics[M_HISTORICAL] = int(historical)
    metrics[M_NODES] = position['nodes_invested']
    metrics[M_SECONDS] = position['time_invested']
    metrics[M_ACTIVE] = active
    metrics[M_QUEUED] = queued
    metrics[M_TRANSPOSITIONS] = alternate_parents
    status_index = {
        'WHITE_WIN': M_WHITE_WIN,
        'BLACK_WIN': M_BLACK_WIN,
        'DRAW': M_DRAW,
        'UNKNOWN': M_STATUS_UNKNOWN,
    }[position['status']]
    metrics[status_index] = 1
    metrics[{
        'TB': M_TB,
        'MATE_PV': M_MATE_PV,
        'MINIMAX': M_MINIMAX,
        'TERMINAL': M_TERMINAL,
        None: M_CLOSURE_NONE,
    }[position['closure']]] = 1
    metrics[{
        'ANDOR': M_ANDOR,
        'ENGINE': M_ENGINE,
        'DISPUTED': M_DISPUTED,
        None: M_PROOF_NONE,
    }[position['proof']]] = 1
    return metrics


def _add_metrics(target, addition):
    for index, value in enumerate(addition):
        target[index] += value


def _prior_parent(prior_snapshot, key):
    if not prior_snapshot:
        return None
    node = (prior_snapshot.get('nodes') or {}).get(key)
    if not isinstance(node, dict):
        return None
    return node.get('r', node.get('display_parent'))


def build_snapshot_data(position_rows, edge_rows, task_rows=(),
                        prior_snapshot=None, root_key=None, generated_at=None):
    """Build one cycle-safe display tree and all post-order aggregates.

    ``position_rows`` and ``edge_rows`` are materialised bulk reads.  No
    transaction or database write is performed here, which keeps solver submit
    paths independent from snapshot generation.
    """
    positions = {}
    for row in position_rows:
        position = _normalise_position(row)
        _validate_position(position)
        positions[position['key']] = position
    root_key = root_key or logic.key_of(logic.start_fen())
    if root_key not in positions:
        raise SnapshotError('Atomic start position is absent')

    adjacency = defaultdict(list)
    incoming = defaultdict(list)
    for row in edge_rows:
        if isinstance(row, dict):
            parent_key = row['parent_id']
            child_key = row['child_id']
            move_uci = row['move_uci']
        else:
            parent_key, child_key, move_uci = row
        if parent_key not in positions or child_key not in positions:
            continue
        adjacency[parent_key].append((move_uci, child_key))
        incoming[child_key].append((parent_key, move_uci))
    for edges in adjacency.values():
        edges.sort(key=lambda edge: (edge[0], edge[1]))
    for edges in incoming.values():
        edges.sort(key=lambda edge: (edge[0], edge[1]))

    depth = {root_key: 0}
    queue = deque((root_key,))
    while queue:
        parent_key = queue.popleft()
        child_depth = depth[parent_key] + 1
        for _move_uci, child_key in adjacency.get(parent_key, ()):
            if child_key not in depth:
                depth[child_key] = child_depth
                queue.append(child_key)
    reachable = set(depth)

    display_parent = {root_key: None}
    display_move = {root_key: None}
    display_children = defaultdict(list)
    for child_key in sorted(reachable, key=lambda key: (depth[key], key)):
        if child_key == root_key:
            continue
        candidates = [
            (parent_key, move_uci)
            for parent_key, move_uci in incoming.get(child_key, ())
            if parent_key in reachable
            and depth[parent_key] + 1 == depth[child_key]
        ]
        if not candidates:
            raise SnapshotError('reachable node has no minimum-depth parent')
        previous = _prior_parent(prior_snapshot, child_key)
        valid_previous = next(
            ((parent_key, move_uci)
             for parent_key, move_uci in candidates
             if parent_key == previous),
            None,
        )
        if valid_previous is not None:
            parent_key, move_uci = valid_previous
        else:
            parent_key, move_uci = min(
                candidates,
                key=lambda candidate: (
                    _regret(
                        positions[candidate[0]], positions[child_key],
                        adjacency.get(candidate[0], ()), positions,
                    ),
                    candidate[1],
                    candidate[0],
                ),
            )
        display_parent[child_key] = parent_key
        display_move[child_key] = move_uci
        display_children[parent_key].append(child_key)
    for parent_key, child_keys in display_children.items():
        child_keys.sort(
            key=lambda key: (display_move[key], key),
        )

    task_counts = defaultdict(lambda: [0, 0])
    for row in task_rows:
        if isinstance(row, dict):
            position_key, state = row['position_id'], row['state']
        else:
            position_key, state = row
        if position_key not in reachable:
            continue
        if state == 'LEASED':
            task_counts[position_key][0] += 1
        elif state == 'PENDING':
            task_counts[position_key][1] += 1

    # El titular de eval del inspector, con la MISMA precedencia que titula
    # la cabecera del explore y la fila del padre (status probado > backed >
    # eval puntual).  Solo el campo de display: el regret del mapa
    # (``_position_utility``) sigue midiendo con la eval cruda.
    from .ingest import known_eval_of

    nodes = {}
    for key in sorted(reachable, key=lambda item: (depth[item], item)):
        position = positions[key]
        parent_key = display_parent[key]
        move_uci = display_move[key]
        reachable_incoming = sum(
            1 for candidate_parent, _uci in incoming.get(key, ())
            if candidate_parent in reachable
        )
        alternate_parents = (
            reachable_incoming if key == root_key
            else max(0, reachable_incoming - 1)
        )
        active, queued = task_counts[key]
        nodes[key] = {
            'f': position['fen'],
            's': position['status'],
            'c': position['closure'],
            'p': position['proof'],
            'e': known_eval_of(position['status'], position['backed_eval'],
                               position['eval_cp']),
            'b': position['best_move'],
            'd': depth[key],
            'r': parent_key,
            'u': move_uci,
            'a': (
                None if parent_key is None
                else _san_for_edge(positions[parent_key]['fen'], move_uci)
            ),
            'i': reachable_incoming,
            'x': alternate_parents,
            'k': list(display_children.get(key, ())),
            'm': _own_metrics(position, active, queued, alternate_parents),
            'wa': active,
            'wq': queued,
            'di': position['depth_invested'],
            'v': position['visits'],
            'ex': position['expanded'],
        }

    # Every child is attributed exactly once, so descending minimum depth is a
    # complete non-recursive post-order even when the stored graph has cycles.
    for key in sorted(reachable, key=lambda item: (depth[item], item),
                      reverse=True):
        parent_key = display_parent[key]
        if parent_key is not None:
            _add_metrics(nodes[parent_key]['m'], nodes[key]['m'])

    reachable_edges = sum(
        1 for parent_key in reachable
        for _move_uci, child_key in adjacency.get(parent_key, ())
        if child_key in reachable
    )
    generated_at = generated_at or datetime.now(datetime_timezone.utc)
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=datetime_timezone.utc)
    timestamp = generated_at.astimezone(datetime_timezone.utc).isoformat(
        timespec='microseconds').replace('+00:00', 'Z')
    unsealed = {
        'schema': SNAPSHOT_SCHEMA,
        'snapshot': {
            'generated_at': timestamp,
            'root_key': root_key,
            'start_key': root_key,
            'positions': len(reachable),
            'edges': reachable_edges,
            'max_depth': max(depth.values(), default=0),
            'unreachable_positions': len(positions) - len(reachable),
            'work_keys': [
                key
                for key in sorted(
                    reachable, key=lambda item: (depth[item], item))
                if nodes[key]['wa'] or nodes[key]['wq']
            ],
        },
        'nodes': nodes,
    }
    return seal_snapshot(unsealed)


def build_snapshot_from_database(prior_snapshot=None, generated_at=None):
    """Bulk-read current state without taking locks or writing model rows."""
    from .models import AnalysisTask, Edge, Position

    position_rows = list(Position.objects.values_list(
        'key', 'fen', 'eval_cp', 'backed_eval', 'status', 'closure', 'proof',
        'best_move', 'depth_invested', 'nodes_invested', 'time_invested',
        'visits', 'priority', 'expanded',
    ))
    edge_rows = list(Edge.objects.values_list(
        'parent_id', 'child_id', 'move_uci',
    ))
    task_rows = list(AnalysisTask.objects.filter(
        state__in=('PENDING', 'LEASED'),
    ).values_list('position_id', 'state'))
    return build_snapshot_data(
        position_rows, edge_rows, task_rows,
        prior_snapshot=prior_snapshot, generated_at=generated_at,
    )


def _expanded_metrics(metrics):
    return {
        'positions': metrics[M_POSITIONS],
        'closed': metrics[M_CLOSED],
        'unknown': metrics[M_UNKNOWN],
        'frontier': metrics[M_FRONTIER],
        'historical': metrics[M_HISTORICAL],
        'nodes': metrics[M_NODES],
        'seconds': metrics[M_SECONDS],
        'active_tasks': metrics[M_ACTIVE],
        'queued_tasks': metrics[M_QUEUED],
        'transpositions': metrics[M_TRANSPOSITIONS],
        'status': {
            'WHITE_WIN': metrics[M_WHITE_WIN],
            'BLACK_WIN': metrics[M_BLACK_WIN],
            'DRAW': metrics[M_DRAW],
            'UNKNOWN': metrics[M_STATUS_UNKNOWN],
        },
        'closure': {
            'TB': metrics[M_TB],
            'MATE_PV': metrics[M_MATE_PV],
            'MINIMAX': metrics[M_MINIMAX],
            'TERMINAL': metrics[M_TERMINAL],
            'NONE': metrics[M_CLOSURE_NONE],
        },
        'proof': {
            'ANDOR': metrics[M_ANDOR],
            'ENGINE': metrics[M_ENGINE],
            'DISPUTED': metrics[M_DISPUTED],
            'NONE': metrics[M_PROOF_NONE],
        },
    }


def _weight(node, weight):
    if weight == 'frontier':
        return node['m'][M_FRONTIER]
    if weight == 'explored':
        return node['m'][M_POSITIONS]
    return node['m'][M_NODES]


def _snapshot_work_keys(snapshot):
    """Return exact-work keys, including a safe legacy-snapshot fallback."""
    metadata = snapshot['snapshot']
    indexed = metadata.get('work_keys')
    if indexed is not None:
        return list(indexed)

    # Production can briefly serve a pre-work-index snapshot after this code
    # deploys.  The published artifact object is process-cached and immutable,
    # so cache its derived index as well instead of scanning a million-node
    # snapshot on every public API request.
    object_id = id(snapshot)
    snapshot_id = metadata.get('id')
    while True:
        with _snapshot_cache_lock:
            if (
                _legacy_work_index_cache['object_id'] == object_id
                and _legacy_work_index_cache['snapshot_id'] == snapshot_id
                and _legacy_work_index_cache['work_keys'] is not None
            ):
                return list(_legacy_work_index_cache['work_keys'])
            generation = _legacy_work_index_cache['generation']
            flight_key = (generation, object_id, snapshot_id)
            event = _legacy_work_index_cache['inflight'].get(flight_key)
            if event is None:
                event = threading.Event()
                _legacy_work_index_cache['inflight'][flight_key] = event
                break
        # Only callers deriving the same snapshot generation wait.  The scan
        # itself and unrelated snapshot generations remain outside the lock.
        event.wait()

    try:
        work_keys = tuple(
            key for key, node in snapshot['nodes'].items()
            if node['wa'] or node['wq']
        )
    except BaseException:
        with _snapshot_cache_lock:
            current = _legacy_work_index_cache['inflight'].pop(
                flight_key, None)
            if current is not None:
                current.set()
        raise

    with _snapshot_cache_lock:
        if _legacy_work_index_cache['generation'] == generation:
            _legacy_work_index_cache.update(
                object_id=object_id,
                snapshot_id=snapshot_id,
                work_keys=work_keys,
            )
        current = _legacy_work_index_cache['inflight'].pop(flight_key, None)
        if current is not None:
            current.set()
    return list(work_keys)


def _lineage_to_start(nodes, key):
    """Return display-tree keys from startpos's first child through ``key``.

    Do not materialise a complete string/list at each ancestor: doing so is
    quadratic for a direct request to a deep root even when only one mark is
    returned.
    """
    lineage = []
    seen = set()
    current = key
    while current is not None:
        if current in seen:
            raise SnapshotError('cycle in display parent lineage')
        seen.add(current)
        node = nodes[current]
        if node['r'] is not None:
            lineage.append(current)
        current = node['r']
    lineage.reverse()
    return lineage


def _san_token(node):
    move_number = (node['d'] + 1) // 2
    return (
        f'{move_number}. {node["a"]}'
        if node['d'] % 2 else node['a']
    )


def _materialise_line(nodes, lineage):
    """Materialise one complete line in one join/list pass."""
    return (
        ' '.join(_san_token(nodes[key]) for key in lineage),
        [nodes[key]['u'] for key in lineage],
    )


def _select_visible(snapshot, root_key, weight, limit, relative_depth):
    nodes = snapshot['nodes']
    selected = {root_key}
    visible_children = defaultdict(list)
    root_depth = nodes[root_key]['d']
    candidates = []

    def push_candidate(key):
        node = nodes[key]
        if (node['k']
                and node['d'] - root_depth < relative_depth):
            heapq.heappush(
                candidates,
                (-_weight(node, weight), node['d'], key),
            )

    push_candidate(root_key)
    while candidates and len(selected) < limit:
        _negative_weight, _depth, parent_key = heapq.heappop(candidates)
        children = nodes[parent_key]['k']
        remaining = limit - len(selected)
        if remaining <= 0:
            break
        ordered = sorted(
            children,
            key=lambda key: (
                -_weight(nodes[key], weight),
                nodes[key]['u'] or '',
                key,
            ),
        )
        chosen = ordered[:remaining]
        visible_children[parent_key] = chosen
        for child_key in chosen:
            selected.add(child_key)
            push_candidate(child_key)
    return selected, visible_children


def render_map(snapshot, root_key, weight='frontier', limit=DEFAULT_MARKS,
               relative_depth=DEFAULT_DEPTH):
    """Expand a bounded hierarchical API document from one flat snapshot."""
    nodes = snapshot['nodes']
    if root_key not in nodes:
        raise KeyError(root_key)
    selected, visible_children = _select_visible(
        snapshot, root_key, weight, limit, relative_depth,
    )
    # One O(depth) base reconstruction for a direct deep zoom. Descendants
    # append only their visible suffix; all subsequent copying corresponds to
    # bytes/lists that the response contract actually emits.
    root_lineage = _lineage_to_start(nodes, root_key)
    root_line_san, root_line_uci = _materialise_line(nodes, root_lineage)
    metadata = snapshot['snapshot']
    start_key = metadata['start_key']
    lineage_keys = [start_key] + root_lineage

    def compact_opening(exact_match, *, exact=True, matched_ply=None):
        if exact_match is None:
            return None
        return {
            'name': exact_match['name'],
            'exact': bool(exact),
            'matched_ply': (
                exact_match['matched_ply']
                if matched_ply is None else matched_ply
            ),
        }

    def compact_summary(key, opening=None):
        node = nodes[key]
        active = node['m'][M_ACTIVE]
        queued = node['m'][M_QUEUED]
        own_active = node['wa']
        own_queued = node['wq']
        descendant_active = max(0, active - own_active)
        descendant_queued = max(0, queued - own_queued)
        summary = {
            'key': key,
            'fen': node['f'],
            'status': node['s'],
            'closure': node['c'],
            'proof': node['p'],
            'eval_cp': node['e'],
            'depth': node['d'],
            'move': (
                None if node['r'] is None
                else {'uci': node['u'], 'san': node['a']}
            ),
            'metrics': _expanded_metrics(node['m']),
            'work': {
                # ``state``, ``active`` and ``queued`` retain their v1
                # subtree semantics for existing clients.
                'state': (
                    'active' if active else ('queued' if queued else 'idle')
                ),
                'active': active,
                'queued': queued,
                'own_active': own_active,
                'own_queued': own_queued,
                'exact_state': (
                    'active' if own_active
                    else ('queued' if own_queued else 'idle')
                ),
                'subtree_active': active,
                'subtree_queued': queued,
                'descendant_active': descendant_active,
                'descendant_queued': descendant_queued,
            },
            'transpositions': {
                'incoming': node['i'],
                'alternate_parents': node['x'],
            },
            'weight': _weight(node, weight),
            'zoomable': bool(node['k']),
        }
        if opening is not None:
            summary['opening'] = opening
        return summary

    def opening_for_lineage(lineage):
        current = None
        final_key = lineage[-1] if lineage else None
        for lineage_key in lineage:
            exact_match = openings.lookup_key(lineage_key)
            if exact_match is not None:
                current = compact_opening(
                    exact_match,
                    exact=lineage_key == final_key,
                    matched_ply=nodes[lineage_key]['d'],
                )
        return current

    def inherited_opening(opening):
        if opening is None:
            return None
        return {
            'name': opening['name'],
            'exact': False,
            'matched_ply': opening['matched_ply'],
        }

    def render_node(key, line_san, line_uci, ancestor_opening=None):
        node = nodes[key]
        children = visible_children.get(key, ())
        all_children = node['k']
        exact_match = openings.lookup_key(key)
        current_opening = (
            compact_opening(
                exact_match, exact=True, matched_ply=node['d'])
            if exact_match is not None
            else inherited_opening(ancestor_opening)
        )

        rendered_children = []
        for child_key in children:
            child = nodes[child_key]
            token = _san_token(child)
            child_line_san = (
                f'{line_san} {token}' if line_san else token
            )
            child_line_uci = line_uci + [child['u']]
            rendered_children.append(render_node(
                child_key, child_line_san, child_line_uci, current_opening))
        rendered = compact_summary(key, current_opening)
        rendered.update({
            'best_move': node['b'],
            'depth_invested': node['di'],
            'visits': node['v'],
            'expanded': node['ex'],
            'line_san': line_san,
            'line_uci': line_uci,
            'truncated': len(children) < len(all_children),
            'hidden_children': len(all_children) - len(children),
            'children': rendered_children,
        })
        return rendered

    root_opening = opening_for_lineage(lineage_keys)
    exact_work_keys = _snapshot_work_keys(snapshot)
    ordered_work_keys = heapq.nsmallest(
        MAX_WORK_ITEMS,
        exact_work_keys,
        key=lambda key: (
            -int(bool(nodes[key]['wa'])),
            -nodes[key]['wa'],
            -nodes[key]['wq'],
            nodes[key]['d'],
            key,
        ),
    )
    work_items = []
    for key in ordered_work_keys:
        lineage = _lineage_to_start(nodes, key)
        line_san, line_uci = _materialise_line(nodes, lineage)
        opening = opening_for_lineage([start_key] + lineage)
        item = compact_summary(key, opening)
        item.update({
            'line_san': line_san,
            'line_uci': line_uci,
        })
        work_items.append(item)

    document = {
        'schema': API_SCHEMA,
        'snapshot': {
            'id': metadata['id'],
            'generated_at': metadata['generated_at'],
            'root_key': metadata['root_key'],
            'start_key': metadata['start_key'],
            'positions': metadata['positions'],
            'edges': metadata['edges'],
            'max_depth': metadata['max_depth'],
            'opening_catalog_sha256': openings.catalog_sha256(),
        },
        'semantics': {
            'tier': 'practical-tier-1',
            'pov': 'white',
            'weight': weight,
            'display_tree': (
                'minimum startpos depth; one stable minimum-depth parent; '
                'otherwise lowest mover regret, UCI, then parent key'
            ),
            'frontier': (
                'startpos-reachable UNKNOWN positions whose scheduler '
                'priority is above the historical tombstone boundary'
            ),
            'status': 'exact practical closure; never inferred from eval',
            'eval': 'heuristic White-POV centipawns; never proof-closing',
            'transpositions': (
                'alternate reachable incoming edges not used as display parent'
            ),
            'opening': (
                'last exact position-key match on the startpos lineage; '
                'unnamed continuations retain that label until replaced'
            ),
            'work': (
                'state/active/queued are subtree-compatible v1 fields; '
                'exact_state and own_* describe this exact position; '
                'descendant_* exclude this exact position'
            ),
        },
        'request': {
            'root': root_key,
            'weight': weight,
            'limit': limit,
            'depth': relative_depth,
        },
        'marks': len(selected),
        'zoomable': bool(nodes[root_key]['k']),
        'work_items': work_items,
        'work_items_total': len(exact_work_keys),
        'work_items_truncated': len(exact_work_keys) > MAX_WORK_ITEMS,
        'lineage': {
            'start_key': start_key,
            'root_key': root_key,
            'line_san': root_line_san,
            'line_uci': root_line_uci,
            'positions': [
                {
                    'key': key,
                    'depth': nodes[key]['d'],
                    'move': (
                        None if nodes[key]['r'] is None else {
                            'uci': nodes[key]['u'],
                            'san': nodes[key]['a'],
                        }
                    ),
                }
                for key in lineage_keys
            ],
        },
        'first_moves': [
            compact_summary(
                key,
                compact_opening(
                    openings.lookup_key(key),
                    exact=True,
                    matched_ply=nodes[key]['d'],
                ),
            )
            for key in nodes[start_key]['k']
        ],
        'root': render_node(
            root_key, root_line_san, root_line_uci, root_opening),
    }
    return document


def _query_integer(request, name, default, minimum, maximum):
    raw = request.GET.get(name)
    if raw in (None, ''):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f'{name} must be an integer') from exc
    if not minimum <= value <= maximum:
        raise ValueError(f'{name} must be between {minimum} and {maximum}')
    return value


def _error(code, message, status):
    return JsonResponse(
        {'schema': ERROR_SCHEMA, 'error': {'code': code, 'message': message}},
        status=status,
    )


def _accepts_gzip(header):
    for part in (header or '').lower().split(','):
        bits = [bit.strip() for bit in part.split(';')]
        if bits[0] != 'gzip':
            continue
        for parameter in bits[1:]:
            if parameter.startswith('q='):
                try:
                    return float(parameter[2:]) > 0
                except ValueError:
                    return False
        return True
    return False


def _etag_matches(header, etag):
    values = [value.strip() for value in (header or '').split(',')]
    weak_etag = etag[2:] if etag.startswith('W/') else etag
    return '*' in values or any(
        (value[2:] if value.startswith('W/') else value) == weak_etag
        for value in values
    )


def _set_cache_headers(response, etag):
    response['ETag'] = etag
    response['Cache-Control'] = 'public, max-age=60, stale-while-revalidate=300'
    response['Vary'] = 'Accept-Encoding'


def map_api(request):
    """Serve only a previously authenticated snapshot; never query live DB."""
    if request.method not in ('GET', 'HEAD'):
        return _error('method_not_allowed', 'GET required', 405)
    unknown = set(request.GET) - {'root', 'weight', 'limit', 'depth'}
    if unknown:
        return _error(
            'invalid_query',
            f'unknown query parameter: {sorted(unknown)[0]}',
            400,
        )
    weight = request.GET.get('weight', 'frontier')
    if weight not in WEIGHTS:
        return _error(
            'invalid_weight',
            'weight must be frontier, explored or compute',
            400,
        )
    try:
        limit = _query_integer(request, 'limit', DEFAULT_MARKS, 1, MAX_MARKS)
        depth = _query_integer(request, 'depth', DEFAULT_DEPTH, 1, MAX_DEPTH)
    except ValueError as exc:
        return _error('invalid_query', str(exc), 400)
    requested_root = request.GET.get('root')
    if requested_root is not None:
        requested_root = requested_root.lower()
        if not HEX_KEY.fullmatch(requested_root):
            return _error('invalid_root', 'root must be a 64-hex key', 400)

    try:
        snapshot = published_snapshot()
    except SnapshotError as exc:
        logger.warning('Atomic move-tree snapshot unavailable or corrupt: %s', exc)
        response = _error(
            'snapshot_unavailable',
            'Atomic move-tree snapshot is temporarily unavailable',
            503,
        )
        response['Cache-Control'] = 'no-store'
        return response
    root_key = requested_root or snapshot['snapshot']['root_key']
    if root_key not in snapshot['nodes']:
        return _error('root_not_found', 'root is not startpos-reachable', 404)
    try:
        document = render_map(snapshot, root_key, weight, limit, depth)
        raw = _canonical_json(document)
    except (KeyError, SnapshotError, RecursionError, ValueError):
        logger.exception('Atomic move-tree snapshot cannot be rendered')
        response = _error(
            'snapshot_unavailable',
            'Atomic move-tree snapshot is temporarily unavailable',
            503,
        )
        response['Cache-Control'] = 'no-store'
        return response
    if len(raw) > MAX_API_BYTES:
        logger.error('Atomic move-tree response exceeded %d bytes', MAX_API_BYTES)
        response = _error(
            'payload_budget_exceeded',
            'Atomic move-tree response exceeds the payload budget',
            503,
        )
        response['Cache-Control'] = 'no-store'
        return response

    representation = hashlib.sha256(raw).hexdigest()
    etag = f'W/"{representation}"'
    if _etag_matches(request.headers.get('If-None-Match'), etag):
        response = HttpResponse(status=304)
        _set_cache_headers(response, etag)
        return response

    use_gzip = _accepts_gzip(request.headers.get('Accept-Encoding'))
    representation_body = (
        gzip.compress(raw, compresslevel=6, mtime=0) if use_gzip else raw
    )
    response = HttpResponse(
        representation_body, content_type='application/json; charset=utf-8')
    if use_gzip:
        response['Content-Encoding'] = 'gzip'
    response['Content-Length'] = str(len(representation_body))
    _set_cache_headers(response, etag)
    return response
