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
    SOLVE   = 'SOLVE'     # certificado df-pn reproducido ENTERO en el servidor


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
    # --- valor RESPALDADO (negamax de lo que el subarbol ya sabe) ---
    # eval_cp es la eval ALMACENADA de esta posicion: la que dejo su ultimo
    # analisis, refinada en el sitio por la cascada heredada cuando el nodo
    # esta completamente expandido.  backed_eval es un valor DERIVADO de los
    # hijos (§ ingest.backup_backed_evals), con guardas de cobertura y de
    # calidad, y vive aparte para no pisar nada.  Perspectiva BLANCA, igual
    # que eval_cp; el unico flip a la del que mueve ocurre al pintar.
    backed_eval  = models.IntegerField(null=True)      # negamax de los hijos
    backed_move  = models.CharField(max_length=8, null=True)  # arista que lo respalda
    backed_plies = models.IntegerField(default=0)      # plies por debajo del origen
    backed_nodes = models.BigIntegerField(default=0)   # calidad (nodos) del respaldo
    won_line  = models.TextField(null=True)   # PV verificada del cierre (testigo)
    mate_in   = models.IntegerField(null=True)  # plies hasta mate, linea probada mas corta
    # Reloj de entrada MAXIMO para el que este cierre decisivo sigue valiendo,
    # en plies del contador de 50 (0..100).  ``canonical_fen`` pone los
    # contadores a cero, asi que un cierre probado aqui lo esta "desde reloj
    # cero"; llegar a la misma posicion con reloj c solo es seguro si
    # c <= clock_slack.  Solo se rellena en cierres DECISIVOS: las tablas son
    # monotonas en el reloj (subirlo solo degrada victorias hacia tablas) y no
    # necesitan slack.  NULL = no medido, que se trata como cero.
    clock_slack = models.SmallIntegerField(null=True)
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
    # Procedencia del analisis, opcional y ADITIVA: un worker antiguo no la
    # manda y no pasa nada.  Una eval no puede envenenar la VERDAD (solo los
    # cierres exactos lo hacen), pero si puede envenenar la LIVENESS: un build
    # roto que devuelve +3000 legales redirige el selector durante meses.  Con
    # el sha del binario y el de la red, ese sesgo es atribuible.
    engine_sha   = models.CharField(max_length=64, blank=True, default='')
    net_sha      = models.CharField(max_length=64, blank=True, default='')
    created      = models.DateTimeField(auto_now_add=True)
    completed    = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['position', 'generation'], name='uniq_task_per_generation')]
        indexes = [models.Index(fields=['state', 'completed'],
                                name='atomic_task_state_done')]


class ProofCampaign(models.Model):
    """Una PRUEBA en curso: raiz, objetivo y politica. No es el DAG.

    El DAG universal (``Position``/``Edge``) es una cache de posiciones: evals
    compartidas por transposicion y hechos exactos reutilizables.  Los numeros
    de prueba NO son propiedad de una posicion — dependen de que se intenta
    demostrar, desde que raiz y bajo que politica de repertorio — asi que viven
    aqui y en ``ProofNode``, en tablas propias, y el DAG se queda como lo que
    es.  Dos campanas pueden mirar la misma posicion con pn/dn distintos sin
    contradecirse.
    """

    class Goal(models.TextChoices):
        WHITE_WIN = 'WHITE_WIN'
        BLACK_WIN = 'BLACK_WIN'

    name = models.CharField(max_length=64, unique=True)
    root = models.ForeignKey(Position, on_delete=models.PROTECT,
                             related_name='proof_campaigns')
    goal = models.CharField(max_length=10, choices=Goal.choices,
                            default=Goal.WHITE_WIN)
    active = models.BooleanField(default=True, db_index=True)
    # Sube cuando cambian las recurrencias o la inicializacion de hojas: los
    # pn/dn calculados con otra version no son comparables con estos.
    algorithm_version = models.IntegerField(default=1)
    # Repertorio BLANDO (no una restriccion de la prueba): que fraccion de las
    # bajadas sigue la eleccion primaria, cual la de respaldo y cual explora.
    # Una eleccion fija que resulte ser una trampa de fortaleza solo demuestra
    # "esta estrategia fallo", nunca "el atacante no gana".
    repertoire_policy = models.JSONField(default=dict, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.name} ({self.goal})'


