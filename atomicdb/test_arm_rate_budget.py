"""El techo de gasto POR HORA de los brazos de compra del walker.

EL CASO, medido el 15-ago-2026.  Los brazos del walker se acotaban contando
lo PENDIENTE — 16 cascadas, 50 de calidad — y un cupo sobre lo pendiente solo
aprieta cuando la flota va por detras.  Con la flota sirviendo al instante la
cola vuelve a cero entre un resultado y el siguiente, el cupo no llega a tocar
nunca, y cada analisis que aterriza compra el suyo: 4.551 compras de cascada
en una hora contra un cupo de 16.  El tope fallaba ABIERTO justo el dia que la
flota era grande, que es cuando el gasto importa.

Lo que clavan estos tests, y por que cada cosa:

* que un brazo que ya lleno su hora NO compra, y que el que decide es el
  RELOJ y no la cola: el mismo arbol, con la cola vacia, compra o no compra
  segun lo que ese brazo creo en los ultimos sesenta minutos;
* que la ventana es MOVIL — lo de hace una hora y un minuto ya no cuenta —
  porque un tope que se resetea en punto permite el doble a caballo del
  cambio de hora, que es la rafaga que esto viene a cortar;
* que el recibo esta rate-limited tambien: uno por brazo y tramo de diez
  minutos, porque un recibo por compra saltada seria el mismo bucle con otra
  tabla;
* que el cupo por cola SIGUE puesto y sigue siendo el que ata la rafaga por
  debajo del techo: son dos mecanismos que miden stock y caudal, y ninguno
  tapa el agujero del otro;
* que esto no toca a quien no se realimenta — peticiones de personas, el
  goteo del colchon, la siembra de la raiz — porque ninguno de ellos compra
  sobre su propio resultado;
* y que ``0`` apaga el brazo entero, que es el rollback sin desplegar nada.
"""

import hashlib
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from . import ingest, logic, proof
from .models import AnalysisTask, DBEvent, Edge, Position, ProofCampaign
from .testing import TestCase

# El walker encendido, que es la unica configuracion en la que la cascada y el
# descenso por valor compran algo (§ ingest, ATOMICDB_DESCENT).
VALUE = {'ATOMICDB_DESCENT': 'value'}
# Topes de dos para no tener que sembrar cientos de filas por test.  Los
# defaults de verdad se comprueban aparte, en ``ArmRateKnobTests``.
TIGHT = dict(VALUE, ATOMICDB_ARM_RATE_CASCADE='2',
             ATOMICDB_ARM_RATE_QUALITY='2', ATOMICDB_ARM_RATE_DESCEND='2')


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **fields):
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **fields)


def _edge(parent, child, uci):
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


def _spend(arm, count, minutes_ago=0):
    """``count`` compras ya hechas por ese brazo, ``minutes_ago`` atras.

    Nacen COMPLETED a proposito, porque ese es el escenario del incidente: la
    flota sirve al instante, asi que lo comprado en la ultima hora ya no esta
    en la cola y el cupo por pendientes no ve absolutamente nada.

    ``created`` es ``auto_now_add``, asi que envejecerlas es un UPDATE
    posterior: es la unica forma de escribir un pasado, y la ventana movil no
    se puede probar sin pasado.
    """
    parent = _pos(f'spent-{arm}-{minutes_ago}-{count}')
    AnalysisTask.objects.bulk_create([
        AnalysisTask(position=parent, generation=index, arm=arm,
                     state=AnalysisTask.TState.COMPLETED,
                     budget_nodes=ingest.BUDGET_LADDER[0])
        for index in range(count)])
    if minutes_ago:
        AnalysisTask.objects.filter(position=parent).update(
            created=timezone.now() - timedelta(minutes=minutes_ago))
    return parent


def _age_receipts(minutes):
    DBEvent.objects.filter(kind=ingest.ARM_RATE_EVENT).update(
        ts=timezone.now() - timedelta(minutes=minutes))


def _receipts():
    return DBEvent.objects.filter(kind=ingest.ARM_RATE_EVENT)


