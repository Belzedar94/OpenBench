"""Reach of the proposed campaigns: a read-only walk, stored, shown as is.

What is pinned here: the walk counts the subtree under the root (each
transposition once) and stops at its cap saying so; it never tags a proposed
line, never measures anything that is not proposed; and the campaigns page
shows the stored numbers (lower bounds when capped) or nothing at all.
"""

import html as html_module
import io
import re
from datetime import timedelta

from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from . import ingest, logic, proposed_campaigns
from .models import Campaign, Edge, Position
from .testing import TestCase


def _reading(body):
    return ' '.join(html_module.unescape(re.sub(r'<[^>]+>', ' ', body)).split())


def _child(parent, uci):
    child = ingest.get_or_create_position(logic.apply_move(parent.fen, uci))
    Edge.objects.get_or_create(parent=parent, move_uci=uci,
                               defaults={'child': child})
    return child


def _campaign(root, state=Campaign.CState.PROPOSED, name='1. Nf3'):
    return Campaign.objects.create(
        name=name, root=root, line_san=name, state=state,
        active=state == Campaign.CState.ACTIVE)


class TreeFixture(TestCase):
    """start -> {e4, Nf3}; the line is Nf3 -> {d6, f6} -> Ng5 under each,
    plus one extra edge f6 -> (d6 Ng5) standing for a transposition.

    Five positions under the root, three explored, one solved; e4 is outside
    the line and is explored and solved, so counting it would show."""

    def setUp(self):
        super().setUp()
        self.start = ingest.get_or_create_position(logic.start_fen())
        self.outside = _child(self.start, 'e2e4')
        self.root = _child(self.start, 'g1f3')
        self.d6 = _child(self.root, 'd7d6')
        self.f6 = _child(self.root, 'f7f6')
        self.d6_ng5 = _child(self.d6, 'f3g5')
        self.f6_ng5 = _child(self.f6, 'f3g5')
        # A transposition: the same child reached from two parents.
        Edge.objects.create(parent=self.f6, move_uci='h7h6',
                            child=self.d6_ng5)
        Position.objects.filter(key__in=[self.root.key, self.d6.key]).update(
            nodes_invested=1000)
        Position.objects.filter(key=self.d6_ng5.key).update(
            nodes_invested=500, status='WHITE_WIN')
        # Outside the line: must not be counted.
        Position.objects.filter(key=self.outside.key).update(
            nodes_invested=9, status='WHITE_WIN')


