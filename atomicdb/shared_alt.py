"""SHARED_ALT: a shadow, log-only measurement inside the value walker.

THE QUESTION (jean_heibig, Nov-2026; study of 23-nov).  At an OR node W of the
walk the spine goes to S = ``backed_move``.  Sometimes an alternative C, within
``proof.VALUE_ALTERNATIVE_MARGIN_CP`` of S and worth at least
``SHARED_ALT_MIN_VALUE_CP`` to the attacker, is REQUIRED WITHOUT W: there is a
path from the walk's root to C that avoids W and uses only obligatory edges

  * from an AND parent, any open reply;
  * from an OR parent, only its ``backed_move``.

Proving W through C would then reuse work that has to be done anyway.  Before
anything acts on that, phase 1 only COUNTS it: every time the walk passes such
a W, one ``SHARED_ALT`` event is written.  Nothing here changes which task is
chosen, any value, any closure, any page or any API answer.

THE CHECK is a bounded upward search from C, one query per ply over the
``Edge.child`` index (parents of the current level, with their status, FEN and
``backed_move`` joined by primary key).  It succeeds when it reaches a node of
the walk ABOVE W through which the walk itself went by obligatory edges: that
prefix is required without W by construction, so it is the target set and the
search does not have to climb to the root.

CAPS, all per walk and shared by every alternative of the walk: plies
(``SHARED_ALT_MAX_PLIES``), positions whose parents were read
(``SHARED_ALT_MAX_NODES``), edge rows per query (``SHARED_ALT_MAX_EDGE_ROWS``)
and wall time (``SHARED_ALT_WALL_SECONDS``).  On top of that each process
spends at most ``SHARED_ALT_SECONDS_PER_MINUTE`` on this per minute, so the
overhead stays bounded whatever the selector's pass size.

RATE LIMIT.  A pair (W, C) is written at most once per ``SHARED_ALT_DEDUPE``
(checked in process first, then against the table), and the whole kind at most
``SHARED_ALT_HOURLY_CAP`` times per rolling hour.  Results of the check, found
or not, are remembered in process for ``SHARED_ALT_MEMO_SECONDS`` so the same
spine walked again costs nothing.

THE SWITCH is ``ATOMICDB_SHARED_ALT_SHADOW``, OFF by default; only ``1``,
``true``, ``yes`` or ``on`` turn it on.  Any exception inside is logged and
swallowed, inside its own savepoint, so a failure can never reach the walk or
the transaction around it.
"""

import logging
import os
import time
from datetime import timedelta

from django.conf import settings
from django.db import router, transaction
from django.utils import timezone

from .models import DBEvent, Edge, ProofNode

logger = logging.getLogger(__name__)

SHARED_ALT_EVENT = 'SHARED_ALT'
SHARED_ALT_SETTING = 'ATOMICDB_SHARED_ALT_SHADOW'

SHARED_ALT_MIN_VALUE_CP = 300
SHARED_ALT_MAX_PLIES = 40
SHARED_ALT_MAX_NODES = 300
SHARED_ALT_MAX_EDGE_ROWS = 2_000
SHARED_ALT_WALL_SECONDS = 0.05
SHARED_ALT_SECONDS_PER_MINUTE = 1.0
SHARED_ALT_DEDUPE = timedelta(hours=1)
SHARED_ALT_HOURLY_CAP = 2_000
SHARED_ALT_MEMO_SECONDS = 600
SHARED_ALT_MEMO_MAX = 20_000

_TRUE = {'1', 'true', 'yes', 'on'}

# Per process.  ``_memo``: (W, C) -> (monotonic time, witness plies or None).
_memo = {}
_minute = [0.0, 0.0]      # [start of the current minute, seconds spent in it]


def enabled():
    raw = getattr(settings, SHARED_ALT_SETTING, None)
    if raw is None:
        raw = os.environ.get(SHARED_ALT_SETTING, '')
    return str(raw).strip().lower() in _TRUE


class Budget:
    """The caps of ONE walk, shared by every alternative it checks."""

    def __init__(self, nodes=None, seconds=None, plies=None,
                 clock=time.monotonic):
        self.nodes = SHARED_ALT_MAX_NODES if nodes is None else nodes
        self.plies = SHARED_ALT_MAX_PLIES if plies is None else plies
        self.clock = clock
        self.deadline = clock() + (SHARED_ALT_WALL_SECONDS if seconds is None
                                   else seconds)
        self.used_nodes = 0
        self.capped = False

    def spent(self):
        if self.used_nodes >= self.nodes or self.clock() >= self.deadline:
            self.capped = True
        return self.capped


def required_without(goal, c_key, w_key, targets, budget):
    """Witness length in plies if C is required without W, else ``None``.

    ``targets`` are keys already known to be required without W.  ``None``
    is also the answer when a cap stops the search (``budget.capped`` says
    which): a shadow that does not know logs nothing.
    """
    from .proof import is_or_node
    seen = {c_key, w_key}
    frontier = [c_key]
    for ply in range(1, budget.plies + 1):
        if not frontier or budget.spent():
            return None
        room = budget.nodes - budget.used_nodes
        if len(frontier) > room:
            frontier = frontier[:room]
            budget.capped = True
        budget.used_nodes += len(frontier)
        rows = list(Edge.objects.filter(child_id__in=frontier).values_list(
            'parent_id', 'move_uci', 'child_id', 'parent__status',
            'parent__fen', 'parent__backed_move')[:SHARED_ALT_MAX_EDGE_ROWS])
        following = []
        for parent, move, child, status, fen, backed_move in rows:
            if parent in seen or parent == child or status != 'UNKNOWN':
                continue
            if is_or_node(fen, goal) and backed_move != move:
                continue      # an OR parent only obliges its own backed_move
            if parent in targets:
                return ply
            seen.add(parent)
            following.append(parent)
        frontier = following
    return None