# ---------------------------------------------------------------------------
# El conmutador: donde vive el numero y que pasa cuando esta mal escrito.
# ---------------------------------------------------------------------------
class ArmRateKnobTests(TestCase):

    def test_the_defaults_are_the_ones_the_owner_agreed(self):
        self.assertEqual(ingest.arm_rate(ingest.VALUE_CASCADE_ARM), 240)
        self.assertEqual(ingest.arm_rate(ingest.QUALITY_ARM), 120)
        self.assertEqual(ingest.arm_rate(ingest.VALUE_DESCEND_ARM), 120)

    def test_the_environment_lowers_the_ceiling(self):
        with override_settings(ATOMICDB_ARM_RATE_CASCADE='30'):
            self.assertEqual(ingest.arm_rate(ingest.VALUE_CASCADE_ARM), 30)

    def test_a_mistyped_value_keeps_the_default(self):
        """Equivocarse al teclear un tope no puede ser un incidente."""
        for value in ('', 'muchas', '-5', '12.5', None):
            with override_settings(ATOMICDB_ARM_RATE_QUALITY=value):
                self.assertEqual(ingest.arm_rate(ingest.QUALITY_ARM), 120,
                                 msg=f'{value!r} tenia que dejar el default')


# ---------------------------------------------------------------------------
# La cascada: el brazo que se midio a 4.551/h.
# ---------------------------------------------------------------------------
@override_settings(**TIGHT)
class CascadeRateTests(TestCase):

    def setUp(self):
        self.node = _pos('CR-node', 'w', eval_cp=300, expanded=True)
        for index in range(ingest.VALUE_CASCADE_CAP + 4):
            _edge(self.node, _pos(f'CR-child-{index}', 'b'), f'c{index}')

    def test_an_arm_at_its_hourly_count_buys_nothing(self):
        _spend(ingest.VALUE_CASCADE_ARM, 2)

        self.assertEqual(ingest._queue_value_cascade(self.node), 0)
        self.assertFalse(AnalysisTask.objects.filter(
            position__key=_key('CR-child-0')).exists())

    def test_the_empty_queue_does_not_reopen_the_ceiling(self):
        """El sintoma exacto: cola vacia, brazo gastado, y aun asi no compra.

        Es lo que separa el techo del cupo.  Con las dos compras ya servidas
        el cupo por pendientes las da por inexistentes y volveria a comprar,
        que es literalmente como se llega a 4.551 en una hora.
        """
        _spend(ingest.VALUE_CASCADE_ARM, 2)
        self.assertEqual(AnalysisTask.objects.filter(
            state='PENDING', arm=ingest.VALUE_CASCADE_ARM).count(), 0)

        self.assertEqual(ingest._queue_value_cascade(self.node), 0)

    def test_the_window_rolls_and_an_old_hour_is_forgotten(self):
        """Las mismas dos compras, una hora y un minuto atras: no cuentan.

        Dentro de la ventana esto compra cero (el test de arriba); fuera,
        el brazo recupera su tope entero y compra los dos que le caben.
        """
        _spend(ingest.VALUE_CASCADE_ARM, 2, minutes_ago=61)

        self.assertEqual(ingest._queue_value_cascade(self.node), 2)

    def test_the_hourly_room_clips_the_batch_below_the_burst_brake(self):
        """El techo lo es: el ultimo lote de la hora no entra entero.

        Hay 20 respuestas sin juzgar y el cupo por cola daria 16.  Con una
        compra ya hecha en la hora y un tope de dos, entra UNA.
        """
        _spend(ingest.VALUE_CASCADE_ARM, 1)

        self.assertEqual(ingest._queue_value_cascade(self.node), 1)
        self.assertEqual(_receipts().count(), 0)

    def test_below_the_ceiling_the_burst_brake_is_what_binds(self):
        """El cupo por cola sigue puesto y sigue siendo el de siempre."""
        with override_settings(ATOMICDB_ARM_RATE_CASCADE='1000'):
            made = ingest._queue_value_cascade(self.node)

        self.assertEqual(made, ingest.VALUE_CASCADE_CAP)
        self.assertEqual(_receipts().count(), 0)

    def test_a_rate_of_zero_turns_the_arm_off(self):
        with override_settings(ATOMICDB_ARM_RATE_CASCADE='0'):
            self.assertEqual(ingest._queue_value_cascade(self.node), 0)

        self.assertFalse(AnalysisTask.objects.exists())
        # Apagar a mano no es alcanzar un techo, asi que no deja recibo.
        self.assertEqual(_receipts().count(), 0)


