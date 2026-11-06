"""Reach of the PROPOSED campaigns: how big each line is and how much is done.

An active campaign owns its subtree (``Position.campaign_id``, adopted at
activation by ``views._tag_campaign_subtree``), so its progress is a grouped
count over the tagged rows (§ ``metrics.campaign_progress``).  A proposed one
owns nothing and MUST NOT: tagging is what gives the selector bonus and the
ownership of transpositions, and that is the owner's decision at activation.

So a proposed line is measured by walking the tree: a read-only BFS over
``Edge`` from the campaign root, capped at ``MAX_NODES`` positions, then the
same three counts as the front page (positions, explored, solved) over the
keys it reached.  Measured on the live tree (6-Nov-2026) one line capped at
~538K nodes cost 35 s of walk plus 38 s of counting, and there are a couple
dozen proposals: this is a job for a timer (§ ``measure_proposed_campaigns``),
never for a page view.  The page only reads the stored result.

When the walk hits the cap the totals are LOWER BOUNDS (``capped``), and the
page says so with a ``+``.  Transpositions reached twice count once.
"""

from django.core.cache import cache
from django.db.models import Count, Q
from django.utils import timezone

from .models import Campaign, Edge, Position

CACHE_KEY = 'atomicdb.proposed-campaign-stats.v1'
# The timer runs once a day.  Three days of life: a run or two that fail
# leave the last good numbers up (with their age on the page) instead of
# blanking the list; after three days without a run they go away.
CACHE_TTL_SECONDS = 72 * 3600
MAX_NODES = 300_000
EDGE_BATCH = 2000
COUNT_BATCH = 5000
STATEMENT_TIMEOUT_MS = 60_000


def walk_subtree(root_key, *, cap=MAX_NODES, batch=EDGE_BATCH):
    """Keys reachable from ``root_key`` (root included), and whether capped.

    Children are fetched one batch of parents at a time and the walk stops
    the moment the cap is reached, so a wide level never materialises more
    than one batch of child ids beyond the cap.
    """
    seen = {root_key}
    frontier = [root_key]
    capped = False
    while frontier and not capped:
        nxt = []
        for start in range(0, len(frontier), batch):
            for child in Edge.objects.filter(
                    parent_id__in=frontier[start:start + batch],
            ).values_list('child_id', flat=True):
                if child in seen:
                    continue
                if len(seen) >= cap:
                    capped = True
                    break
                seen.add(child)
                nxt.append(child)
            if capped:
                break
        frontier = nxt
    return seen, capped


def count_positions(keys, *, batch=COUNT_BATCH):
    """``{'total', 'explored', 'resolved'}`` over ``keys``, in batches."""
    keys = list(keys)
    out = {'total': 0, 'explored': 0, 'resolved': 0}
    for start in range(0, len(keys), batch):
        row = Position.objects.filter(
            key__in=keys[start:start + batch]).aggregate(
                total=Count('key'),
                explored=Count('key', filter=Q(nodes_invested__gt=0)),
                resolved=Count('key', filter=~Q(status='UNKNOWN')))
        for name in out:
            out[name] += row[name] or 0
    return out


def measure_campaign(campaign, *, cap=MAX_NODES, now=None):
    keys, capped = walk_subtree(campaign.root_id, cap=cap)
    row = count_positions(keys)
    row['capped'] = capped
    row['measured_at'] = now or timezone.now()
    return row


def stored():
    """``{campaign_id: row}`` from the last runs (empty when never measured)."""
    entry = cache.get(CACHE_KEY)
    return dict(entry.get('campaigns', {})) if entry else {}


def _publish(rows):
    cache.set(CACHE_KEY, {'campaigns': rows}, CACHE_TTL_SECONDS)


def measure_all(*, cap=MAX_NODES, log=None):
    """Measure every PROPOSED campaign and publish after EACH one.

    Rows of campaigns that are no longer proposed are dropped; rows of the
    ones still waiting their turn in this run keep the previous value, so a
    run that dies half way leaves the page as complete as it was.  Never
    writes to the database.
    """
    proposed = list(Campaign.objects.filter(state=Campaign.CState.PROPOSED)
                    .order_by('id'))
    ids = {campaign.id for campaign in proposed}
    rows = {cid: row for cid, row in stored().items() if cid in ids}
    _publish(rows)
    for campaign in proposed:
        started = timezone.now()
        row = measure_campaign(campaign, cap=cap)
        rows[campaign.id] = row
        _publish(rows)
        if log is not None:
            log(campaign, row, (timezone.now() - started).total_seconds())
    return rows
