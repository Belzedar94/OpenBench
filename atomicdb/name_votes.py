"""Thumbs up and down on opening names, and the names they put under review.

Owner, 11-Nov-2026: every name on the Openings page (catalogue and community
alike) gets a thumbs up and a thumbs down.  One vote per voter and position,
which the voter can change or withdraw.  A name with at least
``min_votes()`` votes, three quarters of them down, is put under review: a
``OpeningNameFlag`` row is born, the poster job puts a poll in the Discord
channel, and an approver decides from the moderation page.

Votes never change a name.  The approver's decision goes through the ordinary
approval of ``OpeningNameSuggestion`` (``views.apply_name_change``), so the
credit and the history of a name read the same whichever way it changed.

Identity, the same as the campaign votes (``views._voter_token``): an
account when the visitor is signed in, else the opaque ``atomicdb_voter``
cookie.  Clearing the cookie gives another vote, so anonymous votes carry two
caps by address: a few per position, and a daily number for votes cast
without sending a cookie back (the only ones nothing else groups).
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError
from django.db.models import Count
from django.utils import timezone

from . import community_names, openings
from .database import atomic
from .models import OpeningNameFlag, OpeningNameVote

logger = logging.getLogger(__name__)

UP, DOWN = 1, -1
VALUES = {'up': UP, 'down': DOWN}
WITHDRAW = 'none'

# A name goes under review with at least this many votes on it...
DEFAULT_MIN_VOTES = 4
# ...and at least this share of them down (3 of 4).
DOWN_SHARE = (3, 4)
# Anonymous votes on one position from one address.  A few, not one: behind
# a campus or a phone network many people share an address.
ANON_VOTES_PER_IP_POSITION = 3
# Votes in a day from one address that came without a cookie.
MINTED_VOTES_PER_IP_DAY = 20

USER_PREFIX = 'u:'
TOKEN_PREFIX = 't:'


def min_votes():
    """``ATOMICDB_NAME_REVIEW_MIN_VOTES`` in settings, else 4."""
    try:
        value = int(getattr(settings, 'ATOMICDB_NAME_REVIEW_MIN_VOTES',
                            DEFAULT_MIN_VOTES))
    except (TypeError, ValueError):
        value = DEFAULT_MIN_VOTES
    return max(1, value)


def under_review(up, down):
    """True when a tally puts its name under review."""
    total = up + down
    return (total >= min_votes()
            and down * DOWN_SHARE[1] >= total * DOWN_SHARE[0])


def approval(up, down):
    """Share of thumbs up, or ``None`` without votes."""
    total = up + down
    return None if not total else up / total


def current_name(key, names=None):
    """``(name, entry)``: the name the position itself shows and the
    community entry in force on it (``None`` for a catalogue name), or
    ``(None, None)`` when the position has no name of its own.

    Only an exact name counts, the same as the moderation queue: a name
    inherited from further up the line belongs to that other position.
    """
    entry = community_names.in_force(key, names=names)
    if entry is not None:
        return entry['name'], entry
    try:
        match = openings.lookup_key(key)
    except openings.OpeningCatalogError:
        logger.exception('opening catalogue unavailable')
        return None, None
    return (None, None) if match is None else (match['name'], None)


def voter_for(user, token):
    """The ``voter`` of a request: the account, or the cookie token."""
    if user is not None and user.is_authenticated:
        return USER_PREFIX + user.username[:64]
    return TOKEN_PREFIX + token


def tallies(current):
    """``{key: (up, down)}`` for ``{key: name shown now}``, in one query.

    A vote counts only while the position still shows the name it was cast
    on (§ ``OpeningNameVote``).
    """
    counts = {}
    if not current:
        return counts
    rows = (OpeningNameVote.objects
            .values('position_key', 'name', 'value')
            .annotate(n=Count('id')).order_by())
    if len(current) <= 200:
        rows = rows.filter(position_key__in=list(current))
    for row in rows:
        key = row['position_key']
        if current.get(key) != row['name']:
            continue
        up, down = counts.get(key, (0, 0))
        if row['value'] > 0:
            up += row['n']
        else:
            down += row['n']
        counts[key] = (up, down)
    return counts


def tally(key, name):
    return tallies({key: name}).get(key, (0, 0))


def votes_of(voter, current):
    """``{key: 'up'|'down'}`` of one voter, for names still shown."""
    if not voter or not current:
        return {}
    mine = {}
    for key, name, value in (OpeningNameVote.objects
                             .filter(voter=voter)
                             .values_list('position_key', 'name', 'value')):
        if current.get(key) == name:
            mine[key] = 'up' if value > 0 else 'down'
    return mine


def cast(key, choice, *, voter, ip, minted=False, merge_voter=None,
         toggle=False):
    """Record, change or withdraw one vote.  Returns a dict with ``status``
    (``voted``, ``withdrawn``, ``unnamed``, ``invalid`` or ``rate-limited``),
    the tally ``up``/``down`` and the voter's ``mine``.

    ``toggle`` is the page's buttons: pressing the thumb already chosen
    withdraws the vote instead of casting it again.

    ``merge_voter`` is the cookie identity of a signed-in visitor: a vote it
    cast on this position before signing in is folded into the account's,
    so one person does not count twice.
    """
    name, entry = current_name(key)
    if name is None:
        return {'status': 'unnamed', 'up': 0, 'down': 0, 'mine': None,
                'under_review': False}
    if choice != WITHDRAW and choice not in VALUES:
        up, down = tally(key, name)
        return {'status': 'invalid', 'up': up, 'down': down, 'mine': None,
                'under_review': False}
    status = 'withdrawn' if choice == WITHDRAW else 'voted'
    anonymous = voter.startswith(TOKEN_PREFIX)
    with atomic():
        row = (OpeningNameVote.objects.select_for_update()
               .filter(position_key=key, voter=voter).first())
        if merge_voter and merge_voter != voter:
            OpeningNameVote.objects.filter(position_key=key,
                                           voter=merge_voter).delete()
        if (toggle and row is not None and choice in VALUES
                and row.name == name and row.value == VALUES[choice]):
            choice, status = WITHDRAW, 'withdrawn'
        if choice == WITHDRAW:
            if row is not None:
                row.delete()
        elif row is not None:
            row.value, row.name = VALUES[choice], name
            row.save(update_fields=['value', 'name', 'updated'])
        elif anonymous and _capped(key, ip, minted):
            status = 'rate-limited'
        else:
            try:
                with atomic():
                    OpeningNameVote.objects.create(
                        position_key=key, voter=voter, name=name,
                        value=VALUES[choice], ip=ip or None, minted=minted)
            except IntegrityError:
                # Two clicks of the same voter racing: the other one won,
                # and this one becomes a change of that row.
                OpeningNameVote.objects.filter(
                    position_key=key, voter=voter).update(
                        value=VALUES[choice], name=name,
                        updated=timezone.now())
        up, down = tally(key, name)
        flagged = under_review(up, down)
        if flagged:
            flag(key, name, entry, up, down)
        else:
            flagged = OpeningNameFlag.objects.filter(
                position_key=key, name=name,
                resolved_at__isnull=True).exists()
    mine = None
    if status == 'voted':
        mine = choice
    elif status == 'rate-limited' and row is not None:
        mine = 'up' if row.value > 0 else 'down'
    return {'status': status, 'up': up, 'down': down, 'mine': mine,
            'under_review': flagged}


def _capped(key, ip, minted):
    if not ip:
        return False
    if OpeningNameVote.objects.filter(
            position_key=key, ip=ip,
            voter__startswith=TOKEN_PREFIX).count() \
            >= ANON_VOTES_PER_IP_POSITION:
        return True
    if minted and OpeningNameVote.objects.filter(
            ip=ip, minted=True,
            created__gte=timezone.now() - timedelta(days=1)).count() \
            >= MINTED_VOTES_PER_IP_DAY:
        return True
    return False


def flag(key, name, entry, up, down):
    """The review flag of ``name`` on ``key``: created the first time only.

    Returns ``(flag, created)``.  The unique pair (position, name) is what
    keeps a name from being posted twice.
    """
    existing = OpeningNameFlag.objects.filter(position_key=key,
                                              name=name).first()
    if existing is not None:
        return existing, False
    try:
        with atomic():
            row = OpeningNameFlag.objects.create(
                position_key=key, name=name,
                catalogue_name=community_names.catalog_name(key) or '',
                named_by=(community_names.credit(entry)
                          if entry is not None else ''),
                community=entry is not None, up=up, down=down)
    except IntegrityError:
        return OpeningNameFlag.objects.get(position_key=key, name=name), False
    return row, True


def open_flags():
    """Flags nobody has resolved yet, oldest first."""
    return (OpeningNameFlag.objects.filter(resolved_at__isnull=True)
            .order_by('flagged_at', 'id'))


def sync_flags(now=None):
    """Bring the flags in line with the votes and the names; ``(new, stale)``.

    Creates the flags a vote should have created and did not (a lower
    threshold in settings, say), and closes as ``STALE`` an open flag whose
    position no longer shows the flagged name.  The poster runs this before
    it posts.
    """
    now = now or timezone.now()
    names = community_names.approved_map()
    keys = set(OpeningNameVote.objects.values_list('position_key', flat=True)
               .distinct())
    keys |= set(open_flags().values_list('position_key', flat=True))
    current, entries = {}, {}
    for key in keys:
        name, entry = current_name(key, names=names)
        if name is not None:
            current[key], entries[key] = name, entry
    stale = 0
    for row in open_flags():
        if current.get(row.position_key) != row.name:
            row.resolution = OpeningNameFlag.Resolution.STALE
            row.resolved_at = now
            row.save(update_fields=['resolution', 'resolved_at'])
            stale += 1
    new = 0
    for key, (up, down) in tallies(current).items():
        if under_review(up, down):
            _row, created = flag(key, current[key], entries[key], up, down)
            new += created
    return new, stale


def current_names():
    """``{key: name}`` of every position that shows a name of its own: the
    catalogue's, or the community's where one is in force."""
    names = community_names.approved_map()
    current = {}
    try:
        for key, opening in openings.load_catalog().items():
            current[key] = opening.name
    except openings.OpeningCatalogError:
        logger.exception('opening catalogue unavailable')
    for key in names:
        entry = community_names.in_force(key, names=names)
        if entry is not None:
            current[key] = entry['name']
    return current


def poll_url(row):
    """The Discord link of a flag's poll, or ''."""
    if not row.discord_message_id:
        return ''
    from . import name_flags_discord
    return name_flags_discord.message_url(row.discord_message_id)


