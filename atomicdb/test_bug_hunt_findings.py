"""Pruebas DEMOSTRATIVAS de la revision de caza de bugs.

Un test SIN ``expectedFailure`` es un hallazgo YA ARREGLADO y esta aqui como
regresion (C1 cascada con save completo, C2 carrera de cierre de hijos, A2
cota de intentos del selector df-pn, A3 relevo del solver reiniciado).  Un
test CON el decorador reproduce un hallazgo aun abierto, de forma minima y
determinista: arreglarlo consiste en quitar el decorador y ver el test pasar.
"""

import re
from datetime import timedelta
from unittest import expectedFailure
from unittest.mock import patch

from django.test import Client, override_settings
from django.utils import timezone

from . import ingest, logic, proof, views
from .models import (AnalysisTask, Campaign, CampaignVote, Edge, Position,
                     SolveTask)
from .testing import TestCase, worker_account

CACHE_FOR_TESTS = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'atomicdb-bug-hunt',
    },
}


def _child_of(parent, uci):
    return Edge.objects.get(parent=parent, move_uci=uci).child


# ---------------------------------------------------------------------------
# HALLAZGO 1 (CRITICO) â€” ingest.backup_cascade:603 hace ``pos.save()`` COMPLETO
# sobre una fila leida sin bloqueo en :540.  Todo lo que otro consumer haya
# escrito en esa fila entre la lectura y el guardado se revierte: eval_cp,
# visits, nodes_invested, last_analysis, backed_*, priority, campaign,
# expanded, time_invested.  ``ingest_analysis`` documenta justo esto en :395
# ("un save() completo los pisaria") y usa update_fields; la cascada no.
# ---------------------------------------------------------------------------
class BackupCascadeFullSaveTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.child = _child_of(self.root, 'g1f3')

    def test_cascade_does_not_roll_back_a_concurrent_analysis(self):
        # El nodo esta cerrado, asi que la cascada entra en su rama de
        # refresco de DTM y acaba en el ``pos.save()`` completo.
        Position.objects.filter(key=self.root.key).update(
            status='WHITE_WIN', closure='MINIMAX', mate_in=9,
            eval_cp=100, visits=1, nodes_invested=8_000_000)
        Position.objects.filter(key=self.child.key).update(
            status='WHITE_WIN', closure='MATE_PV', proof='ANDOR',
            won_line='e2e4', mate_in=1)

        original = ingest.mate_distance_refresh

        def concurrent_consumer(pos, edges):
            # Otro proceso de process_ingest_queue COMMITEA aqui: la cascada
            # ya cargo su copia de la fila y todavia no la ha guardado.
            Position.objects.filter(key=self.root.key).update(
                eval_cp=777, visits=42, nodes_invested=512_000_000)
            return original(pos, edges)

        with patch('atomicdb.ingest.mate_distance_refresh',
                   side_effect=concurrent_consumer):
            ingest.backup_cascade([self.child.key])

        fresh = Position.objects.get(key=self.root.key)
        self.assertEqual(fresh.eval_cp, 777)
        self.assertEqual(fresh.visits, 42)
        self.assertEqual(fresh.nodes_invested, 512_000_000)


# ---------------------------------------------------------------------------
# HALLAZGO 2 (CRITICO) â€” ingest.ingest_analysis lee el hijo en :280 (sin
# select_for_update; el lock es del PADRE) y decide con ``child.status`` en
# :293/:304/:308 sobre esa foto.  El guardado de :313 y :346 es incondicional.
# Con transposicion, dos padres distintos ingieren a la vez y el ultimo que
# escribe gana: puede degradar un ANDOR a ENGINE, cambiar la won_line del
# cierre de otro, o marcar DISPUTED una posicion ya cerrada.
# ---------------------------------------------------------------------------
class ChildClosureRaceTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.child = _child_of(self.root, 'g1f3')

    def test_ingest_keeps_a_closure_committed_by_another_consumer(self):
        proven = {0: (True, ['e7e5', 'f3e5'], 'PROVEN', None)}
        lines = [{'move': 'g1f3', 'eval_cp': 30, 'mate': 5,
                  'pv': ['g1f3', 'e7e5', 'f3e5']}]

        def concurrent_consumer(child, ev):
            # Otro consumer cierra el MISMO hijo por otro padre (el DAG
            # transpone) mientras este ingest tiene su foto ``UNKNOWN``.
            Position.objects.filter(key=child.key).update(
                status='BLACK_WIN', closure='MATE_PV', proof='ANDOR',
                won_line='d7d5 c1h6', mate_in=2)
            return True

        with patch('atomicdb.ingest._seed_child_eval',
                   side_effect=concurrent_consumer):
            ingest.ingest_analysis(self.root.key, lines, 8_000_000,
                                   mate_proofs=proven)

        fresh = Position.objects.get(key=self.child.key)
        # El cierre ajeno (ANDOR, exhaustivo) no debe ser sustituido por el
        # testigo de este pase, que lo vio UNKNOWN y ya no lo esta.
        self.assertEqual(fresh.status, 'BLACK_WIN')
        self.assertEqual(fresh.won_line, 'd7d5 c1h6')


