"""The idle fleet searches again the positions whose value nobody can trust.

A community proposal for the time nobody is requesting analysis: repair the
open positions the tree paid for badly, or never paid for where a line needs
them.  Two kinds qualify, and both are read off what a position stores:

* A SHALLOW SHOWCASE.  The search a position shows (the current lines of
  ``last_analysis``, the pass ``ingest_analysis`` let decide its eval) ran at
  least 128M nodes, the engine reported a depth under 18 and the score is not
  a mate.  A fixed node budget split over many threads is shallow.  Measured
  on the 128M searches with five lines served from mid-September to late
  October: depth 22 at the median on engines of up to 8 threads, 19 on 9 to
  16 and 16 on 17 or more, where four searches in five stayed under 18.  The
  width costs depth as well: on small engines 9% of them stayed under 18.
* A CONNECTOR.  An open position with no search of its own that sits between
  a searched parent and a searched child.  Its numbers are only what climbs
  from below, so one unsearched link can hold a whole line still.  "No search
  of its own" is ``visits == 0``: no analysis was ever applied to it.  Not
  ``searched_budget``, which asks the task table the same question one query
  per position and answers "searched" for a result whose ingest failed and
  never reached the tree; and not ``_completed_max_budget``, which counts the
  rows absorbed without a search.

WHEN THEY ARE FOUND.  When a search lands (``queue_from_landing``, at the end of
``ingest_analysis``): the landed position is checked for a shallow showcase,
which costs no query, and its parents for connectors, one indexed read of a
handful of rows.  What landed before this arm existed, or while its queue was
full, is reached by a slow walk round the completed searches
(``drip_backlog``, one bounded step per tick of the selector service).

WHAT IS BOUGHT.  One search per position: a repair that ran, or one still
waiting, is never bought again, so a repair that comes out shallow itself
cannot start a loop.  It goes in the AUTO band, behind every visitor's
request, and only a slot that reports at most ``REPAIR_MAX_THREADS`` threads
per engine is handed one (``fits_slot``, read by ``api_lease``): the same wide
engine would only buy the same shallow search again.  A slot that misreports
its threads changes nothing but whether it gets these tasks.

HOW MUCH.  Its own cap of PENDING tasks and its own hourly ceiling, both
counted on its own arm only, the pattern of the walker's arms (§ ingest,
``_arm_budget_room``).  The walk down the backlog may fill only half of each,
so a fresh landing always finds room.  ``ATOMICDB_ARM_RATE_REPAIR=0`` turns
the whole arm off.

WHAT IT NEVER DOES: decide a value.  It only chooses which searches to buy.
How a search is compared with the one before it when it lands, and what
climbs from it, is ``ingest_analysis``'s business, untouched.
"""

import json
import logging
import os

from django.conf import settings
from django.db.models import Q

from . import ingest
from .database import atomic
from .models import AnalysisTask, DBEvent, Edge, Position

logger = logging.getLogger(__name__)

REPAIR_ARM = ingest.IDLE_REPAIR_ARM
# "Despite reaching 128M".  The exploration rung and not the floor of
# server-picked work: rolling that floor back to 0 must not make every
# search a candidate.
REPAIR_MIN_NODES = ingest.BUDGET_LADDER[2]
REPAIR_SHALLOW_DEPTH = 18
# The widest search a repair buys again.  A shallow 2B needs an engine of
# hundreds of threads, and repeating it costs sixteen repairs of 128M.
REPAIR_MAX_RUNG = ingest.BUDGET_LADDER[3]
# A REPAIR OUTBIDS WHAT IT REPAIRS, BY A HAIR.  ``ingest_analysis`` lets a pass
# decide the showcase only if it ran at least as many nodes as the one it
# replaces, counted as the engine reported them, and an engine stops a little
# past its budget: measured on 128M searches, a median of 1,500 nodes past it
# on engines of up to 8 threads (3,600 at the 90th percentile) and 10,000 on
# engines of 17 or more (91,384 at most).  Bought at exactly the same budget,
# the repair would lose that comparison to the wide search it repairs most of
# the time and land in the pass history only.  Five per mille more (640,000
# nodes on 128M) clears the worst overshoot measured seven times over and
# stays inside ``RUNG_OVERSHOOT_TOLERANCE``, so every reader of the ladder
# still sees the rung it repairs.  In whole numbers, so 128M buys exactly
# 128,640,000.
REPAIR_OVERBID_PER_MILLE = 5
# And it searches two lines, the house width for a revisit whose purpose is
# depth (§ ``ingest.multipv_for``): at five lines even small engines left 9%
# of the 128M searches under 18, and a position that came out shallow once is
# the likeliest to do it again at the same width.
REPAIR_MULTIPV = ingest.DEPTH_MULTIPV
# The burst brake: how many repairs may wait at once.  Counted on this arm
# only, so neither the pool nor another arm can take its room, and it takes
# none of theirs (§ ``ingest.top_up_analysis_pool``).
REPAIR_QUEUE_CAP = 16
# The walk down the backlog stops at half of it, and at half of the hourly
# ceiling: the other halves are kept for what lands from now on.
REPAIR_BACKLOG_TARGET = REPAIR_QUEUE_CAP // 2
# Parents of a landed position looked at for connectors.  Measured: 96% of
# positions have one parent and the most seen in 46,000 searched ones was 13.
REPAIR_PARENTS_MAX = 16
# Threads per engine of the slots that may take a repair.  The measurements
# above put the shallow searches past this.
REPAIR_MAX_THREADS = 8
REPAIR_EVENT = 'IDLE_REPAIR'
# One step of the backlog walk: at most this many completed searches, and
# never further down the task ids than this, whatever it found.
REPAIR_BACKLOG_SCAN = 200
REPAIR_BACKLOG_WINDOW = 2_000

