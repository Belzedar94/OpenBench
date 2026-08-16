"""La superficie PUBLICA de AtomicDB para programas: leer y pedir.

Dos peticiones de comunidad, las dos de Wolfram y las dos sobre lo mismo — que
lo que el sitio sabe se pueda usar sin abrir el navegador:

* PEDIR (``POST /atomicdb/api/request``): "is there official API for requesting
  analysis".  Lo que se defiende aqui es que la puerta nueva no sea una segunda
  politica: el mismo dedup, la misma escalera, el mismo techo de cola y el
  mismo registro de peticionarios que el click, porque el trabajo lo hace la
  misma funcion.  Y que la exencion de CSRF que un script necesita no le abra
  la cola a una pagina de terceros.
* LEER EL ANALISIS (``GET /atomicdb/api/query``): "can you add to query result
  the engine output in position ... number of nodes + all PVs from last pass +
  all PVs from earlier wider pass ... and preferably all passes that were done
  in the node".  Lo que se defiende es la perspectiva (el bloque nuevo habla en
  la misma que el resto del cuerpo), que sea ADITIVO, y que la serie de pases
  no afirme mas de lo que se guardo.
"""

from unittest import mock

from django.test import Client

from . import ingest, logic
from .models import AnalysisTask, ApiRequestLog, Position
from .testing import TestCase, worker_account


def _lines(fen, value=-40, count=3, raw=None):
    """Un MultiPV plausible sobre las jugadas legales, con linea cruda.

    Los valores EMPEORAN para el que mueve segun baja la lista, que es como
    los entrega un motor de verdad: la linea 1 es la mejor.  Como se guardan en
    perspectiva blanca, eso significa que bajan con blancas al turno y suben
    con negras — y sin ese detalle el fixture ordenaria la primera linea al
    reves y ``eval_cp`` acabaria saliendo de la ULTIMA.
    """
    stm_white = fen.split()[1] == 'w'
    rows = []
    for index, uci in enumerate(logic.legal_moves(fen)[:count]):
        eval_cp = value - index if stm_white else value + index
        row = {'move': uci, 'eval_cp': eval_cp, 'mate': None, 'pv': [uci]}
        if raw is not None:
            row['raw'] = raw.format(multipv=index + 1, cp=eval_cp)
        rows.append(row)
    return rows


RAW = ('info depth 34 seldepth 48 multipv {multipv} score cp {cp} '
       'nodes 128000000 nps 9480000 time 13500 pv e2e4')


