"""Declared move orders of named positions (cz_eclipsia and MoKy, 20-Nov-2026).

What this file pins:

* a line is read from SAN, UCI or an explorer link, and is accepted only if
  it replays from the start position to exactly the named position, legal,
  without repeating a position and within the explorer's route limit;
* an approver sets or clears the line of any named position, catalogue or
  community, from the moderation page, and setting it materialises the
  whole line so its ``?play=`` link opens;
* a visitor's NEW or EDIT may carry a line, refused at once if it does not
  reach the position, and approving the suggestion declares it;
* every link to a name takes the declared line, else the catalogue line,
  else the bare position: the Openings page (and its API, additively), the
  search box, the name in the explorer header, the review rows and the
  Discord poll.
"""

from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from OpenBench.models import Profile

from . import (community_names, ingest, logic, name_flags_discord,
               opening_routes, openings, roots, views)
from .models import (Edge, OpeningNameFlag, OpeningNameSuggestion,
                     OpeningRoute, Position)
from .testing import TestCase

# The community's "Lesha Spam" and the order it is played in.
LESHA = 'Lesha Spam'
LESHA_SAN = '1. Nf3 f6 2. Nc3 c6 3. e4 e6 4. Nb5 cxb5 5. Bb5 Nc6 6. d4'
LESHA_ROUTE = ('g1f3', 'f7f6', 'b1c3', 'c7c6', 'e2e4', 'e7e6', 'c3b5',
               'c6b5', 'f1b5', 'b8c6', 'd2d4')
LESHA_KEY = ('88c94030124a6276fa62c7266dd3351d9c857a87c7228229d236f53be8dc4e9b')
# The same position by another order, the way the tree may have met it.
OTHER_ROUTE = ('e2e4', 'e7e6', 'g1f3', 'f7f6', 'b1c3', 'c7c6', 'c3b5',
               'c6b5', 'f1b5', 'b8c6', 'd2d4')
# 1.Nf3 f6 2.e3 g6, catalogue record atomix-0087, and a transposition.
MODERN = 'Modern Defence'
MODERN_ROUTE = ('g1f3', 'f7f6', 'e2e3', 'g7g6')
MODERN_OTHER = ('e2e3', 'g7g6', 'g1f3', 'f7f6')


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


def _name(pos, name=LESHA):
    OpeningNameSuggestion.objects.create(
        position=pos, proposed_name=name, kind='NEW', ip='127.0.0.1',
        status='APPROVED', resolved_by='boss', resolved_at=timezone.now())
    community_names.invalidate()


def _play(route):
    return '?play=' + ','.join(route)


class _Base(TestCase):

    def setUp(self):
        community_names.invalidate()
        opening_routes.invalidate()
        self.addCleanup(community_names.invalidate)
        self.addCleanup(opening_routes.invalidate)

    def _lesha(self):
        """The Lesha Spam position, met by the other order, named."""
        pos = _walk(OTHER_ROUTE)[-1]
        assert pos.key == LESHA_KEY
        _name(pos)
        return pos


