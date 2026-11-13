"""The +16 sweep: search again the clearly won positions searched only once.

WHY.  Audit of 11-Nov-2026, section 2.6.  Of 36 real open positions that
production searched once at 128M nodes and left open between +16 and +90, the
engine published today resolves 20 to 22 (mate, or a tablebase win inside the
search) with the same 128M, whatever the threads or the number of lines.  The
gain is the engine: those searches ran on builds from August to November.  A
mate the engine finds in a search is only worth something to the tree if
somebody searches again, and nobody does: the selector prefers positions it
has not looked at, and a person asks for what moves the headline.  There are
about 200,000 such positions, which makes this the cheapest closure there is.

WHAT QUALIFIES.  An open position (``status='UNKNOWN'``) with exactly one
search (``visits == 1``) of about 128M nodes (``nodes_invested`` between
``SEARCHED_MIN_NODES`` and ``SEARCHED_MAX_NODES``), whose side to move is the
side its value favours, by between +16 and +90 pawns: White to move at
``+1600 <= v < +9000``, or Black to move at ``-9000 < v <= -1600``.  ``v`` is
the value the tree shows, ``backed_eval`` and else ``eval_cp``, White's side.
From +90 the engine already sees mate and other arms buy that verification;
under +16 a second search closes nothing (0 of 12 in the audit).

IN WHICH ORDER.  First the ones inside White's strategy tree, in the order a
walk from the start position meets them (the shallowest first): White plays
the move its value stands on (``backed_move``, else its best known child),
Black every open reply.  A closure there is a closure of the proof the
project is after.  Then every other one, the highest value first: the
experiment resolved 10 of 12 between +50 and +90 and 6 of 12 between +16 and
+25.

HOW.  Two halves, so that nothing heavy runs on a clock that matters:

* ``build_plan`` (management command ``sweep_plus16 --rebuild``) walks the
  tree and scans the open positions once, and writes the ordered list of
  keys to ``sweep16.json`` in ``ATOMICDB_STATE_DIR``.  Minutes of reads,
  meant for a daily timer.  Without a list nothing is ever queued: building
  it is how the sweep is switched on.
* ``drip`` (a step of ``refresh_selector``, every pass) reads the next keys
  of that list, checks each again against the database, and queues the ones
  that still qualify, up to the room it has.

HOW MUCH, AND THE VALVES.  A task is one search of ``SWEEP_NODES`` (128M) with
one line, in the FILL band.  ``ATOMICDB_SWEEP16_SHARE`` of the leases (0.14
by default) take a sweep task first, requests or not (see the fleet share
below); every other lease serves it behind every request, ahead of the selector's own
AUTO pool, beside the other arms that close nodes (coverage, quality).  The
drip only TOPS UP its own PENDING tasks to ``SWEEP_PENDING_CAP`` (256), like
the analysis pool and never as a dump (the 512-task floods of 8 and 11
October), pays the hourly ceiling of its arm (``ATOMICDB_ARM_RATE_SWEEP16``,
§ ``ingest.arm_rate``), and does nothing while more than
``SWEEP_USER_BACKLOG_MAX`` requests are waiting when the share is 0.  The pool does not
count these tasks (it counts AUTO only), so ``--pool-target`` keeps meaning
what it meant.

KILL SWITCH.  ``ATOMICDB_ARM_RATE_SWEEP16=0`` in the environment of the
selector service, and a restart of it, stops the drip.  Without a restart:
``sweep_plus16 --stop`` deletes the list, and the next pass queues nothing.
``sweep_plus16 --withdraw`` also removes the sweep's PENDING tasks that no
worker took yet.

WHAT IT NEVER DOES: decide a value or close anything.  It only buys searches;
what they find lands through ``ingest_analysis`` like any other result.
"""

import json
import logging
import os
import time
import uuid

from django.conf import settings
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from . import ingest
from .database import atomic
from .models import AnalysisTask, DBEvent, Edge, Position

logger = logging.getLogger(__name__)