class ProofNode(models.Model):
    """pn/dn de UNA posicion dentro de UNA campana.

    ``pn`` = esfuerzo estimado para PROBAR el objetivo desde aqui, ``dn`` para
    REFUTARLO.  Ambos saturan en ``proof.PROOF_INFINITY`` (2^62), que es el
    infinito de la aritmetica: cabe holgadamente en un BigInteger y sumar dos
    infinitos sigue sin desbordar en Python antes del clamp.
    """

    campaign = models.ForeignKey(ProofCampaign, on_delete=models.CASCADE,
                                 related_name='nodes')
    position = models.ForeignKey(Position, on_delete=models.CASCADE,
                                 related_name='proof_nodes')
    pn = models.BigIntegerField(default=1)
    dn = models.BigIntegerField(default=1)
    # El nodo tiene hijos MATERIALIZADOS que la prueba ya considera; distinto
    # de ``Position.expanded``, que habla del DAG universal.
    expanded_in_proof = models.BooleanField(default=False)
    selected_child = models.CharField(max_length=8, null=True, blank=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['campaign', 'position'], name='uniq_proof_node')]
        indexes = [
            models.Index(fields=['campaign', 'pn'], name='atomic_proof_pn'),
            models.Index(fields=['campaign', 'dn'], name='atomic_proof_dn'),
        ]

    def __str__(self):
        return f'{self.position_id[:12]} pn={self.pn} dn={self.dn}'


class IngestJob(models.Model):
    """Cola durable de resultados de worker pendientes de aplicar al arbol.

    Un submit valida lo barato (identidad, lease, formato), RECLAMA la tarea y
    deja aqui el payload crudo, todo en el mismo commit.  El trabajo caro
    (pruebas de mate, expansion, aristas, cascadas) lo hace despues
    ``process_ingest_queue``, fuera de la request.

    Idempotencia: una tarea tiene como mucho un trabajo (OneToOne), y el
    procesador marca DONE dentro de la MISMA transaccion que aplica el
    resultado, asi que aplicarlo dos veces es imposible.
    """

    class JState(models.TextChoices):
        PENDING    = 'PENDING'
        PROCESSING = 'PROCESSING'
        DONE       = 'DONE'
        FAILED     = 'FAILED'

    task     = models.OneToOneField(AnalysisTask, on_delete=models.CASCADE,
                                    related_name='ingest_job')
    position = models.ForeignKey(Position, on_delete=models.CASCADE,
                                 related_name='ingest_jobs')
    payload  = models.JSONField()          # crudo, tal y como llego
    state    = models.CharField(max_length=10, choices=JState.choices,
                                default=JState.PENDING, db_index=True)
    attempts = models.IntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, db_index=True)
    claimed_at = models.DateTimeField(null=True)
    last_error = models.TextField(blank=True, default='')
    summary  = models.JSONField(null=True)
    created  = models.DateTimeField(auto_now_add=True, db_index=True)
    updated  = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['state', 'next_attempt_at', 'id'],
                                name='atomic_ingest_ready')]

    def __str__(self):
        return f'ingest job {self.pk} for task {self.task_id} ({self.state})'


