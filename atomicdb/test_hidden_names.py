"""Hidden opening names (math_god, 18-Nov-2026).

What this file pins:

* an approver can hide the name a position shows, catalogue or community,
  and the catalogue JSON stays as published;
* a visitor can ask for it with an EMPTY name and a reason, and approving
  that suggestion hides the name through the ordinary approval path;
* a hidden position reads as unnamed everywhere: explorer header, query API,
  the Openings page, votes.  Its line keeps the name of the named position
  before it, and so do its descendants;
* the latest approval still wins: a later EDIT or NEW names it again, and
  "show again" gives back the name it had.
"""

import re

from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from OpenBench.models import Profile

from . import community_names, ingest, logic, name_votes, openings
from .models import Edge, OpeningNameFlag, OpeningNameSuggestion
from .testing import TestCase

# 1.Nf3 f6 2.e3 g6, catalogue record atomix-0087.
MODERN_ROUTE = ('g1f3', 'f7f6', 'e2e3', 'g7g6')
MODERN_KEY = ('88612826848caf7b7dd4c7cc7c452d7b'
              '9833bcffc715d4976dd94b5f3ee7022a')
MODERN = 'Modern Defence'
UNNAMED_ROUTE = ('a2a3', 'a7a6', 'a1a2')


def _walk(route):
    """Every position of ``route`` with its edges, start position first."""
    nodes = [ingest.get_or_create_position(logic.start_fen())]
    for uci in route:
        child = ingest.get_or_create_position(
            logic.apply_move(nodes[-1].fen, uci))
        Edge.objects.get_or_create(parent=nodes[-1], move_uci=uci,
                                   defaults={'child': child})
        nodes.append(child)
    return nodes


def _approver(name='boss'):
    user = User.objects.create_user(name, password='pw')
    Profile.objects.create(user=user, enabled=True, approver=True)
    client = Client()
    client.force_login(user)
    return client


def _header(body):
    """The opening name the explorer prints, or ``None``."""
    match = re.search(r'id="opening-name"[^>]*>(.*?)</h2>', body, re.S)
    if match is None:
        return None
    return ' '.join(re.sub(r'<[^>]+>', ' ', match.group(1)).split())


class _Base(TestCase):

    def setUp(self):
        community_names.invalidate()
        self.addCleanup(community_names.invalidate)
        self.nodes = _walk(MODERN_ROUTE)
        self.modern = self.nodes[-1]
        assert self.modern.key == MODERN_KEY
        assert openings.lookup_key(MODERN_KEY)['name'] == MODERN
        # What the line reads with the Modern Defence taken out: the named
        # position before it.
        before = openings.match_line_keys([n.key for n in self.nodes[:-1]])
        self.before = before['name'] if before else None
        assert self.before and self.before != MODERN

    def _page(self, pos, play=None):
        url = f'/atomicdb/explore/{pos.key}/'
        if play:
            url += '?play=' + ','.join(play)
        return self.client.get(url)

    def _api(self, pos):
        return self.client.get('/atomicdb/api/query',
                               {'fen': pos.fen, 'opening': '1'}).json()

    def _hide(self, pos, client=None, comment='not a real opening'):
        client = client or _approver()
        return client.post('/atomicdb/suggestions/hide/',
                           {'position': pos.key, 'comment': comment,
                            'action': 'hide'})


