"""La distancia de mate de un nodo cerrado se recalcula, no solo se acorta.

LO QUE REPORTO WOLFRAM.  En la base viva, la posicion tras 1.Nf3 d5 estaba
cerrada ``WHITE_WIN MINIMAX mate_in=3`` (plies) y su UNICO hijo ganador, f3e5,
tenia ``mate_in=4``.  La consistencia exige padre = 1 + 4 = 5, asi que el
explorador — que pinta ``≤M{(plies+1)//2}`` — enseñaba "≤M2" donde lo que sus
propios hijos sostienen es M3.  El nodo Ne5 llevaba una cota de MOTOR con
horizonte (``proof='ENGINE'``, PV ``f7f6 e5d7 e7e6 d7f8``): la defensa 2...Kd7
sobrevive al patron corto y ni siquiera existe como arista.

EL MECANISMO.  El refresco retroactivo de ``backup_cascade`` tenia dos bloques
gemelos que no lo eran: el AND recalculaba exacto (``new_mate != pos.mate_in``)
y el OR solo se dejaba ACORTAR (``1 + hijo < pos.mate_in``).  Cuando un hijo
corregia su distancia HACIA ARRIBA — porque la certificacion alargo su linea, o
porque el hijo que la sostenia se perdio — el ancestro OR se quedaba con la
cota vieja envenenada para siempre: ninguna via del sistema podia volver a
subir ese numero.

Estos tests fijan la regla nueva, que vive en un solo sitio
(``ingest.mate_distance_refresh``) y la comparten la cascada y el backfill:
el OR recalcula ``min(testigo propio, 1 + mejor hijo ganador)`` en las dos
direcciones, el AND sigue exactamente como estaba, y un testigo propio
(``MATE_PV``) sigue siendo un techo que ningun hijo puede rebasar.
"""

from io import StringIO

from django.core.management import call_command

from . import ingest, logic
from .models import DBEvent, Edge, Position
from .testing import TestCase


def _position(fen, **fields):
    """Fila con ese estado exacto.  ``update_or_create`` porque la posicion
    inicial ya existe: la trae la campaña de prueba por defecto."""
    fen = logic.canonical_fen(fen)
    pos, _ = Position.objects.update_or_create(
        key=logic.key_of(fen), defaults={'fen': fen, **fields})
    return pos


def _after(*moves):
    """FEN canonica tras jugar esas jugadas desde la inicial."""
    fen = logic.start_fen()
    for uci in moves:
        fen = logic.apply_move(fen, uci)
    return fen


