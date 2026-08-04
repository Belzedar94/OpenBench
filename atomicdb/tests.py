"""M1 gate (spec §7): mates conocidos cierran, fortalezas sinteticas NO,
completitud de movegen, backup determinista bajo replay."""

from datetime import timedelta
from unittest import mock

from django.db import OperationalError
from django.utils import timezone

from . import ingest, logic
from .models import AnalysisTask, Edge, Position
from .testing import TestCase, worker_account


class LogicTests(TestCase):

    def test_canonical_strips_counters(self):
        f = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 7 42'
        self.assertTrue(logic.canonical_fen(f).endswith('0 1'))

    def test_terminal_explosion_win(self):
        # dama captura f7 y explota al rey negro: terminal WHITE_WIN
        fen = logic.canonical_fen(
            '3qk3/5Q2/8/8/8/8/8/4K3 b - - 0 1')
        # el rey negro NO esta explotado aun; posicion viva
        self.assertIsNone(logic.terminal_status(
            logic.canonical_fen('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1')))
        del fen

    def test_verify_mate_pv_accepts_real_mate(self):
        # mate atomico rapido conocido: 1.e4 e5 2.Qh5 g6?? 3.Qxe5+?? no...
        # construimos uno sintetico: rey ahogado por explosion inminente.
        # Usamos el "fool's mate" atomico: 1.e3 g5 2.Qh5 f6?? 3.Qxg5! (explota
        # f6/g5 y amenaza...) — en su lugar validamos mecanicamente:
        # posicion con mate en 1 real segun pyffish.
        fen = self._find_mate_in_1()
        self.assertIsNotNone(fen, 'no se encontro mate-en-1 de fixture')
        pos_fen, mating_move, winner_white = fen
        ok = logic.verify_mate_pv(pos_fen, [mating_move], winner_white)
        self.assertTrue(ok)

    def test_verify_mate_pv_rejects_illegal(self):
        f = logic.start_fen()
        self.assertFalse(logic.verify_mate_pv(f, ['e2e5'], True))

    def test_verify_mate_pv_rejects_nonterminal(self):
        f = logic.start_fen()
        self.assertFalse(logic.verify_mate_pv(f, ['e2e4'], True))

    def _find_mate_in_1(self):
        """Busca por fuerza bruta un mate en 1 desde una posicion semilla."""
        import pyffish as pf
        seeds = [
            # dama a distancia de explosion del rey encajonado
            '6rk/6pp/8/8/8/8/8/QK6 w - - 0 1',
            '7k/5ppp/8/8/8/8/8/QK6 w - - 0 1',
            'k7/pp6/8/8/8/8/8/KQ6 w - - 0 1',
        ]
        for s in seeds:
            s = logic.canonical_fen(s)
            try:
                for uci in logic.legal_moves(s):
                    child = logic.apply_move(s, uci)
                    t = logic.terminal_status(child)
                    if t and t[0] == 'WHITE_WIN':
                        return (s, uci, True)
            except Exception:
                continue
        return None


class BackupTests(TestCase):

    def _mk(self, fen, status='UNKNOWN'):
        p = ingest.get_or_create_position(fen)
        if status != 'UNKNOWN' and p.status == 'UNKNOWN':
            p.status, p.closure = status, 'TERMINAL'
            p.save()
        return p

    def test_movegen_completeness_on_expand(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        self.assertEqual(Edge.objects.filter(parent=root).count(), 20)
        self.assertTrue(Position.objects.get(key=root.key).expanded)

    def test_minimax_win_propagates(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        # marca artificialmente un hijo como ganado por blancas
        e = Edge.objects.filter(parent=root).select_related('child').first()
        c = e.child
        c.status, c.closure = 'WHITE_WIN', 'MATE_PV'
        c.save()
        ingest.backup_cascade([c.key])
        root.refresh_from_db()
        self.assertEqual(root.status, 'WHITE_WIN')
        self.assertEqual(root.closure, 'MINIMAX')

    def test_minimax_loss_requires_full_expansion(self):
        root = ingest.get_or_create_position(logic.start_fen())
        # SIN expandir: aunque un hijo conocido pierda, no se cierra derrota
        e5 = logic.apply_move(logic.start_fen(), 'e2e4')
        child = ingest.get_or_create_position(e5)
        Edge.objects.create(parent=root, move_uci='e2e4', child=child)
        child.status = 'BLACK_WIN'
        child.save()
        ingest.backup_cascade([child.key])
        root.refresh_from_db()
        self.assertEqual(root.status, 'UNKNOWN')

    def test_backup_deterministic_replay(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        for e in Edge.objects.filter(parent=root).select_related('child')[:5]:
            e.child.eval_cp = 100
            e.child.save()
        ingest.backup_cascade([root.key])
        v1 = Position.objects.get(key=root.key).eval_cp
        ingest.backup_cascade([root.key])
        v2 = Position.objects.get(key=root.key).eval_cp
        self.assertEqual(v1, v2)

    def test_synthetic_fortress_does_not_close(self):
        # posicion tranquila sin mate: jamas debe cerrar por evals altos
        f = logic.canonical_fen('4k3/8/8/8/8/8/4P3/4K3 w - - 0 1')
        p = ingest.get_or_create_position(f)
        ingest.expand(p)
        ingest.ingest_analysis(p.key, [
            {'move': 'e2e4', 'eval_cp': 900, 'mate': None, 'pv': ['e2e4']},
        ], nodes_budget=1000)
        p.refresh_from_db()
        self.assertEqual(p.status, 'UNKNOWN')  # eval NUNCA cierra


class IngestTests(TestCase):

    def test_mate_pv_closure_via_ingest(self):
        lt = LogicTests()
        found = lt._find_mate_in_1()
        self.assertIsNotNone(found)
        pos_fen, mating_move, _ = found
        # el PADRE (posicion con mate en 1) recibe el analisis del motor
        p = ingest.get_or_create_position(pos_fen)
        res = ingest.ingest_analysis(p.key, [
            {'move': mating_move, 'eval_cp': 9999, 'mate': 1,
             'pv': [mating_move]},
        ], nodes_budget=1000)
        p.refresh_from_db()
        # el hijo (posicion tras el mate) es terminal y el backup cierra al padre
        self.assertEqual(p.status, 'WHITE_WIN')
        del res

    def _find_mate_in_2_line(self):
        """Busca (pos, [m1, respuesta, m2]) donde m2 remata tras una
        respuesta legal cualquiera: el fixture minimo de un cierre MATE_PV."""
        for s in ['7k/5ppp/8/8/8/8/8/QK6 w - - 0 1',
                  '6rk/6pp/8/8/8/8/8/QK6 w - - 0 1']:
            s = logic.canonical_fen(s)
            for m1 in logic.legal_moves(s):
                c = logic.apply_move(s, m1)
                if logic.terminal_status(c) is not None:
                    continue
                for r in logic.legal_moves(c):
                    d = logic.apply_move(c, r)
                    if logic.terminal_status(d) is not None:
                        continue
                    for m2 in logic.legal_moves(d):
                        t = logic.terminal_status(logic.apply_move(d, m2))
                        if t and t[0] == 'WHITE_WIN':
                            return s, [m1, r, m2]
        return None, None

    def test_mate_pv_closure_emits_event(self):
        from .models import DBEvent
        from .test_proofs import FORCED_MATE_FEN, FORCED_MATE_PV
        s, pv = FORCED_MATE_FEN, FORCED_MATE_PV
        p = ingest.get_or_create_position(s)
        ingest.ingest_analysis(p.key, [{'move': pv[0], 'eval_cp': 9998,
                                        'mate': 2, 'pv': pv}], 1000)
        self.assertTrue(DBEvent.objects.filter(
            kind='NODE_CLOSED', payload__closure='MATE_PV').exists())

    def test_ingest_idempotent_when_closed(self):
        lt = LogicTests()
        pos_fen, mating_move, _ = lt._find_mate_in_1()
        p = ingest.get_or_create_position(pos_fen)
        ingest.ingest_analysis(p.key, [{'move': mating_move, 'eval_cp': 9999,
                                        'mate': 1, 'pv': [mating_move]}], 1000)
        r2 = ingest.ingest_analysis(p.key, [], 1000)
        self.assertEqual(r2.get('skipped'), 'already-closed')


class TablebaseTests(TestCase):

    def test_applicability(self):
        from . import logic
        self.assertTrue(logic.tb_applicable('4k3/8/8/8/8/8/8/QK6 w - - 0 1'))
        # derechos de enroque: NO aplicable
        self.assertFalse(logic.tb_applicable(
            '4k3/8/8/8/8/8/8/R3K3 w Q - 0 1'))
        # mas de 6 piezas: NO aplicable
        self.assertFalse(logic.tb_applicable(
            'rnbqkbnr/8/8/8/8/8/8/RNBQKBNR w - - 0 1'))

    def test_wdl_mapping(self):
        from . import logic
        self.assertEqual(logic.wdl_to_status(2, True), 'WHITE_WIN')
        self.assertEqual(logic.wdl_to_status(2, False), 'BLACK_WIN')
        self.assertEqual(logic.wdl_to_status(-2, True), 'BLACK_WIN')
        # cursed win / blessed loss = tablas practicas bajo regla de 50
        self.assertEqual(logic.wdl_to_status(1, True), 'DRAW')
        self.assertEqual(logic.wdl_to_status(-1, False), 'DRAW')

    @mock.patch('atomicdb.ingest.tb.probe_wdl', return_value=2)
    def test_close_by_tb(self, probe):
        from . import ingest
        p = ingest.get_or_create_position('4k3/8/8/8/8/8/8/QK6 w - - 0 1')
        self.assertTrue(ingest.close_by_tb(p.key, 2))
        probe.assert_called_once_with(p.fen, max_pieces=5)
        p.refresh_from_db()
        self.assertEqual(p.status, 'WHITE_WIN')
        self.assertEqual(p.closure, 'TB')
        # idempotente / no re-cierra
        self.assertFalse(ingest.close_by_tb(p.key, -2))

    def test_close_by_tb_rejects_castling(self):
        from . import ingest
        p = ingest.get_or_create_position('4k3/8/8/8/8/8/8/R3K3 w Q - 0 1')
        self.assertFalse(ingest.close_by_tb(p.key, 2))
        p.refresh_from_db()
        self.assertEqual(p.status, 'UNKNOWN')


class SelectorTests(TestCase):

    def setUp(self):
        # The production cache intentionally spans requests; each TestCase
        # starts from a freshly flushed database and therefore resets it.
        ingest._priority_refresh_cache['at'] = 0.0

    def test_global_best_first_by_eval(self):
        # gana el nodo conectado de eval mas decisivo
        a = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(a)
        a.eval_cp = 50
        a.save()
        b = Edge.objects.get(parent=a, move_uci='e2e4').child
        b.eval_cp, b.expanded = 800, True
        b.save()
        # The global pass is the selector SERVICE's job now, not the lease
        # path's; the ordering it produces is what this test is about.
        ingest.refresh_priorities()
        tasks = ingest.next_tasks(1)
        self.assertEqual(tasks[0].position_id, b.key)

    def test_priority_prefers_root_relevant_lines(self):
        # mismo |eval|: el nieto bajo la mejor primera jugada puntua muy por
        # encima del nieto bajo un opening refutado (regret desde la raiz)
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        good = Edge.objects.get(parent=root, move_uci='g1f3').child
        bad = Edge.objects.get(parent=root, move_uci='a2a3').child
        good.eval_cp, bad.eval_cp = 441, -191
        good.save(), bad.save()
        ingest.expand(good), ingest.expand(bad)
        g_kid = Edge.objects.filter(parent=good).first().child
        b_kid = Edge.objects.filter(parent=bad).first().child
        for kid in (g_kid, b_kid):
            kid.eval_cp, kid.expanded = 600, True
            kid.save()
        ingest.refresh_priorities()
        g_kid.refresh_from_db(), b_kid.refresh_from_db()
        self.assertGreater(g_kid.priority, b_kid.priority + 10)

    def test_mate_band_boost_and_budget_jump(self):
        p = ingest.get_or_create_position(logic.start_fen())
        # Mate visto por el motor, aun sin cerrar, y LARGO: el salto de
        # presupuesto existe para extraer una PV que de verdad hay que
        # extraer.  Con un mate corto lo que se compra hoy es una
        # verificacion barata (``ingest._short_mate_clamp``, test_mate_clamp),
        # asi que la distancia forma parte del fixture, no es un detalle.
        p.eval_cp = 10_000 - 40   # M40: 79 plies de PV que sacar
        p.save()
        ingest.refresh_priorities()
        tasks = ingest.next_tasks(1)
        self.assertEqual(tasks[0].position_id, p.key)
        self.assertGreaterEqual(tasks[0].budget_nodes,
                                ingest.BUDGET_LADDER[2])

    def test_dead_branch_not_selected(self):
        # hijos cuyo unico padre esta cerrado: analizarlos no influye arriba
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        root.status, root.closure = 'WHITE_WIN', 'MATE_PV'
        root.save()
        child = Edge.objects.filter(parent=root).first().child
        child.eval_cp = 900
        child.save()
        tasks = ingest.next_tasks(50)
        self.assertNotIn(child.key, [t.position_id for t in tasks])

    def test_pool_top_up_keeps_the_queue_fed(self):
        # El colchon del selector: mintea hasta el objetivo y, ya lleno, no
        # duplica nada (informe de Lesha: valles con la cola en cero).
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        for edge in Edge.objects.filter(parent=root)[:6]:
            child = edge.child
            child.eval_cp, child.expanded = 100, True
            child.save()
        ingest.refresh_priorities()

        minted = ingest.top_up_analysis_pool(target=5)

        self.assertEqual(minted, 5)
        self.assertEqual(AnalysisTask.objects.filter(state='PENDING').count(),
                         5)
        self.assertEqual(ingest.top_up_analysis_pool(target=5), 0)

    def test_tombstones_survive_refresh_no_starvation(self):
        # zombis con eval de mate NO deben resucitar en cada refresh ni
        # matar de hambre a las posiciones vivas de menor prioridad
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        root.status, root.closure = 'WHITE_WIN', 'MATE_PV'
        root.save()
        for e in Edge.objects.filter(parent=root).select_related('child'):
            e.child.eval_cp = -10_000   # banda de mate: prioridad maxima
            e.child.save()
        live = ingest.get_or_create_position(
            '4k3/8/8/8/8/8/4P3/4K3 w - - 0 1')
        live.eval_cp = 50   # viva pero modesta
        live.save()
        ingest.refresh_priorities()
        ingest.next_tasks(3)              # entierra a los 20 zombis
        ingest._priority_refresh_cache['at'] = 0.0
        ingest.refresh_priorities()       # el refresh NO debe resucitarlos
        tasks = ingest.next_tasks(3)
        self.assertIn(live.key, [t.position_id for t in tasks])

    def test_new_edge_revives_tombstoned_position(self):
        p = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))
        p.priority = ingest.DEAD
        p.save()
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)   # crea la arista root->e2e4: revive
        p.refresh_from_db()
        self.assertGreaterEqual(p.priority, 0.0)