class HideCatalogueNameTests(_Base):

    def test_an_approver_hides_a_catalogue_name(self):
        response = self._hide(self.modern)

        self.assertIn('hide=hidden', response['Location'])
        row = OpeningNameSuggestion.objects.get()
        self.assertEqual((row.kind, row.status, row.previous_name,
                          row.proposed_name, row.resolved_by, row.comment),
                         ('HIDE', 'APPROVED', MODERN, '', 'boss',
                          'not a real opening'))
        self.assertIn(MODERN_KEY, community_names.hidden_keys())
        # The catalogue itself still says what it said.
        self.assertEqual(openings.lookup_key(MODERN_KEY)['name'], MODERN)
        self.assertEqual(community_names.catalog_name(MODERN_KEY), MODERN)

    def test_the_header_reads_the_name_before_it(self):
        self._hide(self.modern)

        for play in (MODERN_ROUTE, None):
            with self.subTest(play=play):
                body = self._page(self.modern, play).content.decode()
                self.assertEqual(_header(body), self.before)
                self.assertNotIn(MODERN, _header(body))

    def test_the_query_api_reads_the_name_before_it(self):
        self._hide(self.modern)

        data = self._api(self.modern)

        self.assertEqual(data['opening'], self.before)
        self.assertIsNone(data['opening_named_by'])

    def test_its_descendants_inherit_from_further_up(self):
        keys = set(openings.load_catalog())
        uci = next(m for m in sorted(logic.legal_moves(self.modern.fen))
                   if logic.key_of(logic.apply_move(self.modern.fen, m))
                   not in keys)
        child = _walk(MODERN_ROUTE + (uci,))[-1]
        self._hide(self.modern)

        body = self._page(child, MODERN_ROUTE + (uci,)).content.decode()

        self.assertEqual(_header(body), self.before)
        self.assertEqual(self._api(child)['opening'], self.before)

    def test_it_leaves_the_openings_page_and_cannot_be_voted(self):
        self._hide(self.modern)

        self.assertNotIn(MODERN_KEY, name_votes.current_names())
        self.assertEqual(name_votes.current_name(MODERN_KEY), (None, None))
        body = self.client.get('/atomicdb/roots/').content.decode()
        self.assertNotIn(f'op-{MODERN_KEY}', body)

    def test_the_openings_page_lists_it_while_shown(self):
        body = self.client.get('/atomicdb/roots/').content.decode()
        self.assertIn(f'op-{MODERN_KEY}', body)

    def test_a_position_without_a_name_cannot_be_hidden(self):
        unnamed = _walk(UNNAMED_ROUTE)[-1]

        response = self._hide(unnamed)

        self.assertIn('hide=unnamed', response['Location'])
        self.assertFalse(OpeningNameSuggestion.objects.exists())

    def test_only_an_approver_can_hide(self):
        user = User.objects.create_user('plain', password='pw')
        Profile.objects.create(user=user, enabled=True, approver=False)
        client = Client()
        client.force_login(user)

        self._hide(self.modern, client=client)
        Client().post('/atomicdb/suggestions/hide/',
                      {'position': MODERN_KEY, 'action': 'hide'})

        self.assertFalse(OpeningNameSuggestion.objects.exists())

    def test_an_explorer_link_is_accepted_as_the_position(self):
        _approver().post('/atomicdb/suggestions/hide/', {
            'position': f'https://example.org/atomicdb/explore/{MODERN_KEY}/'
                        '?play=g1f3,f7f6,e2e3,g7g6',
            'action': 'hide'})

        self.assertIn(MODERN_KEY, community_names.hidden_keys())


class HideCommunityNameTests(_Base):

    def setUp(self):
        super().setUp()
        self.line = _walk(UNNAMED_ROUTE)
        self.pos = self.line[-1]
        OpeningNameSuggestion.objects.create(
            position=self.pos, proposed_name='Belfast Gambit',
            ip='10.0.0.9', suggested_by='lesha',
            status=OpeningNameSuggestion.SState.APPROVED,
            resolved_by='boss', resolved_at=timezone.now())
        community_names.invalidate()

    def test_a_flagged_community_name_is_hidden_from_the_review(self):
        flag, _ = name_votes.flag(self.pos.key, 'Belfast Gambit',
                                  community_names.in_force(self.pos.key),
                                  0, 4)

        response = _approver().post('/atomicdb/suggestions/flags/',
                                    {'flag': flag.id, 'action': 'hide'})

        self.assertIn('review=hidden', response['Location'])
        flag.refresh_from_db()
        self.assertEqual(flag.resolution, OpeningNameFlag.Resolution.HIDE)
        self.assertIsNone(community_names.name_for(self.pos.key))
        self.assertIn(self.pos.key, community_names.hidden_keys())
        # The line reads what the catalogue says before it, if anything.
        nodes = _walk(UNNAMED_ROUTE + ('a6a5',))
        above = openings.match_line_keys([n.key for n in nodes])
        above = above['name'] if above else None
        self.assertEqual(self._api(nodes[-1])['opening'], above)
        self.assertEqual(self._api(self.pos)['opening'], above)

    def test_a_catalogue_name_can_be_hidden_from_the_review_too(self):
        flag, _ = name_votes.flag(MODERN_KEY, MODERN, None, 0, 4)

        outcome = name_votes.apply_flag(flag.id, 'hide', approver='boss',
                                        ip='10.0.0.1')

        self.assertEqual(outcome, 'hidden')
        self.assertIn(MODERN_KEY, community_names.hidden_keys())

    def test_a_later_edit_names_it_again(self):
        self._hide(self.pos)
        OpeningNameSuggestion.objects.create(
            position=self.pos, proposed_name='Belfast Countergambit',
            ip='10.0.0.9', kind=OpeningNameSuggestion.SKind.EDIT,
            status=OpeningNameSuggestion.SState.APPROVED,
            resolved_by='boss', resolved_at=timezone.now())
        community_names.invalidate()

        self.assertNotIn(self.pos.key, community_names.hidden_keys())
        self.assertEqual(self._api(self.pos)['opening'],
                         'Belfast Countergambit')

    def test_show_again_gives_back_the_community_name_and_its_credit(self):
        self._hide(self.pos)

        response = _approver('mod').post('/atomicdb/suggestions/hide/', {
            'position': self.pos.key, 'action': 'show'})

        self.assertIn('hide=shown', response['Location'])
        data = self._api(self.pos)
        self.assertEqual((data['opening'], data['opening_named_by']),
                         ('Belfast Gambit', 'lesha'))