SHALLOW = 'shallow'
CONNECTOR = 'connector'

_OPEN = {'status': 'UNKNOWN', 'priority__gt': ingest.DEAD / 2}


def fits_slot(task, threads):
    """May a slot running ``threads`` threads per engine take ``task``?

    Only a repair the server picked is routed.  A visitor who asks for the
    same position promotes the waiting repair to their own request
    (§ ``ingest._request_rung``), and a request goes to whoever is free.  A
    slot that reports no threads at all is not trusted with one: nothing says
    its engine is narrow.
    """
    if task.arm != REPAIR_ARM or task.source != AnalysisTask.Source.AUTO:
        return True
    return 0 < threads <= REPAIR_MAX_THREADS


def shallow_showcase(last_analysis):
    """The nodes a shallow showcase ran, or ``None`` if it is not one.

    The showcase is the first current line of ``last_analysis``: it carries
    the nodes of its pass (``_budget``) and the engine's own ``info`` line.  A
    line with no depth on record is not shallow: not knowing how deep a search
    went is not the same as knowing it was shallow (§ ``ingest.info_fields``).
    """
    top = next((line for line in (last_analysis or [])
                if isinstance(line, dict) and not line.get('prior_pass')),
               None)
    if top is None:
        return None
    searched = top.get('_budget')
    if not isinstance(searched, int) or searched < REPAIR_MIN_NODES:
        return None
    depth = ingest.info_fields(top.get('raw')).get('depth')
    if depth is None or depth >= REPAIR_SHALLOW_DEPTH:
        return None
    score = top.get('eval_cp')
    if (top.get('mate') is not None or not isinstance(score, int)
            or abs(score) >= ingest.MATE_BAND):
        return None
    return searched


def repair_budget(searched):
    """What the repair of a showcase that ran ``searched`` nodes buys.

    ``None`` when there is nothing worth buying: a rung above
    ``REPAIR_MAX_RUNG``, or a search reported so far past its rung that the
    overbid would not clear it.
    """
    rung = ingest._rung_matching(searched)
    if rung > REPAIR_MAX_RUNG:
        return None
    budget = rung * (1000 + REPAIR_OVERBID_PER_MILLE) // 1000
    return budget if budget > searched else None


def _connector_price():
    """A connector's first search: the price of server-picked work, with
    the width of a first look, which informs its whole level of children."""
    budget = ingest.server_picked_budget(REPAIR_MIN_NODES)
    return budget, ingest.multipv_for(0, budget)


def _linked_to_searched(candidates):
    """The candidates that have an open, searched parent of their own."""
    if not candidates:
        return set()
    return set(Edge.objects.filter(
        child_id__in=sorted(candidates), parent__status='UNKNOWN',
        parent__visits__gt=0).values_list('child_id', flat=True))


def connector_parents(parent_keys):
    """The connectors among the parents of a position that was just searched.

    Two indexed reads at most, both over a handful of rows: the parents by
    key, and the parents of the unsearched ones through their edges.
    """
    keys = sorted({key for key in parent_keys if key})[:REPAIR_PARENTS_MAX]
    if not keys:
        return []
    unsearched = list(Position.objects.filter(
        key__in=keys, visits=0, **_OPEN).values_list('key', flat=True))
    return sorted(_linked_to_searched(unsearched))


def _connectors_of(child_keys):
    """``{searched position: [its connector parents]}`` for a batch."""
    rows = list(Edge.objects.filter(
        child_id__in=child_keys, parent__status='UNKNOWN', parent__visits=0,
        parent__priority__gt=ingest.DEAD / 2).values_list(
            'child_id', 'parent_id'))
    linked = _linked_to_searched({parent for _child, parent in rows})
    found = {}
    for child, parent in rows:
        if parent in linked:
            found.setdefault(child, []).append(parent)
    return found


