"""Fun side quests: the pages, and games of Alice Atomic between two people.

A game is two private links, one per side.  These tests walk what a pair of
players does with them: create, open, move, offer a draw, resign, poll; and
what the server refuses: illegal moves, moves out of turn, moves before the
opponent has arrived or after the end, and too many new games from one place.
"""

from datetime import timedelta

from django.test import Client
from django.utils import timezone

from .funquests import alice_atomic as rules
from .funquests import views as fun_views
from .models import AliceGame
from .testing import TestCase, worker_account

API = '/atomicdb/fun/alice-atomic/api/'


def token_of(url):
    return url.rstrip('/').rsplit('/', 1)[-1]


class PagesTests(TestCase):

    def test_the_quest_list_links_the_first_quest(self):
        response = self.client.get('/atomicdb/fun/')
        self.assertContains(response, 'Fun side quests')
        self.assertContains(response, '/atomicdb/fun/alice-atomic/')

    def test_the_nav_links_the_quests(self):
        response = self.client.get('/atomicdb/docs/')
        self.assertContains(response, 'href="/atomicdb/fun/"')

    def test_the_game_page_explains_the_rules(self):
        response = self.client.get('/atomicdb/fun/alice-atomic/')
        self.assertContains(response, 'The rules')
        self.assertContains(response, 'Capture here, explode there')
        self.assertContains(response, '<strong>Check.</strong>')
        self.assertContains(response, 'En passant')
        self.assertContains(response, 'alice_atomic.js')

    def test_the_rules_say_no_square_is_taken_on_both_boards(self):
        response = self.client.get('/atomicdb/fun/alice-atomic/')
        self.assertContains(response, 'a square is never occupied on both boards')
        # Conditions that this makes always true are not listed as rules.
        self.assertNotContains(response, 'which must be empty')
        self.assertNotContains(response, "the king's square must be empty")
        self.assertNotContains(response, 'Every move, captures included')

    def test_the_page_offers_the_one_board_view(self):
        response = self.client.get('/atomicdb/fun/alice-atomic/')
        self.assertContains(response, 'id="aa-view"')
        self.assertContains(response, 'id="aa-legend-one"')
        self.assertContains(response, 'id="aa-board-wrap-b"')

    def test_the_scripts_carry_the_cache_buster(self):
        from . import notifications
        response = self.client.get('/atomicdb/fun/alice-atomic/')
        for name in ('alice_atomic.js', 'alice_atomic_ui.js', 'funquests.css'):
            self.assertContains(
                response, f'funquests/{name}?v={notifications.STATIC_VERSION}')

    def test_an_unknown_game_link_is_a_404(self):
        response = self.client.get('/atomicdb/fun/alice-atomic/play/nope/')
        self.assertEqual(response.status_code, 404)

    def test_a_game_link_opens_the_board_and_stays_out_of_search(self):
        created = self.client.post(API + 'create', {'color': 'white'}).json()
        response = self.client.get(created['your_url'])
        self.assertContains(response, 'data-token="%s"'
                            % token_of(created['your_url']))
        self.assertContains(response, 'noindex')


