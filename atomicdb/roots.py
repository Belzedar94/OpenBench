"""The start position and its first moves, over time.

Community request: keep the best and the worst value the start position and
each of its first moves have ever had, and log how the main line from the
start position evolves.  Two records feed the public page ``/atomicdb/roots/``:

* ``RootExtreme``: per watched position, the highest and the lowest headline
  value since tracking began, and when.  On PostgreSQL a row trigger keeps it
  (migration 0050), so every change counts, the cascade's ``bulk_update``
  included.  Without the trigger, ``fold_extremes`` folds the current values
  in at the hourly capture.
* ``MainLineSnapshot``: the main line once per UTC hour, written by
  ``capture_atomicdb_progress`` beside its own row.

The value is always the one the explorer headlines (``ingest.known_eval_of``),
from White's side: a proven status as +10000, -10000 or 0, then the backed
value, then the point eval.
"""

import functools
from datetime import timedelta, timezone as datetime_timezone

from django.shortcuts import render
from django.utils import timezone

from . import ingest, logic
from .database import atomic, connection
from .models import Edge, MainLineSnapshot, Position, RootExtreme

UTC = datetime_timezone.utc

# The name migration 0050 gives the trigger and its function.
TRIGGER = 'atomicdb_root_extremes'
# The main line is followed this far, like the query it was specified with.
MAIN_LINE_MAX_PLIES = 120
# The explorer replays a ``play`` route of at most this many plies
# (``views.PLAY_ROUTE_MAX_PLIES``; a test holds the two equal).  A longer line
# links to its last position without the route.
PLAY_LINK_MAX_PLIES = 64
# Days of the main-line history the page shows, one row per day.
HISTORY_DAYS = 30
# Plies of each history row shown before the full line goes to the tooltip.
HISTORY_PREVIEW_PLIES = 12

_LINE_FIELDS = ('key', 'status', 'backed_eval', 'eval_cp', 'backed_move',
                'best_move')
_PROVEN = {'WHITE_WIN': 'White win', 'BLACK_WIN': 'Black win',
           'DRAW': 'Draw'}
_MINUS = '−'
_NONE = '—'
_NBSP = chr(0xA0)


@functools.lru_cache(maxsize=1)
def watched_positions():
    """``(uci, san, key)``: the start position (blank move) and each first move.

    Computed with the move generator, so it is the same canonical identity the
    tree uses.  Migration 0050 spells the same 21 keys out for the trigger.
    """
    import pyffish as pf

    start = logic.start_fen()
    rows = [('', '', logic.key_of(start))]
    for uci in sorted(logic.legal_moves(start)):
        rows.append((uci, pf.get_san(logic.VARIANT, start, uci),
                     logic.key_of(logic.apply_move(start, uci))))
    return tuple(rows)


def watched_keys():
    return [key for _uci, _san, key in watched_positions()]


def value_of(status, backed_eval, eval_cp):
    """The headline value, White's side.  The one precedence of the site."""
    return ingest.known_eval_of(status, backed_eval, eval_cp)


def value_text(value, status='UNKNOWN'):
    """``+12.43`` in pawns from White's side; a proven status by its name."""
    label = _PROVEN.get(status)
    if label is not None:
        return label
    if value is None:
        return _NONE
    sign = '+' if value > 0 else (_MINUS if value < 0 else '')
    return f'{sign}{abs(value) / 100:.2f}'


# --------------------------------------------------------------- extremes ---

def trigger_installed():
    """Is the trigger of migration 0050 there, and enabled?"""
    if connection.vendor != 'postgresql':
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_trigger WHERE tgname = %s"
            " AND tgrelid = 'atomicdb_position'::regclass"
            " AND tgenabled <> 'D'", [TRIGGER])
        return cursor.fetchone() is not None


def observe(row, value, status, at):
    """Fold one value into ``row`` in place; ``True`` if it was a change.

    The same rules as the trigger function of migration 0050: a rewrite of
    the value and status already recorded changes nothing, and best and worst
    move only on a strict improvement, so they keep the moment the value was
    first reached.
    """
    if row.last_value == value and row.last_status == status:
        return False
    if value > row.best_value:
        row.best_value, row.best_status, row.best_at = value, status, at
    if value < row.worst_value:
        row.worst_value, row.worst_status, row.worst_at = value, status, at
    row.last_value, row.last_status, row.changed_at = value, status, at
    row.changes += 1
    return True


