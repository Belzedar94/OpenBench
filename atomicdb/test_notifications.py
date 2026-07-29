"""Identidad visible y avisos de "tu analisis ya esta servido".

Tres familias, y las tres defienden la misma frase del producto — el sitio es
publico, casi nadie inicia sesion, y las funciones que ya dependen de la
cuenta (la afinidad de workers, el voto de campanas) no se van a usar solas:

* QUIEN GENERA UN AVISO.  Solo lo que pidio una persona con cuenta, una vez
  por tarea, y contando las hijas de una expansion — que son el resultado que
  esa persona compro aunque no las nombrara.  Las semillas de cobertura y la
  exploracion autonoma no son de nadie y no avisan.
* LA CABECERA.  Que aparece deslogueado, que aparece logueado, que dice la
  insignia, y a donde vuelve el login.
* LA CACHE.  Es lo unico de aqui que puede hacer dano de verdad: dos de las
  paginas iban con ``cache_page`` plano, y una cabecera con nombre dentro de
  una entrada compartida es servirle a un visitante la sesion de otro.
"""

import html as html_module
import re

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from . import ingest, ingest_queue, logic, notifications
from .models import (AnalysisTask, Edge, Position, RequestNotification)
from .testing import TestCase


CACHE_FOR_TESTS = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'atomicdb-notification-tests',
    },
}


def _reading(body):
    """Lo que un visitante LEE: sin etiquetas y con los espacios colapsados.

    La respuesta pasa por el minificador de HTML, asi que una asercion sobre
    los saltos de linea de la plantilla comprobaria el minificador.
    """
    return ' '.join(html_module.unescape(re.sub(r'<[^>]+>', ' ', body)).split())


def _line(*ucis):
    """Materializa una linea desde la raiz y devuelve su ultima posicion."""
    parent = ingest.get_or_create_position(logic.start_fen())
    for uci in ucis:
        child = ingest.get_or_create_position(
            logic.apply_move(parent.fen, uci))
        Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                   defaults={'child': child})
        parent = child
    return parent


def _task(position, requested_by='', source=AnalysisTask.Source.USER,
          budget=None, generation=0):
    return AnalysisTask.objects.create(
        position=position, generation=generation,
        budget_nodes=(ingest.REQUEST_BUDGET_LADDER[0] if budget is None
                      else budget),
        source=source, requested_by=requested_by)


def _lines_for(position, value=-40):
    """Un MultiPV plausible sobre las jugadas legales de la posicion."""
    return [{'move': uci, 'eval_cp': value, 'pv': [uci]}
            for uci in logic.legal_moves(position.fen)[:3]]


def _serve(task, position=None):
    """El resultado de la tarea entra en el arbol, por el camino de siempre.

    Reclamar y encolar es lo que hace el submit; aplicar es lo que hace el
    procesador de la cola.  Aqui van seguidos porque lo que se comprueba esta
    al final del segundo.
    """
    position = position or task.position
    job, _created = ingest_queue.enqueue(task, {
        'lines': _lines_for(position), 'nodes': 1_000, 'elapsed': 1.0,
        'machine': 'm1', 'username': 'w'})
    AnalysisTask.objects.filter(pk=task.pk).update(
        state=AnalysisTask.TState.COMPLETED, completed=timezone.now())
    return ingest_queue.process_job(job)


