"""Declared move orders of named positions (cz_eclipsia and MoKy, 20-Nov-2026).

Positions are a DAG.  A link to a named position without a route opens it on
its canonical lineage, which is one way in among many and often a move order
nobody plays ("Fix opening paths.  Make them real instead of canonical").
The explorer already takes a declared route, ``?play=`` (§
``views._validated_play_route``); what was missing is the route of each name.

Where a link to a name takes its line from, first that answers:

1. the position's ``OpeningRoute``, set by an approver (directly, or by
   approving a suggestion that carried a line).  It works for a catalogue
   name and a community one alike, so it is also how an approver overrides a
   catalogue line;
2. the catalogue's own reference line, for a catalogue name.  It is a real
   move order by construction (it is the documented one, and the catalogue
   validator replays it to the position), and the catalogue rows of the
   Openings page and the name in the explorer header already linked through
   it, so the default is what most of the site already did;
3. nothing: the canonical lineage, as before.

A declared line is written only after replaying it from the start position:
it must be legal, repeat no position, fit the explorer's route limit, and end
exactly on the named position.  Every position along it is materialised then,
the way a click through the line would make it, so its ``?play=`` link always
opens where it says.  The map of lines is a few dozen rows, read in one query
and cached for a few seconds with explicit invalidation, the same contract as
``community_names``.  Fail-open: if it cannot be read, links go back to the
default.
"""

import functools
import logging
import re
from urllib.parse import parse_qs, urlsplit

from django.core.cache import cache
from django.utils import timezone

from . import logic

logger = logging.getLogger(__name__)

CACHE_KEY = 'atomicdb.opening-routes.v1'
CACHE_SECONDS = 5
# The explorer's own limits on a ``play`` route (``views.PLAY_ROUTE_MAX_*``;
# a test holds them equal).  A line the explorer cannot replay is no link.
MAX_PLIES = 64
MAX_CHARS = MAX_PLIES * 6
# What an approver or a visitor may type: SAN movetext, UCI moves, or an
# explorer link with its ``?play=``.
RAW_MAX_CHARS = 2000
_UCI_RE = re.compile(r'^[a-h][1-8][a-h][1-8][qrbn]?$')


class LineError(ValueError):
    """A line that cannot be a declared route; the message says why."""


# ---- reading what was typed -------------------------------------------------

def parse(raw):
    """The UCI moves of what was typed, from the start position.

    Accepts UCI (``g1f3,f7f6`` or ``g1f3 f7f6``), SAN movetext as the
    position box reads it (``1. Nf3 f6 2. Nc3``), or an explorer link with
    a ``?play=``.  ``[]`` for an empty text.  Legality is checked here for
    SAN, which needs the position to read a move; ``validate`` checks it for
    everything.
    """
    text = ' '.join((raw or '').split())
    if not text:
        return []
    if len(text) > RAW_MAX_CHARS:
        raise LineError('that line is too long')
    if 'play=' in text:
        try:
            query = urlsplit(text).query or text.split('?', 1)[-1]
            text = parse_qs(query).get('play', [''])[0]
        except ValueError as exc:
            raise LineError('that link could not be read') from exc
    tokens = [token for token in re.split(r'[\s,]+', text) if token]
    if tokens and all(_UCI_RE.fullmatch(token) for token in tokens):
        return tokens
    return _san_to_uci(text)


def _san_to_uci(text):
    from . import views

    if views._PGN_FEN_HEADER.search(text):
        raise LineError('a line starts at the start position, without a FEN')
    tokens = views._pgn_tokens(text)
    if not tokens:
        raise LineError('no moves found in that line')
    if len(tokens) > MAX_PLIES:
        raise LineError(f'a line is at most {MAX_PLIES} plies')
    fen, ucis = logic.start_fen(), []
    for ply, token in enumerate(tokens):
        uci = views._uci_for_san(fen, token)
        if uci is None:
            number = ply // 2 + 1
            side = 'White' if ply % 2 == 0 else 'Black'
            raise LineError(f'move {number} for {side} ("{token}") is not a '
                            'legal atomic move at that point of the line')
        ucis.append(uci)
        fen = logic.apply_move(fen, uci)
    return ucis


def validate(ucis, target_key=None):
    """``(fens, keys)`` of a line replayed from the start position, both
    starting with the start position.

    Raises ``LineError`` unless every move is legal, no position repeats,
    the line fits the explorer's route limit and, with ``target_key``, it
    ends exactly there.  An empty line is no route.
    """
    ucis = list(ucis)
    if not ucis:
        raise LineError('no moves in that line')
    if len(ucis) > MAX_PLIES or len(','.join(ucis)) > MAX_CHARS:
        raise LineError(f'a line is at most {MAX_PLIES} plies')
    fen = logic.start_fen()
    fens, keys = [fen], [logic.key_of(fen)]
    for ply, uci in enumerate(ucis, start=1):
        if not _UCI_RE.fullmatch(uci) or uci not in logic.legal_moves(fen):
            raise LineError(f'ply {ply} ({uci}) is not a legal atomic move '
                            'at that point of the line')
        fen = logic.apply_move(fen, uci)
        key = logic.key_of(fen)
        if key in keys:
            raise LineError(f'the line repeats a position at ply {ply}')
        fens.append(fen)
        keys.append(key)
    if target_key is not None and keys[-1] != target_key:
        raise LineError('that line does not end on this position')
    return fens, keys