def _already_repaired(keys):
    """The positions among ``keys`` that have a repair that ran or waits.

    One that was absorbed, because another search covered it before a worker
    took it, does not count: that repair never searched anything.
    """
    if not keys:
        return set()
    return set(AnalysisTask.objects.filter(
        position_id__in=sorted(keys), arm=REPAIR_ARM).filter(
            Q(state__in=(AnalysisTask.TState.PENDING,
                         AnalysisTask.TState.LEASED))
            | Q(state=AnalysisTask.TState.COMPLETED, nodes_searched__gt=0))
        .values_list('position_id', flat=True))


def _room(cap, reserve=0):
    """``(how many repairs may be bought now, how many wait)``.

    The waiting ones are counted on open positions only: a repair left on a
    position that closed is never served, and must not hold a place.
    ``reserve`` is the part of the hourly ceiling this buyer leaves to others.
    """
    pending = AnalysisTask.objects.filter(
        state=AnalysisTask.TState.PENDING, arm=REPAIR_ARM,
        position__status='UNKNOWN').count()
    room = cap - pending
    if room <= 0:
        return 0, pending
    hourly = ingest._arm_budget_room(REPAIR_ARM) - reserve
    return max(0, min(room, hourly)), pending


def _mint(targets, room):
    """Create the repairs of ``targets`` until ``room`` runs out.

    Each target is ``(key, kind, budget, multipv)``.  The position is read
    again first, because it may have closed, or a connector been searched,
    since it was found.  The generation is its next visit, the slot every
    producer queues on: a task already there, whoever queued it, is the next
    search of the position and the repair is not added beside it.
    """
    made = []
    for key, kind, budget, multipv in targets:
        if len(made) >= room:
            break
        visits = (Position.objects.filter(key=key, **_OPEN)
                  .values_list('visits', flat=True).first())
        if visits is None or (kind == CONNECTOR and visits):
            continue
        _task, created = AnalysisTask.objects.get_or_create(
            position_id=key, generation=visits,
            defaults={'budget_nodes': budget, 'multipv': multipv,
                      'source': AnalysisTask.Source.AUTO,
                      'arm': REPAIR_ARM})
        if created:
            made.append((key, kind))
    return made


def _receipt(made, pending, cap, **extra):
    """One event per batch that bought something, like the other arms."""
    DBEvent.objects.create(kind=REPAIR_EVENT, payload={
        'queued': len(made),
        'shallow': sum(1 for _key, kind in made if kind == SHALLOW),
        'connector': sum(1 for _key, kind in made if kind == CONNECTOR),
        'keys': [key for key, _kind in made],
        'pending_before': pending, 'cap': cap, **extra})


def queue_from_landing(pos, parent_keys):
    """Repairs found where a search just landed.  Returns how many.

    Runs at the end of ``ingest_analysis`` with the landed position as that
    pass left it, so ``last_analysis`` is already the showcase it decided.
    The common case, nothing to repair, costs one read of the parents'
    rows and no write.

    What buys runs inside a savepoint and its failure is only logged: an arm
    that orders work must never cost the landing it rides on.
    """
    if ingest.arm_rate(REPAIR_ARM) <= 0:
        return 0
    targets = []
    searched = shallow_showcase(pos.last_analysis)
    budget = repair_budget(searched) if searched is not None else None
    if budget is not None:
        targets.append((pos.key, SHALLOW, budget, REPAIR_MULTIPV))
    if parent_keys:
        price = _connector_price()
        targets.extend((parent, CONNECTOR, *price)
                       for parent in connector_parents(parent_keys))
    if not targets:
        return 0
    try:
        with atomic():
            repaired = _already_repaired([target[0] for target in targets])
            targets = [target for target in targets
                       if target[0] not in repaired]
            if not targets:
                return 0
            room, pending = _room(REPAIR_QUEUE_CAP)
            made = _mint(targets, room)
            if made:
                _receipt(made, pending, REPAIR_QUEUE_CAP, via='landing',
                         key=pos.key)
            return len(made)
    except Exception:       # noqa: BLE001 - the landing goes on without it
        logger.exception('AtomicDB idle repair after %s failed', pos.key)
        return 0