class ParseAndValidateTests(_Base):

    def test_san_uci_and_links_read_the_same_line(self):
        for raw in (LESHA_SAN, ','.join(LESHA_ROUTE), ' '.join(LESHA_ROUTE),
                    'Nf3 f6 Nc3 c6 e4 e6 Nb5 cxb5 Bb5 Nc6 d4',
                    f'https://example.org/atomicdb/explore/{LESHA_KEY}/'
                    + _play(LESHA_ROUTE)):
            with self.subTest(raw=raw):
                self.assertEqual(opening_routes.parse(raw), list(LESHA_ROUTE))

    def test_an_empty_text_is_no_line(self):
        self.assertEqual(opening_routes.parse('  '), [])

    def test_an_illegal_san_move_is_named(self):
        with self.assertRaisesRegex(opening_routes.LineError,
                                    r'move 2 for White \("Qh5"\)'):
            opening_routes.parse('1. Nf3 f6 2. Qh5')

    def test_a_fen_header_is_refused(self):
        with self.assertRaises(opening_routes.LineError):
            opening_routes.parse('[FEN "8/8/8/8/8/8/8/8 w - - 0 1"] 1. e4')

    def test_the_line_has_to_end_on_the_position(self):
        _fens, keys = opening_routes.validate(LESHA_ROUTE, LESHA_KEY)
        self.assertEqual(keys[-1], LESHA_KEY)
        self.assertEqual(opening_routes.validate(OTHER_ROUTE, LESHA_KEY)[1][-1],
                         LESHA_KEY)
        with self.assertRaisesRegex(opening_routes.LineError, 'does not end'):
            opening_routes.validate(LESHA_ROUTE[:-1], LESHA_KEY)

    def test_illegal_repeating_and_long_lines_are_refused(self):
        for line, why in (
                (('g1f3', 'f7f6', 'e2e5'), 'not a legal'),
                (('g1f3', 'g8f6', 'f3g1', 'f6g8'), 'repeats'),
                (('g1f3', 'g8f6', 'f3g1', 'f6g8', 'g1f3'), 'repeats'),
                ((), 'no moves')):
            with self.subTest(line=line), \
                    self.assertRaisesRegex(opening_routes.LineError, why):
                opening_routes.validate(line)
        with self.assertRaisesRegex(opening_routes.LineError, 'at most'):
            opening_routes.validate(['a2a3'] * (opening_routes.MAX_PLIES + 1))

    def test_the_limits_are_the_explorers(self):
        self.assertEqual(opening_routes.MAX_PLIES, views.PLAY_ROUTE_MAX_PLIES)
        self.assertEqual(opening_routes.MAX_CHARS, views.PLAY_ROUTE_MAX_CHARS)
        self.assertEqual(opening_routes.MAX_PLIES, roots.PLAY_LINK_MAX_PLIES)


class ApproverLineTests(_Base):

    def _set(self, line, position='', client=None, action='set'):
        client = client or self.approver
        return client.post('/atomicdb/suggestions/line/',
                           {'position': position, 'line': line,
                            'action': action})

    def setUp(self):
        super().setUp()
        self.approver = _approver()

    def test_an_approver_sets_a_line_from_san_and_it_is_materialised(self):
        pos = self._lesha()

        response = self._set(LESHA_SAN)

        self.assertIn('line=set', response['Location'])
        row = OpeningRoute.objects.get()
        self.assertEqual((row.position_key, row.line, row.line_san,
                          row.set_by),
                         (pos.key, ','.join(LESHA_ROUTE), LESHA_SAN, 'boss'))
        # Every position of the line, with its edge, so the link opens.
        nodes = [Position.objects.get(key=logic.key_of(logic.start_fen()))]
        for uci in LESHA_ROUTE:
            edge = Edge.objects.get(parent=nodes[-1], move_uci=uci)
            nodes.append(edge.child)
        self.assertEqual(nodes[-1].key, LESHA_KEY)
        page = self.client.get(f'/atomicdb/explore/{LESHA_KEY}/'
                               + _play(LESHA_ROUTE))
        self.assertEqual(page.status_code, 200)

    def test_the_position_given_has_to_be_where_the_line_ends(self):
        pos = self._lesha()
        modern = _walk(MODERN_ROUTE)[-1]

        self.assertIn('line=elsewhere',
                      self._set(LESHA_SAN, modern.key)['Location'])
        self.assertIn('line=set', self._set(
            LESHA_SAN, f'/atomicdb/explore/{pos.key}/')['Location'])

    def test_an_unnamed_or_unreadable_line_is_refused(self):
        self._lesha()
        self.assertIn('line=unnamed',
                      self._set('1. a3 a6 2. Ra2')['Location'])
        self.assertIn('line=invalid',
                      self._set('1. Nf3 f6 2. Qh5')['Location'])
        self.assertFalse(OpeningRoute.objects.exists())

    def test_a_catalogue_name_takes_an_override(self):
        modern = _walk(MODERN_ROUTE)[-1]
        self.assertIn('line=set',
                      self._set(' '.join(MODERN_OTHER))['Location'])
        self.assertEqual(opening_routes.routes(),
                         {modern.key: MODERN_OTHER})

    def test_clear_and_unchanged(self):
        pos = self._lesha()
        self._set(LESHA_SAN)
        self.assertIn('line=unchanged', self._set(LESHA_SAN)['Location'])

        response = self._set('', pos.key, action='clear')

        self.assertIn('line=cleared', response['Location'])
        self.assertFalse(OpeningRoute.objects.exists())
        self.assertEqual(opening_routes.routes(), {})
        self.assertIn('line=missing',
                      self._set('', pos.key, action='clear')['Location'])

    def test_only_an_approver_can(self):
        self._lesha()
        user = User.objects.create_user('pleb', password='pw')
        Profile.objects.create(user=user, enabled=True)
        client = Client()
        client.force_login(user)

        self._set(LESHA_SAN, client=client)
        self._set(LESHA_SAN, client=Client())

        self.assertFalse(OpeningRoute.objects.exists())

    def test_the_moderation_page_lists_the_lines(self):
        self._lesha()
        self._set(LESHA_SAN)

        body = self.approver.get('/atomicdb/suggestions/').content.decode()

        self.assertIn('id="lines"', body)
        self.assertIn(LESHA_SAN, body)
        self.assertIn(f'/atomicdb/explore/{LESHA_KEY}/'
                      + _play(LESHA_ROUTE), body)


