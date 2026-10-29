"""Mates the engine already reported are handed to the solver, once.

Two community votes ask for the same thing.  P9 (28-Aug-2026, 4-0): an eval at
or beyond +-95 is "mate found, distance unknown" (Eclipsia, 23-Aug), and more
heuristic analysis only digs at a question that is half answered, so such a
position is bought one pass of the solver, which can turn it into an exact
closure.  And the poll of 29-Oct-2026: "automatic 8M solver requests for
unanalysed but known mate moves pls" (Eclipsia), the rows of the explorer that
no engine searched but whose line already reads as a mate.

WHAT QUALIFIES is read off a search's showcase (the current lines of
``last_analysis``, the pass ``ingest_analysis`` let decide the position):

* THE POSITION ITSELF, when the first line of its showcase scores a mate;
* THE CHILD each line of that showcase goes through, when that line scores a
  mate, and only while the position is open.

"Scores a mate" is a value beyond the clamp, ``TB_CLAMP_CEILING`` < |eval| <
10000: the worker writes ``score mate N`` as ``(10000 - N)`` and clamps every
centipawn score to +-9500 (§ ingest, mate CORTO).  So exactly +-9500 is not a
mate the engine found but a tablebase win or a huge eval with no distance, and
the solver's certificates end on real terminal positions, never on a table
(§ solve, "every leaf is genuinely terminal").  Replayed at 8M on the open
positions at the band (29-Oct, a 1/512 key sample, the df-pn build of 22-Aug,
one core): none of the 37 at exactly 9500 proved, each running its 8M to the
end in 15 to 55 seconds; of 20 searched positions scoring a mate, one proved
with a certificate and one without (the server refuses that), and the one
open child of a mate line, a mate in one, proved at once.  The goal goes by
the sign of the score, White's point of view: the pilot asked WHITE_WIN of
positions at -10000 and 23 of its 36 tasks came back DISPROVED at once
(§ ingest, FRAGILE_ARM).

ONE PLY AND NO DEEPER.  The landing seeds exactly that ply with the line's
value (§ ``ingest._seed_child_eval``); the plies after it have no row unless
something else made one, and making rows to queue proofs would grow the tree,
which is not this arm's to do.  A proof of the child covers the line below it,
whose later positions are shorter mates of the same line.  And the children of
a closed position wait: a closed position's value no longer depends on them,
the same reason the proof manager stops paying for siblings it no longer needs
(§ ``proof.proved_or_clamp``).

WHEN THEY ARE FOUND.  When a search lands (``queue_from_landing``, at the end
of ``ingest_analysis``), from the showcase in hand: the common landing, with
no mate in its lines, costs no query.  Measured on 3,134 landings in two
hours: 8 open positions at the band (half of them at the clamp), and 1,124
mate lines whose children the landing itself closed by ``MATE_PV`` in all but
2.  What qualified before this existed is reached by a walk round the
completed searches (``drip_backlog``, one bounded step per tick of the
selector service), as the idle fleet's repairs are (§ idle_repair).

WHAT IS BOUGHT.  One F0 pass of 8M per position, EVER: a pass of this arm, in
any state, is never bought again, so a pass that fails (UNKNOWN, a rejected
certificate) cannot start a loop.  The full scan this replaces skipped only
the positions with a pass PENDING or LEASED, and bought a failed one again two
minutes later, forever.  There is no staged escalation to apply: F1-F3 belong
to SURVIVE50 and are not implemented (§ models.SolveTask.Stage).

HOW MUCH.  Its own cap of PENDING passes and its own hourly ceiling, both
counted on its own arm (§ ingest, ``_arm_budget_room``).  The cap counts every
PENDING pass of the arm, on a closed position too, and that is not
idle_repair's rule: ``api_solve_acquire`` serves a PENDING pass whatever its
position became, so each one holds the solver's time until it is served.  The
walk may fill only half of each, so a fresh landing always finds room.
``ATOMICDB_ARM_RATE_AUTOSOLVE=0`` turns the whole arm off.

WHO SERVES IT.  Only a worker started with ``--solve``, with an engine that
answers ``solve``.  Measured 29-Oct: no pass of any arm completed since
26-Aug, and the engine the workers download (main@16c8f0dd) answers
"Unknown command".  Until that changes, the arm fills its cap and waits, at
the price of one count per landing that finds something.

WHAT IT NEVER DOES: decide a value.  It only chooses which passes to buy.  A
certificate closes a position through ``ingest.apply_solve_result`` and only
once the server has replayed it whole, untouched here.
"""