SWEEP_ARM = ingest.SWEEP16_ARM
SWEEP_EVENT = 'SWEEP16_ENQUEUED'
SWEEP_NODES = ingest.REQUEST_BUDGET_LADDER[0]          # 128M
SWEEP_MULTIPV = 1
SWEEP_LOW_CP = 1600
SWEEP_HIGH_CP = ingest.MATE_BAND                       # 9000, excluded
# "Searched once at 128M": the engine stops a little past its budget, and a
# pass from before the October floor (8M, 32M) or from the days the first
# request rung was 512M is not the search the experiment repeated.
SEARCHED_MIN_NODES = 100_000_000
SEARCHED_MAX_NODES = 141_000_000
SWEEP_PENDING_CAP = 256
SWEEP_USER_BACKLOG_MAX = 500
# Keys read again per drip, at most: a list whose head is all spent costs a
# bounded number of reads per pass, never a walk of the whole list.
SWEEP_SCAN = 2_048
SWEEP_BATCH = 256
# The tree walk of ``build_plan``.  Measured on 11-Nov-2026: about 1.75M open
# positions in White's strategy tree.
TREE_MAX_NODES = 2_500_000
TREE_MAX_PLIES = 200
TREE_CHUNK = 300
# The scan of the open positions, by key prefix: 256 short statements instead
# of one that reads the whole table.
SCAN_PREFIXES = ['%02x' % i for i in range(256)]

PLAN_FILE = 'sweep16.json'
CURSOR_FILE = 'sweep16_cursor.json'

_memory = {'plan': None, 'cursor': None, 'plan_mtime': None}


# ---------------------------------------------------------------- the rule ---

def favoured_value(stm_white, value):
    """Is ``value`` (White's side) +16..+90 for the side to move?"""
    if value is None:
        return False
    if stm_white:
        return SWEEP_LOW_CP <= value < SWEEP_HIGH_CP
    return -SWEEP_HIGH_CP < value <= -SWEEP_LOW_CP


def searched_once(visits, nodes_invested):
    return (visits == 1
            and SEARCHED_MIN_NODES <= (nodes_invested or 0)
            <= SEARCHED_MAX_NODES)


def qualifies(pos):
    """The whole rule, on a position as the database has it now."""
    if pos.status != 'UNKNOWN':
        return False
    if not searched_once(pos.visits, pos.nodes_invested):
        return False
    value = pos.backed_eval if pos.backed_eval is not None else pos.eval_cp
    return favoured_value(pos.fen.split()[1] == 'w', value)


def candidates_q():
    """The same rule in SQL, for the scan (the side to move from the FEN)."""
    return (Q(status='UNKNOWN', visits=1,
              nodes_invested__gte=SEARCHED_MIN_NODES,
              nodes_invested__lte=SEARCHED_MAX_NODES)
            & ((Q(fen__contains=' w ') & Q(sweep_value__gte=SWEEP_LOW_CP)
                & Q(sweep_value__lt=SWEEP_HIGH_CP))
               | (Q(fen__contains=' b ') & Q(sweep_value__gt=-SWEEP_HIGH_CP)
                  & Q(sweep_value__lte=-SWEEP_LOW_CP))))


# ------------------------------------------------------------ the plan -------

def _known(status, backed_eval, eval_cp):
    return ingest.known_eval_of(status, backed_eval, eval_cp)


