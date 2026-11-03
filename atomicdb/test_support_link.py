"""The fundraiser is one click away from every page (MoKy, 3-Nov-2026)."""

from .testing import TestCase

FUNDRAISER = 'https://www.gofundme.com/f/keep-atomicdb-online-solving-atomic-chess'


class SupportLinkTests(TestCase):

    def test_every_page_links_the_fundraiser_in_the_nav(self):
        for url in ('/atomicdb/', '/atomicdb/docs/', '/atomicdb/queue/'):
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                nav = body[body.index('<nav'):body.index('</nav>')]
                self.assertIn(FUNDRAISER, nav)
                self.assertIn('nav-support', nav)

    def test_the_home_page_says_what_it_is_for(self):
        body = self.client.get('/atomicdb/').content.decode()
        start = body.index('support-line')
        self.assertIn(FUNDRAISER, body[start:start + 400])