class WalkTests(TreeFixture):

    def test_the_walk_counts_the_subtree_once_per_position(self):
        row = proposed_campaigns.measure_campaign(_campaign(self.root))
        self.assertEqual(row['total'], 5)
        self.assertEqual(row['explored'], 3)
        self.assertEqual(row['resolved'], 1)
        self.assertFalse(row['capped'])

    def test_nothing_under_a_solved_position_is_counted(self):
        # d6 solved: d6 itself counts, its Ng5 child is reached only through
        # the h6 transposition from f6, which is still open.
        Position.objects.filter(key=self.d6.key).update(status='BLACK_WIN')
        grand = _child(self.d6_ng5, 'e7e6')
        keys, _ = proposed_campaigns.walk_subtree(self.root.key)
        self.assertIn(self.d6.key, keys)
        self.assertIn(self.d6_ng5.key, keys)
        # d6_ng5 is solved too, so its child is not walked.
        self.assertNotIn(grand.key, keys)

    def test_a_solved_root_counts_only_itself(self):
        Position.objects.filter(key=self.root.key).update(status='WHITE_WIN')
        keys, capped = proposed_campaigns.walk_subtree(self.root.key)
        self.assertEqual(keys, {self.root.key})
        self.assertFalse(capped)

    def test_the_walk_stops_at_its_cap_and_says_so(self):
        keys, capped = proposed_campaigns.walk_subtree(self.root.key, cap=3)
        self.assertEqual(len(keys), 3)
        self.assertIn(self.root.key, keys)
        self.assertTrue(capped)

    def test_a_cap_exactly_the_size_of_the_tree_is_not_capped(self):
        _keys, capped = proposed_campaigns.walk_subtree(self.root.key, cap=5)
        self.assertFalse(capped)

    def test_measuring_never_tags_a_proposed_line(self):
        _campaign(self.root)
        call_command('measure_proposed_campaigns', stdout=io.StringIO())
        self.assertFalse(Position.objects.filter(
            campaign__isnull=False).exists())

    def test_proposed_and_paused_campaigns_are_measured(self):
        proposed = _campaign(self.root)
        active = _campaign(self.d6, state=Campaign.CState.ACTIVE, name='x')
        paused = _campaign(self.f6, state=Campaign.CState.PAUSED, name='y')
        call_command('measure_proposed_campaigns', stdout=io.StringIO())
        stored = proposed_campaigns.stored()
        self.assertIn(proposed.id, stored)
        self.assertNotIn(active.id, stored)
        # A paused campaign is listed with the proposed ones (it can win the
        # lead back), so its reach is measured too.
        self.assertIn(paused.id, stored)

    def test_a_line_no_longer_proposed_drops_out_of_the_store(self):
        campaign = _campaign(self.root)
        proposed_campaigns.measure_all()
        Campaign.objects.filter(id=campaign.id).update(
            state=Campaign.CState.ACTIVE)
        proposed_campaigns.measure_all()
        self.assertNotIn(campaign.id, proposed_campaigns.stored())

    def test_the_command_reports_one_line_per_campaign(self):
        _campaign(self.root)
        out = io.StringIO()
        call_command('measure_proposed_campaigns', '--max-nodes', '2',
                     stdout=out)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn('"capped": true', lines[0])


class PageTests(TreeFixture):

    def _page(self):
        return _reading(Client().get('/atomicdb/campaigns/').content.decode())

    def test_an_unmeasured_line_shows_no_numbers(self):
        _campaign(self.root)
        body = self._page()
        self.assertIn('1. Nf3', body)
        self.assertNotIn('positions explored', body)
        self.assertNotIn('measured 0', body)
        self.assertNotIn('positions counted', body)

    def test_a_measured_line_shows_the_home_card_numbers(self):
        _campaign(self.root)
        proposed_campaigns.measure_all()
        body = self._page()
        self.assertIn('3 of 5 positions explored', body)
        self.assertIn('1 solved', body)
        self.assertIn('20.0% of the line solved', body)
        self.assertIn('measured 0', body)

    def test_a_capped_line_shows_lower_bounds(self):
        _campaign(self.root)
        proposed_campaigns.measure_all(cap=3)
        body = self._page()
        self.assertIn('3+ positions', body)
        self.assertIn('% of the positions counted solved', body)
        self.assertNotIn('of the line solved', body)

    def test_the_page_says_how_old_the_numbers_are(self):
        campaign = _campaign(self.root)
        proposed_campaigns._publish({campaign.id: {
            'total': 538_000, 'explored': 12_000, 'resolved': 40,
            'capped': True,
            'measured_at': timezone.now() - timedelta(hours=3)}})
        body = self._page()
        self.assertIn('538,000+ positions', body)
        self.assertIn('measured 3', body)
        self.assertIn('hours ago', body)

    def test_the_front_page_does_not_show_proposed_reach(self):
        _campaign(self.root)
        proposed_campaigns.measure_all()
        body = _reading(Client().get('/atomicdb/').content.decode())
        self.assertNotIn('positions explored', body)


class PausedListedTests(TestCase):

    def test_a_paused_campaign_is_on_the_campaigns_page(self):
        root = ingest.get_or_create_position(logic.start_fen())
        Campaign.objects.create(root=root, name='Paused line', line_san='',
                                state=Campaign.CState.PAUSED, votes=8)
        body = self.client.get('/atomicdb/campaigns/').content.decode()
        self.assertIn('Paused line', body)
