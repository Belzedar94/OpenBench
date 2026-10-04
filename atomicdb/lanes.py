"""What contributing earns, and how a request rides a lighter queue.

CONTRIBUTOR LANES ARE GONE (16 August).  The 10 August vote created them: an
account that delivered worker results inside a window got a private share of
the fleet.  Wolfram's critique killed them, with the data agreeing: the
threshold was binary, so one delivered analysis bought the same share as a
thousand, and the rational move for anybody stuck in the commons was to run a
worker for one minute as a toll.  A repair (proportional shares, decay) would
have bought complexity to defend an idea nobody was attached to.  What
replaced them is the pre-lane arrangement everybody already understood:

- The queue is one commons.  Every account advances by the nodes it already
  has charged (§ ``live_request.fair_share``), so the first request of a
  newcomer beats the thousandth of a flooder, whoever either of them is.
- The ONLY scheduling advantage a contributor keeps is affinity: while your
  worker runs, it serves your own requests first (§ ``views.choose_pending``,
  ``own_first``).  That advantage scales with the CPU you are actually
  plugging in right now, which is the proportionality the lane window never
  had.

WHAT STILL LIVES HERE.

- The delivery-window predicate (``ran_a_worker``, ``contributor_accounts``).
  It no longer buys queue position; the rung selector (§ ``depth.may_choose``)
  still reads it, because choosing deep budgets is trusted to people who have
  put real work into the fleet recently.  ``last_result_at`` and deliberately
  not ``last_seen``: polling forever earns nothing, only a delivered analysis
  counts.
- The backing weight (``effective_account``, ``charged_loads``).  A request
  several people asked for is charged to the best placed of its requesters,
  which is what makes backing a buried request actually move it.
- The queue page table (``measure_queue``), one row per account, read from
  the same predicates the ordering uses, so the page and the queue cannot
  disagree without it being a bug in one place.
"""

import logging
from datetime import timedelta

from django.db.models import Count, Min, Sum
from django.utils import timezone

from .models import AnalysisTask, WorkerPing

logger = logging.getLogger(__name__)

# How long a delivery keeps counting for the rung selector.  Somebody who
# switches the machine off for the weekend stays trusted; someone who lent an
# afternoon three months ago does not.
CONTRIBUTOR_WINDOW_DAYS = 7


def window_start(now=None):
    """The oldest delivery that still counts as contributing."""
    return (now or timezone.now()) - timedelta(days=CONTRIBUTOR_WINDOW_DAYS)


def _recent_worker_accounts(now=None):
    """Account names that DELIVERED inside the window.  One query.

    ``last_result_at`` and deliberately not ``last_seen``: the second is
    ``auto_now`` and moves on any authenticated call, so a process that polls
    ``/api/lease`` forever and never returns an analysis would count as
    contributing without searching a single node.

    The row of a machine that has been off for a month is still there and
    simply falls out of the window.  That is the property the predicate wants:
    the table remembers, the window forgets.
    """
    return set(WorkerPing.objects
               .filter(last_result_at__gte=window_start(now))
               .values_list('user', flat=True))


def _enabled(names):
    """The subset of ``names`` whose OpenBench profile is enabled.

    Two queries and an intersection in Python rather than one join, because
    the database router FORBIDS relations between the AtomicDB database and
    the OpenBench one (§ ``models.RequestNotification``, same reason its
    ``username`` is text and not a foreign key).  The number of accounts with
    a worker is tens, so the set fits in memory by construction.

    A revoked account loses the privilege.  It is the same gate that already
    closes the worker protocol (``views._auth``) and the rung selector
    (``depth.may_choose``): a privilege that survives revocation is not a
    privilege.
    """
    if not names:
        return set()
    from OpenBench.models import Profile
    return set(Profile.objects
               .filter(user__username__in=sorted(names), enabled=True)
               .values_list('user__username', flat=True))


def contributor_accounts(now=None):
    """Every account inside the delivery window, as a frozenset of names."""
    return frozenset(_enabled(_recent_worker_accounts(now)))


def ran_a_worker(username, now=None):
    """Did THIS account deliver inside the window?  One row lookup.

    The single-name form, for the callers that already know whose account they
    are asking about and must not pay for the whole set (the rung selector runs
    on every explorer render).
    """
    if not username:
        return False
    return (WorkerPing.objects
            .filter(user=username, last_result_at__gte=window_start(now))
            .exists())


