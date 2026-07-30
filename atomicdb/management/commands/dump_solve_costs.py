"""Volcado CSV del dataset de COSTE DE RESOLVER (etiquetas censuradas).

PARA QUE
--------
``atomicdb/solve_estimate.py`` es un estimador HAND-CRAFTED: cuatro features y
cuatro pesos que alguien eligio.  Existe para que la puerta doble pueda
funcionar hoy, no para quedarse.  Lo que lo sustituira es un modelo entrenado
sobre lo que este arbol ya sabe — que posiciones acabaron cerrandose, cuanto
costo cerrarlas y cuales llevan meses tragando nodos sin cerrarse — y este
comando es el que produce esas filas.

LA ETIQUETA, Y POR QUE LA MITAD ESTA CENSURADA
----------------------------------------------
La pregunta que el modelo tiene que aprender es "cuanto cuesta cerrar esto".
Para una posicion CERRADA la respuesta esta medida:

    label = tamano del sub-DAG de prueba + nodos de motor acumulados en el

Las dos componentes viven en escalas muy distintas (los nodos de motor
dominan por varios ordenes de magnitud) y se suman a proposito en un solo
escalar, porque el consumidor es un regresor de coste y porque cada una cubre
un agujero de la otra: un nodo cerrado por TERMINAL o por tablebase gasto cero
nodos de motor y aun asi costo una posicion que hubo que visitar y cerrar, asi
que la etiqueta nunca es cero para algo que se cerro.

Para una posicion ABIERTA con esfuerzo invertido la respuesta NO se sabe — solo
se sabe que el coste real es MAYOR que lo ya gastado.  Eso es una observacion
CENSURADA POR LA DERECHA, no un dato que falte y no un dato que sobre: tirarla
sesgaria el dataset hacia lo que resulto ser barato (las posiciones dificiles
son justo las que siguen abiertas) y tratarla como si fuera el coste final
mentiria a la baja.  Se emite con ``censored=1`` y ``label`` = cota inferior,
que es lo que un modelo de supervivencia sabe consumir.

Una posicion abierta SIN esfuerzo no dice nada de nadie y no se emite.

TOPE DE RECORRIDO
-----------------
El sub-DAG se recorre por niveles siguiendo aristas hacia hijos ya CERRADOS,
deduplicando por clave — es un DAG con transposiciones, y sin dedup un cono
ancho se contaria varias veces y uno con ciclo por transposicion no terminaria.
El recorrido se corta en ``--max-subdag`` posiciones; cuando se corta, la
etiqueta deja de ser el coste completo y pasa a ser una cota inferior, asi que
la fila sale marcada ``censored=1``.  El tope es una decision de coste, y
convertirlo en censura es lo que impide que se convierta en una mentira.

MEMORIA
-------
Nada de ``list(Position.objects.all())``.  El recorrido va por ``iterator``
con lotes, y el numero de aristas de cada fila — que el estimador necesita y
que contar una a una serian millones de consultas — se resuelve con UNA
sentencia agrupada por lote.
"""

import csv
from itertools import islice

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q

from atomicdb import solve_estimate
from atomicdb.models import Edge, Position

COLUMNS = ('key', 'fen', 'eval_cp', 'visits', 'nodes_invested', 'annoyance',
           'label', 'censored')

DEFAULT_CHUNK = 500
DEFAULT_MAX_SUBDAG = 5_000
# Tope de claves por sentencia ``IN``.  SQLite tiene un limite de variables
# por sentencia y un lote grande lo cruzaria sin avisar; ademas mantiene los
# planes de consulta pequenos en Postgres.
QUERY_BATCH = 400


def edge_counts(keys):
    """``{clave: numero de aristas salientes}`` en una sentencia por lote."""
    counts = {}
    keys = list(keys)
    for start in range(0, len(keys), QUERY_BATCH):
        rows = (Edge.objects.filter(parent_id__in=keys[start:start + QUERY_BATCH])
                .values('parent_id').annotate(total=Count('id'))
                .values_list('parent_id', 'total'))
        counts.update(dict(rows))
    return counts