def walk_strategy_tree(max_nodes=TREE_MAX_NODES, max_plies=TREE_MAX_PLIES,
                       deadline=None, root=None):
    """Candidates inside White's strategy tree, the shallowest first.

    Returns ``(keys, stats)``.  Breadth first from the start position: at a
    White node the move its value stands on (``backed_move``, else the best
    known child), at a Black node every open reply.  Only open positions are
    walked: a proven one needs no more work and neither does its subtree.
    Transpositions are walked once.  Stops at ``max_nodes`` positions,
    ``max_plies`` plies or ``deadline`` (``time.monotonic()``), whichever
    comes first, and says which in ``stats['stopped']``.
    """
    root = root or ingest.root_key()
    head = (Position.objects.filter(key=root)
            .values_list('status', 'backed_move').first())
    stats = {'nodes': 0, 'plies': 0, 'stopped': 'exhausted'}
    if head is None or head[0] != 'UNKNOWN':
        return [], stats
    level = [(root, head[1])]
    visited = {root}
    found, found_set = [], set()
    for ply in range(max_plies + 1):
        if not level:
            break
        if stats['nodes'] >= max_nodes:
            stats['stopped'] = 'max_nodes'
            break
        if deadline is not None and time.monotonic() > deadline:
            stats['stopped'] = 'time'
            break
        stats['nodes'] += len(level)
        stats['plies'] = ply
        white = ply % 2 == 0
        nxt = []
        for i in range(0, len(level), TREE_CHUNK):
            part = level[i:i + TREE_CHUNK]
            edges = Edge.objects.filter(parent_id__in=[k for k, _ in part])
            if white:
                # Only the edge the value stands on, where there is one: half
                # the tree is White nodes, and each would bring all its
                # replies otherwise.
                picked = Q(pk__in=[])
                bare = []
                for key, move in part:
                    if move:
                        picked |= Q(parent_id=key, move_uci=move)
                    else:
                        bare.append(key)
                edges = Edge.objects.filter(picked | Q(parent_id__in=bare))
            rows = edges.values_list(
                'parent_id', 'move_uci', 'child_id', 'child__status',
                'child__eval_cp', 'child__backed_eval', 'child__backed_move',
                'child__visits', 'child__nodes_invested')
            by_parent = {}
            for row in rows:
                by_parent.setdefault(row[0], []).append(row[1:])
            for key, move in part:
                children = by_parent.get(key, ())
                if white:
                    pick = None
                    if move:
                        pick = next((c for c in children if c[0] == move),
                                    None)
                    if pick is None:
                        best = None
                        for c in children:
                            value = _known(c[2], c[4], c[3])
                            if value is not None and (best is None
                                                      or value > best[0]):
                                best = (value, c)
                        pick = best[1] if best else None
                    if (pick is None or pick[2] != 'UNKNOWN'
                            or pick[1] in visited):
                        continue
                    visited.add(pick[1])
                    nxt.append((pick[1], pick[5]))
                    continue
                for c in children:
                    if c[2] != 'UNKNOWN':
                        continue
                    child = c[1]
                    value = c[4] if c[4] is not None else c[3]
                    if (child not in found_set and searched_once(c[6], c[7])
                            and favoured_value(True, value)):
                        found.append(child)
                        found_set.add(child)
                    if child not in visited:
                        visited.add(child)
                        nxt.append((child, c[5]))
        level = nxt
    else:
        stats['stopped'] = 'max_plies'
    return found, stats


def scan_candidates(exclude=frozenset(), prefixes=None, deadline=None):
    """Every qualifying position outside ``exclude``, highest value first.

    Returns ``(keys, stats)``.  One short statement per key prefix
    (``SCAN_PREFIXES``), so no single read holds the table.
    """
    prefixes = SCAN_PREFIXES if prefixes is None else prefixes
    found = []
    stats = {'prefixes': 0, 'stopped': 'exhausted'}
    base = Position.objects.annotate(
        sweep_value=Coalesce('backed_eval', 'eval_cp')).filter(candidates_q())
    for i, prefix in enumerate(prefixes):
        if deadline is not None and time.monotonic() > deadline:
            stats['stopped'] = 'time'
            break
        upper = (prefixes[i + 1] if i + 1 < len(prefixes)
                 else None)
        rows = base.filter(key__gte=prefix)
        if upper is not None:
            rows = rows.filter(key__lt=upper)
        for key, value in rows.values_list('key', 'sweep_value'):
            if key not in exclude:
                found.append((abs(value), key))
        stats['prefixes'] += 1
    found.sort(key=lambda item: (-item[0], item[1]))
    return [key for _value, key in found], stats


def build_plan(max_nodes=TREE_MAX_NODES, max_plies=TREE_MAX_PLIES,
               time_limit=7200.0, save=True):
    """Walk, scan and (with ``save``) store the ordered list.  Returns it."""
    started = time.monotonic()
    deadline = started + time_limit if time_limit else None
    tree, tree_stats = walk_strategy_tree(max_nodes, max_plies, deadline)
    rest, scan_stats = scan_candidates(set(tree), deadline=deadline)
    plan = {
        'build_id': uuid.uuid4().hex,
        'built_at': timezone.now().isoformat(),
        'tree_count': len(tree),
        'rest_count': len(rest),
        'tree_stats': tree_stats,
        'scan_stats': scan_stats,
        'seconds': round(time.monotonic() - started, 1),
        'keys': tree + rest,
    }
    if save:
        save_plan(plan)
    return plan


