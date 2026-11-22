"""A text-first explorer page that needs no JavaScript at all.

For e-readers and other old or small browsers: a Kindle's WebKit, an e-ink
screen in greyscale, no scripts worth relying on.  Everything here is
rendered on the server and every control is a plain link or a GET form.

WHAT ONE PAGE COSTS.  One position and its children, read by the very
helpers ``api_query`` uses (``_child_moves`` and the header value of
``ingest.best_known_eval``), plus one move generation for the move names.
No walk of the tree: no line to the root, no backed spine, no opening name.
The best line is the closure's own winning line or the position's own engine
line, both stored on the row already.

IT ONLY READS.  An unknown position is reported, never seeded: a crawler or
a stale bookmark must not be able to grow the tree through this page.

THE "BACK" HISTORY RIDES IN THE URL.  ``path`` is the list of position keys
visited before this one, oldest first, so Back is a plain link and works on a
browser whose own back button is awkward to reach.  Every entry is checked
against the shape of a key and nothing in it is looked up; a malformed list
is dropped whole.  It is capped, so the URL stays bounded however long the
walk.
"""

import re

from django.shortcuts import render

from . import ingest, logic
from . import san as move_names
from .models import Position
from .views import (PublicFenError, _analysis_lines_for_display,
                    _arrow_move, _batched_sans, _board_rows, _child_moves,
                    _parse_public_fen)

KEY_SHAPE = re.compile(r'^[0-9a-f]{64}$')
# Positions remembered for Back.  64 hex characters each: 40 of them keep
# the whole URL under 3 kB, which every browser accepts.
PATH_MAX = 40
# Plies of an engine or winning line printed on the page.
LINE_MAX_PLIES = 16

_GLYPHS = {
    'K': '♔', 'Q': '♕', 'R': '♖',
    'B': '♗', 'N': '♘', 'P': '♙',
    'k': '♚', 'q': '♛', 'r': '♜',
    'b': '♝', 'n': '♞', 'p': '♟',
}


def _parse_path(raw):
    """The keys of ``path``, oldest first, or [] when any entry is wrong."""
    keys = [part for part in (raw or '').split(',') if part]
    if not all(KEY_SHAPE.match(key) for key in keys):
        return []
    return keys[-PATH_MAX:]


def lite_url(key, path=(), letters=False):
    """The lite page of ``key`` with ``path`` as its Back history."""
    url = f'/atomicdb/lite/?key={key}'
    if path:
        url += '&path=' + ','.join(list(path)[-PATH_MAX:])
    if letters:
        url += '&letters=1'
    return url


def _board(fen, letters):
    """Ranks 8..1 of (label, dark square, piece is white) for the table."""
    rows = []
    for r, row in enumerate(_board_rows(fen)):
        cells = []
        for c, piece in enumerate(row):
            label = (piece if letters else _GLYPHS.get(piece, piece)) \
                if piece else ''
            cells.append((label, (r + c) % 2 == 1, piece.isupper()))
        rows.append((8 - r, cells))
    return rows


def _mate_str(mate):
    return f'M{mate}' if mate > 0 else f'-M{-mate}'


def _engine_line(pos, stm_white):
    """The position's own first engine line: (SAN, value), or None."""
    for line in _analysis_lines_for_display(pos):
        if line.get('prior_pass'):
            continue
        pv = [uci for uci in (line.get('pv') or []) if isinstance(uci, str)]
        if not pv:
            return None
        shown = pv[:LINE_MAX_PLIES]
        sans = _batched_sans(pos.fen, shown) or shown
        text = ' '.join(sans) + (' ...' if len(pv) > len(shown) else '')
        mate, cp = line.get('mate'), line.get('eval_cp')
        if mate is not None:
            value = _mate_str(mate if stm_white else -mate)
        elif cp is not None:
            value = f'{(cp if stm_white else -cp):+d}cp'
        else:
            value = None
        return text, value
    return None


def _header(pos, stm_white):
    """What the explorer's header says about this position, as text."""
    if pos.status != 'UNKNOWN':
        verdict = {'WHITE_WIN': 'White wins',
                   'BLACK_WIN': 'Black wins'}.get(pos.status, 'Draw')
        mate = (f'M{(pos.mate_in + 1) // 2}' if pos.mate_in else None)
        return {'verdict': verdict, 'closure': pos.closure, 'mate': mate}
    known = ingest.best_known_eval(pos)
    if known is None:
        return {'eval': None}
    eval_stm = int(known if stm_white else -known)
    backed = known != pos.eval_cp
    return {'eval': f'{eval_stm:+d}cp',
            'backed_plies': pos.backed_plies if backed else None}


