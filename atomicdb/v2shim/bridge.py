"""Export rows to v2, call a v2 entry point, write its changes back.

One call (``call_v2``):

1. export every ``Position`` and ``Edge`` row of the AtomicDB database (a
   test database holds a few hundred at most), plus the extra tables the
   entry declares (``av2.entry_tables``);
2. encode the call's arguments as JSON (positions as ``{"$pos": key}``,
   other model instances as ``{"$model": ..., "pk": ...}``, sets, tuples,
   datetimes tagged);
3. ``av2.call(name, request)``: Rust loads the rows into a fresh in-memory
   v2 graph (``ShimGraph``), runs the entry, and replies with the columns it
   changed, the positions and edges it created, the facts it emitted and the
   rows it wants written to other tables;
4. write those back with plain ORM updates (``updated`` is stamped on every
   position row v2 changed, as v1's writers do), refresh the ``Position``
   instances the test passed in, and decode the result.

A v1 value the v2 records cannot hold fails the call with ``V2Error``
(``unrepresentable``) instead of running on a rounded value.
"""

import datetime
import decimal
import importlib
import json
import os
import re
import sys
import time
import uuid

from django.apps import apps
from django.db import models, transaction
from django.utils import timezone

PY_DIR_ENV = 'AV2_PY_DIR'
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PY_DIR = os.path.join(_REPO, 'atomicdb-v2', 'target', 'av2py')

_av2 = {'module': None, 'error': None}


class V2Error(RuntimeError):
    """A v2 call that failed (``code``: not_ported, bad_args, rules, graph,
    unrepresentable)."""

    def __init__(self, name, code, message, issues=None):
        super().__init__(f'{name}: {code}: {message}')
        self.name, self.code, self.message, self.issues = name, code, message, issues or []


def av2():
    """The ``av2`` extension module, or raise ImportError saying how to build it."""
    if _av2['module'] is not None:
        return _av2['module']
    folder = os.environ.get(PY_DIR_ENV) or DEFAULT_PY_DIR
    if folder not in sys.path:
        sys.path.insert(0, folder)
    try:
        import av2 as module  # noqa: PLC0415 - built on demand, not a dependency
    except ImportError as error:
        raise ImportError(
            f'the av2 extension is not built ({error}); run '
            f'`python atomicdb-v2/scripts/build_av2_py.py` or set {PY_DIR_ENV}') from error
    _av2['module'] = module
    return module


def available():
    try:
        av2()
        return True
    except ImportError:
        return False


def ported():
    """v1 names of the ported functions (the shim's own ``shim.*`` excluded)."""
    return {name for name in av2().entries() if not name.startswith('shim.')}


# ---------------------------------------------------------------- export

def _alias():
    from ..database import current_alias
    return current_alias()


def _models():
    from ..models import DBEvent, Edge, Position
    return Position, Edge, DBEvent


def position_columns():
    """``key`` plus the columns v2 models (the Rust side's list)."""
    return ['key'] + list(av2().columns())


def export_graph(alias=None):
    alias = alias or _alias()
    Position, Edge, _ = _models()
    columns = position_columns()
    positions = list(Position.objects.using(alias).order_by('key').values(*columns))
    edges = [{'id': e['id'], 'parent': e['parent_id'], 'move': e['move_uci'],
              'child': e['child_id']}
             for e in Edge.objects.using(alias).order_by('id').values(
                 'id', 'parent_id', 'move_uci', 'child_id')]
    return positions, edges


def _model_for_table(table):
    for model in apps.get_app_config('atomicdb').get_models():
        if model._meta.db_table == table:
            return model
    raise LookupError(f'no atomicdb model for table {table}')


# The modules whose UPPER_CASE constants the rules read (the same list the
# golden-case recorder captures, so entries read both the same way).
CONSTANT_MODULES = ('ingest', 'propagation_queue', 'ingest_queue', 'proof')
SECRET_WORDS = ('PASSWORD', 'SECRET', 'TOKEN', 'KEY', 'CREDENTIAL')