class RequestApiTests(TestCase):
    """La puerta programatica de peticion, y que sea LA MISMA puerta."""

    def setUp(self):
        self.client = Client()
        worker_account('wolfram', 'p')
        worker_account('eclipsia', 'p')
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.target = ingest.get_or_create_position(
            logic.apply_move(self.root.fen, 'e2e4'))

    def _post(self, fen=None, ip='10.0.0.1', **fields):
        return self.client.post('/atomicdb/api/request',
                                {'fen': fen or self.target.fen, **fields},
                                REMOTE_ADDR=ip)

    def _credentials(self, username):
        return {'username': username, 'password': 'p'}

    def test_a_request_queues_the_position_and_says_which_task(self):
        payload = self._post(**self._credentials('wolfram')).json()

        task = AnalysisTask.objects.get(position=self.target)
        self.assertEqual(payload['status'], 'queued')
        self.assertEqual(payload['task'], task.id)
        self.assertEqual(payload['key'], self.target.key)
        self.assertEqual(payload['fen'], self.target.fen)
        self.assertEqual(payload['budget_nodes'], task.budget_nodes)
        self.assertFalse(payload['seeded'])
        self.assertEqual(task.requested_by, 'wolfram')
        self.assertEqual(task.source, AnalysisTask.Source.USER)

    def test_it_buys_the_same_ladder_rung_the_button_buys(self):
        self._post(**self._credentials('wolfram'))

        self.assertEqual(
            AnalysisTask.objects.get(position=self.target).budget_nodes,
            ingest.REQUEST_BUDGET_LADDER[0])

    def test_without_credentials_it_is_served_as_an_anonymous_click(self):
        payload = self._post().json()

        self.assertEqual(payload['status'], 'queued')
        self.assertEqual(
            AnalysisTask.objects.get(position=self.target).requested_by, '')

    def test_a_second_account_backs_the_request_instead_of_duplicating_it(self):
        self._post(**self._credentials('eclipsia'))

        payload = self._post(ip='10.0.0.2',
                             **self._credentials('wolfram')).json()

        self.assertEqual(payload['status'], 'already-queued')
        self.assertTrue(payload['backed'])
        self.assertEqual(
            AnalysisTask.objects.filter(position=self.target).count(), 1)
        task = AnalysisTask.objects.get(position=self.target)
        self.assertEqual(task.requested_by, 'eclipsia')
        self.assertEqual(task.also_requested_by, ['wolfram'])

    def test_the_author_of_a_live_request_is_not_a_backer_of_it(self):
        self._post(**self._credentials('wolfram'))

        payload = self._post(ip='10.0.0.2',
                             **self._credentials('wolfram')).json()

        self.assertEqual(payload['status'], 'already-queued')
        self.assertFalse(payload['backed'])

    def test_asking_twice_from_one_address_is_the_same_click_twice(self):
        self._post(**self._credentials('wolfram'))

        payload = self._post(**self._credentials('wolfram')).json()

        self.assertEqual(payload['status'], 'already-requested')
        self.assertIn('reason', payload)

    def test_a_decided_position_buys_nothing(self):
        Position.objects.filter(key=self.target.key).update(
            status='WHITE_WIN', closure='MINIMAX')

        payload = self._post(**self._credentials('wolfram')).json()

        self.assertEqual(payload['status'], 'already-solved')
        self.assertIsNone(payload['task'])
        self.assertFalse(
            AnalysisTask.objects.filter(position=self.target).exists())

    def test_a_fen_the_tree_does_not_have_is_seeded(self):
        """Un 404 aqui obligaria a pasar por un formulario HTML para preguntar.

        Es lo que ya hace el cajetin de posicion de la portada con una FEN
        nueva, y por eso lo hace esto: nace como cualquier semilla y lo que la
        trabaja es la escalera de peticiones.
        """
        fresh = logic.apply_move(
            logic.apply_move(self.root.fen, 'g1f3'), 'g8f6')

        payload = self._post(fen=fresh, **self._credentials('wolfram')).json()

        self.assertEqual(payload['status'], 'queued')
        self.assertTrue(payload['seeded'])
        self.assertTrue(Position.objects.filter(key=payload['key']).exists())

    def test_an_unreadable_fen_is_refused_without_touching_anything(self):
        payload = self._post(fen='lol nope').json()

        self.assertEqual(payload['status'], 'refused')
        self.assertIn('reason', payload)
        self.assertFalse(AnalysisTask.objects.exists())

    def test_wrong_credentials_are_not_quietly_downgraded_to_anonymous(self):
        """Quien manda su nombre esta pidiendo que la peticion sea SUYA.

        Servirsela sin nombre — sin aviso, sin afinidad y sin su sitio en el
        reparto — seria contestarle que si a otra cosa.
        """
        response = self._post(username='wolfram', password='wrong')

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['status'], 'refused')
        self.assertFalse(AnalysisTask.objects.exists())

    def test_a_budget_outside_the_ladder_is_refused(self):
        response = self._post(budget='999', **self._credentials('wolfram'))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['status'], 'refused')
        self.assertFalse(AnalysisTask.objects.exists())

    def test_a_get_queues_nothing(self):
        response = self.client.get('/atomicdb/api/request')

        self.assertEqual(response.status_code, 405)
        self.assertFalse(AnalysisTask.objects.exists())

    @mock.patch('atomicdb.views.REQUEST_QUEUE_MAX', 0)
    def test_the_queue_cap_of_the_button_also_holds_here(self):
        # El tope se cuenta POR PERSONA (decision del propietario, 15-ago), en
        # la misma direccion que la asignacion horaria de esta API, que ya era
        # por cuenta.  Sigue siendo el mismo 503 por el mismo camino comun; lo
        # que cambia es que el estado dice de QUIEN es la cola que esta llena,
        # y la frase dice que hacer al respecto.
        response = self._post(**self._credentials('wolfram'))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['status'], 'queue-full-account')
        self.assertIn('clear your queue', response.json()['reason'])

    @mock.patch('atomicdb.views.API_REQUESTS_PER_HOUR', 2)
    def test_the_hourly_allowance_is_per_account(self):
        for _ in range(2):
            self._post(ip='10.0.0.9', **self._credentials('wolfram'))

        blocked = self._post(ip='10.0.0.9', **self._credentials('wolfram'))
        other = self._post(ip='10.0.0.9', **self._credentials('eclipsia'))

        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()['status'], 'refused')
        self.assertEqual(blocked['Retry-After'], '3600')
        self.assertNotEqual(other.status_code, 429)

    @mock.patch('atomicdb.views.API_REQUESTS_PER_HOUR', 2)
    def test_anonymous_callers_are_counted_by_address(self):
        for _ in range(2):
            self._post(ip='10.0.0.7')

        blocked = self._post(ip='10.0.0.7')
        elsewhere = self._post(ip='10.0.0.8')

        self.assertEqual(blocked.status_code, 429)
        self.assertNotEqual(elsewhere.status_code, 429)

    @mock.patch('atomicdb.views.API_REQUESTS_PER_HOUR', 1)
    def test_a_refusal_before_any_work_does_not_spend_the_allowance(self):
        """Una FEN mal escrita no puede dejar a nadie fuera de su propia hora."""
        self._post(fen='lol nope', **self._credentials('wolfram'))

        payload = self._post(**self._credentials('wolfram')).json()

        self.assertEqual(payload['status'], 'queued')
        self.assertEqual(ApiRequestLog.objects.count(), 1)

    def test_a_browser_session_does_not_authenticate_this_endpoint(self):
        """LA razon por la que la exencion de CSRF de aqui es segura.

        Los endpoints vecinos documentan al reves el mismo peligro: exentos,
        una pagina de terceros podia gastarle la escalera a quien la visitara
        con la sesion iniciada.  Aqui no se puede pedir el token — un script no
        tiene pagina — asi que lo que se quita es la cookie, y un POST hecho a
        espaldas de su usuario llega como anonimo.
        """
        self.client.login(username='wolfram', password='p')
        payload = self._post().json()
        self.client.logout()

        self.assertEqual(payload['status'], 'queued')
        self.assertEqual(
            AnalysisTask.objects.get(position=self.target).requested_by, '')