# ------------------------------------------------------- where it is kept ---

def _state_path(name):
    root = str(getattr(settings, 'ATOMICDB_STATE_DIR', '') or '')
    return os.path.join(root, name) if root else None


def _write_json(path, payload):
    partial = path + '.tmp'
    with open(partial, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle)
    os.replace(partial, path)


def save_plan(plan):
    """Store a new list; the walk down it starts at its head."""
    _memory.update(plan=plan, cursor={'build_id': plan['build_id'],
                                      'cursor': 0}, plan_mtime=None)
    path = _state_path(PLAN_FILE)
    if path is not None:
        _write_json(path, plan)
        _save_cursor(plan['build_id'], 0)
        try:
            _memory['plan_mtime'] = os.path.getmtime(path)
        except OSError:
            pass


def load_plan():
    """The current list, or ``None``.  Read from disk again only if changed."""
    path = _state_path(PLAN_FILE)
    if path is None:
        return _memory['plan']
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _memory.update(plan=None, plan_mtime=None)
        return None
    if _memory['plan'] is None or _memory['plan_mtime'] != mtime:
        try:
            with open(path, encoding='utf-8') as handle:
                plan = json.load(handle)
            if not isinstance(plan.get('keys'), list):
                raise ValueError('no keys')
        except (OSError, ValueError, TypeError, AttributeError):
            logger.warning('sweep16: unreadable plan at %s', path)
            return None
        _memory.update(plan=plan, plan_mtime=mtime)
    return _memory['plan']


def _load_cursor(build_id):
    """Where the walk stands in the list ``build_id``; 0 for a new list."""
    cached = _memory['cursor']
    if cached and cached.get('build_id') == build_id:
        return cached['cursor']
    path = _state_path(CURSOR_FILE)
    cursor = 0
    if path is not None:
        try:
            with open(path, encoding='utf-8') as handle:
                stored = json.load(handle)
            if (stored.get('build_id') == build_id
                    and isinstance(stored.get('cursor'), int)
                    and stored['cursor'] >= 0):
                cursor = stored['cursor']
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    _memory['cursor'] = {'build_id': build_id, 'cursor': cursor}
    return cursor


def _save_cursor(build_id, cursor):
    _memory['cursor'] = {'build_id': build_id, 'cursor': cursor}
    path = _state_path(CURSOR_FILE)
    if path is not None:
        try:
            _write_json(path, {'build_id': build_id, 'cursor': cursor})
        except OSError:
            pass


def rewind():
    """Send the walk back to the head of the current list.  Returns its id.

    For a list whose head was passed over without being searched: the walk
    meets every key again, and ``drip`` checks each one again as always.  A
    selector that is already running keeps its own place in memory and takes
    the new one when it starts.
    """
    plan = load_plan()
    if not plan:
        return None
    _save_cursor(plan.get('build_id', ''), 0)
    return plan.get('build_id', '')


def forget_plan():
    """Drop the list and the cursor, on disk too: the sweep stops."""
    _memory.update(plan=None, cursor=None, plan_mtime=None)
    for name in (PLAN_FILE, CURSOR_FILE):
        path = _state_path(name)
        if path is not None:
            try:
                os.remove(path)
            except OSError:
                pass


# ------------------------------------------------------------- the drip ------

