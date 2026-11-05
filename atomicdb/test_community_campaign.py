"""The most-voted open campaign on an unsolved root is the active one
(midbored's proposal, adopted by the owner on 5-Nov-2026)."""

from io import StringIO

from django.core.management import call_command

from . import community_campaign, ingest, logic
from .models import Campaign, Position
from .test_campaigns import _campaign_on, _line
from .testing import TestCase


class RotateTests(TestCase):

    def setUp(self):
        self.a = _line('g1f3')
        self.b = _line('g1f3', 'e7e5')
        self.c = _line('g1f3', 'f7f6')

    def _state(self, campaign):
        return Campaign.objects.get(id=campaign.id).state

    def test_the_most_voted_proposal_becomes_active(self):
        low = _campaign_on(self.a, Campaign.CState.PROPOSED, votes=2, name='low')
        high = _campaign_on(self.b, Campaign.CState.PROPOSED, votes=9, name='high')

        summary = community_campaign.rotate()

        self.assertEqual(summary['activated'], 'high')
        self.assertEqual(self._state(high), Campaign.CState.ACTIVE)
        self.assertEqual(self._state(low), Campaign.CState.PROPOSED)
        # The subtree below the root is tagged as the owner's button does.
        self.assertEqual(Position.objects.get(key=self.b.key).campaign_id, high.id)

    def test_a_tie_keeps_the_incumbent(self):
        incumbent = _campaign_on(self.a, Campaign.CState.ACTIVE, votes=5, name='inc')
        challenger = _campaign_on(self.b, Campaign.CState.PROPOSED, votes=5, name='ch')

        summary = community_campaign.rotate()

        self.assertEqual(summary['active'], 'inc')
        self.assertIsNone(summary['activated'])
        self.assertEqual(self._state(challenger), Campaign.CState.PROPOSED)

    def test_more_votes_take_over_and_the_old_one_is_paused(self):
        old = _campaign_on(self.a, Campaign.CState.ACTIVE, votes=5, name='old')
        new = _campaign_on(self.b, Campaign.CState.PROPOSED, votes=6, name='new')

        community_campaign.rotate()

        self.assertEqual(self._state(new), Campaign.CState.ACTIVE)
        self.assertEqual(self._state(old), Campaign.CState.PAUSED)

    def test_a_solved_root_never_wins(self):
        Position.objects.filter(key=self.c.key).update(status='WHITE_WIN')
        solved = _campaign_on(self.c, Campaign.CState.PROPOSED, votes=50, name='solved')
        open_ = _campaign_on(self.a, Campaign.CState.PROPOSED, votes=1, name='open')

        community_campaign.rotate()

        self.assertEqual(self._state(open_), Campaign.CState.ACTIVE)
        self.assertEqual(self._state(solved), Campaign.CState.PROPOSED)

    def test_only_one_stays_active(self):
        one = _campaign_on(self.a, Campaign.CState.ACTIVE, votes=3, name='one')
        two = _campaign_on(self.b, Campaign.CState.ACTIVE, votes=7, name='two')

        community_campaign.rotate()

        self.assertEqual(Campaign.objects.filter(
            state=Campaign.CState.ACTIVE).count(), 1)
        self.assertEqual(self._state(two), Campaign.CState.ACTIVE)
        self.assertEqual(self._state(one), Campaign.CState.PAUSED)

    def test_the_command_prints_its_summary(self):
        _campaign_on(self.a, Campaign.CState.PROPOSED, votes=1, name='cmd')
        out = StringIO()
        call_command('rotate_community_campaign', stdout=out)
        self.assertIn('"active": "cmd"', out.getvalue())