class SuggestedLineTests(_Base):

    def setUp(self):
        super().setUp()
        self.pos = _walk(OTHER_ROUTE)[-1]

    def _suggest(self, line, name=LESHA, comment=''):
        return self.client.post(f'/atomicdb/suggest/{self.pos.key}/',
                                {'name': name, 'comment': comment,
                                 'line': line}, REMOTE_ADDR='10.0.0.7')

    def test_a_suggestion_carries_its_line_as_uci(self):
        response = self._suggest(LESHA_SAN)

        self.assertIn('suggested=ok', response['Location'])
        row = OpeningNameSuggestion.objects.get()
        self.assertEqual((row.kind, row.line), ('NEW', ','.join(LESHA_ROUTE)))

    def test_a_line_that_does_not_reach_the_position_is_refused(self):
        response = self._suggest('1. Nf3 f6 2. Nc3')

        self.assertIn('suggested=line-invalid', response['Location'])
        self.assertFalse(OpeningNameSuggestion.objects.exists())

    def test_without_a_line_nothing_changes(self):
        self._suggest('')
        self.assertEqual(OpeningNameSuggestion.objects.get().line, '')

    def test_approving_it_declares_the_line(self):
        self._suggest(LESHA_SAN)
        approver = _approver()
        row = OpeningNameSuggestion.objects.get()
        body = approver.get('/atomicdb/suggestions/').content.decode()
        self.assertIn(f'move order: {LESHA_SAN}', body)

        approver.post('/atomicdb/suggestions/',
                      {'suggestion': row.id, 'action': 'approve'})

        route = OpeningRoute.objects.get()
        self.assertEqual((route.position_key, route.line, route.set_by),
                         (LESHA_KEY, ','.join(LESHA_ROUTE), 'boss'))

    def test_rejecting_it_does_not(self):
        self._suggest(LESHA_SAN)
        row = OpeningNameSuggestion.objects.get()
        _approver().post('/atomicdb/suggestions/',
                         {'suggestion': row.id, 'action': 'reject'})
        self.assertFalse(OpeningRoute.objects.exists())

    def test_a_hide_request_carries_no_line(self):
        _name(self.pos)
        self._suggest(LESHA_SAN, name='', comment='not an opening')
        row = OpeningNameSuggestion.objects.get(kind='HIDE')
        self.assertEqual(row.line, '')