def _required_prefix(path, start_backed_move, goal):
    """The nodes of the walk, from its root, reached by obligatory edges.

    The walk follows ``backed_move`` at an OR node except when it falls back
    (§ ``proof._spine_step``); a fallback is not obligatory, so the prefix
    ends at that node, which is itself still required.  Returns the prefix as
    ``(step, backed_move of that node)`` pairs.
    """
    from .proof import is_or_node
    prefix, backed_move = [], start_backed_move
    for step in path:
        prefix.append((step, backed_move))
        primary = step.primary
        if primary is None:
            break
        if is_or_node(step.fen, goal) and primary.move_uci != backed_move:
            break
        backed_move = primary.backed_move
    return prefix


def _attacker_value(value, goal):
    from .proof import attacker_is_white
    return value if attacker_is_white(goal) else -value


def _candidates(path, start_backed_move, goal):
    """``(depth, W, S, C, diff_cp)`` to check, deepest W first."""
    from .proof import _competitive, is_or_node
    prefix = _required_prefix(path, start_backed_move, goal)
    keys = [step.key for step in path]
    out = []
    for depth in range(len(prefix) - 1, -1, -1):
        step, backed_move = prefix[depth]
        spine = step.primary
        if spine is None or spine.value is None:
            continue
        if not is_or_node(step.fen, goal) or spine.move_uci != backed_move:
            continue          # only an OR node whose spine IS its backed_move
        stm_white = step.fen.split()[1] == 'w'
        for alt in _competitive(step):
            if alt.key in keys[:depth + 1]:
                continue
            if _attacker_value(alt.value, goal) < SHARED_ALT_MIN_VALUE_CP:
                continue
            diff = (spine.value - alt.value if stm_white
                    else alt.value - spine.value)
            out.append((depth, step, spine, alt, diff))
    return out


def _memo_get(pair, now):
    hit = _memo.get(pair)
    if hit is None or now - hit[0] > SHARED_ALT_MEMO_SECONDS:
        return False, None
    return True, hit[1]


def _memo_put(pair, now, value):
    if len(_memo) >= SHARED_ALT_MEMO_MAX:
        _memo.clear()
    _memo[pair] = (now, value)


def _room_this_minute(now):
    if now - _minute[0] >= 60.0:
        _minute[0], _minute[1] = now, 0.0
    return _minute[1] < SHARED_ALT_SECONDS_PER_MINUTE


def _record(campaign, path, depth, step, spine, alt, diff, witness):
    since = timezone.now() - SHARED_ALT_DEDUPE
    events = DBEvent.objects.filter(kind=SHARED_ALT_EVENT, ts__gte=since)
    if events.filter(payload__w=step.key, payload__c=alt.key).exists():
        return False
    if events.count() >= SHARED_ALT_HOURLY_CAP:
        return False
    numbers = {row.position_id: row.pn for row in ProofNode.objects.filter(
        campaign=campaign, position_id__in=[spine.key, alt.key])}
    DBEvent.objects.create(kind=SHARED_ALT_EVENT, payload={
        'campaign': campaign.pk, 'root': path[0].key, 'ply': depth,
        'w': step.key, 's': spine.key, 'c': alt.key,
        's_move': spine.move_uci, 'c_move': alt.move_uci,
        's_value': spine.value, 'c_value': alt.value, 'diff_cp': diff,
        'pn_s': numbers.get(spine.key), 'pn_c': numbers.get(alt.key),
        'witness_plies': witness})
    return True


def observe(campaign, path, start_backed_move, budget=None):
    """Look at one finished walk and log what it passed.  Never raises.

    Returns the number of events written (for tests); the walk ignores it.
    """
    if not enabled():
        return 0
    clock = time.monotonic
    began = clock()
    if not _room_this_minute(began):
        return 0
    written = 0
    try:
        with transaction.atomic(using=router.db_for_write(DBEvent)):
            written = _observe(campaign, path, start_backed_move,
                               budget or Budget())
    except Exception:
        logger.exception('SHARED_ALT shadow failed; the walk is unaffected')
    finally:
        _minute[1] += clock() - began
    return written


def _observe(campaign, path, start_backed_move, budget):
    goal = campaign.goal
    written = 0
    for depth, step, spine, alt, diff in _candidates(path, start_backed_move,
                                                     goal):
        pair = (step.key, alt.key)
        now = time.monotonic()
        known, witness = _memo_get(pair, now)
        if not known:
            if budget.spent():
                break
            targets = {item.key for item in path[:depth]}
            if not targets:
                continue      # W is the root: nothing above it to share with
            witness = required_without(goal, alt.key, step.key, targets,
                                       budget)
            if witness is None and budget.capped:
                break         # undecided: neither logged nor remembered
            _memo_put(pair, now, witness)
            if witness is not None and _record(campaign, path, depth, step,
                                               spine, alt, diff, witness):
                written += 1
    return written