class GameFlowTests(TestCase):

    def setUp(self):
        self.white = Client()
        self.black = Client()
        created = self.white.post(API + 'create', {'color': 'white'})
        self.assertEqual(created.status_code, 201)
        body = created.json()
        self.assertEqual(body['color'], 'white')
        self.wt = token_of(body['your_url'])
        self.bt = token_of(body['invite_url'])
        self.assertNotEqual(self.wt, self.bt)

    def join(self, client, token):
        return client.post(API + token + '/join').json()

    def move(self, client, token, text, **extra):
        return client.post(API + token + '/move', dict(move=text, **extra))

    def start(self):
        self.join(self.white, self.wt)
        return self.join(self.black, self.bt)

    def test_the_game_waits_for_the_second_player(self):
        state = self.join(self.white, self.wt)
        self.assertEqual(state['state'], 'waiting')
        self.assertEqual(state['you'], 'w')
        self.assertIn(self.bt, state['invite_url'])
        refused = self.move(self.white, self.wt, 'Ae2e4')
        self.assertEqual(refused.status_code, 409)

        state = self.join(self.black, self.bt)
        self.assertEqual(state['state'], 'live')
        self.assertEqual(state['you'], 'b')
        self.assertNotIn('invite_url', state)

    def test_moves_are_checked_and_played_in_turn(self):
        self.start()
        self.assertEqual(self.move(self.black, self.bt, 'Ae7e5').status_code,
                         409)
        self.assertEqual(self.move(self.white, self.wt, 'Ae2e5').status_code,
                         400)
        played = self.move(self.white, self.wt, 'Ae2e4', ply='0')
        self.assertEqual(played.status_code, 200)
        body = played.json()
        self.assertEqual(body['moves'], ['Ae2e4'])
        self.assertEqual(body['ply'], 1)
        self.assertEqual(body['afen'], rules.to_afen(
            rules.play(rules.start_state(), 'Ae2e4')))
        stale = self.move(self.black, self.bt, 'Ae7e5', ply='0')
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.move(self.black, self.bt, 'Ae7e5',
                                   ply='1').status_code, 200)

    def test_polling_answers_small_when_nothing_changed(self):
        state = self.start()
        version = state['version']
        quiet = self.black.get(API + self.bt + '/state',
                               {'since': version}).json()
        self.assertEqual(quiet, {'version': version, 'changed': False})
        cached = self.black.get(API + self.bt + '/state',
                                HTTP_IF_NONE_MATCH=f'"{version}"')
        self.assertEqual(cached.status_code, 304)

        self.move(self.white, self.wt, 'Ae2e4')
        fresh = self.black.get(API + self.bt + '/state',
                               {'since': version}).json()
        self.assertEqual(fresh['moves'], ['Ae2e4'])
        self.assertGreater(fresh['version'], version)

    def test_resigning_ends_the_game(self):
        self.start()
        body = self.black.post(API + self.bt + '/resign').json()
        self.assertEqual((body['state'], body['result'], body['reason']),
                         ('finished', '1-0', 'resignation'))
        self.assertEqual(self.move(self.white, self.wt, 'Ae2e4').status_code,
                         409)

    def test_a_draw_offer_can_be_accepted_or_declined(self):
        self.start()
        offered = self.white.post(API + self.wt + '/draw',
                                  {'action': 'offer'}).json()
        self.assertEqual(offered['draw_offer'], 'w')
        own = self.white.post(API + self.wt + '/draw', {'action': 'accept'})
        self.assertEqual(own.status_code, 409)
        declined = self.black.post(API + self.bt + '/draw',
                                   {'action': 'decline'}).json()
        self.assertEqual(declined['draw_offer'], '')

        self.white.post(API + self.wt + '/draw', {'action': 'offer'})
        accepted = self.black.post(API + self.bt + '/draw',
                                   {'action': 'accept'}).json()
        self.assertEqual((accepted['state'], accepted['result'],
                          accepted['reason']),
                         ('finished', '1/2-1/2', 'agreement'))

    def test_a_move_answers_a_draw_offer_with_no(self):
        self.start()
        self.white.post(API + self.wt + '/draw', {'action': 'offer'})
        # The offer stands through White's own move...
        body = self.move(self.white, self.wt, 'Ae2e4').json()
        self.assertEqual(body['draw_offer'], 'w')
        # ...and Black playing on turns it down.
        body = self.move(self.black, self.bt, 'Ae7e5').json()
        self.assertEqual(body['draw_offer'], '')
        self.assertEqual(body['state'], 'live')

    def test_offering_back_a_standing_offer_agrees_to_it(self):
        self.start()
        self.white.post(API + self.wt + '/draw', {'action': 'offer'})
        body = self.black.post(API + self.bt + '/draw',
                               {'action': 'offer'}).json()
        self.assertEqual((body['result'], body['reason']),
                         ('1/2-1/2', 'agreement'))

    def test_threefold_repetition_is_a_draw(self):
        self.start()
        shuffle = [(self.white, self.wt, 'Ab1c3'), (self.black, self.bt, 'Ag8f6'),
                   (self.white, self.wt, 'Bc3b1'), (self.black, self.bt, 'Bf6g8')]
        for client, token, text in shuffle * 2:
            body = self.move(client, token, text).json()
        self.assertEqual((body['state'], body['result'], body['reason']),
                         ('finished', '1/2-1/2', 'repetition'))

    def test_blowing_up_the_king_ends_the_game(self):
        self.start()
        game = AliceGame.objects.get(white_token=self.wt)
        game.afen = '8/5p2/8/7Q/8/8/8/4K3|4k3/8/8/8/8/8/8/8 w - - 0 1'
        game.save()
        body = self.move(self.white, self.wt, 'Ah5f7').json()
        self.assertEqual((body['state'], body['result'], body['reason']),
                         ('finished', '1-0', 'explosion'))

    def test_a_game_nobody_moves_in_for_a_day_is_abandoned(self):
        self.start()
        AliceGame.objects.filter(white_token=self.wt).update(
            updated=timezone.now() - timedelta(hours=25))
        body = self.white.get(API + self.wt + '/state').json()
        self.assertEqual(body['state'], 'abandoned')
        self.assertEqual(self.move(self.white, self.wt, 'Ae2e4').status_code,
                         409)

    def test_the_endpoints_want_post(self):
        self.assertEqual(self.white.get(API + self.wt + '/move').status_code,
                         405)
        self.assertEqual(self.white.get(API + 'create').status_code, 405)

    def test_an_unknown_token_is_a_404(self):
        self.assertEqual(self.white.get(API + 'x' * 24 + '/state').status_code,
                         404)
        self.assertEqual(self.white.post(API + 'x' * 24 + '/move',
                                         {'move': 'Ae2e4'}).status_code, 404)


class AccountsAndLimitsTests(TestCase):

    def test_signed_in_players_are_shown_by_name(self):
        worker_account('alice', 'p')
        worker_account('bob', 'p')
        white, black = Client(), Client()
        white.login(username='alice', password='p')
        black.login(username='bob', password='p')
        body = white.post(API + 'create', {'color': 'black'}).json()
        self.assertEqual(body['color'], 'black')
        state = white.post(API + token_of(body['your_url']) + '/join').json()
        self.assertEqual(state['black']['name'], 'alice')
        state = black.post(API + token_of(body['invite_url']) + '/join').json()
        self.assertEqual(state['white']['name'], 'bob')
        self.assertEqual(state['you'], 'w')

    def test_a_bad_colour_is_refused(self):
        response = self.client.post(API + 'create', {'color': 'green'})
        self.assertEqual(response.status_code, 400)

    def test_new_games_from_one_address_are_capped(self):
        for _ in range(fun_views.CREATE_LIMIT_PER_HOUR):
            self.assertEqual(self.client.post(
                API + 'create', REMOTE_ADDR='10.1.2.3').status_code, 201)
        refused = self.client.post(API + 'create', REMOTE_ADDR='10.1.2.3')
        self.assertEqual(refused.status_code, 429)
        other = self.client.post(API + 'create', REMOTE_ADDR='10.9.9.9')
        self.assertEqual(other.status_code, 201)
