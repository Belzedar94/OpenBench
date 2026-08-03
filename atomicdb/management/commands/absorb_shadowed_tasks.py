"""Cierra las peticiones PENDING que otro analisis ya dejo sin trabajo.

Reporte de comunidad: peticiones que "are stuck forever in your queue on
profile description... if you click them, position is already analysed".  Dos
tareas sobre la misma posicion — la del selector y la de un click, o dos
clicks a profundidades distintas — y solo una llega a un worker: la otra se
quedaba PENDING para siempre, porque nada en el ciclo de vida miraba a las
demas cuando una entregaba su resultado.

``ingest_queue._absorb_shadowed`` ya las absorbe en el momento de aplicar el
analisis.  Esto es la pasada UNICA para las que se quedaron colgadas antes de
que esa regla existiera, y por eso el criterio es el de la fila y no el de un
pase concreto: se absorbe la PENDING cuya posicion tenga YA una tarea
COMPLETED con presupuesto igual o mayor que el suyo.  Lo que pide mas hondo
que todo lo completado sigue en cola, que es lo que su autor encargo.

Absorber es COMPLETED sin nodos y sin maquina: el trabajo se hizo, pero no lo
hizo esta fila y no puede cobrarlo (los agregados por maquina filtran
``machine__in``, asi que una fila sin maquina cae fuera de la contabilidad
sola).  Idempotente: una segunda pasada no encuentra ninguna.
"""

from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef
from django.utils import timezone

from atomicdb.models import AnalysisTask


class Command(BaseCommand):
    help = ('Cierra las peticiones PENDING cuya posicion ya tiene un analisis '
            'igual de hondo o mas')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Cuenta lo que absorberia, sin escribir nada.')

    def handle(self, *args, **options):
        # Una sola sentencia: la subconsulta correlacionada pregunta por fila
        # "hay algo COMPLETED aqui que ya cubra mi presupuesto?", que es
        # justo la pregunta que nadie hacia.
        shadowed = AnalysisTask.objects.filter(
            state=AnalysisTask.TState.PENDING,
        ).filter(Exists(AnalysisTask.objects.filter(
            position_id=OuterRef('position_id'),
            state=AnalysisTask.TState.COMPLETED,
            budget_nodes__gte=OuterRef('budget_nodes'))))
        if options['dry_run']:
            self.stdout.write('absorb_shadowed_tasks: %d por absorber '
                              '(dry-run, sin escribir)' % shadowed.count())
            return
        absorbed = shadowed.update(state=AnalysisTask.TState.COMPLETED,
                                   completed=timezone.now(), nodes_searched=0)
        self.stdout.write(self.style.SUCCESS(
            'absorb_shadowed_tasks: %d absorbidas' % absorbed))
