"""Which community names deserve a second look, read from the names alone.

The opening-name review of 11-Nov-2026 sorted the names in force into five
groups.  This module computes the same groups live from the database, and the
Openings page shows them as a small "suggested for review" mark next to the
thumbs.  It is only a hint for voters: the votes are what put a name under
review (``name_votes``), never this.

The groups, for a community name in force on a position:

* ``same-name``: the same name (ignoring case, punctuation and
  defence/defense) stands on another position, and neither line continues
  the other;
* ``replaces-catalogue``: an approved edit hides the catalogue's name;
* ``joke``: the name reads as a joke, spam or a player handle;
* ``renamed``: the position was renamed two or more times;
* ``anonymous``: the name was proposed without an account.

Catalogue names are never marked: they come from published lists and are
only part of a ``same-name`` group when a community name repeats them.
"""

import logging
import re
import unicodedata

from django.core.cache import cache

from . import community_names, openings
from .models import OpeningNameSuggestion

logger = logging.getLogger(__name__)

CACHE_KEY = 'atomicdb.name-review.v1'
CACHE_SECONDS = 15 * 60

# Words that make a name read as a joke or spam.
JOKE_WORDS = ('spam', 'troll', 'noob', 'blunder', 'floppa', 'stomper',
              'suicide', 'kidney', 'lol', 'lmao', 'xd', 'meme', 'bruh',
              'idiot', 'stupid')
# Words of real opening names, never taken for a handle.
OPENING_WORDS = frozenset((
    'attack', 'defence', 'defense', 'gambit', 'variation', 'opening',
    'line', 'mainline', 'main', 'system', 'counterattack', 'trap', 'knight',
    'knights', 'bishop', 'queen', 'king', 'rook', 'pawn', 'pawns', 'center',
    'centre', 'central', 'modern', 'old', 'new', 'accepted', 'declined',
    'reversed', 'inverse', 'swapped', 'two', 'three', 'double', 'atomic',
    'the', 'and', 'with', 'of', 'anti', 'delayed', 'special', 'mate',
))
REASONS = {
    'same-name': 'the same name is on an unrelated line',
    'replaces-catalogue': 'replaces the catalogue name',
    'joke': 'reads like a joke, spam or a player handle',
    'renamed': 'renamed several times',
    'anonymous': 'proposed without an account',
}
_SAN_RE = re.compile(r'^(?:[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?'
                     r'|O-O(?:-O)?)[+#]?$')
_MOVE_NUMBER_RE = re.compile(r'^\d+\.+$')
_WORD_RE = re.compile(r"[0-9A-Za-zÀ-ÿ]+")


def normalised(name):
    """The key two names are "the same" under: case, punctuation and the
    defence/defense spelling ignored."""
    text = unicodedata.normalize('NFKD', name or '').casefold()
    text = text.replace('defence', 'defense')
    text = re.sub("['’`]", '', text)
    return ' '.join(re.findall(r'[0-9a-z]+', text))


def line_tokens(san):
    """The moves of a SAN line, move numbers dropped."""
    return tuple(token for token in (san or '').split()
                 if not _MOVE_NUMBER_RE.match(token))


def related(a, b):
    """True when one line continues the other (or a line is unknown)."""
    if not a or not b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return longer[:len(shorter)] == shorter


def _has_emoji(name):
    return any(unicodedata.category(ch) in ('So', 'Cs') or ord(ch) > 0xFFFF
               for ch in name)


def joke_reasons(name, proposer='', handles=frozenset(),
                 catalogue_words=frozenset()):
    """Why ``name`` reads as a joke, spam or a handle: a list of short
    phrases, empty when nothing does."""
    reasons = []
    folded = name.casefold()
    for word in JOKE_WORDS:
        pattern = (rf'(?<![a-z]){word}(?![a-z])' if len(word) <= 3
                   else word)
        if re.search(pattern, folded):
            reasons.append(f'contains "{word}"')
    letters = [ch for ch in name if ch.isalpha()]
    if len(letters) > 3 and name.isupper():
        reasons.append('all capitals')
    if _has_emoji(name):
        reasons.append('has an emoji')
    proposer = (proposer or '').casefold()
    if len(proposer) >= 4 and proposer in folded:
        reasons.append('names the account that proposed it')
    for word in _WORD_RE.findall(name):
        lower = word.casefold()
        if lower in OPENING_WORDS or lower in catalogue_words:
            continue
        if _SAN_RE.match(word):
            continue
        handle_like = (re.search(r'[a-z][A-Z]', word)
                       or re.search(r'[A-Za-z]{2,}\d', word))
        if handle_like or (len(lower) >= 4 and lower in handles):
            reasons.append(f'looks like a player handle ({word})')
            break
    return reasons