def san_text(ucis):
    """Numbered SAN of a line from the start position, or ``''``."""
    from . import roots
    return roots.numbered(roots._san(list(ucis)).split())


# ---- the map ----------------------------------------------------------------

def routes():
    """``{position_key: (uci, ...)}`` of every declared line — fail-open."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    from .models import OpeningRoute
    try:
        loaded = {key: tuple(line.split(','))
                  for key, line in OpeningRoute.objects.values_list(
                      'position_key', 'line') if line}
    except Exception:
        logger.exception('opening routes unavailable')
        return {}
    cache.set(CACHE_KEY, loaded, CACHE_SECONDS)
    return loaded


def invalidate():
    cache.delete(CACHE_KEY)


def route_for(key, catalogue_line=(), declared=None):
    """The line a link to the name of ``key`` takes, or ``None``.

    The declared line first, then ``catalogue_line`` (the catalogue's
    reference line of that position, when it names it).  ``declared`` is the
    map already read by a caller that links many names (§ ``routes``).
    """
    if declared is None:
        declared = routes()
    line = declared.get(key) or tuple(catalogue_line or ())
    if not line or len(line) > MAX_PLIES:
        return None
    return tuple(line)


def catalogue_line(key):
    """The catalogue's reference line of ``key`` when it names it, else ``()``.
    """
    from . import openings
    try:
        opening = openings.load_catalog().get(key)
    except openings.OpeningCatalogError:
        return ()
    return tuple(opening.reference_line_uci) if opening is not None else ()


def explore_url(key, line=None):
    """``/atomicdb/explore/<key>/``, with ``?play=`` when there is a line."""
    url = f'/atomicdb/explore/{key}/'
    if line:
        url += '?play=' + ','.join(line)
    return url


def name_url(key, declared=None):
    """Where a link to the name of ``key`` goes (§ ``route_for``)."""
    return explore_url(key, route_for(key, catalogue_line(key), declared))


@functools.lru_cache(maxsize=4096)
def _prefix_keys(line):
    """The keys along a line, start position first; ``()`` if it does not
    replay.  Lines are immutable inputs (catalogue or declared), so a
    process remembers them."""
    try:
        return tuple(validate(line)[1])
    except Exception:
        return ()


def openable(pairs):
    """``{key: line}`` of the ``(key, line)`` pairs whose line the explorer
    will open as it is: it replays to ``key`` and every position along it is
    in the tree.  One query for all of them.

    A ``?play=`` whose prefix is not in the tree lands on the deepest
    position it has, which is a worse link than none for a name the tree
    already holds by another order; this is how a list of links keeps the
    route only where it works.
    """
    walked = {}
    for key, line in pairs:
        if not line:
            continue
        keys = _prefix_keys(tuple(line))
        if keys and keys[-1] == key:
            walked[key] = (tuple(line), keys)
    if not walked:
        return {}
    from .models import Position
    wanted = {k for _line, keys in walked.values() for k in keys}
    have = set(Position.objects.filter(key__in=list(wanted))
               .values_list('key', flat=True))
    return {key: line for key, (line, keys) in walked.items()
            if all(k in have for k in keys)}


# ---- writing ----------------------------------------------------------------

def set_route(key, ucis, *, by):
    """Declare ``ucis`` as the line of ``key``, or clear it with ``[]``.

    Returns ``'set'``, ``'cleared'`` or ``'unchanged'``; raises ``LineError``
    for a line that does not replay to ``key``.  Every position along the
    line is materialised with its edge, through the explorer's own click
    path (``views._materialise_child``), inside one transaction with the
    row.
    """
    from . import ingest, views
    from .database import atomic
    from .models import OpeningRoute, Position

    ucis = list(ucis)
    if not ucis:
        deleted, _ = OpeningRoute.objects.filter(position_key=key).delete()
        invalidate()
        return 'cleared' if deleted else 'unchanged'
    validate(ucis, key)
    line = ','.join(ucis)
    with atomic():
        node = Position.objects.filter(key=logic.key_of(
            logic.start_fen())).first()
        if node is None:
            node = ingest.get_or_create_position(logic.start_fen())
        for uci in ucis:
            node = views._materialise_child(node, uci)
        current = OpeningRoute.objects.filter(position_key=key).first()
        if current is not None and current.line == line:
            return 'unchanged'
        OpeningRoute.objects.update_or_create(
            position_key=key,
            defaults={'line': line, 'line_san': san_text(ucis)[:1024],
                      'set_by': (by or '')[:64], 'set_at': timezone.now()})
    invalidate()
    return 'set'