class EmptyNameSuggestionTests(_Base):

    def _suggest(self, pos, **data):
        payload = {'name': '', 'comment': 'only a move order, no opening'}
        payload.update(data)
        return self.client.post(f'/atomicdb/suggest/{pos.key}/', payload)

    def test_an_empty_name_with_a_reason_is_filed_as_a_hide(self):
        response = self._suggest(self.modern)

        self.assertIn('suggested=hide-ok', response['Location'])
        row = OpeningNameSuggestion.objects.get()
        self.assertEqual((row.kind, row.status, row.previous_name,
                          row.proposed_name, row.comment),
                         ('HIDE', 'PENDING', MODERN, '',
                          'only a move order, no opening'))
        # Nothing changes before an approver says so.
        self.assertNotIn(MODERN_KEY, community_names.hidden_keys())

    def test_an_empty_name_needs_a_reason(self):
        response = self._suggest(self.modern, comment='  ')

        self.assertIn('suggested=hide-why', response['Location'])
        self.assertFalse(OpeningNameSuggestion.objects.exists())

    def test_the_queue_shows_it_and_approving_it_hides_the_name(self):
        self._suggest(self.modern)
        approver = _approver()
        body = approver.get('/atomicdb/suggestions/').content.decode()
        self.assertIn('HIDE NAME:', body)
        self.assertIn('only a move order, no opening', body)
        row = OpeningNameSuggestion.objects.get()

        approver.post('/atomicdb/suggestions/',
                      {'suggestion': row.id, 'action': 'approve'})

        row.refresh_from_db()
        self.assertEqual((row.status, row.kind), ('APPROVED', 'HIDE'))
        self.assertIn(MODERN_KEY, community_names.hidden_keys())
        self.assertEqual(self._api(self.modern)['opening'], self.before)
        body = approver.get('/atomicdb/suggestions/').content.decode()
        self.assertIn('id="hidden"', body)
        self.assertIn('Show again', body)

    def test_a_new_name_after_a_hidden_catalogue_name_is_approved(self):
        self._hide(self.modern)
        self.client.post(f'/atomicdb/suggest/{MODERN_KEY}/',
                         {'name': 'Nimzo Fianchetto'})
        row = OpeningNameSuggestion.objects.get(status='PENDING')
        self.assertEqual(row.kind, 'NEW')

        _approver('mod').post('/atomicdb/suggestions/',
                              {'suggestion': row.id, 'action': 'approve'})

        row.refresh_from_db()
        # It replaces the catalogue there, so it is approved as an edit.
        self.assertEqual((row.status, row.kind), ('APPROVED', 'EDIT'))
        self.assertNotIn(MODERN_KEY, community_names.hidden_keys())
        self.assertEqual(self._api(self.modern)['opening'],
                         'Nimzo Fianchetto')

    def test_a_new_name_over_a_visible_catalogue_name_is_still_refused(self):
        row = OpeningNameSuggestion.objects.create(
            position=self.modern, proposed_name='Nimzo Fianchetto',
            ip='10.0.0.9')

        _approver().post('/atomicdb/suggestions/',
                         {'suggestion': row.id, 'action': 'approve'})

        row.refresh_from_db()
        self.assertEqual(row.status, 'REJECTED')

    def test_show_again_gives_back_the_catalogue_name(self):
        self._hide(self.modern)

        _approver('mod').post('/atomicdb/suggestions/hide/', {
            'position': MODERN_KEY, 'action': 'show'})

        self.assertNotIn(MODERN_KEY, community_names.hidden_keys())
        self.assertEqual(self._api(self.modern)['opening'], MODERN)
        self.assertIsNone(community_names.in_force(MODERN_KEY))