# ---- approvals --------------------------------------------------------------

def resolve_suggestion(row, decision, username):
    """Approve or reject a PENDING suggestion: THE approval path.

    The moderation queue and the review actions both come through here, so a
    name changed after a vote carries the same ``resolved_by``, date and
    credit as one approved from the queue.  The caller holds the row locked
    inside a transaction.  Returns the state written.

    The catalogue may have named the position while a NEW name waited, and a
    NEW name never covers it: the approval becomes a rejection, as always.
    An EDIT or a REMOVE was born against the name shown, so it is not
    affected.
    """
    from .models import OpeningNameSuggestion
    if (decision == OpeningNameSuggestion.SState.APPROVED
            and row.kind == OpeningNameSuggestion.SKind.NEW
            and openings.lookup_key(row.position_id) is not None):
        decision = OpeningNameSuggestion.SState.REJECTED
    row.status = decision
    row.resolved_by = (username or '')[:64]
    row.resolved_at = timezone.now()
    row.save(update_fields=['status', 'resolved_by', 'resolved_at'])
    return decision


def invalidate_names():
    """After any approval: the name map, and the review marks read from it."""
    from . import name_review
    community_names.invalidate()
    name_review.invalidate()


REVIEW_COMMENT = 'Opening-name review'
OUTCOMES = {'keep': 'kept', 'restore': 'restored', 'remove': 'removed',
            'rename': 'renamed'}