class QueryApiAnalysisTests(TestCase):
    """El bloque de analisis del motor en la respuesta de consulta."""

    def setUp(self):
        self.client = Client()
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.black = ingest.get_or_create_position(
            logic.apply_move(self.root.fen, 'e2e4'))
        ingest.expand(self.black)

    def _query(self, pos=None):
        pos = pos or self.root
        return self.client.get('/atomicdb/api/query',
                               {'fen': pos.fen}).json()

    def test_a_position_nobody_has_searched_says_so_with_a_null(self):
        """Una clave vacia en un cuerpo de veinte es ruido, no informacion."""
        self.assertIsNone(self._query()['analysis'])

    def test_the_rest_of_the_reply_keeps_its_shape(self):
        """ADITIVO: quien consumia esto antes no se entera de que hay algo mas."""
        ingest.ingest_analysis(self.root.key, _lines(self.root.fen, raw=RAW),
                               128_000_000)

        payload = self._query()

        for key in ('fen', 'key', 'status', 'closure', 'score', 'point',
                    'backed_plies', 'best_move', 'tier', 'trust',
                    'history_scope', 'visits', 'nodes', 'moves'):
            self.assertIn(key, payload)
        self.assertEqual(payload['tier'], 'PRACTICAL')

    def test_the_lines_carry_the_engine_numbers_and_the_raw_string(self):
        ingest.ingest_analysis(self.root.key, _lines(self.root.fen, raw=RAW),
                               128_000_000)

        block = self._query()['analysis']

        self.assertEqual(len(block['lines']), 3)
        top = block['lines'][0]
        self.assertEqual(top['depth'], 34)
        self.assertEqual(top['seldepth'], 48)
        self.assertEqual(top['nodes'], 128_000_000)
        self.assertTrue(top['raw'].startswith('info depth 34'))
        self.assertEqual(top['pv'], [top['move']])
        self.assertEqual(block['budget_nodes'], 128_000_000)
        self.assertEqual(block['visits'], 1)

    def test_a_pass_without_a_raw_string_reports_no_depth_at_all(self):
        """No saber la profundidad y afirmar que fue cero son cosas distintas."""
        ingest.ingest_analysis(self.root.key, _lines(self.root.fen),
                               128_000_000)

        top = self._query()['analysis']['lines'][0]

        self.assertNotIn('depth', top)
        self.assertNotIn('raw', top)

    def test_the_scores_are_in_the_same_perspective_as_the_rest(self):
        """Convencion chessdb.cn: del que mueve, en TODO el cuerpo.

        El arbol guarda White-POV, asi que con negras al turno el signo se da
        la vuelta.  Un cuerpo con ``score`` en una perspectiva y las lineas en
        la otra es una trampa para el que lo lea.
        """
        ingest.ingest_analysis(self.black.key,
                               _lines(self.black.fen, value=-40, raw=RAW),
                               128_000_000)

        payload = self._query(self.black)

        self.assertGreater(payload['point'], 0)      # -40 blancas = +40 negras
        self.assertEqual(payload['analysis']['lines'][0]['cp'],
                         payload['point'])

    def test_the_wider_earlier_pass_travels_next_to_the_current_one(self):
        """La otra mitad de la peticion del 29-jul, ahora publicada.

        Un pase profundo de dos lineas no repite lo que las lineas 3..N de un
        pase mas ancho ya sabian, asi que aquel se conserva; esto solo lo saca
        por la API.
        """
        ingest.ingest_analysis(self.root.key,
                               _lines(self.root.fen, count=5, raw=RAW),
                               128_000_000)
        ingest.ingest_analysis(self.root.key,
                               _lines(self.root.fen, value=10, count=2,
                                      raw=RAW),
                               2_000_000_000)

        block = self._query()['analysis']

        self.assertEqual(len(block['lines']), 2)
        self.assertEqual(len(block['previous']), 5)
        self.assertEqual(block['budget_nodes'], 2_000_000_000)

    def test_every_pass_leaves_a_summary_newest_first(self):
        """La pregunta entera: que evaluacion dio el motor a menos profundidad."""
        ingest.ingest_analysis(self.root.key,
                               _lines(self.root.fen, value=-40, count=3,
                                      raw=RAW),
                               128_000_000)
        ingest.ingest_analysis(self.root.key,
                               _lines(self.root.fen, value=120, count=2,
                                      raw=RAW),
                               2_000_000_000)

        passes = self._query()['analysis']['passes']

        self.assertEqual([entry['budget'] for entry in passes],
                         [2_000_000_000, 128_000_000])
        self.assertEqual([entry['eval_cp'] for entry in passes], [120, -40])
        self.assertEqual([entry['lines'] for entry in passes], [2, 3])
        self.assertEqual(passes[0]['depth'], 34)

    def test_a_pass_that_lost_the_arbitration_is_recorded_all_the_same(self):
        """Es JUSTAMENTE el pase que responde la pregunta.

        Una sonda corta no manda en el nodo — y no debe — pero lo que dijo es
        exactamente "que daba el motor a menos profundidad".  ``showcase``
        distingue las dos cosas sin tirar ninguna.
        """
        ingest.ingest_analysis(self.root.key,
                               _lines(self.root.fen, count=3, raw=RAW),
                               128_000_000)
        ingest.ingest_analysis(self.root.key,
                               _lines(self.root.fen, value=900, count=1,
                                      raw=RAW),
                               8_000_000)

        block = self._query()['analysis']

        self.assertEqual(block['passes'][0]['budget'], 8_000_000)
        self.assertFalse(block['passes'][0]['showcase'])
        self.assertTrue(block['passes'][1]['showcase'])
        # Y la foto vigente sigue siendo la del pase que si arbitra.
        self.assertEqual(len(block['lines']), 3)
        self.assertEqual(block['budget_nodes'], 128_000_000)

    def test_the_series_never_claims_more_than_it_kept(self):
        """Los pases anteriores al 15-ago no se guardaron y no se inventan."""
        ingest.ingest_analysis(self.root.key, _lines(self.root.fen, raw=RAW),
                               128_000_000)
        Position.objects.filter(key=self.root.key).update(visits=4)

        block = self._query()['analysis']

        self.assertEqual(len(block['passes']), 1)
        self.assertFalse(block['passes_complete'])

    def test_the_series_is_capped(self):
        """Es una columna de una tabla de millones de filas, no un archivo."""
        for index in range(ingest.PASS_HISTORY_MAX + 3):
            ingest.ingest_analysis(
                self.root.key,
                _lines(self.root.fen, value=index, count=3, raw=RAW),
                128_000_000)

        passes = self._query()['analysis']['passes']

        self.assertEqual(len(passes), ingest.PASS_HISTORY_MAX)
        self.assertEqual(passes[0]['eval_cp'],
                         ingest.PASS_HISTORY_MAX + 2)
