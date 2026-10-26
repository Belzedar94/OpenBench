"""The start position, its first moves and the named openings, over time.

Community requests: keep the best and the worst value the start position and
each of its first moves have ever had, and log how the main line from the
start position evolves (0050); then the three main replies to 1.Nf3 and every
position the opening catalogue names (0051).  Two records feed the public
page ``/atomicdb/roots/``:

* ``RootExtreme``: per watched position, the highest and the lowest headline
  value since tracking began, and when.  On PostgreSQL a row trigger keeps it,
  so every change counts, the cascade's ``bulk_update`` included.  Without
  the trigger, ``fold_extremes`` folds the current values in at the hourly
  capture.
* ``MainLineSnapshot``: the main line once per UTC hour, written by
  ``capture_atomicdb_progress`` beside its own row.

The value is always the one the explorer headlines (``ingest.known_eval_of``),
from White's side: a proven status as +10000, -10000 or 0, then the backed
value, then the point eval.
"""

import functools
import logging
from dataclasses import dataclass
from datetime import timedelta, timezone as datetime_timezone

from django.shortcuts import render
from django.utils import timezone

from . import community_names, ingest, logic, openings
from .database import atomic, connection
from .models import Edge, MainLineSnapshot, Position, RootExtreme

logger = logging.getLogger(__name__)

UTC = datetime_timezone.utc

# The names migrations 0050/0051 give the trigger, its function, and the
# function its WHEN clause asks whether a key is watched.
TRIGGER = 'atomicdb_root_extremes'
WATCH_FUNCTION = 'atomicdb_watched_key'
# The replies the first table also follows, indented under their first move.
# Community request (Torom): the three main answers to 1.Nf3.
REPLIES = {'g1f3': ('f7f6', 'e7e5', 'd7d6')}
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


@dataclass(frozen=True)
class Watched:
    """One watched position, as the page lists it."""

    key: str
    kind: str            # 'start', 'move', 'reply' or 'opening'
    label: str           # 'Start position', '1. e4', '1... f6', or a name
    line: tuple = ()     # UCI moves from the start position
    line_san: str = ''   # the same line in numbered SAN
    parent: str = ''     # for a reply, the first move it answers


@functools.lru_cache(maxsize=1)
def first_table():
    """The start position, each first move, and the replies of ``REPLIES``.

    Computed with the move generator, so it is the same canonical identity the
    tree uses.  Each reply comes right after the move it answers.
    """
    import pyffish as pf

    start = logic.start_fen()
    rows = [Watched(logic.key_of(start), 'start', 'Start position')]
    for uci in sorted(logic.legal_moves(start)):
        fen = logic.apply_move(start, uci)
        san = pf.get_san(logic.VARIANT, start, uci)
        rows.append(Watched(logic.key_of(fen), 'move', f'1. {san}', (uci,),
                            f'1. {san}'))
        for reply in REPLIES.get(uci, ()):
            reply_san = pf.get_san(logic.VARIANT, fen, reply)
            rows.append(Watched(
                logic.key_of(logic.apply_move(fen, reply)), 'reply',
                f'1... {reply_san}', (uci, reply), f'1. {san} {reply_san}',
                parent=uci))
    return tuple(rows)


@functools.lru_cache(maxsize=1)
def _catalogue_rows():
    return tuple(
        Watched(opening.position_key, 'opening', opening.name,
                tuple(opening.reference_line_uci),
                opening.reference_line_san)
        for opening in openings.load_catalog().values())


def named_openings():
    """Every position the opening catalogue names, in catalogue order.

    Fail-open like every other reader of the catalogue: if it cannot load,
    the page and the capture go on without this list (the trigger keeps its
    own copy of the keys, written into migration 0051).
    """
    try:
        return _catalogue_rows()
    except openings.OpeningCatalogError:
        logger.exception('opening catalogue unavailable')
        return ()


def watched_keys():
    """Every watched key once: the first table, then the catalogue."""
    return list(dict.fromkeys(
        row.key for row in first_table() + named_openings()))


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
    """Is the extremes trigger (migrations 0050/0051) there, and enabled?"""
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


_RECORDS_SQL = (
    'SELECT p.key, p.status, p.backed_eval, p.eval_cp,'
    ' r.best_value, r.best_status, r.best_at,'
    ' r.worst_value, r.worst_status, r.worst_at, r.changes, r.since'
    ' FROM {positions} p LEFT JOIN {extremes} r ON r.key = p.key'
    ' WHERE p.key IN ({params})')


def _aware(moment):
    # SQLite hands a raw cursor naive UTC datetimes; PostgreSQL aware ones.
    if moment is not None and timezone.is_naive(moment):
        return timezone.make_aware(moment, UTC)
    return moment


def _records(keys):
    """Current value and record of every watched key, in ONE query.

    A position the tree does not have yet is simply absent from the result.
    """
    if not keys:
        return {}
    sql = _RECORDS_SQL.format(positions=Position._meta.db_table,
                              extremes=RootExtreme._meta.db_table,
                              params=', '.join(['%s'] * len(keys)))
    with connection.cursor() as cursor:
        cursor.execute(sql, list(keys))
        rows = cursor.fetchall()
    records = {}
    for (key, status, backed_eval, eval_cp, best_value, best_status, best_at,
         worst_value, worst_status, worst_at, changes, since) in rows:
        records[key] = {
            'value': value_of(status, backed_eval, eval_cp), 'status': status,
            'best_value': best_value, 'best_status': best_status,
            'best_at': _aware(best_at), 'worst_value': worst_value,
            'worst_status': worst_status, 'worst_at': _aware(worst_at),
            'changes': changes, 'since': _aware(since)}
    return records