def apply_flag(flag_id, action, *, approver, ip, new_name=''):
    """An approver's decision on a name under review.  Returns an outcome
    word for the page: ``kept``, ``restored``, ``removed``, ``renamed``,
    ``invalid``, ``moved`` (the name changed since the flag) or ``done``
    (already resolved).

    ``restore`` and ``remove`` exist only where a community name is in force;
    ``rename`` needs a valid name and a position in the tree.  Each change
    is a new ``OpeningNameSuggestion`` approved at once through
    ``resolve_suggestion``, the queue's own path, with the approver as
    ``resolved_by``.  A restored or removed name is credited to nobody (the
    suggestion has no ``suggested_by``), a rename to the approver who typed
    it.
    """
    from .models import OpeningNameSuggestion, Position
    from . import views
    kinds = {'keep': OpeningNameFlag.Resolution.KEEP,
             'restore': OpeningNameFlag.Resolution.RESTORE,
             'remove': OpeningNameFlag.Resolution.REMOVE,
             'rename': OpeningNameFlag.Resolution.RENAME}
    if action not in kinds:
        return 'invalid'
    with atomic():
        flag_row = (OpeningNameFlag.objects.select_for_update()
                    .filter(id=flag_id).first())
        if flag_row is None:
            return 'invalid'
        if flag_row.resolved_at is not None:
            return 'done'
        key = flag_row.position_key
        name, entry = current_name(key)
        if name != flag_row.name:
            return 'moved'
        catalogue = community_names.catalog_name(key) or ''
        change = None
        if action == 'restore':
            if entry is None or not catalogue or catalogue == name:
                return 'invalid'
            change = dict(kind=OpeningNameSuggestion.SKind.EDIT,
                          proposed_name=catalogue[:60], suggested_by='')
        elif action == 'remove':
            if entry is None:
                return 'invalid'
            change = dict(kind=OpeningNameSuggestion.SKind.REMOVE,
                          proposed_name='', suggested_by='')
        elif action == 'rename':
            if (len(new_name) < views.SUGGESTION_NAME_MIN_CHARS
                    or len(new_name) > views.SUGGESTION_NAME_MAX_CHARS
                    or not views.SUGGESTION_NAME_RE.fullmatch(new_name)
                    or new_name == name):
                return 'invalid'
            change = dict(kind=OpeningNameSuggestion.SKind.EDIT,
                          proposed_name=new_name,
                          suggested_by=(approver or '')[:64])
        if change is not None:
            position = Position.objects.filter(key=key).first()
            if position is None:
                return 'invalid'
            row = OpeningNameSuggestion.objects.create(
                position=position, previous_name=name, ip=ip or '0.0.0.0',
                comment=REVIEW_COMMENT, **change)
            resolve_suggestion(row, OpeningNameSuggestion.SState.APPROVED,
                               approver)
        flag_row.resolution = kinds[action]
        flag_row.resolved_by = (approver or '')[:64]
        flag_row.resolved_at = timezone.now()
        flag_row.save(update_fields=['resolution', 'resolved_by',
                                     'resolved_at'])
    invalidate_names()
    return OUTCOMES[action]