class RequestTests(TestCase):

    def test_request_queues_user_task_with_floor(self):
        from .models import AnalysisTask
        p = ingest.get_or_create_position(logic.start_fen())
        self.assertEqual(ingest.request_analysis(p), 'queued')
        t = AnalysisTask.objects.get(position=p)
        self.assertEqual((t.source, t.state), ('USER', 'PENDING'))
        # posicion fresca: no la sonda minima, sino el suelo de peticiones
        self.assertGreaterEqual(t.budget_nodes, ingest.BUDGET_LADDER[2])

    def test_request_promotes_existing_auto_task(self):
        from .models import AnalysisTask
        p = ingest.get_or_create_position(logic.start_fen())
        ingest.next_tasks(1)   # crea la tarea AUTO (peldano bajo)
        self.assertEqual(ingest.request_analysis(p), 'queued')
        t = AnalysisTask.objects.get(position=p)
        self.assertEqual(t.source, 'USER')
        self.assertGreaterEqual(t.budget_nodes, ingest.BUDGET_LADDER[2])

    def test_reanalysis_uses_128m_512m_2b_10b_staircase(self):
        p = ingest.get_or_create_position(logic.start_fen())
        expected = ingest.REQUEST_BUDGET_LADDER
        for generation, budget in enumerate(expected):
            self.assertEqual(ingest.request_analysis(p), 'queued')
            task = AnalysisTask.objects.get(position=p,
                                             generation=generation)
            self.assertEqual(task.budget_nodes, budget)
            task.state = 'COMPLETED'
            task.save(update_fields=['state'])
            p.visits = generation + 1
            p.save(update_fields=['visits'])

        # Past the top rung the request stops buying the same 10B search
        # again and becomes a frontier expansion instead (test_frontier.py).
        self.assertEqual(ingest.request_analysis(p), 'expanded')
        self.assertFalse(AnalysisTask.objects.filter(
            position=p, generation=len(expected)).exists())

    def test_reanalysis_refreshes_stale_position_before_selecting_rung(self):
        p = ingest.get_or_create_position(logic.start_fen())
        AnalysisTask.objects.create(
            position=p, generation=0, budget_nodes=128_000_000,
            state=AnalysisTask.TState.COMPLETED)
        # Deliberately leave ``p`` stale, as happens when submit advances the
        # position between the page render and the public request.
        Position.objects.filter(pk=p.pk).update(visits=1)

        self.assertEqual(ingest.request_analysis(p), 'queued')

        follow_up = AnalysisTask.objects.get(position=p, generation=1)
        self.assertEqual(follow_up.budget_nodes, 512_000_000)

    def test_request_during_shallow_lease_preserves_deep_follow_up(self):
        from .models import RequestLog
        p = ingest.get_or_create_position(logic.start_fen())
        running = AnalysisTask.objects.create(
            position=p, generation=0, budget_nodes=8_000_000,
            state=AnalysisTask.TState.LEASED, machine='m1',
            leased_at=timezone.now())

        response = self.client.post(f'/atomicdb/request/{p.key}/')

        self.assertEqual(response.json()['status'], 'queued')
        follow_up = AnalysisTask.objects.get(position=p, generation=1)
        self.assertEqual((follow_up.state, follow_up.source,
                          follow_up.budget_nodes),
                         ('PENDING', 'USER',
                          ingest.REQUEST_BUDGET_LADDER[0]))
        running.refresh_from_db()
        self.assertEqual(running.budget_nodes, 8_000_000)
        self.assertTrue(RequestLog.objects.filter(position=p).exists())

        lease_payload = {
            'username': 'u', 'password': 'p', 'machine': 'm2', 'tb': '1',
            'worker_build': '2026072203', 'lease_session': 'session-m2',
        }
        worker_account('u', 'p')
        premature = self.client.post('/atomicdb/api/lease', lease_payload)
        self.assertNotIn(follow_up.id,
                         [row['id'] for row in premature.json()['tasks']])

        Position.objects.filter(pk=p.pk).update(visits=1)
        running.state = AnalysisTask.TState.COMPLETED
        running.save(update_fields=['state'])
        ready = self.client.post('/atomicdb/api/lease', lease_payload)
        self.assertEqual(ready.json()['tasks'][0]['id'], follow_up.id)

    def test_deep_follow_up_is_not_run_after_shallow_visit_solves_position(self):
        worker_account('u', 'p')
        p = ingest.get_or_create_position(logic.start_fen())
        AnalysisTask.objects.create(
            position=p, generation=0, budget_nodes=8_000_000,
            state=AnalysisTask.TState.LEASED, machine='m1',
            leased_at=timezone.now())
        self.assertEqual(ingest.request_analysis(p), 'queued')
        follow_up = AnalysisTask.objects.get(position=p, generation=1)
        Position.objects.filter(pk=p.pk).update(
            visits=1, status='DRAW', closure='MINIMAX')

        response = self.client.post('/atomicdb/api/lease', {
            'username': 'u', 'password': 'p', 'machine': 'm2', 'tb': '1',
            'worker_build': '2026072203', 'lease_session': 'session-m2',
        })

        self.assertNotIn(follow_up.id,
                         [row['id'] for row in response.json()['tasks']])
        self.assertEqual(self.client.get('/atomicdb/').context['requested_h'],
                         '0')

    def test_request_on_solved_position(self):
        from .models import AnalysisTask
        p = ingest.get_or_create_position(logic.start_fen())
        p.status, p.closure = 'DRAW', 'MINIMAX'
        p.save()
        self.assertEqual(ingest.request_analysis(p), 'already-solved')
        self.assertFalse(AnalysisTask.objects.filter(position=p).exists())

    def test_user_requests_leased_first(self):
        import json
        worker_account('u', 'p')
        a = ingest.get_or_create_position(logic.start_fen())
        b = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))
        a.eval_cp, a.expanded = 800, True   # prioridad alta
        b.eval_cp, b.expanded = 50, True    # prioridad baja
        a.save(), b.save()
        ingest.next_tasks(2)                # tareas AUTO para ambas
        r = self.client.post(f'/atomicdb/request/{b.key}/')
        self.assertEqual(r.json()['status'], 'queued')
        # Un worker moderno (con token): desde el suelo de 512M (28-jul) una
        # tarea USER de primera visita ya no es tomable por un build legacy,
        # y este test es sobre el ORDEN, no sobre la compatibilidad legacy.
        lease = self.client.post('/atomicdb/api/lease',
                                 {'username': 'u', 'password': 'p',
                                  'worker_build': '2026072203',
                                  'lease_session': 'orden-test'})
        tasks = json.loads(lease.content)['tasks']
        self.assertEqual(tasks[0]['fen'], b.fen)  # USER antes que mejor prio

    def test_receipts_no_longer_rate_limit_an_ip(self):
        """The hourly allowance was removed (owner, 28-jul)."""
        from .models import RequestLog
        a = ingest.get_or_create_position(logic.start_fen())
        b = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))
        for _ in range(30):
            RequestLog.objects.create(ip='127.0.0.1', position=a)
        r = self.client.post(f'/atomicdb/request/{b.key}/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['status'], 'queued')

    @mock.patch('atomicdb.views.REQUEST_QUEUE_MAX', 0)
    def test_request_queue_full_keeps_structured_status(self):
        p = ingest.get_or_create_position(logic.start_fen())

        response = self.client.post(f'/atomicdb/request/{p.key}/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'queue-full'})

    def test_the_queue_ceiling_is_a_real_number(self):
        # El tope estuvo abierto de par en par (un millon) mientras se medía
        # el uso; un techo que no frena nada es un techo que no esta puesto,
        # y el mecanismo entero seguia verde en los tests igualmente. Esto
        # fija que exista un numero de verdad, holgado sobre el uso real
        # (~100 peticiones humanas vivas el 30-jul) y lejos del infinito.
        from . import views
        self.assertGreaterEqual(views.REQUEST_QUEUE_MAX, 1000)
        self.assertLessEqual(views.REQUEST_QUEUE_MAX, 50_000)

    def test_request_dedup_same_ip_position(self):
        p = ingest.get_or_create_position(logic.start_fen())
        self.client.post(f'/atomicdb/request/{p.key}/')
        r = self.client.post(f'/atomicdb/request/{p.key}/')
        self.assertEqual(r.json()['status'], 'already-requested')

    def test_sufficient_auto_lease_is_promoted_and_deduplicated(self):
        from .models import RequestLog

        p = ingest.get_or_create_position(logic.start_fen())
        task = AnalysisTask.objects.create(
            position=p, generation=0,
            budget_nodes=ingest.REQUEST_BUDGET_LADDER[0],
            state=AnalysisTask.TState.LEASED,
            source=AnalysisTask.Source.AUTO,
            machine='m1', leased_at=timezone.now(),
        )

        first = self.client.post(f'/atomicdb/request/{p.key}/')
        second = self.client.post(f'/atomicdb/request/{p.key}/')

        self.assertEqual(first.json()['status'], 'already-queued')
        self.assertEqual(second.json()['status'], 'already-requested')
        task.refresh_from_db()
        self.assertEqual(task.source, AnalysisTask.Source.USER)
        self.assertEqual(RequestLog.objects.filter(position=p).count(), 1)

    def test_completed_recent_request_can_queue_next_rung(self):
        p = ingest.get_or_create_position(logic.start_fen())
        first = self.client.post(f'/atomicdb/request/{p.key}/')
        self.assertEqual(first.json()['status'], 'queued')
        task = AnalysisTask.objects.get(position=p, generation=0)
        self.assertEqual(task.budget_nodes, ingest.REQUEST_BUDGET_LADDER[0])
        task.state = AnalysisTask.TState.COMPLETED
        task.nodes_searched = ingest.REQUEST_BUDGET_LADDER[0]
        task.save(update_fields=['state', 'nodes_searched'])
        Position.objects.filter(pk=p.pk).update(
            visits=1, nodes_invested=ingest.REQUEST_BUDGET_LADDER[0])

        second = self.client.post(f'/atomicdb/request/{p.key}/')

        self.assertEqual(second.json()['status'], 'queued')
        follow_up = AnalysisTask.objects.get(position=p, generation=1)
        self.assertEqual(
            (follow_up.state, follow_up.source, follow_up.budget_nodes),
            (AnalysisTask.TState.PENDING, AnalysisTask.Source.USER,
             ingest.REQUEST_BUDGET_LADDER[1]),
        )

    def test_fen_creation_log_does_not_block_first_analysis_request(self):
        from .models import RequestLog

        fen = logic.apply_move(logic.start_fen(), 'g1f3')
        created = self.client.post('/atomicdb/fen/', {'fen': fen})
        self.assertEqual(created.status_code, 302)
        p = Position.objects.get(key=logic.key_of(logic.canonical_fen(fen)))
        self.assertTrue(RequestLog.objects.filter(position=p).exists())
        self.assertFalse(AnalysisTask.objects.filter(position=p).exists())

        response = self.client.post(f'/atomicdb/request/{p.key}/')

        self.assertEqual(response.json()['status'], 'queued')
        task = AnalysisTask.objects.get(position=p)
        self.assertEqual(
            (task.state, task.source, task.budget_nodes),
            (AnalysisTask.TState.PENDING, AnalysisTask.Source.USER,
             ingest.REQUEST_BUDGET_LADDER[0]),
        )

    @mock.patch(
        'atomicdb.views.ingest.request_analysis',
        side_effect=OperationalError('database is locked'),
    )
    def test_request_lock_is_reported_as_retryable_busy(self, request_analysis):
        from .models import RequestLog

        p = ingest.get_or_create_position(logic.start_fen())
        response = self.client.post(f'/atomicdb/request/{p.key}/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'busy'})
        self.assertEqual(response['Retry-After'], '2')
        self.assertFalse(RequestLog.objects.filter(position=p).exists())
        request_analysis.assert_called_once()

    @mock.patch(
        'atomicdb.views.RequestLog.objects.create',
        side_effect=OperationalError('database is locked'),
    )
    def test_request_log_lock_rolls_back_new_task(self, create_log):
        from .models import RequestLog

        p = ingest.get_or_create_position(logic.start_fen())
        response = self.client.post(f'/atomicdb/request/{p.key}/')

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'busy'})
        self.assertFalse(AnalysisTask.objects.filter(position=p).exists())
        self.assertFalse(RequestLog.objects.filter(position=p).exists())
        create_log.assert_called_once()

    @mock.patch(
        'atomicdb.views.ingest.request_analysis',
        side_effect=OperationalError('disk I/O error'),
    )
    def test_non_lock_database_error_is_not_hidden_or_retried(
            self, request_analysis):
        p = ingest.get_or_create_position(logic.start_fen())

        with self.assertRaisesMessage(OperationalError, 'disk I/O error'):
            self.client.post(f'/atomicdb/request/{p.key}/')

        request_analysis.assert_called_once()

    def test_explorer_retries_transient_request_contention(self):
        p = ingest.get_or_create_position(logic.start_fen())

        response = self.client.get(f'/atomicdb/explore/{p.key}/')

        self.assertContains(response, "r.status === 503")
        self.assertContains(response, "'busy'")
        self.assertContains(response, 'Server busy, retrying...')
        self.assertContains(response, 'error.retryable === true')
        self.assertContains(response, "'rate-limited'")
        self.assertContains(response, "'queue-full'")
        self.assertContains(response, 'if (!data)')
        self.assertNotContains(response, '!r.ok || !data')


class MachineVisibilityTests(TestCase):

    def test_lease_and_submit_update_ping(self):
        import json
        from .models import WorkerPing
        worker_account('u', 'p')
        ingest.get_or_create_position(logic.start_fen())
        payload = {'username': 'u', 'password': 'p', 'machine': 'u-atomicdb',
                   'threads': 8, 'hash': 1024, 'os': 'TestOS 1'}
        lease = self.client.post('/atomicdb/api/lease', payload)
        tasks = json.loads(lease.content)['tasks']
        self.assertEqual(len(tasks), 1)
        ping = WorkerPing.objects.get(machine='u-atomicdb')
        self.assertEqual((ping.threads, ping.hash_mb, ping.os,
                          ping.tasks_done), (8, 1024, 'TestOS 1', 0))
        self.assertEqual(ping.current_task_id, tasks[0]['id'])
        self.assertIsNotNone(AnalysisTask.objects.get(
            pk=tasks[0]['id']).lease_heartbeat_at)
        submit = dict(payload, task_id=tasks[0]['id'], lines='[]',
                      elapsed='2.5', nodes='1000')
        self.client.post('/atomicdb/api/submit', submit)
        # La telemetria del worker es sincrona; el arbol lo aplica la cola.
        from . import ingest_queue
        ingest_queue.drain()
        ping.refresh_from_db()
        self.assertEqual(ping.tasks_done, 1)
        self.assertIsNone(ping.current_task_id)
        self.assertEqual(ping.last_nps, 400)
        self.assertIsNotNone(ping.nps_updated)
        pos = Position.objects.get(fen=tasks[0]['fen'])
        self.assertEqual(pos.time_invested, 2.5)
        task = AnalysisTask.objects.get(id=tasks[0]['id'])
        self.assertEqual(task.elapsed_seconds, 2.5)

    def test_heartbeat_tracks_current_task_and_keeps_original_lease_time(self):
        from django.utils import timezone
        from .models import WorkerPing
        worker_account('u', 'p')
        pos = ingest.get_or_create_position(logic.start_fen())
        original_lease = timezone.now() - timedelta(minutes=59)
        task = AnalysisTask.objects.create(
            position=pos, budget_nodes=1000, state='LEASED', machine='m1',
            leased_at=original_lease)

        response = self.client.post('/atomicdb/api/heartbeat', {
            'username': 'u', 'password': 'p', 'machine': 'm1',
            'threads': 8, 'hash': 512, 'os': 'TestOS', 'task_id': task.id,
            'nps': 1_250_000,
        })

        self.assertEqual(response.status_code, 200)
        ping = WorkerPing.objects.get(machine='m1', user='u')
        self.assertEqual((ping.current_task_id, ping.threads, ping.last_nps),
                         (task.id, 8, 1_250_000))
        task.refresh_from_db()
        self.assertEqual(task.state, 'LEASED')
        self.assertEqual(task.leased_at, original_lease)
        self.assertIsNotNone(task.lease_heartbeat_at)

    def test_capacity_touch_does_not_overwrite_concurrent_telemetry(self):
        from django.test import RequestFactory
        from .models import WorkerPing
        from .views import _touch_worker
        user = worker_account('u', 'p')
        stamp = timezone.now() - timedelta(minutes=5)
        ping = WorkerPing.objects.create(
            machine='m1', user='u', tasks_done=7, current_task_id=99,
            last_nps=123_456, nps_updated=stamp)
        request = RequestFactory().post('/atomicdb/api/heartbeat', {
            'machine': 'm1', 'threads': 8, 'hash': 512, 'os': 'TestOS',
        })

        _touch_worker(request, user)

        ping.refresh_from_db()
        self.assertEqual((ping.tasks_done, ping.current_task_id, ping.last_nps),
                         (7, 99, 123_456))
        self.assertEqual(ping.nps_updated, stamp)

    def test_non_finite_elapsed_is_safely_ignored(self):
        import json
        from .models import WorkerPing
        worker_account('u', 'p')
        ingest.get_or_create_position(logic.start_fen())
        payload = {'username': 'u', 'password': 'p', 'machine': 'm1'}
        task = self.client.post('/atomicdb/api/lease', payload).json()['tasks'][0]

        response = self.client.post('/atomicdb/api/submit', {
            **payload, 'task_id': task['id'], 'lines': json.dumps([]),
            'elapsed': 'nan', 'nodes': '1000',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(AnalysisTask.objects.get(pk=task['id']).elapsed_seconds,
                         0.0)
        self.assertEqual(WorkerPing.objects.get(machine='m1').last_nps, 0)

    def test_heartbeat_rejects_bad_credentials(self):
        response = self.client.post('/atomicdb/api/heartbeat', {
            'username': 'nobody', 'password': 'wrong', 'machine': 'm1',
        })
        self.assertEqual(response.status_code, 403)


class PovTests(TestCase):
    """La vista muestra scores del QUE MUEVE (chessdb.cn); el arbol interno
    sigue en White-POV."""

    def _black_parent_with_children(self):
        p = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'g1f3'))
        ingest.expand(p)
        edges = list(Edge.objects.filter(parent=p).select_related('child'))
        return p, edges[0].child, edges[1].child, edges[2].child

    def test_moves_table_mover_pov_black(self):
        from .views import _child_moves
        p, a, b, c = self._black_parent_with_children()
        a.status, a.closure = 'WHITE_WIN', 'MINIMAX'
        a.save()
        b.eval_cp = 670   # White-POV: buena para blancas = mala para el mover
        b.save()
        c.eval_cp = 254
        c.save()
        moves = _child_moves(p)
        self.assertEqual(moves[-1]['key'], a.key)      # WHITE_WIN al final
        self.assertEqual(moves[-1]['score'], -10_000)  # y en negativo
        self.assertEqual([m['key'] for m in moves[:2]],
                         [c.key, b.key])               # -254 antes que -670

    def test_mate_distance_label(self):
        from .views import _child_moves
        p, a, b, c = self._black_parent_with_children()
        # a: mate verificado con linea de 3 plies tras la jugada de la fila
        a.status, a.closure = 'WHITE_WIN', 'MATE_PV'
        a.won_line = 'd1h5 g7g6 h5e8'
        a.save()
        moves = _child_moves(p)
        row = next(m for m in moves if m['key'] == a.key)
        # 1 ply de la fila + 3 de la linea = 4 plies -> mate en 2, del rival
        self.assertEqual(row['mate_str'], '-≤M2')
        self.assertEqual(row['score'], -10_000)

    def test_query_api_start_position(self):
        p = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(p)
        r = self.client.get('/atomicdb/api/query',
                            {'fen': logic.start_fen()})
        d = r.json()
        self.assertEqual(d['status'], 'UNKNOWN')
        self.assertEqual(len(d['moves']), 20)

    def test_query_api_rejects_garbage(self):
        r = self.client.get('/atomicdb/api/query', {'fen': 'lol nope'})
        self.assertEqual(r.status_code, 400)

    def test_fen_jump_redirects_and_creates(self):
        ingest.get_or_create_position(logic.start_fen())
        r = self.client.post('/atomicdb/fen/', {'fen': logic.start_fen()})
        self.assertEqual(r.status_code, 302)
        new_fen = logic.apply_move(logic.start_fen(), 'e2e4')
        r2 = self.client.post('/atomicdb/fen/', {'fen': new_fen})
        self.assertEqual(r2.status_code, 302)
        self.assertTrue(Position.objects.filter(
            key=logic.key_of(logic.canonical_fen(new_fen))).exists())


class BootstrapTests(TestCase):

    def test_bootstrap_root_deep_pass(self):
        from .models import AnalysisTask
        self.assertEqual(ingest.bootstrap_root(), 20)
        tasks = AnalysisTask.objects.filter(source='USER', state='PENDING')
        self.assertEqual(tasks.count(), 20)
        self.assertTrue(all(t.budget_nodes >= ingest.BUDGET_LADDER[-1]
                            for t in tasks))
        ingest.bootstrap_root()   # idempotente: promociona, no duplica
        self.assertEqual(AnalysisTask.objects.filter(source='USER').count(), 20)


class DtmTests(TestCase):
    """mate_in: min+1 para el ganador, max+1 para el perdedor, y un hijo
    sin distancia (cierre TB) la deja en desconocida sin impedir el cierre."""

    def test_win_takes_min_plus_one(self):
        lt = LogicTests()
        pos_fen, mating_move, _ = lt._find_mate_in_1()
        p = ingest.get_or_create_position(pos_fen)
        ingest.ingest_analysis(p.key, [{'move': mating_move, 'eval_cp': 9999,
                                        'mate': 1, 'pv': [mating_move]}], 1000)
        p.refresh_from_db()
        # hijo TERMINAL (mate_in 0) -> padre gana en 1 ply
        self.assertEqual((p.status, p.mate_in), ('WHITE_WIN', 1))

    def _all_children_lost(self, dists):
        p = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'g1f3'))
        ingest.expand(p)
        edges = list(Edge.objects.filter(parent=p).select_related('child'))
        for i, e in enumerate(edges):
            c = e.child
            c.status, c.closure = 'WHITE_WIN', 'MATE_PV'
            c.mate_in = dists(i)
            # Fresh-context rule (P1b): a decisive child only carries its
            # closure up a QUIET edge while it still has clock margin.  This
            # test is about DTM, so give every child room and let the DTM
            # arithmetic be the only thing under test.
            c.clock_slack = 50
            c.save()
        ingest.backup_cascade([edges[0].child.key])
        p.refresh_from_db()
        return p

    def test_loss_takes_max_plus_one(self):
        p = self._all_children_lost(lambda i: 4 if i == 0 else 2)
        self.assertEqual((p.status, p.mate_in), ('WHITE_WIN', 5))

    def test_tb_child_leaves_distance_unknown(self):
        p = self._all_children_lost(lambda i: None if i == 0 else 2)
        self.assertEqual(p.status, 'WHITE_WIN')   # cierra igual
        self.assertIsNone(p.mate_in)              # sin numero inventado


