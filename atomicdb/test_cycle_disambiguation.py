"""El cero de una repeticion elegida compra su propia desambiguacion.

El caso de comunidad (Wolfram, 11-ago): el paseo de repeticion anulo a
tablas al unico hijo bueno de un nodo cuyas alternativas eran mates
perdidos.  El cero gano el negamax, borro un ~+9 de toda la linea de
arriba, y cuando la espina dejo de ciclar nadie desperto al padre — el
ascenso solo mira cambios de valor y el backed del hijo no habia cambiado.
Desde este parche, un respaldo que se decide por un hijo anulado encola en
el acto un pase mas hondo de ese hijo: el ingest de ese pase re-apunta la
espina y siembra a los padres, que es el despertador que faltaba.

El 12-ago se afino QUIEN puede quedarse con ese cero (§ ingest, el bloque de
la repeticion): con el anillo abierto — eslabones a los que les faltan
respuestas por abrir — el cero ya no decide nada y el anillo se deshace solo
hasta la unica cifra que alguien midio.  La compra, en cambio, se hace en los
dos casos, porque la pregunta es la misma.  Por eso el anillo de aqui esta
CERRADO: es la unica forma en la que una repeticion se queda de titular, que
es lo que estos tests vienen a vigilar.
"""

import hashlib

from . import ingest
from .models import AnalysisTask, DBEvent, Edge, Position
from .testing import TestCase


def _key(name):
    return hashlib.sha256(name.encode()).hexdigest()


def _pos(name, stm='w', **kw):
    return Position.objects.create(
        key=_key(name), fen=f'4k3/8/8/8/8/8/8/4K3 {stm} - - 0 1', **kw)


def _edge(parent, child, uci):
    return Edge.objects.create(parent=parent, move_uci=uci, child=child)


def _wolfram_shape(name):
    """A(max) cuyo unico hijo bueno C presta su valor por una espina que
    vuelve a A; la alternativa es un mate perdido.  El paseo anula a C, el
    cero gana el negamax (0 > mate perdido) y A queda respaldada en 0.

    Los tres eslabones con su lista de respuestas COMPLETA: sin eso ninguno
    de ellos puede quedarse con el cero (§ el docstring de arriba) y el
    clavado que estos tests vigilan no llega a existir."""
    a = _pos(f'{name}-A', 'w', eval_cp=827, nodes_invested=128_000_000,
             expanded=True)
    c = _pos(f'{name}-C', 'b', eval_cp=898, nodes_invested=128_000_000,
             backed_eval=903, backed_move='x1x1', backed_plies=2,
             backed_nodes=128_000_000, expanded=True)
    d = _pos(f'{name}-D', 'w', backed_eval=903, backed_move='y1y1',
             backed_plies=1, expanded=True)
    lost = _pos(f'{name}-L', 'b', status='BLACK_WIN', closure='MINIMAX')
    _edge(a, c, 'e6c5')
    _edge(c, d, 'x1x1')
    _edge(d, a, 'y1y1')          # la espina de C se cierra sobre A
    _edge(a, lost, 'a2a3')
    return a, c


class CycleDisambiguationTests(TestCase):

    def test_a_chosen_cycle_zero_buys_the_child_a_deeper_pass(self):
        a, c = _wolfram_shape('CD')

        ingest.backup_backed_evals([a.key])

        a.refresh_from_db()
        # El cero del ciclo gana el negamax (mejor que el mate perdido)...
        self.assertEqual(a.backed_eval, 0)
        self.assertEqual(a.backed_move, 'e6c5')
        # ...y por eso mismo viaja con su correccion comprada: un pase del
        # hijo MAS HONDO que sus 128M actuales.
        task = AnalysisTask.objects.get(position=c)
        self.assertGreater(task.budget_nodes, 128_000_000)
        self.assertEqual(task.arm, ingest.QUALITY_ARM)
        self.assertTrue(DBEvent.objects.filter(
            kind='CYCLE_DISAMBIGUATION').exists())

    def test_a_stale_pin_retries_the_purchase_on_every_pass(self):
        # El clavado historico: el backed ya ERA el cero del ciclo, la
        # pasada no cambia nada ("sin cambios")... y aun asi la compra se
        # re-intenta — el dedup de tareas absorbe la repeticion.
        a, c = _wolfram_shape('SP')
        ingest.backup_backed_evals([a.key])
        AnalysisTask.objects.all().delete()      # simular cola drenada

        ingest.backup_backed_evals([a.key])      # pasada sin cambios

        self.assertTrue(AnalysisTask.objects.filter(position=c).exists())

    def test_an_unchosen_cycle_buys_nothing(self):
        # Con una alternativa real mejor que el cero, el ciclo anulado no
        # decide nada y no hay pregunta abierta que comprar.
        a, c = _wolfram_shape('NC')
        winner = _pos('NC-W', 'b', eval_cp=500, nodes_invested=128_000_000)
        _edge(a, winner, 'd2d4')

        ingest.backup_backed_evals([a.key])

        a.refresh_from_db()
        self.assertEqual(a.backed_eval, 500)
        self.assertEqual(a.backed_move, 'd2d4')
        self.assertFalse(AnalysisTask.objects.filter(position=c).exists())
