"""Cierra las peticiones PENDING a las que ya no les queda trabajo que comprar.

Reporte de comunidad: peticiones que "are stuck forever in your queue on
profile description... if you click them, position is already analysed".  Dos
tareas sobre la misma posicion — la del selector y la de un click, o dos
clicks a profundidades distintas — y solo una llega a un worker: la otra se
quedaba PENDING para siempre, porque nada en el ciclo de vida miraba a las
demas cuando una entregaba su resultado.

Son DOS clases de zombi, con dos guardianes distintos en el codigo vivo, y
esta pasada es la unica para lo que se quedo colgado antes de que existieran:

* SOMBREADA: su posicion ya tiene una tarea COMPLETED con presupuesto igual o
  mayor.  El criterio es el de la fila y no el de un pase concreto.  Lo que
  pide mas hondo que todo lo completado sigue en cola, que es lo que su autor
  encargo.  En vivo lo cubre ``ingest_queue._absorb_shadowed``.
* SOBRE POSICION CERRADA: su posicion ya no esta en 'UNKNOWN'.  El selector
  salta lo que no esta abierto (``views.choose_pending``), asi que no la sirve
  nadie nunca; y la absorcion por analisis solo dispara cuando un analisis
  ATERRIZA en esa posicion, que es justo lo que ya no puede pasar.  Una
  posicion cerrada por PROPAGACION nunca vio un motor — 30-jul, las mas viejas
  del dueno con ``eval_cp=-9999`` y ``nodes_invested=0`` — asi que tampoco
  tiene un COMPLETED que las sombree: por eso hacen falta las dos preguntas y
  no una.  En vivo lo cubre ``ingest._emit_closure_events``.

Lo que NO se toca: las LAPIDAS.  Una posicion con ``priority <= DEAD/2``
(§ ingest._still_reachable) esta fuera del SELECTOR, que es quien crea tareas
nuevas — ``next_tasks`` filtra ``priority__gt=DEAD/2`` — pero sigue en
'UNKNOWN' y ``choose_pending`` no mira la prioridad en ningun sitio: una tarea
que ya existe sobre una lapida SE ARRIENDA igual que cualquier otra.  No es un
zombi, es una peticion que su autor todavia puede cobrar, y absorberla seria
inventarse un cierre.

Absorber es COMPLETED sin nodos y sin maquina, y ``LEASED`` no entra: el como
vive en ``ingest.absorb_tasks``, que es el mismo que usan los dos guardianes
en vivo.  Idempotente: una segunda pasada no encuentra ninguna.
"""

from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef

from atomicdb.ingest import absorb_tasks
from atomicdb.models import AnalysisTask


class Command(BaseCommand):
    help = ('Cierra las peticiones PENDING que ya no pueden comprar nada: '
            'sombreadas por un analisis mas hondo, o sobre posicion cerrada')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Cuenta lo que absorberia, sin escribir nada.')

    def handle(self, *args, **options):
        pending = AnalysisTask.objects.filter(
            state=AnalysisTask.TState.PENDING)
        # Las dos clases van DISJUNTAS: una fila puede cumplir las dos
        # condiciones, y un total que la sume dos veces no es un total.  La
        # frontera es el estado de la posicion, asi que se corta ahi.
        closed = pending.exclude(position__status='UNKNOWN')
        # La subconsulta correlacionada pregunta por fila "hay algo COMPLETED
        # aqui que ya cubra mi presupuesto?", que es justo la pregunta que
        # nadie hacia.
        shadowed = pending.filter(position__status='UNKNOWN').filter(
            Exists(AnalysisTask.objects.filter(
                position_id=OuterRef('position_id'),
                state=AnalysisTask.TState.COMPLETED,
                budget_nodes__gte=OuterRef('budget_nodes'))))
        if options['dry_run']:
            self.stdout.write(
                'absorb_shadowed_tasks: %d sombreadas + %d sobre posicion '
                'cerrada por absorber (dry-run, sin escribir)'
                % (shadowed.count(), closed.count()))
            return
        # El orden da igual: el filtro de 'UNKNOWN' las hace disjuntas, asi
        # que ninguna de las dos escrituras mueve el conjunto de la otra.
        closed_rows = absorb_tasks(closed)
        shadowed_rows = absorb_tasks(shadowed)
        self.stdout.write(self.style.SUCCESS(
            'absorb_shadowed_tasks: %d sombreadas + %d sobre posicion cerrada '
            'absorbidas' % (shadowed_rows, closed_rows)))