import json
import logging
import os

from django.conf import settings

from . import ingest
from .database import atomic
from .models import AnalysisTask, DBEvent, Edge, Position, SolveTask

logger = logging.getLogger(__name__)

AUTOSOLVE_ARM = ingest.AUTOSOLVE95_ARM
AUTOSOLVE_STAGE = SolveTask.Stage.F0
AUTOSOLVE_NODES = ingest.AUTOSOLVE95_STAGE_NODES
# The burst brake: how many passes may wait at once.  The cap of the full
# scan this replaces.
AUTOSOLVE_QUEUE_CAP = 32
# The walk stops at half of it, and at half of the hourly ceiling: the other
# halves are kept for what lands from now on.
AUTOSOLVE_BACKLOG_TARGET = AUTOSOLVE_QUEUE_CAP // 2
AUTOSOLVE_EVENT = 'AUTOSOLVE95_ENQUEUED'
# One step of the walk: at most this many completed searches, and never
# further down the task ids than this, whatever it found.
AUTOSOLVE_BACKLOG_SCAN = 200
AUTOSOLVE_BACKLOG_WINDOW = 2_000

OWN = 'own'
CHILD = 'child'

_OPEN = {'status': 'UNKNOWN', 'priority__gt': ingest.DEAD / 2}
_LIVE = (SolveTask.TState.PENDING, SolveTask.TState.LEASED)


def mate_goal(line):
    """The goal a line that scores a mate proves, or ``None``.

    A line whose mate score names the other side as the winner contradicts
    its own value and claims nothing.
    """
    if not isinstance(line, dict):
        return None
    score = line.get('eval_cp')
    if (not isinstance(score, int) or isinstance(score, bool)
            or not ingest.TB_CLAMP_CEILING < abs(score) < 10_000):
        return None
    mate = line.get('mate')
    if isinstance(mate, int) and mate and (mate > 0) != (score > 0):
        return None
    return 'WHITE_WIN' if score > 0 else 'BLACK_WIN'


def claims(last_analysis):
    """What a showcase claims: ``(own goal or None, [(move, goal), ...])``.

    The moves come shortest mate first, the likeliest to be proved in 8M.
    Only the current pass counts: the lines kept from a wider pass before it
    (``prior_pass``) are not what the position shows.
    """
    lines = [line for line in (last_analysis or [])
             if isinstance(line, dict) and not line.get('prior_pass')]
    if not lines:
        return None, []
    found = {}
    for line in lines:
        goal, move = mate_goal(line), line.get('move')
        if goal is not None and isinstance(move, str) and move not in found:
            found[move] = (abs(line['eval_cp']), goal)
    moves = sorted(found.items(), key=lambda item: -item[1][0])
    return mate_goal(lines[0]), [(move, goal) for move, (_score, goal)
                                 in moves]


def _children(pairs):
    """``{(parent, move): child}`` for the ``(parent, move)`` pairs given.

    One read of the unique ``(parent, move)`` index, whatever the number of
    parents; the superset it returns is cut back to the pairs asked for.
    """
    wanted = set(pairs)
    if not wanted:
        return {}
    rows = Edge.objects.filter(
        parent_id__in=sorted({parent for parent, _move in wanted}),
        move_uci__in=sorted({move for _parent, move in wanted}),
    ).values_list('parent_id', 'move_uci', 'child_id')
    return {(parent, move): child for parent, move, child in rows
            if (parent, move) in wanted}


def _open(keys):
    """The positions among ``keys`` still open and alive, in one read."""
    if not keys:
        return set()
    return set(Position.objects.filter(key__in=sorted(set(keys)), **_OPEN)
               .values_list('key', flat=True))


def _targets(parent, own, moves, children, alive):
    """``[(key, goal, kind)]`` a searched position's claims buy, best first.

    The position first: its closure is the one that climbs.  Its children
    only while it is open.
    """
    if parent not in alive:
        return []
    targets = [(parent, own, OWN)] if own is not None else []
    # Two captures can blow up the same squares and reach the same position.
    seen = {parent}
    for move, goal in moves:
        child = children.get((parent, move))
        if child in alive and child not in seen:
            seen.add(child)
            targets.append((child, goal, CHILD))
    return targets