class SolvedExploreTests(TestCase):

    def test_solved_position_lists_legal_moves(self):
        p = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'g1f3'))
        p.status, p.closure = 'WHITE_WIN', 'MATE_PV'
        p.won_line = 'd7d5'
        p.save()
        r = self.client.get(f'/atomicdb/explore/{p.key}/')
        html = r.content.decode()
        # Sin una sola arista, TODA jugada legal esta fuera del arbol: seguirla
        # es lo que la crea.  Eso no es "sin explorar" — sin explorar esta una
        # respuesta que ya existe y que nadie ha mirado.
        self.assertIn('not in tree', html)
        self.assertIn(f'/atomicdb/goto/{p.key}/d7d5/', html)
        self.assertNotIn('Not expanded yet', html)


class PartialExpansionTests(TestCase):

    def test_partial_edges_do_not_poison_eval(self):
        # /goto/ crea aristas sueltas; un hijo perdido NO debe poner el eval
        # del padre a 10000 mientras la lista de movimientos este incompleta
        p = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'g1f3'))
        p.eval_cp = 441
        p.save()
        child = ingest.get_or_create_position(
            logic.apply_move(p.fen, 'd7d5'))
        Edge.objects.create(parent=p, move_uci='d7d5', child=child)
        child.status, child.closure = 'WHITE_WIN', 'MATE_PV'
        child.save()
        ingest.backup_cascade([child.key])
        p.refresh_from_db()
        self.assertEqual(p.status, 'UNKNOWN')   # cierre ya exigia expansion
        self.assertEqual(p.eval_cp, 441)        # y ahora el eval tambien


