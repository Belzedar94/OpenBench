"""Corre los DOS selectores sobre la misma base y publica la diferencia.

QUE INSTRUMENTO ES ESTE.  El selector acotado no se enciende porque los tests
pasen: los tests demuestran que las dos formulas coinciden en un grafo de
juguete, y lo que hay que demostrar es que coinciden en la CIMA de la base
viva, que es lo unico que alguien consume.  Esta orden es la medida que
autoriza a tocar ``ATOMICDB_SELECTOR_V2``, y hasta que sus numeros esten
publicados el conmutador se queda donde esta.

NINGUNO DE LOS DOS ESCRIBE.  Los dos motores corren en modo solo calculo
(``top_k``) y devuelven ``{clave: prioridad}`` de su propia cima.  Si el
primero escribiese, el segundo mediria una base que el primero acaba de
mover, y la comparacion no compararia nada.

QUE SE MIRA, Y POR QUE ESOS DOS NUMEROS.  Jaccard dice si los dos motores
eligen el MISMO conjunto de posiciones; Kendall tau dice si las ordenan igual
entre las que ambos eligieron.  Hacen falta los dos: un motor que acierte el
conjunto y lo baraje sirve para poco, y uno que ordene finisimo un conjunto
equivocado sirve para menos.  Los umbrales (Jaccard >= 0,95, tau >= 0,9)
salen del diseno, no de lo que salga hoy — se fijaron antes de medir.

LA MEMORIA, CON SU LETRA PEQUENA.  El pico de RSS que da el sistema es del
PROCESO y solo sube; no se puede poner a cero entre motores.  Por eso el orden
importa y esta fijado: primero v1, que es el que se sabe caro, y luego v2.  Un
``pico Δ`` de v2 cercano a cero significa que nunca hizo falta mas memoria que
la que v1 ya habia pedido — que es exactamente el aprobado que se busca.
"""

import csv
import json
import math
import os
import sys
import time

from django.core.management.base import BaseCommand

from atomicdb import ingest
from atomicdb.database import connection

try:                        # Unix: el pico del proceso sin dependencias
    import resource
except ImportError:         # pragma: no cover - Windows
    resource = None

try:
    import psutil
except ImportError:         # pragma: no cover - entorno sin psutil
    psutil = None

MIN_JACCARD = 0.95
MIN_TAU = 0.90


class Command(BaseCommand):
    help = 'Compare the v1 and v2 selector engines without writing anything.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--passes', type=int, default=1,
            help='How many times to run both engines (default: 1).  Mas de '
                 'una porque la base se mueve debajo: un Jaccard bueno una '
                 'vez es una anecdota, sostenido es una medida.')
        parser.add_argument(
            '--top', type=int, default=1000,
            help='Size of the compared head (default: 1000).')
        parser.add_argument(
            '--json', action='store_true',
            help='Emit one JSON object per pass instead of the table.')
        parser.add_argument(
            '--dump', metavar='PATH',
            help='Vuelca a CSV la UNION de las dos cimas con las columnas '
                 'que explican por que difieren.  Son 2*top filas como mucho, '
                 'no la base: un volcado que hubiera que abrir por lotes no '
                 'se abre.')

    def handle(self, *args, **options):
        passes = max(1, options['passes'])
        top = max(1, options['top'])

        connection.ensure_connection()
        if connection.vendor == 'sqlite':
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA busy_timeout = 30000')

        if not options['json']:
            self.stdout.write(
                f'selector_shadow: {passes} pasada(s), top-{top}')
            self.stdout.write(
                'pass engine  seconds   keys   pico_MB  pico_d_MB   rss_d_MB')

        jaccards, taus = [], []
        for number in range(1, passes + 1):
            first = self._run('v1', ingest.refresh_priorities_v1, top)
            second = self._run('v2', ingest.refresh_priorities_v2, top)
            jaccard = _jaccard(set(first['head']), set(second['head']))
            tau, common = _kendall_tau(first['head'], second['head'])
            jaccards.append(jaccard)
            taus.append(tau)
            if options['json']:
                self.stdout.write(json.dumps({
                    'pass': number, 'top': top, 'jaccard': jaccard,
                    'kendall_tau': tau, 'common': common,
                    'v1': _measures(first), 'v2': _measures(second),
                }, sort_keys=True))
                continue
            for row in (first, second):
                self.stdout.write(
                    f'{number:>4} {row["engine"]:<6} {row["seconds"]:>8.3f} '
                    f'{len(row["head"]):>6} {_mb(row["peak_mb"]):>9} '
                    f'{_mb(row["peak_delta_mb"]):>10} '
                    f'{_mb(row["rss_delta_mb"]):>10}')
            self.stdout.write(
                f'     jaccard={jaccard:.4f}  tau={tau:.4f}  '
                f'comunes={common}')

        verdict = (min(jaccards) >= MIN_JACCARD and min(taus) >= MIN_TAU)
        summary = {
            'passes': passes, 'top': top,
            'jaccard_min': min(jaccards),
            'jaccard_mean': sum(jaccards) / len(jaccards),
            'tau_min': min(taus),
            'tau_mean': sum(taus) / len(taus),
            'thresholds': {'jaccard': MIN_JACCARD, 'tau': MIN_TAU},
            # Un veredicto, no una nota: quien lea esto quiere saber si puede
            # tocar el conmutador, y "0,97" no contesta esa pregunta solo.
            'verdict': 'PASS' if verdict else 'FAIL',
        }
        # El volcado sale de la ULTIMA pasada: si el veredicto es FAIL, lo que
        # hay que mirar es la foto que lo produjo, no un promedio de tres.
        if options['dump']:
            summary['dump'] = _dump(options['dump'], first['head'],
                                    second['head'])
        self.stdout.write(json.dumps(summary, sort_keys=True))

    def _run(self, engine, refresh, top):
        """Una pasada de un motor, con su reloj y su memoria alrededor."""
        peak_before, rss_before = _peak_mb(), _rss_mb()
        started = time.monotonic()
        head = refresh(force=True, top_k=top)
        seconds = time.monotonic() - started
        peak_after, rss_after = _peak_mb(), _rss_mb()
        return {
            'engine': engine, 'seconds': seconds, 'head': head,
            'peak_mb': peak_after,
            'peak_delta_mb': _difference(peak_after, peak_before),
            'rss_delta_mb': _difference(rss_after, rss_before),
        }


