"""The public list of active workers: what each one is searching, and for whom.

WHAT COUNTS AS ACTIVE.  Exactly what the "active workers" number on the home
page counts (§ metrics, ``LIVE_SECONDS``): a ``WorkerPing`` with a heartbeat
in the last three minutes.  That number links here, so the rows of this page
have to be the machines behind it, one row per ping, busy or not.

WHAT A ROW SAYS, AND WHERE IT WAS ALREADY PUBLIC.  The page only groups what
other pages publish one at a time:

* the machine, its owner, threads, hash and OS: the workers table of each
  contributor page (§ contributors.present);
* the line, the budget and the source of the task it is searching: the "Now
  analyzing" column of the home page, which shows five of them;
* who asked for it: the contributor page lists the account's running
  requests with the machine serving them, and the queue page has the
  anonymous visitors as one row.  Somebody who joined a request is a count
  and never a name, as on the contributor page (§ AnalysisTask.backers);
* and one figure of the page's own, the worker's speed: the nodes per second
  of the searches it finished in the last hour, from the nodes and the time
  every finished search records (§ contributors.speeds; views.api_submit
  keeps the last one as ``last_nps``).  An average of dozens of searches and
  not the heartbeat of the moment, it neither jumps from one position to the
  next nor drops to zero between tasks, and a faster machine shows within
  the hour.  The contributor page shows the same figure.

THE ROWS GO BY ACCOUNT (cz_eclipsia, 9-Nov-2026).  One collapsed row per
account, three cells like the worker table's: the account, its workers'
speed added up and the searches they finished in the last hour (the same
hour and the same read as the speed).  Opening it says how many of its
workers are live, their threads and what they are doing, then shows that
account's rows.  The accounts with the most
workers come first, then the fastest.  The groups are built from the rows
above, so they cost no read of their own.

Nothing else of the ping reaches the template: the rows are built here from
named fields, and the lease token never leaves the database (the query only
asks whether there is one).

WHICH TASK A WORKER IS ON follows the rules of "Now analyzing" (§ views.home):
the exact task a fresh worker publishes in its heartbeat, and for a build too
old to publish one, the tokenless lease its machine holds.  A modern worker
whose engine stopped advancing clears its task, and then it is between tasks
here exactly as it is absent there.  The rules are restated rather than
shared because ``home`` keeps them inline; ``test_active_workers`` pins the
two pages to the same answer.

WHAT IT COSTS.  Three indexed reads whatever the size of the fleet: the live
pings (``last_seen`` is indexed), the live leases of those machines (the
LEASED band of ``state``, a few rows) and the searches those machines
finished in the last hour (the COMPLETED band of the (state, completed)
index: one hour of searches, never the whole table).  The lines come from the
lineage and route label caches the home page already fills, so a warm page
reads nothing else, and a cold one pays the reverse walk once per ply for
all rows at once.  The one cost that grows with the rows is the known one of
every page that shows declared routes: a cold route is revalidated with one
primary-key read (§ views._validated_play_route).
"""

import re
from datetime import timedelta

from django.db.models import BooleanField, Case, Q, Value, When
from django.shortcuts import render
from django.utils import timezone

from . import contributors, idle_repair, metrics, views
from .models import AnalysisTask, WorkerPing

USER = AnalysisTask.Source.USER


def live_pings(now):
    """The pings the home page counts as active workers, in a stable order.

    Same field, same comparison and same window as ``metrics._compute``: the
    number on the home page is ``len()`` of this list taken at its own moment.
    """
    since = now - timedelta(seconds=metrics.LIVE_SECONDS)
    pings = list(WorkerPing.objects.filter(last_seen__gte=since).only(
        'machine', 'user', 'threads', 'hash_mb', 'os', 'current_task_id'))
    pings.sort(key=lambda ping: (ping.user.lower(), ping.user,
                                 _natural(ping.machine), ping.machine))
    return pings


def _natural(text):
    """``box#2`` before ``box#10``: a ``--jobs`` machine lists its slots
    in order."""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r'(\d+)', text or '')]


def current_tasks(pings, now):
    """``{ping.pk: lease row}`` for the pings that are searching right now.

    A lease row is a dict of the task fields this page shows.  A task is
    given to one ping at most: two accounts can announce the same machine
    name, and a lease held under that name is still one search.  That is why
    the exact tasks are handed out before the fallback for old builds, which
    only knows the machine name.
    """
    machines = sorted({ping.machine for ping in pings})
    if not machines:
        return {}
    lease_cutoff = now - timedelta(minutes=views.LEASE_MINUTES)
    legacy_cutoff = now - timedelta(minutes=views.LEGACY_DISPLAY_MINUTES)
    leases = list(
        AnalysisTask.objects
        .filter(state=AnalysisTask.TState.LEASED, machine__in=machines)
        .filter(Q(lease_token='', leased_at__gte=legacy_cutoff)
                | Q(lease_token__gt='', lease_heartbeat_at__gte=lease_cutoff)
                | Q(lease_token__gt='', lease_heartbeat_at__isnull=True,
                    leased_at__gte=lease_cutoff))
        .annotate(legacy=Case(When(lease_token='', then=Value(True)),
                              default=Value(False),
                              output_field=BooleanField()))
        .order_by('machine', 'leased_at', 'id')
        .values('id', 'position_id', 'budget_nodes', 'source', 'arm',
                'requested_by', 'also_requested_by', 'route', 'machine',
                'leased_at', 'legacy'))
    by_id = {row['id']: row for row in leases}
    legacy_by_machine = {}
    for row in leases:
        if row['legacy']:
            legacy_by_machine.setdefault(row['machine'], row)
    current, taken = {}, set()
    for exact in (True, False):
        for ping in pings:
            if ping.pk in current:
                continue
            row = (by_id.get(ping.current_task_id) if exact
                   else legacy_by_machine.get(ping.machine))
            if row is not None and row['id'] not in taken:
                current[ping.pk] = row
                taken.add(row['id'])
    return current