class NotificationCreationTests(TestCase):
    """Que tarea servida deja aviso, y a quien."""

    def setUp(self):
        User.objects.create_user(username='lesha', password='pw')
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.target = _line('e2e4')

    def test_a_served_request_notifies_the_account_that_asked(self):
        _serve(_task(self.target, requested_by='lesha'))

        row = RequestNotification.objects.get()
        self.assertEqual(row.username, 'lesha')
        self.assertEqual(row.position_id, self.target.key)
        self.assertFalse(row.seen)

    def test_an_anonymous_request_notifies_nobody(self):
        _serve(_task(self.target, requested_by=''))

        self.assertEqual(RequestNotification.objects.count(), 0)

    def test_a_name_without_an_account_notifies_nobody(self):
        # ``requested_by`` es texto que viajo desde una sesion: un nombre que
        # ya no existe (cuenta borrada) no puede inventar un destinatario.
        _serve(_task(self.target, requested_by='fantasma'))

        self.assertEqual(RequestNotification.objects.count(), 0)

    def test_one_task_notifies_exactly_once(self):
        task = _task(self.target, requested_by='lesha')
        job, _created = ingest_queue.enqueue(task, {
            'lines': _lines_for(self.target), 'nodes': 1_000, 'elapsed': 1.0,
            'machine': 'm1', 'username': 'w'})
        AnalysisTask.objects.filter(pk=task.pk).update(
            state=AnalysisTask.TState.COMPLETED, completed=timezone.now())

        ingest_queue.process_job(job)
        ingest_queue.process_job(job)     # reproceso: el trabajo ya esta DONE

        self.assertEqual(RequestNotification.objects.count(), 1)

    def test_a_small_coverage_fill_does_not_notify(self):
        # La semilla de cobertura son 8M sobre jugadas concretas y las encola
        # el sistema de cientos en cientos.  Aunque arrastrase un nombre, no
        # es una peticion: nadie la pidio y llenaria la campana de ruido.
        _serve(_task(self.target, requested_by='lesha',
                     source=AnalysisTask.Source.FILL,
                     budget=ingest.COVERAGE_SEED_NODES))

        self.assertEqual(RequestNotification.objects.count(), 0)

    def test_the_criterion_is_the_band_or_the_request_floor(self):
        deserved = ingest.notification_deserved
        self.assertTrue(deserved(AnalysisTask.Source.USER,
                                 ingest.COVERAGE_SEED_NODES))
        self.assertTrue(deserved(AnalysisTask.Source.AUTO,
                                 ingest.REQUEST_BUDGET_LADDER[0]))
        self.assertFalse(deserved(AnalysisTask.Source.FILL,
                                  ingest.COVERAGE_SEED_NODES))
        self.assertFalse(deserved(AnalysisTask.Source.AUTO, None))

    @override_settings(ATOMICDB_BREADTH_SWAP=True)
    def test_an_expansion_notifies_through_each_child_it_bought(self):
        """La peticion que compra ANCHURA avisa igual, hija a hija.

        Nadie escribio nada especial para esto: las tareas de las hijas nacen
        con el mismo ``requested_by`` y por la misma banda, asi que el
        mecanismo por-tarea las cubre — y cada aviso apunta a la posicion
        concreta que la persona podia querer mirar, no a la que pincho.
        """
        parent = _line('e2e4')
        Position.objects.filter(key=parent.key).update(eval_cp=25)
        AnalysisTask.objects.create(
            position=parent, generation=0,
            budget_nodes=ingest.REQUEST_BUDGET_LADDER[0],
            state=AnalysisTask.TState.COMPLETED, completed=timezone.now())

        outcome = ingest.request_analysis(
            Position.objects.get(key=parent.key), requested_by='lesha')

        self.assertEqual(str(outcome), 'expanded')
        children = AnalysisTask.objects.filter(
            state=AnalysisTask.TState.PENDING).exclude(position=parent)
        self.assertTrue(children.exists())
        self.assertEqual({task.requested_by for task in children}, {'lesha'})

        child_task = children.order_by('position_id').first()
        _serve(child_task, position=child_task.position)

        row = RequestNotification.objects.get()
        self.assertEqual(row.username, 'lesha')
        self.assertEqual(row.position_id, child_task.position_id)
        self.assertNotEqual(row.position_id, parent.key)