# ---------------------------------------------------------------------------
# HALLAZGO 3 (ALTO) â€” ingest._next_tasks_by_proof reutiliza el nombre
# ``budget``: en :1752 es el TOPE DE INTENTOS del bucle y en :1765 pasa a ser
# el presupuesto de nodos de la posicion (>= 8.000.000).  Tras el primer
# descenso util la guarda ``attempts < budget`` deja de acotar nada y el
# bucle puede dar millones de vueltas â€” dentro de /atomicdb/api/lease.
# ---------------------------------------------------------------------------
@override_settings(ATOMICDB_SELECTOR='pn')
class ProofSelectorAttemptCapTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.first = _child_of(self.root, 'g1f3')
        # Una tarea COMPLETED para esa generacion: el descenso la encuentra,
        # ``task.state != 'PENDING'`` y ``tasks`` no crece nunca.
        AnalysisTask.objects.create(position=self.first,
                                    generation=self.first.visits,
                                    budget_nodes=8_000_000,
                                    state='COMPLETED')

    def test_the_descent_loop_stops_at_its_attempt_cap(self):
        wanted = 2
        cap = 4 * wanted
        calls = {'n': 0}

        def descend(campaign, counter=0, avoid=()):
            calls['n'] += 1
            if calls['n'] > cap + 4:
                raise AssertionError(
                    f'attempt cap defeated: {calls["n"]} descents for n={wanted}')
            if calls['n'] == 1:
                return self.first, 1
            return None, 0

        with patch('atomicdb.proof.active_campaigns',
                   return_value=list(proof.ProofCampaign.objects.all()[:1])):
            with patch('atomicdb.proof.descend', side_effect=descend):
                ingest._next_tasks_by_proof(wanted)

        self.assertLessEqual(calls['n'], cap)


# ---------------------------------------------------------------------------
# HALLAZGO 4 (ALTO) â€” el protocolo SOLVE nunca recibio el relevo tras reinicio
# que views.api_lease si tiene (RESTART_RECYCLE_MINUTES, incidente t24).
# Ademas el worker publicado NUNCA llama a /api/solve/heartbeat (solo lo hacen
# los tests), asi que lease_heartbeat_at se queda clavado en leased_at.  Un
# worker reiniciado, o una respuesta de acquire perdida, dejan su hilo de
# pruebas parado los 20 minutos enteros de SOLVE_LEASE_MINUTES.
# ---------------------------------------------------------------------------
class SolveLeaseRestartTests(TestCase):

    def setUp(self):
        worker_account('solver', 'pw')
        self.root = ingest.get_or_create_position(logic.start_fen())
        self.campaign = proof.ProofCampaign.objects.first()
        SolveTask.objects.create(position=self.root, campaign=self.campaign,
                                 goal='WHITE_WIN', budget_nodes=2_000_000)

    def _acquire(self, session):
        return self.client.post('/atomicdb/api/solve/acquire', {
            'username': 'solver', 'password': 'pw', 'machine': 'box-1',
            'lease_session': session, 'threads': 1, 'hash': 64})

    def test_a_restarted_solver_is_relieved_like_an_analysis_worker(self):
        first = self._acquire('session-a')
        self.assertEqual(len(first.json()['tasks']), 1)

        # El titular lleva mas de RESTART_RECYCLE_MINUTES sin dar senales:
        # esta muerto, y quien pide con SU identidad es su relevo.
        cold = timezone.now() - timedelta(minutes=5)
        SolveTask.objects.all().update(leased_at=cold,
                                       lease_heartbeat_at=cold)

        relief = self._acquire('session-b')
        self.assertEqual(len(relief.json()['tasks']), 1)