def closed_subdag(key, own_nodes, max_nodes=DEFAULT_MAX_SUBDAG):
    """``(tamano, nodos_de_motor, truncado)`` del sub-DAG cerrado bajo ``key``.

    Solo se desciende por hijos ya cerrados: un hijo abierto no forma parte de
    la prueba que se acabo pagando.  ``key`` cuenta como una posicion y sus
    propios nodos entran en el total, asi que una hoja cerrada devuelve
    ``(1, sus nodos, False)``.
    """
    seen = {key}
    total = int(own_nodes or 0)
    frontier = [key]
    truncated = False
    while frontier and not truncated:
        following = []
        for start in range(0, len(frontier), QUERY_BATCH):
            rows = (Edge.objects
                    .filter(parent_id__in=frontier[start:start + QUERY_BATCH])
                    .exclude(child__status='UNKNOWN')
                    .values_list('child_id', 'child__nodes_invested'))
            for child_id, invested in rows:
                if child_id in seen:
                    continue
                if len(seen) >= max_nodes:
                    truncated = True
                    break
                seen.add(child_id)
                total += int(invested or 0)
                following.append(child_id)
            if truncated:
                break
        frontier = following
    return len(seen), total, truncated


class Command(BaseCommand):
    help = ('Dump the solve-cost training set (closed = measured cost, '
            'open with effort = right-censored lower bound) as CSV.')

    def add_arguments(self, parser):
        parser.add_argument('path', help='Destination CSV file.')
        parser.add_argument(
            '--limit', type=int, default=0,
            help='Stop after this many rows (0 = every row).')
        parser.add_argument(
            '--chunk', type=int, default=DEFAULT_CHUNK,
            help='Rows held in memory at a time.')
        parser.add_argument(
            '--max-subdag', type=int, default=DEFAULT_MAX_SUBDAG,
            help='Traversal cap per closed row; hitting it censors the label.')

    def handle(self, *args, **options):
        path = options['path']
        limit = max(0, int(options['limit']))
        chunk = max(1, min(QUERY_BATCH, int(options['chunk'])))
        max_subdag = max(1, int(options['max_subdag']))

        # Cerradas (cueste lo que cueste) y abiertas CON esfuerzo.  Una
        # abierta sin esfuerzo no aporta ni una cota inferior.
        rows = (Position.objects
                .filter(Q(nodes_invested__gt=0) | ~Q(status='UNKNOWN'))
                .order_by('key'))

        written = closed = censored = 0
        try:
            handle = open(path, 'w', newline='', encoding='utf-8')
        except OSError as error:
            raise CommandError(f'cannot write {path}: {error}')
        with handle:
            writer = csv.writer(handle)
            writer.writerow(COLUMNS)
            stream = rows.iterator(chunk_size=chunk)
            while True:
                batch = list(islice(stream, chunk))
                if not batch:
                    break
                counts = edge_counts(row.key for row in batch)
                for row in batch:
                    label, is_censored = self._label(row, max_subdag)
                    writer.writerow(self._row(row, counts, label, is_censored))
                    written += 1
                    closed += row.status != 'UNKNOWN'
                    censored += is_censored
                    if limit and written >= limit:
                        break
                if limit and written >= limit:
                    break

        self.stdout.write(f'wrote {written:,} rows to {path}')
        self.stdout.write(f'  closed (measured)  {closed:,}')
        self.stdout.write(f'  censored (bound)   {censored:,}')

    def _label(self, row, max_subdag):
        if row.status == 'UNKNOWN':
            # Cota inferior: lo gastado sin cerrar.
            return int(row.nodes_invested or 0), 1
        size, invested, truncated = closed_subdag(
            row.key, row.nodes_invested, max_subdag)
        return size + invested, 1 if truncated else 0

    def _row(self, row, counts, label, censored):
        annoyance = solve_estimate.annoyance(
            row, branching=counts.get(row.key))
        return [row.key, row.fen,
                '' if row.eval_cp is None else row.eval_cp,
                row.visits, row.nodes_invested, f'{annoyance:.4f}',
                label, censored]