class NotificationPageTests(TestCase):
    """La lista completa, y el POST que marca vistos."""

    def setUp(self):
        cache.clear()
        User.objects.create_user(username='lesha', password='pw')
        self.client = Client()
        self.client.login(username='lesha', password='pw')
        self.position = _line('e2e4', 'e7e5')
        self.row = RequestNotification.objects.create(
            username='lesha', position=self.position,
            task=_task(self.position, requested_by='lesha'))

    def test_the_page_lists_the_line_and_links_to_the_position(self):
        response = self.client.get('/atomicdb/notifications/')

        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'/atomicdb/explore/{self.position.key}/', body)
        self.assertIn('Your analysis of 1. e4 e5 is ready', _reading(body))

    def test_reading_the_list_marks_nothing(self):
        """LEIDA = VISITADA: mirar la lista no apaga nada.

        Lo que apaga un aviso es llegar a su posicion; quien repasa su lista
        sin visitar conserva las negritas para la proxima vez."""
        self.assertEqual(notifications.unseen_count('lesha'), 1)

        self.client.get('/atomicdb/notifications/')
        body = self.client.get('/atomicdb/notifications/').content.decode()

        self.assertEqual(notifications.unseen_count('lesha'), 1)
        self.assertIn('notif-row unseen', body)

    def test_visiting_the_position_marks_its_notice_and_only_its(self):
        other = _line('d2d4')
        RequestNotification.objects.create(
            username='lesha', position=other,
            task=_task(other, requested_by='lesha'))
        self.assertEqual(notifications.unseen_count('lesha'), 2)

        self.client.get(f'/atomicdb/explore/{self.position.key}/')

        self.assertEqual(notifications.unseen_count('lesha'), 1)
        row = RequestNotification.objects.get(position=self.position)
        self.assertTrue(row.seen)

    def test_unread_rows_sort_before_read_ones(self):
        visited = _line('d2d4')
        RequestNotification.objects.create(
            username='lesha', position=visited, seen=True,
            task=_task(visited, requested_by='lesha'))

        rows = notifications.presented('lesha')

        self.assertEqual([row['seen'] for row in rows], [False, True])

    def test_mark_all_read_returns_to_where_the_click_came_from(self):
        response = self.client.post('/atomicdb/notifications/',
                                    {'back': '/atomicdb/notifications/'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/atomicdb/notifications/')
        self.assertEqual(notifications.unseen_count('lesha'), 0)

    def test_mark_all_never_redirects_off_atomicdb(self):
        response = self.client.post('/atomicdb/notifications/',
                                    {'back': 'https://evil.example/'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/atomicdb/notifications/')

    def test_the_same_post_answers_json_when_it_comes_from_the_dropdown(self):
        response = self.client.post('/atomicdb/notifications/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'ok': True, 'seen': 1})

    def test_an_anonymous_visitor_is_sent_to_sign_in_and_back(self):
        response = Client().get('/atomicdb/notifications/')

        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/?next=', response['Location'])
        self.assertIn('notifications', response['Location'])

    def test_nobody_reads_anybody_elses_list(self):
        User.objects.create_user(username='otra', password='pw')
        theirs = _line('d2d4')
        RequestNotification.objects.create(username='otra', position=theirs)
        other = Client()
        other.login(username='otra', password='pw')

        body = other.get('/atomicdb/notifications/').content.decode()

        self.assertIn(theirs.key, body)
        self.assertNotIn(self.position.key, body)
        # Y leerla no le apaga los avisos a nadie mas.
        self.assertEqual(notifications.unseen_count('lesha'), 1)

    def test_the_unseen_count_is_one_query(self):
        with self.assertNumQueries(
                1, using=settings.ATOMICDB_DATABASE_ALIAS):
            notifications.unseen_count('lesha')


class HeaderTests(TestCase):
    """La zona de identidad, en todas las paginas de AtomicDB."""

    def setUp(self):
        cache.clear()
        User.objects.create_user(username='lesha', password='pw')
        self.position = _line('e2e4')

    def test_a_visitor_without_a_session_is_offered_one(self):
        body = Client().get(f'/atomicdb/explore/{self.position.key}/'
                            ).content.decode()

        self.assertIn('Sign in', _reading(body))
        self.assertIn('Register', _reading(body))
        self.assertNotIn('bell-panel', body)

    def test_a_visitor_with_a_session_sees_their_name_and_the_bell(self):
        client = Client()
        client.login(username='lesha', password='pw')

        body = client.get(f'/atomicdb/explore/{self.position.key}/'
                          ).content.decode()

        reading = _reading(body)
        self.assertIn('lesha', reading)
        self.assertIn('Sign out', reading)
        self.assertIn('id="bell"', body)
        self.assertNotIn('Sign in', reading)

    def test_the_badge_shows_what_is_unseen_and_nothing_else(self):
        RequestNotification.objects.create(
            username='lesha', position=self.position, seen=True)
        # Los pendientes viven en OTRA posicion: visitar la pagina de una
        # posicion apaga los avisos de ESA posicion (leida = visitada), y el
        # test mide la insignia, no el apagado.
        elsewhere = _line('d2d4')
        for _ in range(2):
            RequestNotification.objects.create(
                username='lesha', position=elsewhere)
        client = Client()
        client.login(username='lesha', password='pw')

        body = client.get(f'/atomicdb/explore/{self.position.key}/'
                          ).content.decode()

        # Por regex y no por la cadena literal: el minificador reordena los
        # atributos (``aria-hidden`` se le pone delante de ``class``), asi que
        # una asercion sobre el HTML exacto comprobaria el minificador.
        self.assertTrue(re.search(r'<span[^>]*bell-badge[^>]*>2<', body))
        # Y quien no ve la insignia la oye: el numero tambien esta en el
        # nombre accesible del boton.
        self.assertIn('Analyses you asked for, 2 new', _reading(body))

    def test_without_unseen_notifications_there_is_no_badge(self):
        RequestNotification.objects.create(
            username='lesha', position=self.position, seen=True)
        client = Client()
        client.login(username='lesha', password='pw')

        body = client.get(f'/atomicdb/explore/{self.position.key}/'
                          ).content.decode()

        self.assertIn('id="bell"', body)
        self.assertNotIn('bell-badge', body)

    def test_the_age_is_one_unit_and_the_freshest_one_has_a_name(self):
        """Ni "3 hours, 12 minutes ago" ni "0 minutes ago".

        El segundo es el que mordio: ``timesince`` une numero y unidad con un
        espacio DURO, asi que reconocer el cero comparando contra un espacio
        normal no funciona nunca — y el aviso recien llegado, que es
        justamente el que la gente va a ver, salia con la forma rara.
        """
        now = timezone.now()
        self.assertEqual(notifications.ago(now), 'just now')
        self.assertEqual(
            notifications.ago(now - timezone.timedelta(minutes=3)),
            '3 minutes ago')
        self.assertEqual(
            notifications.ago(now - timezone.timedelta(hours=3, minutes=12)),
            '3 hours ago')

    def test_a_long_count_stops_being_a_number(self):
        self.assertEqual(notifications.badge(0), '')
        self.assertEqual(notifications.badge(7), '7')
        self.assertEqual(notifications.badge(notifications.BADGE_CAP + 1),
                         f'{notifications.BADGE_CAP}+')

    def test_the_header_is_on_every_atomicdb_page(self):
        client = Client()
        client.login(username='lesha', password='pw')
        pages = ('/atomicdb/', '/atomicdb/map/', '/atomicdb/method/',
                 f'/atomicdb/explore/{self.position.key}/',
                 '/atomicdb/notifications/')

        for url in pages:
            with self.subTest(url=url):
                body = client.get(url).content.decode()
                self.assertIn('id="bell"', body)
                self.assertIn('lesha', _reading(body))

    def test_the_sign_in_link_comes_back_to_the_page_it_started_from(self):
        body = Client().get('/atomicdb/map/').content.decode()

        self.assertIn('/login/?next=%2Fatomicdb%2Fmap%2F', body)
        self.assertIn('/register/?next=%2Fatomicdb%2Fmap%2F', body)

    def test_the_header_stays_out_of_the_openbench_pages(self):
        # El context processor es global; su primera linea es no hacer nada
        # fuera de /atomicdb/.
        self.assertEqual(notifications.identity(
            _FakeRequest('/index/', user=None)), {})

    def test_an_anonymous_page_never_reads_the_notifications_table(self):
        from django.db import connections
        from django.test.utils import CaptureQueriesContext

        connection = connections[settings.ATOMICDB_DATABASE_ALIAS]
        with CaptureQueriesContext(connection) as captured:
            Client().get(f'/atomicdb/explore/{self.position.key}/')

        self.assertFalse([query for query in captured.captured_queries
                          if 'requestnotification' in query['sql'].lower()])


class _FakeRequest:
    """Lo minimo que mira el context processor."""

    def __init__(self, path, user=None):
        self.path = path
        self.user = user
        self.META = {}


@override_settings(CACHES=CACHE_FOR_TESTS)
class CachedHeaderTests(TestCase):
    """La cabecera con nombre dentro de una pagina cacheada.

    ``map`` y ``method`` iban con ``cache_page`` PLANO — una entrada para todo
    el mundo — porque no tenian nada por visitante.  La cabecera de identidad
    cambia eso, y lo unico que lo hace seguro es que las dos varien por
    cookie.  Estas pruebas son ese contrato.
    """

    def setUp(self):
        cache.clear()
        User.objects.create_user(username='lesha', password='pw')
        User.objects.create_user(username='otra', password='pw')
        self.position = _line('e2e4')
        RequestNotification.objects.create(
            username='lesha', position=self.position)

    def _client(self, username=None):
        client = Client()
        if username:
            client.login(username=username, password='pw')
        return client

    def test_a_cached_page_never_serves_one_session_to_another(self):
        mine = self._client('lesha')
        theirs = self._client('otra')

        for url in ('/atomicdb/map/', '/atomicdb/method/', '/atomicdb/'):
            with self.subTest(url=url):
                mine.get(url)                     # calienta la entrada
                mine_body = mine.get(url).content.decode()
                theirs_body = theirs.get(url).content.decode()

                self.assertIn('lesha', _reading(mine_body))
                self.assertIn('otra', _reading(theirs_body))
                self.assertNotIn('lesha', _reading(theirs_body))
                self.assertNotIn('bell-badge', theirs_body)

    def test_an_anonymous_visitor_is_never_served_a_signed_in_header(self):
        mine = self._client('lesha')

        for url in ('/atomicdb/map/', '/atomicdb/method/'):
            with self.subTest(url=url):
                mine.get(url)
                mine.get(url)
                body = Client().get(url).content.decode()

                self.assertIn('Sign in', _reading(body))
                self.assertNotIn('lesha', _reading(body))

    def test_the_cached_pages_still_declare_their_cache(self):
        # Variar por cookie no puede haber apagado la cache: sigue absorbiendo
        # la tormenta de F5 de un mismo visitante.
        client = self._client()
        for url, seconds in (('/atomicdb/map/', 30),
                             ('/atomicdb/method/', 30)):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertIn(f'max-age={seconds}',
                              response.headers.get('Cache-Control', ''))
                self.assertIn('Cookie', response.headers.get('Vary', ''))


class LoginReturnTests(TestCase):
    """A donde vuelve un login que empezo en AtomicDB.

    Vive aqui y no en las pruebas de OpenBench porque es la otra mitad de la
    cabecera: sin esto, el enlace "Sign in" de una posicion concreta acaba en
    la tabla de tests de otro proyecto, que es perder al visitante.
    """

    def setUp(self):
        User.objects.create_user(username='lesha', password='pw')
        self.client = Client()

    def test_a_login_comes_back_to_the_atomicdb_page(self):
        response = self.client.post('/login/', {
            'username': 'lesha', 'password': 'pw',
            'next': '/atomicdb/map/'})

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/atomicdb/map/')

    def test_a_destination_on_another_host_is_refused(self):
        for hostile in ('https://evil.example/steal',
                        '//evil.example/steal',
                        'http://127.0.0.1:1/'):
            with self.subTest(next=hostile):
                response = Client().post('/login/', {
                    'username': 'lesha', 'password': 'pw', 'next': hostile})

                self.assertEqual(response['Location'], '/index/')

    def test_a_failed_attempt_keeps_the_destination(self):
        response = self.client.post('/login/', {
            'username': 'lesha', 'password': 'wrong',
            'next': '/atomicdb/map/'})

        self.assertEqual(response['Location'],
                         '/login/?next=%2Fatomicdb%2Fmap%2F')

    def test_the_form_carries_the_destination(self):
        body = self.client.get('/login/?next=/atomicdb/map/').content.decode()

        self.assertIn('name="next"', body)
        self.assertIn('/atomicdb/map/', body)