# ---------------------------------------------------------------------------
# HALLAZGO 5 (MEDIO) â€” views.campaign_vote documenta que el recuento se
# reescribe con el COUNT real "en la misma transaccion que el voto, nunca con
# un +1: un incremento pierde una carrera".  El COUNT sin ``select_for_update``
# sobre la campana pierde exactamente la misma carrera, y aqui no hay nada que
# lo repare despues: la desviacion es permanente y alimenta CAMPAIGN_BONUS.
# ---------------------------------------------------------------------------
class CampaignVoteCountTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        self.campaign = Campaign.objects.create(
            name='line', root=self.root, state=Campaign.CState.PROPOSED,
            active=False)

    def test_the_cached_count_never_drifts_from_the_rows(self):
        # El seno de la carrera es el recuento: otro votante COMMITEA justo
        # antes de que este voto reescriba la cache.  Mientras el COUNT se
        # hacia en Python y se guardaba despues, ese voto se perdia para
        # siempre; ahora el COUNT lo hace la base dentro del mismo UPDATE.
        original = views._recount_campaign_votes

        def racing_recount(campaign_id):
            if not CampaignVote.objects.filter(token='other').exists():
                CampaignVote.objects.create(campaign=self.campaign,
                                            token='other')
            return original(campaign_id)

        with patch.object(views, '_recount_campaign_votes',
                          side_effect=racing_recount):
            response = self.client.post(
                f'/atomicdb/campaign/{self.campaign.id}/vote/', {})
        self.assertEqual(response.status_code, 200)

        self.campaign.refresh_from_db()
        self.assertEqual(
            self.campaign.votes,
            CampaignVote.objects.filter(campaign=self.campaign).count())
        self.assertEqual(self.campaign.votes, 2)


# ---------------------------------------------------------------------------
# HALLAZGO 6 (ALTO) â€” ``cache_page`` es un DECORADOR, asi que
# UpdateCacheMiddleware.process_response corre ANTES de que
# CsrfViewMiddleware ponga la cookie.  La guarda de Django "no cachear una
# respuesta que estrena cookie ante una peticion sin cookies" mira
# ``response.cookies``, que en ese instante esta vacio, asi que NO dispara â€”
# al reves de lo que afirma el comentario de atomicdb/urls.py:13-15.
# Resultado: el segundo visitante SIN cookies recibe el cuerpo del primero
# (su token CSRF incluido) y no recibe cookie csrftoken ninguna, con lo que su
# POST del cajetin de FEN se rechaza.
# ---------------------------------------------------------------------------
@override_settings(CACHES=CACHE_FOR_TESTS)
class CookielessHomeCacheTests(TestCase):

    def setUp(self):
        ingest.get_or_create_position(logic.start_fen())

    def test_a_second_cookieless_visitor_gets_its_own_csrf_state(self):
        first = Client().get('/atomicdb/')
        second = Client().get('/atomicdb/')

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertIn('csrfmiddlewaretoken', first.content.decode())
        # Sin esto el formulario de la home es inservible para el segundo.
        self.assertIn('csrftoken', second.cookies)
        self.assertNotEqual(first.content, second.content)


# ---------------------------------------------------------------------------
# HALLAZGO 7 (MEDIO) â€” la misma pagina llama "unexplored" a dos poblaciones
# DISJUNTAS.  El boton usa ingest.unexplored_children (hijos MATERIALIZADOS
# sin ningun conocimiento) y la tabla lista las jugadas legales que NO tienen
# arista.  Un nodo expandido cuyos hijos estan todos sin analizar ensena
# "Analyse all N unexplored replies" con CERO filas "unexplored" debajo.
# ---------------------------------------------------------------------------
class UnexploredLabelTests(TestCase):

    @expectedFailure
    def test_the_button_count_and_the_unexplored_rows_agree(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)          # todas las aristas existen, ningun eval

        page = self.client.get(f'/atomicdb/explore/{root.key}/')
        body = page.content.decode()

        advertised = re.search(r'Analyse all (\d+) unexplored replies', body)
        self.assertIsNotNone(advertised, 'the button is not on the page')
        # explore.html:358 marca cada fila sin explorar con este texto.
        rows = len(re.findall(r'>unexplored<', body))
        self.assertEqual(int(advertised.group(1)), rows)


# ---------------------------------------------------------------------------
# HALLAZGO 8 (MEDIO) â€” el tope diario de propuestas de campana se cuenta
# contra ``proposed_token``, y views._voter_token acuna un token NUEVO en
# cuanto la cookie no llega.  Un cliente que simplemente no devuelva la cookie
# tiene el contador a cero en cada peticion: el tope no acota nada, y cada
# propuesta cuesta un recorrido de linaje completo mas una fila.
# ---------------------------------------------------------------------------
class CampaignProposalCapTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.keys = [edge.child_id for edge in
                     Edge.objects.filter(parent=self.root)[:6]]

    def test_the_daily_cap_holds_for_a_client_that_drops_the_cookie(self):
        from .views import CAMPAIGN_PROPOSALS_PER_VOTER_DAY

        for key in self.keys:
            Client().post('/atomicdb/campaign/propose/', {'key': key})

        self.assertLessEqual(Campaign.objects.count(),
                             CAMPAIGN_PROPOSALS_PER_VOTER_DAY)