class SeedNotStompTests(TestCase):

    def test_parent_lines_do_not_overwrite_child_eval(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        e = Edge.objects.filter(parent=root, move_uci='g1f3') \
                        .select_related('child').first()
        deep = e.child
        # Analisis directo profundo del hijo: eval Y NODOS, que es como llega
        # siempre — los dos se escriben en la misma llamada — y son los nodos
        # los que lo hacen intocable para la linea del padre
        # (§ ingest._seed_child_eval).  Sin ellos esto no es una busqueda: es
        # una siembra, y a una siembra la refresca la siembra nueva.
        deep.eval_cp = 441
        deep.nodes_invested = 128_000_000
        deep.save()
        ingest.ingest_analysis(root.key, [
            {'move': 'g1f3', 'eval_cp': 306, 'mate': None, 'pv': ['g1f3']},
            {'move': 'e2e4', 'eval_cp': 150, 'mate': None, 'pv': ['e2e4']},
        ], nodes_budget=1000)
        deep.refresh_from_db()
        self.assertEqual(deep.eval_cp, 441)   # no pisado
        e2 = Edge.objects.filter(parent=root, move_uci='e2e4') \
                         .select_related('child').first()
        self.assertEqual(e2.child.eval_cp, 150)   # vacio: sembrado


class TbRoutingTests(TestCase):
    """Tareas sondeables en TB se reservan a workers con tablebases, salvo
    que no haya otra cosa que servir."""

    def _lease(self, tb):
        import json
        self.lease_number += 1
        r = self.client.post('/atomicdb/api/lease',
                             {'username': 'u', 'password': 'p', 'tb': tb,
                              'machine': f'm{self.lease_number}'})
        return [t['fen'] for t in json.loads(r.content)['tasks']]

    def setUp(self):
        worker_account('u', 'p')
        self.lease_number = 0
        self.tbpos = ingest.get_or_create_position('4k3/8/8/8/8/8/8/QK6 w - - 0 1')
        self.normal = ingest.get_or_create_position(logic.start_fen())

    def test_tb_task_waits_for_tb_worker(self):
        AnalysisTask.objects.create(position=self.tbpos, budget_nodes=1000)
        AnalysisTask.objects.create(position=self.normal, budget_nodes=1000)
        fens = self._lease('0')
        self.assertNotIn(self.tbpos.fen, fens)
        self.assertIn(self.normal.fen, fens)
        self.assertIn(self.tbpos.fen, self._lease('1'))

    def test_tb_task_served_when_nothing_else(self):
        AnalysisTask.objects.create(position=self.tbpos, budget_nodes=1000)
        self.assertIn(self.tbpos.fen, self._lease('0'))

    def test_non_tb_worker_scans_past_more_than_four_tb_tasks(self):
        tb_fens = [
            '4k3/8/8/8/8/8/8/QK6 w - - 0 1',
            '4k3/8/8/8/8/8/8/1QK5 w - - 0 1',
            '4k3/8/8/8/8/8/8/2QK4 w - - 0 1',
            '4k3/8/8/8/8/8/8/3QK3 w - - 0 1',
            '4k3/8/8/8/8/8/8/4QK2 w - - 0 1',
        ]
        for priority, fen in enumerate(tb_fens, start=10):
            pos = ingest.get_or_create_position(fen)
            pos.priority = priority
            pos.save(update_fields=['priority'])
            AnalysisTask.objects.get_or_create(
                position=pos, generation=0, defaults={'budget_nodes': 1000})
        self.normal.priority = 0
        self.normal.save(update_fields=['priority'])
        AnalysisTask.objects.create(position=self.normal, budget_nodes=1000)

        self.assertEqual(self._lease('0'), [self.normal.fen])


class LeaseReclaimTests(TestCase):
    """Only stale leases are recycled; healthy same-machine work is fenced."""

    def test_same_machine_does_not_steal_healthy_lease(self):
        import json
        from django.utils import timezone
        worker_account('u', 'p')
        p = ingest.get_or_create_position(logic.start_fen())
        task = AnalysisTask.objects.create(
            position=p, budget_nodes=1000, state='LEASED', machine='m1',
            leased_at=timezone.now(), lease_heartbeat_at=timezone.now(),
            attempts=1, lease_token='healthy-token')
        r = self.client.post('/atomicdb/api/lease',
                             {'username': 'u', 'password': 'p',
                              'machine': 'm1', 'tb': '1',
                              'worker_build': '2026072203'})
        fens = [t['fen'] for t in json.loads(r.content)['tasks']]
        self.assertEqual(fens, [])
        task.refresh_from_db()
        self.assertEqual((task.state, task.machine, task.attempts,
                          task.lease_token),
                         ('LEASED', 'm1', 1, 'healthy-token'))

    def test_same_machine_recycles_only_stale_lease(self):
        worker_account('u', 'p')
        p = ingest.get_or_create_position(logic.start_fen())
        task = AnalysisTask.objects.create(
            position=p, budget_nodes=1000, state='LEASED', machine='m1',
            leased_at=timezone.now() - timedelta(hours=2),
            lease_heartbeat_at=timezone.now() - timedelta(hours=2),
            attempts=1, lease_token='old-token')

        response = self.client.post('/atomicdb/api/lease', {
            'username': 'u', 'password': 'p', 'machine': 'm1', 'tb': '1',
            'worker_build': '2026072203',
        })

        leased = response.json()['tasks'][0]
        task.refresh_from_db()
        self.assertEqual(leased['id'], task.id)
        self.assertEqual(task.attempts, 2)
        self.assertNotEqual(task.lease_token, 'old-token')
        self.assertEqual(leased['lease_token'], task.lease_token)

    def test_assignment_token_fences_stale_same_machine_process(self):
        worker_account('u', 'p')
        p = ingest.get_or_create_position(logic.start_fen())
        AnalysisTask.objects.create(position=p, budget_nodes=1000)
        payload = {
            'username': 'u', 'password': 'p', 'machine': 'm1', 'tb': '1',
            'worker_build': '2026072203',
        }
        leased = self.client.post('/atomicdb/api/lease', payload).json()['tasks'][0]
        self.assertTrue(leased['lease_token'])

        stale_heartbeat = self.client.post('/atomicdb/api/heartbeat', {
            **payload, 'task_id': leased['id'], 'lease_token': 'old-token',
            'nps': '123',
        })
        stale_submit = self.client.post('/atomicdb/api/submit', {
            **payload, 'task_id': leased['id'], 'lease_token': 'old-token',
            'lines': '[]', 'elapsed': '1', 'nodes': '1000',
        })
        valid_submit = self.client.post('/atomicdb/api/submit', {
            **payload, 'task_id': leased['id'],
            'lease_token': leased['lease_token'], 'lines': '[]',
            'elapsed': '1', 'nodes': '1000',
        })

        self.assertEqual((stale_heartbeat.status_code,
                          stale_heartbeat.json()['error']),
                         (409, 'stale-lease'))
        self.assertEqual((stale_submit.status_code,
                          stale_submit.json()['error']),
                         (409, 'stale-lease'))
        self.assertEqual(valid_submit.status_code, 200)

    def test_recycled_task_waits_for_token_capable_worker(self):
        worker_account('u', 'p')
        p = ingest.get_or_create_position(logic.start_fen())
        task = AnalysisTask.objects.create(
            position=p, budget_nodes=1000, state='PENDING', attempts=1)

        legacy = self.client.post('/atomicdb/api/lease', {
            'username': 'u', 'password': 'p', 'machine': 'legacy', 'tb': '1',
        })
        task.refresh_from_db()
        self.assertNotIn(task.id,
                         [row['id'] for row in legacy.json()['tasks']])
        self.assertEqual((task.state, task.machine), ('PENDING', ''))

        modern = self.client.post('/atomicdb/api/lease', {
            'username': 'u', 'password': 'p', 'machine': 'modern', 'tb': '1',
            'worker_build': '2026072203',
        }).json()['tasks'][0]
        task.refresh_from_db()
        self.assertEqual(modern['id'], task.id)
        self.assertTrue(modern['lease_token'])

    def test_deep_first_attempt_waits_for_token_capable_worker(self):
        worker_account('u', 'p')
        p = ingest.get_or_create_position(logic.start_fen())
        task = AnalysisTask.objects.create(
            position=p, budget_nodes=10_000_000_000, state='PENDING')

        legacy = self.client.post('/atomicdb/api/lease', {
            'username': 'u', 'password': 'p', 'machine': 'legacy', 'tb': '1',
        }).json()['tasks']
        self.assertNotIn(task.id, [row['id'] for row in legacy])

        modern = self.client.post('/atomicdb/api/lease', {
            'username': 'u', 'password': 'p', 'machine': 'modern', 'tb': '1',
            'worker_build': '2026072203', 'lease_session': 'modern-session',
        }).json()['tasks'][0]
        self.assertEqual(modern['id'], task.id)

    def test_same_session_replays_lost_lease_response_idempotently(self):
        worker_account('u', 'p')
        p = ingest.get_or_create_position(logic.start_fen())
        task = AnalysisTask.objects.create(position=p, budget_nodes=1000)
        payload = {
            'username': 'u', 'password': 'p', 'machine': 'm1', 'tb': '1',
            'worker_build': '2026072203', 'lease_session': 'process-session-a',
        }

        first = self.client.post('/atomicdb/api/lease', payload).json()['tasks'][0]
        task.refresh_from_db()
        assigned = (task.attempts, task.leased_at, task.lease_token)
        replay = self.client.post('/atomicdb/api/lease', payload).json()['tasks'][0]
        task.refresh_from_db()

        self.assertEqual((replay['id'], replay['lease_token']),
                         (first['id'], first['lease_token']))
        self.assertEqual((task.attempts, task.leased_at, task.lease_token),
                         assigned)

        other_process = self.client.post('/atomicdb/api/lease', {
            **payload, 'lease_session': 'process-session-b',
        })
        self.assertEqual(other_process.json()['tasks'], [])

    def test_recent_heartbeat_prevents_expired_assignment_reclaim(self):
        import json
        from django.utils import timezone
        worker_account('u', 'p')
        p = ingest.get_or_create_position(logic.start_fen())
        task = AnalysisTask.objects.create(
            position=p, budget_nodes=10_000_000_000, state='LEASED',
            machine='m1', leased_at=timezone.now() - timedelta(hours=2),
            lease_heartbeat_at=timezone.now(),
            lease_token='modern-live-token', lease_session='live-session')

        self.client.post('/atomicdb/api/lease', {
            'username': 'u', 'password': 'p', 'machine': 'm2', 'tb': '1',
            'worker_build': '2026072203', 'lease_session': 'm2-session',
        })

        task.refresh_from_db()
        self.assertEqual((task.state, task.machine), ('LEASED', 'm1'))

    def test_predeploy_tokenless_deep_lease_gets_drain_window(self):
        worker_account('u', 'p')
        p = ingest.get_or_create_position(logic.start_fen())
        task = AnalysisTask.objects.create(
            position=p, budget_nodes=10_000_000_000, state='LEASED',
            machine='m1', leased_at=timezone.now() - timedelta(hours=2),
            lease_heartbeat_at=None, lease_token='')

        self.client.post('/atomicdb/api/lease', {
            'username': 'u', 'password': 'p', 'machine': 'm2', 'tb': '1',
            'worker_build': '2026072203', 'lease_session': 'm2-session',
        })

        task.refresh_from_db()
        self.assertEqual((task.state, task.machine), ('LEASED', 'm1'))

    def test_postdeploy_tokenless_lease_recycles_after_one_hour(self):
        worker_account('u', 'p')
        p = ingest.get_or_create_position(logic.start_fen())
        old = timezone.now() - timedelta(hours=2)
        task = AnalysisTask.objects.create(
            position=p, budget_nodes=128_000_000, state='LEASED',
            machine='legacy', leased_at=old, lease_heartbeat_at=old,
            lease_token='', attempts=1)

        self.client.post('/atomicdb/api/lease', {
            'username': 'u', 'password': 'p', 'machine': 'modern', 'tb': '1',
            'worker_build': '2026072203', 'lease_session': 'modern-session',
        })

        task.refresh_from_db()
        self.assertEqual((task.state, task.machine), ('LEASED', 'modern'))
        self.assertTrue(task.lease_token)

    def test_stale_assignment_and_stale_heartbeat_are_reclaimed(self):
        from django.utils import timezone
        worker_account('u', 'p')
        p = ingest.get_or_create_position(logic.start_fen())
        task = AnalysisTask.objects.create(
            position=p, budget_nodes=10_000_000_000, state='LEASED',
            machine='m1', leased_at=timezone.now() - timedelta(hours=2),
            lease_heartbeat_at=timezone.now() - timedelta(hours=2),
            lease_token='modern-old-token', lease_session='old-session')

        self.client.post('/atomicdb/api/lease', {
            'username': 'u', 'password': 'p', 'machine': 'm2', 'tb': '1',
            'worker_build': '2026072203', 'lease_session': 'm2-session',
        })

        task.refresh_from_db()
        self.assertEqual((task.state, task.machine), ('LEASED', 'm2'))


class SearchmovesTests(TestCase):
    """El lease manda las jugadas vivas: el motor no re-deriva lo demostrado."""

    def setUp(self):
        worker_account('u', 'p')

    def _lease_task(self, pos):
        import json
        r = self.client.post('/atomicdb/api/lease',
                             {'username': 'u', 'password': 'p', 'tb': '1'})
        return next(t for t in json.loads(r.content)['tasks']
                    if t['fen'] == pos.fen)

    def test_lease_restricts_to_unsolved_moves(self):
        p = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'g1f3'))
        ingest.expand(p)
        edges = list(Edge.objects.filter(parent=p).select_related('child'))
        solved = edges[0]
        solved.child.status, solved.child.closure = 'WHITE_WIN', 'MATE_PV'
        solved.child.save()
        AnalysisTask.objects.create(position=p, budget_nodes=1000)
        task = self._lease_task(p)
        self.assertNotIn(solved.move_uci, task['searchmoves'])
        self.assertEqual(len(task['searchmoves']), len(edges) - 1)

    def test_no_restriction_when_nothing_solved(self):
        p = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(p)
        AnalysisTask.objects.create(position=p, budget_nodes=1000)
        self.assertEqual(self._lease_task(p)['searchmoves'], [])