def export_constants():
    """``{module: {NAME: value}}`` as the modules hold them now (a constant a
    test patched with ``mock.patch`` is sent patched)."""
    out = {}
    for name in CONSTANT_MODULES:
        module = importlib.import_module(f'atomicdb.{name}')
        out[name] = {attr: list(v) if isinstance(v, (tuple, frozenset)) else v
                     for attr, v in vars(module).items()
                     if attr.isupper() and not attr.startswith('_')
                     and isinstance(v, (bool, int, float, str, tuple, frozenset))}
    return out


def export_settings():
    from django.conf import settings
    return {name: getattr(settings, name) for name in dir(settings)
            if name.startswith('ATOMICDB_') and not any(w in name for w in SECRET_WORDS)}


def _prepared(name, args, kwargs, alias):
    from .prepare import PREPARE
    hook = PREPARE.get(name)
    return hook(args, kwargs, alias) if hook is not None else {}


def export_tables(tables, alias=None):
    alias = alias or _alias()
    return {table: list(_model_for_table(table).objects.using(alias).values())
            for table in tables}


# ---------------------------------------------------------------- values

def encode(value):
    """A call argument as the Rust side reads it (``registry::Args``)."""
    Position = _models()[0]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            return {'$float': repr(value)}
        return value
    if isinstance(value, Position):
        return {'$pos': value.pk}
    if isinstance(value, models.Model):
        fields = {}
        for field in value._meta.concrete_fields:
            fields[field.attname] = encode(getattr(value, field.attname))
        return {'$model': value._meta.label, 'pk': encode(value.pk), 'fields': fields}
    if isinstance(value, models.QuerySet):
        return [encode(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return {'$set': [encode(v) for v in sorted(value, key=repr)]}
    if isinstance(value, tuple):
        return {'$tuple': [encode(v) for v in value]}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k): encode(v) for k, v in value.items()}
    if isinstance(value, (datetime.datetime, datetime.date)):
        return {'$datetime': value.isoformat()}
    if isinstance(value, datetime.timedelta):
        return {'$seconds': value.total_seconds()}
    if isinstance(value, (decimal.Decimal, uuid.UUID)):
        return str(value)
    return {'$repr': repr(value)}


def decode(value, alias=None):
    """A result as Python: ``$pos`` -> Position, ``$set`` -> set, ``$tuple`` -> tuple."""
    if isinstance(value, list):
        return [decode(v, alias) for v in value]
    if isinstance(value, dict):
        if '$pos' in value and len(value) == 1:
            Position = _models()[0]
            return Position.objects.using(alias or _alias()).get(pk=value['$pos'])
        if '$set' in value and len(value) == 1:
            return {_hashable(decode(v, alias)) for v in value['$set']}
        if '$tuple' in value and len(value) == 1:
            return tuple(decode(v, alias) for v in value['$tuple'])
        if '$float' in value and len(value) == 1:
            return float(value['$float'])
        return {k: decode(v, alias) for k, v in value.items()}
    return value


def _hashable(v):
    return tuple(v) if isinstance(v, list) else v