def _already_asked(goals):
    """The positions of ``goals`` (``{key: goal}``) not to ask again.

    One read of a table of a few thousand rows, for three reasons:

    * a pass of this arm, in any state: one per position, ever;
    * a pass of any arm still PENDING or LEASED, which is the same question
      already on its way;
    * a completed pass of any arm at least as big, for the same goal: the
      same question at the same budget gets the same answer (§ ingest,
      FRAGILE_ARM).  A smaller one does not count: 8M can prove what 2M
      could not.
    """
    if not goals:
        return set()
    asked = set()
    for key, arm, state, goal, budget in SolveTask.objects.filter(
            position_id__in=sorted(goals)).values_list(
                'position_id', 'arm', 'state', 'goal', 'budget_nodes'):
        if (arm == AUTOSOLVE_ARM or state in _LIVE
                or (state == SolveTask.TState.COMPLETED
                    and budget >= AUTOSOLVE_NODES and goal == goals[key])):
            asked.add(key)
    return asked


def _room(cap, reserve=0):
    """``(how many passes may be bought now, how many wait)``.

    ``reserve`` is the part of the hourly ceiling this buyer leaves to others.
    """
    pending = SolveTask.objects.filter(
        arm=AUTOSOLVE_ARM, state=SolveTask.TState.PENDING).count()
    room = cap - pending
    if room <= 0:
        return 0, pending
    hourly = ingest._arm_budget_room(AUTOSOLVE_ARM, SolveTask.objects) \
        - reserve
    return max(0, min(room, hourly)), pending


def _mint(targets):
    """One INSERT for the passes of ``targets``.

    The table has no unique row to stop two processes buying the same
    position at the same moment (a landing and the walk, two landings over one
    child).  The second pass costs one 8M search, less than a lock on every
    landing would (§ ingest, the hourly ceiling makes the same trade).
    """
    if targets:
        SolveTask.objects.bulk_create([
            SolveTask(position_id=key, goal=goal, budget_stage=AUTOSOLVE_STAGE,
                      budget_nodes=AUTOSOLVE_NODES, arm=AUTOSOLVE_ARM)
            for key, goal, _kind in targets])
    return targets


def _receipt(made, pending, cap, **extra):
    """One event per batch that bought something, like the other arms."""
    DBEvent.objects.create(kind=AUTOSOLVE_EVENT, payload={
        'queued': len(made),
        'own': sum(1 for _key, _goal, kind in made if kind == OWN),
        'child': sum(1 for _key, _goal, kind in made if kind == CHILD),
        'keys': [key for key, _goal, _kind in made],
        'pending_before': pending, 'cap': cap, **extra})


def queue_from_landing(pos):
    """Passes found where a search just landed.  Returns how many.

    Runs at the end of ``ingest_analysis`` with the landed position as that
    pass left it, so ``last_analysis`` is already the showcase it decided.  A
    landing whose showcase scores no mate costs nothing; one that does costs
    an indexed read of its children and of their rows, and, when something
    is open, one read of the passes already bought, two counts and one
    INSERT.

    What buys runs inside a savepoint and its failure is only logged: an arm
    that orders work must never cost the landing it rides on.
    """
    if ingest.arm_rate(AUTOSOLVE_ARM) <= 0:
        return 0
    own, moves = claims(pos.last_analysis)
    if own is None and not moves:
        return 0
    try:
        with atomic():
            children = _children([(pos.key, move) for move, _goal in moves])
            alive = _open([pos.key, *children.values()])
            targets = _targets(pos.key, own, moves, children, alive)
            if not targets:
                return 0
            asked = _already_asked({key: goal for key, goal, _kind
                                    in targets})
            targets = [target for target in targets if target[0] not in asked]
            if not targets:
                return 0
            room, pending = _room(AUTOSOLVE_QUEUE_CAP)
            made = _mint(targets[:room])
            if made:
                _receipt(made, pending, AUTOSOLVE_QUEUE_CAP, via='landing',
                         key=pos.key)
            return len(made)
    except Exception:       # noqa: BLE001 - the landing goes on without it
        logger.exception('AtomicDB autosolve after %s failed', pos.key)
        return 0