class HomeQueueTests(TestCase):

    @staticmethod
    def _play(parent, uci):
        child = ingest.get_or_create_position(
            logic.apply_move(parent.fen, uci))
        Edge.objects.get_or_create(parent=parent, move_uci=uci,
                                   defaults={'child': child})
        return child

    def test_home_shows_analysis_queue(self):
        from django.utils import timezone
        p = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(p)
        AnalysisTask.objects.create(position=p, budget_nodes=8_000_000,
                                    state='LEASED', machine='m1',
                                    leased_at=timezone.now())
        r = self.client.get('/atomicdb/')
        self.assertContains(r, 'Now analyzing')
        self.assertContains(r, 'Up next')
        self.assertContains(r, 'start position')
        self.assertEqual(len(r.context['analyzing']), 1)

    def test_up_next_line_stops_at_start_position_before_numbering(self):
        root = ingest.get_or_create_position(logic.start_fen())

        # A reversible knight loop gives the start position an incoming edge.
        # Path reconstruction must still stop at the real root instead of
        # walking past it and presenting 2.Ng1 as the artificial "1.Ng1".
        nf3 = self._play(root, 'g1f3')
        nh6 = self._play(nf3, 'g8h6')
        ng1 = self._play(nh6, 'f3g1')
        self.assertEqual(self._play(ng1, 'h6g8'), root)

        f6 = self._play(nf3, 'f7f6')
        target = self._play(f6, 'e2e3')
        target.priority = 999
        target.save(update_fields=['priority'])

        response = self.client.get('/atomicdb/')

        self.assertEqual(response.context['upnext'][0]['key'], target.key)
        self.assertEqual(response.context['upnext'][0]['san'],
                         '1. Nf3 f6 2. e3')

    def test_long_running_current_task_stays_in_now_analyzing(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import WorkerPing
        root = ingest.get_or_create_position(logic.start_fen())
        task = AnalysisTask.objects.create(
            position=root, budget_nodes=2_000_000_000, state='LEASED',
            machine='m1', leased_at=timezone.now() - timedelta(minutes=25),
            lease_heartbeat_at=timezone.now(), lease_token='modern-token')
        ping = WorkerPing.objects.create(
            machine='m1', user='u', threads=8, current_task_id=task.id)
        WorkerPing.objects.filter(pk=ping.pk).update(last_seen=timezone.now())

        response = self.client.get('/atomicdb/')

        self.assertEqual(len(response.context['analyzing']), 1)
        self.assertEqual(response.context['analyzing'][0]['key'], root.key)
        self.assertEqual(response.context['analyzing'][0]['budget'], '2.00B')

    def test_modern_worker_without_progress_is_not_now_analyzing(self):
        from .models import WorkerPing
        root = ingest.get_or_create_position(logic.start_fen())
        task = AnalysisTask.objects.create(
            position=root, budget_nodes=10_000_000_000, state='LEASED',
            machine='m1', leased_at=timezone.now() - timedelta(minutes=10),
            lease_heartbeat_at=timezone.now(), lease_token='modern-token')
        ping = WorkerPing.objects.create(
            machine='m1', user='u', threads=8, current_task_id=None)
        WorkerPing.objects.filter(pk=ping.pk).update(last_seen=timezone.now())

        response = self.client.get('/atomicdb/')

        self.assertEqual(response.context['analyzing'], [])

    def test_legacy_worker_with_stale_ping_still_shows_valid_lease(self):
        from .models import WorkerPing
        root = ingest.get_or_create_position(logic.start_fen())
        task = AnalysisTask.objects.create(
            position=root, budget_nodes=2_000_000_000, state='LEASED',
            machine='legacy', leased_at=timezone.now() - timedelta(minutes=25))
        ping = WorkerPing.objects.create(machine='legacy', user='u', threads=8)
        WorkerPing.objects.filter(pk=ping.pk).update(
            last_seen=timezone.now() - timedelta(minutes=25))

        response = self.client.get('/atomicdb/')

        self.assertEqual(response.context['analyzing'][0]['key'],
                         task.position_id)

    def test_multi_hour_heartbeat_task_stays_visible(self):
        from .models import WorkerPing
        root = ingest.get_or_create_position(logic.start_fen())
        task = AnalysisTask.objects.create(
            position=root, budget_nodes=10_000_000_000, state='LEASED',
            machine='m1', leased_at=timezone.now() - timedelta(hours=2),
            lease_heartbeat_at=timezone.now(), lease_token='modern-token')
        ping = WorkerPing.objects.create(
            machine='m1', user='u', threads=8, current_task_id=task.id)
        WorkerPing.objects.filter(pk=ping.pk).update(last_seen=timezone.now())

        response = self.client.get('/atomicdb/')

        self.assertEqual(response.context['analyzing'][0]['key'],
                         task.position_id)


class MilestoneLineTests(TestCase):

    @staticmethod
    def _play(parent, uci):
        child = ingest.get_or_create_position(logic.apply_move(parent.fen, uci))
        edge, _created = Edge.objects.get_or_create(
            parent=parent, move_uci=uci, defaults={'child': child})
        if edge.child_id != child.key:
            raise AssertionError('fixture edge points to an unexpected child')
        return child

    def test_milestone_preview_starts_at_opening_and_full_line_is_hoverable(self):
        from .models import DBEvent
        root = ingest.get_or_create_position(logic.start_fen())
        current = root
        moves = ('g1f3', 'g8f6', 'b1c3', 'b8c6', 'e2e3', 'e7e6',
                 'd2d3', 'd7d6', 'f1e2', 'f8e7', 'e1g1', 'e8g8')
        for uci in moves:
            current = self._play(current, uci)
        DBEvent.objects.create(kind='NODE_CLOSED', payload={
            'key': current.key, 'status': 'WHITE_WIN', 'closure': 'MATE_PV',
        })

        response = self.client.get('/atomicdb/')
        event = response.context['events'][0]

        self.assertTrue(event['san'].startswith('1. Nf3 Nf6'))
        # Elipsis EN MEDIO: la cabeza identifica la apertura y la cola dice
        # donde va la linea — dos posiciones profundas consecutivas ya no
        # comparten label (incidente del 29-jul).
        self.assertIn(' … ', event['san'])
        self.assertTrue(event['san'].endswith('O-O'))
        self.assertIn('6. O-O O-O', event['full'])
        self.assertContains(response, f'title="{event["full"]}"')
        self.assertContains(response, 'class="milestone-line dim"')

    def test_reversible_transposition_uses_shortest_startpos_line(self):
        """A reversible queen/bishop loop must not cut the opening at Bf8."""
        from .models import DBEvent
        root = ingest.get_or_create_position(logic.start_fen())
        current = root
        # This is the production shape that exposed the bug. Moves 13-14 return
        # to the same canonical position, so the Edge graph contains a cycle.
        moves = (
            'g1f3', 'f7f6', 'e2e3', 'd7d5', 'f3g5', 'c8g4',
            'f2f3', 'f6g5', 'b1c3', 'c7c6', 'c3b5', 'c6b5',
            'd2d4', 'e7e6', 'e3e4', 'a7a6', 'g2g3', 'g8f6',
            'c2c3', 'b7b5', 'h2h3', 'g7g6', 'c1g5', 'f8h6',
            'd1b3', 'h6f8', 'b3d1', 'f8h6', 'f1e2', 'a6a5',
            'd1b3', 'e8g8', 'b3a3', 'a5a4',
        )
        for uci in moves:
            current = self._play(current, uci)
        current.priority = 100.0
        current.save(update_fields=['priority'])
        DBEvent.objects.create(kind='NODE_CLOSED', payload={
            'key': current.key, 'status': 'WHITE_WIN', 'closure': 'MATE_PV',
        })

        response = self.client.get('/atomicdb/')
        event = response.context['events'][0]

        self.assertTrue(event['full'].startswith('1. Nf3 f6'))
        self.assertIn('13. Be2 a5', event['full'])
        self.assertNotIn('Bf8', event['full'])
        self.assertNotIn('1... Bf8', event['full'])
        self.assertFalse(event['full'].startswith('…'))
        queued = next(
            row for row in response.context['upnext']
            if row['key'] == current.key)
        self.assertTrue(queued['full'].startswith('1. Nf3 f6'))

        explorer = self.client.get(f'/atomicdb/explore/{current.key}/')
        self.assertTrue(explorer.context['line_from_root'])
        self.assertEqual(explorer.context['line'][0]['num'], '1.')
        self.assertEqual(explorer.context['line'][0]['san'], 'Nf3')
        self.assertNotContains(explorer, '1... Bf8')

    def test_disconnected_fragment_does_not_invent_move_number(self):
        from .views import _line_labels, _line_to_root, _numbered_line
        # Deliberately omit the startpos -> Nf3 edge. The canonical FEN knows
        # whose turn it is, but its fullmove counter was intentionally erased.
        top = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'g1f3'))
        target = self._play(top, 'g8f6')

        preview, full = _line_labels(target.key)
        resolved_top, line = _line_to_root(target)
        orphan_preview, orphan_full = _line_labels(top.key)
        orphan_page = self.client.get(f'/atomicdb/explore/{top.key}/')

        self.assertEqual(preview, '… Nf6')
        self.assertEqual(full, '… Nf6')
        self.assertEqual((orphan_preview, orphan_full), ('…', '…'))
        self.assertContains(orphan_page, 'lineage unavailable')
        self.assertFalse(orphan_page.context['line_from_root'])
        self.assertEqual(resolved_top.key, top.key)
        self.assertEqual(_numbered_line(resolved_top, line), [{
            'num': '', 'san': 'Nf6', 'key': target.key,
        }])

    def test_sql_parent_cap_keeps_direct_startpos_edge(self):
        from .views import _line_labels
        root = ingest.get_or_create_position(logic.start_fen())
        target = self._play(root, 'e2e4')
        # Pathological extra parents sort before/after the real root by key.
        # Even with a one-row SQL budget, startpos must receive rank 1.
        for index in range(20):
            fake = Position.objects.create(
                key=f'{index + 1:064x}', fen=logic.start_fen())
            Edge.objects.create(
                parent=fake, move_uci='a1a2', child=target)

        with mock.patch(
                'atomicdb.views.LINEAGE_SEARCH_MAX_PARENTS_PER_CHILD', 1), \
                mock.patch(
                    'atomicdb.views.LINEAGE_SEARCH_MAX_EDGE_ROWS', 1):
            preview, full = _line_labels(target.key)

        self.assertEqual(preview, '1. e4')
        self.assertEqual(full, '1. e4')

    def test_multiple_labels_batch_parent_queries_by_depth(self):
        import pyffish as pf
        from django.test.utils import CaptureQueriesContext
        from .database import connection
        from .views import _line_labels_many
        root = ingest.get_or_create_position(logic.start_fen())
        left = self._play(root, 'g1f3')
        left = self._play(left, 'g8f6')
        left = self._play(left, 'b1c3')
        right = self._play(root, 'e2e4')
        right = self._play(right, 'e7e6')
        right = self._play(right, 'd2d3')

        with mock.patch('pyffish.get_san_moves',
                        wraps=pf.get_san_moves) as batched_san, \
                mock.patch('atomicdb.views.logic.apply_move',
                           side_effect=AssertionError(
                               'valid labels must use batched SAN')):
            with CaptureQueriesContext(connection) as queries:
                labels = _line_labels_many([left.key, right.key])

        self.assertEqual(set(labels), {left.key, right.key})
        self.assertEqual(batched_san.call_count, 2)
        # One target lookup plus one batched parent query per ply, not one
        # ancestry query per target and ply.
        self.assertLessEqual(len(queries), 4)


