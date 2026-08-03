"""El sombra mide los dos selectores sin escribir ni uno de los dos.

Es la orden que autoriza a tocar ``ATOMICDB_SELECTOR_V2``, asi que lo que hay
que fijar aqui no es que "corra": es que no escriba, que compare la CIMA y no
otra cosa, y que termine diciendo si se puede conmutar o no.  Un instrumento
que da un numero sin veredicto deja la decision donde estaba.

El grafo es el mismo ancla del motor acotado (``test_selector_v2``): sobre el,
los dos motores coinciden exactamente, asi que un Jaccard por debajo de uno
aqui significa que el sombra esta midiendo mal, no que los motores difieran.

Y UN SEGUNDO GRAFO, que es el que faltaba.  El ancla pone los cierres como
HOJAS, asi que nunca tuvo poblacion DETRAS de un muro cerrado — justo la que
hizo fallar a la sombra sobre la base viva el 3-ago.  ``WalledSubtreeShadow
Tests`` monta las tres poblaciones (detras del muro, rama abierta, sueltas de
verdad) y exige el acuerdo ahi; y como el diagnostico vale tanto como el
arreglo, fija tambien la FIRMA del fallo — pertenencia rota con el orden
intacto — para que una regresion vuelva con nombre en vez de con misterio.
"""

import json
from io import StringIO

from django.core.management import call_command

from . import ingest, logic
from .models import Edge, Position
from .test_selector_v2 import AnchorFixture, quiet_fen
from .testing import TestCase


class ShadowCommandTests(TestCase):

    def setUp(self):
        self.fixture = AnchorFixture().build()

    def _shadow(self, *args):
        out = StringIO()
        call_command('selector_shadow', *args, stdout=out)
        return out.getvalue()

    def test_it_passes_on_a_graph_where_the_two_engines_agree(self):
        output = self._shadow('--passes', '2', '--top', '10')
        summary = json.loads(output.strip().splitlines()[-1])
        self.assertEqual(summary['verdict'], 'PASS')
        self.assertEqual(summary['jaccard_min'], 1.0)
        self.assertAlmostEqual(summary['tau_min'], 1.0, delta=1e-9)
        self.assertEqual(summary['passes'], 2)

    def test_it_writes_nothing(self):
        """Si el primero escribiese, el segundo mediria otra base."""
        before = dict(Position.objects.values_list('key', 'priority'))
        self._shadow('--passes', '1', '--top', '5')
        self.assertEqual(dict(Position.objects.values_list('key', 'priority')),
                         before)

    def test_the_table_names_both_engines(self):
        output = self._shadow('--passes', '1', '--top', '5')
        self.assertIn('v1', output)
        self.assertIn('v2', output)
        self.assertIn('jaccard=', output)

    def test_json_mode_reports_per_pass(self):
        output = self._shadow('--passes', '1', '--top', '5', '--json')
        rows = [json.loads(line) for line in output.strip().splitlines()]
        self.assertEqual(rows[0]['pass'], 1)
        self.assertEqual(rows[0]['v2']['keys'], 5)
        self.assertEqual(rows[-1]['verdict'], 'PASS')

    def test_the_head_is_capped_where_it_was_asked_to_be(self):
        """La cima es acotada: comparar millones seria el problema otra vez."""
        rows = [json.loads(line) for line
                in self._shadow('--passes', '1', '--top', '3',
                                '--json').strip().splitlines()]
        self.assertEqual(rows[0]['v1']['keys'], 3)
        self.assertEqual(rows[0]['v2']['keys'], 3)

    def test_a_head_with_no_order_in_it_does_not_pass(self):
        """El aprobado por AUSENCIA de evidencia es el fallo que mas duele.

        Una cima de una sola clave, o un motor que le pusiera la misma
        prioridad a todo, no demuestran acuerdo: demuestran que no hay orden
        que medir.  El veredicto tiene que decir que no.
        """
        summary = json.loads(
            self._shadow('--passes', '1', '--top', '1').strip()
            .splitlines()[-1])
        self.assertEqual(summary['jaccard_min'], 1.0)
        self.assertEqual(summary['tau_min'], 0.0)
        self.assertEqual(summary['verdict'], 'FAIL')


WALLED = 30      # detras de un cierre: ningun recorrido llega
OPEN = 30        # rama abierta, dentro de la bola
ORPHANS = 3      # sin una sola arista
TOP = 25

_SQUARES = [f'{file}{rank}' for file in 'abcdefgh' for rank in '12345678']


def _uci(index):
    """Etiqueta de arista distinta por indice; no tiene que ser legal."""
    return _SQUARES[index % 64] + _SQUARES[(index * 7 + 3) % 64]


def _quiet_fens(count):
    out = [quiet_fen(file_index, rank=rank, white_to_move=stm)
           for file_index in range(7)       # nunca en la columna del rey negro
           for rank in range(2, 8)
           for stm in (True, False)]
    assert len(out) >= count, f'{len(out)} FENs para {count}'
    return iter(out[:count])


