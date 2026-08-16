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

    def test_every_section_is_there(self):
        response = self.client.get('/atomicdb/docs/')

        for heading in self.SECTIONS:
            self.assertContains(response, heading)

    def test_the_sections_are_linkable(self):
        """Los anclajes son la mitad de para que sirve: se citan sueltos."""
        response = self.client.get('/atomicdb/docs/')

        for anchor in ('id="rows"', 'id="backing"', 'id="repetition"',
                       'id="queue"', 'id="contributing"', 'id="api"',
                       'id="reading"'):
            self.assertContains(response, anchor)

    def test_the_limits_come_from_the_constants_that_apply_them(self):
        response = self.client.get('/atomicdb/docs/')

        self.assertContains(response,
                            f'<strong>{views.REQUEST_QUEUE_MAX:,}</strong>')
        self.assertContains(
            response, f'<strong>{views.API_REQUESTS_PER_HOUR}</strong>')

    def test_the_navigation_takes_you_there(self):
        """Una pagina publica a la que no lleva ningun enlace no esta publicada."""
        response = self.client.get('/atomicdb/')

        self.assertContains(response, 'href="/atomicdb/docs/">Docs</a>')
