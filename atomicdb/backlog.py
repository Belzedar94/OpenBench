"""Community backlogs: re-searches anyone signed in can adopt.

Some searches the tree needs are only ever bought by the server, and the
server only buys when nobody is requesting anything.  With thousands of
requests always waiting, two such lists sat untouched (Discord, 15-Nov-2026):

* LOST LINES.  Open positions whose stored search kept fewer lines than it
  asked for: the old worker rule dropped every line after the first bound
  one.  Found by a one-off read of the ingest history (``find_dropped.py``,
  14-Nov) and searched again at 128M nodes with five lines, the
  re-search agreed with Torom.
* SHALLOW SHOWCASES.  Open positions whose shown search (the first current
  line of ``last_analysis``) ran 128M nodes but the engine reported a depth
  under 18 and no mate: the same definition as the idle fleet's repair arm
  (§ ``idle_repair.shallow_showcase``), which only gets the AUTO band and
  narrow engines and has not kept up.
* CLEAR WINS (16-Nov-2026).  The +16 sweep's positions (§ ``sweep``): open,
  searched once at about 128M, the side to move ahead by +16 to +90.  The
  sweep bought them on a fixed share of the leases, requests or not; the
  community asked for them here instead, as a list it adopts like the other
  two.  Filled from the sweep's own ordered list (``sweep16.json``: White's
  strategy tree first), so the order of service is the sweep's.
* DEEP WINS (16-Nov-2026, asked for by MoKy).  Open positions searched
  exactly twice (``visits == 2``) whose side to move is ahead by +25 or more,
  the value read as the sweep reads it (``backed_eval``, else ``eval_cp``,
  White's side), searched again at 512M with one line.  Found by a walk of
  the completed searches by id, like the shallow scan, and loaded the highest
  value first (``atomicdb_backlog load-deep-wins``).
* WHITE'S BEST LINE (17-Nov-2026, asked for by MoKy, narrowed by Eclipsia).
  Open positions reachable from the start position with White always
  playing the move its value stands on and Black playing anything (the
  sweep's walk of White's strategy tree, § ``sweep.walk_strategy_tree``),
  whose shown search ran under 128M nodes (an 8M or 32M pass) and reached a
  depth under 14, with no mate.  Searched again at 128.64M, like a shallow
  showcase, and loaded in the order the walk meets them, the shallowest
  first (``atomicdb_backlog load-best-line``).  White's choice moves as the
  tree learns, so a position is checked again for its search when adopted,
  not for its place on the line.

MoKy's proposal: cut the lists into chunks anyone can adopt.  A batch adopted
from the queue page becomes the adopter's own requests (source USER,
``requested_by`` the adopter), so the existing rules apply unchanged: their
own workers serve them first, the fair share charges them to their account,
and they show in their queue and on their profile.  The ``arm`` says which
backlog a request came from.

WHAT A BATCH BUYS.  One search per position, on the slot every producer
queues on: the next visit, ``(position, generation=visits)``.  Each position
is read again when it is adopted, and dropped from the list instead if it has
closed, no longer qualifies, or already has its next search queued or
running.  A search the server queued there itself and nobody has taken is
promoted to the adopter's request, the same promotion a click makes
(§ ``ingest._request_rung``).

THE PRICE IS 128M NODES, AND A HAIR MORE.  ``ingest_analysis`` lets a search
replace the shown one only if it ran at least as many nodes, counted as the
engine reported them, and an engine stops a little past its budget.  Bought
at exactly 128M, a re-search would lose that comparison to the search it is
meant to replace most of the time and land in the pass history only, which
helps neither list.  So it is bought at the repair arm's overbid, 128.64M
(§ ``idle_repair.REPAIR_OVERBID_PER_MILLE``), which every reader of the
ladder still sees as 128M.  A position whose shown search already ran that
much is dropped: a 128M search could not replace it.  Lost lines and clear
wins bid over the nodes the shown search ran when that is more, as long as
the bid stays on the 128M rung: a short search or a single search may itself
have been bought at 128.64M.

DEEP WINS PAY THE SAME RULE ON THE 512M RUNG: 514.56M (``DEEP_BUDGET``), or
two per mille over the shown search when that already ran as much, as long
as the bid stays on the 512M rung; a position shown by a 2B search is
dropped as ``deep``.  A shown search of 128M is simply replaced.

WIDTH.  Lost lines: five, the point of the re-search.  Shallow showcases and
White's best line: the house policy for a revisit at this budget
(§ ``ingest.multipv_for``).  Clear
wins: one, what the sweep buys (§ ``sweep.SWEEP_MULTIPV``): the search only
has to find the mate.  Deep wins: one, for the same reason.

BATCH SIZES.  25, 50 or 100 positions; deep wins 5, 10 or 20, since each of
their searches costs four 128M ones (``sizes_of``).

HOW MUCH ONE ACCOUNT MAY TAKE.  A batch counts against the same allowance of
waiting requests as every other request (``views.REQUEST_QUEUE_MAX``) and
is refused the same way when it is full.  On top of that an account has
``ADOPT_WAITING_MAX`` places for adopted positions waiting at once; a
position takes one place, or four for a 512M search (``weight_of``), so the
ceiling holds the compute waiting about steady whichever list it comes from.
Adopting more is possible as soon as some are served.  An adopted request is a
request like any other in the fair share, so it never takes another
account's turn.

SUBSCRIPTIONS (Torom, Discord, 15-Nov-2026: "a new adoption of 100 positions
when the last one is done").  A signed-in account can subscribe to a backlog
with one of its batch sizes.  Every five minutes the top-up
(``top_up``, run by ``manage.py warm_public_snapshot``) looks at each active
subscription and, when fewer than one batch of the account's adopted
positions from that backlog are waiting, adopts up to two batches for it with
``adopt`` itself: the same re-checks, the same allowance of waiting requests
and its receipt, the same ceiling of adopted positions.  A subscription turns
itself off when its backlog has nothing left to adopt, and the account can
turn it off any time.  One pass is bounded in subscriptions and in seconds,
and a failure stays inside it: the host command still exits cleanly.

WHAT IT NEVER DOES: decide a value, or change what any other producer buys.
The lists are filled by ``manage.py atomicdb_backlog``.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from urllib.parse import quote, urlencode

from django.core.cache import cache
from django.db.models import Count, F, Q
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone

from . import idle_repair, ingest, logic, sweep
from .database import atomic
from .models import AnalysisTask, BacklogItem, BacklogSubscription, Position

log = logging.getLogger(__name__)

LOST_LINES = 'lost-lines'
SHALLOW = 'shallow'
CLEAR_WINS = 'clear-wins'
DEEP_WINS = 'deep-wins'
DEEP_ARM = 'deep25'
BEST_LINE = 'best-line'
BEST_LINE_ARM = 'bestline'

# How many positions one click or one subscription batch adopts, unless a
# backlog says otherwise.
ADOPT_SIZES = (25, 50, 100)

# What a list is called on the page, what it is, and the arm its requests
# carry.  ``relines`` is the arm the lost-lines drip used, so every search
# bought for that list, before and after, answers to one name.
BACKLOGS = {
    LOST_LINES: {
        'arm': 'relines',
        'title': 'Lost lines',
        'about': ('Open positions whose stored search kept fewer lines than '
                  'it asked for. Each one is searched again at 128M nodes '
                  'with five lines.'),
    },
    SHALLOW: {
        'arm': 'shallow',
        'title': 'Shallow searches',
        'about': ('Open positions whose shown search ran 128M nodes but '
                  'stopped before depth 18. Each one is searched again at '
                  '128M nodes; an engine of 8 threads or fewer goes deepest '
                  'on them.'),
    },
    # ``sweep16``: the arm of the sweep's own tasks, so the adopted ones and
    # the ones the sweep queued itself answer to one name.
    CLEAR_WINS: {
        'arm': sweep.SWEEP_ARM,
        'title': 'Clear wins',
        'about': ('Open positions searched only once, at 128M nodes, where '
                  'the side to move is ahead by 16 to 90 pawns. Each one is '
                  'searched again at 128M nodes with one line; the current '
                  'engine often finds the mate.'),
    },
    # A 512M search costs four 128M ones: smaller batches, and each waiting
    # position takes four of the account's places (``ADOPT_WAITING_MAX``).
    DEEP_WINS: {
        'arm': DEEP_ARM,
        'title': 'Deep wins',
        'about': ('Open positions searched twice where the side to move is '
                  'ahead by 25 pawns or more. Each one is searched again at '
                  '512M nodes with one line; one such search costs as much '
                  'as four at 128M, so the batches are smaller.'),
        'sizes': (5, 10, 20),
        'weight': 4,
    },
    BEST_LINE: {
        'arm': BEST_LINE_ARM,
        'title': "White's best line",
        'about': ('Open positions White reaches by always playing the move '
                  'the tree rates best, whatever Black replies, whose shown '
                  'search ran fewer than 128M nodes and stopped before depth '
                  '14. Each one is searched again at 128M nodes.'),
    },
}
ORDER = (LOST_LINES, SHALLOW, CLEAR_WINS, DEEP_WINS, BEST_LINE)
ARMS = tuple(spec['arm'] for spec in BACKLOGS.values())
# The places one waiting adopted position takes, by the arm it carries.
ARM_WEIGHTS = {spec['arm']: spec.get('weight', 1)
               for spec in BACKLOGS.values()}


def sizes_of(backlog):
    """The batch sizes ``backlog`` offers."""
    return BACKLOGS[backlog].get('sizes', ADOPT_SIZES)


def default_size_of(backlog):
    """The batch size a subscription gets when it names none: the middle."""
    sizes = sizes_of(backlog)
    return sizes[len(sizes) // 2]


def weight_of(backlog):
    """The places one waiting position of ``backlog`` takes."""
    return BACKLOGS[backlog].get('weight', 1)


ADOPT_BUDGET = (idle_repair.REPAIR_MIN_NODES
                * (1000 + idle_repair.REPAIR_OVERBID_PER_MILLE) // 1000)
# Deep wins: the same overbid on the 512M rung, 514,560,000 nodes, which
# every reader of the ladder still sees as 512M.
DEEP_RUNG = ingest.BUDGET_LADDER[3]
DEEP_BUDGET = (DEEP_RUNG * (1000 + idle_repair.REPAIR_OVERBID_PER_MILLE)
               // 1000)
DEEP_MULTIPV = 1
# "Ahead by 25 pawns or more", White's side as the tree shows it: White to
# move at +2500 or more, Black to move at -2500 or less.  No ceiling: a value
# in the mate band is a mate the engine reported and nobody verified.
DEEP_LOW_CP = 2500
DEEP_VISITS = 2
LOST_LINES_MULTIPV = 5
# White's best line: a shown search under the 128M rung that reached a depth
# under this, Eclipsia's cut (17-Nov-2026).
BEST_LINE_DEPTH = 14
# A lost-lines position whose short search already ran the adoption budget
# (the engine stops a little past it: 128,640,068 nodes) is bought this much
# over the nodes it shows, so the new search reliably wins the showcase.
# Small enough to stay on the 128M rung (ingest.RUNG_OVERSHOOT_TOLERANCE).
LOST_LINES_REBID_PER_MILLE = 2
# The widest line count a lost-lines position is held to: fewer legal moves
# than this and every one of them is enough.
LOST_LINES_WANTED = 5
# Places for adopted positions waiting, per account.  A position takes the
# weight of its backlog (one, or four for a 512M search), so the ceiling
# stays about the compute an account holds waiting: 500 searches of 128M, or
# 125 of 512M.
ADOPT_WAITING_MAX = 500
# Rows read per batch beyond what it asks for, so the ones dropped on the way
# do not leave a batch short; and a ceiling on all the rows one click reads.
ADOPT_SLACK = 25
ADOPT_SCAN_MAX = 600

# Why a row left the list without a request.
CLOSED = 'closed'      # the position closed, or its branch is dead
DONE = 'done'          # it no longer qualifies: already searched again
QUEUED = 'queued'      # its next search is already queued or running
DEEP = 'deep'          # its shown search ran more than the rung could beat

COUNTS_CACHE_KEY = 'atomicdb:backlog:counts'
COUNTS_CACHE_SECONDS = 30
KEY_RE = re.compile(r'^[0-9a-f]{64}$')

_OPEN = {'status': 'UNKNOWN', 'priority__gt': ingest.DEAD / 2}


# ---------------- what qualifies ----------------

def _current_lines(last_analysis):
    return [line for line in (last_analysis or [])
            if isinstance(line, dict) and not line.get('prior_pass')]


def showcase_budget(last_analysis):
    """The nodes the shown search ran, as ``ingest_analysis`` compares them."""
    current = _current_lines(last_analysis)
    budget = current[0].get('_budget') if current else None
    return budget if isinstance(budget, int) else 0


def lines_short(fen, last_analysis):
    """Does the stored search show fewer lines than it should?

    Counted the way the list was found (``find_dropped.py``): every stored
    line, against five or the number of legal moves if that is fewer.
    """
    if not isinstance(last_analysis, list):
        return False
    try:
        legal = len(logic.legal_moves(fen))
    except Exception:       # noqa: BLE001 - an unreadable FEN is not short
        return False
    return len(last_analysis) < min(LOST_LINES_WANTED, legal)


def showcase_restricted(analysis_passes):
    """Was the shown search restricted to the moves still open?

    The server asks for ``searchmoves`` without the replies already proven,
    so such a search shows fewer lines on purpose: it is not short.  The
    newest pass that took the showcase says (``ingest.pass_summary``).
    """
    for entry in analysis_passes or []:
        if isinstance(entry, dict) and entry.get('showcase'):
            return bool(entry.get('restricted'))
    return False


def shallow_now(last_analysis):
    """Is the shown search a shallow 128M one that 128.64M can replace?"""
    searched = idle_repair.shallow_showcase(last_analysis)
    return searched is not None and searched < ADOPT_BUDGET


def clear_win_now(row):
    """Does the position still qualify for the +16 sweep?

    The sweep's own rule (§ ``sweep.qualifies``), on a row of ``adopt``.
    """
    return sweep.qualifies(SimpleNamespace(
        status=row['status'], visits=row['visits'],
        nodes_invested=row['nodes_invested'], fen=row['fen'],
        backed_eval=row['backed_eval'], eval_cp=row['eval_cp']))


def deep_value(backed_eval, eval_cp):
    """The value the tree shows, White's side, read as the sweep reads it."""
    return backed_eval if backed_eval is not None else eval_cp