# ---------------------------------------------------------------------------
# El recibo: uno por brazo y tramo, no uno por compra saltada.
# ---------------------------------------------------------------------------
@override_settings(**TIGHT)
class ArmRateReceiptTests(TestCase):

    def setUp(self):
        self.node = _pos('RR-node', 'w', eval_cp=300, expanded=True)
        _edge(self.node, _pos('RR-child', 'b'), 'c0')
        _spend(ingest.VALUE_CASCADE_ARM, 2)

    def test_a_skip_leaves_a_receipt_with_its_numbers(self):
        ingest._queue_value_cascade(self.node)

        receipt = _receipts().get()
        self.assertEqual(receipt.payload['arm'], ingest.VALUE_CASCADE_ARM)
        self.assertEqual(receipt.payload['rate'], 2)
        self.assertEqual(receipt.payload['made'], 2)
        self.assertEqual(receipt.payload['window_minutes'], 60)

    def test_a_second_skip_inside_ten_minutes_leaves_none(self):
        for _ in range(5):
            ingest._queue_value_cascade(self.node)

        self.assertEqual(_receipts().count(), 1)

    def test_the_next_stretch_gets_its_own_receipt(self):
        ingest._queue_value_cascade(self.node)
        _age_receipts(11)

        ingest._queue_value_cascade(self.node)

        self.assertEqual(_receipts().count(), 2)

    def test_another_arm_is_another_receipt(self):
        """El silencio de un brazo no puede tapar el techo de otro."""
        ingest._queue_value_cascade(self.node)
        _spend(ingest.QUALITY_ARM, 2)

        ingest._queue_quality_convergence([('a1a2', self.node.key, 8_000_000)])

        self.assertEqual(_receipts().count(), 2)
        self.assertEqual(
            {receipt.payload['arm'] for receipt in _receipts()},
            {ingest.VALUE_CASCADE_ARM, ingest.QUALITY_ARM})


# ---------------------------------------------------------------------------
# Calidad: dos compradores, una sola bolsa.
# ---------------------------------------------------------------------------
@override_settings(**TIGHT)
class QualityRateTests(TestCase):

    def setUp(self):
        self.parent = _pos('QR-parent', 'w', eval_cp=100,
                           nodes_invested=512_000_000, expanded=True)
        self.child = _pos('QR-child', 'b', eval_cp=-900)
        _edge(self.parent, self.child, 'a1a2')
        self.picks = [('a1a2', self.parent.key, 8_000_000)]

    def test_the_convergence_stops_at_its_hourly_count(self):
        _spend(ingest.QUALITY_ARM, 2)

        self.assertEqual(ingest._queue_quality_convergence(self.picks), 0)
        self.assertFalse(AnalysisTask.objects.filter(
            position=self.child).exists())

    def test_the_cycle_disambiguation_shares_the_same_bag(self):
        """Corren en la misma pasada de respaldo: un tope, no dos."""
        _spend(ingest.QUALITY_ARM, 2)

        self.assertEqual(ingest._queue_cycle_disambiguation(self.picks), 0)

    def test_with_room_left_the_purchase_is_the_one_of_always(self):
        made = ingest._queue_quality_convergence(self.picks)

        self.assertEqual(made, 1)
        self.assertEqual(AnalysisTask.objects.get(position=self.child).arm,
                         ingest.QUALITY_ARM)

    def test_a_rate_of_zero_turns_the_arm_off(self):
        with override_settings(ATOMICDB_ARM_RATE_QUALITY='0'):
            self.assertEqual(ingest._queue_cycle_disambiguation(self.picks), 0)
            self.assertEqual(ingest._queue_quality_convergence(self.picks), 0)

        self.assertFalse(AnalysisTask.objects.exists())