class SolveTask(models.Model):
    """Una peticion de PRUEBA exhaustiva. Tabla aparte de ``AnalysisTask``.

    Deliberadamente separada: un worker antiguo pide analisis por
    ``/api/lease`` y nunca ve estas filas, asi que el protocolo nuevo es
    puramente aditivo.  El patron de arriendo (lease, heartbeat, token de
    fencing, sesion replayable) es el MISMO, porque ya esta probado en
    produccion y dos patrones distintos serian dos superficies de fallo.

    ``certificate`` va como blob comprimido EN LA FILA, no como fichero en
    MEDIA.  Razones, en orden: (1) el cierre exacto y su evidencia entran en
    la misma transaccion, asi que no puede existir un certificado huerfano ni
    una fila sin certificado; (2) la copia de seguridad y el recibo de
    identidad del split cubren TABLAS, no el sistema de ficheros — un
    certificado en disco quedaria fuera de las dos; (3) los topes duros
    (``solve.MAX_COMPRESSED_BYTES``) acotan la fila por construccion.  El
    precio es que un certificado enorme se rechaza en vez de guardarse; eso es
    exactamente lo que queremos de un limite anti-bomba.
    """

    class TState(models.TextChoices):
        PENDING   = 'PENDING'
        LEASED    = 'LEASED'
        COMPLETED = 'COMPLETED'
        FAILED    = 'FAILED'

    class Outcome(models.TextChoices):
        PROVED    = 'PROVED'
        DISPROVED = 'DISPROVED'
        UNKNOWN   = 'UNKNOWN'

    position     = models.ForeignKey(Position, on_delete=models.CASCADE,
                                     related_name='solve_tasks')
    campaign     = models.ForeignKey(ProofCampaign, null=True, blank=True,
                                     on_delete=models.SET_NULL,
                                     related_name='solve_tasks')
    goal         = models.CharField(max_length=10,
                                    choices=ProofCampaign.Goal.choices,
                                    default=ProofCampaign.Goal.WHITE_WIN)
    budget_nodes = models.BigIntegerField(default=10_000_000)
    state        = models.CharField(max_length=10, choices=TState.choices,
                                    default=TState.PENDING, db_index=True)
    machine      = models.CharField(max_length=64, default='')
    leased_at    = models.DateTimeField(null=True)
    lease_heartbeat_at = models.DateTimeField(null=True)
    lease_token  = models.CharField(max_length=64, default='')
    lease_session = models.CharField(max_length=64, default='')
    attempts     = models.IntegerField(default=0)
    # Resultado
    outcome      = models.CharField(max_length=10, choices=Outcome.choices,
                                    blank=True, default='')
    certificate  = models.BinaryField(null=True, blank=True)
    certificate_bytes = models.IntegerField(default=0)
    certificate_nodes = models.IntegerField(default=0)
    verified     = models.BooleanField(default=False)
    reject_reason = models.TextField(blank=True, default='')
    # Pistas de planificacion, NUNCA hechos: dependen del build, de la TT y
    # del presupuesto del worker que las produjo.
    advisory_pn  = models.BigIntegerField(null=True)
    advisory_dn  = models.BigIntegerField(null=True)
    searched_nodes = models.BigIntegerField(default=0)
    elapsed_seconds = models.FloatField(default=0.0)
    solver_build = models.CharField(max_length=64, blank=True, default='')
    # Etiqueta libre del arnes del piloto ('' fuera de el).
    arm          = models.CharField(max_length=32, blank=True, default='',
                                    db_index=True)
    created      = models.DateTimeField(auto_now_add=True, db_index=True)
    completed    = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=['state', 'created'],
                                name='atomic_solve_state')]

    def __str__(self):
        return f'solve {self.pk} {self.position_id[:12]} {self.state}'


