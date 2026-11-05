"""The settings cog in the header and the move notation it switches."""

import re
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client

from . import ingest, logic, notifications
from .models import ContributorPref, Edge
from .testing import TestCase


def _line(*ucis):
    parent = ingest.get_or_create_position(logic.start_fen())
    for uci in ucis:
        child = ingest.get_or_create_position(
            logic.apply_move(parent.fen, uci))
        Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                   defaults={'child': child})
        parent = child
    return parent


def _panel(body):
    start = body.index('id="settings-panel"')
    return body[start:body.index('</header>', start)]


class SettingsCogTests(TestCase):

    def setUp(self):
        User.objects.create_user(username='lesha', password='pw')
        self.position = _line('e2e4')
        self.url = f'/atomicdb/explore/{self.position.key}/'

    def _signed_in(self):
        client = Client()
        client.login(username='lesha', password='pw')
        return client

    def test_a_visitor_gets_the_theme_and_the_notation_only(self):
        body = Client().get(self.url).content.decode()

        self.assertIn('id="settings-cog"', body)
        panel = _panel(body)
        self.assertIn('name="settings-theme"', panel)
        self.assertIn('name="settings-notation"', panel)
        self.assertNotIn('/atomicdb/queue/lifo/', panel)
        self.assertNotIn('/atomicdb/notifications/settings/', panel)

    def test_a_signed_in_account_also_gets_the_queue_and_the_notices(self):
        body = self._signed_in().get(self.url).content.decode()

        panel = _panel(body)
        self.assertIn('name="settings-notation"', panel)
        self.assertIn('action="/atomicdb/queue/lifo/"', panel)
        self.assertIn('href="/atomicdb/notifications/settings/"', panel)
        self.assertIn('Newest first in my queue', panel)
        self.assertRegex(panel, r'name="back"[^>]*value="' + re.escape(
            self.url) + '"|value="' + re.escape(self.url) + '"[^>]*name="back"')
        box = re.search(r'<input[^>]*name="lifo"[^>]*type="checkbox"[^>]*>',
                        panel).group(0)
        self.assertNotIn('checked', box)

    def test_the_queue_box_shows_the_stored_option(self):
        ContributorPref.objects.create(account='lesha', lifo_queue=True)

        panel = _panel(self._signed_in().get(self.url).content.decode())

        box = re.search(r'<input[^>]*name="lifo"[^>]*type="checkbox"[^>]*>',
                        panel).group(0)
        self.assertIn('checked', box)

    def test_the_cog_sits_beside_the_theme_switch(self):
        body = Client().get(self.url).content.decode()

        self.assertLess(body.index('id="settings-cog"'),
                        body.index('id="theme-toggle"'))
        self.assertIn('id="theme-toggle"', body)

    def test_the_script_reaches_browsers_with_the_cache_buster(self):
        body = Client().get(self.url).content.decode()

        self.assertIn(
            f'atomicdb/settings.js?v={notifications.STATIC_VERSION}', body)

    def test_the_cache_buster_covers_the_scripts_too(self):
        # A change that only touches a script must move the URL as well.
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'atomicdb.css').write_text('a {}')
            Path(folder, 'settings.js').write_text('1')
            before = notifications._static_version(folder)
            Path(folder, 'settings.js').write_text('2')

            self.assertNotEqual(before, notifications._static_version(folder))


class QueueOptionFromThePanelTests(TestCase):

    def setUp(self):
        User.objects.create_user(username='lesha', password='pw')
        self.client = Client()
        self.client.login(username='lesha', password='pw')

    def test_the_panel_returns_to_the_page_it_was_on(self):
        response = self.client.post('/atomicdb/queue/lifo/', {
            'lifo': ['0', '1'], 'back': '/atomicdb/map/?x=1'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/atomicdb/map/?x=1')
        self.assertTrue(ContributorPref.objects.get(
            account='lesha').lifo_queue)

    def test_the_profile_form_still_returns_to_the_profile(self):
        response = self.client.post('/atomicdb/queue/lifo/', {'lifo': '0'})

        self.assertEqual(response['Location'], '/atomicdb/user/lesha/')
        self.assertFalse(ContributorPref.objects.get(
            account='lesha').lifo_queue)

    def test_a_destination_off_atomicdb_is_refused(self):
        for hostile in ('https://evil.example/', '//evil.example/', '/index/'):
            with self.subTest(back=hostile):
                response = self.client.post('/atomicdb/queue/lifo/', {
                    'lifo': '1', 'back': hostile})

                self.assertEqual(response['Location'], '/atomicdb/user/lesha/')


class MoveNotationMarkupTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)

    def test_the_moves_of_the_table_carry_their_uci(self):
        body = Client().get(
            f'/atomicdb/explore/{self.root.key}/').content.decode()

        self.assertRegex(body, r'<strong[^>]*data-uci="g1f3"[^>]*>Nf3</strong>')

    def test_the_line_above_the_board_carries_its_uci(self):
        position = _line('e2e4', 'e7e5')

        body = Client().get(
            f'/atomicdb/explore/{position.key}/').content.decode()

        self.assertRegex(body, r'<span data-uci="e7e5">e5</span>')

    def test_the_choice_is_read_before_the_page_paints(self):
        body = Client().get(
            f'/atomicdb/explore/{self.root.key}/').content.decode()

        self.assertLess(body.index('localStorage.getItem("atomicdb-notation")'),
                        body.index('chessground.base.css'))
        self.assertLess(body.index('</footer>'),
                        body.index('window.atomicdbNotation = function'))

    def test_the_stylesheet_hides_the_moves_until_they_are_swapped(self):
        style = (Path(settings.BASE_DIR) / 'atomicdb' / 'static' / 'atomicdb'
                 / 'atomicdb.css').read_text(encoding='utf-8')

        self.assertIn(
            'html[data-notation="uci"]:not(.notation-ready) [data-uci]', style)


class NewestFirstOnlyInTheMenuTests(TestCase):
    """cz_eclipsia, 3-Nov-2026: the profile no longer has its own copy."""

    def test_the_own_profile_has_no_newest_first_box(self):
        from django.test import Client
        from .testing import worker_account
        worker_account('lesha', 'pw')
        client = Client()
        client.login(username='lesha', password='pw')
        body = client.get('/atomicdb/user/lesha/').content.decode()
        self.assertNotIn('lifo-toggle', body)
        start = body.index('settings-panel')
        self.assertIn('Newest first in my queue', body[start:])


class EvalViewSettingTests(TestCase):
    """cz_eclipsia, 4-Nov-2026: the cog switches evals between centipawns
    from the side to move and White's side in pawns."""

    def test_the_cog_offers_both_views(self):
        body = Client().get('/atomicdb/').content.decode()
        panel = body[body.index('id="settings-panel"'):]
        self.assertIn('Centipawns', panel)
        self.assertIn('Pawns', panel)
        self.assertIn('window.atomicdbEval = function', body)

    def test_evals_carry_their_number_and_side(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        from .models import Position
        child = Edge.objects.filter(parent=root).first().child
        Position.objects.filter(key=child.key).update(
            eval_cp=40, nodes_invested=128_000_000, visits=1)
        Position.objects.filter(key=root.key).update(
            eval_cp=30, nodes_invested=128_000_000, visits=1)
        body = Client().get(f'/atomicdb/explore/{root.key}/').content.decode()
        self.assertRegex(body, r'<span[^>]*class="ev"[^>]*data-cp="40"')
        self.assertRegex(body, r'class="ev"[^>]*data-stm="w"|data-stm="w"[^>]*class="ev"')