# ---------------------------------------------------------------------------
# El descenso por valor: agotarlo devuelve df-pn, no deja la cola vacia.
# ---------------------------------------------------------------------------
@override_settings(ATOMICDB_SELECTOR='pn', **TIGHT)
class DescendRateTests(TestCase):

    def setUp(self):
        # Una espina de dos aristas: la raiz respalda por ``m0`` y la punta
        # sin respaldo es lo que el walker compra.
        self.root = _pos('DR-root', 'w', eval_cp=40, backed_eval=812,
                         backed_move='m0', expanded=True)
        self.tip = _pos('DR-tip', 'b', eval_cp=812)
        _edge(self.root, self.tip, 'm0')
        ProofCampaign.objects.filter(active=True).update(active=False)
        self.campaign = ProofCampaign.objects.create(
            name='rate-test', root=self.root,
            goal=ProofCampaign.Goal.WHITE_WIN,
            algorithm_version=proof.ALGORITHM_VERSION,
            repertoire_policy={'primary': 1.0, 'backup': 0.0, 'explore': 0.0})

    def test_the_walker_marks_what_it_buys(self):
        tasks = ingest._next_tasks_by_proof(1)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].position_id, self.tip.key)
        self.assertEqual(tasks[0].arm, ingest.VALUE_DESCEND_ARM)

    def test_a_spent_hour_falls_back_to_the_proof_descent(self):
        """Agotado el techo, la cola SIGUE recibiendo: cambia quien elige."""
        _spend(ingest.VALUE_DESCEND_ARM, 2)

        tasks = ingest._next_tasks_by_proof(1)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].arm, '')
        self.assertEqual(_receipts().count(), 1)

    def test_the_top_up_still_fills_the_pool_with_the_arm_spent(self):
        """El goteo del colchon no se realimenta y no se le corta el grifo."""
        _spend(ingest.VALUE_DESCEND_ARM, 2)

        minted = ingest.top_up_analysis_pool(target=1)

        self.assertEqual(minted, 1)

    def test_a_rate_of_zero_leaves_the_descent_of_always(self):
        with override_settings(ATOMICDB_ARM_RATE_DESCEND='0'):
            tasks = ingest._next_tasks_by_proof(1)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].arm, '')


# ---------------------------------------------------------------------------
# Lo que NO paga: nada que no compre sobre su propio resultado.
# ---------------------------------------------------------------------------
@override_settings(**TIGHT)
class UnbudgetedDoorsTests(TestCase):

    def setUp(self):
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)
        self.child = Edge.objects.get(parent=self.root,
                                      move_uci='g1f3').child
        # Los tres brazos a tope, para que ninguna de estas puertas pueda
        # colar por tener sitio.
        for arm in (ingest.VALUE_CASCADE_ARM, ingest.QUALITY_ARM,
                    ingest.VALUE_DESCEND_ARM):
            _spend(arm, 50)

    def test_a_visitor_request_is_never_rate_limited(self):
        self.assertEqual(ingest.request_analysis(self.child), 'queued')

        task = AnalysisTask.objects.get(position=self.child)
        self.assertEqual(task.source, AnalysisTask.Source.USER)
        self.assertEqual(task.arm, '')

    def test_the_root_seeding_is_never_rate_limited(self):
        made = ingest.bootstrap_root()

        self.assertGreater(made, 0)
        self.assertEqual(_receipts().count(), 0)

    def test_the_top_up_is_never_rate_limited(self):
        minted = ingest.top_up_analysis_pool(target=3)

        self.assertEqual(minted, 3)

    def test_the_disputed_witness_rebuy_is_never_rate_limited(self):
        task = ingest._queue_disputed_reanalysis(self.child)

        self.assertEqual(task.position_id, self.child.key)
        self.assertEqual(task.arm, '')
        self.assertEqual(_receipts().count(), 0)


# ---------------------------------------------------------------------------
# El recibo es telemetria, no una noticia sobre el arbol.
# ---------------------------------------------------------------------------
class ArmRateFeedTests(TestCase):

    def test_the_receipt_stays_out_of_the_front_page_feed(self):
        from . import views

        self.assertIn(ingest.ARM_RATE_EVENT, views.FEED_HIDDEN_KINDS)