def _json_default(value):
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, (decimal.Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return repr(value)


# ---------------------------------------------------------------- write back

def _instances(value, out):
    """Every Position instance inside the call's arguments."""
    Position = _models()[0]
    if isinstance(value, Position):
        out.setdefault(value.pk, []).append(value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            _instances(v, out)
    elif isinstance(value, dict):
        for v in value.values():
            _instances(v, out)
    return out


def _project_fact(fact, alias):
    """Facts that v1 keeps as rows.  An opaque fact whose kind is a
    ``DBEvent`` kind (upper case, as ``av2_rules::effects::Event::to_fact``
    writes them: ``NODE_CLOSED``, ``CASCADE_GUARD``...) becomes that
    ``DBEvent`` with the fact's data as payload."""
    _, _, DBEvent = _models()
    if fact.get('kind') != 'opaque':
        return False
    body = fact.get('fact') or {}
    kind = body.get('kind') or ''
    if not kind or kind != kind.upper():
        return False
    data = body.get('data')
    DBEvent.objects.using(alias).create(kind=kind, payload=data if data is not None else {})
    return True


def _write_table(table, rows, alias):
    model = _model_for_table(table)
    for row in rows if isinstance(rows, list) else [rows]:
        row = dict(row)
        pk = row.pop(model._meta.pk.attname, None)
        if pk is None:
            model.objects.using(alias).create(**row)
        else:
            model.objects.using(alias).update_or_create(pk=pk, defaults=row)


# Per-entry write-back of ``writes`` (rows for tables outside the graph):
# ``WRITEBACK[v1_name](writes, alias)``.  Without one, each table's rows are
# created (no pk) or updated by pk.
WRITEBACK = {}


def write_back(name, reply, args, kwargs, alias):
    Position, Edge, _ = _models()
    now = timezone.now()
    with transaction.atomic(using=alias):
        for key, cols in reply.get('updated', {}).items():
            Position.objects.using(alias).filter(pk=key).update(updated=now, **cols)
        created = [Position(**row) for row in reply.get('created', [])]
        if created:
            Position.objects.using(alias).bulk_create(created)
        edges = [Edge(parent_id=e['parent'], move_uci=e['move'], child_id=e['child'])
                 for e in reply.get('edges', [])]
        if edges:
            Edge.objects.using(alias).bulk_create(edges)
        unprojected = [f for f in reply.get('facts', []) if not _project_fact(f, alias)]
        writes = dict(reply.get('writes') or {})
        out = writes.pop('$out', None) or {}
        calls = writes.pop('$calls', None) or []
        if writes:
            hook = WRITEBACK.get(name)
            if hook is not None:
                hook(writes, alias)
            else:
                for table, rows in writes.items():
                    _write_table(table, rows, alias)
    # The instances the test holds see what v2 wrote, as they would have
    # seen v1's in-place assignments.
    for key, objs in _instances((args, kwargs), {}).items():
        cols = reply.get('updated', {}).get(key)
        if cols:
            for obj in objs:
                for col, value in cols.items():
                    setattr(obj, col, value)
                obj.updated = now
    # Out-parameters: v1 functions that fill a dict the caller passed
    # (``report=``).  ``{"report": {...}}`` names a keyword, ``{"0": ...}``
    # a position.
    for target_name, value in out.items():
        target = (args[int(target_name)] if target_name.isdigit() and int(target_name) < len(args)
                  else kwargs.get(target_name))
        value = decode(value, alias)
        if isinstance(target, dict) and isinstance(value, dict):
            target.update(value)
        elif isinstance(target, list) and isinstance(value, list):
            target[:] = value
    # Follow-up calls an entry asks for (a v1 side effect another port
    # owns, e.g. the proof-number refresh at the end of a cascade): made
    # through the module attribute, so they go to v2 once that is ported.
    for call in calls:
        module_name, _, attr = call['name'].rpartition('.')
        fn = getattr(importlib.import_module(f'atomicdb.{module_name}'), attr)
        fn(*decode(call.get('args') or [], alias), **decode(call.get('kwargs') or {}, alias))
    return unprojected


# ---------------------------------------------------------------- moves

_UCI = re.compile(r'^[a-h][1-8][a-h][1-8][nbrqk]?$')
MOVE_COLUMNS = ('best_move', 'backed_move')


def _is_uci(move):
    return bool(_UCI.match(move)) and move[:2] != move[2:4]


class SyntheticMoves:
    """Test fixtures name moves freely (``'wb'``, ``'x1x1'``); v2 holds a
    move in 16 bits and only takes UCI on 8x8.  For one call, every move
    string of the exported rows that is not UCI gets a stand-in UCI move
    that no real edge of the call uses, assigned in sorted order of the
    names (so ties broken by move name among them keep their order), and is
    turned back on the way out.  The per-test report counts the calls that
    needed it (``synthetic_moves``)."""

    def __init__(self, positions, edges):
        real, names = set(), set()
        for e in edges:
            (real if _is_uci(e['move']) else names).add(e['move'])
        for row in positions:
            for col in MOVE_COLUMNS:
                value = row.get(col)
                if value is not None:
                    (real if _is_uci(value) else names).add(value)
        pool = (f'{a}{b}{c}{d}' for a in 'abcdefgh' for b in '12345678'
                for c in 'abcdefgh' for d in '12345678' if (a, b) != (c, d))
        self.fwd = {}
        for name in sorted(names):
            stand_in = next(m for m in pool if m not in real)
            self.fwd[name] = stand_in
        self.rev = {v: k for k, v in self.fwd.items()}
        if self.fwd:
            for e in edges:
                e['move'] = self.fwd.get(e['move'], e['move'])
            for row in positions:
                for col in MOVE_COLUMNS:
                    if row.get(col) is not None:
                        row[col] = self.fwd.get(row[col], row[col])

    def _map(self, value, table):
        if not table:
            return value
        if isinstance(value, str):
            return table.get(value, value)
        if isinstance(value, list):
            return [self._map(v, table) for v in value]
        if isinstance(value, tuple):
            return tuple(self._map(v, table) for v in value)
        if isinstance(value, dict):
            return {k: self._map(v, table) for k, v in value.items()}
        return value

    def forward(self, value):
        return self._map(value, self.fwd)

    def back(self, value):
        return self._map(value, self.rev)


# ---------------------------------------------------------------- the call

class Stats:
    """What the current test sent to v2 (reset by the runner per test)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.v2_calls = {}
        self.v1_calls = {}
        self.v2_errors = []
        self.v2_seconds = 0.0
        self.unprojected_facts = 0
        self.synthetic_moves = 0

    def as_dict(self):
        return {'v2_calls': dict(self.v2_calls), 'v1_calls': dict(self.v1_calls),
                'v2_errors': list(self.v2_errors),
                'v2_ms': round(self.v2_seconds * 1000, 3),
                'unprojected_facts': self.unprojected_facts,
                'synthetic_moves': self.synthetic_moves}


STATS = Stats()


def call_v2(name, args=(), kwargs=None, alias=None):
    """Run the v2 entry ``name`` on the current database's rows."""
    kwargs = kwargs or {}
    original_args, original_kwargs = args, kwargs
    alias = alias or _alias()
    module = av2()
    started = time.perf_counter()
    positions, edges = export_graph(alias)
    moves = SyntheticMoves(positions, edges)
    args, kwargs = moves.forward(tuple(args)), moves.forward(dict(kwargs))
    STATS.synthetic_moves += len(moves.fwd)
    request = {
        'positions': positions,
        'edges': edges,
        'tables': export_tables(module.entry_tables(name), alias),
        'constants': export_constants(),
        'settings': export_settings(),
        'prepared': _prepared(name, args, kwargs, alias),
        'args': {'args': [encode(a) for a in args],
                 'kwargs': {k: encode(v) for k, v in kwargs.items()}},
    }
    reply = json.loads(module.call(name, json.dumps(request, default=_json_default)))
    try:
        if not reply.get('ok'):
            error = V2Error(name, reply.get('error'), reply.get('message'),
                            reply.get('issues'))
            STATS.v2_errors.append({'name': name, 'code': error.code,
                                    'message': (error.message or '')[:500]})
            raise error
        reply = moves.back(reply)
        args, kwargs = moves.back(args), moves.back(kwargs)
        STATS.unprojected_facts += len(write_back(name, reply, original_args, original_kwargs, alias))
        return decode(reply.get('result'), alias)
    finally:
        STATS.v2_seconds += time.perf_counter() - started
