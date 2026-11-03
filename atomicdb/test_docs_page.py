"""La pagina que explica que significa cada numero del explorador.

Lo que se asserta aqui es lo que la hace UTIL a quien la pidio: que este,
que la puedan leer los que no tienen cuenta (que son la mayoria de los que
llegan preguntando), que sus secciones sigan ahi, y que los dos limites que
cita salgan de las constantes que los aplican de verdad.  Una documentacion
con un numero copiado a mano miente en cuanto alguien cambia el numero, y
encima con cara de verdad.

El sitio se sirve MINIFICADO (§ test_honesty), asi que todo lo que se busca
aqui cabe en una linea del HTML de origen.
"""

from django.test import Client

from . import views
from .testing import TestCase, worker_account


class DocsPageTests(TestCase):

    SECTIONS = ('What a position row shows',
                'How a backed value is built',
                'Repetition',
                'The queue',
                'Contributing and attribution',
                'API',
                'Reading a page')

    def setUp(self):
        self.client = Client()

    def test_the_page_is_public(self):
        response = self.client.get('/atomicdb/docs/')

        self.assertEqual(response.status_code, 200)

    def test_it_renders_for_a_visitor_without_an_account(self):
        """El caso de quien pregunta: llega sin sesion y desde fuera.

        La cabecera de identidad se pinta en las dos formas y la pagina entra
        en una cache que VARIA POR COOKIE (§ urls), asi que un render anonimo
        roto no lo veria nadie con la sesion iniciada.
        """
        response = self.client.get('/atomicdb/docs/')

        self.assertContains(response, 'Sign in')
        self.assertNotContains(response, 'Sign out')

    def test_it_renders_for_somebody_signed_in(self):
        worker_account('lesha', 'p')
        self.client.login(username='lesha', password='p')

        response = self.client.get('/atomicdb/docs/')

        self.assertContains(response, 'Sign out')

    PAGES = ('rows', 'backing', 'repetition', 'queue', 'contributing', 'api',
             'reading')

    def test_every_section_has_its_own_page(self):
        for slug, heading in zip(self.PAGES, self.SECTIONS):
            with self.subTest(slug=slug):
                response = self.client.get(f'/atomicdb/docs/{slug}/')
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, heading)
                self.assertContains(response, f'id="{slug}"')

    def test_the_index_lists_every_page(self):
        response = self.client.get('/atomicdb/docs/')

        for slug in self.PAGES + ('method',):
            self.assertContains(response, f'href="/atomicdb/docs/{slug}/"')

    def test_old_section_anchors_go_on_to_their_page(self):
        """A fragment never reaches the server, so the index maps it itself:
        /atomicdb/docs/#queue (the queue page's help icon, old posts) must
        still land on the queue section."""
        body = self.client.get('/atomicdb/docs/').content.decode()

        self.assertIn('window.location.replace("/atomicdb/docs/" + hash',
                      body)
        for slug in self.PAGES:
            self.assertIn(f'"{slug}"', body)

    def test_an_unknown_page_is_a_404(self):
        response = self.client.get('/atomicdb/docs/nothing-here/')

        self.assertEqual(response.status_code, 404)

    def test_the_method_page_moved_under_the_docs(self):
        response = self.client.get('/atomicdb/method/')

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response['Location'], '/atomicdb/docs/method/')
        page = self.client.get('/atomicdb/docs/method/')
        self.assertContains(page, 'What "solved" means here')
        self.assertContains(page, 'id="api"')

    def test_the_pages_link_to_each_other_not_to_old_anchors(self):
        for slug in self.PAGES + ('method',):
            with self.subTest(slug=slug):
                body = self.client.get(f'/atomicdb/docs/{slug}/').content.decode()
                for old in self.PAGES:
                    self.assertNotIn(f'href="#{old}"', body)
                self.assertNotIn('href="/atomicdb/method/"', body)

    def test_the_limits_come_from_the_constants_that_apply_them(self):
        response = self.client.get('/atomicdb/docs/queue/')

        self.assertContains(response,
                            f'<strong>{views.REQUEST_QUEUE_MAX:,}</strong>')
        response = self.client.get('/atomicdb/docs/api/')
        self.assertContains(response, 'no hourly limit on this door')

    def test_the_navigation_takes_you_there(self):
        """Una pagina publica a la que no lleva ningun enlace no esta publicada."""
        response = self.client.get('/atomicdb/')

        self.assertContains(response, 'href="/atomicdb/docs/">Docs</a>')