# ------------------------------------------------------- the fleet share ---
#
# Owner and community, 11-Nov-2026: the sweep gets a FIXED SHARE of the leases
# even while requests are waiting, about the compute the mate-stop worker
# (2026111101) frees on closures.  Without it the sweep would never run: the
# request queue has not been empty for weeks.
#
# HOW A LEASE DECIDES.  A counter shared by every web process (the site's
# cache, Redis in production) counts the leases that could have taken a sweep
# task; lease ``n`` takes one when ``floor(n * share)`` steps up.  That is
# exactly ``share`` of those leases, evenly spread, the same for every worker
# whatever its size.  Without a working cache it falls back to a coin.
#
# CHEAP WHEN THERE IS NOTHING.  Whether the sweep has waiting tasks is cached
# for ``WAITING_CACHE_SECONDS``: the drip says yes as soon as it queues, and a
# lease that found nothing says no, so a lease with an empty sweep costs one
# cache read and no query.
#
# ``ATOMICDB_SWEEP16_SHARE`` (environment or settings), a fraction between 0
# and 1; ``0`` turns the share off and leaves the order of service as before.
SHARE_SETTING = 'ATOMICDB_SWEEP16_SHARE'
SHARE_DEFAULT = 0.14
WAITING_CACHE_KEY = 'atomicdb:sweep16:waiting'
COUNTER_CACHE_KEY = 'atomicdb:sweep16:leases'
WAITING_CACHE_SECONDS = 60


def share():
    """The fraction of leases that serve the sweep first, 0 to 1."""
    raw = getattr(settings, SHARE_SETTING, None)
    if raw is None:
        raw = os.environ.get(SHARE_SETTING, '')
    try:
        value = float(str(raw).strip())
    except ValueError:
        return SHARE_DEFAULT
    if value != value:          # NaN
        return SHARE_DEFAULT
    return min(1.0, max(0.0, value))


def _cache():
    from django.core.cache import cache
    return cache


def note_waiting(waiting):
    try:
        _cache().set(WAITING_CACHE_KEY, bool(waiting), WAITING_CACHE_SECONDS)
    except Exception:           # noqa: BLE001 - a cache is never required
        pass


def has_waiting():
    """Are sweep tasks waiting?  Cached; one indexed query on a miss."""
    try:
        cached = _cache().get(WAITING_CACHE_KEY)
    except Exception:           # noqa: BLE001
        cached = None
    if cached is not None:
        return bool(cached)
    waiting = waiting_tasks().exists()
    note_waiting(waiting)
    return waiting


def waiting_tasks():
    """The sweep tasks a lease may take, oldest first."""
    return (AnalysisTask.objects
            .filter(state='PENDING', arm=SWEEP_ARM,
                    source=AnalysisTask.Source.FILL,
                    position__status='UNKNOWN')
            .order_by('id'))


def _next_lease_number():
    try:
        cache = _cache()
        try:
            return cache.incr(COUNTER_CACHE_KEY)
        except ValueError:
            cache.add(COUNTER_CACHE_KEY, 0, None)
            return cache.incr(COUNTER_CACHE_KEY)
    except Exception:           # noqa: BLE001
        return None


def take_share(fraction=None):
    """Should this lease serve the sweep first?"""
    fraction = share() if fraction is None else fraction
    if fraction <= 0 or not has_waiting():
        return False
    if fraction >= 1:
        return True
    number = _next_lease_number()
    if number is None:
        import random
        return random.random() < fraction
    return int(number * fraction) != int((number - 1) * fraction)


def user_backlog():
    return AnalysisTask.objects.filter(
        state='PENDING', source=AnalysisTask.Source.USER).count()


def pending_count():
    return AnalysisTask.objects.filter(state='PENDING', arm=SWEEP_ARM).count()


def _revive(task):
    """Put the sweep's own absorbed task back in the queue.  True if it did.

    An absorbed row is COMPLETED with no nodes and no machine: nobody searched
    it (§ ``ingest.absorb_tasks``).  It holds the one slot its position has
    for this visit, so without this the position could never be queued again.
    Its position still qualifies (the caller just checked), so the search it
    was made for is still owed.  ``created`` moves to now: the task waits from
    now on, and it counts against this hour's ceiling like a new one.
    """
    if task.arm != SWEEP_ARM or not ingest.is_absorbed(task):
        return False
    return bool(AnalysisTask.objects.filter(
        pk=task.pk, arm=SWEEP_ARM, state=AnalysisTask.TState.COMPLETED,
        nodes_searched=0, machine='',
    ).update(state=AnalysisTask.TState.PENDING, completed=None,
             created=timezone.now()))