class WalledSubtreeShadowTests(TestCase):
    """Con poblacion detras de un muro, los dos motores siguen coincidiendo.

    Las tres poblaciones estan elegidas para que el fallo, si vuelve, se note
    en el CONJUNTO y no en el orden:

    * detras del muro, banda de mate y muchas visitas — puntuan alto para v1
      en cuanto se les perdona el regret, que es exactamente lo que hace el
      precio de suelto;
    * en la rama abierta, evals modestas y regret real pequeno;
    * sueltas, banda de mate y sin visitas — las unicas que las dos cimas
      comparten cuando la columna se siembra mal, y por eso el ``tau`` sale
      casi perfecto sobre una interseccion diminuta.
    """

    def setUp(self):
        pool = _quiet_fens(WALLED + OPEN + ORPHANS)
        self.root = ingest.get_or_create_position(logic.start_fen())
        ingest.expand(self.root)

        self.wall = Edge.objects.get(parent=self.root, move_uci='h2h4').child
        self.wall.status, self.wall.closure = 'WHITE_WIN', 'MINIMAX'
        self.wall.eval_cp, self.wall.mate_in = 9_800, 2
        self.wall.save()

        self.walled = []
        for index in range(WALLED):
            node = next(self._seed(pool, eval_cp=9_500, visits=3 + index))
            Edge.objects.create(parent=self.wall, move_uci=_uci(index),
                                child=node)
            self.walled.append(node.key)

        self.branch = Edge.objects.get(parent=self.root,
                                       move_uci='e2e4').child
        self.opened = []
        for index in range(OPEN):
            node = next(self._seed(pool, eval_cp=100 + index))
            Edge.objects.create(parent=self.branch, move_uci=_uci(index),
                                child=node)
            self.opened.append(node.key)

        self.orphans = [next(self._seed(pool, eval_cp=9_800, visits=index)).key
                        for index in range(ORPHANS)]

        call_command('backfill_reachable', stdout=StringIO())

    def _seed(self, pool, **fields):
        node = ingest.get_or_create_position(next(pool))
        for name, value in fields.items():
            setattr(node, name, value)
        node.save()
        yield node

    def _summary(self, *args):
        out = StringIO()
        call_command('selector_shadow', *args, stdout=out)
        return json.loads(out.getvalue().strip().splitlines()[-1])

    # ---------------- la columna dice lo que dice el recorrido ----------------

    def test_the_column_stops_where_the_walk_stops(self):
        walked = ingest._regret_from_root()
        for key in self.walled:
            self.assertEqual(walked[key], float('inf'))
            self.assertFalse(Position.objects.get(key=key).reachable)
        for key in self.opened:
            self.assertLess(walked[key], float('inf'))
            self.assertTrue(Position.objects.get(key=key).reachable)

    # ---------------- el acuerdo ----------------

    def test_the_shadow_passes_with_population_behind_the_wall(self):
        summary = self._summary('--passes', '3', '--top', str(TOP))
        self.assertEqual(summary['verdict'], 'PASS')
        self.assertEqual(summary['jaccard_min'], 1.0)
        self.assertAlmostEqual(summary['tau_min'], 1.0, delta=1e-9)

    def test_the_walled_nodes_are_in_BOTH_heads(self):
        first = ingest.refresh_priorities_v1(force=True, top_k=TOP)
        second = ingest.refresh_priorities_v2(force=True, top_k=TOP)
        walled = set(self.walled)
        self.assertTrue(set(first) & walled)
        self.assertEqual(set(first), set(second))

    # ---------------- la firma del fallo, fijada ----------------

    def test_a_column_that_crossed_the_wall_gives_the_3_ago_signature(self):
        """Pertenencia rota, orden intacto: 0,011 con tau 0,997.

        Se reproduce marcando a mano lo que un BFS de aristas habria marcado.
        Sin esto, el dia que alguien vuelva a cruzar el muro el sombra dira
        FAIL y nadie sabra que ese numero ya tiene nombre.
        """
        Position.objects.filter(key__in=self.walled).update(reachable=True)
        summary = self._summary('--passes', '2', '--top', str(TOP))
        self.assertEqual(summary['verdict'], 'FAIL')
        self.assertLess(summary['jaccard_mean'], 0.10)
        self.assertGreater(summary['tau_min'], 0.99)
        # Constante entre pasadas: estructural, no una carrera.
        self.assertEqual(summary['jaccard_min'], summary['jaccard_mean'])

    def test_the_dump_names_the_column_that_moved_the_head(self):
        """El volcado contesta la pregunta, no la reformula.

        Con la columna cruzando el muro, el recuento por clase tiene que decir
        ``walled`` sin que nadie abra el CSV: nadie las alcanza caminando y la
        columna las marca igual.  Esa palabra ES el diagnostico.
        """
        import csv
        import os
        import tempfile

        Position.objects.filter(key__in=self.walled).update(reachable=True)
        path = os.path.join(tempfile.mkdtemp(), 'shadow.csv')
        summary = self._summary('--passes', '1', '--top', str(TOP),
                                '--dump', path)

        self.assertEqual(summary['dump']['classes']['walled'], TOP - ORPHANS)
        self.assertEqual(summary['dump']['classes']['agree'], ORPHANS)

        with open(path, encoding='utf-8') as handle:
            rows = {row['key']: row for row in csv.DictReader(handle)}
        self.assertEqual(len(rows), summary['dump']['union'])
        walled = rows[self.walled[0]]
        self.assertEqual(walled['klass'], 'walled')
        self.assertEqual(walled['in_v1'], '1')
        self.assertEqual(walled['in_v2'], '0')
        self.assertEqual(walled['in_ball'], '0')
        self.assertEqual(walled['reachable'], '1')
        self.assertEqual(walled['mate_band'], '1')
        self.assertEqual(float(walled['runits_v1']),
                         ingest.DISCONNECTED_REGRET)
        self.assertEqual(rows[self.orphans[0]]['klass'], 'agree')