def _catalogue_words():
    words = set()
    try:
        for opening in openings.load_catalog().values():
            for word in _WORD_RE.findall(opening.name):
                words.add(word.casefold())
    except openings.OpeningCatalogError:
        logger.exception('opening catalogue unavailable')
    return frozenset(words)


def _known_handles(proposers):
    handles = {p.casefold() for p in proposers if p}
    try:
        from django.contrib.auth.models import User
        handles |= {u.casefold() for u in
                    User.objects.values_list('username', flat=True)}
    except Exception:
        logger.exception('accounts unavailable for the name review')
    return frozenset(h for h in handles if len(h) >= 4)


def compute(lines=None):
    """``{position_key: [reason, ...]}`` for every community name in force
    that falls in at least one group.

    ``lines`` maps a key to its SAN line; it is read from the tree for the
    keys the catalogue does not name when not given.
    """
    names = community_names.approved_map()
    in_force = {}
    for key in names:
        entry = community_names.in_force(key, names=names)
        if entry is not None:
            in_force[key] = entry
    if not in_force:
        return {}
    history = {}
    for row in (OpeningNameSuggestion.objects
                .filter(status=OpeningNameSuggestion.SState.APPROVED,
                        position_id__in=list(in_force))
                .order_by('resolved_at', 'id')
                .values('position_id', 'kind', 'suggested_by')):
        history.setdefault(row['position_id'], []).append(row)

    try:
        catalogue = openings.load_catalog()
    except openings.OpeningCatalogError:
        logger.exception('opening catalogue unavailable')
        catalogue = {}
    if lines is None:
        lines = {}
        missing = [key for key in in_force if key not in catalogue]
        if missing:
            from . import views
            for key, (_preview, full) in views._line_labels_many(
                    missing).items():
                lines[key] = full
    token_lines = {key: line_tokens(san) for key, san in lines.items()}
    for key, opening in catalogue.items():
        token_lines.setdefault(key, line_tokens(opening.reference_line_san))

    # Every name shown on its own position, catalogue and community, by
    # normalised name.
    shown = {}
    for key, opening in catalogue.items():
        if key not in in_force:
            shown.setdefault(normalised(opening.name), set()).add(key)
    for key, entry in in_force.items():
        shown.setdefault(normalised(entry['name']), set()).add(key)

    catalogue_words = _catalogue_words()
    handles = _known_handles(entry.get('suggested_by') or ''
                             for entry in in_force.values())
    marks = {}
    for key, entry in in_force.items():
        reasons = []
        same = shown.get(normalised(entry['name']), set()) - {key}
        if any(not related(token_lines.get(key), token_lines.get(other))
               for other in same):
            reasons.append(REASONS['same-name'])
        if (entry['kind'] == OpeningNameSuggestion.SKind.EDIT
                and key in catalogue):
            reasons.append(REASONS['replaces-catalogue'])
        if joke_reasons(entry['name'], entry.get('suggested_by') or '',
                        handles, catalogue_words):
            reasons.append(REASONS['joke'])
        rows = history.get(key, [])
        renames = len(rows) - (0 if key in catalogue else 1)
        if renames >= 2:
            reasons.append(REASONS['renamed'])
        if not (entry.get('suggested_by') or '').strip():
            reasons.append(REASONS['anonymous'])
        if reasons:
            marks[key] = reasons
    return marks


def suggested():
    """``compute()`` behind a shared cache: the Openings page asks on every
    render, and the answer only moves when a name is approved."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    try:
        marks = compute()
    except Exception:
        logger.exception('name review marks unavailable')
        return {}
    cache.set(CACHE_KEY, marks, CACHE_SECONDS)
    return marks


def invalidate():
    cache.delete(CACHE_KEY)