def deep_favoured(stm_white, value):
    """Is ``value`` (White's side) +25 or more for the side to move?"""
    if value is None:
        return False
    return value >= DEEP_LOW_CP if stm_white else value <= -DEEP_LOW_CP


def deep_win_now(row):
    """Open, searched exactly twice, the side to move ahead by +25 or more."""
    return (row['status'] == 'UNKNOWN' and row['visits'] == DEEP_VISITS
            and deep_favoured(row['fen'].split()[1] == 'w',
                              deep_value(row['backed_eval'], row['eval_cp'])))


def deep_wins_q():
    """``deep_win_now`` in SQL, on rows annotated with ``deep_value``."""
    return (Q(status='UNKNOWN', visits=DEEP_VISITS,
              priority__gt=ingest.DEAD / 2)
            & ((Q(fen__contains=' w ') & Q(deep_value__gte=DEEP_LOW_CP))
               | (Q(fen__contains=' b ') & Q(deep_value__lte=-DEEP_LOW_CP))))


def best_line_shallow(last_analysis):
    """The nodes the shown search ran if it qualifies for the best line.

    ``None`` unless the shown search (the first current line, read as
    ``idle_repair.shallow_showcase`` reads it) ran fewer than 128M nodes,
    reached a depth under ``BEST_LINE_DEPTH`` and found no mate.  A line with
    no nodes or no depth on record does not qualify: not knowing is not the
    same as knowing it was shallow.
    """
    current = _current_lines(last_analysis)
    if not current:
        return None
    top = current[0]
    searched = top.get('_budget')
    if (not isinstance(searched, int) or searched <= 0
            or searched >= idle_repair.REPAIR_MIN_NODES):
        return None
    depth = ingest.info_fields(top.get('raw')).get('depth')
    if depth is None or depth >= BEST_LINE_DEPTH:
        return None
    score = top.get('eval_cp')
    if (top.get('mate') is not None or not isinstance(score, int)
            or abs(score) >= ingest.MATE_BAND):
        return None
    return searched