def start_row(key, value, status, at):
    """A row's first value: it starts the row and is not a change."""
    return RootExtreme(
        key=key, best_value=value, best_status=status, best_at=at,
        worst_value=value, worst_status=status, worst_at=at,
        last_value=value, last_status=status, changed_at=at, changes=0,
        since=at)


def fold_extremes(now=None):
    """Fold the current values of the watched positions in; ``(new, changed)``.

    For a database without the trigger, where this runs once an hour: a value
    that came and went between two runs is not seen.  Where the trigger is
    installed it is the trigger's job and this is not called.
    """
    at = now or timezone.now()
    keys = watched_keys()
    created = changed = 0
    with atomic():
        rows = {row.key: row for row in
                RootExtreme.objects.select_for_update().filter(key__in=keys)}
        for key, status, backed_eval, eval_cp in (
                Position.objects.filter(key__in=keys)
                .values_list('key', 'status', 'backed_eval', 'eval_cp')):
            value = value_of(status, backed_eval, eval_cp)
            if value is None:
                continue
            row = rows.get(key)
            if row is None:
                start_row(key, value, status, at).save(force_insert=True)
                created += 1
            elif observe(row, value, status, at):
                row.save()
                changed += 1
    return created, changed


# -------------------------------------------------------------- main line ---

def _san(moves):
    """SAN of the whole line in one call, or ``''`` if it cannot be replayed."""
    if not moves:
        return ''
    import pyffish as pf

    try:
        sans = list(pf.get_san_moves(logic.VARIANT, logic.start_fen(), moves))
    except Exception:
        return ''
    return ' '.join(sans) if len(sans) == len(moves) else ''


def walk_main_line(max_plies=MAIN_LINE_MAX_PLIES):
    """Follow ``backed_move``, else ``best_move``, from the start position.

    One edge lookup per ply.  Stops at a proven position (which is part of
    the line), at ``max_plies``, at a position naming no move, at a move with
    no edge, or before a position the line already went through.  ``None``
    when the tree has no start position.
    """
    End = MainLineSnapshot.End
    node = (Position.objects.filter(key=ingest.root_key())
            .values(*_LINE_FIELDS).first())
    if node is None:
        return None
    root = node
    seen = {node['key']}
    moves = []
    child_fields = [f'child__{field}' for field in _LINE_FIELDS]
    while True:
        if node['status'] != 'UNKNOWN':
            end = End.PROVEN
            break
        if len(moves) >= max_plies:
            end = End.LIMIT
            break
        move = (node['backed_move'] if node['backed_move'] is not None
                else node['best_move'])
        if not move:
            end = End.NO_MOVE
            break
        row = (Edge.objects.filter(parent_id=node['key'], move_uci=move)
               .values_list(*child_fields).first())
        if row is None:
            end = End.NO_EDGE
            break
        child = dict(zip(_LINE_FIELDS, row))
        if child['key'] in seen:
            end = End.REPEAT
            break
        seen.add(child['key'])
        moves.append(move)
        node = child
    return {
        'value': value_of(root['status'], root['backed_eval'],
                          root['eval_cp']),
        'status': root['status'],
        'plies': len(moves),
        'moves': ' '.join(moves),
        'san': _san(moves),
        'end_key': node['key'],
        'end_value': value_of(node['status'], node['backed_eval'],
                              node['eval_cp']),
        'end_status': node['status'],
        'end_reason': end,
    }


def _utc_hour(value):
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def capture_main_line(*, now=None):
    """Record this UTC hour's main line once; ``(snapshot, created)``.

    An hour already recorded is returned untouched.  ``(None, False)`` when
    the tree has no start position.
    """
    bucket = _utc_hour(now or timezone.now())
    existing = MainLineSnapshot.objects.filter(bucket_start=bucket).first()
    if existing is not None:
        return existing, False
    line = walk_main_line()
    if line is None:
        return None, False
    with atomic():
        return MainLineSnapshot.objects.get_or_create(
            bucket_start=bucket, defaults=line)


def capture(*, now=None):
    """The hourly part of the capture command: the main line, and the
    extremes where no trigger keeps them."""
    now = now or timezone.now()
    snapshot, created = capture_main_line(now=now)
    folded = None if trigger_installed() else fold_extremes(now=now)
    return snapshot, created, folded


# ------------------------------------------------------------------- page ---

def numbered(tokens):
    """``1. Nf3 f6 2. Nc3``: the line starts at the start position."""
    parts = []
    for index, token in enumerate(tokens):
        parts.append(f'{index // 2 + 1}. {token}' if index % 2 == 0
                     else token)
    return ' '.join(parts)


def _tokens(snapshot):
    return (snapshot.san or snapshot.moves).split()


