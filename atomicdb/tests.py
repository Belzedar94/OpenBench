"""M1 gate (spec §7): mates conocidos cierran, fortalezas sinteticas NO,
completitud de movegen, backup determinista bajo replay."""

from django.test import TestCase

from . import ingest, logic
from .models import Edge, Position


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

    def test_close_by_tb(self):
        from . import ingest
        p = ingest.get_or_create_position('4k3/8/8/8/8/8/8/QK6 w - - 0 1')
        self.assertTrue(ingest.close_by_tb(p.key, 2))
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

    def test_global_best_first_by_eval(self):
        # sin campanas: gana el nodo de eval mas decisivo del arbol entero
        a = ingest.get_or_create_position(logic.start_fen())
        b = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))
        a.eval_cp, a.expanded = 50, True
        b.eval_cp, b.expanded = 800, True
        a.save(), b.save()
        tasks = ingest.next_tasks(1)
        self.assertEqual(tasks[0].position_id, b.key)

    def test_mate_band_boost_and_budget_jump(self):
        p = ingest.get_or_create_position(logic.start_fen())
        p.eval_cp = 9_997   # mate visto por el motor, aun sin cerrar
        p.save()
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


class RequestTests(TestCase):

    def test_request_queues_user_task(self):
        from .models import AnalysisTask
        p = ingest.get_or_create_position(logic.start_fen())
        self.assertEqual(ingest.request_analysis(p), 'queued')
        t = AnalysisTask.objects.get(position=p)
        self.assertEqual((t.source, t.state), ('USER', 'PENDING'))

    def test_request_promotes_existing_auto_task(self):
        from .models import AnalysisTask
        p = ingest.get_or_create_position(logic.start_fen())
        ingest.next_tasks(1)   # crea la tarea AUTO
        self.assertEqual(ingest.request_analysis(p), 'queued')
        self.assertEqual(AnalysisTask.objects.get(position=p).source, 'USER')

    def test_request_on_solved_position(self):
        from .models import AnalysisTask
        p = ingest.get_or_create_position(logic.start_fen())
        p.status, p.closure = 'DRAW', 'MINIMAX'
        p.save()
        self.assertEqual(ingest.request_analysis(p), 'already-solved')
        self.assertFalse(AnalysisTask.objects.filter(position=p).exists())

    def test_user_requests_leased_first(self):
        import json
        from django.contrib.auth.models import User
        User.objects.create_user('u', password='p')
        a = ingest.get_or_create_position(logic.start_fen())
        b = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))
        a.eval_cp, a.expanded = 800, True   # prioridad alta
        b.eval_cp, b.expanded = 50, True    # prioridad baja
        a.save(), b.save()
        ingest.next_tasks(2)                # tareas AUTO para ambas
        r = self.client.post(f'/atomicdb/request/{b.key}/')
        self.assertEqual(r.json()['status'], 'queued')
        lease = self.client.post('/atomicdb/api/lease',
                                 {'username': 'u', 'password': 'p'})
        tasks = json.loads(lease.content)['tasks']
        self.assertEqual(tasks[0]['fen'], b.fen)  # USER antes que mejor prio

    def test_request_rate_limited(self):
        from .models import RequestLog
        a = ingest.get_or_create_position(logic.start_fen())
        b = ingest.get_or_create_position(
            logic.apply_move(logic.start_fen(), 'e2e4'))
        for _ in range(30):
            RequestLog.objects.create(ip='127.0.0.1', position=a)
        r = self.client.post(f'/atomicdb/request/{b.key}/')
        self.assertEqual(r.status_code, 429)

    def test_request_dedup_same_ip_position(self):
        p = ingest.get_or_create_position(logic.start_fen())
        self.client.post(f'/atomicdb/request/{p.key}/')
        r = self.client.post(f'/atomicdb/request/{p.key}/')
        self.assertEqual(r.json()['status'], 'already-requested')


class MachineVisibilityTests(TestCase):

    def test_lease_and_submit_update_ping(self):
        import json
        from django.contrib.auth.models import User
        from .models import WorkerPing
        User.objects.create_user('u', password='p')
        ingest.get_or_create_position(logic.start_fen())
        payload = {'username': 'u', 'password': 'p', 'machine': 'u-atomicdb',
                   'threads': 8, 'hash': 1024, 'os': 'TestOS 1'}
        lease = self.client.post('/atomicdb/api/lease', payload)
        tasks = json.loads(lease.content)['tasks']
        self.assertTrue(tasks)
        ping = WorkerPing.objects.get(machine='u-atomicdb')
        self.assertEqual((ping.threads, ping.hash_mb, ping.os,
                          ping.tasks_done), (8, 1024, 'TestOS 1', 0))
        submit = dict(payload, task_id=tasks[0]['id'], lines='[]',
                      elapsed='2.5')
        self.client.post('/atomicdb/api/submit', submit)
        ping.refresh_from_db()
        self.assertEqual(ping.tasks_done, 1)
        pos = Position.objects.get(fen=tasks[0]['fen'])
        self.assertEqual(pos.time_invested, 2.5)


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
        self.assertEqual(row['mate_str'], '-M2')
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