def _move_rows(pos, children, path, letters, sans):
    rows = []
    seen = set(path) | {pos.key}
    child_path = list(path) + [pos.key]
    for m in children:
        repetition = m['key'] in seen or m['repetition']
        if m['unexplored']:
            state = 'unexplored'
        elif m['status'] == 'UNKNOWN':
            state = 'unsolved'
        else:
            state = f"{m['status']} {m['closure'] or ''}".strip()
        if m['mate_str']:
            value = m['mate_str']
        elif m['score'] is not None:
            value = f"{int(m['score']):+d}"
        else:
            value = 'not analysed'
        notes = []
        if m['backed'] and m['score'] is not None and not m['mate_str']:
            notes.append(f"{'walked' if m['backed_light'] else 'backed'} "
                         f"{m['backed_plies']}")
        if m['borrowed']:
            notes.append('borrowed')
        if repetition:
            notes.append('repetition')
        rows.append({
            'san': sans.get(m['uci'], m['uci']), 'uci': m['uci'],
            'value': value, 'notes': ' · '.join(notes), 'state': state,
            # A move back into a position already on this walk is shown and
            # not linked, as on the full explorer.
            'url': (None if m['key'] in seen
                    else lite_url(m['key'], child_path, letters)),
        })
    return rows


def lite(request):
    letters = request.GET.get('letters') == '1'
    raw_key = (request.GET.get('key') or '').strip().lower()
    raw_fen = (request.GET.get('fen') or '').strip()
    path = _parse_path(request.GET.get('path'))
    context = {'letters': letters, 'fen_value': raw_fen,
               'start_url': lite_url(ingest.root_key(), letters=letters),
               'letters_url': None, 'back_url': None}
    if path:
        context['back_url'] = lite_url(path[-1], path[:-1], letters)

    fen, key, error = None, None, None
    if raw_fen:
        try:
            fen = _parse_public_fen(raw_fen)
            key = logic.key_of(fen)
        except PublicFenError as exc:
            error = f'That FEN was not accepted: {exc}.'
        except Exception:
            error = 'That FEN could not be read.'
    elif raw_key:
        if KEY_SHAPE.match(raw_key):
            key = raw_key
        else:
            error = 'That position key is not valid.'
    else:
        key = ingest.root_key()

    pos = None
    if key is not None:
        pos = Position.objects.filter(key=key).first()
        if pos is None:
            error = 'This position is not in AtomicDB yet.'
            context['explorer_url'] = '/atomicdb/'
    if error:
        context['error'] = error
        if fen is not None:
            context['board'] = _board(fen, letters)
            context['stm'] = 'White' if fen.split()[1] == 'w' else 'Black'
            context['fen'] = fen
        return render(request, 'atomicdb/lite.html', context)

    stm_white = pos.fen.split()[1] == 'w'
    legal = [] if pos.closure == 'TERMINAL' else logic.legal_moves(pos.fen)
    children = _child_moves(pos)
    known = {m['uci'] for m in children}
    offtree = [uci for uci in legal if uci not in known]
    best = None if pos.closure == 'TERMINAL' else _arrow_move(children, pos)
    sans = move_names.sans(
        pos.fen, [m['uci'] for m in children] + offtree + [best],
        legal=legal or None)
    won_line = (pos.won_line or '').split()
    won_shown = won_line[:LINE_MAX_PLIES]
    won_san = ''
    if won_shown:
        won_san = ' '.join(_batched_sans(pos.fen, won_shown) or won_shown)
        if len(won_line) > len(won_shown):
            won_san += ' ...'
    context.update({
        'pos': pos, 'fen': pos.fen,
        'stm': 'White' if stm_white else 'Black',
        'board': _board(pos.fen, letters),
        'header': _header(pos, stm_white),
        'best_san': sans.get(best, best) if best else None,
        'won_line_san': won_san,
        'engine_line': None if won_san else _engine_line(pos, stm_white),
        # The rows reuse the explorer's table builder; it is cheap enough
        # for the public query API, which calls it on every request.
        'moves': _move_rows(pos, children, path, letters, sans),
        'offtree': [sans.get(uci, uci) for uci in offtree],
        'explorer_url': f'/atomicdb/explore/{pos.key}/',
        'letters_url': lite_url(pos.key, path, not letters),
    })
    return render(request, 'atomicdb/lite.html', context)