def effective_account(author, backers, loads):
    """Which of a request's requesters it rides with: the lightest queue.

    Reported live on 15 August (Eclipsia, on a request authored by soothdest).
    A request several people asked for was charged to its author alone, so with
    a saturated author it stayed buried under that author's own backlog, about
    2690 rows deep, and backing it changed nothing anybody could see.  That
    reads as the feature being broken, and it is a fair reading: the point of
    backing is that more people waiting means sooner, not later.

    ``loads`` maps an account to the nodes it already has charged.  Ties break
    on the AUTHOR, which is what keeps an unbacked request unchanged by
    construction: with one candidate there is nothing to choose.

    Returns '' when the author wins, because '' is what the column means by
    "nobody moved this" and writing the author's own name would be the same
    fact spelled twice.
    """
    candidates = [name for name in [author] + list(backers or []) if name]
    if len(candidates) < 2:
        return ''
    best = min(candidates,
               key=lambda name: (loads.get(name, 0), candidates.index(name)))
    return '' if best == author else best


def charged_loads(names):
    """``{account: charged nodes}`` for ``names``.  One GROUP BY.

    The same band the ordering charges, read through the same predicates, so
    "whose queue is lighter" means here exactly what it means there.
    """
    from .live_request import CHARGED, SERVEABLE
    if not names:
        return {}
    rows = (AnalysisTask.objects
            .filter(source=AnalysisTask.Source.USER,
                    requested_by__in=sorted(names))
            .filter(CHARGED).filter(SERVEABLE)
            .values('requested_by').annotate(load=Sum('budget_nodes'))
            .order_by())
    return {row['requested_by']: row['load'] or 0 for row in rows}


# ---------------- what the queue looks like, per account ----------------

# How far back the served-share column looks.  One hour is the window over
# which the fleet turns the queue over several times (about 1,100 tasks an hour
# on the pool that has actually been measured), so it shows what the queue is
# doing NOW rather than what it did over a day that has already ended.
SERVED_WINDOW = timedelta(hours=1)
QUEUE_CACHE_KEY = 'atomicdb.queue.table.v1'
QUEUE_CACHE_SECONDS = 60


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


def measure_queue(now):
    """The queue, one row per account that currently has work or was served.

    Two GROUP BY over the task table, which is why it is published through the
    shared snapshot rather than recomputed per visitor (§
    ``metrics.shared_snapshot``, the pattern ``contributors.fleet`` already
    uses for the fleet totals).  The anonymous tide is one row: it cannot be
    told apart, so it cannot be split up.
    """
    charged = _charged_by_requester(now)
    served = _served_by_requester(now)

    account_rows = {}
    for requester in set(charged) | set(served):
        entry = account_rows.setdefault(requester, {
            'account': requester,
            'waiting': 0, 'running': 0, 'nodes': 0, 'served': 0,
            'oldest': None,
        })
        mine = charged.get(requester)
        if mine is not None:
            entry['waiting'] += mine['waiting']
            entry['running'] += mine['running']
            entry['nodes'] += mine['nodes']
            if mine['oldest'] is not None and (
                    entry['oldest'] is None or mine['oldest'] < entry['oldest']):
                entry['oldest'] = mine['oldest']
        entry['served'] += served.get(requester, 0)

    total_served = sum(entry['served'] for entry in account_rows.values())
    for entry in account_rows.values():
        entry['share'] = (round(100.0 * entry['served'] / total_served, 1)
                          if total_served else 0.0)
    # Busiest account first, and an account with nothing queued but recent
    # deliveries sinks to the bottom rather than disappearing: it explains
    # where the last hour went, which is exactly what somebody checking
    # fairness came to see.
    return {
        'rows': sorted(account_rows.values(),
                       key=lambda row: (-row['waiting'], -row['nodes'],
                                        row['account'] or '~')),
        'served_total': total_served,
        'served_minutes': int(SERVED_WINDOW.total_seconds() // 60),
    }


def queue_table(now=None):
    """``measure_queue`` behind the shared 60 second snapshot."""
    from . import metrics
    return metrics.shared_snapshot(QUEUE_CACHE_KEY, build=measure_queue,
                                   required=True, now=now,
                                   fresh_seconds=QUEUE_CACHE_SECONDS)
