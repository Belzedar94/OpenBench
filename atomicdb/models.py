"""AtomicDB: arbol persistente de resolucion practica de Atomic.

Spec: Atomic Project/Atomic-Stockfish-solving-docs/docs/atomic/solving/
atomicdb-tier1-spec.md. Disciplina central: eval (heuristico) y status
(exacto) nunca se mezclan — eval ordena la exploracion, status cierra.
"""

from django.core.exceptions import ValidationError
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


class Proof(models.TextChoices):
    ANDOR    = 'ANDOR'     # mate forzado verificado exhaustivamente
    ENGINE   = 'ENGINE'    # witness legal; certificacion agoto su presupuesto
    DISPUTED = 'DISPUTED'  # busqueda exhaustiva contradijo el witness


class Position(models.Model):
    key       = models.CharField(max_length=64, primary_key=True)  # sha256 hex
    fen       = models.TextField()                                 # canonica (sin contadores)
    eval_cp   = models.IntegerField(null=True)                     # perspectiva blanca
    status    = models.CharField(max_length=10, choices=Status.choices,
                                 default=Status.UNKNOWN, db_index=True)
    closure   = models.CharField(max_length=8, choices=Closure.choices, null=True)
    proof     = models.CharField(max_length=8, choices=Proof.choices, null=True)
    best_move = models.CharField(max_length=8, null=True)          # uci, heuristica
    won_line  = models.TextField(null=True)   # PV verificada del cierre (testigo)
    mate_in   = models.IntegerField(null=True)  # plies hasta mate, linea probada mas corta
    last_analysis = models.JSONField(null=True)  # raw MultiPV del ultimo analisis
    expanded  = models.BooleanField(default=False)                 # aristas completas creadas
    depth_invested = models.IntegerField(default=0)
    nodes_invested = models.BigIntegerField(default=0)
    time_invested  = models.FloatField(default=0.0)   # segundos de motor acumulados
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
    nodes_searched = models.BigIntegerField(default=0)  # nodos REALES buscados
    elapsed_seconds = models.FloatField(default=0.0)  # tiempo reportado por el motor
    multipv      = models.IntegerField(default=5)
    generation   = models.IntegerField(default=0)   # visita n-esima (escalera)
    source       = models.CharField(max_length=4, choices=Source.choices,
                                    default=Source.AUTO, db_index=True)
    state        = models.CharField(max_length=10, choices=TState.choices,
                                    default=TState.PENDING, db_index=True)
    machine      = models.CharField(max_length=64, default='')
    leased_at    = models.DateTimeField(null=True)
    # Separate keepalive preserves the immutable assignment timestamp while
    # preventing a healthy multi-hour search from being leased a second time.
    lease_heartbeat_at = models.DateTimeField(null=True)
    # Opaque fencing token for builds that understand assignment identities.
    # Blank remains the compatibility marker for leases issued to old workers.
    lease_token = models.CharField(max_length=64, default='')
    # Stable for one worker process. It makes a lost lease HTTP response
    # replayable without letting a different same-machine process steal it.
    lease_session = models.CharField(max_length=64, default='')
    attempts     = models.IntegerField(default=0)
    created      = models.DateTimeField(auto_now_add=True)
    completed    = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['position', 'generation'], name='uniq_task_per_generation')]
        indexes = [models.Index(fields=['state', 'completed'],
                                name='atomic_task_state_done')]


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
    current_task_id = models.BigIntegerField(null=True)
    last_nps   = models.BigIntegerField(default=0)
    nps_updated = models.DateTimeField(null=True)
    last_seen  = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['machine', 'user'],
                                               name='uniq_worker_machine')]


class ProgressSnapshot(models.Model):
    """Append-only hourly observation for the future Progress view.

    These are cumulative counters observed at capture time, not reconstructed
    history and not deltas.  ``bucket_start`` is always the start of a UTC
    hour when written by ``capture_atomicdb_progress``.
    """

    bucket_start = models.DateTimeField(unique=True)
    captured_at = models.DateTimeField(auto_now_add=True)

    positions_total = models.BigIntegerField(default=0)
    positions_unknown = models.BigIntegerField(default=0)
    positions_closed = models.BigIntegerField(default=0)
    positions_expanded = models.BigIntegerField(default=0)

    engine_nodes_total = models.BigIntegerField(default=0)
    engine_seconds_total = models.FloatField(default=0.0)
    analyses_completed = models.BigIntegerField(default=0)

    tasks_pending = models.BigIntegerField(default=0)
    tasks_leased = models.BigIntegerField(default=0)
    tasks_retried = models.BigIntegerField(default=0)
    lease_retries_total = models.BigIntegerField(default=0)
    recorded_rejections_total = models.BigIntegerField(default=0)

    active_workers = models.BigIntegerField(default=0)
    active_threads = models.BigIntegerField(default=0)
    active_nps = models.BigIntegerField(default=0)

    closure_terminal = models.BigIntegerField(default=0)
    closure_tb = models.BigIntegerField(default=0)
    closure_mate_pv = models.BigIntegerField(default=0)
    closure_minimax = models.BigIntegerField(default=0)
    closure_unclassified = models.BigIntegerField(default=0)

    trust_verified = models.BigIntegerField(default=0)
    trust_andor = models.BigIntegerField(default=0)
    trust_engine = models.BigIntegerField(default=0)
    trust_disputed = models.BigIntegerField(default=0)
    trust_unclassified = models.BigIntegerField(default=0)

    class Meta:
        ordering = ['bucket_start']

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('ProgressSnapshot rows are append-only')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('ProgressSnapshot rows are append-only')

    def __str__(self):
        return f'AtomicDB progress {self.bucket_start.isoformat()}'