# ---------------- the backlog: what landed before this arm ----------------
#
# Sampled on the production tree in late October (37,510 positions by key
# prefix, 46,000 searched positions by task window): about one showcase of
# 128M or more in ten was shallow, some 156,000 positions, nearly all of them
# from September and early October, when wide engines served the fleet; and
# connectors are rare, a few hundred to a few thousand.  The fleet of late
# October lands about one shallow search in a hundred, so without this walk
# the arm would have almost nothing to do.
#
# THE WALK.  Down the completed searches by id, newest first: a recent search
# is the likeliest to be on a line somebody cares about today.  Each tick reads
# at most ``REPAIR_BACKLOG_SCAN`` searches of 128M or more, never further than
# ``REPAIR_BACKLOG_WINDOW`` ids below where it stands, then their positions by
# key and their parents through their edges: index ranges and lookups, never a
# scan of the tree.  It only walks while there is room, and stops where the
# room ran out, so nothing is skipped; a full queue costs one count.
#
# AND IT GOES ROUND.  Past the oldest search it starts again from the newest.
# That is what picks up a landing the arm had to let go because its queue was
# full, which is exactly while the fleet is busy with requests; the second time
# round, whatever was repaired is skipped without spending room.
#
# WHERE IT STANDS is a file in ``ATOMICDB_STATE_DIR``, like the selector's own
# state (§ ingest, ``_delta_state_path``): a restart resumes the walk.
# Without the directory the walk lives in the process and starts from the
# newest search again after a restart.  Deleting the file and restarting the
# service starts the walk over.
_backlog = {'cursor': None, 'loaded': False}


def _backlog_path():
    root = str(getattr(settings, 'ATOMICDB_STATE_DIR', '') or '')
    return os.path.join(root, 'idle_repair.json') if root else None


def _backlog_state():
    """Where the walk stands, read from disk once per process.

    ``cursor`` is the task id the next step reads below, ``None`` for the
    top.  Anything unreadable on disk is the top: re-reading what was walked
    costs reads, never a second repair.
    """
    if not _backlog['loaded']:
        _backlog['loaded'] = True
        path = _backlog_path()
        if path is not None:
            try:
                with open(path, encoding='utf-8') as handle:
                    cursor = json.load(handle).get('cursor')
                if isinstance(cursor, int) and cursor > 0:
                    _backlog['cursor'] = cursor
            except (OSError, ValueError, TypeError, AttributeError):
                pass
    return _backlog


def _save_backlog(cursor):
    _backlog.update(cursor=cursor, loaded=True)
    path = _backlog_path()
    if path is None:
        return
    try:
        partial = path + '.tmp'
        with open(partial, 'w', encoding='utf-8') as handle:
            json.dump({'cursor': cursor}, handle)
        os.replace(partial, path)
    except OSError:
        pass


def reset_backlog_state():
    """Forget where the walk stands, on disk too: it starts over."""
    _backlog.update(cursor=None, loaded=False)
    path = _backlog_path()
    if path is not None:
        try:
            os.remove(path)
        except OSError:
            pass


def drip_backlog(target=REPAIR_BACKLOG_TARGET, scan=REPAIR_BACKLOG_SCAN,
                 window=REPAIR_BACKLOG_WINDOW):
    """One step of the walk down the backlog.  Returns how many it bought."""
    rate = ingest.arm_rate(REPAIR_ARM)
    if rate <= 0:
        return 0
    room, pending = _room(target, reserve=rate - rate // 2)
    if room <= 0:
        return 0
    cursor = _backlog_state()['cursor']
    if cursor is None:
        cursor = 1 + (AnalysisTask.objects.order_by('-id')
                      .values_list('id', flat=True).first() or 0)
    floor = max(0, cursor - window)
    rows = list(AnalysisTask.objects.filter(
        id__lt=cursor, id__gte=floor, state=AnalysisTask.TState.COMPLETED,
        nodes_searched__gte=REPAIR_MIN_NODES).order_by('-id').values_list(
            'id', 'position_id')[:scan])
    # Below the last search read, or the whole window when it held fewer.
    resume = rows[-1][0] if len(rows) >= scan else floor
    keys = list(dict.fromkeys(key for _id, key in rows))
    made = []
    with atomic():
        showcases = dict(Position.objects.filter(key__in=keys, **_OPEN)
                         .values_list('key', 'last_analysis'))
        connectors = _connectors_of(keys) if keys else {}
        seen = _already_repaired(
            set(showcases) | {parent for parents in connectors.values()
                              for parent in parents})
        price = _connector_price()
        for task_id, key in rows:
            targets = []
            if key in showcases and key not in seen:
                searched = shallow_showcase(showcases[key])
                budget = (repair_budget(searched) if searched is not None
                          else None)
                if budget is not None:
                    targets.append((key, SHALLOW, budget, REPAIR_MULTIPV))
            targets.extend((parent, CONNECTOR, *price)
                           for parent in connectors.get(key, ())
                           if parent not in seen)
            seen.update(target[0] for target in targets)
            made.extend(_mint(targets, room - len(made)))
            if len(made) >= room:
                # This search may hold more than the room took: it is read
                # again next time, and what it already bought is skipped.
                resume = task_id + 1
                break
        if made:
            _receipt(made, pending, target, via='backlog', cursor=resume,
                     scanned=len(rows))
    # Below the first task there is nothing left: the next step starts again
    # from the newest search.
    _save_backlog(resume if resume > 0 else None)
    return len(made)