def _end_text(snapshot):
    End = MainLineSnapshot.End
    if snapshot.end_reason == End.PROVEN:
        return {'WHITE_WIN': 'ends in a position solved as a White win',
                'BLACK_WIN': 'ends in a position solved as a Black win',
                'DRAW': 'ends in a position solved as a draw',
                }.get(snapshot.end_status, 'ends in a solved position')
    return {
        End.LIMIT: f'stops at {MAIN_LINE_MAX_PLIES} plies, the length '
                   'recorded',
        End.NO_MOVE: 'ends at a position not analysed yet',
        End.NO_EDGE: 'stops where the next move is not in the tree yet',
        End.REPEAT: 'stops where the next move would repeat a position',
    }.get(snapshot.end_reason, '')


def _line_context(snapshot):
    tokens = _tokens(snapshot)
    moves = snapshot.moves.split()
    url = f'/atomicdb/explore/{snapshot.end_key}/'
    if moves and len(moves) <= PLAY_LINK_MAX_PLIES:
        url += '?play=' + ','.join(moves)
    return {
        'at': snapshot.bucket_start,
        'value_h': value_text(snapshot.value, snapshot.status),
        'plies': snapshot.plies,
        'text': numbered(tokens),
        'end_text': _end_text(snapshot),
        'end_url': url if snapshot.end_key else '',
    }


def _daily_history(now, days=HISTORY_DAYS):
    """The last recording of each UTC day, newest day first.

    Two reads: the dates of the window, then only the rows that are shown,
    so the lines of the hours in between are never fetched.
    """
    last_of_day = {}
    for pk, bucket in (MainLineSnapshot.objects
                       .filter(bucket_start__gte=now - timedelta(days=days))
                       .order_by('bucket_start')
                       .values_list('pk', 'bucket_start')):
        last_of_day[bucket.astimezone(UTC).date()] = pk
    shown = MainLineSnapshot.objects.in_bulk(list(last_of_day.values()))
    rows = []
    for day in sorted(last_of_day, reverse=True):
        snapshot = shown[last_of_day[day]]
        tokens = _tokens(snapshot)
        preview = numbered(tokens[:HISTORY_PREVIEW_PLIES])
        if len(tokens) > HISTORY_PREVIEW_PLIES:
            preview += _NBSP + '…'      # the ellipsis never wraps alone
        rows.append({'day': day,
                     'value_h': value_text(snapshot.value, snapshot.status),
                     'plies': snapshot.plies,
                     'preview': preview,
                     'full': numbered(tokens)})
    return rows


def page_context(now=None):
    now = now or timezone.now()
    watched = watched_positions()
    keys = [key for _uci, _san, key in watched]
    current = {key: (status, backed_eval, eval_cp)
               for key, status, backed_eval, eval_cp in
               Position.objects.filter(key__in=keys)
               .values_list('key', 'status', 'backed_eval', 'eval_cp')}
    extremes = RootExtreme.objects.in_bulk(keys)
    start, moves = None, []
    for uci, san, key in watched:
        status, backed_eval, eval_cp = current.get(key, ('UNKNOWN', None, None))
        value = value_of(status, backed_eval, eval_cp)
        extreme = extremes.get(key)
        row = {
            'key': key,
            'label': f'1. {san}' if uci else 'Start position',
            'value': value,
            'now_h': value_text(value, status),
            'best_h': (value_text(extreme.best_value, extreme.best_status)
                       if extreme else _NONE),
            'best_at': extreme.best_at if extreme else None,
            'worst_h': (value_text(extreme.worst_value, extreme.worst_status)
                        if extreme else _NONE),
            'worst_at': extreme.worst_at if extreme else None,
            'changes': extreme.changes if extreme else 0,
        }
        if uci:
            moves.append(row)
        else:
            start = row
    # White's best first move on top, the way the front page orders them;
    # moves with no value yet go last.
    moves.sort(key=lambda row: (row['value'] is None, -(row['value'] or 0),
                                row['label']))
    latest = MainLineSnapshot.objects.order_by('-bucket_start').first()
    return {
        'rows': [start] + moves,
        'since': min((row.since for row in extremes.values()), default=None),
        'line': _line_context(latest) if latest else None,
        'max_plies': MAIN_LINE_MAX_PLIES,
        'history': _daily_history(now),
        'history_days': HISTORY_DAYS,
    }


def roots_page(request):
    """Public page: the start position and first moves, best and worst, and
    the main line.  Cached in ``urls`` like the other read views."""
    return render(request, 'atomicdb/roots.html', page_context())