def _measures(row):
    return {'seconds': round(row['seconds'], 3), 'keys': len(row['head']),
            'peak_mb': _round(row['peak_mb']),
            'peak_delta_mb': _round(row['peak_delta_mb']),
            'rss_delta_mb': _round(row['rss_delta_mb'])}


def _jaccard(left, right):
    """Cuanto se solapan las dos cimas.  Dos vacias son identicas."""
    union = left | right
    if not union:
        return 1.0
    return len(left & right) / len(union)


def _kendall_tau(left, right):
    """tau-b sobre las claves que ambos motores metieron en su cima.

    Se cuentan los pares a mano — O(n^2), medio millon de comparaciones con
    n=1000 — en vez de traer scipy para esto.  ``tau-b`` y no ``tau-a`` porque
    los empates de prioridad son comunes de verdad: un nodo sin eval y sin
    visitas puntua igual que sus hermanos, y tratar ese empate como desorden
    seria castigar a los dos motores por coincidir.

    LOS CASOS DEGENERADOS DEVUELVEN CERO, Y ESO ES UN SUSPENSO A PROPOSITO.
    Menos de dos claves comunes, o un lado entero empatado, no significan
    "coinciden": significan que no hay orden que medir.  Un instrumento que
    autoriza a conmutar no puede dar el aprobado por AUSENCIA de evidencia —
    devolver 1,0 aqui haria pasar a un motor que le pusiera la misma prioridad
    a todo.
    """
    common = sorted(set(left) & set(right))
    n = len(common)
    if n < 2:
        return 0.0, n
    scores = [(left[key], right[key]) for key in common]
    concordant = discordant = tied_left = tied_right = tied_both = 0
    for i in range(n):
        first_left, first_right = scores[i]
        for j in range(i + 1, n):
            other_left, other_right = scores[j]
            delta_left = first_left - other_left
            delta_right = first_right - other_right
            if delta_left == 0 and delta_right == 0:
                tied_both += 1
            elif delta_left == 0:
                tied_left += 1
            elif delta_right == 0:
                tied_right += 1
            elif (delta_left > 0) == (delta_right > 0):
                concordant += 1
            else:
                discordant += 1
    ranked = concordant + discordant
    denominator = math.sqrt((ranked + tied_right) * (ranked + tied_left))
    if denominator == 0:
        return 0.0, n     # un lado entero empatado: no hay orden que comparar
    return (concordant - discordant) / denominator, n


# ---------------- el volcado ----------------

DUMP_BATCH = 500

DUMP_COLUMNS = ('key', 'klass', 'in_v1', 'in_v2', 'in_ball', 'reachable',
                'prio_v1', 'prio_v2', 'delta', 'runits_v1', 'runits_v2',
                'regret_ball', 'status', 'mate_band', 'expanded', 'visits',
                'campaign', 'eval_cp', 'backed_eval')


def _runits_of(priority, base):
    """Las unidades de regret que un motor cobro, DESPEJADAS de su prioridad.

    La formula es ``base - REGRET_WEIGHT * runits`` y ``base`` sale entera de
    las columnas de la fila, asi que esta resta devuelve exactamente lo que el
    motor uso — sin pagar un segundo Dijkstra global para preguntarselo.  Es la
    diferencia entre un diagnostico que se puede lanzar en la maquina de
    produccion y uno que hay que pedir por ventana tranquila.
    """
    if priority is None:
        return None
    return round((base - priority) / ingest.REGRET_WEIGHT, 6)


