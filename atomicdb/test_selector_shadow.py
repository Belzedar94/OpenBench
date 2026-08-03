"""El sombra mide los dos selectores sin escribir ni uno de los dos.

Es la orden que autoriza a tocar ``ATOMICDB_SELECTOR_V2``, asi que lo que hay
que fijar aqui no es que "corra": es que no escriba, que compare la CIMA y no
otra cosa, y que termine diciendo si se puede conmutar o no.  Un instrumento
que da un numero sin veredicto deja la decision donde estaba.

El grafo es el mismo ancla del motor acotado (``test_selector_v2``): sobre el,
los dos motores coinciden exactamente, asi que un Jaccard por debajo de uno
aqui significa que el sombra esta midiendo mal, no que los motores difieran.
"""

import json
from io import StringIO

from django.core.management import call_command

from .models import Position
from .test_selector_v2 import AnchorFixture
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