class NameLinkTests(_Base):

    def test_the_default_is_the_catalogue_line_then_nothing(self):
        modern_key = logic.key_of(_walk(MODERN_ROUTE)[-1].fen)
        self.assertEqual(opening_routes.name_url(modern_key),
                         f'/atomicdb/explore/{modern_key}/'
                         + _play(MODERN_ROUTE))
        self.assertEqual(opening_routes.name_url(LESHA_KEY),
                         f'/atomicdb/explore/{LESHA_KEY}/')

    def test_the_declared_line_wins(self):
        modern = _walk(MODERN_ROUTE)[-1]
        opening_routes.set_route(modern.key, MODERN_OTHER, by='boss')
        self.assertEqual(opening_routes.name_url(modern.key),
                         f'/atomicdb/explore/{modern.key}/'
                         + _play(MODERN_OTHER))

    def test_the_openings_page_and_its_api(self):
        self._lesha()
        modern = _walk(MODERN_ROUTE)[-1]
        roots.sync_watched_keys()

        def rows():
            return {row['key']: row for row in roots.api_context()['openings']}

        before = rows()
        self.assertEqual(before[LESHA_KEY]['route'], [])
        self.assertEqual(before[modern.key]['route'], list(MODERN_ROUTE))

        opening_routes.set_route(LESHA_KEY, LESHA_ROUTE, by='boss')
        after = rows()
        self.assertEqual(after[LESHA_KEY]['route'], list(LESHA_ROUTE))
        # ``line`` is still the catalogue's, as the API always said.
        self.assertEqual(after[LESHA_KEY]['line'], [])
        body = self.client.get('/atomicdb/roots/').content.decode()
        self.assertIn(f'href="/atomicdb/explore/{LESHA_KEY}/'
                      f'{_play(LESHA_ROUTE)}">{LESHA}</a>', body)
        self.assertIn(LESHA_SAN, body)

    def test_the_search_box(self):
        self._lesha()

        response = self.client.post('/atomicdb/fen/', {'fen': LESHA})
        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{LESHA_KEY}/')

        opening_routes.set_route(LESHA_KEY, LESHA_ROUTE, by='boss')
        response = self.client.post('/atomicdb/fen/', {'fen': LESHA})
        self.assertEqual(response['Location'],
                         f'/atomicdb/explore/{LESHA_KEY}/'
                         + _play(LESHA_ROUTE))

    def test_a_catalogue_line_only_where_the_tree_can_open_it(self):
        modern = openings.lookup_key(
            logic.key_of(self._fen_of(MODERN_ROUTE)))
        self.assertEqual(modern['name'], MODERN)
        key = modern['position_key']
        pairs = [(key, MODERN_ROUTE)]
        self.assertEqual(opening_routes.openable(pairs), {})
        _walk(MODERN_ROUTE)
        self.assertEqual(opening_routes.openable(pairs), {key: MODERN_ROUTE})
        # A line that ends elsewhere is never kept.
        self.assertEqual(opening_routes.openable([(LESHA_KEY, MODERN_ROUTE)]),
                         {})

    def test_the_search_results_list(self):
        pos = self._lesha()
        other = _walk(MODERN_ROUTE)[-1]
        _name(other, 'Lesha Spam Deferred')
        opening_routes.set_route(pos.key, LESHA_ROUTE, by='boss')

        body = self.client.post('/atomicdb/fen/',
                                {'fen': 'lesha'}).content.decode()

        self.assertIn(f'href="/atomicdb/explore/{LESHA_KEY}/'
                      f'{_play(LESHA_ROUTE)}">{LESHA}</a>', body)
        self.assertIn(LESHA_SAN, body)

    def test_the_name_in_the_explorer_header(self):
        modern = _walk(MODERN_ROUTE)[-1]
        opening = {'position_key': modern.key, 'matched_ply': 4,
                   'reference_line_uci': list(MODERN_ROUTE)}
        child = 'f' * 64
        self.assertEqual(views._opening_name_url(opening, child, None, None),
                         views._explore_url(modern.key, list(MODERN_ROUTE)))
        opening_routes.set_route(modern.key, MODERN_OTHER, by='boss')
        self.assertEqual(views._opening_name_url(opening, child, None, None),
                         views._explore_url(modern.key, list(MODERN_OTHER)))
        # Reached along the page's own route, that route still wins.
        keys = opening_routes.validate(MODERN_ROUTE)[1]
        self.assertEqual(
            views._opening_name_url(opening, child, list(MODERN_ROUTE) + [
                'a2a3'], list(keys) + [child]),
            views._explore_url(modern.key, list(MODERN_ROUTE)))

    def test_the_review_row_and_the_discord_poll(self):
        pos = self._lesha()
        opening_routes.set_route(pos.key, LESHA_ROUTE, by='boss')
        flag = OpeningNameFlag.objects.create(position_key=pos.key,
                                              name=LESHA, community=True)
        url = f'/atomicdb/explore/{LESHA_KEY}/' + _play(LESHA_ROUTE)

        body = _approver().get('/atomicdb/suggestions/').content.decode()
        self.assertIn(f'href="{url}"', body)
        content = name_flags_discord.payload(flag)['content']
        self.assertIn(url, content)
        self.assertIn(f'Line: {LESHA_SAN}', content)

    @staticmethod
    def _fen_of(route):
        fen = logic.start_fen()
        for uci in route:
            fen = logic.apply_move(fen, uci)
        return fen
