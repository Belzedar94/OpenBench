"""AtomicDB: arbol persistente de resolucion practica de Atomic.

Spec: Atomic Project/Atomic-Stockfish-solving-docs/docs/atomic/solving/
atomicdb-tier1-spec.md. Disciplina central: eval (heuristico) y status
(exacto) nunca se mezclan — eval ordena la exploracion, status cierra.
"""

from django.db import models


class Status(models.TextChoices):
    UNKNOWN   = 'UNKNOWN'
    WHITE_WIN = 'WHITE_WIN'
    BLACK_WIN = 'BLACK_WIN'
    DRAW      = 'DRAW'


class Closure(models.TextChoices):
    TB      = 'TB'        # tablebase (fase posterior)
    MATE_PV = 'MATE_PV'   # PV de mate re-verificada jugada a jugada
    MINIMAX = 'MINIMAX'   # retropropagado desde hijos exactos
    TERMINAL= 'TERMINAL'  # la propia posicion es terminal (mate/ahogado/explosion)


class Position(models.Model):
    key       = models.CharField(max_length=64, primary_key=True)  # sha256 hex
    fen       = models.TextField()                                 # canonica (sin contadores)
    eval_cp   = models.IntegerField(null=True)                     # perspectiva blanca
    status    = models.CharField(max_length=10, choices=Status.choices,
                                 default=Status.UNKNOWN, db_index=True)
    closure   = models.CharField(max_length=8, choices=Closure.choices, null=True)
    best_move = models.CharField(max_length=8, null=True)          # uci, heuristica
    won_line  = models.TextField(null=True)   # PV verificada del cierre (testigo)
    last_analysis = models.JSONField(null=True)  # raw MultiPV del ultimo analisis
    expanded  = models.BooleanField(default=False)                 # aristas completas creadas
    depth_invested = models.IntegerField(default=0)
    nodes_invested = models.BigIntegerField(default=0)
    visits    = models.IntegerField(default=0)
    priority  = models.FloatField(default=0.0, db_index=True)      # selector (§4.1)
    campaign  = models.ForeignKey('Campaign', null=True, on_delete=models.SET_NULL,
                                  related_name='positions')
    updated   = models.DateTimeField(auto_now=True)


class Edge(models.Model):
    parent   = models.ForeignKey(Position, on_delete=models.CASCADE,
                                 related_name='edges_out')
    move_uci = models.CharField(max_length=8)
    child    = models.ForeignKey(Position, on_delete=models.PROTECT,
                                 related_name='edges_in')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['parent', 'move_uci'],
                                               name='uniq_parent_move')]
        indexes = [models.Index(fields=['child'])]


class Campaign(models.Model):
    name     = models.CharField(max_length=64, unique=True)
    root     = models.ForeignKey(Position, on_delete=models.PROTECT,
                                 related_name='campaign_roots')
    line_san = models.TextField(default='')      # para mostrar ("1.Nf3 f6 2.Nc3 Nh6")
    active   = models.BooleanField(default=True)
    created  = models.DateTimeField(auto_now_add=True)


class AnalysisTask(models.Model):
    class TState(models.TextChoices):
        PENDING   = 'PENDING'
        LEASED    = 'LEASED'
        COMPLETED = 'COMPLETED'

    class Source(models.TextChoices):
        AUTO = 'AUTO'   # selector best-first
        USER = 'USER'   # peticion publica: se sirve primero

    position     = models.ForeignKey(Position, on_delete=models.CASCADE)
    budget_nodes = models.BigIntegerField()
    multipv      = models.IntegerField(default=5)
    generation   = models.IntegerField(default=0)   # visita n-esima (escalera)
    source       = models.CharField(max_length=4, choices=Source.choices,
                                    default=Source.AUTO, db_index=True)
    state        = models.CharField(max_length=10, choices=TState.choices,
                                    default=TState.PENDING, db_index=True)
    machine      = models.CharField(max_length=64, default='')
    leased_at    = models.DateTimeField(null=True)
    attempts     = models.IntegerField(default=0)
    created      = models.DateTimeField(auto_now_add=True)
    completed    = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['position', 'generation'], name='uniq_task_per_generation')]


class DBEvent(models.Model):
    ts      = models.DateTimeField(auto_now_add=True, db_index=True)
    kind    = models.CharField(max_length=32)     # SUBTREE_CLOSED, WALL, CAMPAIGN...
    payload = models.JSONField(default=dict)


class RequestLog(models.Model):
    """Peticiones publicas de analisis (rate-limit y dedup por IP)."""
    ip       = models.GenericIPAddressField(db_index=True)
    position = models.ForeignKey(Position, on_delete=models.CASCADE)
    created  = models.DateTimeField(auto_now_add=True, db_index=True)


class WorkerPing(models.Model):
    """Presencia de workers de AtomicDB, para la pagina /machines/."""
    machine    = models.CharField(max_length=64)
    user       = models.CharField(max_length=64)
    threads    = models.IntegerField(default=0)
    hash_mb    = models.IntegerField(default=0)
    os         = models.CharField(max_length=64, default='')
    tasks_done = models.IntegerField(default=0)
    last_seen  = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['machine', 'user'],
                                               name='uniq_worker_machine')]
