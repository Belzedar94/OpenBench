"""Siembra ``Position.reachable`` con un BFS desde la raiz, por lotes.

POR QUE EXISTE.  La columna nace en False para todas las filas que ya estaban
(§ migracion 0036), y sin marcar NO significa "barato": significa el precio
del cajetin FEN suelto, que es el GENEROSO.  Encender ``ATOMICDB_SELECTOR_V2``
sobre una base sin sembrar le pondria ese precio a media base.  Esta pasada es
el paso intermedio obligatorio entre la migracion y el conmutador.

LA MEMORIA, QUE ES EL PUNTO DE TODO ESTO.  El BFS no lleva un conjunto de
visitados en RAM: la MARCA es el conjunto de visitados.  Cada lote pregunta a
la base cuales de los hijos siguen sin marcar, los marca, y esos — y solo esos
— son la frontera siguiente.  Una fila entra en la frontera como mucho una vez,
asi que termina; y lo unico que vive en memoria es un nivel, no el grafo.  Es
la misma disciplina que el selector acotado: ``values_list`` por lotes y nada
de instancias ORM completas.

POR ESO EMPIEZA BORRANDO, que es la parte que sorprende.  Si la marca es el
conjunto de visitados, una marca PREVIA — la que ``expand`` fue dejando ply a
ply desde que se aplico la migracion — para el recorrido en seco: los hijos del
primer nivel ya estan marcados, el filtro "dame los que faltan" no devuelve
ninguno, y el BFS se cree terminado en la frontera equivocada.  Asi que esto no
parchea la columna, la RECALCULA: apaga lo que hubiera y vuelve a caminar desde
la raiz.  El borrado es un solo ``UPDATE`` sobre las filas ya marcadas, sin
memoria de por medio.

QUE SIGNIFICA "IDEMPOTENTE" AQUI: el RESULTADO es el mismo, no la escritura.
Una segunda pasada vuelve a marcar el mismo conjunto, y por eso repara lo que
``expand`` no puede — propaga UN ply, asi que una transposicion que aterrice
sobre un subarbol que nacio suelto deja su interior sin marcar hasta que esto
vuelva a correr.

CUANDO CORRERLO: con el conmutador ``ATOMICDB_SELECTOR_V2`` apagado, o
sabiendo que durante la pasada la periferia cobra precio de cajetin suelto.  El
apagon de la marca dura lo que dura el recorrido y no deja la base peor que
antes de la migracion, pero no es transparente y no conviene fingir que lo es.

UNA CLAVE SIN FILA SE SALTA.  Una arista puede apuntar a una posicion que
todavia no esta visible (la carrera que mata al Dijkstra v1); aqui no es un
caso especial siquiera: el filtro ``key__in`` devuelve lo que existe, y lo que
no existe lo recogera la pasada siguiente.
"""

import json

from django.core.management.base import BaseCommand

from atomicdb import ingest
from atomicdb.database import connection
from atomicdb.models import Edge, Position


