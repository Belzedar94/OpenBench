"""AtomicDB: arbol persistente de resolucion practica de Atomic.

Spec: Atomic Project/Atomic-Stockfish-solving-docs/docs/atomic/solving/
atomicdb-tier1-spec.md. Disciplina central: eval (heuristico) y status
(exacto) nunca se mezclan — eval ordena la exploracion, status cierra.
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


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
    """Una CAMPANA DE EXPLORACION: una linea que la comunidad quiere trabajar.

    El reparto de poder es deliberado y es lo unico que este modelo tiene que
    hacer cumplir: cualquiera PROPONE y cualquiera VOTA, pero solo el
    propietario ACTIVA.  Por eso el estado no es un booleano — ``PROPOSED`` y
    ``PAUSED`` son cosas distintas y las dos son "no activa" — y por eso el
    recuento de votos NO decide nada por si mismo: entra en el selector como
    un peso (§ ingest.CAMPAIGN_BONUS) sobre una campana que ya esta ACTIVE.

    ``active`` sobrevive como ESPEJO del estado, no como fuente de verdad.
    Habia codigo desplegado leyendolo y borrar la columna en la misma
    migracion que introduce ``state`` seria pedir una ventana de despliegue
    atomica que nadie necesita; ``apply_state`` mantiene los dos de acuerdo y
    la columna se retirara cuando no quede lector.

    ``votes`` es una CACHE del ``COUNT`` de ``CampaignVote``: la fila
    autoritativa es el voto, y esta columna existe para poder ordenar y
    puntuar sin un GROUP BY por pasada del selector.  Quien la escribe la
    recalcula entera, nunca la incrementa.
    """

    class CState(models.TextChoices):
        PROPOSED = 'PROPOSED'   # en el buzon, esperando al propietario
        ACTIVE   = 'ACTIVE'     # el selector la esta empujando
        PAUSED   = 'PAUSED'     # activada y detenida; conserva sus etiquetas
        DONE     = 'DONE'       # cerrada (raiz resuelta o archivada a mano)

    # Estados en los que una campana sigue "viva" para el dedup de propuestas:
    # proponer la misma raiz otra vez devuelve la que ya hay.  ``DONE`` no
    # esta, y eso es lo que permite volver a proponer una linea archivada.
    OPEN_STATES = (CState.PROPOSED, CState.ACTIVE, CState.PAUSED)

    name     = models.CharField(max_length=64, unique=True)
    root     = models.ForeignKey(Position, on_delete=models.PROTECT,
                                 related_name='campaign_roots')
    line_san = models.TextField(default='')      # para mostrar ("1.Nf3 f6 2.Nc3 Nh6")
    active   = models.BooleanField(default=True)   # DEPRECADO: espejo de state
    state    = models.CharField(max_length=8, choices=CState.choices,
                                default=CState.PROPOSED, db_index=True)
    votes    = models.IntegerField(default=0)      # cache del COUNT de votos
    proposed_by = models.CharField(max_length=64, blank=True, default='')
    # Cookie anonima del proponente.  No identifica a nadie; es lo unico que
    # hace aplicable el tope de propuestas por visitante, que se cuenta contra
    # la MISMA cookie que los votos (§ views._voter_token).
    proposed_token = models.CharField(max_length=64, blank=True, default='',
                                      db_index=True)
    activated_at = models.DateTimeField(null=True)
    created  = models.DateTimeField(auto_now_add=True)

    def apply_state(self, state, now=None):
        """Cambia de estado dejando el espejo ``active`` de acuerdo.

        Un ``save`` suelto sobre ``state`` dejaria las dos columnas mintiendo
        la una sobre la otra, y la que leen los caminos antiguos es justo la
        que se quedaria vieja.  Una sola puerta, como en el resto del modulo.
        """
        fields = ['state', 'active']
        self.state = state
        self.active = state == self.CState.ACTIVE
        if state == self.CState.ACTIVE:
            self.activated_at = now or timezone.now()
            fields.append('activated_at')
        self.save(update_fields=fields)
        return self

    def __str__(self):
        return f'{self.name} ({self.state})'


class CampaignVote(models.Model):
    """Un voto por campana y por visitante, sin cuenta.

    La identidad es una cookie opaca, no una IP.  Detras de una IP compartida
    — un campus, un movil en CGNAT — hay cincuenta personas, y contarlas como
    una sola les daria un voto entre todas y un solo cupo de propuestas para
    todas.  No pretende ser infalsificable (borrar la cookie da otro voto),
    sino proporcionado a lo que decide: un ORDEN en una lista que el
    propietario mira antes de activar nada.

    ``name`` es opcional y decorativo: quien quiere que se sepa que voto lo
    dice, y quien no, no.
    """

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE,
                                 related_name='vote_rows')
    token    = models.CharField(max_length=64)
    name     = models.CharField(max_length=64, blank=True, default='')
    created  = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['campaign', 'token'], name='uniq_campaign_vote')]

    def __str__(self):
        return f'vote for campaign {self.campaign_id}'


class AnalysisTask(models.Model):
    class TState(models.TextChoices):
        PENDING   = 'PENDING'
        LEASED    = 'LEASED'
        COMPLETED = 'COMPLETED'

    class Source(models.TextChoices):
        AUTO = 'AUTO'   # selector best-first
        FILL = 'FILL'   # completado de cobertura (ver ingest, politica AND)
        USER = 'USER'   # peticion publica: se sirve primero

    # El orden de servicio es ``-source``, y alfabeticamente descendente eso
    # da USER > FILL > AUTO: una peticion de visitante sigue yendo primero, el
    # completado de cobertura va por delante de la exploracion normal — porque
    # cierra nodos en vez de solo mirarlos — y nunca se disfraza de visitante.

    position     = models.ForeignKey(Position, on_delete=models.CASCADE)
    budget_nodes = models.BigIntegerField()
    nodes_searched = models.BigIntegerField(default=0)  # nodos REALES buscados
    elapsed_seconds = models.FloatField(default=0.0)  # tiempo reportado por el motor
    multipv      = models.IntegerField(default=5)
    generation   = models.IntegerField(default=0)   # visita n-esima (escalera)
    source       = models.CharField(max_length=4, choices=Source.choices,
                                    default=Source.AUTO, db_index=True)
    # Cuenta OB del que PIDIO el analisis (vacio = anonimo).  Un worker de
    # esa misma cuenta atiende primero sus propias peticiones dentro de la
    # banda USER; para el resto de la flota no cambia nada.
    requested_by = models.CharField(max_length=64, default='', blank=True)
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
        # SURVIVE50 (doc 18 §6.1).  El PRIMER disproof que llega con
        # certificado reproducible: los ``DISPROVED`` de arriba son
        # afirmaciones sin reproducir, porque el solver df-pn solo emite
        # certificado cuando PRUEBA.  Este trae una estrategia de
        # supervivencia entera y se replica jugada a jugada.
        #
        # NO es ``BLACK_WIN`` y jamas debe convertirse en uno.  Refuta el
        # objetivo booleano WHITE_WIN y no dice nada del objetivo
        # independiente: Black sobrevive, que es compatible con tablas y con
        # victoria negra, y distinguirlas necesita otra prueba.  Por eso este
        # resultado NO cierra ninguna ``Position`` — alimenta al orquestador,
        # que elimina el candidato blanco, y nada mas.
        DISPROVED_WHITE_WIN = 'DISPROVED_WHITE_WIN'

    class Stage(models.TextChoices):
        """Peldano de la escalera F del doc 18 (§5).

        Hoy solo existen los dos extremos: F0, el peldano barato con el que se
        certifica deuda y se clasifica una fortaleza, y F4, el presupuesto de
        escalada que ya usaban las peticiones y el piloto.  F1-F3 pertenecen a
        SURVIVE50 (solver de umbrales tau) y NO estan implementados; la
        columna existe para que cuando lleguen no haya que re-etiquetar el
        historico.
        """

        F0 = 'F0'
        F1 = 'F1'
        F2 = 'F2'
        F3 = 'F3'
        F4 = 'F4'

    position     = models.ForeignKey(Position, on_delete=models.CASCADE,
                                     related_name='solve_tasks')
    campaign     = models.ForeignKey(ProofCampaign, null=True, blank=True,
                                     on_delete=models.SET_NULL,
                                     related_name='solve_tasks')
    goal         = models.CharField(max_length=10,
                                    choices=ProofCampaign.Goal.choices,
                                    default=ProofCampaign.Goal.WHITE_WIN)
    budget_stage = models.CharField(max_length=2, choices=Stage.choices,
                                    default=Stage.F4, db_index=True)
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
    outcome      = models.CharField(max_length=24, choices=Outcome.choices,
                                    blank=True, default='')
    certificate  = models.BinaryField(null=True, blank=True)
    certificate_bytes = models.IntegerField(default=0)
    certificate_nodes = models.IntegerField(default=0)
    # Que verificador replica este certificado.  Hay dos formatos y dos
    # verificadores independientes, y una fila que no dice cual es suyo es una
    # fila que no se puede re-verificar sin adivinar.
    certificate_format = models.CharField(max_length=48, blank=True,
                                          default='')
    # SURVIVE50: el reloj MINIMO desde el que la estrategia aguanta, en plies
    # del contador de 50.  Es el valor central del certificado, no telemetria:
    # ``tau <= reloj de entrada`` es exactamente lo que se ha probado.
    survival_tau    = models.SmallIntegerField(null=True, blank=True)
    survival_states = models.IntegerField(default=0)
    verified     = models.BooleanField(default=False)
    reject_reason = models.TextField(blank=True, default='')
    # Pistas de planificacion, NUNCA hechos: dependen del build, de la TT y
    # del presupuesto del worker que las produjo.
    advisory_pn  = models.BigIntegerField(null=True)
    advisory_dn  = models.BigIntegerField(null=True)
    searched_nodes = models.BigIntegerField(default=0)
    elapsed_seconds = models.FloatField(default=0.0)
    solver_build = models.CharField(max_length=64, blank=True, default='')
    # Telemetria de clasificacion de fortaleza (doc 18 §5).  ADVISORY: son
    # pistas de scheduling y NO autorizan ningun resultado; un veredicto solo
    # sale de un certificado reproducido entero.
    telemetry    = models.JSONField(null=True, blank=True)
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
    Un nombre aprobado aqui se APLICA por encima de el (§ community_names) y
    queda marcado como comunitario.

    Dos formas, y la diferencia importa (28-jul, peticion del propietario:
    "que la gente no solo pueda proponer nuevos nombres sino editar existentes
    tambien, con la misma aprobacion que ahora"):

    * ``NEW`` — la posicion no tenia nombre.  Sigue sin poder pisar al catalogo
      auditado: si alguien lo nombra mientras la propuesta espera, la
      aprobacion se convierte en rechazo, igual que antes.
    * ``EDIT`` — la posicion YA tenia nombre y esto es una correccion de ese
      nombre.  ``previous_name`` congela el nombre que se mostraba EN EL
      MOMENTO DE PROPONER, no en el de aprobar: es lo que el proponente estaba
      mirando, y es lo unico que hace legible un "actual -> propuesto" en la
      cola aunque el nombre cambie entre medias.  Una edicion aprobada SI
      desplaza al catalogo auditado en pantalla — ver community_names, donde
      esta escrito por que eso no rompe la propiedad que el catalogo protege.

    ``resolved_by`` guarda el nombre de usuario, no una FK: el router de bases
    prohibe relaciones entre la base de AtomicDB y la de OpenBench.
    """

    class SState(models.TextChoices):
        PENDING  = 'PENDING'
        APPROVED = 'APPROVED'
        REJECTED = 'REJECTED'

    class SKind(models.TextChoices):
        NEW  = 'NEW'
        EDIT = 'EDIT'

    position      = models.ForeignKey(Position, on_delete=models.CASCADE,
                                      related_name='name_suggestions')
    proposed_name = models.CharField(max_length=60)
    # Un flag explicito y no "previous_name != ''": lo que decide si algo
    # puede desplazar al catalogo auditado tiene que decir lo que ES, no
    # deducirse de que una cadena venga vacia.
    kind          = models.CharField(max_length=4, choices=SKind.choices,
                                     default=SKind.NEW)
    previous_name = models.CharField(max_length=60, blank=True, default='')
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
        if self.kind == self.SKind.EDIT:
            return (f'{self.previous_name} -> {self.proposed_name} '
                    f'({self.status})')
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

    # --- salud del FRENTE DE PRUEBA (definicion en proof.frontier_rows) ---
    #
    # Estas tres son las UNICAS de la fila que no se pueden reconstruir mas
    # tarde.  Los eventos son durables y los ``RequestLog`` tambien, asi que
    # cualquier conteo sobre ellos se puede recalcular manana; los ``pn``/``dn``
    # de un ``ProofNode`` se sobrescriben en el sitio en cada cascada, asi que
    # la distribucion de ``dn`` de esta hora deja de existir en cuanto pasa.
    # Si no se guarda aqui, no hay "antes" contra el que medir nada.
    frontier_and_nodes = models.BigIntegerField(default=0)
    frontier_dn_median = models.BigIntegerField(default=0)
    frontier_dn_thin = models.BigIntegerField(default=0)

    # --- cierres por PROCEDENCIA, acumulados desde el despliegue ---
    #
    # Cuentan eventos ``NODE_CLOSED`` que llevan la etiqueta ``source`` que
    # ``ingest.closure_attribution`` empezo a poner.  Los cierres anteriores al
    # despliegue no la llevan y no entran en ninguno de los cinco: eso es
    # deliberado y es lo que hace que la suma NO cuadre con
    # ``positions_closed``.  Un contador que mintiera para cuadrar seria peor
    # que uno que no cubre el pasado.
    closures_user = models.BigIntegerField(default=0)
    closures_fill = models.BigIntegerField(default=0)
    closures_auto = models.BigIntegerField(default=0)
    closures_solve = models.BigIntegerField(default=0)
    closures_none = models.BigIntegerField(default=0)

    # --- tiempo hasta el cierre de lo que toco una persona ---
    #
    # Mediana de segundos entre la PRIMERA peticion publica sobre una posicion
    # y su cierre, sobre los cierres de la ventana reciente.  Es una VENTANA,
    # no un acumulado — como los medidores de workers vivos — y su tamano de
    # muestra viaja al lado para que una mediana de dos casos no se lea como
    # una propiedad de la flota.
    human_close_median_seconds = models.BigIntegerField(default=0)
    human_close_samples = models.BigIntegerField(default=0)

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
