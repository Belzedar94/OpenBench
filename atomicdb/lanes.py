"""Who shares the fleet with whom: contributor lanes.

WHAT A LANE IS.  The unit that receives an equal share of the fleet.  An
account that ran a worker inside the contributor window gets a lane of its
own; everybody else, named or anonymous, shares one lane called the commons.
The community voted for this on 10 August: "only plugging in CPU earns you a
lane", which is the whole point.  Creating alt accounts buys nothing, because
every alt lands in the same commons and the commons is one lane however many
people are standing in it.

WHY IT IS DERIVED AND NEVER STORED.  A lane with nothing queued does not exist:
it is not a row anywhere, it is what the queue looks like when you group it by
the accounts that are currently in it.  So an absent contributor never holds
capacity, membership refreshes itself on the next read, and there is no cron,
no backfill and no state that can go stale.  The price is one small query per
lease call, and it is the same query the queue page already pays.

THE WINDOW IS SHARED WITH THE DEPTH SELECTOR ON PURPOSE.  ``depth.may_choose``
has been asking "did this account run a worker in the last seven days" since
the rung selector shipped, and two implementations of one question drift the
day one of them changes.  The predicate lives here now and both read it.  What
does NOT move here is the owner shortcut: ``is_staff`` opens the rung selector
and it does not open a lane, because the vote said CPU is what earns a lane and
the owner runs workers like everybody else.
"""

from datetime import timedelta

from django.db.models import Count, Min, Sum
from django.utils import timezone

from .models import AnalysisTask, WorkerPing

# The commons is keyed by the empty string, which is also what an anonymous
# request already carries in ``requested_by``.  That is not a coincidence worth
# hiding: the anonymous tide is one member of the commons (it cannot be told
# apart, so it cannot be shared out), and the sentinel that means "no account"
# is exactly the sentinel that means "the shared lane".
LANE_COMMONS = ''
# How long a worker keeps its owner's lane open after it goes quiet.  More
# generous than any presence gauge on the site (240s on the contributor page,
# minutes on the home meters) on purpose: those count LIVE capacity, this
# answers "does this person hold the project up", which is a question of weeks
# and not of seconds.  Someone who switches the machine off for the weekend
# keeps their lane; someone who lent an afternoon three months ago does not.
CONTRIBUTOR_WINDOW_DAYS = 7


def window_start(now=None):
    """The oldest worker sighting that still counts as contributing."""
    return (now or timezone.now()) - timedelta(days=CONTRIBUTOR_WINDOW_DAYS)


def _recent_worker_accounts(now=None):
    """Account names with a worker seen inside the window.  One query.

    Reads ``WorkerPing.last_seen``, which is ``auto_now`` and indexed, so the
    row of a machine that has been off for a month is still there and simply
    falls out of the window.  That is the property the lane wants: the table
    remembers, the window forgets.
    """
    return set(WorkerPing.objects
               .filter(last_seen__gte=window_start(now))
               .values_list('user', flat=True))


def _enabled(names):
    """The subset of ``names`` whose OpenBench profile is enabled.

    Two queries and an intersection in Python rather than one join, because
    the database router FORBIDS relations between the AtomicDB database and
    the OpenBench one (§ ``models.RequestNotification``, same reason its
    ``username`` is text and not a foreign key).  The number of accounts with
    a worker is tens, so the set fits in memory by construction.

    A revoked account loses its lane and falls back to the commons.  It is the
    same gate that already closes the worker protocol (``views._auth``) and the
    rung selector (``depth.may_choose``): a privilege that survives revocation
    is not a privilege.
    """
    if not names:
        return set()
    from OpenBench.models import Profile
    return set(Profile.objects
               .filter(user__username__in=sorted(names), enabled=True)
               .values_list('user__username', flat=True))


def contributor_accounts(now=None):
    """Every account that currently owns a lane, as a frozenset of names."""
    return frozenset(_enabled(_recent_worker_accounts(now)))


def ran_a_worker(username, now=None):
    """Did THIS account run a worker inside the window?  One row lookup.

    The single-name form, for the callers that already know whose account they
    are asking about and must not pay for the whole set (the rung selector runs
    on every explorer render).
    """
    if not username:
        return False
    return (WorkerPing.objects
            .filter(user=username, last_seen__gte=window_start(now))
            .exists())


def lane_of(requested_by, contributors):
    """The lane a request belongs to: its own account, or the commons.

    ``contributors`` is the set from ``contributor_accounts``.  Passing it in
    rather than looking it up keeps this a pure function, which is what lets
    the ordering resolve thousands of rows against one query.
    """
    if requested_by and requested_by in contributors:
        return requested_by
    return LANE_COMMONS


# ---------------- what the queue looks like, grouped by lane ----------------

