"""What a search engine reads (cz_eclipsia, 3-Nov-2026: "is atomicdb SEO
optimized?")."""

import re

from django.test import Client

from . import ingest, logic, openings
from .testing import TestCase


class SitemapTests(TestCase):

    def test_the_sitemap_lists_the_pages_and_the_named_positions(self):
        charon = next(o for o in openings.load_catalog().values()
                      if o.name == 'Charon Gambit')
        ingest.get_or_create_position(charon.fen)

        response = Client().get('/atomicdb/sitemap.xml')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['Content-Type'].startswith('application/xml'))
        body = response.content.decode()
        self.assertIn('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
                      body)
        self.assertIn('<loc>http://testserver/atomicdb/</loc>', body)
        self.assertIn('<loc>http://testserver/atomicdb/docs/api/</loc>', body)
        self.assertIn(f'/atomicdb/explore/{charon.position_key}/</loc>', body)

    def test_a_named_position_the_tree_lacks_is_left_out(self):
        charon = next(o for o in openings.load_catalog().values()
                      if o.name == 'Charon Gambit')

        body = Client().get('/atomicdb/sitemap.xml').content.decode()

        self.assertNotIn(charon.position_key, body)


class ExplorerHeadTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())

    def test_a_play_route_points_at_the_one_canonical_url(self):
        body = Client().get(
            f'/atomicdb/explore/{self.root.key}/?play=').content.decode()

        link = re.search(r'<link[^>]*rel="canonical"[^>]*>', body).group(0)
        self.assertIn(
            f'href="http://testserver/atomicdb/explore/{self.root.key}/"', link)

    def test_every_page_has_a_description(self):
        for path in ('/atomicdb/', f'/atomicdb/explore/{self.root.key}/'):
            body = Client().get(path).content.decode()
            self.assertRegex(body, r'<meta[^>]*name="description"')
            self.assertIn('Atomic Chess', body)

    def test_a_named_position_is_named_in_its_title(self):
        charon = next(o for o in openings.load_catalog().values()
                      if o.name == 'Charon Gambit')
        pos = ingest.get_or_create_position(charon.fen)

        body = Client().get(f'/atomicdb/explore/{pos.key}/').content.decode()

        self.assertIn('<title>Charon Gambit · AtomicDB explorer</title>', body)