# ---------------------------------------------------------------------------
# DISENO PENDIENTE: legal_move_inventory  (P2, sin migracion todavia)
# ---------------------------------------------------------------------------
#
# EL PROBLEMA.  Un nodo expandido materializa una fila de ``Edge`` mas una de
# ``Position`` por cada jugada legal: ~70 filas por expansion en la apertura
# atomica.  A escala eso es la mayor parte del almacenamiento, y la mayoria de
# esas filas no se visitan jamas — un nodo tipico solo necesita los tres a
# cinco hijos del MultiPV.
#
# POR QUE NO BASTA "expandir en perezoso".  El cierre AND exige saber que se
# han cubierto TODAS las respuestas legales.  Hoy ``expanded`` significa "las
# filas que hay son todas las que hay", que es una afirmacion sobre el estado
# de la tabla, no sobre las reglas.  Si las aristas se materializan a demanda,
# esa afirmacion deja de tener sentido y ``backup_status`` se queda sin su
# guarda — el agujero clasico de "cerre con movegen parcial".
#
# LA FORMA CORRECTA.  Separar COBERTURA de MATERIALIZACION:
#
#     class LegalMoveInventory(models.Model):
#         position       = models.OneToOneField(Position, ...)
#         movegen_version = models.CharField(max_length=32)   # pyffish build
#         move_count     = models.SmallIntegerField()
#         packed_moves   = models.BinaryField()   # 2 bytes/jugada, ordenadas
#         move_set_sha256 = models.CharField(max_length=64)
#         created        = models.DateTimeField(auto_now_add=True)
#
# Con ~70 jugadas y 16 bits por jugada son ~140 bytes, frente a las ~70 filas
# de posicion mas ~70 de arista que hoy cuesta lo mismo.  Y entonces:
#
#   * "expandido" pasa a significar "la cobertura materializada IGUALA un
#     conjunto legal AUTENTICADO", no "las filas que hay parecen todas";
#   * un nodo AND solo cierra si sus hijos cubren ``move_set_sha256``;
#   * un certificado SOLVE puede comparar su nodo AND contra el inventario en
#     vez de regenerar el movegen, y el verificador sigue regenerandolo de
#     todos modos como red;
#   * un cambio de movegen invalida inventarios por ``movegen_version`` en vez
#     de invalidar el arbol entero a ciegas.
#
# NO se implementa aqui: cambiar el significado de ``expanded`` toca el punto
# fijo exacto, y eso se hace con el gestor de prueba ya asentado y con la
# migracion a Postgres hecha, no en la misma semana que ambos.


class DBEvent(models.Model):
    ts      = models.DateTimeField(auto_now_add=True, db_index=True)
    kind    = models.CharField(max_length=32)     # SUBTREE_CLOSED, WALL, CAMPAIGN...
    payload = models.JSONField(default=dict)


class RequestLog(models.Model):
    """Peticiones publicas de analisis (rate-limit y dedup por IP)."""
    ip       = models.GenericIPAddressField(db_index=True)
    position = models.ForeignKey(Position, on_delete=models.CASCADE)
    created  = models.DateTimeField(auto_now_add=True, db_index=True)


class OpeningNameSuggestion(models.Model):
    """Nombre de apertura propuesto por la comunidad, sin cuenta.

    El catalogo auditado (``data/atomic_openings_v1.json``) es inmutable y su
    identidad esta fijada por digest: nada en tiempo de ejecucion lo reescribe.
    Un nombre aprobado aqui se APLICA por encima de el, sobre posiciones que el
    catalogo no nombra (§ community_names), y queda marcado como comunitario.

    ``resolved_by`` guarda el nombre de usuario, no una FK: el router de bases
    prohibe relaciones entre la base de AtomicDB y la de OpenBench.
    """

    class SState(models.TextChoices):
        PENDING  = 'PENDING'
        APPROVED = 'APPROVED'
        REJECTED = 'REJECTED'

    position      = models.ForeignKey(Position, on_delete=models.CASCADE,
                                      related_name='name_suggestions')
    proposed_name = models.CharField(max_length=60)
    comment       = models.CharField(max_length=280, blank=True, default='')
    ip            = models.GenericIPAddressField(db_index=True)
    created       = models.DateTimeField(auto_now_add=True, db_index=True)
    status        = models.CharField(max_length=8, choices=SState.choices,
                                     default=SState.PENDING, db_index=True)
    resolved_by   = models.CharField(max_length=64, blank=True, default='')
    resolved_at   = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=['status', 'created'],
                                name='atomic_suggestion_state')]

    def __str__(self):
        return f'{self.proposed_name} ({self.status})'


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

    # pn/dn de la raiz de la campana por defecto, en el instante de la
    # captura.  Son el indicador de progreso REAL del proyecto — cuanto
    # esfuerzo queda para probar la conjetura y cuanto para refutarla — y a la
    # vez el detector de que la conjetura podria ser falsa: dn estancado con
    # pn creciendo.  Se guardan saturados en ``proof.PROOF_INFINITY``, que la
    # vista pinta como infinito; cero es un valor legitimo (probado).
    root_pn = models.BigIntegerField(default=0)
    root_dn = models.BigIntegerField(default=0)

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
