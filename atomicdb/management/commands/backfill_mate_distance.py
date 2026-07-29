"""Recompute ``mate_in`` over the CLOSED subgraph with the corrected recurrence.

POR QUE EXISTE.  El refresco OR de ``backup_cascade`` solo se dejaba ACORTAR:
actualizaba la distancia de un ancestro cuando aparecia una linea mas corta y
la ignoraba cuando el hijo CORREGIA la suya hacia arriba.  El bloque AND de al
lado ya recalculaba exacto.  Resultado en la base viva (reporte de Wolfram,
posicion tras 1.Nf3 d5): ``WHITE_WIN MINIMAX mate_in=3`` con un unico hijo
ganador a 4 plies — el padre enseñaba "≤M2" cuando lo que sus propios hijos
sostienen es M3 (1+4 = 5 plies).  Arreglado el mecanismo, las filas ya escritas
siguen mintiendo hasta que alguien las recorra: esto es ese recorrido.

LA RECURRENCIA (la misma funcion que usa la cascada, ``mate_distance_refresh``,
para que las dos converjan al MISMO punto fijo y no a dos parecidos):

* hoja ``TERMINAL`` — 0, y no se toca: no tiene hijos de los que derivar nada.
* ``MATE_PV`` — su propio testigo (``len(won_line)``) es el techo: un hijo
  puede acortarlo, ninguno puede alargarlo.  Retirar un testigo de motor
  demasiado corto es trabajo de la certificacion (``recertify_mates``, la
  deuda de la flota), no de este recorrido.
* ``MINIMAX``/``TB``/``SOLVE`` sin testigo propio — distancia enteramente
  derivada: OR (gana el que mueve) ``1 + min`` sobre los hijos ganadores con
  distancia; AND ``1 + max`` sobre TODOS los hijos, y solo si todos la tienen.
* Sin nada que nombrar, ``NULL``.  Un numero que ya no sostiene ningun hijo es
  peor que no tener numero.

CICLOS Y PUNTO FIJO.  El DAG transpone y llega a cerrar ciclos (1.Nf3 Nf6
2.Ng1 Ng8 ES la posicion inicial una vez quitados los contadores), asi que no
hay orden topologico que recorrer: se itera hasta que una pasada no cambia
nada, con tope ``--max-passes``.  Solo la PRIMERA pasada barre el cierre
entero; las siguientes releen la FRONTERA (los padres de lo que se acaba de
mover), porque una fila solo depende de sus hijos.  Una correccion que sube
una cadena de won_line de 60 plies necesita 60 pasadas — baratas: cada una
toca cientos de filas, no cientos de miles.

Un ciclo cuyos nodos se sostienen unos a otros SIN suelo (nadie del ciclo tiene
testigo propio ni hijo terminal) no tiene distancia finita, y la recurrencia lo
canta subiendo un ply por pasada para siempre.  Una base consistente no puede
tener uno — el primer cierre de un ciclo tuvo que apoyarse en algo de fuera, y
revocar ese apoyo reabre el ciclo entero — pero este comando existe justamente
porque la base NO es consistente.  Contencion: una fila que SUBE en
``--max-climbs`` pasadas seguidas se CONGELA en lo que la base tiene hoy y se
reporta.  No se empeora una fila que no sabemos leer.  OJO CON EL LISTON: una
correccion legitima que sube por una cadena larga tambien sube un ply por
pasada hasta asentarse (relajacion simultanea: el frente avanza un nivel por
pasada), asi que el tope debe superar la cadena real mas larga; con un tope
corto las subidas legitimas se congelan como falsos ciclos.  Asentarse (subir
y luego bajar mientras los hermanos convergen) no cuenta: el contador se
reinicia en cuanto la fila deja de subir.  El reinicio deja ciego al contador
ante una OSCILACION (cambiar sin parar sin subir en monotono): eso lo caza
``--max-flips``, el total de pasadas en que la fila cambio de valor, por
encima de ``--max-climbs`` para no pisar a las cadenas.

TODO O NADA.  Las filas nuevas se acumulan en memoria y se escriben en un solo
UPDATE por lotes AL FINAL, y solo si hubo punto fijo.  Una pasada a medias
dejaria numeros relajados a medias, que es peor que el rancio que ya hay.

MEMORIA.  No carga el arbol: recorre por lotes ordenados por clave y lee los
hijos con un solo JOIN por lote.  En memoria quedan solo las filas que
CAMBIAN, una fraccion pequeña del cierre.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from atomicdb import ingest
from atomicdb.database import atomic, connection
from atomicdb.models import DBEvent, Edge, Position

DECISIVE = ('WHITE_WIN', 'BLACK_WIN')


class _Node:
    """Proyeccion minima que ``mate_distance_refresh`` sabe leer."""

    __slots__ = ('key', 'fen', 'status', 'closure', 'proof', 'won_line',
                 'mate_in', 'best_move')

    def __init__(self, row, mate_in, best_move):
        self.key = row['key']
        self.fen = row['fen']
        self.status = row['status']
        self.closure = row['closure']
        self.proof = row['proof']
        self.won_line = row['won_line']
        self.mate_in = mate_in
        self.best_move = best_move


class _Child:
    __slots__ = ('key', 'status', 'closure', 'proof', 'mate_in')

    def __init__(self, key, status, closure, proof, mate_in):
        self.key = key
        self.status = status
        self.closure = closure
        self.proof = proof
        self.mate_in = mate_in


class _Edge:
    __slots__ = ('move_uci', 'child')

    def __init__(self, move_uci, child):
        self.move_uci = move_uci
        self.child = child


class Command(BaseCommand):
    help = ('Recompute mate_in bottom-up over the closed subgraph with the '
            'corrected OR/AND recurrence. Idempotent; --dry-run reports only.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--batch-size', type=int, default=2_000,
            help='Closed positions read per query (default: 2000).')
        parser.add_argument(
            '--max-passes', type=int, default=200,
            help='Cap on passes before giving up on the fixed point '
                 '(default: 200). Nothing is written without one. Only the '
                 'first pass scans the whole closed set; the rest walk the '
                 'frontier (parents of what changed), so a correction that '
                 'climbs a 60-ply chain costs 60 cheap passes, not 60 scans.')
        parser.add_argument(
            '--max-climbs', type=int, default=120,
            help='A row whose distance CLIMBS on this many consecutive '
                 'passes is FROZEN at its stored value and reported '
                 '(default: 120). Climbing forever is the signature of a '
                 'cycle with no ground under it — but a correction riding up '
                 'a long chain ALSO climbs one ply per pass until it '
                 'settles, so the cap must exceed the longest real chain '
                 '(won lines run about 100 plies) or legitimate raises get '
                 'frozen as false cycles.')
        parser.add_argument(
            '--max-flips', type=int, default=150,
            help='A row whose value changes on this many passes in TOTAL is '
                 'FROZEN and reported (default: 150). Backstop above '
                 '--max-climbs for oscillations that never climb '
                 'monotonically; must stay ABOVE it or chain corrections '
                 'trip this first.')
        parser.add_argument(
            '--max-write', type=int, default=0,
            help='Refuse to write more rows than this and report instead '
                 '(default: 0 = unlimited). Safety valve for running the '
                 'real pass straight after a differently-shaped dry run.')
        parser.add_argument(
            '--sample', type=int, default=10,
            help='Rows printed per kind of change (default: 10).')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing anything.')

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        max_passes = options['max_passes']
        max_climbs = options['max_climbs']
        max_flips = options['max_flips']
        max_write = options['max_write']
        sample = options['sample']
        dry_run = options['dry_run']
        if batch_size <= 0:
            raise ValueError('--batch-size must be positive')
        if max_passes <= 0:
            raise ValueError('--max-passes must be positive')
        if max_climbs <= 0:
            raise ValueError('--max-climbs must be positive')
        if max_flips <= max_climbs:
            raise ValueError('--max-flips must exceed --max-climbs: chain '
                             'corrections climb once per pass and must not '
                             'trip the oscillation backstop first')
        if max_write < 0:
            raise ValueError('--max-write must be non-negative')
        if sample < 0:
            raise ValueError('--sample must be non-negative')

        connection.ensure_connection()
        if connection.vendor == 'sqlite':
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA busy_timeout = 30000')

        # Las cuentas de cabecera son EXACTAMENTE lo que se va a escribir: se
        # calculan al final, comparando el valor original de cada fila con el
        # que quedo tras el punto fijo.  ``transitions`` es aparte y solo mide
        # cuanto costo converger (una fila se mueve varias veces mientras la
        # correccion sube por la cadena).
        counts = {'scanned': 0, 'raised': 0, 'lowered': 0, 'cleared': 0,
                  'named': 0, 'witness_only': 0, 'frozen': 0,
                  'rows_changed': 0, 'transitions': 0}
        # Valor vigente de lo que se va a escribir.  Se mantiene en memoria
        # durante TODAS las pasadas (tambien fuera de --dry-run) y solo se
        # persiste al final: si el punto fijo no llega, la base se queda como
        # estaba en vez de con numeros a medio relajar.
        overlay = {}
        origin = {}         # key -> (mate_in original, etiqueta legible)
        climbs = {}         # key -> pasadas CONSECUTIVAS subiendo
        flips = {}          # key -> pasadas TOTALES cambiando de valor
        frozen = set()
        passes = 0
        unstable = False
        # Pasada 1: el conjunto cerrado entero.  Despues, solo la FRONTERA:
        # una fila depende de sus hijos, asi que solo pueden moverse los
        # padres de lo que acaba de moverse (o de congelarse: sus padres
        # derivaban del valor que se retiro del overlay).  Una correccion
        # que sube una cadena de 60 plies cuesta 60 pasadas baratas en vez
        # de 60 barridos completos de la tabla.
        frontier = None

        while passes < max_passes:
            passes += 1
            pending, scanned = self._one_pass(
                batch_size, overlay, frozen, origin, keys=frontier)
            if passes == 1:
                counts['scanned'] = scanned
            self.stdout.write(
                f'pass {passes}: {len(pending)} row(s) to change '
                f'over {scanned} closed position(s)')
            if not pending:
                break
            counts['transitions'] += len(pending)
            for key, (new_mate, _move) in pending.items():
                previous = (overlay[key][0] if key in overlay
                            else origin[key][0])
                if (new_mate is not None and previous is not None
                        and new_mate > previous):
                    climbs[key] = climbs.get(key, 0) + 1
                else:
                    climbs[key] = 0          # se asento: no es un ciclo
                flips[key] = flips.get(key, 0) + 1
                if climbs[key] >= max_climbs or flips[key] >= max_flips:
                    # Ciclo sin suelo (sube un ply por pasada) u oscilacion
                    # (cambia sin parar sin subir en monotono, invisible para
                    # el contador de subidas).  Se congela en lo que la base
                    # tiene hoy — no se empeora una fila que no sabemos
                    # leer — y se reporta para mirarla a mano.
                    frozen.add(key)
                    overlay.pop(key, None)
            overlay.update({key: value for key, value in pending.items()
                            if key not in frozen})
            frontier = self._parents_of(set(pending), batch_size)
        else:
            unstable = True

        samples = self._classify(overlay, origin, counts, sample)
        counts['frozen'] = len(frozen)
        counts['rows_changed'] = len(overlay)
        orphans = Position.objects.filter(
            status='UNKNOWN', mate_in__isnull=False).count()
        overflow = bool(max_write and len(overlay) > max_write)
        written, skipped = 0, 0
        if overlay and not dry_run and not unstable and not overflow:
            written, skipped = self._write(overlay, origin)

        for kind in ('raised', 'lowered', 'cleared', 'named'):
            for line in samples[kind]:
                self.stdout.write(f'{kind.upper()} {line}')
        for key in sorted(frozen)[:sample]:
            self.stdout.write(f'FROZEN {key} (no fixed point; left as stored)')
        self.stdout.write(
            'backfill_mate_distance: '
            + ' '.join(f'{name}={value}' for name, value in counts.items())
            + f' passes={passes} written={written} skipped={skipped}'
            + f' dry_run={bool(dry_run)}'
            + f' fixed_point={not unstable}')
        if orphans:
            self.stdout.write(
                f'backfill_mate_distance: {orphans} UNKNOWN row(s) still '
                'carry a mate_in (revocation leftovers, not touched here)')
        if unstable:
            self.stderr.write(
                f'backfill_mate_distance: no fixed point in {max_passes} '
                'pass(es); NOTHING written. Raise --max-passes or look at '
                'the graph.')
        if overflow:
            self.stderr.write(
                f'backfill_mate_distance: {len(overlay)} row(s) exceed '
                f'--max-write {max_write}; NOTHING written.')
        if written:
            DBEvent.objects.create(kind='MATE_DISTANCE_BACKFILL', payload={
                key: value for key, value in counts.items()
            } | {'passes': passes, 'written': written,
                 'orphan_unknown_mate_in': orphans,
                 'frozen_sample': sorted(frozen)[:16]})

    # ---------------- una pasada completa ----------------

    def _one_pass(self, batch_size, overlay, frozen, origin, keys=None):
        """Devuelve ``({key: (mate_in, best_move)}, filas_leidas)``.

        Con ``keys`` (la frontera) solo se relee ese subconjunto; los filtros
        de cierre son los mismos que en el barrido completo.  ``live`` es la
        vista Gauss-Seidel: lo recien calculado se usa YA dentro de la misma
        pasada (cuando el orden de claves acompaña, una cadena entera colapsa
        de golpe), mientras que ``overlay`` — la referencia contra la que el
        llamante cuenta subidas y congela — no se toca desde aqui."""
        pending = {}
        scanned = 0
        live = dict(overlay)
        if keys is None:
            chunks = (None,)
        else:
            ordered = sorted(keys)
            chunks = tuple(ordered[i:i + batch_size]
                           for i in range(0, len(ordered), batch_size))
        for chunk in chunks:
            after = ''
            while True:
                base = Position.objects.filter(
                    status__in=DECISIVE, key__gt=after,
                ).exclude(closure='TERMINAL')
                if chunk is not None:
                    base = base.filter(key__in=chunk)
                rows = list(base.order_by('key').values(
                    'key', 'fen', 'status', 'closure', 'proof', 'won_line',
                    'mate_in', 'best_move')[:batch_size])
                if not rows:
                    break
                after = rows[-1]['key']
                scanned += len(rows)
                edges = self._edges_by_parent([row['key'] for row in rows])
                for row in rows:
                    raw_edges = edges.get(row['key'])
                    if not raw_edges or row['key'] in frozen:
                        continue    # sin hijos no hay nada que derivar
                    out_edges = [
                        e if e.child.key not in live else
                        _Edge(e.move_uci, _Child(
                            e.child.key, e.child.status, e.child.closure,
                            e.child.proof, live[e.child.key][0]))
                        for e in raw_edges]
                    current = live.get(row['key'],
                                       (row['mate_in'], row['best_move']))
                    node = _Node(row, current[0], current[1])
                    new_mate, new_move = ingest.mate_distance_refresh(
                        node, out_edges)
                    if (new_mate, new_move) == current:
                        continue
                    pending[row['key']] = (new_mate, new_move)
                    live[row['key']] = (new_mate, new_move)
                    # La FOTO con la que se leyo la fila, no solo su etiqueta:
                    # al final se escribe filtrando por ella, asi que hacen
                    # falta las dos columnas que se van a pisar.
                    origin.setdefault(row['key'], (
                        row['mate_in'], row['best_move'],
                        f"{row['key'][:16]} {row['status']} {row['closure']} "
                        f"| {row['fen']}"))
        return pending, scanned

    def _parents_of(self, keys, batch_size):
        """Frontera de la siguiente pasada: padres CERRADOS de lo movido."""
        parents = set()
        ordered = sorted(keys)
        for i in range(0, len(ordered), batch_size):
            parents.update(Edge.objects.filter(
                child_id__in=ordered[i:i + batch_size],
            ).values_list('parent_id', flat=True))
        return parents

    def _classify(self, overlay, origin, counts, sample):
        """Cuentas y muestras del cambio NETO de cada fila superviviente."""
        samples = {'raised': [], 'lowered': [], 'cleared': [], 'named': []}
        for key, (new, _move) in overlay.items():
            old, _stored_move, label = origin[key]
            if old == new:
                counts['witness_only'] += 1
                continue
            if old is None:
                kind = 'named'
            elif new is None:
                kind = 'cleared'
            elif new > old:
                kind = 'raised'
            else:
                kind = 'lowered'
            counts[kind] += 1
            if len(samples[kind]) < sample:
                head, _, fen = label.partition('|')
                samples[kind].append(f'{head}mate_in {old} -> {new} |{fen}')
        return samples

    def _edges_by_parent(self, keys):
        """Hijos con su valor ALMACENADO: el vigente lo pone la vista live."""
        by_parent = {}
        for parent, move, child, status, closure, proof, mate_in in (
                Edge.objects.filter(parent_id__in=keys).values_list(
                    'parent_id', 'move_uci', 'child_id', 'child__status',
                    'child__closure', 'child__proof', 'child__mate_in')):
            by_parent.setdefault(parent, []).append(
                _Edge(move, _Child(child, status, closure, proof, mate_in)))
        return by_parent

    def _write(self, pending, origin):
        """Persiste el punto fijo SIN pisar lo que se movio mientras tanto.

        Entre la lectura y esta escritura pueden pasar muchas pasadas largas, y
        ``backup_cascade`` esta escribiendo ``mate_in``/``best_move`` en vivo
        sobre las mismas filas.  Un ``bulk_update`` ciego devolvia esas filas al
        valor derivado de una foto vieja.  Se escribe con la misma disciplina
        que ``verify_mates``: un UPDATE por fila FILTRADO por la foto de
        origen, y la que ya no encaje se queda como esta — su valor lo puso
        alguien con informacion mas fresca que la nuestra.

        Devuelve ``(escritas, saltadas)``.
        """
        now = timezone.now()
        written, skipped = 0, 0
        with atomic():
            for key, (mate_in, best_move) in pending.items():
                stored_mate, stored_move, _label = origin[key]
                changed = Position.objects.filter(
                    key=key, mate_in=stored_mate, best_move=stored_move,
                ).update(mate_in=mate_in, best_move=best_move, updated=now)
                if changed:
                    written += 1
                else:
                    skipped += 1
        return written, skipped