class ReportedShapeTests(TestCase):
    """La forma EXACTA del caso: 1.Nf3 d5, padre a 3 plies, hijo a 4."""

    def _reported_graph(self, child_mate_in=4):
        """Padre OR cerrado con distancia rancia + su unico hijo ganador.

        FENs reales (jugadas reales desde la inicial); lo sintetico es solo el
        estado de cierre, que es lo que la base viva tenia escrito.
        """
        parent = _position(_after('g1f3', 'd7d5'))
        ingest.expand(parent)          # lista de jugadas completa, como en vivo
        parent.status = 'WHITE_WIN'
        parent.closure = 'MINIMAX'
        parent.proof = 'ENGINE'
        parent.mate_in = 3             # la cota rancia que nadie podia subir
        parent.best_move = 'f3e5'
        parent.clock_slack = 90
        parent.save()
        edge = Edge.objects.select_related('child').get(parent=parent,
                                                       move_uci='f3e5')
        child = edge.child
        child.status = 'WHITE_WIN'
        child.closure = 'MATE_PV'
        child.proof = 'ENGINE'         # cota de motor, no certificado
        child.won_line = 'f7f6 e5d7 e7e6 d7f8'
        child.mate_in = child_mate_in
        child.best_move = 'f7f6'
        child.clock_slack = 90
        child.save()
        return parent, child

    def test_the_parent_follows_a_child_that_corrected_upwards(self):
        parent, child = self._reported_graph()

        ingest.backup_cascade([child.key])

        parent.refresh_from_db()
        self.assertEqual(parent.mate_in, 5)          # 1 + 4, no el 3 rancio
        self.assertEqual(parent.best_move, 'f3e5')
        # Lo que ve el visitante: ``≤M{(plies+1)//2}`` pasa de M2 a M3.
        self.assertEqual((parent.mate_in + 1) // 2, 3)

    def test_the_parent_still_shortens_when_a_child_improves(self):
        """La mejora retroactiva de siempre no se pierde por recalcular."""
        parent, child = self._reported_graph(child_mate_in=1)

        ingest.backup_cascade([child.key])

        parent.refresh_from_db()
        self.assertEqual(parent.mate_in, 2)
        self.assertEqual(parent.best_move, 'f3e5')

    def test_the_backfill_repairs_the_row_already_written(self):
        """El mecanismo arreglado no toca lo ya escrito; esto si."""
        parent, _child = self._reported_graph()
        out = StringIO()

        call_command('backfill_mate_distance', stdout=out, stderr=out)

        parent.refresh_from_db()
        self.assertEqual(parent.mate_in, 5)
        report = out.getvalue()
        self.assertIn('raised=1', report)
        self.assertIn('written=1', report)
        self.assertIn('fixed_point=True', report)
        self.assertTrue(
            DBEvent.objects.filter(kind='MATE_DISTANCE_BACKFILL').exists())

        again = StringIO()
        call_command('backfill_mate_distance', stdout=again, stderr=again)

        self.assertIn('written=0', again.getvalue())   # idempotente

    def test_the_backfill_dry_run_writes_nothing(self):
        parent, _child = self._reported_graph()
        out = StringIO()

        call_command('backfill_mate_distance', '--dry-run', stdout=out,
                     stderr=out)

        parent.refresh_from_db()
        self.assertEqual(parent.mate_in, 3)          # intacto
        self.assertIn('raised=1', out.getvalue())
        self.assertIn('dry_run=True', out.getvalue())
        self.assertFalse(
            DBEvent.objects.filter(kind='MATE_DISTANCE_BACKFILL').exists())


class BackfillCycleTests(TestCase):
    """1.Nf3 Nf6 2.Ng1 Ng8 ES la posicion inicial: el DAG cierra ciclos.

    Un ciclo cuyos nodos se sostienen SOLO unos a otros no tiene distancia
    finita, y una recurrencia exacta lo canta subiendo un ply por pasada para
    siempre.  La base viva no deberia tener uno (el primer cierre de un ciclo
    se apoyo en algo de fuera), pero el backfill existe justamente porque la
    base no es consistente: la contencion es congelar, no inflar.
    """

    def test_an_ungrounded_cycle_is_frozen_instead_of_inflated(self):
        cycle = [('g1f3', logic.start_fen())]
        for uci in ('g8f6', 'f3g1', 'f6g8'):
            cycle.append((uci, logic.apply_move(cycle[-1][1], cycle[-1][0])))
        self.assertEqual(logic.key_of(logic.apply_move(cycle[-1][1], 'f6g8')),
                         logic.key_of(logic.start_fen()))
        nodes = []
        for uci, fen in cycle:
            node = _position(fen, status='WHITE_WIN', closure='MINIMAX',
                             proof='ENGINE', mate_in=10, best_move=uci,
                             expanded=True, clock_slack=90)
            nodes.append((node, uci))
        for node, uci in nodes:
            child_fen = logic.apply_move(node.fen, uci)
            Edge.objects.get_or_create(
                parent=node, move_uci=uci,
                defaults={'child': Position.objects.get(
                    key=logic.key_of(child_fen))})
        out = StringIO()

        call_command('backfill_mate_distance', stdout=out, stderr=out)

        report = out.getvalue()
        self.assertIn('frozen=4', report)
        self.assertIn('written=0', report)
        for node, _uci in nodes:
            node.refresh_from_db()
            self.assertEqual(node.mate_in, 10)     # ni una fila empeorada


class AndMirrorTests(TestCase):
    """El espejo AND ya era correcto: regresion, para que siga siendolo."""

    def _all_children_lost(self, first_child_mate_in):
        """Nodo AND (negras al turno, todas las respuestas pierden)."""
        parent = _position(_after('g1f3'))
        ingest.expand(parent)
        parent.status = 'WHITE_WIN'
        parent.closure = 'MINIMAX'
        parent.proof = 'ENGINE'
        parent.mate_in = 3
        parent.clock_slack = 90
        parent.save()
        edges = list(Edge.objects.filter(parent=parent).select_related('child'))
        for index, edge in enumerate(edges):
            child = edge.child
            child.status = 'WHITE_WIN'
            child.closure = 'MATE_PV'
            child.proof = 'ENGINE'
            child.mate_in = first_child_mate_in if index == 0 else 2
            child.clock_slack = 90
            child.save()
        return parent, edges[0].child

    def test_the_and_parent_recomputes_exactly_upwards(self):
        parent, child = self._all_children_lost(8)

        ingest.backup_cascade([child.key])

        parent.refresh_from_db()
        self.assertEqual(parent.mate_in, 9)          # 1 + max, hacia arriba

    def test_the_and_parent_recomputes_exactly_downwards(self):
        parent, child = self._all_children_lost(1)

        ingest.backup_cascade([child.key])

        parent.refresh_from_db()
        self.assertEqual(parent.mate_in, 3)          # 1 + max(1, 2)


class OwnWitnessTests(TestCase):
    """Un cierre con testigo propio no lo alarga un hijo; solo lo acorta."""

    def _witnessed_parent(self, child_mate_in, own_line='f3e5 d8d6 e5f7',
                          via='e2e4'):
        """Nodo con testigo propio de 3 plies + un hijo ganador por OTRA via.

        La via del hijo es deliberadamente distinta de la primera jugada del
        testigo: un hijo EN la linea guardada arrastra al padre cuando se
        revoca (``_witness_runs_through``), y lo que se mide aqui es la
        aritmetica de la distancia, no esa cascada.
        """
        parent = _position(_after('g1f3', 'd7d5'))
        ingest.expand(parent)
        parent.status = 'WHITE_WIN'
        parent.closure = 'MATE_PV'
        parent.proof = 'ENGINE'
        parent.won_line = own_line
        parent.mate_in = len(own_line.split())
        parent.best_move = own_line.split()[0]
        parent.clock_slack = 90
        parent.save()
        edge = Edge.objects.select_related('child').get(parent=parent,
                                                       move_uci=via)
        child = edge.child
        child.status = 'WHITE_WIN'
        child.closure = 'MATE_PV'
        child.proof = 'ENGINE'
        child.mate_in = child_mate_in
        child.clock_slack = 90
        child.save()
        return parent, child

    def test_a_longer_child_cannot_stretch_the_stored_witness(self):
        """El testigo propio es un techo: retirarlo es de la certificacion."""
        parent, child = self._witnessed_parent(child_mate_in=20)

        ingest.backup_cascade([child.key])

        parent.refresh_from_db()
        self.assertEqual(parent.mate_in, 3)          # su propia linea
        self.assertEqual(parent.best_move, 'f3e5')

    def test_a_shorter_child_still_shortens_it(self):
        parent, child = self._witnessed_parent(child_mate_in=1)

        ingest.backup_cascade([child.key])

        parent.refresh_from_db()
        self.assertEqual(parent.mate_in, 2)
        self.assertEqual(parent.best_move, 'e2e4')   # el testigo mas corto

    def test_the_witness_comes_back_when_the_short_child_goes_away(self):
        """Y aqui esta la simetria que faltaba, en el otro sentido."""
        parent, child = self._witnessed_parent(child_mate_in=1)
        ingest.backup_cascade([child.key])
        parent.refresh_from_db()
        self.assertEqual(parent.mate_in, 2)

        ingest.revoke_closure(child.key, reason='test', requeue=False)

        parent.refresh_from_db()
        self.assertEqual(parent.status, 'WHITE_WIN')  # su linea sigue en pie
        self.assertEqual(parent.mate_in, 3)           # vuelve a su testigo
        self.assertEqual(parent.best_move, 'f3e5')


class UnnameableDistanceTests(TestCase):
    """Un numero que ya no sostiene ningun hijo es peor que ningun numero."""

    def test_a_minimax_or_without_a_measured_winner_loses_its_number(self):
        parent = _position(_after('g1f3', 'd7d5'))
        ingest.expand(parent)
        parent.status = 'WHITE_WIN'
        parent.closure = 'MINIMAX'
        parent.proof = 'ENGINE'
        parent.mate_in = 3
        parent.best_move = 'f3e5'
        parent.clock_slack = 90
        parent.save()
        edge = Edge.objects.select_related('child').get(parent=parent,
                                                       move_uci='f3e5')
        child = edge.child
        # Cierre de tablebase: gana, pero sin distancia que ofrecer.
        child.status = 'WHITE_WIN'
        child.closure = 'TB'
        child.mate_in = None
        child.clock_slack = 90
        child.save()

        ingest.backup_cascade([child.key])

        parent.refresh_from_db()
        self.assertEqual(parent.status, 'WHITE_WIN')  # el cierre no se toca
        self.assertIsNone(parent.mate_in)             # el numero rancio, si


# Mate atomico forzado y real: 1.Qg6 y las negras no tienen defensa.  Es el
# mismo fixture que usa ``test_won_line_chain``, y el prover de verdad es quien
# dice que lo es.
FORCED_MATE_FEN = '7k/6p1/8/8/8/8/8/3QK3 w - - 0 1'


_FIXTURE = {}


def _forced_line():
    """Linea de mate REAL del fixture, con un primer movimiento que lo fuerza.

    La busca el prover de verdad, no yo.  Cacheada: es calculo puro y las
    pruebas exhaustivas se pagan en segundos.
    """
    if 'line' in _FIXTURE:
        return _FIXTURE['line']
    fen = logic.canonical_fen(FORCED_MATE_FEN)
    for first in logic.legal_moves(fen):
        child = logic.apply_move(fen, first)
        if logic.terminal_status(child):
            continue
        if logic.prove_forced_mate(child, True, 2,
                                   budget_positions=200_000) != 'PROVEN':
            continue
        for reply in logic.legal_moves(child):
            after = logic.apply_move(child, reply)
            for third in logic.legal_moves(after):
                if logic.verify_mate_pv(fen, [first, reply, third], True):
                    _FIXTURE['line'] = [first, reply, third]
                    return _FIXTURE['line']
    raise AssertionError('el fixture ya no tiene un mate forzado en 3 plies')


def _proven_proofs(lines):
    """``mate_proofs`` del prover real, SIN el reloj online de 5 s.

    ``ingest_analysis`` acepta los proofs ya preparados — es como los pasa la
    cola de ingesta — y asi el test mide lo que hace la ingesta con un
    veredicto ``PROVEN``, no si esta caja cargada llega a producirlo en cinco
    segundos.
    """
    if 'proofs' not in _FIXTURE:
        _FIXTURE['proofs'] = ingest.prepare_mate_proofs(
            logic.canonical_fen(FORCED_MATE_FEN), lines, deadline_seconds=None)
        assert _FIXTURE['proofs'][0][2] == 'PROVEN', _FIXTURE['proofs']
    return _FIXTURE['proofs']


class CertifiedLineAdoptionTests(TestCase):
    """Que hace la ingesta con OTRA linea de mate para un hijo YA cerrado.

    Decision (documentada en ``ingest._adopt_certified_line``): ``mate_in`` es
    la LONGITUD DEL TESTIGO guardado, no una cota flotante.  Dos testigos sin
    certificar no se arbitran ni por el minimo (perpetua las cotas de horizonte
    — es el bug de Ne5 un nivel mas abajo) ni por el ultimo (la evidencia llega
    por la PV del PADRE, y en un DAG el mismo hijo cuelga de padres analizados
    a presupuestos distintos: seria ping-pong).  Un testigo sin certificar solo
    lo retira quien puede refutarlo, y esa maquinaria ya existe.

    Lo que SI es mejor evidencia es un veredicto ``PROVEN``, y hasta ahora se
    tiraba a la basura cuando el hijo ya estaba cerrado.
    """

    def _graph(self):
        line = _forced_line()
        parent = ingest.get_or_create_position(
            logic.canonical_fen(FORCED_MATE_FEN))
        ingest.expand(parent)
        edge = Edge.objects.select_related('child').get(parent=parent,
                                                        move_uci=line[0])
        child = edge.child
        # Cierre viejo del hijo: un testigo de motor MAS CORTO que la realidad.
        stale = logic.legal_moves(child.fen)[0]
        child.status = 'WHITE_WIN'
        child.closure = 'MATE_PV'
        child.proof = 'ENGINE'
        child.won_line = stale
        child.mate_in = 1
        child.best_move = stale
        child.clock_slack = 99
        child.save()
        lines = [{'move': line[0], 'eval_cp': 9999, 'mate': 2, 'pv': line}]
        return parent, child, line, lines

    def test_a_certificate_replaces_the_uncertified_witness(self):
        parent, child, line, lines = self._graph()

        summary = ingest.ingest_analysis(parent.key, lines, 1_000_000,
                                         mate_proofs=_proven_proofs(lines))

        child.refresh_from_db()
        self.assertEqual(summary.get('certified'), 1)
        self.assertEqual(child.proof, 'ANDOR')
        self.assertEqual(child.mate_in, 2)               # 1 -> 2, hacia ARRIBA
        self.assertEqual(child.won_line, ' '.join(line[1:]))
        self.assertEqual(child.best_move, line[1])
        self.assertTrue(DBEvent.objects.filter(
            kind='MATE_WITNESS_CERTIFIED', payload__was_mate_in=1).exists())

    def test_the_corrected_child_carries_the_parent_with_it(self):
        """La correccion sube: es justo lo que el bug del OR impedia."""
        parent, _child, _line, lines = self._graph()

        ingest.ingest_analysis(parent.key, lines, 1_000_000,
                               mate_proofs=_proven_proofs(lines))

        parent.refresh_from_db()
        self.assertEqual(parent.status, 'WHITE_WIN')
        self.assertEqual(parent.mate_in, 3)              # 1 + 2

    def test_an_uncertified_longer_line_leaves_the_witness_alone(self):
        """Sin certificado no hay correccion: esa deuda es de la flota."""
        parent, child, line, lines = self._graph()
        stale_line, stale_mate = child.won_line, child.mate_in

        summary = ingest.ingest_analysis(
            parent.key, lines, 1_000_000,
            mate_proofs={0: (True, line[1:], 'INCONCLUSIVE', None)})

        child.refresh_from_db()
        self.assertNotIn('certified', summary)
        self.assertEqual((child.won_line, child.mate_in),
                         (stale_line, stale_mate))
        self.assertEqual(child.proof, 'ENGINE')

    def test_a_certificate_does_not_touch_an_already_certified_witness(self):
        parent, child, line, lines = self._graph()
        child.proof = 'ANDOR'
        child.save(update_fields=['proof'])

        summary = ingest.ingest_analysis(parent.key, lines, 1_000_000,
                                         mate_proofs=_proven_proofs(lines))

        child.refresh_from_db()
        self.assertNotIn('certified', summary)
        self.assertEqual(child.mate_in, 1)
