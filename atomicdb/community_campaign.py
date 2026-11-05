"""The community picks the line the automatic queue pushes.

Until 5-Nov-2026 a campaign was worked only once the site owner activated
it, and the votes merely ordered the list.  The owner handed that choice to
the vote (midbored's proposal: the auto queue works either on solving, or
on the line the community chooses): the most-voted open campaign whose root
is still unsolved is the ACTIVE one, and the selector's campaign bonus
(§ ingest.CAMPAIGN_BONUS) pushes it.  Everything else the selector does,
the solving work, goes on beside it.

Activation goes through the same door as the owner's button
(§ views.campaign_state): the state change and the tagging of the subtree
that already exists, in one transaction.  A campaign that loses the lead is
PAUSED, which keeps its tags, so winning it back costs nothing.

Hysteresis: the incumbent keeps its place on a tie, and a challenger has to
have strictly more votes.  Without it two campaigns a vote apart would swap
on every pass, and every swap re-tags a subtree.
"""

from .database import atomic
from .models import Campaign

OPEN = (Campaign.CState.PROPOSED, Campaign.CState.ACTIVE,
        Campaign.CState.PAUSED)


def _candidates():
    """Open campaigns on an unsolved root, most voted first, oldest first."""
    return list(Campaign.objects
                .filter(state__in=OPEN, root__status='UNKNOWN')
                .select_related('root')
                .order_by('-votes', 'id'))


def rotate():
    """Make the most-voted open campaign the active one.  Returns a summary.

    ``{'active': name or None, 'activated': name or None,
       'paused': [names], 'tagged': n}``.
    """
    from .views import _tag_campaign_subtree

    summary = {'active': None, 'activated': None, 'paused': [], 'tagged': 0}
    with atomic():
        candidates = _candidates()
        actives = [c for c in candidates if c.state == Campaign.CState.ACTIVE]
        # An active campaign on a solved root is closed by the ingest
        # (§ ingest, CAMPAIGN_CLOSED); one that slipped past is not a
        # candidate and is simply paused below.
        stale = list(Campaign.objects
                     .filter(state=Campaign.CState.ACTIVE)
                     .exclude(id__in=[c.id for c in candidates]))
        leader = candidates[0] if candidates else None
        incumbent = actives[0] if actives else None
        winner = leader
        if (incumbent is not None and leader is not None
                and leader.votes <= incumbent.votes):
            winner = incumbent
        for campaign in actives[1:] + stale + (
                [incumbent] if incumbent and incumbent != winner else []):
            if campaign.state == Campaign.CState.ACTIVE:
                campaign.apply_state(Campaign.CState.PAUSED)
                summary['paused'].append(campaign.name)
        if winner is not None and winner.state != Campaign.CState.ACTIVE:
            winner.apply_state(Campaign.CState.ACTIVE)
            tagged, _visited = _tag_campaign_subtree(winner)
            summary['activated'] = winner.name
            summary['tagged'] = tagged
        summary['active'] = winner.name if winner is not None else None
    return summary
