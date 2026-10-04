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

IN SLICES (``--slice-chars``).  The enumeration of "every position with
edges" is ONE query over the whole table: a hash aggregate of 78M edges
that spills to ``pgsql_tmp`` before the first row comes back, and that the
sweep pays again at the start of every pass.  On 4-Oct it was what killed
the run ("No space left on device").  With ``--slice-chars 3`` the key
space is walked in 4096 ranges of about 18 000 positions each; every range
is a primary-key scan with an index probe per row, writes no temp file,
and the sweep can be stopped and resumed at any range with ``--key-from``.

ON DEMAND (``--keys``).  One ascent seeded at the keys given and nothing
else: what a report of "this position shows a stale number" needs, in
milliseconds and without touching the rest of the graph.

OPEN ONLY (``--open-only``).  A proven position headlines its status and
its parents read that status, never its backed value (see
ingest._child_contribution), so recomputing it repairs nothing anybody
reads.  Four out of five positions with edges are proven.
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
        parser.add_argument(
            '--open-only', action='store_true',
            help='Only positions that are still UNKNOWN')
        parser.add_argument(
            '--slice-chars', type=int, default=0, choices=(0, 1, 2, 3, 4),
            help='Walk the key space in 16**N ranges instead of one query '
                 'over the whole table (0 = one query, as always)')
        parser.add_argument(
            '--key-from', default='',
            help='First key or key prefix of the sweep, inclusive')
        parser.add_argument(
            '--key-to', default='',
            help='Stop before this key or key prefix, exclusive')
        parser.add_argument(
            '--keys', default='',
            help='Comma separated position keys: recompute these and '
                 'whatever stands on them, then stop')

    @staticmethod
    def _slices(chars, key_from, key_to):
        """``(lo, hi)`` ranges that cover ``[key_from, key_to)`` in order.

        ``hi`` is exclusive and an empty bound means "no bound".  Keys are
        lowercase hex, so a prefix compares like the keys it starts.
        """
        if not chars:
            yield key_from, key_to
            return
        total = 16 ** chars
        for index in range(total):
            lo = format(index, '0%dx' % chars)
            hi = format(index + 1, '0%dx' % chars) if index + 1 < total else ''
            if key_to and lo >= key_to:
                return
            if key_from and hi and hi <= key_from:
                continue
            yield (max(lo, key_from),
                   min(hi, key_to) if hi and key_to else hi or key_to)

    def handle(self, *args, **opts):
        chunk, cap = opts['chunk'], opts['max_passes']
        valve = opts['max_changes']
        # Solo nodos con hijos: una hoja sin aristas no tiene respaldo que
        # recalcular (y si lo tuviera escrito, el primer padre la recoge).
        if opts['keys']:
            keys = [key for key in opts['keys'].split(',') if key]
            self.stdout.write('%d filas cambiadas desde %d claves' % (
                ingest.backup_backed_evals(keys), len(keys)))
            return
        with_kids = Position.objects.filter(
            Exists(Edge.objects.filter(parent_id=OuterRef('key'))))
        if opts['open_only']:
            with_kids = with_kids.filter(status='UNKNOWN')
        for n in range(1, cap + 1):
            changed = 0
            for lo, hi in self._slices(opts['slice_chars'],
                                       opts['key_from'], opts['key_to']):
                ranged = with_kids
                if lo:
                    ranged = ranged.filter(key__gte=lo)
                if hi:
                    ranged = ranged.filter(key__lt=hi)
                keys = ranged.values_list('key', flat=True).iterator(
                    chunk_size=chunk)
                batch = []
                for key in keys:
                    batch.append(key)
                    if len(batch) >= chunk:
                        changed += ingest.backup_backed_evals(batch)
                        batch = []
                if batch:
                    changed += ingest.backup_backed_evals(batch)
                if opts['slice_chars'] and opts['verbosity'] > 1:
                    # The resume point, should the run be stopped here.
                    self.stdout.write('  pasada %d: hasta %s, %d filas'
                                      % (n, hi or 'fin', changed))
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