class Command(BaseCommand):
    help = 'Seed Position.reachable with a batched BFS from the root.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--batch-size', type=int, default=500,
            help='Keys per query and per bulk_update (default: 500).  Es el '
                 'tope de parametros de un IN, no una preferencia: hay bases '
                 'que se plantan mucho antes de lo que uno cree.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report how many rows WOULD be marked, without writing.  '
                 'Ojo: sin la columna como marca de visitado, el recuento se '
                 'paga con un conjunto de claves en RAM proporcional a la '
                 'parte alcanzable del grafo.  Es una medida puntual, no la '
                 'pasada.')

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        if batch_size <= 0:
            raise ValueError('--batch-size must be positive')

        connection.ensure_connection()
        if connection.vendor == 'sqlite':
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA busy_timeout = 30000')

        root = ingest.root_key()
        report = {'root': root, 'dry_run': bool(options['dry_run'])}
        if not Position.objects.filter(key=root).exists():
            # Sin raiz no hay nada que sembrar y tampoco hay nada roto: es una
            # base recien creada.  Decirlo y salir en cero.
            report.update(root=None, marked=0, cleared=0)
            self.stdout.write(json.dumps(report, sort_keys=True))
            return

        if options['dry_run']:
            # ``reachable_nodes`` es lo que la pasada de verdad marcaria (todo,
            # porque empieza borrando); ``unmarked_now`` es cuantos de esos no
            # lo estan ya, que es la cifra que dice si vale la pena lanzarla.
            nodes, unmarked = self._dry_run(root, batch_size)
            report.update(reachable_nodes=nodes, unmarked_now=unmarked)
        else:
            cleared, marked = self._seed(root, batch_size)
            report.update(cleared=cleared, marked=marked)
        report['unreachable_rows_left'] = (
            Position.objects.filter(reachable=False).count())
        self.stdout.write(json.dumps(report, sort_keys=True))

    # ---------------- la pasada de verdad ----------------

    def _seed(self, root, batch_size):
        """BFS usando la columna como conjunto de visitados.

        Devuelve (filas desmarcadas al empezar, filas marcadas al terminar).
        La segunda cifra ES el tamano de la parte alcanzable del grafo, raiz
        incluida; la primera dice cuanto habia antes y solo sirve para leer el
        parte con contexto.
        """
        # Un solo ``UPDATE`` sobre lo que estuviera marcado: sin esto, la marca
        # que ``expand`` fue dejando desde la migracion hace de muro y el
        # recorrido se para en el primer nivel (ver la cabecera del modulo).
        cleared = Position.objects.filter(reachable=True).update(
            reachable=False)
        frontier = [root]
        marked = self._mark(frontier, batch_size)
        while frontier:
            fresh = set()
            for chunk in _chunks(frontier, batch_size):
                for kids in _chunks(sorted(self._children_of(chunk)),
                                    batch_size):
                    # El mismo hijo cuelga de varios padres del mismo nivel;
                    # sin el conjunto, una transposicion ancha duplica la
                    # frontera siguiente y con ella la memoria del nivel.
                    fresh.update(
                        Position.objects.filter(key__in=kids, reachable=False)
                        .values_list('key', flat=True))
            frontier = sorted(fresh)
            marked += self._mark(frontier, batch_size)
        return cleared, marked

    def _mark(self, keys, batch_size):
        """``reachable=True`` sobre shells, en lotes: nada de instancias."""
        written = 0
        for chunk in _chunks(list(keys), batch_size):
            Position.objects.bulk_update(
                [Position(key=key, reachable=True) for key in chunk],
                ['reachable'])
            written += len(chunk)
        return written

    # ---------------- la medida ----------------

    def _dry_run(self, root, batch_size):
        """Lo mismo, pero con el conjunto de visitados en RAM.

        Sin escribir no hay marca, y sin marca el BFS necesita recordar por
        donde paso.  Es el unico sitio de todo esto que crece con el grafo, y
        esta aqui a sabiendas: un ``--dry-run`` se lanza a mano, una vez, para
        decidir si lanzar la pasada de verdad.
        """
        seen = {root}
        frontier = [root]
        while frontier:
            fresh = []
            for chunk in _chunks(frontier, batch_size):
                for kid in self._children_of(chunk):
                    if kid not in seen:
                        seen.add(kid)
                        fresh.append(kid)
            frontier = fresh
        # Una clave del recorrido puede no tener FILA (una arista que apunta a
        # una posicion que aun no se ve).  No se cuenta: la pasada de verdad
        # tampoco la marcaria, y un dry-run que prometa mas de lo que la pasada
        # entrega no sirve para decidir nada.
        existing = unmarked = 0
        for chunk in _chunks(sorted(seen), batch_size):
            rows = list(Position.objects.filter(key__in=chunk)
                        .values_list('reachable', flat=True))
            existing += len(rows)
            unmarked += sum(1 for marked in rows if not marked)
        return existing, unmarked

    # ---------------- adyacencia ----------------

    def _children_of(self, parents):
        return set(Edge.objects.filter(parent_id__in=parents)
                   .values_list('child_id', flat=True))


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]