# ---------------- the backlog: what qualified before this arm ----------------
#
# Sampled on the production tree on 29-Oct (1/512 of the keys): about 14,000
# open searched positions whose showcase scores a mate, and a few hundred open
# children of a mate line; next to them, some 53,000 open positions at exactly
# +-9500, which do not qualify (see the top of this module).  Every searched
# one sampled had a completed search of its own, and 85 of the 87 children
# hung from a searched parent whose current line went through them, so a walk
# round the completed searches reaches the backlog without reading the tree.
#
# THE WALK is idle_repair's (§ idle_repair, ``drip_backlog``): down the
# completed searches by id, newest first, at most ``AUTOSOLVE_BACKLOG_SCAN``
# of them and never further than ``AUTOSOLVE_BACKLOG_WINDOW`` ids below where
# it stands; then the showcases of those that are still open, by key, and the
# children their mate lines go through, by the unique edge index: index
# ranges and lookups, never a scan of the tree.  It only walks while there is
# room, stops where the room ran out, and past the oldest search starts again
# from the newest.  A full queue costs one count.
#
# WHERE IT STANDS is a file in ``ATOMICDB_STATE_DIR``, beside the repair
# walk's: a restart resumes it.  Deleting the file and restarting the service
# starts the walk over.
_backlog = {'cursor': None, 'loaded': False}


def _backlog_path():
    root = str(getattr(settings, 'ATOMICDB_STATE_DIR', '') or '')
    return os.path.join(root, 'autosolve.json') if root else None


def _backlog_state():
    """Where the walk stands, read from disk once per process.

    ``cursor`` is the task id the next step reads below, ``None`` for the
    top.  Anything unreadable on disk is the top: re-reading what was walked
    costs reads, never a second pass.
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


def drip_backlog(target=AUTOSOLVE_BACKLOG_TARGET, scan=AUTOSOLVE_BACKLOG_SCAN,
                 window=AUTOSOLVE_BACKLOG_WINDOW):
    """One step of the walk down the backlog.  Returns how many it bought.

    ``target`` is the PENDING passes it tops the arm up to, never more than
    ``AUTOSOLVE_BACKLOG_TARGET``: the rest of the cap is the landings'.
    """
    rate = ingest.arm_rate(AUTOSOLVE_ARM)
    target = min(int(target), AUTOSOLVE_BACKLOG_TARGET)
    if rate <= 0 or target <= 0:
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
    ).order_by('-id').values_list('id', 'position_id')[:scan])
    # Below the last search read, or the whole window when it held fewer.
    resume = rows[-1][0] if len(rows) >= scan else floor
    keys = list(dict.fromkeys(key for _id, key in rows))
    made = []
    with atomic():
        claimed = {}
        for key, analysis in Position.objects.filter(
                key__in=keys, **_OPEN).values_list('key', 'last_analysis'):
            own, moves = claims(analysis)
            if own is not None or moves:
                claimed[key] = (own, moves)
        children = _children([(key, move) for key, (_own, moves)
                              in claimed.items() for move, _goal in moves])
        alive = set(claimed) | _open(children.values())
        # The newest search of each position, and what its showcase buys.
        found = []
        for task_id, key in rows:
            if key in claimed:
                own, moves = claimed.pop(key)
                found.append((task_id, _targets(key, own, moves, children,
                                                alive)))
        # A position two parents claim keeps the goal of the first claim.
        goals = {}
        for _task_id, targets in found:
            for key, goal, _kind in targets:
                goals.setdefault(key, goal)
        asked = _already_asked(goals)
        batch = []
        for task_id, targets in found:
            for item in targets:
                if item[0] not in asked:
                    asked.add(item[0])
                    batch.append(item)
            if len(batch) >= room:
                # This search may hold more than the room took: it is read
                # again next time, and what it already bought is skipped.
                batch = batch[:room]
                resume = task_id + 1
                break
        made = _mint(batch)
        if made:
            _receipt(made, pending, target, via='backlog', cursor=resume,
                     scanned=len(rows))
    # Below the first task there is nothing left: the next step starts again
    # from the newest search.
    _save_backlog(resume if resume > 0 else None)
    return len(made)