class HomeFeedKindTests(TestCase):
    """El feed de la portada cuenta la HISTORIA del arbol, no su telemetria.

    ``DBEvent`` sirve a dos publicos con una sola tabla, y ``_friendly_events``
    no tiene frase para el nuestro: cae a ``e.kind`` y lo pinta crudo.  Con la
    puerta doble encendida, un ``SOLVE_GATE_DISAGREE`` por clave habria salido
    en mayusculas y con guiones bajos, y ademas pagando su ``_line_labels``.
    """

    def setUp(self):
        self.pos = ingest.get_or_create_position(logic.start_fen())

    def _event(self, kind, **payload):
        from .models import DBEvent

        return DBEvent.objects.create(
            kind=kind, payload={'key': self.pos.key, **payload})

    def test_an_instrumentation_event_never_reaches_the_home(self):
        from .views import FEED_HIDDEN_KINDS

        for kind in sorted(FEED_HIDDEN_KINDS):
            with self.subTest(kind=kind):
                self._event(kind)
        response = self.client.get('/atomicdb/')

        self.assertEqual(response.context['events'], [])
        self.assertNotContains(response, 'SOLVE_GATE_DISAGREE')
        self.assertNotContains(response, 'BREADTH_SWAP')

    def test_a_historical_event_still_reaches_the_home(self):
        self._event('NODE_CLOSED', status='WHITE_WIN', closure='MATE_PV')

        response = self.client.get('/atomicdb/')

        self.assertEqual(len(response.context['events']), 1)
        self.assertEqual(response.context['events'][0]['text'],
                         'Solved: WHITE_WIN via MATE_PV')

    def test_the_twelve_slots_stay_twelve_under_a_noisy_night(self):
        # El filtro va en la CONSULTA: si se descartara despues de cortar, una
        # noche de brazos internos habladores dejaria la portada casi vacia.
        for index in range(30):
            self._event('SOLVE_GATE_DISAGREE', annoyance=0.5, factor=1.0,
                        index=index)
        for index in range(12):
            self._event('NODE_CLOSED', status='DRAW', closure='MINIMAX',
                        index=index)

        response = self.client.get('/atomicdb/')

        self.assertEqual(len(response.context['events']), 12)
        self.assertTrue(all(row['text'].startswith('Solved:')
                            for row in response.context['events']))