def _klass(in_first, in_second, in_ball, reachable):
    """La CLASE de la fila, que es el diagnostico ya masticado.

    Las cuatro banderas se combinan en pocas formas y cada una tiene un nombre
    y una causa.  ``walled`` es la que hay que buscar cuando el veredicto sale
    FAIL con el orden intacto: nadie la alcanza caminando (un cierre no relaja
    hacia abajo) pero la columna dice que cuelga de la raiz, asi que la foto
    global le perdona el regret y el acotado se lo cobra saturado.
    """
    if in_first and in_second:
        return 'agree'
    if in_ball:
        return 'in-ball-disagree'      # no deberia pasar: la formula es una
    if reachable:
        return 'walled'                # sin camino abierto, con arista
    return 'loose'                     # suelta de verdad: los dos cobran 5


def _dump(path, first, second):
    """La union de las dos cimas, con el porque de cada fila al lado.

    Se vuelca la UNION y no la base: lo que hay que contestar es "quien esta en
    una cima y no en la otra, y que columna lo explica", y para eso sobran dos
    mil filas.  Un volcado de cinco millones no se abre.

    LO QUE NO SE HACE AQUI, Y POR QUE.  No se recalcula el Dijkstra GLOBAL para
    poder poner el ``runits`` de v1 en las filas que v1 dejo fuera de su cima:
    esa foto son los 4,7 GB que este trabajo viene a quitar, y un diagnostico
    que hay que pedir por ventana tranquila deja de lanzarse.  La pregunta se
    contesta igual de bien con ``in_ball`` — que sale del recorrido ACOTADO,
    barato — mas ``reachable``: una fila que nadie alcanzo caminando pero que
    la columna marca es exactamente la que los dos motores tasan distinto, y
    ``klass`` ya la llama por su nombre.
    """
    from atomicdb.models import Position

    keys = sorted(set(first) | set(second))
    votes = ingest._active_campaign_votes()
    ball = ingest._regret_from_root_bounded(campaign_votes=votes)
    buckets = {}
    written = 0
    with open(path, 'w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=DUMP_COLUMNS)
        writer.writeheader()
        for start in range(0, len(keys), DUMP_BATCH):
            chunk = keys[start:start + DUMP_BATCH]
            for (key, eval_cp, backed_eval, status, expanded, visits,
                 campaign_id, reachable) in (
                     Position.objects.filter(key__in=chunk).values_list(
                         'key', 'eval_cp', 'backed_eval', 'status', 'expanded',
                         'visits', 'campaign_id', 'reachable')):
                known = ingest.known_eval_of(status, backed_eval, eval_cp)
                base = ingest.priority_of(known, expanded, visits, campaign_id,
                                          0.0, votes)
                one, two = first.get(key), second.get(key)
                in_ball = key in ball
                klass = _klass(key in first, key in second, in_ball, reachable)
                buckets[klass] = buckets.get(klass, 0) + 1
                writer.writerow({
                    'key': key, 'klass': klass,
                    'in_v1': int(key in first), 'in_v2': int(key in second),
                    'in_ball': int(in_ball), 'reachable': int(reachable),
                    'prio_v1': _round6(one), 'prio_v2': _round6(two),
                    'delta': (None if one is None or two is None
                              else round(one - two, 6)),
                    'runits_v1': _runits_of(one, base),
                    'runits_v2': _runits_of(two, base),
                    'regret_ball': _round6(ball.get(key)),
                    'status': status,
                    'mate_band': int(known is not None
                                     and abs(known) >= ingest.MATE_BAND),
                    'expanded': int(expanded), 'visits': visits,
                    'campaign': campaign_id, 'eval_cp': eval_cp,
                    'backed_eval': backed_eval})
                written += 1
    # El recuento por clase es el diagnostico en una linea.  Un FAIL con
    # ``walled`` grande no necesita que nadie abra el CSV: dice que la columna
    # y el recorrido no contestan la misma pregunta.
    return {'path': path, 'rows': written, 'union': len(keys),
            'classes': dict(sorted(buckets.items(),
                                   key=lambda item: -item[1]))}


def _round6(value):
    return None if value is None else round(value, 6)


# ---------------- memoria ----------------

def _peak_mb():
    """Pico de RSS del proceso, en MB.  Monotono: nunca baja."""
    if resource is not None:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux lo da en kilobytes; macOS, en bytes.
        divisor = 1024.0 ** 2 if sys.platform == 'darwin' else 1024.0
        return peak / divisor
    if psutil is not None:
        peak = getattr(psutil.Process().memory_info(), 'peak_wset', None)
        if peak is not None:
            return peak / (1024.0 ** 2)
    return None


def _rss_mb():
    """RSS actual, en MB, o None si no hay de donde sacarlo."""
    try:
        with open('/proc/self/statm', encoding='ascii') as handle:
            pages = int(handle.read().split()[1])
        return pages * os.sysconf('SC_PAGE_SIZE') / (1024.0 ** 2)
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    if psutil is not None:
        return psutil.Process().memory_info().rss / (1024.0 ** 2)
    return None


def _difference(after, before):
    if after is None or before is None:
        return None
    return after - before


def _round(value):
    return None if value is None else round(value, 1)


def _mb(value):
    return 'n/a' if value is None else f'{value:.1f}'