def drip(cap=SWEEP_PENDING_CAP, user_backlog_max=SWEEP_USER_BACKLOG_MAX,
         scan=SWEEP_SCAN):
    """One step down the list.  Returns how many tasks it queued.

    Tops the sweep's own PENDING tasks up to ``cap``, within the hourly
    ceiling of its arm, and only while at most ``user_backlog_max`` requests
    are waiting (a negative value skips that check; so does a fleet share
    above zero, which serves the sweep whatever the requests).  Reads at most
    ``scan``
    keys, each checked again against the database: a position that closed,
    was searched again, or already has a live task is passed over for good.
    A task of the sweep that was absorbed without a search is queued again.
    """
    if cap <= 0 or ingest.arm_rate(SWEEP_ARM) <= 0:
        return 0
    plan = load_plan()
    if not plan or not plan.get('keys'):
        return 0
    pending = pending_count()
    room = cap - pending
    if room <= 0:
        return 0
    if (user_backlog_max >= 0 and share() <= 0
            and user_backlog() > user_backlog_max):
        return 0
    room = min(room, ingest._arm_budget_room(SWEEP_ARM))
    if room <= 0:
        return 0
    keys = plan['keys']
    build_id = plan.get('build_id', '')
    cursor = start = _load_cursor(build_id)
    made = 0
    while made < room and cursor < len(keys) and cursor - start < scan:
        batch = keys[cursor:cursor + min(SWEEP_BATCH, scan - (cursor - start))]
        positions = {pos.key: pos for pos in Position.objects.filter(
            key__in=batch).only('key', 'fen', 'status', 'visits',
                                'nodes_invested', 'eval_cp', 'backed_eval')}
        live = set(AnalysisTask.objects.filter(
            position_id__in=batch,
            state__in=(AnalysisTask.TState.PENDING,
                       AnalysisTask.TState.LEASED))
            .values_list('position_id', flat=True))
        used = 0
        with atomic():
            for key in batch:
                if made >= room:
                    break
                used += 1
                pos = positions.get(key)
                if (pos is None or key in live or not qualifies(pos)
                        or not ingest._still_reachable(pos)):
                    continue
                task, created = AnalysisTask.objects.get_or_create(
                    position=pos, generation=pos.visits,
                    defaults={'budget_nodes': SWEEP_NODES,
                              'multipv': SWEEP_MULTIPV,
                              'source': AnalysisTask.Source.FILL,
                              'arm': SWEEP_ARM})
                if created or _revive(task):
                    made += 1
        cursor += used
    _save_cursor(build_id, cursor)
    if made:
        note_waiting(True)
        DBEvent.objects.create(kind=SWEEP_EVENT, payload={
            'queued': made, 'pending_before': pending, 'cap': cap,
            'cursor': cursor, 'total': len(keys)})
    return made


def status():
    """What a person checking on the sweep wants to see, as a dict."""
    plan = load_plan()
    out = {'rate_per_hour': ingest.arm_rate(SWEEP_ARM),
           'pending': pending_count(),
           'leased': AnalysisTask.objects.filter(
               state='LEASED', arm=SWEEP_ARM).count(),
           'completed': AnalysisTask.objects.filter(
               state='COMPLETED', arm=SWEEP_ARM).count(),
           'user_backlog': user_backlog(),
           'plan': None}
    if plan:
        cursor = _load_cursor(plan.get('build_id', ''))
        out['plan'] = {key: plan.get(key) for key in (
            'build_id', 'built_at', 'tree_count', 'rest_count', 'tree_stats',
            'scan_stats', 'seconds')}
        out['plan']['cursor'] = cursor
        out['plan']['left'] = max(0, len(plan.get('keys', ())) - cursor)
    return out


def withdraw_pending():
    """Remove the sweep's tasks no worker has taken.  Returns how many.

    They are the server's own rows, never a person's request, and a PENDING
    row holds nothing but its slot: the next producer that wants the same
    position and visit simply creates it again.
    """
    deleted, _ = AnalysisTask.objects.filter(
        state='PENDING', arm=SWEEP_ARM).delete()
    return deleted