# How far back the served-share column looks.  One hour is the window over
# which the fleet turns the queue over several times (about 1,100 tasks an hour
# on the pool that has actually been measured), so it shows what the queue is
# doing NOW rather than what it did over a day that has already ended.
SERVED_WINDOW = timedelta(hours=1)
LANE_CACHE_KEY = 'atomicdb.lanes.table.v1'
LANE_CACHE_SECONDS = 60


def _charged_by_requester(now):
    """Per account: waiting rows, running rows, charged nodes, oldest wait.

    One GROUP BY over the same band the ordering charges (§
    ``live_request.CHARGED`` and ``SERVEABLE``): pending plus leased, over
    positions still unknown.  The predicates are imported rather than rewritten
    so that the number this page publishes is the number the queue enforces.
    """
    from .live_request import CHARGED, SERVEABLE
    rows = (AnalysisTask.objects
            .filter(source=AnalysisTask.Source.USER)
            .filter(CHARGED).filter(SERVEABLE)
            .values('requested_by', 'state')
            .annotate(rows=Count('id'), nodes=Sum('budget_nodes'),
                      oldest=Min('created'))
            .order_by())
    folded = {}
    for row in rows:
        entry = folded.setdefault(row['requested_by'],
                                  {'waiting': 0, 'running': 0, 'nodes': 0,
                                   'oldest': None})
        entry['nodes'] += row['nodes'] or 0
        if row['state'] == AnalysisTask.TState.PENDING:
            entry['waiting'] += row['rows']
            # Only a WAITING row has an age worth printing.  A leased row is
            # not waiting for anybody, it is being searched right now.
            if row['oldest'] is not None and (entry['oldest'] is None
                                              or row['oldest'] < entry['oldest']):
                entry['oldest'] = row['oldest']
        else:
            entry['running'] += row['rows']
    return folded


def _served_by_requester(now):
    """Nodes actually delivered per account inside the served window.

    ``nodes_searched`` and not ``budget_nodes``: the share this column reports
    is what the fleet SPENT, not what somebody asked it to spend.
    """
    rows = (AnalysisTask.objects
            .filter(source=AnalysisTask.Source.USER,
                    state=AnalysisTask.TState.COMPLETED,
                    completed__gte=now - SERVED_WINDOW)
            .values('requested_by')
            .annotate(nodes=Sum('nodes_searched'))
            .order_by())
    return {row['requested_by']: row['nodes'] or 0 for row in rows}


def measure_lanes(now):
    """The queue grouped by lane: one row per lane that currently has work.

    Three GROUP BY over the task table plus the contributor lookup, which is
    why it is published through the shared snapshot rather than recomputed per
    visitor (§ ``metrics.shared_snapshot``, the pattern ``contributors.fleet``
    already uses for the fleet totals).
    """
    contributors = contributor_accounts(now)
    charged = _charged_by_requester(now)
    served = _served_by_requester(now)

    lane_rows = {}
    for requester in set(charged) | set(served):
        lane = lane_of(requester, contributors)
        entry = lane_rows.setdefault(lane, {
            'lane': lane,
            'contributor': lane != LANE_COMMONS,
            'members': set(),
            'waiting': 0, 'running': 0, 'nodes': 0, 'served': 0,
            'oldest': None,
        })
        mine = charged.get(requester)
        if mine is not None:
            # A member is somebody with work IN the queue.  Having been served
            # an hour ago and having nothing queued now is not membership: the
            # lane share is divided among the people currently standing in it.
            entry['members'].add(requester)
            entry['waiting'] += mine['waiting']
            entry['running'] += mine['running']
            entry['nodes'] += mine['nodes']
            if mine['oldest'] is not None and (
                    entry['oldest'] is None or mine['oldest'] < entry['oldest']):
                entry['oldest'] = mine['oldest']
        entry['served'] += served.get(requester, 0)

    total_served = sum(entry['served'] for entry in lane_rows.values())
    for entry in lane_rows.values():
        entry['member_count'] = len(entry['members'])
        entry['share'] = (round(100.0 * entry['served'] / total_served, 1)
                          if total_served else 0.0)
        del entry['members']
    # Busiest lane first, and a lane with nothing queued but recent deliveries
    # sinks to the bottom rather than disappearing: it explains where the last
    # hour went, which is exactly what somebody checking fairness came to see.
    return {
        'rows': sorted(lane_rows.values(),
                       key=lambda row: (-row['waiting'], -row['nodes'],
                                        row['lane'] or '~')),
        'served_total': total_served,
        'served_minutes': int(SERVED_WINDOW.total_seconds() // 60),
    }


def lane_table(now=None):
    """``measure_lanes`` behind the shared 60 second snapshot."""
    from . import metrics
    return metrics.shared_snapshot(LANE_CACHE_KEY, build=measure_lanes,
                                   required=True, now=now,
                                   fresh_seconds=LANE_CACHE_SECONDS)