def _explore_url(row):
    url = f'/atomicdb/explore/{row.key}/'
    if row.line and len(row.line) <= PLAY_LINK_MAX_PLIES:
        url += '?play=' + ','.join(row.line)
    return url


def _row(watched, record, label=None):
    record = record or {}
    tracked = record.get('best_value') is not None
    return {
        'key': watched.key,
        'label': label or watched.label,
        'line_san': watched.line_san,
        'url': _explore_url(watched),
        'present': bool(record),
        'value': record.get('value'),
        'now_h': value_text(record.get('value'),
                            record.get('status', 'UNKNOWN')),
        'best_value': record.get('best_value'),
        'best_h': (value_text(record['best_value'], record['best_status'])
                   if tracked else _NONE),
        'best_at': record.get('best_at'),
        'worst_value': record.get('worst_value'),
        'worst_h': (value_text(record['worst_value'], record['worst_status'])
                    if tracked else _NONE),
        'worst_at': record.get('worst_at'),
        'changes': record.get('changes'),
        'indent': watched.kind == 'reply',
    }


def _by_value(rows, descending):
    """Highest value first (or lowest), rows with no value last."""
    known = [row for row in rows if row['value'] is not None]
    known.sort(key=lambda row: row['value'], reverse=descending)
    return known + [row for row in rows if row['value'] is None]


def _first_table_rows(records):
    """Start position, then White's first moves best first, each followed by
    the replies it has, Black's best reply first."""
    start, moves, replies = None, [], {}
    for watched in first_table():
        row = _row(watched, records.get(watched.key))
        if watched.kind == 'start':
            start = row
        elif watched.kind == 'move':
            moves.append((watched, row))
        else:
            replies.setdefault(watched.parent, []).append(row)
    ordered = [start]
    for watched, row in sorted(moves, key=lambda item: (
            item[1]['value'] is None, -(item[1]['value'] or 0),
            item[1]['label'])):
        ordered.append(row)
        ordered += _by_value(replies.get(watched.line[0], []),
                             descending=False)
    return ordered


# Columns of the named-openings table: the sort key, and whether the column
# sorts highest first when clicked.  ``-`` before a column flips it.
SORTS = {
    'name': (lambda row: (row['label'].casefold(), row['line_san']), False),
    'now': (lambda row: row['value'], True),
    'best': (lambda row: row['best_value'], True),
    'worst': (lambda row: row['worst_value'], False),
    'changes': (lambda row: row['changes'], True),
}
DEFAULT_SORT = 'name'


def _sorted(rows, sort):
    """Rows in the order ``sort`` asks for; unknown values sort by name.

    Rows without the sorted value go last either way, and ties keep name
    order (the sort is stable over a list already ordered by name).
    """
    column = sort.lstrip('-')
    if column not in SORTS or sort.count('-') > 1:
        column, sort = DEFAULT_SORT, DEFAULT_SORT
    key, descending = SORTS[column]
    if sort.startswith('-'):
        descending = not descending
    rows = sorted(rows, key=SORTS['name'][0])
    if column == 'name':
        ordered = list(reversed(rows)) if descending else rows
    else:
        known = [row for row in rows if key(row) is not None]
        known.sort(key=key, reverse=descending)
        ordered = known + [row for row in rows if key(row) is None]
    return ordered, column, descending


def _opening_rows(records, sort):
    names = community_names.approved_map()
    rows = []
    for watched in named_openings():
        label = community_names.name_for(watched.key, names=names)
        rows.append(_row(watched, records.get(watched.key), label=label))
    rows, column, descending = _sorted(rows, sort)
    # Each header sorts by its column; the column in force flips instead.
    links = {}
    for name, (_key, default_descending) in SORTS.items():
        if name != column:
            links[name] = name
        else:
            links[name] = (('-' + name) if descending == default_descending
                           else name)
    arrows = {name: '' for name in SORTS}
    arrows[column] = ' ▾' if descending else ' ▴'
    return rows, links, arrows


def page_context(now=None, sort=DEFAULT_SORT):
    now = now or timezone.now()
    records = _records(watched_keys())
    first_rows = _first_table_rows(records)
    opening_rows, sort_links, sort_arrows = _opening_rows(records, sort)
    latest = MainLineSnapshot.objects.order_by('-bucket_start').first()
    return {
        'rows': first_rows,
        'since': min((records[row['key']]['since'] for row in first_rows
                      if row['key'] in records
                      and records[row['key']]['since'] is not None),
                     default=None),
        'openings': opening_rows,
        'sort_links': sort_links,
        'sort_arrows': sort_arrows,
        'line': _line_context(latest) if latest else None,
        'max_plies': MAIN_LINE_MAX_PLIES,
        'history': _daily_history(now),
        'history_days': HISTORY_DAYS,
    }


def roots_page(request):
    """Public page: the start position, first moves and named openings with
    their best and worst, and the main line.  Cached in ``urls`` like the
    other read views; the sort order is part of the URL."""
    return render(request, 'atomicdb/roots.html',
                  page_context(sort=request.GET.get('sort', DEFAULT_SORT)))
