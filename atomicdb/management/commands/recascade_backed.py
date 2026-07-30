"""Recascada GLOBAL de valores respaldados.

Existe por las reliquias: cada endurecimiento de las guardas de ``_backed_for``
(direccional, calidad, corte de autoridad de prueba) corrige el calculo HACIA
DELANTE, pero los ``backed_eval`` ya escritos con las reglas viejas se quedan
tal cual hasta que algo vuelva a tocar su familia.  Caso Wolfram (30-jul): un
nodo ancla con eval propio 671 seguia mostrando el 9994 que una espina
caminada le subio ANTES de la guarda; la regla de hoy calcula 671, pero nadie
habia recomputado el nodo.

Pasadas completas por chunks hasta punto fijo, estilo backfill_mate_distance:
cada pasada siembra ``backup_backed_evals`` con todos los nodos que tienen
aristas (los unicos que pueden tener respaldo) y deja que el ascenso natural
propague; se repite hasta que una pasada no cambie ninguna fila o se agote el
tope.  Idempotente y seguro en vivo: recalcular un respaldo correcto lo deja
identico (el corte ``_backed_stored`` no escribe), y el churn de la ingesta
concurrente solo puede anadir trabajo que la pasada siguiente absorbe.
"""

from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef

from atomicdb import ingest
from atomicdb.models import Edge, Position


class Command(BaseCommand):
    help = 'Recalcula todos los backed_eval con las guardas vigentes'

    def add_arguments(self, parser):
        parser.add_argument('--chunk', type=int, default=2000)
        parser.add_argument('--max-passes', type=int, default=8)
        parser.add_argument(
            '--max-changes', type=int, default=0,
            help='Valvula: aborta si una pasada cambia mas filas que esto '
                 '(0 = sin valvula)')

    def handle(self, *args, **opts):
        chunk, cap = opts['chunk'], opts['max_passes']
        valve = opts['max_changes']
        # Solo nodos con hijos: una hoja sin aristas no tiene respaldo que
        # recalcular (y si lo tuviera escrito, el primer padre la recoge).
        with_kids = Position.objects.filter(
            Exists(Edge.objects.filter(parent_id=OuterRef('key'))))
        for n in range(1, cap + 1):
            keys = with_kids.values_list('key', flat=True).iterator(
                chunk_size=chunk)
            changed = 0
            batch = []
            for key in keys:
                batch.append(key)
                if len(batch) >= chunk:
                    changed += ingest.backup_backed_evals(batch)
                    batch = []
            if batch:
                changed += ingest.backup_backed_evals(batch)
            self.stdout.write('pasada %d: %d filas cambiadas' % (n, changed))
            if valve and changed > valve:
                self.stdout.write(self.style.ERROR(
                    'valvula: %d > %d, abortando' % (changed, valve)))
                return
            if changed == 0:
                self.stdout.write(self.style.SUCCESS(
                    'punto fijo en la pasada %d' % n))
                return
        self.stdout.write(self.style.WARNING(
            'tope de pasadas (%d) sin punto fijo' % cap))