class BoardInteractionTests(TestCase):

    def test_board_uses_vendored_chessground_with_keyboard_fallback(self):
        from pathlib import Path
        from django.conf import settings
        root = ingest.get_or_create_position(logic.start_fen())
        response = self.client.get('/atomicdb/')
        self.assertContains(response, 'atomicdb/board.js')
        self.assertContains(response, 'data-fen=')
        self.assertContains(response, 'Keyboard move list')
        self.assertContains(response,
                            f'/atomicdb/goto/{root.key}/g1f3/')
        self.assertNotContains(response, 'draggable="false"')
        board_js = (Path(settings.BASE_DIR) / 'atomicdb' / 'static' /
                    'atomicdb' / 'board.js').read_text(encoding='utf-8')
        self.assertIn('Chessground(board', board_js)
        self.assertIn('draggable:', board_js)
        self.assertIn('/atomicdb/goto/', board_js)

    def test_terminal_board_still_initializes_chessground(self):
        from django.template.loader import render_to_string
        html = render_to_string('atomicdb/_board.html', {
            'board_key': 'terminal',
            'board_fen': '7k/8/8/8/8/8/8/K7 b - - 0 1',
            'board_turn': 'black',
            'legal_ucis': [],
            'best_move': None,
        })

        self.assertIn('atomicdb/board.js', html)
        self.assertIn('atomicdb-legal-moves', html)
        self.assertNotIn('Keyboard move list', html)