def _already_deep(keys):
    """The keys of ``keys`` a search of this backlog's arm already ran on."""
    if not keys:
        return set()
    return set(AnalysisTask.objects.filter(
        position_id__in=keys, arm=DEEP_ARM,
        state=AnalysisTask.TState.COMPLETED, nodes_searched__gt=0)
        .values_list('position_id', flat=True))


# The backlogs whose re-search bids over the nodes the shown search ran.
REBID = (LOST_LINES, CLEAR_WINS, DEEP_WINS)


def _base_budget(backlog):
    return DEEP_BUDGET if backlog == DEEP_WINS else ADOPT_BUDGET


def adopt_budget(backlog, last_analysis):
    """The nodes the adopted search of a position is bought at."""
    base = _base_budget(backlog)
    if backlog not in REBID:
        return base
    shown = showcase_budget(last_analysis)
    return max(base, shown * (1000 + LOST_LINES_REBID_PER_MILLE) // 1000)


def _off_rung(backlog, last_analysis):
    """Would the bid leave its rung?  Then a search there cannot replace it."""
    return (ingest._rung_matching(adopt_budget(backlog, last_analysis))
            > ingest._rung_matching(_base_budget(backlog)))


def _verdict(backlog, row, repaired):
    """``None`` if the position still qualifies, else why it is dropped."""
    if row is None:
        return CLOSED
    if backlog == BEST_LINE:
        # Searched again at 128M or deeper, or closed: done.  A search of
        # 128.64M always replaces one under 128M, so no repair record is
        # needed to keep a position from coming back.
        if best_line_shallow(row['last_analysis']) is None:
            return DONE
        if not ingest._still_reachable(row['key']):
            return CLOSED
        return None
    if backlog == DEEP_WINS:
        # ``repaired``: the positions a search of this arm already ran on.
        if not deep_win_now(row) or row['key'] in repaired:
            return DONE
        if _off_rung(backlog, row['last_analysis']):
            return DEEP
        if not ingest._still_reachable(row['key']):
            return CLOSED
        return None
    if backlog == CLEAR_WINS:
        # The drip's checks (§ ``sweep.drip``): a second visit means it was
        # searched again; every parent proven means the search no longer
        # reaches the tree (§ ``ingest._still_reachable``).
        if not clear_win_now(row):
            return DONE
        if _off_rung(backlog, row['last_analysis']):
            return DEEP
        if not ingest._still_reachable(row['key']):
            return CLOSED
        return None
    if backlog == LOST_LINES:
        # Its re-search only has to beat the nodes the shown one ran; it is
        # deep when that would leave the 128M rung.
        if _off_rung(backlog, row['last_analysis']):
            return DEEP
    elif showcase_budget(row['last_analysis']) >= ADOPT_BUDGET:
        return DEEP
    if backlog == LOST_LINES:
        if (not lines_short(row['fen'], row['last_analysis'])
                or showcase_restricted(row.get('analysis_passes'))):
            return DONE
    elif row['key'] in repaired or not shallow_now(row['last_analysis']):
        return DONE
    return None


def _multipv(backlog, visits):
    if backlog == LOST_LINES:
        return LOST_LINES_MULTIPV
    if backlog == CLEAR_WINS:
        return sweep.SWEEP_MULTIPV
    if backlog == DEEP_WINS:
        return DEEP_MULTIPV
    return ingest.multipv_for(visits, ADOPT_BUDGET)


# ---------------- the counts the page shows ----------------

def counts(fresh=False):
    """``{backlog: {'open': n, 'adopted': n, 'adopters': n}}``.

    One GROUP BY over the list, behind a short cache: the queue page is
    public and the numbers only move when someone adopts or a loader runs.
    """
    if not fresh:
        cached = cache.get(COUNTS_CACHE_KEY)
        if cached is not None:
            return cached
    out = {name: {'open': 0, 'adopted': 0, 'adopters': 0} for name in ORDER}
    for row in (BacklogItem.objects.values('backlog', 'state')
                .annotate(n=Count('id'),
                          people=Count('adopted_by', distinct=True))):
        entry = out.get(row['backlog'])
        if entry is None:
            continue
        if row['state'] == BacklogItem.State.OPEN:
            entry['open'] = row['n']
        elif row['state'] == BacklogItem.State.ADOPTED:
            entry['adopted'] = row['n']
            entry['adopters'] = row['people']
    cache.set(COUNTS_CACHE_KEY, out, COUNTS_CACHE_SECONDS)
    return out


def adopted_waiting(username):
    """The places this account's waiting adopted requests take.

    One per position, or its backlog's weight (§ ``weight_of``): a 512M
    search takes four.  Counted against ``ADOPT_WAITING_MAX``.
    """
    if not username:
        return 0
    rows = (AnalysisTask.objects
            .filter(state=AnalysisTask.TState.PENDING,
                    source=AnalysisTask.Source.USER,
                    requested_by=username, arm__in=ARMS,
                    position__status='UNKNOWN')
            .order_by().values('arm').annotate(n=Count('id')))
    return sum(ARM_WEIGHTS.get(row['arm'], 1) * row['n'] for row in rows)


def heavy_backlogs():
    """``[{'title', 'weight'}]`` of the backlogs whose positions take more
    than one place, for the box's note."""
    return [{'title': BACKLOGS[name]['title'], 'weight': weight_of(name)}
            for name in ORDER if weight_of(name) > 1]


def boxes():
    """What the queue page's box shows, one entry per backlog."""
    numbers = counts()
    return [{'name': name, **BACKLOGS[name], 'sizes': sizes_of(name),
             'default_size': default_size_of(name), **numbers[name]}
            for name in ORDER]


# ---------------- adopting a batch ----------------

@dataclass
class Adoption:
    status: str
    adopted: int = 0
    dropped: int = 0
    task_ids: list = field(default_factory=list)


def _mint(key, visits, multipv, arm, username, now, budget=ADOPT_BUDGET):
    """Queue the adopted search of ``key``.  The task, or ``None``.

    The slot is the next visit, like every producer's.  What already sits
    there decides: a withdrawn or absorbed row comes back as this request
    (the slot can hold no other), a search the server queued and nobody has
    taken becomes this request, and anything else (somebody's request, or a
    search already running) is left alone.
    """
    task, created = AnalysisTask.objects.get_or_create(
        position_id=key, generation=visits,
        defaults={'budget_nodes': budget, 'multipv': multipv,
                  'source': AnalysisTask.Source.USER,
                  'requested_by': username, 'requested_at': now,
                  'arm': arm})
    if created:
        return task
    if (task.state == AnalysisTask.TState.CANCELLED
            or ingest.is_absorbed(task)):
        ingest._revive_withdrawn(task, budget, multipv, username)
        task.arm = arm
        task.save(update_fields=['arm'])
        return task
    if (task.state == AnalysisTask.TState.PENDING
            and task.source != AnalysisTask.Source.USER
            and task.requested_by in ('', username)):
        task.source = AnalysisTask.Source.USER
        task.requested_by = username
        task.budget_nodes = max(task.budget_nodes, budget)
        task.requested_at = now
        fields = ['source', 'requested_by', 'budget_nodes', 'requested_at']
        if not task.arm:
            task.arm = arm
            fields.append('arm')
        task.save(update_fields=fields)
        return task
    return None


def _queue_room(username):
    """``(room in the account's allowance, refusal status or '')``."""
    from .views import REQUEST_QUEUE_MAX, _queue_full_payload

    waiting = (AnalysisTask.objects
               .filter(state=AnalysisTask.TState.PENDING,
                       source=AnalysisTask.Source.USER,
                       requested_by=username, position__status='UNKNOWN')
               .count())
    room = REQUEST_QUEUE_MAX - waiting
    if room <= 0:
        _queue_full_payload(username)   # the same receipt as any refusal
        return 0, 'queue-full'
    return room, ''


def adopt(username, backlog, size):
    """Adopt up to ``size`` positions of ``backlog`` for ``username``."""
    if not username:
        return Adoption('sign-in')
    if backlog not in BACKLOGS or size not in sizes_of(backlog):
        return Adoption('bad-request')
    room, refused = _queue_room(username)
    if refused:
        return Adoption(refused)
    adopt_room = ((ADOPT_WAITING_MAX - adopted_waiting(username))
                  // weight_of(backlog))
    if adopt_room <= 0:
        return Adoption('adopted-full')
    want = min(size, room, adopt_room)
    arm = BACKLOGS[backlog]['arm']
    outcome = Adoption('adopted')
    now = timezone.now()
    scanned = 0
    with atomic():
        while outcome.adopted < want and scanned < ADOPT_SCAN_MAX:
            take = min(want - outcome.adopted + ADOPT_SLACK,
                       ADOPT_SCAN_MAX - scanned)
            # SKIP LOCKED: two people adopting at once take different rows
            # instead of queueing behind each other.  (No-op on SQLite.)
            items = list(BacklogItem.objects.select_for_update(
                skip_locked=True).filter(
                    backlog=backlog, state=BacklogItem.State.OPEN)
                .order_by('id')[:take])
            if not items:
                break
            scanned += len(items)
            keys = [item.position_key for item in items]
            rows = {row['key']: row for row in Position.objects.filter(
                key__in=keys, **_OPEN).values(
                    'key', 'fen', 'status', 'visits', 'nodes_invested',
                    'eval_cp', 'backed_eval', 'last_analysis',
                    'analysis_passes')}
            if backlog == SHALLOW:
                repaired = idle_repair._already_repaired(list(rows))
            elif backlog == DEEP_WINS:
                repaired = _already_deep(list(rows))
            else:
                repaired = set()
            for item in items:
                if outcome.adopted >= want:
                    break
                row = rows.get(item.position_key)
                reason = _verdict(backlog, row, repaired)
                task = None
                if reason is None:
                    task = _mint(item.position_key, row['visits'],
                                 _multipv(backlog, row['visits']), arm,
                                 username, now,
                                 adopt_budget(backlog, row['last_analysis']))
                    if task is None:
                        reason = QUEUED
                if task is not None:
                    item.state = BacklogItem.State.ADOPTED
                    item.adopted_by = username
                    item.adopted_at = now
                    item.task_id = task.pk
                    item.save(update_fields=['state', 'adopted_by',
                                             'adopted_at', 'task_id'])
                    outcome.adopted += 1
                    outcome.task_ids.append(task.pk)
                else:
                    item.state = BacklogItem.State.DROPPED
                    item.reason = reason
                    item.save(update_fields=['state', 'reason'])
                    outcome.dropped += 1
    if not outcome.adopted:
        outcome.status = 'empty'
    cache.delete(COUNTS_CACHE_KEY)
    return outcome


QUEUE_PAGE = '/atomicdb/queue/'


def api_adopt(request):
    """The box's Adopt buttons: a plain form POST, answered with the page.

    Scripts may send ``format=json`` for the outcome as JSON instead.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    wants_json = request.POST.get('format') == 'json'
    if not request.user.is_authenticated:
        if wants_json:
            return JsonResponse({'status': 'sign-in'}, status=401)
        return redirect(f'/login/?next={quote(QUEUE_PAGE + "#backlog")}')
    backlog = request.POST.get('backlog', '')
    try:
        size = int(request.POST.get('size', ''))
    except ValueError:
        size = 0
    outcome = adopt(request.user.username, backlog, size)
    if wants_json:
        code = {'bad-request': 400, 'queue-full': 503}.get(outcome.status,
                                                           200)
        return JsonResponse({'status': outcome.status,
                             'adopted': outcome.adopted,
                             'dropped': outcome.dropped}, status=code)
    query = urlencode({'adopt': outcome.status, 'backlog': backlog,
                       'n': outcome.adopted, 'dropped': outcome.dropped})
    return redirect(f'{QUEUE_PAGE}?{query}#backlog')


def receipt(params):
    """The sentence the box shows after a click, from the redirect's query."""
    status = params.get('adopt')
    if not status:
        return None
    try:
        adopted = int(params.get('n', 0))
        dropped = int(params.get('dropped', 0))
    except ValueError:
        return None
    title = BACKLOGS.get(params.get('backlog', ''), {}).get('title', '')
    return {'status': status, 'adopted': adopted, 'dropped': dropped,
            'title': title, 'waiting_max': ADOPT_WAITING_MAX}


# ---------------- subscriptions ----------------

# The default of a backlog with the usual sizes (§ ``default_size_of``).
SUBSCRIBE_DEFAULT_SIZE = 50
# Topped up when fewer than batch // TOPUP_DIVISOR adopted positions of that
# backlog are waiting: one batch.  A pass then adopts batches until about
# TOPUP_BATCHES_PER_PASS batches are waiting.  Half a batch every five minutes
# ran dry: Torom's workers finish ~20 positions a minute (15-Nov-2026), so
# the queue emptied between passes.
TOPUP_DIVISOR = 1
TOPUP_BATCHES_PER_PASS = 2
# One pass, every five minutes: at most this many subscriptions, and no new
# one started after this many seconds.  The least recently checked go first,
# so a pass cut short leaves the rest for the next one.
TOPUP_MAX_SUBSCRIPTIONS = 40
TOPUP_SECONDS = 30.0

# What the last top-up found (``BacklogSubscription.last_status``).
SUB_ADOPTED = 'adopted'        # it adopted a batch
SUB_ENOUGH = 'enough'          # enough adopted positions were still waiting
SUB_EMPTY = 'empty'            # the backlog ran out: turned off
SUB_STOPPED = 'stopped'        # the account turned it off
SUB_ACCOUNT = 'account-off'    # the account is disabled: turned off
SUB_ERROR = 'error'            # this one failed; the next pass tries again
# ``adopt`` refusals ('queue-full', 'adopted-full') are stored as they are:
# the subscription stays on and the next pass tries again.


def backlog_waiting(username, backlog):
    """This account's adopted requests from ``backlog`` still waiting."""
    return (AnalysisTask.objects
            .filter(state=AnalysisTask.TState.PENDING,
                    source=AnalysisTask.Source.USER,
                    requested_by=username, arm=BACKLOGS[backlog]['arm'],
                    position__status='UNKNOWN')
            .count())


def topup_below(batch_size):
    return batch_size // TOPUP_DIVISOR


def subscriptions_of(username):
    """``{backlog: BacklogSubscription}`` of this account, on or off."""
    if not username:
        return {}
    return {sub.backlog: sub for sub in
            BacklogSubscription.objects.filter(username=username,
                                               backlog__in=ORDER)}


def subscribe(username, backlog, size):
    """Turn on (or resize) ``username``'s subscription to ``backlog``."""
    if not username:
        return 'sign-in'
    if backlog not in BACKLOGS or size not in sizes_of(backlog):
        return 'bad-request'
    BacklogSubscription.objects.update_or_create(
        username=username, backlog=backlog,
        defaults={'batch_size': size, 'active': True, 'last_status': ''})
    return 'subscribed'


def unsubscribe(username, backlog):
    """Turn ``username``'s subscription to ``backlog`` off."""
    if not username:
        return 'sign-in'
    if backlog not in BACKLOGS:
        return 'bad-request'
    BacklogSubscription.objects.filter(
        username=username, backlog=backlog, active=True).update(
            active=False, last_status=SUB_STOPPED)
    return 'unsubscribed'


def _enabled_accounts(usernames):
    from django.contrib.auth.models import User

    return set(User.objects.filter(username__in=usernames, is_active=True)
               .values_list('username', flat=True))


def _top_up_one(sub, enabled, has_open):
    """``(status, adopted, still active)`` for one subscription."""
    if (sub.backlog not in BACKLOGS
            or sub.batch_size not in sizes_of(sub.backlog)):
        return 'bad-request', 0, False
    if sub.username not in enabled:
        return SUB_ACCOUNT, 0, False
    if not has_open(sub.backlog):
        return SUB_EMPTY, 0, False
    waiting = backlog_waiting(sub.username, sub.backlog)
    if waiting >= topup_below(sub.batch_size):
        return SUB_ENOUGH, 0, True
    adopted, status = 0, SUB_ENOUGH
    for _ in range(TOPUP_BATCHES_PER_PASS):
        if waiting + adopted >= TOPUP_BATCHES_PER_PASS * sub.batch_size:
            break
        outcome = adopt(sub.username, sub.backlog, sub.batch_size)
        if outcome.status == 'empty':
            # Every row it read had left the list; off only if none is left.
            if not adopted and not has_open(sub.backlog, fresh=True):
                return SUB_EMPTY, 0, False
            break
        if not outcome.adopted:
            # A refusal ('queue-full', 'adopted-full') is the status only
            # when nothing was adopted in this pass.
            if not adopted:
                status = outcome.status
            break
        status = outcome.status
        adopted += outcome.adopted
    return status, adopted, True


def top_up(max_subscriptions=None, seconds=None):
    """Adopt the next batch for every subscription that is running low.

    Bounded: at most ``max_subscriptions`` rows, the least recently checked
    first, and none started after ``seconds``.  A subscription that fails is
    marked and skipped; nothing raised here escapes.  Returns a summary.
    """
    limit = TOPUP_MAX_SUBSCRIPTIONS if max_subscriptions is None \
        else max_subscriptions
    budget = TOPUP_SECONDS if seconds is None else seconds
    started = time.monotonic()
    summary = {'checked': 0, 'topped_up': 0, 'adopted': 0, 'ended': 0,
               'errors': 0, 'deferred': 0}
    try:
        subs = list(BacklogSubscription.objects.filter(active=True)
                    .order_by(F('checked_at').asc(nulls_first=True), 'id')
                    [:limit])
        enabled = _enabled_accounts({sub.username for sub in subs}) \
            if subs else set()
    except Exception:                       # noqa: BLE001
        log.exception('backlog top-up: could not read the subscriptions')
        summary['errors'] += 1
        return summary
    open_now = {}

    def has_open(name, fresh=False):
        if fresh or name not in open_now:
            open_now[name] = BacklogItem.objects.filter(
                backlog=name, state=BacklogItem.State.OPEN).exists()
        return open_now[name]

    for index, sub in enumerate(subs):
        if time.monotonic() - started > budget:
            summary['deferred'] = len(subs) - index
            break
        summary['checked'] += 1
        try:
            status, adopted, active = _top_up_one(sub, enabled, has_open)
        except Exception:                   # noqa: BLE001
            log.exception('backlog top-up: %s on %s failed',
                          sub.username, sub.backlog)
            status, adopted, active = SUB_ERROR, 0, True
            summary['errors'] += 1
        if adopted:
            summary['topped_up'] += 1
            summary['adopted'] += adopted
        if not active:
            summary['ended'] += 1
        try:
            # Only if it is still on: an untick during the pass wins.
            BacklogSubscription.objects.filter(pk=sub.pk, active=True).update(
                checked_at=timezone.now(), last_status=status, active=active,
                adopted_total=F('adopted_total') + adopted)
        except Exception:                   # noqa: BLE001
            log.exception('backlog top-up: could not record %s on %s',
                          sub.username, sub.backlog)
            summary['errors'] += 1
    return summary


def api_subscribe(request):
    """The box's Subscribe and Unsubscribe buttons: a form POST.

    Scripts may send ``format=json`` for the outcome as JSON instead.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    wants_json = request.POST.get('format') == 'json'
    if not request.user.is_authenticated:
        if wants_json:
            return JsonResponse({'status': 'sign-in'}, status=401)
        return redirect(f'/login/?next={quote(QUEUE_PAGE + "#backlog")}')
    backlog = request.POST.get('backlog', '')
    username = request.user.username
    if request.POST.get('action') == 'unsubscribe':
        status = unsubscribe(username, backlog)
    else:
        default = (default_size_of(backlog) if backlog in BACKLOGS
                   else SUBSCRIBE_DEFAULT_SIZE)
        try:
            size = int(request.POST.get('size', default))
        except ValueError:
            size = 0
        status = subscribe(username, backlog, size)
    if wants_json:
        return JsonResponse({'status': status},
                            status=400 if status == 'bad-request' else 200)
    query = urlencode({'sub': status, 'backlog': backlog})
    return redirect(f'{QUEUE_PAGE}?{query}#backlog')


def subscription_receipt(params):
    """The sentence the box shows after a subscription click."""
    status = params.get('sub')
    if status not in ('subscribed', 'unsubscribed', 'bad-request'):
        return None
    title = BACKLOGS.get(params.get('backlog', ''), {}).get('title', '')
    return {'status': status, 'title': title}


def _ago(moment):
    """"3 minutes ago", in one unit, or "just now"; '' for never."""
    if moment is None:
        return ''
    from django.utils.timesince import timesince
    text = timesince(moment, now=timezone.now(), depth=1)
    return 'just now' if text.split()[:1] == ['0'] else f'{text} ago'


def subscription_rows(username):
    """What a subscriber sees of their subscriptions, one per backlog."""
    subs = subscriptions_of(username)
    rows = []
    for name in ORDER:
        sub = subs.get(name)
        if sub is None:
            continue
        rows.append({'name': name, 'title': BACKLOGS[name]['title'],
                     'active': sub.active, 'batch_size': sub.batch_size,
                     'below': topup_below(sub.batch_size),
                     'status': sub.last_status,
                     'checked': _ago(sub.checked_at),
                     'adopted_total': sub.adopted_total,
                     'waiting': (backlog_waiting(username, name)
                                 if sub.active else None)})
    return rows


# ---------------- filling the lists ----------------

def load_keys(backlog, keys, chunk=2_000):
    """Add ``keys`` to ``backlog``.  ``(added, already there, invalid)``.

    Idempotent: a key already on the list, in any state, is left as it is.
    """
    if backlog not in BACKLOGS:
        raise ValueError(f'unknown backlog {backlog!r}')
    valid, invalid = [], 0
    seen = set()
    for key in keys:
        key = key.strip().lower()
        if not key:
            continue
        if not KEY_RE.match(key):
            invalid += 1
            continue
        if key not in seen:
            seen.add(key)
            valid.append(key)
    added = 0
    for start in range(0, len(valid), chunk):
        part = valid[start:start + chunk]
        known = set(BacklogItem.objects.filter(
            backlog=backlog, position_key__in=part).values_list(
                'position_key', flat=True))
        fresh = [BacklogItem(backlog=backlog, position_key=key)
                 for key in part if key not in known]
        BacklogItem.objects.bulk_create(fresh, ignore_conflicts=True)
        added += len(fresh)
    if added:
        cache.delete(COUNTS_CACHE_KEY)
    return added, len(valid) - added, invalid


def load_clear_wins(keys, chunk=2_000):
    """Add the sweep's keys that still qualify, in their order.

    ``keys`` is the sweep's list (§ ``sweep.build_plan``: White's strategy
    tree first), and the rows keep that order, which is the order a batch
    takes them in.  Each key is read again and left out if its position no
    longer qualifies (§ ``sweep.qualifies``); adopting checks it once more.
    Idempotent, so a run cut short is resumed by running it again.  Returns
    ``(added, already there, left out)``.
    """
    added = known = left_out = 0
    keys = list(dict.fromkeys(key.strip().lower() for key in keys
                              if isinstance(key, str)))
    for start in range(0, len(keys), chunk):
        part = keys[start:start + chunk]
        rows = {pos.key: pos for pos in Position.objects.filter(
            key__in=part).only('key', 'fen', 'status', 'visits',
                               'nodes_invested', 'eval_cp', 'backed_eval')}
        keep = [key for key in part
                if key in rows and sweep.qualifies(rows[key])]
        left_out += len(part) - len(keep)
        if keep:
            fresh, there, invalid = load_keys(CLEAR_WINS, keep, chunk)
            added += fresh
            known += there
            left_out += invalid
    return added, known, left_out


def scan_shallow_step(cursor, batch, stop_below=0, dry_run=False):
    """One step of the shallow scan, down the completed searches by id.

    Reads the searches of 128M or more with ids in ``[cursor - batch,
    cursor)``, then their positions by key: an index range and a lookup,
    never a scan of the tree.  Adds the open positions whose shown search is
    a shallow 128M one, not repaired yet.  Returns ``(next cursor, searches
    read, positions added)``; the next cursor is where the following step
    starts.  ``dry_run`` adds nothing and counts what it would add.
    """
    floor = max(stop_below, cursor - batch, 0)
    keys = list(dict.fromkeys(AnalysisTask.objects.filter(
        id__lt=cursor, id__gte=floor, state=AnalysisTask.TState.COMPLETED,
        nodes_searched__gte=idle_repair.REPAIR_MIN_NODES)
        .order_by('-id').values_list('position_id', flat=True)))
    found = []
    if keys:
        for key, showcase in (Position.objects.filter(key__in=keys, **_OPEN)
                              .values_list('key', 'last_analysis')):
            if shallow_now(showcase):
                found.append(key)
        repaired = idle_repair._already_repaired(found)
        found = [key for key in found if key not in repaired]
    if dry_run:
        known = set(BacklogItem.objects.filter(
            backlog=SHALLOW, position_key__in=found).values_list(
                'position_key', flat=True)) if found else set()
        return floor, len(keys), len(set(found) - known)
    added = load_keys(SHALLOW, found)[0] if found else 0
    return floor, len(keys), added


def scan_shallow(cursor=None, batch=2_000, pause=0.5, max_steps=None,
                 stop_below=0, on_step=None, dry_run=False):
    """Walk ``scan_shallow_step`` from ``cursor`` down to ``stop_below``.

    ``cursor`` ``None`` starts above the newest task.  ``on_step`` is called
    after each step with ``(cursor, read, added)`` so a caller can save where
    it stands; ``pause`` seconds pass between steps.  Returns the totals.
    """
    if cursor is None:
        cursor = 1 + (AnalysisTask.objects.order_by('-id')
                      .values_list('id', flat=True).first() or 0)
    steps = read_total = added_total = 0
    while cursor > stop_below:
        if max_steps is not None and steps >= max_steps:
            break
        cursor, read, added = scan_shallow_step(cursor, batch, stop_below,
                                                dry_run=dry_run)
        steps += 1
        read_total += read
        added_total += added
        if on_step is not None:
            on_step(cursor, read, added)
        if cursor > stop_below and pause:
            time.sleep(pause)
    return {'cursor': cursor, 'steps': steps, 'read': read_total,
            'added': added_total}


def scan_deep_step(cursor, batch, stop_below=0):
    """One step of the deep-wins scan, down the completed searches by id.

    Reads the completed searches with ids in ``[cursor - batch, cursor)``,
    then their positions by key, keeping the ones that qualify now
    (§ ``deep_wins_q``): an index range and a lookup, never a scan of the
    tree.  Adds nothing.  Returns ``(next cursor, positions read, found)``,
    ``found`` a list of ``(key, value, selector priority)``.
    """
    floor = max(stop_below, cursor - batch, 0)
    keys = list(dict.fromkeys(AnalysisTask.objects.filter(
        id__lt=cursor, id__gte=floor, state=AnalysisTask.TState.COMPLETED)
        .order_by('-id').values_list('position_id', flat=True)))
    found = []
    if keys:
        found = list(Position.objects.filter(key__in=keys)
                     .annotate(deep_value=Coalesce('backed_eval', 'eval_cp'))
                     .filter(deep_wins_q())
                     .values_list('key', 'deep_value', 'priority'))
    return floor, len(keys), found


def deep_tree_match(visits, nodes_invested, value):
    """The deep-wins rule for a White-to-move position of the tree walk."""
    return visits == DEEP_VISITS and deep_favoured(True, value)


def order_deep_wins(found, tree=(), below_mate=False):
    """The order the deep-wins list is loaded in, which a batch takes.

    ``found`` maps a key to ``(value, priority)``.  First the keys of
    ``tree`` (a walk of White's strategy tree, § ``sweep.walk_strategy_tree``)
    in the order the walk met them; then the rest, the highest value for the
    side to move first, the selector's priority breaking ties.
    ``below_mate`` leaves out the values in the mate band.
    """
    if below_mate:
        found = {key: entry for key, entry in found.items()
                 if abs(entry[0]) < ingest.MATE_BAND}
    head = [key for key in dict.fromkeys(tree) if key in found]
    seen = set(head)
    rest = sorted((key for key in found if key not in seen),
                  key=lambda key: (-abs(found[key][0]),
                                   -(found[key][1] or 0.0), key))
    return head + rest


def _never(visits, nodes_invested, value):
    return False


def walk_best_line(on_level, deadline=None, max_plies=None, max_nodes=None):
    """Walk White's strategy tree, handing every ply's open positions to
    ``on_level``.  The sweep's walk (§ ``sweep.walk_strategy_tree``), which
    keeps nothing of its own here.  Returns its stats."""
    _found, stats = sweep.walk_strategy_tree(
        max_nodes=sweep.TREE_MAX_NODES if max_nodes is None else max_nodes,
        max_plies=sweep.TREE_MAX_PLIES if max_plies is None else max_plies,
        deadline=deadline, match=_never, on_level=on_level)
    return stats


def best_line_check(keys):
    """The keys of ``keys`` that qualify for the best line now, in order.

    One lookup by key; open positions only, as ``adopt`` reads them.
    """
    if not keys:
        return []
    shown = dict(Position.objects.filter(key__in=keys, **_OPEN)
                 .values_list('key', 'last_analysis'))
    return [key for key in keys
            if key in shown and best_line_shallow(shown[key]) is not None]