def _requester(row):
    """Who asked for the task: an account, an anonymous visitor or nobody."""
    if row['source'] != USER:
        # AUTO and FILL: the selector and its arms chose it, nobody asked.
        # The idle fleet's repairs say so, being searches the tree already
        # had once (§ idle_repair).
        if row.get('arm') == idle_repair.REPAIR_ARM:
            return {'requester': 'server', 'reason': 'repair'}
        return {'requester': 'server'}
    if not row['requested_by']:
        return {'requester': 'anonymous'}
    return {'requester': 'account',
            'requested_by': row['requested_by'],
            'more': len(row['also_requested_by'] or [])}


def _task_rows(current, now):
    """The searching part of each busy row, with its line already readable.

    The declared route wins over the canonical lineage when it still holds,
    as on the home page; the lineage walk is only paid for the rest.
    """
    rows = list(current.values())
    routes = views._route_labels_many(
        [(row['route'], row['position_id']) for row in rows if row['route']])
    plain = [row['position_id'] for row in rows
             if (row['route'], row['position_id']) not in routes]
    labels = views._line_labels_many(plain)
    presented = {}
    for ping_pk, row in current.items():
        routed = routes.get((row['route'], row['position_id']))
        if routed is not None:
            preview, full = routed[:2]
        else:
            preview, full = labels.get(row['position_id'], ('', ''))
        presented[ping_pk] = {
            'key': row['position_id'],
            'san': preview or 'start position',
            'full': full or 'start position',
            'play': row['route'] if routed is not None else '',
            'budget': views._human(row['budget_nodes']),
            'source': row['source'],
            'since': contributors._ago(row['leased_at'], now),
            **_requester(row),
        }
    return presented


def present(now=None):
    """The whole context of the page.  Reads only."""
    now = now or timezone.now()
    pings = live_pings(now)
    tasks = _task_rows(current_tasks(pings, now), now)
    speed = contributors.speeds(pings, now)
    workers, groups = [], {}
    for ping in pings:
        nps, searches = speed.get((ping.user, ping.machine), (0, 0))
        group = groups.setdefault(ping.user, {'owner': ping.user, 'nps': 0.0,
                                              'searches': 0, 'workers': []})
        group['nps'] += nps
        group['searches'] += searches
        workers.append({
            'machine': ping.machine,
            'owner': ping.user,
            # Capped like the "active cores" total of the home page, so the
            # threads summed here are that number.
            'threads': min(views.MAX_REPORTED_THREADS,
                           max(0, ping.threads or 0)),
            'hash_mb': ping.hash_mb,
            'os': ping.os,
            'task': tasks.get(ping.pk),
            'nps_h': views._human(round(nps)) if nps else '',
            'searches': searches,
            'searches_h': views._human(searches),
        })
        group['workers'].append(workers[-1])
    threads = sum(worker['threads'] for worker in workers)
    return {
        'workers': workers,
        'groups': _groups(groups.values()),
        'count': len(workers),
        'busy': sum(1 for worker in workers if worker['task']),
        'accounts': len({worker['owner'] for worker in workers}),
        'threads': threads,
        'threads_h': views._human(threads),
        'live_minutes': metrics.LIVE_SECONDS // 60,
    }


def _groups(groups):
    """One summary per account, the most workers first, then the fastest.

    Within an account the rows keep the order of ``live_pings``.
    """
    presented = []
    for group in groups:
        rows = group['workers']
        tasks = [row['task'] for row in rows if row['task']]
        threads = sum(row['threads'] for row in rows)
        requests = sum(1 for task in tasks if task['source'] == USER)
        presented.append({
            'owner': group['owner'],
            'workers': rows,
            'count': len(rows),
            'threads': threads,
            'threads_h': views._human(threads),
            'nps': group['nps'],
            'nps_h': (views._human(round(group['nps']))
                      if group['nps'] else ''),
            'searches': group['searches'],
            'searches_h': views._human(group['searches']),
            'busy': len(tasks),
            'requests': requests,
            'server': len(tasks) - requests,
            'idle': len(rows) - len(tasks),
        })
    presented.sort(key=lambda group: (-group['count'], -group['nps'],
                                      group['owner'].lower(), group['owner']))
    for group in presented:
        del group['nps']
    return presented


def workers_page(request):
    return render(request, 'atomicdb/workers.html', present())