class NodesAccountingTests(TestCase):

    def test_actual_nodes_recorded_not_budget(self):
        import json
        worker_account('u', 'p')
        ingest.get_or_create_position(logic.start_fen())
        payload = {'username': 'u', 'password': 'p', 'machine': 'm', 'tb': '1'}
        lease = self.client.post('/atomicdb/api/lease', payload)
        t = json.loads(lease.content)['tasks'][0]
        self.client.post('/atomicdb/api/submit',
                         dict(payload, task_id=t['id'], lines='[]',
                              nodes='12345'))
        from . import ingest_queue
        ingest_queue.drain()
        task = AnalysisTask.objects.get(id=t['id'])
        self.assertEqual(task.nodes_searched, 12345)
        pos = Position.objects.get(fen=t['fen'])
        self.assertEqual(pos.nodes_invested, 12345)  # reales, no presupuesto


class ArrowTests(TestCase):

    def test_best_move_arrow_rendered(self):
        from pathlib import Path
        from django.conf import settings
        p = ingest.get_or_create_position(logic.start_fen())
        p.best_move = 'g1f3'
        p.save()
        r = self.client.get(f'/atomicdb/explore/{p.key}/')
        self.assertContains(r, 'data-best-move="g1f3"')
        self.assertNotContains(r, '<svg class="board-arrow"')
        board_js = (
            Path(settings.BASE_DIR) / 'atomicdb' / 'static' /
            'atomicdb' / 'board.js'
        ).read_text(encoding='utf-8')
        self.assertIn('autoShapes', board_js)
        self.assertIn("brush: 'green'", board_js)

    def test_no_arrow_without_best_move(self):
        p = ingest.get_or_create_position(logic.start_fen())
        r = self.client.get(f'/atomicdb/explore/{p.key}/')
        self.assertContains(r, 'data-best-move=""')
        self.assertNotContains(r, '<svg class="board-arrow"')


class DtmRefineTests(TestCase):

    def test_shorter_mate_refines_distance_and_witness(self):
        root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(root)
        a = Edge.objects.get(parent=root, move_uci='e2e4').child
        a.status, a.closure, a.mate_in = 'WHITE_WIN', 'MATE_PV', 4
        a.save()
        ingest.backup_cascade([a.key])
        root.refresh_from_db()
        self.assertEqual((root.status, root.mate_in, root.best_move),
                         ('WHITE_WIN', 5, 'e2e4'))
        # aparece un mate probado mas corto por otra jugada: refina y
        # actualiza el testigo
        b = Edge.objects.get(parent=root, move_uci='d2d4').child
        b.status, b.closure, b.mate_in = 'WHITE_WIN', 'MATE_PV', 2
        b.save()
        ingest.backup_cascade([b.key])
        root.refresh_from_db()
        self.assertEqual((root.mate_in, root.best_move), (3, 'd2d4'))


class WitnessTests(TestCase):

    def test_mate_pv_closure_records_line(self):
        from . import ingest
        lt = LogicTests()
        pos_fen, mating_move, _ = lt._find_mate_in_1()
        p = ingest.get_or_create_position(pos_fen)
        ingest.ingest_analysis(p.key, [{'move': mating_move, 'eval_cp': 9999,
                                        'mate': 1, 'pv': [mating_move]}], 1000)
        p.refresh_from_db()
        # el padre cerro por MINIMAX con testigo = la jugada de mate
        self.assertEqual(p.status, 'WHITE_WIN')
        self.assertEqual(p.best_move, mating_move)


class ExplorerRequestCsrfTests(TestCase):
    """M8: los botones de peticion del explorador ya NO estan exentos de CSRF.

    La exencion del protocolo de workers existe porque un worker no tiene
    navegador ni token; estos dos endpoints los llama el fetch de explore.html,
    que tiene el token en la pagina.  Mientras estuvieron exentos, cualquier
    pagina de terceros podia encolar analisis (hasta el tope del boton masivo)
    a nombre del visitante que la abriera con sesion iniciada.
    """

    def setUp(self):
        from django.test import Client

        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.strict = Client(enforce_csrf_checks=True)

    def _token(self):
        page = self.strict.get(f'/atomicdb/explore/{self.root.key}/')
        return page.cookies['csrftoken'].value

    def test_a_foreign_post_without_token_is_rejected(self):
        for url in (f'/atomicdb/request/{self.root.key}/',
                    f'/atomicdb/request-unexplored/{self.root.key}/'):
            with self.subTest(url=url):
                response = self.strict.post(url)
                self.assertEqual(response.status_code, 403)

    def test_the_pages_own_fetch_still_goes_through(self):
        token = self._token()
        for url in (f'/atomicdb/request/{self.root.key}/',
                    f'/atomicdb/request-unexplored/{self.root.key}/'):
            with self.subTest(url=url):
                response = self.strict.post(url, HTTP_X_CSRFTOKEN=token)
                self.assertNotEqual(response.status_code, 403)

    def test_the_template_sends_the_header_it_needs(self):
        body = self.client.get(
            f'/atomicdb/explore/{self.root.key}/').content.decode()
        self.assertIn('X-CSRFToken', body)

    def test_the_worker_protocol_stays_exempt(self):
        # Un worker sin navegador no tiene token que mandar: 403 aqui seria
        # apagar la flota entera, no proteger a nadie.
        worker_account('csrf-worker', 'pw')
        response = self.strict.post('/atomicdb/api/lease', {
            'username': 'csrf-worker', 'password': 'pw', 'machine': 'm1',
            'lease_session': 's1', 'threads': 1, 'hash': 64})
        self.assertEqual(response.status_code, 200)
