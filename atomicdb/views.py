"""API de AtomicDB (worker) + paginas publicas del Explorer."""

import json
import logging
import math
import secrets
import time
from datetime import timedelta
from urllib.parse import quote, urlsplit

from django.conf import settings
from django.contrib.auth import authenticate
from django.core import signing
from django.core.cache import cache
from django.db import IntegrityError, OperationalError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

import re

from django.db.models import (Case, Count, F, FloatField, IntegerField,
                              OuterRef, Q, Subquery, Sum, Value, When, Window)
from django.db.models.functions import Coalesce, RowNumber

from . import (community_names, contributors, ingest, ingest_queue, logic,
               metrics, notifications, openings, proof, solve, solve_estimate)
from .database import atomic
from .metrics import worker_metrics
from .models import (AnalysisTask, Campaign, CampaignVote, DBEvent, Edge,
                     OpeningNameSuggestion, Position, ProgressSnapshot,
                     ProofCampaign, ProofNode, RequestLog, SolveTask,
                     WorkerPing)

LEASE_MINUTES = 10
LEGACY_LEASE_MINUTES = 24 * 60
POST_DEPLOY_LEGACY_LEASE_MINUTES = 60
# Relevo tras reinicio: una peticion con la MISMA identidad de maquina, OTRA
# sesion y el heartbeat del titular mas viejo que esto recicla el lease en el
# acto en vez de esperar la ventana entera de caducidad. Los heartbeats van
# cada ~30-60s: tres minutos de silencio es un proceso muerto, no uno lento.
RESTART_RECYCLE_MINUTES = 3
LEGACY_DISPLAY_MINUTES = 60
# One task per lease is intentional. It makes the queue truthful and prevents
# a later task in a sequential batch expiring before the worker even starts it.
# Emergencia inline: el colchon lo mantiene el selector
# (ingest.top_up_analysis_pool); esto solo cubre el hueco entre ciclos.
TASK_REFILL_COUNT = 8
LEASE_TOKEN_BUILD = 2026072203
LEGACY_MAX_BUDGET = 128_000_000
# Un worker con --jobs K se presenta como K leaseholders, "base#0".."base#K-1",
# porque asi cada trabajo hereda TAL CUAL las reglas que el protocolo ya
# aplica por maquina: una asignacion viva, token de fencing, heartbeat y
# replay de una respuesta perdida.  No hace falta nada nuevo en el protocolo
# ni una columna nueva; el slot vive en la identidad.
#
# Este tope es lo unico que hay que anadir, y no es contra el contribuidor
# honesto: es contra el que se equivoca de cero en --jobs, o el que decide
# que la cola es suya.  32 deja sitio de sobra a una maquina grande de verdad
# y sigue impidiendo que una sola vacie la cola.
LEASES_PER_MACHINE = 32
MAX_REPORTED_NPS = 1_000_000_000_000
REQUEST_QUEUE_MAX = 1000000  # efectivamente sin tope (orden 27-jul; el propietario lo monitoriza)
MAX_SUBMIT_LINES_BYTES = 512 * 1024
MAX_SUBMIT_PV_PLIES = 512
# Breadcrumb reconstruction is public-request work. Keep the cycle-safe
# reverse search useful for recent rows that are not in a materialized map
# yet, but put hard ceilings on graph fan-out and memory.
# The tree reached 40-90 plies the day 372 community cores arrived, and a
# breadcrumb that cannot reach the root is worse than useless — it renders a
# headless tail.  96 plies is one Edge query per ply for ALL the rows on a
# page (the walk is batched), so the home costs at most ~96 queries behind a
# 15s cache.  Beyond this the answer is not a bigger ceiling but a lineage
# cached on the task at creation time; see the deep-line label below.
LINEAGE_SEARCH_MAX_PLIES = 96
LINEAGE_SEARCH_MAX_NODES = 1024
LINEAGE_SEARCH_MAX_FRONTIER = 64
LINEAGE_SEARCH_MAX_PARENTS_PER_CHILD = 16
LINEAGE_SEARCH_MAX_EDGE_ROWS = 4096
# ...and the walk above is what every explorer click paid, every time.  The
# night a batch job and a traffic record met on the same box, the VPS went to
# load 34 and a click on a deep position took ~300ms of window-function
# queries — recomputing a line to the root that had not changed since the
# previous click and almost never does.
#
# So it is cached, and the TTL is the ONLY invalidation.  That is a choice,
# not a shortcut: a new edge can only ever make a breadcrumb SHORTER, never
# wrong, and nobody navigating a 96-ply line needs to see that improvement
# within the same minute.  Anything stronger (cross-process events, a shared
# store) is infrastructure this deployment does not have and does not need —
# five gunicorn workers keep five independent copies, and a restart clears
# all of them.  Two minutes is also the blast radius if this is ever wrong.
LINEAGE_CACHE_SECONDS = 120
# Bump when the cached payload shape changes, so a deploy cannot read an old
# entry with new code.  ``0`` seconds above disables the cache outright.
LINEAGE_CACHE_VERSION = 1
PLAY_ROUTE_MAX_PLIES = 64
PLAY_ROUTE_MAX_CHARS = PLAY_ROUTE_MAX_PLIES * 6
PLAY_UCI_RE = re.compile(r'^[a-h][1-8][a-h][1-8][qrbn]?$')
OPENING_ANCHOR_PARAM = 'opening'
OPENING_ANCHOR_SALT = 'atomicdb.explorer.opening-anchor.v1'
OPENING_ANCHOR_MAX_CHARS = 1024
# board.js predates opening anchors and carries only one ``play`` value.
# A distinct, signed sentinel keeps drag/drop navigation working after the
# bounded replay route rolls over, without accepting any extra replay tokens.
OPENING_ANCHOR_PLAY_PREFIX = 'signed-opening.'
# Propuestas publicas de nombre de apertura (§ community_names). Mismo patron
# de rate-limit que RequestLog: contar filas propias por IP en una ventana.
SUGGESTION_NAME_MAX_CHARS = 60
SUGGESTION_NAME_MIN_CHARS = 2
SUGGESTION_COMMENT_MAX_CHARS = 280
SUGGESTIONS_PER_IP_DAY = 10
SUGGESTION_MODERATION_PAGE = 100
# Letras (con acentos), cifras y la puntuacion que un nombre de apertura de
# verdad necesita. Nada de <>&"' ni control chars: el escapado de plantilla ya
# protege, pero un nombre no tiene por que contener markup para empezar.
SUGGESTION_NAME_RE = re.compile(r"^[0-9A-Za-zÀ-ÿ][0-9A-Za-zÀ-ÿ .,:'’\-()/]*$")

# ---- campanas de exploracion propuestas y votadas por la comunidad ----
#
# La identidad del votante es una cookie opaca, no la IP.  Las dos cosas que
# se protegen son distintas y una sola llave no vale para las dos: el tope de
# propuestas quiere "una persona no llena el buzon" (y una IP compartida son
# muchas personas), y el voto quiere "una persona un voto" (y una IP
# compartida seguiria siendo un voto).  La cookie separa a los visitantes de
# un mismo CGNAT sin fingir que identifica a nadie.
CAMPAIGN_VOTER_COOKIE = 'atomicdb_voter'
CAMPAIGN_VOTER_BYTES = 24            # token_urlsafe(24) -> 32 caracteres
CAMPAIGN_VOTER_COOKIE_SECONDS = 400 * 24 * 3600   # tope que aceptan los
CAMPAIGN_VOTER_RE = re.compile(r'^[A-Za-z0-9_-]{16,64}$')  # navegadores
CAMPAIGN_PROPOSALS_PER_VOTER_DAY = 3
CAMPAIGN_PROPOSER_MAX_CHARS = 64
# Titulo elegido por quien propone.  Es texto plano y se pinta escapado como
# todo lo demas; lo unico que se hace al recibirlo es colapsar espacios y
# recortarlo al ancho de la columna, que es tambien el ancho del titular.
CAMPAIGN_TITLE_MAX_CHARS = 64
# Cuanta linea entra en el rotulo de una campana. Un breadcrumb sin tope
# puede traer cien plies, y la tarjeta de portada es un titular.
CAMPAIGN_LINE_PLIES = 24
# Tope del etiquetado por BFS al activar. No es una estimacion del tamano de
# una campana: es lo que se acepta gastar de una vez dentro de un POST.
CAMPAIGN_TAG_MAX_NODES = 50_000
CAMPAIGN_TAG_BATCH = 500
HOME_ACTIVE_CAMPAIGNS = 2
HOME_PROPOSED_CAMPAIGNS = 6

logger = logging.getLogger(__name__)


class PlayRouteError(ValueError):
    """An explicit explorer route is malformed or illegal."""

    status_code = 400


class PlayRouteConflict(PlayRouteError):
    """A legal route does not identify the requested AtomicDB position."""

    status_code = 409


def _auth(request):
    """Credenciales de worker: usuario correcto Y cuenta habilitada.

    Expulsar a alguien es ponerle ``Profile.enabled`` a False, y hasta ahora
    eso solo le cerraba OpenBench: sus workers seguian arrendando, latiendo y
    entregando analisis aqui como el primer dia.  El criterio es el mismo que
    el del protocolo de OpenBench (``authenticate(requireEnabled=True)``): sin
    perfil habilitado no hay usuario, y quien llame recibe el mismo 403 que
    quien se equivoca de contrasena.
    """
    # Import local, como en ``_approver_gate``: mantiene a AtomicDB importable
    # sin arrastrar OpenBench.
    from OpenBench.models import Profile
    user = authenticate(username=request.POST.get('username', ''),
                        password=request.POST.get('password', ''))
    if user is None:
        return None
    if not Profile.objects.filter(user=user, enabled=True).exists():
        return None
    return user


def _touch_worker(request, user):
    """Refresh capacity without changing, extending or reclaiming any lease."""
    machine = request.POST.get('machine', user.username)[:64]
    try:
        threads = max(0, int(request.POST.get('threads', 0) or 0))
        hash_mb = max(0, int(request.POST.get('hash', 0) or 0))
    except ValueError:
        threads, hash_mb = 0, 0
    now = timezone.now()
    capacity = {
        'threads': threads,
        'hash_mb': hash_mb,
        'os': request.POST.get('os', '')[:64],
        'last_seen': now,
    }
    ping, created = WorkerPing.objects.get_or_create(
        machine=machine, user=user.username, defaults=capacity)
    if not created:
        # Do not save the stale model instance: submit and heartbeat update
        # telemetry concurrently and a full save could roll those fields back.
        WorkerPing.objects.filter(pk=ping.pk).update(**capacity)
    return ping


# ---------------- API worker ----------------

def _live_moves(task):
    """Jugadas sin resolver de la posicion: el motor no debe gastar ni un
    nodo re-derivando defensas ya demostradas (go searchmoves). Vacio = sin
    restriccion (nada resuelto aun, o posicion sin expandir).

    QUE SIGNIFICA LO QUE VUELVE DE UN PASE ASI.  Un pase restringido no mide
    la posicion: mide LO MEJOR ENTRE LO QUE SIGUE SIN RESOLVER.  Si la jugada
    buena de verdad ya esta cerrada y fuera de la lista, el numero que devuelve
    el motor es PEOR que la posicion, y ese numero alimenta budget_for, el
    breadth-swap, witness-refuted y la incertidumbre.  Por eso el worker
    devuelve la restriccion que le dieron (§ ``ingest.ingest_analysis``,
    ``restricted``) y un pase restringido nunca puede EMPEORAR ``eval_cp`` para
    el que mueve: solo puede mejorarlo, que es lo unico que puede afirmar
    honestamente sobre la posicion entera.
    """
    pos = task.position
    if not pos.expanded:
        return []
    edges = list(Edge.objects.filter(parent=pos).select_related('child'))
    live = [e.move_uci for e in edges if e.child.status == 'UNKNOWN']
    if 0 < len(live) < len(edges):
        return live
    return []


def machine_base(machine):
    """La maquina fisica detras de un slot: ``base#3`` -> ``base``.

    Un worker con --jobs se identifica por slot, y el tope de leases es por
    maquina, no por slot -- si no, --jobs 1000 lo sortearia trivialmente.
    """
    return (machine or '').split('#', 1)[0]


def _machine_at_lease_cap(machine):
    """True si la maquina fisica ya tiene todas sus asignaciones vivas.

    El prefijo se compara con el separador incluido: ``bob-pc-atomicdb`` y
    ``bob-pc-atomicdb2`` son maquinas distintas y un ``startswith`` a secas
    las mezclaria.  Se cuenta DESPUES de reciclar los leases caducados, en la
    misma transaccion, asi que un lease muerto no ocupa sitio.
    """
    base = machine_base(machine)
    if not base:
        return False
    held = (AnalysisTask.objects
            .filter(state='LEASED')
            .filter(Q(machine=base) | Q(machine__startswith=base + '#'))
            .count())
    return held >= LEASES_PER_MACHINE


@csrf_exempt
def api_lease(request):
    user = _auth(request)
    if user is None:
        return JsonResponse({'error': 'bad credentials'}, status=403)
    ping = _touch_worker(request, user)
    machine = ping.machine

    try:
        worker_build = int(request.POST.get('worker_build', 0) or 0)
    except ValueError:
        worker_build = 0
    supports_lease_token = worker_build >= LEASE_TOKEN_BUILD
    lease_session = request.POST.get('lease_session', '')[:64]
    active_task_id = None

    with atomic():
        # recuperar leases caducados
        now = timezone.now()
        stale = now - timedelta(minutes=LEASE_MINUTES)
        legacy_stale = now - timedelta(minutes=LEGACY_LEASE_MINUTES)
        post_legacy_stale = now - timedelta(
            minutes=POST_DEPLOY_LEGACY_LEASE_MINUTES)
        AnalysisTask.objects.filter(
            state='LEASED', lease_token__gt='', leased_at__lt=stale,
        ).filter(
            Q(lease_heartbeat_at__isnull=True)
            | Q(lease_heartbeat_at__lt=stale)
        ).update(state='PENDING', machine='', lease_heartbeat_at=None,
                 lease_token='', lease_session='')
        # A pre-deploy worker cannot heartbeat or fence a deep result. Give
        # already-running tokenless work a one-time long drain window; new
        # legacy assignments below are limited to short first attempts.
        AnalysisTask.objects.filter(
            state='LEASED', lease_token='', lease_heartbeat_at__isnull=False,
            lease_heartbeat_at__lt=post_legacy_stale,
        ).update(state='PENDING', machine='', lease_heartbeat_at=None,
                 lease_session='')
        AnalysisTask.objects.filter(
            state='LEASED', lease_token='', lease_heartbeat_at__isnull=True,
            leased_at__lt=legacy_stale,
        ).update(state='PENDING', machine='', lease_heartbeat_at=None,
                 lease_session='')

        # A second process using the same machine identity must not steal a
        # healthy assignment. The per-assignment token below fences old
        # processes once a genuinely stale lease is recycled.
        active_same_machine = (AnalysisTask.objects.select_for_update()
            .select_related('position')
            .filter(state='LEASED', machine=machine)
            .filter(Q(lease_token='', lease_heartbeat_at__isnull=True,
                      leased_at__gte=legacy_stale)
                    | Q(lease_token='',
                        lease_heartbeat_at__gte=post_legacy_stale)
                    | Q(lease_token__gt='', lease_heartbeat_at__gte=stale)
                    | Q(lease_token__gt='', lease_heartbeat_at__isnull=True,
                        leased_at__gte=stale))
            .order_by('leased_at', 'id').first())

        def choose_pending():
            # Scan the ordered queue, rather than an arbitrary prefix: a
            # non-TB worker must not receive a TB task merely because several
            # TB positions happen to be ahead of the first compatible task.
            first = None
            supports_tb = request.POST.get('tb') == '1'
            # Dentro de la banda USER el orden es FIFO puro (id de creacion).
            # Un click es un click: ordenar a los humanos por la prioridad de
            # la POSICION hacia que una linea profunda y perdedora (prioridad
            # muy negativa) esperase detras de cada click fresco de cualquier
            # otro — Wolfram espero una hora mientras la cola volaba.  La
            # prioridad de posicion sigue mandando en AUTO y FILL, que es
            # donde significa algo (el selector la calcula para eso).
            human_rank = Case(
                When(source=AnalysisTask.Source.USER, then=Value(0.0)),
                default=F('position__priority'),
                output_field=FloatField())
            # Afinidad worker-peticionario: dentro de la banda USER, las
            # peticiones de la MISMA cuenta que corre este worker van
            # primero (quien pone hierro cobra el suyo antes); entre ellas
            # y entre las ajenas, el FIFO de siempre.  AUTO/FILL ni se
            # enteran: own vale 0 en toda la banda.
            own_first = Case(
                When(source=AnalysisTask.Source.USER,
                     requested_by=user.username, then=Value(1)),
                default=Value(0), output_field=IntegerField())
            # Peticiones CON NOMBRE por delante de la marea anonima.  Un
            # "Analyse all" sin login encola decenas de tareas de cobertura;
            # un humano identificado que pide UNA posicion no debe esperar
            # detras de dos mil de esas (Lesha, 31-jul: sus requests en el
            # puesto ~1400 del FIFO, horas de cola).  Dentro de cada estrato
            # el FIFO de siempre; AUTO/FILL ni se enteran.
            named_first = Case(
                When(source=AnalysisTask.Source.USER,
                     requested_by__gt='', then=Value(1)),
                default=Value(0), output_field=IntegerField())
            queryset = (AnalysisTask.objects
                .select_for_update(skip_locked=True)
                .select_related('position').filter(state='PENDING')
                .annotate(queue_rank=human_rank, own_first=own_first,
                          named_first=named_first)
                .order_by('-source', '-own_first', '-named_first',
                          '-queue_rank', 'id'))
            for candidate in queryset.iterator(chunk_size=64):
                # A queued follow-up is an intent for the next visit, not a
                # parallel analysis. It becomes runnable only after the prior
                # generation commits, and never runs if that visit solved the
                # position outright.
                if (candidate.position.status != 'UNKNOWN'
                        or candidate.generation > candidate.position.visits):
                    continue
                # A tokenless first attempt remains compatible with a worker
                # that was already deployed before this protocol. Once an
                # attempt has been recycled, only a token-capable build may
                # take it: otherwise the deceased process could submit against
                # a new tokenless lease with the same machine identity.
                if (not supports_lease_token
                        and (candidate.attempts > 0
                             or candidate.budget_nodes > LEGACY_MAX_BUDGET)):
                    continue
                if first is None:
                    first = candidate
                if supports_tb or not logic.tb_applicable(candidate.position.fen):
                    return candidate
            # Preserve the historic fallback when every queued task is TB.
            return first

        batch = []
        replayed = False
        if active_same_machine is not None:
            if (supports_lease_token and lease_session
                    and secrets.compare_digest(
                        active_same_machine.lease_session, lease_session)):
                # The first HTTP response may have been lost after commit.
                # Replay the exact task/token without changing the attempt.
                batch = [active_same_machine]
                replayed = True
            else:
                # REINICIO frente a ROBO.  Una sesion DISTINTA con el
                # heartbeat del titular ya frio no es un segundo proceso
                # robando trabajo vivo: es el relevo del muerto pidiendo
                # trabajo con su misma identidad.  Sin esto, el worker
                # relanzado se queda "sin tareas" hasta agotar la ventana
                # entera de caducidad (incidente t24, 29-jul: ~15 min de
                # maquina parada con 755 tareas en cola).  Un heartbeat
                # fresco sigue bloqueando: esa proteccion es la correcta.
                beat = (active_same_machine.lease_heartbeat_at
                        or active_same_machine.leased_at)
                dead = now - timedelta(minutes=RESTART_RECYCLE_MINUTES)
                if beat is not None and beat < dead:
                    AnalysisTask.objects.filter(
                        id=active_same_machine.id, state='LEASED',
                    ).update(state='PENDING', machine='',
                             lease_heartbeat_at=None, lease_token='',
                             lease_session='')
                    # El relevo no espera al proximo poll: con el zombi
                    # reciclado, ESTA misma request baja a elegir tarea.
                    active_same_machine = None
                else:
                    active_task_id = active_same_machine.id
        if (active_same_machine is None and not replayed
                and active_task_id is None):
            if _machine_at_lease_cap(machine):
                # La maquina fisica ya tiene LEASES_PER_MACHINE asignaciones
                # vivas repartidas entre sus slots.  No es un error del worker
                # honesto -- con --jobs sensato nunca se llega -- asi que se
                # le responde como a una cola vacia y reintenta, en vez de
                # fallar.
                pass
            else:
                chosen = choose_pending()
                if chosen is None:
                    ingest.next_tasks(TASK_REFILL_COUNT)
                    chosen = choose_pending()
                if chosen is not None:
                    batch = [chosen]
        if not replayed:
            assigned_at = timezone.now()
            for t in batch:
                t.state, t.machine, t.leased_at = 'LEASED', machine, assigned_at
                t.lease_heartbeat_at = assigned_at
                t.lease_token = (secrets.token_urlsafe(32)
                                 if supports_lease_token else '')
                t.lease_session = (lease_session
                                   if supports_lease_token else '')
                t.attempts += 1
                t.save(update_fields=['state', 'machine', 'leased_at',
                                      'lease_heartbeat_at', 'lease_token',
                                      'lease_session', 'attempts'])

    # With one task per lease, the server can expose the exact current task
    # immediately even to the previous worker build, before its first heartbeat.
    if batch:
        WorkerPing.objects.filter(pk=ping.pk).update(
            current_task_id=batch[0].id,
            last_nps=0,
            nps_updated=None,
            last_seen=timezone.now(),
        )
    elif active_task_id is not None:
        WorkerPing.objects.filter(pk=ping.pk).update(
            current_task_id=active_task_id,
            last_seen=timezone.now(),
        )
    else:
        WorkerPing.objects.filter(pk=ping.pk).update(
            current_task_id=None,
            last_nps=0,
            nps_updated=None,
            last_seen=timezone.now(),
        )

    return JsonResponse({'tasks': [
        {'id': t.id, 'fen': t.position.fen, 'budget_nodes': t.budget_nodes,
         'multipv': t.multipv, 'searchmoves': _live_moves(t),
         'lease_token': t.lease_token}
        for t in batch]})


@csrf_exempt
def api_heartbeat(request):
    """Publish live work and keep only that authenticated assignment alive."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    user = _auth(request)
    if user is None:
        return JsonResponse({'error': 'bad credentials'}, status=403)
    ping = _touch_worker(request, user)
    try:
        heartbeat_nps = max(0, min(int(request.POST.get('nps', 0) or 0),
                                   MAX_REPORTED_NPS))
    except ValueError:
        return JsonResponse({'error': 'invalid nps'}, status=400)
    raw_task_id = request.POST.get('task_id', '')
    current_task_id = None
    if raw_task_id:
        try:
            candidate = int(raw_task_id)
        except ValueError:
            return JsonResponse({'error': 'invalid task_id'}, status=400)
        keepalive_at = timezone.now()
        lease_token = request.POST.get('lease_token', '')
        renewed = (AnalysisTask.objects
            .filter(id=candidate, state='LEASED', machine=ping.machine)
            .filter(Q(lease_token='') | Q(lease_token=lease_token))
            .update(lease_heartbeat_at=keepalive_at))
        if renewed == 1:
            current_task_id = candidate
        else:
            return JsonResponse({'error': 'stale-lease'}, status=409)
    updates = {'current_task_id': current_task_id,
               'last_seen': timezone.now(),
               'last_nps': heartbeat_nps if current_task_id else 0,
               'nps_updated': timezone.now() if current_task_id else None}
    WorkerPing.objects.filter(pk=ping.pk).update(**updates)
    return JsonResponse({'ok': True, 'machine': ping.machine,
                         'current_task_id': current_task_id})


@csrf_exempt
def api_submit(request):
    submit_started = time.monotonic()
    user = _auth(request)
    if user is None:
        return JsonResponse({'error': 'bad credentials'}, status=403)
    try:
        task_id = int(request.POST['task_id'])
        raw_lines = request.POST['lines']
        if len(raw_lines.encode('utf-8')) > MAX_SUBMIT_LINES_BYTES:
            raise ValueError('lines payload is too large')
        lines = json.loads(raw_lines)
        if not isinstance(lines, list) or len(lines) > 32:
            raise ValueError('lines must be a list with at most 32 entries')
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError('each line must be an object')
            move = line.get('move')
            if not isinstance(move, str) or not move or len(move) > 16:
                raise ValueError('invalid root move')
            for score_name in ('eval_cp', 'mate'):
                score = line.get(score_name)
                if score is not None and (not isinstance(score, int)
                                          or isinstance(score, bool)):
                    raise ValueError(f'invalid {score_name}')
            pv = line.get('pv')
            if pv is not None and (not isinstance(pv, list)
                                   or len(pv) > MAX_SUBMIT_PV_PLIES
                                   or any(not isinstance(item, str)
                                          or len(item) > 16 for item in pv)):
                raise ValueError('invalid or excessively long PV')
            raw = line.get('raw')
            if raw is not None and (not isinstance(raw, str)
                                    or len(raw) > 65_536):
                raise ValueError('raw line is too large')
    except Exception as e:
        return JsonResponse({'error': f'malformed: {e}'}, status=400)

    try:
        elapsed = float(request.POST.get('elapsed', 0) or 0)
        if not math.isfinite(elapsed):
            raise ValueError
        elapsed = min(max(elapsed, 0.0), 86_400.0)
    except (TypeError, ValueError):
        elapsed = 0.0
    try:
        searched = max(0, int(request.POST.get('nodes', 0) or 0))
    except ValueError:
        searched = 0
    tb_wdl = request.POST.get('tb_wdl')
    try:
        parsed_wdl = None if tb_wdl in (None, '') else int(tb_wdl)
    except ValueError:
        return JsonResponse({'error': 'malformed: invalid tb_wdl'}, status=400)
    # DTZ opcional y ADITIVO: sin el, el cierre TB vale solo con el reloj a
    # cero (clock_slack 0), que es lo que ya ocurria.  Un valor ilegible se
    # ignora en vez de rechazar el submit entero: es contexto de reloj, no el
    # hecho exacto, y el hecho exacto ya viene verificado aparte.
    tb_dtz = request.POST.get('tb_dtz')
    try:
        parsed_dtz = None if tb_dtz in (None, '') else int(tb_dtz)
    except (TypeError, ValueError):
        parsed_dtz = None
    if parsed_dtz is not None and abs(parsed_dtz) > 1024:
        parsed_dtz = None

    machine = request.POST.get('machine', '')
    engine_sha = _sha_field(request.POST.get('engine_sha', ''))
    net_sha = _sha_field(request.POST.get('net_sha', ''))
    provided_lease_token = request.POST.get('lease_token', '')
    try:
        snapshot = AnalysisTask.objects.select_related('position').get(id=task_id)
    except AnalysisTask.DoesNotExist:
        return JsonResponse({'error': 'malformed: unknown task'}, status=400)
    if snapshot.state not in ('LEASED', 'COMPLETED'):
        return JsonResponse({'error': 'not-leased'}, status=400)
    if not machine or machine != snapshot.machine:
        return JsonResponse({'error': 'not-your-lease'}, status=409)
    if (snapshot.lease_token
            and not secrets.compare_digest(snapshot.lease_token,
                                           provided_lease_token)):
        return JsonResponse({'error': 'stale-lease'}, status=409)
    if snapshot.state == 'COMPLETED':
        return JsonResponse({'ok': True, 'dup': True})

    # A partir de aqui el submit solo RECLAMA y ENCOLA: la parte cara (probar
    # mates, expandir, aristas, cascadas, retropropagacion) la hace despues
    # process_ingest_queue.  Reclamacion y encolado son un unico commit, asi
    # que un lease no puede caducar con el payload esperando en la cola.
    transaction_started = time.monotonic()
    with atomic():
        claimed = AnalysisTask.objects.filter(
            id=task_id, state='LEASED', machine=machine,
            attempts=snapshot.attempts, leased_at=snapshot.leased_at,
            lease_token=snapshot.lease_token,
        ).update(state='COMPLETED')
        if claimed != 1:
            current = AnalysisTask.objects.get(id=task_id)
            if current.state == 'COMPLETED':
                return JsonResponse({'ok': True, 'dup': True})
            if current.state != 'LEASED':
                return JsonResponse({'error': 'not-leased'}, status=400)
            if (current.machine == machine
                    and (current.attempts != snapshot.attempts
                         or current.leased_at != snapshot.leased_at
                         or current.lease_token != snapshot.lease_token)):
                return JsonResponse({'error': 'stale-lease'}, status=409)
            return JsonResponse({'error': 'not-your-lease'}, status=409)

        task = (AnalysisTask.objects.select_for_update()
                .select_related('position').get(id=task_id))
        searched = min(searched, 2 * task.budget_nodes)
        job, _created = ingest_queue.enqueue(task, {
            'lines': lines,
            'nodes': searched,
            'elapsed': elapsed,
            'machine': machine,
            'username': user.username,
            'tb_wdl': parsed_wdl,
            'tb_dtz': parsed_dtz,
            # Estas lineas hablan de la posicion ENTERA o solo de lo que
            # quedaba sin resolver (§ ``_live_moves``).  El worker devuelve la
            # restriccion con la que busco; uno anterior a este build no la
            # manda, y entonces se toma el pase como completo, que es
            # exactamente lo que se hacia antes.
            'restricted': bool((request.POST.get('searchmoves') or '').split()),
        })
        task.state, task.machine = 'COMPLETED', machine
        task.completed = timezone.now()
        task.nodes_searched = searched
        task.elapsed_seconds = elapsed
        # Procedencia opcional: un worker anterior a este build no la manda y
        # las columnas se quedan vacias, que es exactamente lo que ya pasaba.
        task.engine_sha = engine_sha
        task.net_sha = net_sha
        task.save(update_fields=[
            'state', 'machine', 'completed', 'nodes_searched',
            'elapsed_seconds', 'engine_sha', 'net_sha'])
        ping_updates = {
            'tasks_done': F('tasks_done') + 1,
            'last_seen': timezone.now(),
        }
        if searched > 0 and elapsed > 0:
            ping_updates.update({
                'last_nps': min(round(searched / elapsed),
                                MAX_REPORTED_NPS),
                'nps_updated': timezone.now(),
            })
        ping_updates['current_task_id'] = None
        WorkerPing.objects.filter(machine=machine, user=user.username).update(
            **ping_updates)
    transaction_seconds = time.monotonic() - transaction_started

    summary = {'queued': True, 'ingest_job': job.pk}
    apply_seconds = 0.0
    if ingest_queue.synchronous_ingest_enabled():
        # Interruptor de despliegue: mismo codigo de aplicacion, aqui mismo.
        apply_started = time.monotonic()
        applied, error = ingest_queue.process_job(job)
        apply_seconds = time.monotonic() - apply_started
        summary = applied if error is None else {'ingest_job': job.pk,
                                                 'ingest_error': error}
    total_seconds = time.monotonic() - submit_started
    logger.info(
        'AtomicDB submit task=%s machine=%s kind=%s lines=%s job=%s '
        'transaction_seconds=%.3f apply_seconds=%.3f total_seconds=%.3f',
        task_id, machine, 'tb' if parsed_wdl is not None else 'engine',
        len(lines), job.pk, transaction_seconds, apply_seconds, total_seconds)
    summary = dict(summary or {})
    summary['submit_timing_seconds'] = {
        'transaction': round(transaction_seconds, 3),
        'apply': round(apply_seconds, 3),
        'total': round(total_seconds, 3),
    }
    return JsonResponse({'ok': True, 'summary': summary})


def _sha_field(raw):
    """Un sha256 hex en minusculas, o cadena vacia. Nunca levanta.

    Es telemetria opcional que llega de un cliente no confiable: se sanea a
    lo que la columna admite y punto.  No cierra nada, no autoriza nada; solo
    hace ATRIBUIBLE un sesgo de evals a un binario concreto.
    """
    raw = str(raw or '').strip().lower()
    if len(raw) != 64 or any(ch not in '0123456789abcdef' for ch in raw):
        return ''
    return raw


# ---------------- API SOLVE (aditiva) ----------------
#
# Un worker anterior a este protocolo no llama a ninguno de estos endpoints y
# no cambia nada para el.  El patron de arriendo es EL MISMO que el de
# AnalysisTask — token de fencing por asignacion, heartbeat separado, sesion
# replayable — porque ya esta probado contra los modos de fallo reales
# (respuesta perdida tras el commit, proceso zombi con la misma identidad de
# maquina) y dos patrones distintos serian dos superficies de fallo.

SOLVE_LEASE_MINUTES = 20
MAX_SOLVE_CERTIFICATE_BYTES = solve.MAX_COMPRESSED_BYTES


@csrf_exempt
def api_solve_acquire(request):
    """Arrienda UNA tarea de prueba. Sin tareas, responde con la lista vacia."""
    user = _auth(request)
    if user is None:
        return JsonResponse({'error': 'bad credentials'}, status=403)
    ping = _touch_worker(request, user)
    machine = ping.machine
    lease_session = request.POST.get('lease_session', '')[:64]

    with atomic():
        now = timezone.now()
        stale = now - timedelta(minutes=SOLVE_LEASE_MINUTES)
        SolveTask.objects.filter(
            state='LEASED', leased_at__lt=stale,
        ).filter(Q(lease_heartbeat_at__isnull=True)
                 | Q(lease_heartbeat_at__lt=stale)).update(
            state='PENDING', machine='', lease_heartbeat_at=None,
            lease_token='', lease_session='')

        active = (SolveTask.objects.select_for_update()
                  .select_related('position')
                  .filter(state='LEASED', machine=machine,
                          lease_heartbeat_at__gte=stale)
                  .order_by('leased_at', 'id').first())
        if active is not None:
            if lease_session and secrets.compare_digest(
                    active.lease_session, lease_session):
                # The first response may have been lost after the commit.
                # Replay the same assignment without burning an attempt.
                return JsonResponse({'tasks': [_solve_payload(active)]})
            # REINICIO frente a ROBO, el mismo relevo que ya tiene api_lease
            # (incidente t24).  Una sesion distinta con el latido del titular
            # ya frio no es un segundo proceso robandole trabajo vivo: es su
            # relevo pidiendo con su misma identidad de maquina.  Sin esto un
            # solver relanzado se queda mirando los 20 minutos enteros de
            # SOLVE_LEASE_MINUTES con la cola llena.
            beat = active.lease_heartbeat_at or active.leased_at
            dead = now - timedelta(minutes=RESTART_RECYCLE_MINUTES)
            if beat is None or beat >= dead:
                return JsonResponse({'tasks': []})
            SolveTask.objects.filter(id=active.id, state='LEASED').update(
                state='PENDING', machine='', lease_heartbeat_at=None,
                lease_token='', lease_session='')
            # El relevo no espera al proximo poll: con el zombi reciclado,
            # ESTA misma peticion baja a elegir tarea.

        # Orden: primero todo lo que NO es deuda (peticiones, piloto), y
        # dentro de cada grupo el presupuesto mayor primero.  La deuda de
        # certificacion es importante pero nunca urgente: si se sirviera por
        # tamano de presupuesto se colaria delante por ser barata.
        task = (SolveTask.objects.select_for_update(skip_locked=True)
                .select_related('position').filter(state='PENDING')
                .annotate(is_debt=Case(
                    When(arm=ingest.DEBT_ARM, then=Value(1)),
                    default=Value(0), output_field=IntegerField()))
                .order_by('is_debt', '-budget_nodes', 'id').first())
        if task is None:
            return JsonResponse({'tasks': []})
        assigned_at = timezone.now()
        task.state, task.machine, task.leased_at = 'LEASED', machine, assigned_at
        task.lease_heartbeat_at = assigned_at
        task.lease_token = secrets.token_urlsafe(32)
        task.lease_session = lease_session
        task.attempts += 1
        task.save(update_fields=['state', 'machine', 'leased_at',
                                 'lease_heartbeat_at', 'lease_token',
                                 'lease_session', 'attempts'])
    return JsonResponse({'tasks': [_solve_payload(task)]})


def _solve_payload(task):
    return {
        'id': task.id,
        'fen': task.position.fen,
        'goal': task.goal,
        'stage': task.budget_stage,
        'budget_nodes': task.budget_nodes,
        'ruleset': logic.RULESET_ID,
        'lease_token': task.lease_token,
    }


@csrf_exempt
def api_solve_heartbeat(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    user = _auth(request)
    if user is None:
        return JsonResponse({'error': 'bad credentials'}, status=403)
    ping = _touch_worker(request, user)
    try:
        task_id = int(request.POST.get('task_id', ''))
    except (TypeError, ValueError):
        return JsonResponse({'error': 'invalid task_id'}, status=400)
    renewed = (SolveTask.objects
               .filter(id=task_id, state='LEASED', machine=ping.machine,
                       lease_token=request.POST.get('lease_token', ''))
               .update(lease_heartbeat_at=timezone.now()))
    if renewed != 1:
        return JsonResponse({'error': 'stale-lease'}, status=409)
    return JsonResponse({'ok': True, 'current_task_id': task_id})


@csrf_exempt
def api_solve_submit(request):
    """Recibe un resultado SOLVE. El certificado se VERIFICA aqui, entero."""
    user = _auth(request)
    if user is None:
        return JsonResponse({'error': 'bad credentials'}, status=403)
    try:
        task_id = int(request.POST['task_id'])
    except (KeyError, TypeError, ValueError):
        return JsonResponse({'error': 'malformed: task_id'}, status=400)
    outcome = request.POST.get('outcome', '')
    if outcome not in ('PROVED', 'DISPROVED', 'UNKNOWN'):
        return JsonResponse({'error': 'malformed: outcome'}, status=400)
    machine = request.POST.get('machine', '')
    provided_token = request.POST.get('lease_token', '')

    try:
        task = SolveTask.objects.select_related('position').get(id=task_id)
    except SolveTask.DoesNotExist:
        return JsonResponse({'error': 'malformed: unknown task'}, status=400)
    if task.state == 'COMPLETED':
        return JsonResponse({'ok': True, 'dup': True})
    if task.state != 'LEASED':
        return JsonResponse({'error': 'not-leased'}, status=400)
    if not machine or machine != task.machine:
        return JsonResponse({'error': 'not-your-lease'}, status=409)
    if task.lease_token and not secrets.compare_digest(task.lease_token,
                                                       provided_token):
        return JsonResponse({'error': 'stale-lease'}, status=409)

    certificate = request.FILES.get('certificate')
    blob = certificate.read(MAX_SOLVE_CERTIFICATE_BYTES + 1) \
        if certificate is not None else b''
    if len(blob) > MAX_SOLVE_CERTIFICATE_BYTES:
        return JsonResponse({'error': 'certificate too large'}, status=413)

    def as_int(name):
        try:
            return int(request.POST.get(name, ''))
        except (TypeError, ValueError):
            return None

    # Telemetria de fortaleza: ADVISORY, saneada, y jamas capaz de cambiar un
    # veredicto (doc 18 §5: el clasificador solo afecta al scheduling).
    telemetry = {}
    for name in ('tt_hit', 'quiet_scc', 'reset_rate', 'stagnation',
                 'score'):
        raw = request.POST.get(f'fortress_{name}')
        if raw in (None, ''):
            continue
        try:
            telemetry[name] = max(0.0, min(1e9, float(raw)))
        except (TypeError, ValueError):
            continue

    trusted = set(getattr(settings, 'ATOMICDB_TB_TRUSTED', ()))
    trusted_submitter = bool(getattr(user, 'is_staff', False)
                             or user.username in trusted)

    summary = ingest.apply_solve_result(
        task, outcome=outcome, certificate_blob=blob or None,
        advisory_pn=as_int('pn'), advisory_dn=as_int('dn'),
        searched_nodes=as_int('nodes') or 0,
        elapsed_seconds=request.POST.get('elapsed', ''),
        telemetry=telemetry or None, trusted_submitter=trusted_submitter,
        solver_build=_sha_field(request.POST.get('solver_build', ''))
        or request.POST.get('solver_build', '')[:64])
    return JsonResponse({'ok': True, 'summary': summary})


# Sin limite de peticiones masivas (orden 28-jul: "lo volveremos a poner si
# se complica; ahora la queue va muy rapida").  El evento BULK_REQUEST se
# sigue registrando por IP: es la auditoria que permitiria reintroducir un
# limite informado el dia que haga falta.


def api_request_unexplored(request, key):
    """Pide TODAS las jugadas sin mirar de una posicion, de una vez.

    El completado automatico de cobertura solo actua cuando el nodo esta a
    punto de cerrarse.  Esto es la version humana: alguien mira una posicion,
    ve doce filas que dicen "unexplored" y decide que merecen atencion.

    SIN ``csrf_exempt``, a diferencia del protocolo de workers: esto no lo
    llama un worker sin navegador, lo llama el explorador — que tiene el token
    en la pagina y lo manda en ``X-CSRFToken``.  Exento, una pagina de terceros
    podia encolar hasta ``UNEXPLORED_CLICK_CAP`` analisis a nombre de quien la
    visitara estando logueado, y quedaban a su nombre.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return JsonResponse({'status': 'unknown-position'}, status=404)
    if pos.status != 'UNKNOWN':
        return JsonResponse({'status': 'already-solved'})
    # La misma puerta que el boton de una sola jugada: una expansion masiva no
    # tiene MENOS motivos para respetar el tope de cola que una peticion
    # suelta, y era el unico camino que entraba sin mirarlo.
    if AnalysisTask.objects.filter(state='PENDING', source='USER',
                                   position__status='UNKNOWN') \
                           .count() >= REQUEST_QUEUE_MAX:
        return JsonResponse({'status': 'queue-full'}, status=503)

    ip = _client_ip(request)

    # La ruta declarada del PETICIONARIO hasta esta pagina, igual que en
    # api_request: cada tarea hija hereda ruta + su jugada, y el aviso de
    # vuelta habla en el orden del autor y no en el linaje canonico (el bug
    # reportado: pedia por 1.Nf3 d6 y la campana contaba 1.Nf3 f6).
    route = ''
    raw_route = (request.POST.get('route') or '').strip()
    if raw_route:
        try:
            _validated_play_route(raw_route, key)
            route = raw_route
        except (PlayRouteError, PlayRouteConflict):
            route = ''

    with atomic():
        pos = Position.objects.select_for_update().get(key=key)
        pending = ingest.unexplored_children(pos)
        if not pending:
            return JsonResponse({'status': 'nothing-to-do', 'queued': 0})
        queued = ingest.enqueue_unexplored_children(
            pos, requested_by=(request.user.username
                               if request.user.is_authenticated else ''),
            route=route)
        RequestLog.objects.create(ip=ip, position=pos)
        DBEvent.objects.create(kind='BULK_REQUEST', payload={
            'ip': ip, 'key': pos.key, 'queued': queued,
            'candidates': len(pending)})
    return JsonResponse({'status': 'queued', 'queued': queued,
                         'candidates': len(pending)})


def api_pv_verify(request, key):
    """Encola analisis por cada posicion de la PV vigente de esta posicion.

    El boton de arriba compra PROFUNDIDAD aqui y el masivo compra ANCHURA aqui;
    este compra la LINEA. Es la respuesta al nodo cuyo eval propio profundo y
    cuyo respaldo no se ponen de acuerdo: la PV almacenada reclama algo, y lo
    unico que puede confirmarlo o tumbarlo es lo que digan los analisis de las
    posiciones por las que pasa.  Hasta ahora eso se hacia pidiendo analisis a
    mano, posicion por posicion.

    SIN ``csrf_exempt``, por lo mismo que sus dos vecinos: esto lo llama el
    fetch de explore.html, que tiene el token en la pagina.  Exento, una pagina
    de terceros podia encolar hasta ``PV_VERIFY_MAX_PLIES`` analisis a nombre
    de quien la visitara con sesion iniciada.

    NO escribe ``RequestLog``, y es deliberado: ese recibo es el dedup por
    ip+posicion del boton de peldano, y esto no compra un peldano en ESTA
    posicion — pone tareas en las de mas abajo.  Anotarlo aqui haria que el
    siguiente click en "Request analysis" se leyera como repetido.  Lo que si
    respeta, porque acota el gasto de verdad, es el tope de cola.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return JsonResponse({'status': 'unknown-position'}, status=404)
    if pos.status != 'UNKNOWN':
        return JsonResponse({'status': 'already-solved'})
    if AnalysisTask.objects.filter(state='PENDING', source='USER',
                                   position__status='UNKNOWN') \
                           .count() >= REQUEST_QUEUE_MAX:
        return JsonResponse({'status': 'queue-full'}, status=503)
    # Afinidad worker-peticionario, igual que en ``api_request``: la cuenta OB
    # del visitante logueado viaja hasta cada tarea de la linea.
    requested_by = (request.user.username
                    if request.user.is_authenticated else '')
    # Y la ruta declarada del autor: cada tarea de la PV hereda ruta + los
    # plies caminados, y el aviso vuelve en SU orden de jugadas.
    route = ''
    raw_route = (request.POST.get('route') or '').strip()
    if raw_route:
        try:
            _validated_play_route(raw_route, key)
            route = raw_route
        except (PlayRouteError, PlayRouteConflict):
            route = ''
    with atomic():
        pos = Position.objects.select_for_update().get(key=key)
        queued = ingest.enqueue_pv_verification(pos,
                                                requested_by=requested_by,
                                                route=route)
    if not queued:
        return JsonResponse({'status': 'nothing-to-do', 'queued': 0})
    return JsonResponse({'status': 'queued', 'queued': queued})


def _client_ip(request):
    """Ultima entrada de XFF: la puso nuestro nginx, el cliente no la falsea."""
    return (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[-1].strip()
            or request.META.get('REMOTE_ADDR', '0.0.0.0'))


def _api_request_once(request, key):
    """Peticion publica de analisis (estilo chessdb.cn), sin cuenta.

    Protecciones: dedup ip+posicion y tope global de cola.  SIN limite
    horario: el propietario lo retiro (28-jul) porque estaba frenando a la
    gente que de verdad usa el explorador mientras no frenaba a nadie mas —
    lo que acota el gasto real es el dedup (un click por posicion mientras su
    trabajo sigue vivo) y el techo de la cola, no un contador por IP.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    ip = _client_ip(request)
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return JsonResponse({'status': 'unknown-position'}, status=404)
    hour_ago = timezone.now() - timedelta(hours=1)
    # A recent click is only a duplicate while the request it represented is
    # still queued or running.  Once that task has completed, the same visitor
    # must be able to request the next 128M -> 512M -> 2B -> 10B rung.  A
    # RequestLog may also have been created merely by adding a new FEN, without
    # ever creating an AnalysisTask, so the log alone is not evidence of work.
    recent_same_position = RequestLog.objects.filter(
        ip=ip, created__gte=hour_ago, position=pos,
    ).exists()
    active_user_request = AnalysisTask.objects.filter(
        position=pos,
        source=AnalysisTask.Source.USER,
        state__in=(AnalysisTask.TState.PENDING, AnalysisTask.TState.LEASED),
    ).exists()
    if recent_same_position and not active_user_request \
            and ingest.ladder_exhausted(pos):
        # A frontier expansion puts its tasks on the CHILDREN, so the parent
        # row alone can no longer prove that the previous click is still
        # being served.  One click stays one expansion event.
        active_user_request = AnalysisTask.objects.filter(
            position__edges_in__parent=pos,
            source=AnalysisTask.Source.USER,
            state__in=(AnalysisTask.TState.PENDING,
                       AnalysisTask.TState.LEASED),
        ).exists()
    if recent_same_position and active_user_request:
        return JsonResponse({'status': 'already-requested'})
    if AnalysisTask.objects.filter(state='PENDING', source='USER',
                                   position__status='UNKNOWN') \
                           .count() >= REQUEST_QUEUE_MAX:
        return JsonResponse({'status': 'queue-full'}, status=503)
    # Task creation/promotion and its rate-limit receipt are one commit.  A
    # lock while writing RequestLog must roll the task mutation back as well;
    # otherwise a browser retry could accidentally request the next rung.
    requested_by = (request.user.username
                    if request.user.is_authenticated else '')
    # Ruta declarada por el peticionario (el ?play= de su pagina): validada
    # entera contra las reglas antes de guardarse.  Una ruta rota no tumba
    # la peticion — se pide igual y se pinta el linaje canonico.
    route = ''
    raw_route = (request.POST.get('route') or '').strip()
    if raw_route:
        try:
            _validated_play_route(raw_route, pos.key)
            route = raw_route
        except (PlayRouteError, PlayRouteConflict):
            route = ''
    with atomic():
        outcome = ingest.request_analysis(pos, requested_by=requested_by,
                                          route=route)
        # 'saturated' bought nothing, but it walked the tree to find that out
        # and the honest answer is still an answer: one click, one receipt,
        # so the per-position dedup keeps bounding what a descent costs us.
        if outcome in ('queued', 'already-queued', 'expanded', 'saturated'):
            RequestLog.objects.create(ip=ip, position=pos)
    payload = {'status': str(outcome)}
    # Only a frontier expansion carries counters; every other status keeps
    # the exact single-key body explore.html has always parsed.
    payload.update(getattr(outcome, 'detail', None) or {})
    if str(outcome) in ('queued', 'already-queued'):
        ahead = _user_queue_ahead(pos)
        if ahead is not None:
            payload['ahead'] = ahead
    return JsonResponse(payload)


def _user_queue_ahead(pos):
    """Cuantas tareas USER PENDING cobran antes que la de esta posicion.

    El mismo orden que ``choose_pending`` (nombradas por delante de las
    anonimas, FIFO dentro de cada estrato), sin el matiz own-first porque
    depende de que worker pregunte.  Es transparencia, no contrato: la cifra
    que ve el humano es "cuanta cola tengo delante", que era exactamente lo
    que el boton de espera no decia (Lesha, 31-jul: horas de espera con cara
    de 'waiting...' generico).
    """
    task = (AnalysisTask.objects
            .filter(position=pos, source=AnalysisTask.Source.USER,
                    state=AnalysisTask.TState.PENDING)
            .order_by('id').first())
    if task is None:
        return None
    named = AnalysisTask.objects.filter(
        state=AnalysisTask.TState.PENDING, source=AnalysisTask.Source.USER,
        requested_by__gt='')
    anon = AnalysisTask.objects.filter(
        state=AnalysisTask.TState.PENDING, source=AnalysisTask.Source.USER,
        requested_by='')
    if task.requested_by:
        return named.filter(id__lt=task.id).count()
    return named.count() + anon.filter(id__lt=task.id).count()


def api_request(request, key):
    """Peticion de analisis de UNA posicion, desde el explorador.

    SIN ``csrf_exempt``: la exencion del protocolo de workers existe porque un
    worker no tiene navegador ni token, y esto es exactamente lo contrario —
    un fetch de explore.html, que manda el token de la propia pagina en
    ``X-CSRFToken``.  Mientras estuvo exenta, cualquier pagina de terceros
    podia gastarle la escalera de peticiones a un visitante logueado, y las
    peticiones quedaban atribuidas a el.
    """
    try:
        return _api_request_once(request, key)
    except OperationalError as error:
        # SQLite can reject a read->write transaction upgrade immediately
        # while the worker is committing a large analysis, even with a long
        # busy_timeout.  The atomic task+receipt block above guarantees that a
        # structured busy response has no committed side effect and is safe
        # for the browser to retry.
        message = str(error).lower()
        if 'database is locked' not in message \
                and 'database table is locked' not in message:
            raise
        response = JsonResponse({'status': 'busy'}, status=503)
        response['Retry-After'] = '2'
        return response


# ---------------- paginas publicas ----------------

def _board_rows(fen):
    """Filas 8..1 de [(casilla, pieza)] para la plantilla SVG."""
    board = fen.split()[0]
    rows = []
    for rank_str in board.split('/'):
        row = []
        for ch in rank_str:
            if ch.isdigit():
                row.extend([''] * int(ch))
            else:
                row.append(ch)
        rows.append(row)
    return rows


def _piece_code(ch):
    if not ch:
        return ''
    color = 'w' if ch.isupper() else 'b'
    return color + ch.upper()


def _ctx_board(fen):
    rows = _board_rows(fen)
    out = []
    for r, row in enumerate(rows):
        rank = 8 - r
        out.append([(_piece_code(p), (r + c) % 2 == 1,
                     'abcdefgh'[c] + str(rank))
                    for c, p in enumerate(row)])
    return out


def _route_query(ucis, opening_anchor=None):
    """Canonical query suffix for validated route or signed opening state."""
    parts = []
    if ucis:
        parts.append('play=' + ','.join(ucis))
    if opening_anchor:
        parts.append(
            f'{OPENING_ANCHOR_PARAM}='
            + quote(opening_anchor, safe=''),
        )
    return '' if not parts else '?' + '&'.join(parts)


def _explore_url(key, ucis=None, opening_anchor=None):
    return (
        f'/atomicdb/explore/{key}/'
        + _route_query(ucis, opening_anchor)
    )


def _goto_url(key, uci, ucis=None, opening_anchor=None):
    return (
        f'/atomicdb/goto/{key}/{uci}/'
        + _route_query(ucis, opening_anchor)
    )


def _backed_source_url(key, ucis=None):
    """Enlace al caminante de la espina, con la ruta propia del que pincha.

    A diferencia de ``_explore_url``, una ruta VACIA tambien viaja: en la
    posicion inicial la historia del visitante es legitimamente de cero plies,
    y omitir el parametro haria que el destino cayese al linaje canonico en
    vez de extender lo que esa persona camino de verdad.  ``None`` (sin ruta
    validada, o ancla de desbordamiento) sigue siendo un enlace pelado.
    """
    base = f'/atomicdb/backed-source/{key}/'
    return base if ucis is None else base + '?play=' + ','.join(ucis)


def _signed_opening_anchor(target_key, match, route_ply):
    """Bind the last catalogued opening to one exact explorer target.

    The token is only an overflow continuation for a route that was already
    replayed and validated.  Names never come from the token: consumers load
    the signed opening key from the immutable catalogue again.
    """
    if match is None:
        return None
    opening_key = match.get('position_key')
    matched_ply = match.get('matched_ply')
    if (
        not isinstance(opening_key, str)
        or isinstance(matched_ply, bool)
        or not isinstance(matched_ply, int)
        or matched_ply < 0
        or isinstance(route_ply, bool)
        or not isinstance(route_ply, int)
        or route_ply < matched_ply
    ):
        return None
    exact = openings.lookup_key(opening_key)
    if exact is None or exact.get('position_key') != opening_key:
        return None
    return signing.dumps(
        {
            'v': 1,
            'target': target_key,
            'opening': opening_key,
            'matched_ply': matched_ply,
            'route_ply': route_ply,
        },
        salt=OPENING_ANCHOR_SALT,
        compress=True,
    )


def _validated_opening_anchor(raw, target_key):
    """Return a catalog-backed opening match and route ply, or fail closed."""
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw) > OPENING_ANCHOR_MAX_CHARS
    ):
        return None, None
    try:
        payload = signing.loads(raw, salt=OPENING_ANCHOR_SALT)
    except (signing.BadSignature, TypeError, ValueError):
        return None, None
    if (
        not isinstance(payload, dict)
        or set(payload) != {
            'v', 'target', 'opening', 'matched_ply', 'route_ply',
        }
        or payload.get('v') != 1
        or payload.get('target') != target_key
    ):
        return None, None
    opening_key = payload.get('opening')
    matched_ply = payload.get('matched_ply')
    route_ply = payload.get('route_ply')
    if (
        not isinstance(opening_key, str)
        or isinstance(matched_ply, bool)
        or not isinstance(matched_ply, int)
        or matched_ply < 0
        or isinstance(route_ply, bool)
        or not isinstance(route_ply, int)
        or route_ply < matched_ply
    ):
        return None, None
    exact = openings.lookup_key(opening_key)
    if exact is None or exact.get('position_key') != opening_key:
        return None, None
    match = dict(exact)
    match.update({
        'matched_ply': matched_ply,
        'current_key': target_key,
        'exact': opening_key == target_key,
    })
    return match, route_ply


def _navigation_opening(active_ucis, raw_anchor, target_key, *,
                        explicit_play=False):
    """Resolve opening state and the route that navigation should propagate.

    An explicitly supplied and validated ``play`` route wins.  Otherwise a
    valid target-bound opening anchor wins over reconstructed lineage: the
    latter may be a shorter transposition and must not erase overflow state.
    """
    if explicit_play and active_ucis is not None:
        try:
            match = openings.match_line(active_ucis)
        except openings.InvalidOpeningLine:
            match = None
        return match, len(active_ucis), active_ucis
    anchored, route_ply = _validated_opening_anchor(raw_anchor, target_key)
    if anchored is not None:
        return anchored, route_ply, None
    if active_ucis is not None:
        try:
            match = openings.match_line(active_ucis)
        except openings.InvalidOpeningLine:
            match = None
        return match, len(active_ucis), active_ucis
    exact = openings.lookup_key(target_key)
    if exact is None:
        return None, None, None
    return exact, exact['matched_ply'], None


def _opening_after_move(current, child_key, child_ply):
    """Advance a trusted opening anchor by one already-validated legal move."""
    exact = openings.lookup_key(child_key)
    if exact is not None:
        result = dict(exact)
        if child_ply is not None:
            result['matched_ply'] = child_ply
        result.update({'current_key': child_key, 'exact': True})
        return result
    if current is None:
        return None
    result = dict(current)
    result.update({'current_key': child_key, 'exact': False})
    return result


def _child_navigation_state(active_ucis, current_opening, route_ply,
                            child_key, move_uci):
    """Choose a bounded replay route or a signed overflow anchor for a child."""
    child_ply = None if route_ply is None else route_ply + 1
    child_opening = _opening_after_move(
        current_opening, child_key, child_ply)
    child_route = (
        None if active_ucis is None else [*active_ucis, move_uci]
    )
    if child_route is not None and len(child_route) <= PLAY_ROUTE_MAX_PLIES:
        return child_route, None
    anchor_ply = child_ply
    if anchor_ply is None and child_opening is not None:
        anchor_ply = child_opening.get('matched_ply')
    child_anchor = _signed_opening_anchor(
        child_key, child_opening, anchor_ply)
    return None, child_anchor


def _validated_play_route(raw, target_key):
    """Return a read-only, startpos-rooted route or ``None``.

    ``play`` is deliberately a compact list of UCI tokens rather than trusted
    lineage.  Every move is replayed with the Atomic rules, the terminal key
    must be the requested page, and every prefix must already exist in
    AtomicDB.  This makes a GET incapable of materialising a route merely by
    naming one.
    """
    if raw is None:
        return None
    if len(raw) > PLAY_ROUTE_MAX_CHARS:
        raise PlayRouteError('move path exceeds the supported length')
    ucis = [] if raw == '' else raw.split(',')
    if len(ucis) > PLAY_ROUTE_MAX_PLIES or any(
            not PLAY_UCI_RE.fullmatch(uci) for uci in ucis):
        raise PlayRouteError('move path contains an invalid UCI token')

    import pyffish as pf

    fen = logic.start_fen()
    prefix_keys = [logic.key_of(fen)]
    white = True
    line = []
    for uci in ucis:
        if uci not in logic.legal_moves(fen):
            raise PlayRouteError('move path contains an illegal Atomic move')
        try:
            san = pf.get_san('atomic', fen, uci)
            fen = logic.apply_move(fen, uci)
        except Exception as exc:
            raise PlayRouteError(
                'move path could not be replayed under Atomic rules') from exc
        child_key = logic.key_of(fen)
        prefix_keys.append(child_key)
        line.append({
            'uci': uci, 'san': san, 'key': child_key, 'white': white,
        })
        white = not white

    if prefix_keys[-1] != target_key:
        raise PlayRouteConflict(
            'move path does not reach the requested position')
    positions = Position.objects.only('key', 'fen').in_bulk(prefix_keys)
    if any(key not in positions for key in prefix_keys):
        raise PlayRouteConflict(
            'move path is not fully materialized in AtomicDB')
    return positions[prefix_keys[0]], line, ucis


def _play_route_error_response(exc):
    response = HttpResponse(
        str(exc), status=exc.status_code, content_type='text/plain')
    response['Cache-Control'] = 'no-store'
    return response


def _canonical_route(pos):
    """Fallback lineage plus a replayable route when startpos was reached."""
    top, line = _line_to_root(pos)
    if top.fen != logic.start_fen():
        return top, line, None
    return top, line, [step['uci'] for step in line]


def goto(request, key, uci):
    """Navegacion jugando: valida la jugada, crea/encuentra el hijo y salta."""
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return redirect('/atomicdb/')
    raw_play = request.GET.get('play')
    board_anchor = None
    if (
        isinstance(raw_play, str)
        and raw_play.startswith(OPENING_ANCHOR_PLAY_PREFIX)
    ):
        board_anchor = raw_play[len(OPENING_ANCHOR_PLAY_PREFIX):]
        raw_play = None
    try:
        route = _validated_play_route(raw_play, key)
    except PlayRouteError as exc:
        return _play_route_error_response(exc)
    if route is None:
        _top, _line, active_ucis = _canonical_route(pos)
    else:
        _top, _line, active_ucis = route
    current_opening, route_ply, active_ucis = _navigation_opening(
        active_ucis,
        request.GET.get(OPENING_ANCHOR_PARAM) or board_anchor,
        pos.key,
        explicit_play=route is not None,
    )
    current_anchor = (
        None if active_ucis is not None
        else _signed_opening_anchor(pos.key, current_opening, route_ply)
    )
    if uci not in logic.legal_moves(pos.fen):
        return redirect(_explore_url(key, active_ucis, current_anchor))
    # Misma herencia que en ``ingest.expand``: un nodo que nace de un click
    # bajo una linea de campana es de esa campana.  Por ``campaign_id``, que
    # es lo unico que hace falta y no cuesta una consulta a ``Campaign``.
    child = ingest.get_or_create_position(logic.apply_move(pos.fen, uci),
                                          campaign_id=pos.campaign_id)
    Edge.objects.get_or_create(parent=pos, move_uci=uci,
                               defaults={'child': child})
    if child.status != 'UNKNOWN':
        # Navegar puede DESCUBRIR un terminal (Bxe7 que explota al rey es
        # mate en 1 nada mas crear la arista).  Sin esta cascada el padre
        # seguia diciendo UNSOLVED +302 con un hijo WHITE_WIN TERMINAL en la
        # tabla hasta que algun analisis ajeno tocara la familia — el
        # "delay" que reporto la comunidad (29-jul).  El descubridor paga
        # las cuatro consultas de su hallazgo.
        ingest.backup_cascade([pos.key])
        ingest.backup_backed_evals([pos.key])
    if child.priority <= ingest.DEAD / 2:
        child.priority = 0.0   # ruta nueva: revive de la lapida
        child.save(update_fields=['priority'])
    child_route, child_anchor = _child_navigation_state(
        active_ucis, current_opening, route_ply, child.key, uci)
    return redirect(_explore_url(child.key, child_route, child_anchor))


def _walk_backed_spine(pos):
    """Baja por la espina de soporte hasta donde NACE el valor respaldado.

    Devuelve ``(origen, ucis_caminados)``.  El valor de un nodo respaldado no
    es suyo: se lo presta ``backed_move``, y ese hijo puede a su vez estar
    prestandolo.  Bajar a mano esa cadena es lo que este paseo ahorra.

    PARADAS, todas ellas "este nodo ES el origen":
      * ``status != UNKNOWN``: el valor viene de un cierre PROBADO, y por
        debajo de una prueba no hay nada que buscar.
      * sin ``backed_move``, o ``backed_eval`` a None: no hay espina.
      * ``backed_eval == eval_cp``: la busqueda propia del nodo ancla el
        valor; el respaldo solo coincide con lo que el motor ya dijo aqui.
    Y dos paradas defensivas, que dejan al visitante donde el paseo llego:
      * la arista ``backed_move`` no existe (respaldo mas viejo que el grafo).
      * el hijo ya estaba visitado: el DAG transpone y un ciclo colgaria esto.
    El tope de ``ingest.BACKED_MAX_PLIES`` es el mismo con el que subio el
    valor, asi que ninguna espina legitima puede ser mas larga que el.

    Una consulta por paso como mucho.  Es un GET esporadico de un click
    humano, no algo que se pague al pintar una tabla.
    """
    node, walked, seen = pos, [], {pos.key}
    for _ in range(ingest.BACKED_MAX_PLIES):
        if node.status != 'UNKNOWN':
            break
        if not node.backed_move or node.backed_eval is None:
            break
        if node.backed_eval == node.eval_cp:
            break
        try:
            edge = (Edge.objects.select_related('child')
                    .get(parent=node, move_uci=node.backed_move))
        except Edge.DoesNotExist:
            break
        if edge.child.key in seen:
            break
        seen.add(edge.child.key)
        walked.append(node.backed_move)
        node = edge.child
    return node, walked


def backed_source(request, key):
    """Salta al ply donde NACE el valor respaldado de esta posicion.

    Un GET puro: lee, redirige y no escribe nada — ni siquiera marca avisos
    como vistos, porque de eso ya se encarga el ``explore`` de destino.

    La ruta declarada en ``?play=`` se revalida entera contra la posicion DE
    PARTIDA, igual que en ``api_request``: un enlace no puede materializar una
    historia con solo nombrarla.  Si vale, el destino la recibe extendida con
    los ucis de la espina y el visitante aterriza con SU partida, no con un
    linaje canonico reconstruido.  Si no vale (o no viene), redirect pelado.
    """
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return render(request, 'atomicdb/missing.html', status=404)
    try:
        route = _validated_play_route(request.GET.get('play'), pos.key)
    except PlayRouteError:
        route = None
    origin, walked = _walk_backed_spine(pos)
    ucis = None
    if route is not None:
        _top, _line, play_ucis = route
        ucis = [*play_ucis, *walked]
    # El destino revalida la ruta y RECHAZA la que se pase de largo.  Una
    # espina profunda bajo una partida larga puede desbordar el tope, y
    # entonces el enlace propio serviria una pagina de error: mejor llegar sin
    # historia que no llegar.
    if ucis is not None and len(ucis) > PLAY_ROUTE_MAX_PLIES:
        ucis = None
    response = redirect(_explore_url(origin.key, ucis))
    # El origen se mueve en cuanto cae analisis nuevo bajo esta posicion: este
    # 302 no es cacheable por nadie.
    response['Cache-Control'] = 'no-store'
    return response


# A truncated breadcrumb keeps its HEAD and its TAIL, with the ellipsis in the
# middle: the head is what identifies the opening ("1. Nf3 f6 2. Nc3") and the
# tail is what says where you are now.  Dropping the head was the bug — it
# produced "… d4 Bc4 Bg7 d3" and left the reader with no idea which line that
# was.
SAN_HEAD_PLIES = 6
SAN_TAIL_PLIES = 8


def _format_san_line(top, line, max_plies=16, keep_head=False):
    """Numbered SAN breadcrumb from the root ("1. Nf3 f6 …").

    THE LINE ALWAYS STARTS AT THE ROOT when the walk reached it.  When it is
    too long, the ellipsis goes in the MIDDLE.

    When the reverse walk could NOT reach the root inside its budget, the
    canonical FEN does not record the absolute ply, so numbering the fragment
    would be inventing a "1..." that may be false.  It says so instead of
    printing a mute tail: a labelled fragment is honest, an unlabelled one
    reads like a bug — and it was reported as one.
    """
    if not line:
        return '' if top.fen == logic.start_fen() else '…'
    from_start = top.fen == logic.start_fen()

    if not from_start:
        # Two different reasons the walk did not reach the root, and only one
        # of them is the reported bug.
        #
        # It ran out of BUDGET — plies, or the node ceiling inside a
        # transposition-heavy component (the walk marks that itself): the
        # line really is deeper than what the walk could cross, and a bare
        # tail reads as broken.  Say how deep it is.
        if (len(line) >= LINEAGE_SEARCH_MAX_PLIES
                or getattr(top, 'lineage_capped', False)):
            tail = ' '.join(st['san'] for st in line[-4:])
            return f'deep line, ply ≥{len(line)} · … {tail}'
        # Or it ran out of ANCESTORS: a short fragment whose link to the root
        # is genuinely missing (a FEN someone pasted, an edge never
        # materialised).  That is not depth and must not claim to be; the page
        # already says "lineage unavailable" beside it.
        return '… ' + ' '.join(st['san'] for st in line)

    parts = []
    n = 1
    for i, st in enumerate(line):
        if st['white']:
            parts.append(f"{n}. {st['san']}")
        else:
            parts.append(f"{n}... {st['san']}" if i == 0 else st['san'])
            n += 1

    if len(parts) <= max_plies:
        return ' '.join(parts)
    if keep_head:
        return ' '.join(parts[:max_plies]) + ' …'
    # Presupuestos cortos (previews de portada): reparto equilibrado.  Con el
    # reparto fijo 6+8, un preview de 10 dejaba UN ply de cabeza; y cortar
    # solo por el final hacia que posiciones distintas de la misma linea
    # profunda pintaran labels IDENTICOS — un worker avanzando por la linea
    # parecia encallado en la misma tarea (incidente 29-jul).
    head = min(SAN_HEAD_PLIES, max(2, (max_plies - 1) // 2))
    tail = max(1, max_plies - head - 1)
    return ' '.join(parts[:head]) + ' … ' + ' '.join(parts[-tail:])


def _walk_lines_to_root(keys, max_plies=LINEAGE_SEARCH_MAX_PLIES):
    """Resolve canonical minimum-ply breadcrumbs from startpos in batches.

    The home page contains queue rows and milestones with heavily overlapping
    ancestry. Resolving each independently caused hundreds of small queries.
    A single-parent greedy walk is not safe here: reversible moves and
    transpositions can make the lexicographically first parent enter a cycle
    even though another parent leads to startpos. Reverse BFS explores every
    parent at the current distance, remains cycle-safe, and uses one Edge query
    per ply for all requested positions, up to the public-search ceilings.

    This is the expensive half.  Callers go through ``_lines_to_root``, which
    only sends it the keys a short-lived cache could not answer.
    """
    import pyffish as pf

    keys = list(dict.fromkeys(key for key in keys if key))
    positions = Position.objects.only('key', 'fen', 'status').in_bulk(keys)
    start_key = logic.key_of(logic.start_fen())
    states = {}
    for key in keys:
        if key not in positions:
            continue
        states[key] = {
            'frontier': {key},
            'seen': {key},
            # For every discovered ancestor: the first edge towards target.
            'toward_target': {},
            'nodes': {key: positions[key]},
            'distance': {key: 0},
            'sources': [],
            'found_start': key == start_key,
        }
    active = {
        key for key, state in states.items() if not state['found_start']
    }

    for _ in range(min(max_plies, LINEAGE_SEARCH_MAX_PLIES)):
        child_ids = set()
        for key in active:
            child_ids.update(states[key]['frontier'])
        if not child_ids:
            break
        parents_by_child = {}
        root_first = Case(
            When(parent_id=start_key, then=Value(0)),
            default=Value(1), output_field=IntegerField(),
        )
        # Entre varios padres transpuestos, el breadcrumb prefiere los NO
        # resueltos: un orden de jugadas que atraviesa un nodo ya solved es
        # un camino muerto y leerlo como "la linea" es absurdo (ubdip,
        # 29-jul).  Preferencia, no exclusion: si TODOS los caminos pasan
        # por nodos resueltos, se ensena el mas corto igual que siempre.
        alive_first = Case(
            When(parent__status='UNKNOWN', then=Value(0)),
            default=Value(1), output_field=IntegerField(),
        )
        edges = (Edge.objects.filter(child_id__in=child_ids)
                 .select_related('parent')
                 .only('child_id', 'move_uci', 'parent__key', 'parent__fen',
                       'parent__status')
                 .annotate(_lineage_rank=Window(
                     expression=RowNumber(),
                     partition_by=[F('child_id')],
                     order_by=(
                         root_first.asc(), alive_first.asc(),
                         F('parent_id').asc(), F('move_uci').asc(),
                     ),
                 ))
                 .filter(_lineage_rank__lte=
                         LINEAGE_SEARCH_MAX_PARENTS_PER_CHILD)
                 # Rank-first ordering gives every frontier child its best
                 # parent before any child receives a second one. The root
                 # edge, when present, is always rank 1.
                 .order_by('_lineage_rank', 'child_id',
                           'parent_id', 'move_uci')
                 [:LINEAGE_SEARCH_MAX_EDGE_ROWS])
        for edge in edges:
            parents_by_child.setdefault(edge.child_id, []).append(edge)
        next_active = set()
        for key in sorted(active):
            state = states[key]
            next_frontier = set()
            # En los puntos de UNION del DAG, el primer hijo que reclama a un
            # padre decide el camino del breadcrumb.  Los nodos vivos van
            # primero para que la linea reconstruida atraviese el arbol
            # abierto y no un orden ya resuelto (la preferencia de arriba
            # solo ordenaba la admision por-hijo, no la eleccion de rama).
            def _alive_then_key(node_key, _nodes=state['nodes']):
                node = _nodes.get(node_key)
                solved = (getattr(node, 'status', 'UNKNOWN') != 'UNKNOWN'
                          if node is not None else False)
                return (1 if solved else 0, node_key)
            for child_key in sorted(state['frontier'], key=_alive_then_key):
                child_parents = parents_by_child.get(child_key, ())
                if not child_parents:
                    state['sources'].append(
                        (state['distance'][child_key], child_key))
                for edge in child_parents:
                    parent_key = edge.parent_id
                    if parent_key in state['seen']:
                        continue
                    # Always admit startpos when it is on this layer. Other
                    # ancestry is bounded so a highly transposed component
                    # cannot turn the public home page into a graph scan.
                    if (parent_key != start_key
                            and (len(state['seen'])
                                 >= LINEAGE_SEARCH_MAX_NODES
                                 or len(next_frontier)
                                 >= LINEAGE_SEARCH_MAX_FRONTIER)):
                        continue
                    state['seen'].add(parent_key)
                    state['nodes'][parent_key] = edge.parent
                    state['distance'][parent_key] = (
                        state['distance'][child_key] + 1)
                    state['toward_target'][parent_key] = (
                        child_key, edge.move_uci)
                    next_frontier.add(parent_key)
            state['frontier'] = next_frontier
            if start_key in state['seen']:
                state['found_start'] = True
                continue
            if next_frontier:
                next_active.add(key)
        active = next_active
        if not active:
            break

    result = {}
    for key, state in states.items():
        if state['found_start']:
            top_key = start_key
        elif state['sources']:
            # Prefer the available boundary that provides the most context.
            _distance, top_key = min(
                state['sources'],
                key=lambda source: (-source[0], source[1]),
            )
        else:
            # A closed cyclic component or a walk ceiling (depth, or the
            # node budget that a transposition-heavy shuffle component
            # exhausts long before reaching startpos): retain the longest
            # acyclic context found, but do not pretend it is move 1.
            top_key = min(
                state['seen'],
                key=lambda node_key: (-state['distance'][node_key], node_key),
            )
            state['capped'] = True

        ordered = []
        cursor = top_key
        while cursor != key:
            step = state['toward_target'].get(cursor)
            if step is None:
                ordered = []
                top_key = key
                break
            child_key, move_uci = step
            ordered.append((move_uci, child_key))
            cursor = child_key

        top = state['nodes'][top_key]
        if state.get('capped'):
            # The walk hit its OWN ceilings without finding a real boundary.
            # The renderer must read this as "deep", never as "unrooted":
            # an endgame shuffle sits in a transposition component that the
            # bounded reverse BFS cannot cross, and painting it as an orphan
            # fragment is exactly the reported head-drop bug again.
            top.lineage_capped = True
        ucis = [uci for uci, _child_key in ordered]
        try:
            # One C++ call parses and advances the full line. Calling
            # get_san()+get_fen() for every ply dominated the home page once
            # milestones grew into long PVs (roughly 500 crossings/request).
            sans = list(pf.get_san_moves('atomic', top.fen, ucis))
            if len(sans) != len(ordered):
                raise ValueError('incomplete SAN line')
        except Exception:
            # Preserve the previous per-ply behaviour if this PyFFish build
            # cannot batch a line or returns an incomplete result.
            sans, fen = [], top.fen
            for uci in ucis:
                try:
                    sans.append(pf.get_san('atomic', fen, uci))
                except Exception:
                    sans.append(uci)
                fen = logic.apply_move(fen, uci)
        white = top.fen.split()[1] == 'w'
        line = []
        for (uci, child_key), san in zip(ordered, sans):
            line.append({
                'uci': uci, 'san': san, 'key': child_key, 'white': white,
            })
            white = not white
        result[key] = (top, line)
    return result


class _LineageTop:
    """The part of a top position a breadcrumb actually reads.

    The walk hands back a live ``Position`` with ``lineage_capped`` glued on
    as an attribute, and the renderers downstream read two things from it:
    ``fen`` (is this the root? whose move is it?) and that marker.  ``key``
    is carried too because it names the node the line starts from.  Nothing
    else — an ORM row is not a cache payload, it drags a database identity
    and a row of live state into an entry meant to outlive the request.

    Both the hit and the miss path return one of these, on purpose: if the
    shape only appeared after the first render, the difference would surface
    as a bug that needs traffic and a warm cache to reproduce.
    """

    __slots__ = ('key', 'fen', 'lineage_capped')

    def __init__(self, key, fen, lineage_capped=False):
        self.key = key
        self.fen = fen
        self.lineage_capped = lineage_capped


def _lineage_cache_key(key, max_plies):
    # The ply budget is part of the identity: the explorer walks 64 and the
    # home page walks 96, and the same position can be rooted under one and
    # "deep line" under the other.  One shared entry would let whichever
    # page rendered first decide what the other one says.
    return f'atomicdb.lineage.v{LINEAGE_CACHE_VERSION}.{max_plies}.{key}'


def _lineage_payload(top, line):
    """Serialisable form of one resolved breadcrumb."""
    return {
        'top_key': top.key,
        'top_fen': top.fen,
        # This marker is why the payload is explicit rather than a pickled
        # object: it is the difference between "deep line, ply ≥96" and a
        # headless "… Bc4 Bd7" fragment, and it travels as an injected
        # attribute that any serialisation would silently drop.
        'capped': bool(getattr(top, 'lineage_capped', False)),
        'line': [
            {'uci': step['uci'], 'san': step['san'],
             'key': step['key'], 'white': step['white']}
            for step in line
        ],
    }


def _lineage_from_payload(payload):
    top = _LineageTop(payload['top_key'], payload['top_fen'])
    if payload['capped']:
        top.lineage_capped = True
    return top, [dict(step) for step in payload['line']]


def _lines_to_root(keys, max_plies=LINEAGE_SEARCH_MAX_PLIES):
    """Cached front door to the reverse walk.

    Everything the explorer shows about the CURRENT position — evals, move
    rows, proof numbers — stays uncached and is read fresh on every render.
    Only the line BACK to the root, which is what actually costs the queries,
    is served from here.
    """
    keys = list(dict.fromkeys(key for key in keys if key))
    if not keys:
        return {}
    budget = min(max_plies, LINEAGE_SEARCH_MAX_PLIES)
    ttl = LINEAGE_CACHE_SECONDS
    if ttl <= 0:
        return _walk_lines_to_root(keys, max_plies=max_plies)

    cache_keys = {key: _lineage_cache_key(key, budget) for key in keys}
    stored = cache.get_many(list(cache_keys.values()))
    resolved, misses = {}, []
    for key in keys:
        payload = stored.get(cache_keys[key])
        if payload is None:
            misses.append(key)
        else:
            resolved[key] = _lineage_from_payload(payload)

    if misses:
        fresh = {}
        for key, (top, line) in _walk_lines_to_root(
                misses, max_plies=max_plies).items():
            payload = _lineage_payload(top, line)
            fresh[cache_keys[key]] = payload
            # Round-trip even on the miss, so a hit and a miss are the same
            # object shape.
            resolved[key] = _lineage_from_payload(payload)
        if fresh:
            # Keys the walk did not return are positions AtomicDB does not
            # have yet.  Not caching that absence is deliberate: `goto`
            # materialises positions, and a two-minute "no such node" would
            # be a broken explorer on the click right after seeding one.
            cache.set_many(fresh, ttl)

    return {key: resolved[key] for key in keys if key in resolved}


def _line_labels_many(keys, preview_plies=10):
    resolved = _lines_to_root(keys)
    labels = {}
    for key, (top, line) in resolved.items():
        full = _format_san_line(top, line, max_plies=512, keep_head=True)
        # Preview con elipsis EN MEDIO: la cabeza dice que apertura es y la
        # cola dice DONDE va — dos posiciones distintas de la misma linea
        # profunda nunca deben compartir label.
        preview = _format_san_line(top, line, max_plies=preview_plies)
        labels[key] = (preview, full)
    return labels


def _line_labels(key, preview_plies=10):
    """Opening-first preview and full breadcrumb from one batched traversal."""
    return _line_labels_many([key], preview_plies).get(key, ('', ''))


def _route_labels(route, target_key, preview_plies=10):
    """(preview, full) desde la RUTA DECLARADA del peticionario, o None.

    El DAG transpone y el linaje canonico puede pintar la peticion en un
    orden de jugadas que su autor no reconoce.  La ruta llego validada al
    guardarse; aqui se re-valida entera (legalidad, llegada al objetivo,
    prefijos materializados) porque el arbol pudo cambiar debajo — ante
    cualquier duda, None y el linaje canonico de siempre."""
    try:
        top, line, _ucis = _validated_play_route(route, target_key)
    except (PlayRouteError, PlayRouteConflict):
        return None
    full = _format_san_line(top, line, max_plies=512, keep_head=True)
    preview = _format_san_line(top, line, max_plies=preview_plies)
    return preview, full


def _san_line(key, max_plies=16, keep_head=False):
    """Backward-compatible single label helper."""
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return ''
    top, line = _line_to_root(pos, max_plies=max(64, max_plies))
    return _format_san_line(top, line, max_plies=max_plies,
                            keep_head=keep_head)


# EL FEED DE LA PORTADA CUENTA LA HISTORIA DEL ARBOL, NO SU TELEMETRIA.
#
# ``DBEvent`` es una sola tabla para dos publicos.  Uno es el visitante, que
# viene a leer que le ha pasado al arbol: una posicion cerro, una campana se
# resolvio, un cierre se cayo, alguien sembro un FEN nuevo.  El otro somos
# nosotros, y lo que registramos para nosotros son guardas que saltaron, cupos
# que se llenaron, brazos que eligieron una rama, submissions rechazadas y
# medidas de coste.  Nada de eso cambia lo que la base AFIRMA sobre ninguna
# posicion, y ``_friendly_events`` no tiene frase para ello: cae a ``e.kind``
# y lo pinta crudo, en mayusculas y con guiones bajos.
#
# El criterio, entonces: se queda lo que cambia el VEREDICTO PUBLICO sobre una
# posicion (status, closure, grado de prueba) o lo que es un acto humano sobre
# el arbol (sembrar una posicion nueva).  Se va todo lo demas.
#
# Caso por caso, y por que cada uno cae del lado que cae:
#
#   * ``SOLVE_GATE_DISAGREE`` — el cuadrante donde motor y estimador no se
#     ponen de acuerdo.  Existe para alimentar un dataset offline; su payload
#     son cuatro numeros de instrumentacion (annoyance, factor) que no
#     significan nada fuera de esa regresion.  Se emite POR CLAVE, ademas, asi
#     que ademas de ruido cada fila arrastraba un ``_line_labels``.
#   * ``BREADTH_SWAP``, ``UNCERTAINTY_EXPAND``, ``WITNESS_REFUTED``,
#     ``DN_REPAIR`` — decisiones internas de los brazos: por donde gasto el
#     explorador su siguiente click o su siguiente ciclo.  Encolan trabajo; no
#     concluyen nada.
#   * ``CASCADE_GUARD``, ``BACKED_GUARD``, ``REVOKE_GUARD``, ``PROOF_GUARD`` —
#     topes de recorrido que saltaron.  Son literalmente "esta pasada se paro
#     antes de tiempo": una alarma nuestra, no una noticia.
#   * ``DEBT_ENQUEUED``, ``COVERAGE_ENQUEUED``, ``FRAGILE_ENQUEUED``,
#     ``QUALITY_CONVERGENCE`` — contabilidad de cola (cuantas tareas se
#     mintearon, con que cupo).  Ni siquiera llevan ``key``.
#   * ``SOLVE_REJECTED``, ``TB_REJECTED``, ``SOLVE_DISPUTE_SIGNAL`` — auditoria
#     de lo que llego y no se acepto, con nombre de maquina dentro.  Que una
#     submission no colase no es un hecho sobre la posicion.
#   * ``SOLVE_VERIFIED``, ``SURVIVE_VERIFIED`` — la MEDIDA del coste de
#     verificar (segundos, nodos, bytes del certificado).  Cuando ademas
#     cierran algo, ese cierre ya sale por ``NODE_CLOSED``, asi que dejarlos
#     era contar la misma noticia dos veces.
#   * ``BULK_REQUEST`` — el recibo por IP del boton masivo.  Su propio
#     comentario lo dice: existe como auditoria para poder reintroducir un
#     limite informado.  Un recibo no es una historia, y lleva la IP del
#     visitante.
#
# Lo que SE QUEDA, por si alguien anade un kind manana: ``NODE_CLOSED``,
# ``CAMPAIGN_CLOSED``, ``CLOSURE_REVOKED``, ``MATE_PROOF_DISPUTED``,
# ``MATE_WITNESS_CERTIFIED``, ``PROOF_UPGRADED`` y ``SEEDED``.
#
# Es una LISTA NEGRA a proposito, y tiene su precio: un kind nuevo entra al
# feed por defecto.  La alternativa — lista blanca — falla al reves y peor,
# escondiendo en silencio una noticia de verdad el dia que se anada una.  Un
# kind instrumental nuevo se ve en la portada a la primera y se anade aqui; una
# noticia que nunca aparece no la echa de menos nadie.
FEED_HIDDEN_KINDS = {
    'BACKED_GUARD',
    'BREADTH_SWAP',
    'BULK_REQUEST',
    'CASCADE_GUARD',
    'COVERAGE_ENQUEUED',
    'DEBT_ENQUEUED',
    'DN_REPAIR',
    'FRAGILE_ENQUEUED',
    'PROOF_GUARD',
    'QUALITY_CONVERGENCE',
    'REVOKE_GUARD',
    'SOLVE_DISPUTE_SIGNAL',
    'SOLVE_GATE_DISAGREE',
    'SOLVE_REJECTED',
    'SOLVE_VERIFIED',
    'SURVIVE_VERIFIED',
    'TB_REJECTED',
    'UNCERTAINTY_EXPAND',
    'WITNESS_REFUTED',
}


def _friendly_events(events, labels=None):
    out = []
    for e in events:
        pl = e.payload or {}
        key = pl.get('key', '')
        if key:
            san, full = (labels or {}).get(key) or _line_labels(key)
        else:
            san, full = '', ''
        if e.kind == 'NODE_CLOSED':
            txt = f"Solved: {pl.get('status', '?')} via {pl.get('closure', '?')}"
        elif e.kind == 'CAMPAIGN_CLOSED':
            txt = f"Campaign {pl.get('campaign', '?')} SOLVED: {pl.get('status', '?')}"
            key = ''
        else:
            txt = e.kind
        out.append({'ts': e.ts, 'text': txt, 'key': key, 'san': san,
                    'full': full})
    return out


def _human(n):
    """3322 -> '3,322'; 108600000 -> '108.6M'; 2400000000 -> '2.40B'."""
    n = n or 0
    if n >= 1_000_000_000_000:
        return f'{n / 1e12:.2f}T'
    if n >= 1_000_000_000:
        return f'{n / 1e9:.2f}B'
    if n >= 1_000_000:
        return f'{n / 1e6:.1f}M'
    return f'{n:,}'


def _human_seconds(value):
    """4200 -> '1.2h'; 90 -> '1.5m'; 0 -> '0s'. Sin decimales que no informan."""
    value = max(0, int(value or 0))
    if value >= 86_400:
        return f'{value / 86_400:.1f}d'
    if value >= 3_600:
        return f'{value / 3_600:.1f}h'
    if value >= 60:
        return f'{value / 60:.1f}m'
    return f'{value}s'


def _attribution_rows(window):
    """Las cinco procedencias de una ventana, ya con su porcentaje.

    El denominador es lo ETIQUETADO, no todos los cierres: un porcentaje sobre
    un total que incluye eventos anteriores al despliegue de la etiqueta
    sumaria siempre menos de cien y nadie sabria por que.  Lo que falta se
    dice aparte, en claro.
    """
    stamped = window['stamped']
    rows = []
    for name, count in window['sources'].items():
        rows.append({'source': name, 'count': count,
                     'pct': (round(100.0 * count / stamped, 1)
                             if stamped else 0.0)})
    return rows


def _proof_health(now):
    """Los KPIs del paquete adversarial para la portada.

    Las dos cifras del frente y la latencia humana salen de la ULTIMA captura
    horaria, no de una consulta en vivo: la mediana de ``dn`` recorre el
    frente entero y la latencia cruza eventos con peticiones, y ninguna de las
    dos tiene sitio dentro del render de una portada publica.  Una hora de
    retraso en un indicador de tendencia no cambia ninguna decision; un
    segundo extra por visita si.

    Los cierres por procedencia SI van en vivo: son ``COUNT`` sobre un indice
    de fecha y responden a "que esta haciendo el servidor ahora mismo", que es
    justo la pregunta que una captura horaria contestaria tarde.
    """
    snapshot = ProgressSnapshot.objects.order_by('-bucket_start').first()
    day = metrics.closure_attribution_window(now=now, hours=24)
    week = metrics.closure_attribution_window(now=now, hours=24 * 7)
    health = {
        'attribution_24h': _attribution_rows(day),
        'attribution_7d': _attribution_rows(week),
        'attribution_stamped_24h': day['stamped'],
        'attribution_total_24h': day['total'],
        'attribution_stamped_7d': week['stamped'],
        'attribution_total_7d': week['total'],
        'has_attribution': bool(week['stamped']),
        'has_frontier': False,
    }
    if snapshot is None:
        return health
    health.update({
        'has_frontier': bool(snapshot.frontier_and_nodes),
        'frontier_and_nodes_h': _human(snapshot.frontier_and_nodes),
        'frontier_dn_median_h': proof.format_number(
            snapshot.frontier_dn_median),
        'frontier_dn_thin_h': _human(snapshot.frontier_dn_thin),
        'frontier_dn_thin_pct': (
            round(100.0 * snapshot.frontier_dn_thin
                  / snapshot.frontier_and_nodes, 1)
            if snapshot.frontier_and_nodes else 0.0),
        'dn_repair_floor': ingest.DN_REPAIR_FLOOR,
        'human_close_h': _human_seconds(snapshot.human_close_median_seconds),
        'human_close_samples': snapshot.human_close_samples,
        'has_human_close': bool(snapshot.human_close_samples),
        'proof_health_at': snapshot.bucket_start,
    })
    return health


def _own_search(point, nodes):
    """Que dijo el motor MIRANDO ESA POSICION, para las filas [BACKED].

    Una fila respaldada pinta el negamax del subarbol, asi que la eval propia
    del hijo desaparece de la tabla — y con ella la unica forma que tiene un
    visitante de ver de donde sale el numero.  ``point`` ya viene en la
    perspectiva del que mueve, igual que la celda; el soporte es lo que hace
    honesta la frase, porque "+388" con 2.000 nodos detras y "+388" con 128M
    no son la misma afirmacion.  Sin eval propia se dice, sin adornos.
    """
    if point is None:
        return 'no direct search yet'
    if not nodes:
        return f'own search: {point:+d}'
    return f'own search: {point:+d} @ {_human(nodes)} nodes'


def _move_css(status, score, win_status):
    """Color RELATIVO AL QUE MUEVE: su victoria en verde, su derrota en rojo."""
    if status == 'DRAW':
        return 'draw'
    if status != 'UNKNOWN':
        return 'won' if status == win_status else 'lost'
    e = abs(score or 0)
    return 'hot' if e >= 500 else ('warm' if e >= 200 else 'cold')


def _proof_numbers_for(keys):
    """{key: (pn, dn)} de la campana activa, en UNA consulta.

    Una fila sin ``ProofNode`` sale AUSENTE del diccionario, no con (1, 1):
    ese 1/1 es la inicializacion de PNS para "no se nada", y pintarlo seria
    afirmar que se midio algo que no se ha medido.
    """
    keys = [key for key in keys if key]
    if not keys:
        return {}
    campaign = ProofCampaign.objects.filter(active=True).order_by('id').first()
    if campaign is None:
        return {}
    return {row[0]: (row[1], row[2]) for row in ProofNode.objects.filter(
        campaign=campaign, position_id__in=keys,
    ).values_list('position_id', 'pn', 'dn')}


def _child_moves(pos):
    """Tabla de hijos en perspectiva DEL QUE MUEVE (convencion chessdb.cn).
    El almacenamiento interno sigue siendo White-POV; solo la vista voltea —
    exactamente UN cambio de signo por ply mostrado.
    Cada fila muestra el MEJOR CONOCIMIENTO ACTUAL del hijo (status probado >
    backed > eval puntual); ``point`` conserva la eval puntual cruda para que
    el respaldo nunca borre lo que el motor dijo de verdad.
    Orden: victorias del que mueve, luego por score, derrotas al final."""
    stm_white = pos.fen.split()[1] == 'w'
    win = 'WHITE_WIN' if stm_white else 'BLACK_WIN'
    loss = 'BLACK_WIN' if stm_white else 'WHITE_WIN'
    moves = []
    edges = list(Edge.objects.filter(parent=pos).select_related('child'))
    numbers = _proof_numbers_for([edge.child.key for edge in edges])
    for e in edges:
        c = e.child
        mate = None
        point = None if c.eval_cp is None else (
            c.eval_cp if stm_white else -c.eval_cp)
        backed_plies = 0
        if c.status == win:
            score, rank = 10_000, 10_001
        elif c.status == loss:
            score, rank = -10_000, -10_001
        elif c.status == 'DRAW':
            score, rank = 0, 0
        elif c.backed_eval is not None or c.eval_cp is not None:
            known = ingest.best_known_eval(c)
            score = known if stm_white else -known
            rank = score
            if c.backed_eval is not None and c.backed_eval != c.eval_cp:
                backed_plies = c.backed_plies
        else:
            score, rank = None, -9_999.5   # sin analizar: encima de perder
        if c.status in (win, loss):
            # distancia de mate: mate_in propagado por minimax; fallback a la
            # linea verificada. La jugada de la fila cuenta como primer ply.
            plies = None
            if c.mate_in is not None:
                plies = 1 + c.mate_in
            elif c.closure == 'TERMINAL':
                plies = 1
            elif c.closure == 'MATE_PV' and c.won_line:
                plies = 1 + len(c.won_line.split())
            if plies is not None:
                n = (plies + 1) // 2
                mate = n if c.status == win else -n
                # mates cortos primero al ganar; resistencia larga primero al perder
                rank += ((999 - min(n, 999)) if c.status == win
                         else min(n, 999)) * 1e-3
        pn, dn = numbers.get(c.key, (None, None))
        moves.append({'uci': e.move_uci, 'key': c.key, 'status': c.status,
                      # MISMO predicado que cuenta el boton de la cabecera
                      # (§ ingest.is_unexplored): la fila y el boton tienen
                      # que hablar de la misma poblacion o el numero miente.
                      'unexplored': ingest.is_unexplored(c),
                      'pn_h': None if pn is None else proof.format_number(pn),
                      'dn_h': None if dn is None else proof.format_number(dn),
                      'closure': c.closure, 'score': score, 'rank': rank,
                      'mate': mate, 'point': point,
                      'backed_plies': backed_plies,
                      'backed': bool(backed_plies),
                      # Territorio VIRGEN de motor: ni el respaldo trae peso
                      # de busqueda ni el nodo tiene busqueda propia — el
                      # valor solo puede venir de una linea caminada, y el
                      # chip lo dice.  Con busqueda propia (aunque el
                      # respaldo pese 0) el sitio no es virgen y el chip
                      # normal con su tooltip de own search sigue mandando.
                      'backed_light': (bool(backed_plies)
                                       and not (c.backed_nodes or 0)
                                       and not (c.nodes_invested or 0)),
                      'own_search': _own_search(point, c.nodes_invested),
                      'mate_str': None if mate is None else
                      (f'≤M{mate}' if mate > 0 else f'-≤M{-mate}'),
                      'visits': c.visits, 'css': _move_css(c.status, score, win)})
    moves.sort(key=lambda m: -m['rank'])
    return moves


def home(request):
    total = Position.objects.count()
    closed = Position.objects.exclude(status='UNKNOWN').count()
    analyses = AnalysisTask.objects.filter(state='COMPLETED').count()
    requested = AnalysisTask.objects.filter(
        state='PENDING', source='USER', position__status='UNKNOWN').count()
    nodes = Position.objects.aggregate(n=Sum('nodes_invested'))['n'] or 0
    day_ago = timezone.now() - timedelta(hours=24)
    closed_24h = DBEvent.objects.filter(kind='NODE_CLOSED',
                                        ts__gte=day_ago).count()
    nodes_24h = AnalysisTask.objects.filter(
        state='COMPLETED', completed__gte=day_ago).aggregate(
        n=Sum('nodes_searched'))['n'] or 0
    root_key = logic.key_of(logic.start_fen())
    try:
        first_moves = _child_moves(Position.objects.get(key=root_key))
    except Position.DoesNotExist:
        first_moves = []
    solved_first = sum(1 for m in first_moves if m['status'] != 'UNKNOWN')
    solved_pct = round(100.0 * closed / total, 1) if total else 0.0
    now = timezone.now()
    live_cutoff = now - timedelta(seconds=180)
    live_pings = list(WorkerPing.objects.filter(last_seen__gte=live_cutoff)
                      .order_by('machine'))
    lease_cutoff = now - timedelta(minutes=LEASE_MINUTES)
    legacy_display_cutoff = now - timedelta(minutes=LEGACY_DISPLAY_MINUTES)
    candidate_leases = list(AnalysisTask.objects.filter(
        state='LEASED',
    ).filter(
        Q(lease_token='', leased_at__gte=legacy_display_cutoff)
        | Q(lease_token__gt='', lease_heartbeat_at__gte=lease_cutoff)
        | Q(lease_token__gt='', lease_heartbeat_at__isnull=True,
            leased_at__gte=lease_cutoff)
    ).select_related('position').order_by('machine', 'leased_at', 'id'))
    legacy_leases_by_machine = {}
    leases_by_id = {task.id: task for task in candidate_leases}
    for task in candidate_leases:
        if not task.lease_token:
            legacy_leases_by_machine.setdefault(task.machine, task)
    # Prefer the exact task from a fresh worker. Still-valid leases remain a
    # compatibility fallback for the previous worker build, which only touched
    # WorkerPing at lease/submit boundaries.
    leased = []
    selected_ids = set()
    selected_machines = set()
    for ping in live_pings:
        task = leases_by_id.get(ping.current_task_id)
        if task is None:
            # Old workers did not publish an exact task heartbeat. Modern
            # workers deliberately clear current_task_id when engine nodes
            # stop advancing, so falling back for tokenized leases would call
            # a hung engine "Now analyzing" for up to another hour.
            task = legacy_leases_by_machine.get(ping.machine)
        if task is not None:
            leased.append(task)
            selected_ids.add(task.id)
            selected_machines.add(task.machine)
    for task in candidate_leases:
        if task.lease_token:
            continue
        if task.id in selected_ids or task.machine in selected_machines:
            continue
        leased.append(task)
        selected_machines.add(task.machine)
    leased = leased[:5]
    leased_keys = {t.position_id for t in leased}
    upnext_positions = []
    for pos in Position.objects.filter(status='UNKNOWN',
                                       priority__gt=-1e8) \
                               .order_by('-priority')[:12]:
        if pos.key in leased_keys:
            continue
        upnext_positions.append(pos)
        if len(upnext_positions) >= 5:
            break
    # El filtro va en la CONSULTA, no en el render: descartar despues de
    # cortar dejaria una portada con siete noticias las noches en que los
    # brazos internos hablan mucho, y ademas ya habriamos pagado el
    # ``_line_labels`` de las que se tiran (§ FEED_HIDDEN_KINDS).
    event_rows = list(DBEvent.objects.exclude(kind__in=FEED_HIDDEN_KINDS)
                      .order_by('-ts')[:12])
    event_keys = [(event.payload or {}).get('key', '') for event in event_rows]
    labels = _line_labels_many(
        [task.position_id for task in leased]
        + [pos.key for pos in upnext_positions] + event_keys)
    analyzing = []
    for task in leased:
        preview, full = labels.get(task.position_id, ('', ''))
        routed = (_route_labels(task.route, task.position_id)
                  if task.route else None)
        if routed is not None:
            preview, full = routed
        analyzing.append({'key': task.position_id,
                          'san': preview or 'start position',
                          'full': full or 'start position',
                          'budget': _human(task.budget_nodes),
                          'machine': task.machine,
                          'source': task.source,
                          'play': task.route if routed is not None else ''})
    pending_meta = {}
    for pid, source, route in (AnalysisTask.objects.filter(
            position_id__in=[pos.key for pos in upnext_positions],
            state=AnalysisTask.TState.PENDING)
            .order_by('position_id', 'generation')
            .values_list('position_id', 'source', 'route')):
        pending_meta.setdefault(pid, (source, route))
    upnext = []
    for pos in upnext_positions:
        preview, full = labels.get(pos.key, ('', ''))
        source, route = pending_meta.get(pos.key, ('', ''))
        routed = _route_labels(route, pos.key) if route else None
        if routed is not None:
            preview, full = routed
        upnext.append({'key': pos.key,
                       'san': preview or 'start position',
                       'full': full or 'start position',
                       'source': source,
                       'play': route if routed is not None else ''})
    events = _friendly_events(event_rows, labels)
    root = ingest.get_or_create_position(logic.start_fen())
    root_legal_ucis = logic.legal_moves(root.fen)
    compute = worker_metrics()
    root_pn, root_dn = proof.headline_numbers()
    return render(request, 'atomicdb/home.html', {
        **_suggestions_badge(request),
        **_proof_health(now),
        **_campaign_context(request),
        'root_pn_h': proof.format_number(root_pn),
        'root_dn_h': proof.format_number(root_dn),
        'has_proof_numbers': root_pn is not None,
        'analyzing': analyzing, 'upnext': upnext,
        'total_h': _human(total), 'closed_h': _human(closed),
        'analyses_h': _human(analyses), 'nodes_h': _human(nodes),
        'requested_h': _human(requested),
        'workers_h': _human(compute['workers']),
        'cores_h': _human(compute['cores']),
        'compute_nps_h': _human(compute['nps']),
        'positions_pm_h': (f"{compute['positions_per_minute']:.1f}"
                           if compute['positions_per_minute'] < 100
                           else _human(round(compute['positions_per_minute']))),
        'solved_first': solved_first, 'n_first': len(first_moves),
        'solved_pct': solved_pct,
        'closed_24h_h': _human(closed_24h), 'nodes_24h_h': _human(nodes_24h),
        'first_moves': first_moves, 'events': events,
        'root_key': root_key, 'board': _ctx_board(root.fen),
        'board_fen': root.fen,
        'board_turn': 'white' if root.fen.split()[1] == 'w' else 'black',
        'board_key': root.key, 'best_move': root.best_move, 'board_play': '',
        'legal_ucis': root_legal_ucis,
        'legal_move_links': [
            {'uci': uci, 'url': _goto_url(root.key, uci, [])}
            for uci in root_legal_ucis
        ]})


# Cuatro, cinco o seis campos.  Un FEN pegado de un GUI o de un chat llega a
# menudo con el contador de 50 y sin el numero de jugada, y rechazarlo por eso
# es rechazar al usuario por un detalle que ademas da igual: la identidad
# canonica pone los dos contadores a cero de todas formas.
FEN_SHAPE = re.compile(
    r'^[pnbrqkPNBRQK1-8]+(/[pnbrqkPNBRQK1-8]+){7} [wb]'
    r' (-|[KQkqA-Ha-h]+) (-|[a-h][36])( \d+)?( \d+)?$')


class PublicFenError(ValueError):
    """A public FEN that cannot be turned into a position, and why.

    The reason matters: "this is not a legal atomic position" and "we do not
    have this position yet" are different answers, and answering the first
    with the second is how a visitor concludes the site is broken.
    """

    def __init__(self, reason, detail=''):
        super().__init__(detail or reason)
        self.reason = reason


def _parse_public_fen(raw):
    """Validacion estricta de FEN publica ANTES de pasarla a pyffish."""
    raw = ' '.join(raw.replace('_', ' ').split())
    if not raw:
        raise PublicFenError('empty', 'no FEN given')
    if len(raw) > 100:
        raise PublicFenError('malformed', 'FEN is too long')
    if not FEN_SHAPE.match(raw):
        raise PublicFenError(
            'malformed',
            'expected "board side castling en-passant [halfmove] [fullmove]"')
    board = raw.split()[0]
    if board.count('K') != 1 or board.count('k') != 1:
        raise PublicFenError('kings', 'an atomic position needs exactly one '
                                      'king per side')
    for rank in board.split('/'):
        if sum(int(c) if c.isdigit() else 1 for c in rank) != 8:
            raise PublicFenError('ranks', 'every rank must describe 8 squares')
    fields = raw.split()
    # The canonical identity zeroes both counters anyway, so a missing one is
    # not information we lose: it is information that never mattered.
    while len(fields) < 6:
        fields.append('0' if len(fields) == 4 else '1')
    try:
        fen = logic.canonical_fen(' '.join(fields))
        logic.legal_moves(fen)   # pyffish la acepta
    except PublicFenError:
        raise
    except Exception as error:
        raise PublicFenError('illegal',
                             'the atomic rules reject this position')             from error
    return fen


# El cajetin dice FEN, pero el portapapeles del visitante trae lo que trae:
# la gente pega lineas PGN enteras y "FEN is too long" es responderle al
# formato en vez de a la persona.  Un PGN se reconoce, se camina desde la
# posicion inicial y aterriza en la posicion final de la linea.
PGN_MAX_CHARS = 20_000
PGN_MAX_PLIES = 300
_PGN_RESULTS = {'1-0', '0-1', '1/2-1/2', '*'}
_MOVETEXT_HINT = re.compile(
    r'^\[|(^|\s)\d+\.|(^|\s)[O0]-[O0]'
    r'|^[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](=[QRBN])?[+#]?(\s|$)')


def _looks_like_movetext(raw):
    return bool(_MOVETEXT_HINT.search(raw.strip()))


def _pgn_tokens(raw):
    """SAN de la linea principal: fuera cabeceras, comentarios, variantes,
    NAGs, numeros de jugada y resultado."""
    text = re.sub(r'\[[^\]]*\]', ' ', raw)
    text = re.sub(r'\{[^}]*\}', ' ', text)
    text = re.sub(r'\$\d+', ' ', text)
    depth, keep = 0, []
    for ch in text:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth = max(0, depth - 1)
        elif depth == 0:
            keep.append(ch)
    tokens = []
    for item in ''.join(keep).split():
        if item in _PGN_RESULTS:
            continue
        item = re.sub(r'^\d+\.{0,3}', '', item)     # "12.", "12...", "12.Nf3"
        item = item.rstrip('!?+#')
        if item:
            tokens.append(item)
    return tokens


def _uci_for_san(fen, token):
    """La UCI legal cuyo SAN es ``token``, o None.

    La heuristica propia (tablero parseado del FEN) deja 1-3 candidatas y
    solo entonces se le pregunta a pyffish, porque ``get_san`` reconstruye la
    variante en cada llamada y un barrido ciego por ply seria un segundo por
    jugada en el peor caso.
    """
    import pyffish as pf

    legal = logic.legal_moves(fen)
    board, stm = fen.split()[0], fen.split()[1]
    piece_at = {}
    for rank_index, rank in enumerate(board.split('/')):
        file_index = 0
        for ch in rank:
            if ch.isdigit():
                file_index += int(ch)
            else:
                piece_at[chr(ord('a') + file_index) + str(8 - rank_index)] = ch
                file_index += 1

    bare = token.rstrip('+#')
    castle = bare.replace('0', 'O')
    if castle in ('O-O', 'O-O-O'):
        king = 'K' if stm == 'w' else 'k'
        wings = ('g', 'h') if castle == 'O-O' else ('c', 'a')
        # FSF emite el enroque como rey-a-torre o rey-dos-columnas segun la
        # convencion; se aceptan las dos y decide el SAN generado.
        candidates = [uci for uci in legal
                      if piece_at.get(uci[:2]) == king and uci[0] == 'e'
                      and uci[2] in wings]
        bare = castle
    else:
        match = re.match(
            r'^([KQRBN]?)([a-h]?)([1-8]?)x?([a-h][1-8])(=?([QRBN]))?$', bare)
        if match is None:
            return None
        piece, from_file, from_rank, target, _raw_promo, promo = match.groups()
        candidates = []
        for uci in legal:
            src, dst = uci[:2], uci[2:4]
            if dst != target:
                continue
            if piece_at.get(src, '?').upper() != (piece or 'P'):
                continue
            if from_file and src[0] != from_file:
                continue
            if from_rank and src[1] != from_rank:
                continue
            if promo:
                if len(uci) < 5 or uci[4].upper() != promo:
                    continue
            elif len(uci) >= 5:
                continue
            candidates.append(uci)
    if len(candidates) == 1:
        return candidates[0]

    def san_matches(uci):
        try:
            produced = pf.get_san('atomic', fen, uci).rstrip('+#')
        except Exception:
            return False
        return produced == bare or produced.replace('=', '') == \
            bare.replace('=', '')

    matches = [uci for uci in (candidates or legal) if san_matches(uci)]
    return matches[0] if len(matches) == 1 else None


def _parse_public_pgn(raw):
    if len(raw) > PGN_MAX_CHARS:
        raise PublicFenError('malformed', 'PGN is too long')
    tokens = _pgn_tokens(raw)
    if not tokens:
        raise PublicFenError('empty', 'no moves found in the PGN')
    if len(tokens) > PGN_MAX_PLIES:
        raise PublicFenError('malformed',
                             f'PGN longer than {PGN_MAX_PLIES} plies')
    fen = logic.start_fen()
    for ply, token in enumerate(tokens):
        uci = _uci_for_san(fen, token)
        if uci is None:
            side = 'White' if ply % 2 == 0 else 'Black'
            raise PublicFenError(
                'pgn-move',
                f'move {ply // 2 + 1} for {side} ("{token}") is not a legal '
                'atomic move at that point of the line')
        fen = logic.apply_move(fen, uci)
    return logic.canonical_fen(fen)


def _parse_public_position(raw):
    """FEN o PGN: lo que el visitante tenga en el portapapeles."""
    try:
        return _parse_public_fen(raw)
    except PublicFenError as fen_error:
        if not _looks_like_movetext(raw):
            raise fen_error
    return _parse_public_pgn(raw)


def api_query(request):
    """API publica de consulta por FEN. Scores en perspectiva del que mueve
    (convencion chessdb.cn); el arbol interno almacena White-POV."""
    try:
        fen = _parse_public_fen(request.GET.get('fen', ''))
    except Exception:
        return JsonResponse({'error': 'invalid fen'}, status=400)
    try:
        pos = Position.objects.get(key=logic.key_of(fen))
    except Position.DoesNotExist:
        return JsonResponse({'error': 'unknown position'}, status=404)
    stm_white = pos.fen.split()[1] == 'w'
    # ``score`` es el mejor conocimiento actual (respaldado por el subarbol);
    # ``point`` conserva la eval puntual cruda de esta misma posicion.
    known = ingest.best_known_eval(pos) if pos.status == 'UNKNOWN' \
        else pos.eval_cp
    score = None if known is None else (known if stm_white else -known)
    point = None if pos.eval_cp is None else (
        pos.eval_cp if stm_white else -pos.eval_cp)
    moves = [{'uci': m['uci'], 'status': m['status'], 'closure': m['closure'],
              'score': m['score'], 'point': m['point'], 'mate': m['mate'],
              'backed_plies': m['backed_plies'], 'visits': m['visits']}
             for m in _child_moves(pos)]
    return JsonResponse({
        'fen': pos.fen, 'key': pos.key, 'status': pos.status,
        'closure': pos.closure, 'score': score, 'point': point,
        'backed_plies': pos.backed_plies, 'best_move': pos.best_move,
        'tier': 'PRACTICAL', 'trust': _trust_for(pos),
        'history_scope': 'COUNTERS_AND_REPETITION_IGNORED',
        'visits': pos.visits, 'nodes': pos.nodes_invested, 'moves': moves})


def api_frontier(request, key):
    """What a click bought BELOW this position, for the explorer's poller.

    Once the ladder is spent the work no longer lands on the position the
    visitor clicked, so watching that row alone says nothing for six minutes.
    This reports the level underneath instead: visitor tasks running, waiting
    and finished on the direct children, plus how many of those children the
    tree has already closed.

    Deliberately two statements and no more.  The level is folded into ONE
    aggregate because a position can have sixty children and this is polled
    every ten seconds; a query per child would make the page cost grow with
    the width of the tree.
    """
    status = (Position.objects.filter(key=key)
              .values_list('status', flat=True).first())
    if status is None:
        return JsonResponse({'error': 'unknown position'}, status=404)
    requested = Q(child__analysistask__source=AnalysisTask.Source.USER)

    def tasks_in(state):
        return Count('child__analysistask',
                     filter=requested & Q(child__analysistask__state=state))

    level = Edge.objects.filter(parent_id=key).aggregate(
        children_total=Count('child', distinct=True),
        children_solved=Count('child', distinct=True,
                              filter=~Q(child__status='UNKNOWN')),
        running=tasks_in(AnalysisTask.TState.LEASED),
        queued=tasks_in(AnalysisTask.TState.PENDING),
        done=tasks_in(AnalysisTask.TState.COMPLETED))
    return JsonResponse({'key': key, 'status': status, **level})


def _trust_for(pos):
    # SOLVE joins TERMINAL and TB: a certificate that the server replayed in
    # full, with its own move generator, is verified evidence in the same
    # sense they are — not a witness someone else vouched for.
    if pos.closure in ('TERMINAL', 'TB', 'SOLVE'):
        return 'VERIFIED'
    if pos.proof:
        return pos.proof
    if pos.closure in ('MATE_PV', 'MINIMAX'):
        return 'UNCLASSIFIED'
    return 'UNPROVEN'


def fen_jump(request):
    """Cajetin de posicion: FEN o PGN; salta a la posicion y si no existe la
    crea como semilla."""
    if request.method != 'POST':
        return redirect('/atomicdb/')
    try:
        fen = _parse_public_position(request.POST.get('fen', ''))
    except PublicFenError as error:
        # "Not a legal atomic position" is not the same answer as "we do not
        # have this position yet", and saying the second when the first is
        # true is how a visitor concludes the explorer is broken.
        return render(request, 'atomicdb/missing.html',
                      {'fen_error': str(error)}, status=400)
    except Exception:
        return render(request, 'atomicdb/missing.html',
                      {'fen_error': 'that FEN could not be read'}, status=400)
    key = logic.key_of(fen)
    if not Position.objects.filter(key=key).exists():
        # Seeding a position the tree does not have yet.  It is a
        # counter-zero instance like every other node and it starts with NO
        # path to the root, so `refresh_priorities` gives it
        # DISCONNECTED_REGRET and the pn descent never reaches it from a
        # campaign root: only the visitor ladder works it, until something
        # expands a parent that reaches it and the ordinary edges connect it.
        ip = _client_ip(request)
        pos = ingest.get_or_create_position(fen)
        RequestLog.objects.create(ip=ip, position=pos)
        DBEvent.objects.create(kind='SEEDED', payload={
            'key': pos.key, 'fen': pos.fen, 'ip': ip})
    return redirect(f'/atomicdb/explore/{key}/')


def _line_to_root(pos, max_plies=64):
    """Single-position wrapper around the shared cycle-safe reconstruction."""
    return _lines_to_root([pos.key], max_plies=max_plies)[pos.key]


def _numbered_line(top, line, route_ucis=None, opening_match=None,
                   route_ply=None):
    """Explorer breadcrumbs with route- or anchor-preserving destinations."""
    from_start = top.fen == logic.start_fen()
    numbered, n = [], 1
    for i, step in enumerate(line):
        if not from_start:
            number = ''
        elif step['white']:
            number = f'{n}.'
        else:
            number = f'{n}...' if i == 0 else ''
            n += 1
        token = {
            'num': number, 'san': step['san'], 'key': step['key'],
        }
        if route_ucis is not None:
            token['url'] = _explore_url(step['key'], route_ucis[:i + 1])
        elif opening_match is not None and route_ply is not None:
            breadcrumb_ply = route_ply - (len(line) - i - 1)
            anchor = _signed_opening_anchor(
                step['key'], opening_match, breadcrumb_ply)
            if anchor is not None:
                token['url'] = _explore_url(
                    step['key'], opening_anchor=anchor)
        numbered.append(token)
    return numbered


def _opening_for_template(match):
    """Add presentation-only URL safety without changing catalog records."""
    if match is None:
        return None
    result = dict(match)
    result['aliases'] = list(match.get('aliases', ()))
    result['sources'] = []
    provenance_labels = {
        'source_line_number': 'Source line',
        'source_row': 'Source row',
        'same_atomix_position': 'ATOMIX matches',
        'same_eao_position': 'EAO match',
    }

    def provenance_value(value):
        if isinstance(value, dict):
            label = value.get('name') or value.get('label') or value.get('id')
            qualifier = value.get('code') or (
                value.get('id') if value.get('id') != label else None)
            if label and qualifier:
                return f'{label} ({qualifier})'
            if label:
                return str(label)
            return ', '.join(
                f'{key}: {provenance_value(item)}'
                for key, item in value.items()
                if item not in (None, '', [], {})
            )
        if isinstance(value, list):
            return '; '.join(provenance_value(item) for item in value)
        if isinstance(value, bool):
            return 'yes' if value else 'no'
        return str(value)

    for raw_source in match.get('sources', ()):
        source = dict(raw_source)
        source['evidence'] = []
        for raw_evidence in raw_source.get('evidence', ()):
            evidence = dict(raw_evidence)
            try:
                parsed = urlsplit(str(evidence.get('url', '')))
                evidence['safe_url'] = (
                    evidence['url']
                    if parsed.scheme in ('http', 'https') and parsed.netloc
                    else ''
                )
            except (TypeError, ValueError):
                evidence['safe_url'] = ''
            source['evidence'].append(evidence)
        source['provenance_rows'] = []
        for field, value in raw_source.get('provenance', {}).items():
            if value is None or value == '' or value == [] or value == {}:
                continue
            source['provenance_rows'].append({
                'label': provenance_labels.get(
                    field, field.replace('_', ' ').title()),
                'value': provenance_value(value),
            })
        result['sources'].append(source)
    return result


def _named_right_here(position_key):
    """El nombre que lleva EXACTAMENTE esta posicion, o ``None``.

    Una sola fuente para las tres preguntas que se hacen lo mismo: que pinta
    la ficha, que etiqueta la jugada del padre, y contra que nombre se propone
    una correccion.  El orden es el de ``community_names``: la capa comunitaria
    solo devuelve algo cuando de verdad manda (una correccion aprobada), asi
    que preguntarle primero no le da precedencia que no tenga.

    Un nombre HEREDADO de un ancestro no cuenta: aqui se responde por esta
    posicion exacta, igual que hacia el catalogo.
    """
    name = community_names.name_for(position_key)
    if name is not None:
        return name
    match = openings.lookup_key(position_key)
    return None if match is None else match['name']


def _exact_child_opening(child_key, current_opening):
    name = _named_right_here(child_key)
    if name is None:
        return None
    if current_opening is not None and name == current_opening['name']:
        return None
    return name


# ---------------- nombres de apertura de la comunidad ----------------

SUGGESTION_MESSAGES = {
    'ok': 'Thanks: your name is queued for review.',
    'duplicate': 'You already have a pending suggestion for this position.',
    'rate-limited': 'Daily suggestion limit reached, try again tomorrow.',
    'invalid': 'A name is 2 to 60 characters of ordinary text.',
    'unchanged': 'That is already the name this position shows.',
}


def _suggestion_return_url(request, pos, outcome):
    """Vuelve a la MISMA pagina de exploracion, ruta incluida.

    La ruta se revalida entera (no se confia en lo que venga en el POST), asi
    que esto no puede convertirse en un redirect abierto ni materializar una
    linea nombrandola.
    """
    ucis, anchor = None, None
    try:
        route = _validated_play_route(request.POST.get('play') or None,
                                      pos.key)
    except PlayRouteError:
        route = None
    if route is not None:
        ucis = route[2]
    else:
        candidate = request.POST.get('opening') or ''
        match, _ply = _validated_opening_anchor(candidate, pos.key)
        if match is not None:
            anchor = candidate
    url = _explore_url(pos.key, ucis, anchor)
    return f"{url}{'&' if '?' in url else '?'}suggested={outcome}"


def suggest_opening_name(request, key):
    """Propuesta publica de nombre, sin cuenta.

    Dos formas por el MISMO formulario y el mismo endpoint: si la posicion no
    tiene nombre se propone uno nuevo; si lo tiene, esto es una correccion de
    ese nombre y la fila lo dice (``kind=EDIT``) y congela cual era
    (``previous_name``).  Ese nombre se lee AQUI, no se acepta del POST: si
    viniera del cliente, la cola de moderacion pintaria un "actual" que
    cualquiera podria escribir.

    Anti-spam: exactamente el mismo, y por eso vale para las dos formas — una
    sola propuesta pendiente por IP y posicion, tope diario por IP, longitud y
    alfabeto validados.  Lo unico que se anade es que una "correccion" que no
    corrige nada no llega a la cola.
    """
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return render(request, 'atomicdb/missing.html', status=404)
    if request.method != 'POST':
        return redirect(_explore_url(pos.key))

    def back(outcome):
        return redirect(_suggestion_return_url(request, pos, outcome))

    name = ' '.join((request.POST.get('name') or '').split())
    comment = ' '.join((request.POST.get('comment') or '').split())
    if (len(name) < SUGGESTION_NAME_MIN_CHARS
            or len(name) > SUGGESTION_NAME_MAX_CHARS
            or not SUGGESTION_NAME_RE.fullmatch(name)
            or len(comment) > SUGGESTION_COMMENT_MAX_CHARS):
        return back('invalid')

    current = _named_right_here(pos.key)
    if current == name:
        return back('unchanged')
    kind = (OpeningNameSuggestion.SKind.NEW if current is None
            else OpeningNameSuggestion.SKind.EDIT)

    ip = _client_ip(request)
    if OpeningNameSuggestion.objects.filter(
            ip=ip, position=pos,
            status=OpeningNameSuggestion.SState.PENDING).exists():
        return back('duplicate')
    day_ago = timezone.now() - timedelta(days=1)
    if OpeningNameSuggestion.objects.filter(
            ip=ip, created__gte=day_ago).count() >= SUGGESTIONS_PER_IP_DAY:
        return back('rate-limited')
    OpeningNameSuggestion.objects.create(
        position=pos, proposed_name=name, comment=comment, ip=ip,
        kind=kind, previous_name=current or '',
        suggested_by=(request.user.username
                      if request.user.is_authenticated else ''))
    return back('ok')


def _suggestions_badge(request):
    """Contexto del enlace de moderacion, SOLO para approvers.

    Se devuelve como diccionario para fusionar en el contexto de una vista
    concreta, no como context processor global.  Hoy ya no es obligatorio —
    desde la cabecera de identidad, ``map`` y ``method`` tambien varian por
    cookie (§ urls) y un render de approver alli ya no se le serviria a otro —
    pero se queda como esta porque el enlace de moderacion NO es de todas las
    paginas: aparece donde la vista lo pasa a proposito, ``home`` y
    ``explore``.  Donde no se pasa, la plantilla no lo pinta.
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated:
        return {}
    from OpenBench.models import Profile
    if not Profile.objects.filter(user=user, approver=True).exists():
        return {}
    return {'suggestions_pending': OpeningNameSuggestion.objects.filter(
        status=OpeningNameSuggestion.SState.PENDING).count()}


def _approver_gate(request):
    """Mismo criterio que /networks/: login, y Profile.approver."""
    # Import local: mantiene a AtomicDB importable sin arrastrar OpenBench.
    from OpenBench.models import Profile
    if not request.user.is_authenticated:
        return redirect('/login/')
    if not Profile.objects.filter(user=request.user, approver=True).exists():
        return redirect('/index/')
    return None


def suggestions(request):
    """Moderacion de nombres propuestos. Solo para approvers."""
    denied = _approver_gate(request)
    if denied is not None:
        return denied

    if request.method == 'POST':
        decisions = {'approve': OpeningNameSuggestion.SState.APPROVED,
                     'reject': OpeningNameSuggestion.SState.REJECTED}
        decision = decisions.get(request.POST.get('action', ''))
        try:
            suggestion_id = int(request.POST.get('suggestion', ''))
        except (TypeError, ValueError):
            suggestion_id = None
        if decision is not None and suggestion_id is not None:
            with atomic():
                row = (OpeningNameSuggestion.objects.select_for_update()
                       .filter(id=suggestion_id,
                               status=OpeningNameSuggestion.SState.PENDING)
                       .first())
                if row is not None:
                    # El catalogo auditado pudo nombrarla mientras esperaba, y
                    # un nombre NUEVO no lo pisa: la aprobacion se convierte en
                    # rechazo, igual que siempre.  Una CORRECCION no cae aqui —
                    # nacio contra un nombre existente y el moderador acaba de
                    # leer cual y cual lo sustituye.
                    if (decision == OpeningNameSuggestion.SState.APPROVED
                            and row.kind != OpeningNameSuggestion.SKind.EDIT
                            and openings.lookup_key(row.position_id)
                            is not None):
                        decision = OpeningNameSuggestion.SState.REJECTED
                    row.status = decision
                    row.resolved_by = request.user.username[:64]
                    row.resolved_at = timezone.now()
                    row.save(update_fields=['status', 'resolved_by',
                                            'resolved_at'])
        community_names.invalidate()
        return redirect('/atomicdb/suggestions/')

    pending = list(OpeningNameSuggestion.objects
                   .filter(status=OpeningNameSuggestion.SState.PENDING)
                   .select_related('position')
                   .order_by('created')[:SUGGESTION_MODERATION_PAGE])
    resolved = list(OpeningNameSuggestion.objects
                    .exclude(status=OpeningNameSuggestion.SState.PENDING)
                    .select_related('position')
                    .order_by('-resolved_at')[:20])
    labels = _line_labels_many([row.position_id for row in pending + resolved])

    def presented(row, live=False):
        preview, full = labels.get(row.position_id, ('', ''))
        item = {'row': row, 'san': preview or 'start position',
                'full': full or 'start position', 'drifted': ''}
        # El "actual" de la fila es el de CUANDO se propuso.  Si el nombre se
        # movio mientras esperaba, el moderador tiene que verlo: aprobar esto
        # sustituye lo que hay HOY, no lo que habia entonces.
        if live and row.kind == OpeningNameSuggestion.SKind.EDIT:
            current = _named_right_here(row.position_id)
            if (current or '') != row.previous_name:
                item['drifted'] = current or '(no name)'
        return item

    rows = [presented(row, live=True) for row in pending]
    history = [presented(row) for row in resolved]
    return render(request, 'atomicdb/suggestions.html', {
        'pending': rows, 'history': history,
        'pending_total': OpeningNameSuggestion.objects.filter(
            status=OpeningNameSuggestion.SState.PENDING).count(),
    })


def notifications_page(request):
    """La lista completa de avisos, y el POST del boton Mark all read.

    LEIDA = VISITADA.  Lo que apaga un aviso es llegar a su posicion (la
    vista de explore lo marca en cada carga con sesion), no abrir el panel ni
    esta pagina: quien mira su lista y no visita nada conserva sus negritas.
    El POST es el unico atajo — el boton explicito de marcar todo — y con
    ``back`` vuelve a la pagina de la que salio el click.
    """
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={quote("/atomicdb/notifications/")}')
    if request.method == 'POST':
        seen = notifications.mark_seen(request.user.username)
        back = request.POST.get('back', '')
        if back:
            return redirect(back if back.startswith('/atomicdb/')
                            else '/atomicdb/notifications/')
        return JsonResponse({'ok': True, 'seen': seen})
    rows = notifications.presented(request.user.username,
                                   limit=notifications.PAGE_ROWS)
    return render(request, 'atomicdb/notifications.html', {'rows': rows})


# ---------------- pagina de contribuidor ----------------

def contributor_me(request):
    """Atajo a tu propia pagina. Sin sesion, al login de OpenBench.

    El enlace de la cabecera apunta AQUI y no a ``/atomicdb/user/<yo>/``: asi
    el chrome de todas las paginas sigue siendo el mismo HTML para cualquier
    visitante con sesion, y una copia cacheada no puede llevar el nombre de
    nadie en el destino del enlace (§ urls, por que map y method varian por
    cookie).  El ``next`` se compone con la ruta literal, no con nada que
    venga del cliente.
    """
    if not request.user.is_authenticated:
        return redirect(f'/login/?next={quote("/atomicdb/me/")}')
    return redirect(
        f'/atomicdb/user/{quote(request.user.username, safe="")}/')


def contributor(request, username):
    """La pagina publica de una cuenta con actividad en AtomicDB.

    404 si no la tiene: una pagina vacia por cada nombre que alguien teclee
    seria afirmar que esa cuenta contribuye.  Lo que decide es actividad
    REAL — workers, peticiones o nombres propuestos — y no la existencia de
    la cuenta en OpenBench, que es otra base y otra pregunta.
    """
    username = (username or '')[:64]
    if not contributors.has_activity(username):
        return render(request, 'atomicdb/missing.html', {
            'missing_note': 'No AtomicDB activity under that name (yet).',
        }, status=404)
    viewer = (request.user.username if request.user.is_authenticated else '')
    context = contributors.present(username)
    context.update({
        **_suggestions_badge(request),
        'is_self': viewer == username,
        # Los titulos hablan de la persona que se esta mirando: "Your queue"
        # cuando eres tu, "alice's queue" cuando no.  La pagina es publica y
        # tutear al visitante sobre la cola de otro seria mentirle.
        'owner_label': 'Your' if viewer == username else f'{username}’s',
    })
    return render(request, 'atomicdb/contributor.html', context)


# ---------------- campanas de exploracion ----------------
#
# QUIEN PUEDE QUE.  Proponer y votar es publico y sin cuenta, igual que el
# buzon de nombres; ACTIVAR es del propietario y de nadie mas.  Esa frontera
# es el producto entero, asi que esta escrita en un solo sitio (``is_staff``
# en ``campaign_state``) y no se deduce en ninguna plantilla: la portada
# esconde los botones por cortesia, el endpoint es quien dice que no.
#
# CSRF.  Mismo patron que el buzon de sugerencias: vistas normales, sin
# ``csrf_exempt``, y el token en el formulario.  Lo que se exime aqui es el
# protocolo de workers, que no tiene navegador detras; esto si lo tiene.

CAMPAIGN_MESSAGES = {
    'ok': 'Thanks: your line is on the board and open to votes.',
    'exists': 'That line was already proposed. Your vote counts on its row.',
    'rate-limited': 'Daily proposal limit reached, try again tomorrow.',
    'unknown-position': 'That position is not in the tree yet.',
    'name-taken': 'That title is already in use. Pick another one.',
    'voted': 'Vote counted.',
    'already': 'You had already voted for that campaign.',
    'activated': 'Campaign activated.',
    'paused': 'Campaign paused.',
    'done': 'Campaign closed.',
}


def _voter_token(request):
    """``(token, recien_creado)`` del visitante que propone o vota.

    Una cookie que no encaje en la forma esperada se DESCARTA y se sustituye
    en vez de rechazarse: lo que llega en un ``Cookie:`` lo escribe cualquiera,
    y el unico papel de este valor es agrupar los actos de una misma persona.
    Tratarlo como un dato de confianza seria darle un poder que no tiene.
    """
    raw = (request.COOKIES.get(CAMPAIGN_VOTER_COOKIE) or '').strip()
    if CAMPAIGN_VOTER_RE.fullmatch(raw):
        return raw, False
    return secrets.token_urlsafe(CAMPAIGN_VOTER_BYTES), True


def _campaign_reply(request, payload, status=200, token=None, minted=False):
    """JSON por defecto; vuelta a la portada si el POST vino de un boton.

    Los tres endpoints tienen contrato JSON porque son utiles fuera del
    navegador, pero los botones de la portada son formularios normales y sin
    JS — y aterrizar en una pagina de JSON crudo despues de votar es una web
    rota.  El campo ``back`` lo pide el formulario a proposito; quien no lo
    manda recibe el JSON tal cual estaba especificado.
    """
    if request.POST.get('back'):
        response = redirect(f"/atomicdb/?campaign={payload['status']}"
                            f"#campaigns")
    else:
        response = JsonResponse(payload, status=status)
    if token is not None and minted:
        response.set_cookie(CAMPAIGN_VOTER_COOKIE, token,
                            max_age=CAMPAIGN_VOTER_COOKIE_SECONDS,
                            samesite='Lax', httponly=True)
    return response


def _campaign_line_san(key):
    """El rotulo de una campana, generado del linaje de su raiz.

    Se autogenera y NO se acepta del cliente: es lo que la portada pinta como
    titulo de la tarjeta, y un titulo que escribe quien propone es un sitio
    donde escribir cualquier cosa.  La raiz manda; el texto es una lectura de
    la raiz.
    """
    preview, _full = _line_labels(key, preview_plies=CAMPAIGN_LINE_PLIES)
    return preview or 'start position'


def _campaign_name_for(pos, line_san):
    """Un nombre unico y legible para una campana recien propuesta.

    ``Campaign.name`` es unico desde el primer dia y no hay ningun sitio donde
    escribirlo a mano, asi que se deriva de la linea.  Chocar solo es posible
    contra una campana ya ``DONE`` sobre la misma raiz — el dedup cubre todo
    lo demas — asi que un sufijo corto basta, y si aun asi choca manda la
    clave, que es unica por construccion.
    """
    base = (line_san or '').strip()[:56].strip() or f'line-{pos.key[:12]}'
    name = base
    for suffix in range(2, 12):
        if not Campaign.objects.filter(name=name).exists():
            return name
        name = f'{base} #{suffix}'[:64]
    return f'line-{pos.key[:16]}-{secrets.token_hex(4)}'[:64]


def _tag_campaign_subtree(campaign, cap=CAMPAIGN_TAG_MAX_NODES,
                          batch=CAMPAIGN_TAG_BATCH):
    """Adopta de una vez el subarbol que YA existia bajo la raiz.

    Sin esto una campana recien activada solo puntuaria sobre lo que naciera a
    partir de ahora (§ ``ingest.expand``), y lo que la comunidad pide explorar
    es justo una linea que normalmente ya tiene medio arbol debajo.

    Dos reglas y las dos importan:

    * Solo se adopta lo que no tiene dueno.  Una posicion que ya pertenece a
      otra campana se queda donde esta: por transposicion los subarboles se
      solapan, y "el ultimo en activar se lo lleva todo" convertiria cada
      activacion en un robo silencioso.
    * Hay tope.  Esto corre dentro de un POST, y un BFS sin techo sobre un
      grafo de millones de nodos no es una operacion interactiva.  Se recorren
      como mucho ``cap`` nodos y se dice cuantos; lo que quede fuera lo
      recogera la herencia segun el arbol crezca.

    Se escribe con ``update`` por lotes y no con ``bulk_update``: todas las
    filas reciben EL MISMO valor, asi que cargar cincuenta mil instancias para
    escribir una columna identica en todas seria trabajo puro.
    """
    seen = {campaign.root_id}
    order = [campaign.root_id]
    frontier = [campaign.root_id]
    while frontier and len(seen) < cap:
        children = []
        for start in range(0, len(frontier), batch):
            children.extend(Edge.objects.filter(
                parent_id__in=frontier[start:start + batch],
            ).values_list('child_id', flat=True))
        nxt = []
        for child in children:
            if child in seen:
                continue
            seen.add(child)
            order.append(child)
            nxt.append(child)
            if len(seen) >= cap:
                break
        frontier = nxt
    tagged = 0
    for start in range(0, len(order), batch):
        tagged += Position.objects.filter(
            key__in=order[start:start + batch], campaign__isnull=True,
        ).update(campaign=campaign)
    return tagged, len(seen)


def _campaign_credit(request):
    """Quien firma la propuesta.

    Con sesion iniciada manda el usuario y el campo libre se IGNORA: si se
    aceptase, cualquiera podria firmar con su cuenta el nombre de otro, y un
    credito que se puede escribir a mano no es un credito.  Sin sesion queda
    el campo libre, que no afirma identidad ninguna y se pinta como lo que es.
    """
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        return user.username[:CAMPAIGN_PROPOSER_MAX_CHARS]
    return ' '.join((request.POST.get('name') or '').split())[
        :CAMPAIGN_PROPOSER_MAX_CHARS]


def campaign_propose(request):
    """Propuesta publica de campana, sin cuenta: ``key=<clave de posicion>``.

    Del visitante se acepta QUE posicion, con que TITULO llamarla y, si es
    anonimo, con que nombre firmarla.  El estado nace ``PROPOSED``: proponer
    no enciende nada, solo pone la linea en la lista.

    TITULO Y LINEA SON COSAS DISTINTAS.  ``line_san`` sale siempre del linaje
    y es la IDENTIDAD de la campana — lo que de verdad se va a explorar — asi
    que no se acepta del cliente ni cuando trae titulo.  El titulo es solo el
    rotulo: si viene, es el nombre; si no, se deriva de la linea.  Como el
    nombre es unico en la tabla, un titulo ya usado se rechaza SIN crear nada,
    para que quien propone elija otro en vez de encontrarse con una campana
    llamada "lo mismo #2".

    Anti-spam en dos capas, ninguna de ellas un captcha: el dedup por raiz
    (proponer una linea ya propuesta devuelve la que hay, para que la gente
    vote en vez de duplicar) y un tope diario por cookie.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    token, minted = _voter_token(request)

    def reply(payload, status=200):
        return _campaign_reply(request, payload, status, token, minted)

    # La clave se recorta al ancho de la columna antes de consultar: no cambia
    # ninguna respuesta (nada mas largo puede existir) y evita mandar a la
    # base una cadena arbitraria del cliente.
    key = (request.POST.get('key') or '').strip()[:64]
    if not Position.objects.filter(key=key).exists():
        return reply({'status': 'unknown-position', 'id': None}, 404)
    title = ' '.join((request.POST.get('title') or '').split())[
        :CAMPAIGN_TITLE_MAX_CHARS]
    proposed_by = _campaign_credit(request)
    # Se guarda SOLO en las propuestas sin cookie: es lo unico que las agrupa,
    # y en las demas seria un dato personal que no hace falta para nada.
    client_ip = _client_ip(request) if minted else ''
    day_ago = timezone.now() - timedelta(days=1)
    with atomic():
        # El bloqueo sobre la RAIZ serializa las propuestas de esa linea: sin
        # el, dos envios simultaneos pasan los dos el dedup y la linea acaba
        # con dos campanas abiertas que nadie sabe cual es.
        pos = Position.objects.select_for_update().get(key=key)
        existing = (Campaign.objects
                    .filter(root=pos, state__in=Campaign.OPEN_STATES)
                    .order_by('id').first())
        if existing is not None:
            return reply({'status': 'exists', 'id': existing.id})
        if Campaign.objects.filter(
                proposed_token=token,
                created__gte=day_ago).count() \
                >= CAMPAIGN_PROPOSALS_PER_VOTER_DAY:
            return reply({'status': 'rate-limited', 'id': None}, 429)
        # Sin cookie previa el token es NUEVO, asi que su contador vale cero
        # por construccion y el tope de arriba no acota nada: bastaba con no
        # devolver la cookie para proponer sin limite, y cada propuesta cuesta
        # un recorrido de linaje entero.  A esas — y solo a esas — se les
        # cuenta por direccion, como a las sugerencias de nombre.  El CGNAT
        # sigue protegido: quien devuelve su cookie no entra aqui jamas.
        if minted and Campaign.objects.filter(
                proposed_ip=client_ip,
                created__gte=day_ago).count() \
                >= CAMPAIGN_PROPOSALS_PER_VOTER_DAY:
            return reply({'status': 'rate-limited', 'id': None}, 429)
        line_san = _campaign_line_san(pos.key)
        if title and Campaign.objects.filter(name=title).exists():
            return reply({'status': 'name-taken', 'id': None}, 409)
        try:
            # ``atomic`` anidado = savepoint: si la unicidad salta por una
            # carrera con otra propuesta, se deshace SOLO esto y la
            # transaccion de fuera sigue viva para poder contestar.
            with atomic():
                campaign = Campaign.objects.create(
                    name=title or _campaign_name_for(pos, line_san),
                    root=pos, line_san=line_san,
                    state=Campaign.CState.PROPOSED,
                    # El espejo deprecado se escribe a mano en el unico sitio
                    # que no pasa por ``apply_state``: una propuesta NO esta
                    # activa, y el default del campo (heredado de cuando el
                    # booleano mandaba) dice que si.
                    active=False,
                    proposed_by=proposed_by,
                    proposed_token=token,
                    proposed_ip=client_ip)
        except IntegrityError:
            return reply({'status': 'name-taken', 'id': None}, 409)
    return reply({'status': 'ok', 'id': campaign.id})


def campaign_vote(request, campaign_id):
    """Un voto por campana y por cookie. Repetirlo no suma.

    El recuento cacheado se reescribe con el ``COUNT`` real en la misma
    transaccion que el voto, nunca con un ``+1``: un incremento pierde una
    carrera y deja la portada mintiendo hasta que alguien lo note.

    Pero un ``COUNT`` sin bloquear la campana pierde EXACTAMENTE la misma
    carrera: dos votantes cuentan a la vez, los dos ven el mismo total y el
    segundo lo reescribe con el numero del primero.  Aqui no hay nada que lo
    repare despues — la desviacion es permanente y alimenta CAMPAIGN_BONUS —
    asi que la campana se bloquea antes de contar y los votos concurrentes
    hacen cola.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    token, minted = _voter_token(request)

    def reply(payload, status=200):
        return _campaign_reply(request, payload, status, token, minted)

    try:
        campaign = Campaign.objects.get(id=campaign_id)
    except Campaign.DoesNotExist:
        return reply({'status': 'unknown-campaign', 'votes': 0}, 404)
    name = ' '.join((request.POST.get('name') or '').split())
    with atomic():
        # El bloqueo va PRIMERO y sobre la campana, que es la fila cuyo
        # recuento se va a reescribir: serializa a los votantes de ESTA
        # campana y a nadie mas.
        locked = (Campaign.objects.select_for_update()
                  .filter(id=campaign.id).first())
        if locked is None:
            return reply({'status': 'unknown-campaign', 'votes': 0}, 404)
        _row, created = CampaignVote.objects.get_or_create(
            campaign=locked, token=token,
            defaults={'name': name[:CAMPAIGN_PROPOSER_MAX_CHARS]})
        votes = _recount_campaign_votes(locked.id)
    return reply({'status': 'voted' if created else 'already', 'votes': votes})


def _recount_campaign_votes(campaign_id):
    """Reescribe la cache de votos con el COUNT que ve LA BASE.

    Contar en Python y guardar despues deja una ventana entre las dos cosas, y
    un voto que commitee justo ahi se pierde para siempre: no hay ninguna
    pasada posterior que recalcule esta columna, y de ella come CAMPAIGN_BONUS.
    Con el COUNT dentro del mismo UPDATE no hay ventana que perder.

    Devuelve lo que quedo escrito, que es lo unico que se le puede contar al
    votante sin volver a mentirle.
    """
    counted = Subquery(
        CampaignVote.objects.filter(campaign=OuterRef('pk'))
        .order_by().values('campaign').annotate(n=Count('pk')).values('n'),
        output_field=IntegerField())
    Campaign.objects.filter(id=campaign_id).update(
        votes=Coalesce(counted, Value(0)))
    return (Campaign.objects.filter(id=campaign_id)
            .values_list('votes', flat=True).first() or 0)


def campaign_state(request, campaign_id):
    """Activar, pausar o cerrar una campana. SOLO el propietario.

    Aqui es donde vive la unica asimetria del diseno: los votos ordenan la
    lista y no deciden nada.  ``is_staff`` es el criterio — el mismo que
    distingue al propietario en el Django de este sitio — y responde 403 a
    todo lo demas, este quien este mirando la portada.

    ``pause`` y ``done`` NO desetiquetan nada.  Las etiquetas son de la
    posicion, no del turno: una campana pausada que se reactiva tiene que
    recuperar su subarbol, y borrar cincuenta mil etiquetas para volver a
    escribirlas manana es trabajo a cambio de nada.  El estado ya basta para
    que el selector deje de puntuarla.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated or not user.is_staff:
        return JsonResponse({'status': 'forbidden'}, status=403)
    try:
        campaign = Campaign.objects.get(id=campaign_id)
    except Campaign.DoesNotExist:
        return JsonResponse({'status': 'unknown-campaign'}, status=404)
    action = (request.POST.get('action') or '').strip().lower()
    if action == 'activate':
        with atomic():
            campaign.apply_state(Campaign.CState.ACTIVE)
            tagged, visited = _tag_campaign_subtree(campaign)
        return _campaign_reply(request, {'status': 'activated',
                                         'id': campaign.id,
                                         'tagged': tagged,
                                         'visited': visited})
    if action in ('pause', 'done'):
        campaign.apply_state(Campaign.CState.PAUSED if action == 'pause'
                             else Campaign.CState.DONE)
        return _campaign_reply(request, {
            'status': 'paused' if action == 'pause' else 'done',
            'id': campaign.id})
    return JsonResponse({'status': 'bad-action'}, status=400)


def _campaign_identity(campaign):
    """Titulo, linea y credito de una campana, tal y como se pintan.

    La linea solo baja a subtitulo cuando DICE algo que el titulo no dice ya:
    sin titulo propio el nombre ES la linea, y repetirla debajo de si misma no
    informa a nadie.  El credito nunca queda en blanco — una propuesta sin
    firma es anonima, y decirlo es mas honesto que dejar un hueco.
    """
    return {
        'id': campaign.id, 'title': campaign.name,
        'line_san': campaign.line_san,
        'line_shown': ('' if campaign.line_san == campaign.name
                       else campaign.line_san),
        'root_key': campaign.root_id, 'votes': campaign.votes,
        'proposed_by': campaign.proposed_by,
        'proposed_by_h': campaign.proposed_by or 'anonymous',
    }


def _campaign_context(request):
    """Lo que la portada pinta de campanas: la activa arriba y el buzon.

    Los conteos de progreso salen de UNA consulta agrupada para todas las
    campanas activas, no de tres por campana.  No llevan cache propia: la
    portada entera ya va con ``cache_page(15)`` (§ urls), y una segunda cache
    dentro de una cacheada solo anadiria una copia mas vieja de los mismos
    numeros.

    Los botones de propietario se calculan aqui y no en la plantilla por la
    misma razon que el enlace de moderacion (§ ``_suggestions_badge``): la
    portada varia por cookie, asi que su render de staff no se le sirve a
    nadie mas.
    """
    active = list(Campaign.objects.filter(state=Campaign.CState.ACTIVE)
                  .order_by('-votes', 'id')[:HOME_ACTIVE_CAMPAIGNS])
    totals = {}
    if active:
        for row in (Position.objects
                    .filter(campaign_id__in=[c.id for c in active])
                    .values('campaign_id')
                    .annotate(total=Count('key'),
                              explored=Count('key',
                                             filter=Q(nodes_invested__gt=0)),
                              resolved=Count('key',
                                             filter=~Q(status='UNKNOWN')))):
            totals[row['campaign_id']] = row
    cards = []
    for campaign in active:
        row = totals.get(campaign.id, {})
        total = row.get('total', 0)
        resolved = row.get('resolved', 0)
        cards.append({
            **_campaign_identity(campaign),
            'total': total, 'total_h': _human(total),
            'explored': row.get('explored', 0),
            'explored_h': _human(row.get('explored', 0)),
            'resolved': resolved, 'resolved_h': _human(resolved),
            'solved_pct': round(100.0 * resolved / total, 1) if total else 0.0,
        })
    proposed = [
        _campaign_identity(campaign)
        for campaign in Campaign.objects
        .filter(state=Campaign.CState.PROPOSED)
        .order_by('-votes', '-created')[:HOME_PROPOSED_CAMPAIGNS]]
    user = getattr(request, 'user', None)
    is_owner = bool(user is not None and user.is_authenticated
                    and user.is_staff)
    outcome = request.GET.get('campaign', '')
    return {'campaign_cards': cards, 'campaigns_proposed': proposed,
            'campaign_owner': is_owner,
            'campaign_message': CAMPAIGN_MESSAGES.get(outcome, '')}


# Por debajo de esto no hay linea que verificar: son la jugada y su respuesta,
# y para eso ya esta el boton de al lado.
PV_VERIFY_MIN_PLIES = 4


def _pv_verify_plies(pos):
    """Plies verificables de la PV vigente, o 0 si el boton no procede.

    Cuenta SIN tocar la base — el mismo ``current_line`` que usa el helper que
    encolara — y no promete legalidad: la PV puede estar rota y el helper corta
    donde toque.  Lo que decide es si hay algo que ofrecer.
    """
    if pos.status != 'UNKNOWN':
        return 0
    line = solve_estimate.current_line(pos)
    pv = (line or {}).get('pv')
    if not isinstance(pv, list) or len(pv) < PV_VERIFY_MIN_PLIES:
        return 0
    return min(len(pv), ingest.PV_VERIFY_MAX_PLIES)


def explore(request, key):
    try:
        pos = Position.objects.get(key=key)
    except Position.DoesNotExist:
        return render(request, 'atomicdb/missing.html', status=404)
    if request.user.is_authenticated:
        # Leida = VISITADA: lo que apaga un aviso es llegar a su posicion —
        # desde el propio aviso o por su pie — no abrir el panel.
        notifications.mark_position_seen(request.user.username, pos.key)
    moves = _child_moves(pos)
    parents = [{'key': e.parent_id, 'uci': e.move_uci}
               for e in Edge.objects.filter(child=pos)[:8]]
    raw_play = request.GET.get('play')
    try:
        explicit_route = _validated_play_route(raw_play, pos.key)
    except PlayRouteError as exc:
        return _play_route_error_response(exc)
    if explicit_route is None:
        top, line, active_ucis = _canonical_route(pos)
    else:
        top, line, active_ucis = explicit_route
    # tambien en posiciones resueltas: se puede explorar la winning line
    legal_ucis = ([] if pos.closure == 'TERMINAL'
                  else logic.legal_moves(pos.fen))
    known = {m['uci'] for m in moves}
    current_opening_match, route_ply, active_ucis = _navigation_opening(
        active_ucis,
        request.GET.get(OPENING_ANCHOR_PARAM),
        pos.key,
        explicit_play=explicit_route is not None,
    )
    current_anchor = (
        None if active_ucis is not None
        else _signed_opening_anchor(
            pos.key, current_opening_match, route_ply)
    )
    current_opening = _opening_for_template(current_opening_match)
    # Un nombre comunitario aprobado solo puede aparecer donde el catalogo
    # auditado calla sobre ESTA posicion, y gana a un nombre heredado de un
    # ancestro. La navegacion (anclas firmadas) sigue anclada al catalogo.
    community = community_names.opening_for(pos.key, pos.fen)
    if community is not None:
        current_opening = _opening_for_template(community)

    for move in moves:
        child_route, child_anchor = _child_navigation_state(
            active_ucis, current_opening_match, route_ply,
            move['key'], move['uci'],
        )
        move['url'] = _explore_url(
            move['key'], child_route, child_anchor)
        # Salto al origen del respaldo de ESTA fila: misma ruta que su enlace
        # de navegacion, ya construida arriba.  Solo se pinta cuando la fila
        # lleva chip; aqui es una cadena, no una consulta — el paseo por la
        # espina se paga al pinchar, nunca al pintar la tabla.
        move['backed_url'] = _backed_source_url(move['key'], child_route)
        move['enters_opening'] = _exact_child_opening(
            move['key'], current_opening)
    # FUERA DEL ARBOL: jugadas legales que no tienen ni arista.  No son "sin
    # explorar" — sin explorar esta una respuesta que EXISTE y a la que nadie
    # ha mirado, y esa se etiqueta en su propia fila (§ ingest.is_unexplored)
    # — sino jugadas que todavia no son un nodo.  Pincharlas las crea.
    offtree = []
    for uci in legal_ucis:
        if uci in known:
            continue
        child_fen = logic.apply_move(pos.fen, uci)
        child_key = logic.key_of(child_fen)
        offtree.append({
            'uci': uci,
            'url': _goto_url(
                pos.key, uci, active_ucis, current_anchor),
            'enters_opening': _exact_child_opening(
                child_key, current_opening),
        })
    numbered = _numbered_line(
        top,
        line,
        active_ucis,
        current_opening_match if current_anchor else None,
        route_ply,
    )
    board_play = (
        ','.join(active_ucis)
        if active_ucis is not None
        else (
            OPENING_ANCHOR_PLAY_PREFIX + current_anchor
            if current_anchor else ''
        )
    )
    legal_move_links = [
        {
            'uci': uci,
            'url': _goto_url(
                pos.key, uci, active_ucis, current_anchor),
        }
        for uci in legal_ucis
    ]
    stm_white = pos.fen.split()[1] == 'w'
    win = 'WHITE_WIN' if stm_white else 'BLACK_WIN'
    # Cabecera: el valor RESPALDADO (lo que el subarbol ya sabe) con caida
    # limpia a la eval puntual. Un solo flip a la perspectiva del que mueve.
    # UNA sola fuente en todo el render: exactamente la misma llamada que usa
    # la fila de este nodo en la pagina de su padre.  Dos aggregaciones — una
    # almacenada y otra recalculada al pintar — es como la cabecera de una
    # pagina acaba discrepando de la fila que la enlaza.
    known = None if pos.status != 'UNKNOWN' else ingest.best_known_eval(pos)
    eval_stm = None if known is None else (known if stm_white else -known)
    point_stm = None if pos.eval_cp is None else (
        pos.eval_cp if stm_white else -pos.eval_cp)
    eval_backed = (known is not None and known != pos.eval_cp
                   and pos.status == 'UNKNOWN')
    return render(request, 'atomicdb/explore.html', {
        **_suggestions_badge(request),
        'pos': pos, 'moves': moves, 'parents': parents,
        'line': numbered, 'line_from_root': top.fen == logic.start_fen(),
        'active_play': active_ucis, 'board_play': board_play,
        'opening': current_opening,
        'board_key': pos.key, 'legal_ucis': legal_ucis,
        'legal_move_links': legal_move_links,
        'board_fen': pos.fen, 'board_turn': 'white' if stm_white else 'black',
        'board': _ctx_board(pos.fen),
        'unexplored_count': (len(ingest.unexplored_children(pos))
                             if pos.status == 'UNKNOWN' else 0),
        # Cuantos plies de la PV VIGENTE se pueden verificar de un click, ya
        # acotados por el tope del helper.  Cero apaga el boton, y por eso se
        # cuenta aqui y no en la plantilla: con menos de cuatro plies no hay
        # linea que contrastar — es la jugada y su respuesta, que es
        # exactamente lo que el boton de al lado ya compra.
        'pv_verify_plies': _pv_verify_plies(pos),
        # La flecha apunta al TOP de la misma lista que pinta la tabla (mejor
        # conocimiento, backed incluido), no al best_move de la ultima
        # busqueda propia: cuando un respaldo adelanta, tabla y flecha deben
        # moverse JUNTAS o el tablero contradice a su propia columna (bug
        # reportado 29-jul; misma leccion de la fuente unica de verdad).
        'best_move': (None if pos.closure == 'TERMINAL'
                      else (moves[0]['uci']
                            if moves and (moves[0].get('score') is not None
                                          or moves[0].get('mate') is not None)
                            else pos.best_move)),
        'stm': 'White' if stm_white else 'Black',
        'eval_stm_str': None if eval_stm is None else f'{eval_stm:+d}cp',
        'eval_backed': eval_backed,
        'eval_backed_plies': pos.backed_plies,
        # Salto al origen del respaldo de la CABECERA, con la ruta que el
        # visitante trae puesta para que la espina la extienda.
        'backed_source_url': _backed_source_url(pos.key, active_ucis),
        # Respaldo SIN peso de busqueda en territorio SIN busqueda propia: el
        # valor subio por una linea que un visitante camino, asi que es una
        # cota sin verificar, no un veredicto del motor.  El chip lo dice.
        'eval_backed_light': (eval_backed and not (pos.backed_nodes or 0)
                              and not (pos.nodes_invested or 0)),
        'eval_point_str': None if point_stm is None else f'{point_stm:+d}cp',
        # Propuesta de nombre: en TODAS las posiciones desde el 28-jul.  Donde
        # no hay nombre se propone uno; donde lo hay, la misma caja propone una
        # correccion de ese nombre, que es lo que aqui se pasa a la plantilla.
        'suggest_current_name': _named_right_here(pos.key) or '',
        'suggest_play': '' if active_ucis is None else ','.join(active_ucis),
        'suggest_anchor': current_anchor or '',
        'suggest_name_max': SUGGESTION_NAME_MAX_CHARS,
        'suggest_comment_max': SUGGESTION_COMMENT_MAX_CHARS,
        'suggest_message': SUGGESTION_MESSAGES.get(
            request.GET.get('suggested', '')),
        'suggest_ok': request.GET.get('suggested') == 'ok',
        # Proponer una campana se hace desde la posicion que se esta mirando:
        # la raiz de la campana ES esta posicion, y pedirle a alguien que
        # copie una clave de 64 hex en otra pagina es pedirle que no lo haga.
        'campaign_name_max': CAMPAIGN_PROPOSER_MAX_CHARS,
        'campaign_title_max': CAMPAIGN_TITLE_MAX_CHARS,
        # Con sesion iniciada el credito ya esta decidido, asi que la caja no
        # ofrece un campo que el endpoint va a ignorar: dice con que nombre va
        # a salir.  El calculo es el MISMO que usa el endpoint.
        'campaign_credit': _campaign_credit(request),
        'nodes_h': _human(pos.nodes_invested),
        'time_str': f'{pos.time_invested:,.1f}s',
        'offtree': offtree,
        # Spent ladder: a click here lands below, so the page shows the
        # cascade underneath from the first render, before any click.
        'frontier_exhausted': ingest.ladder_exhausted(pos),
        'verdict_mate': (f'≤M{(pos.mate_in + 1) // 2}'
                         if pos.status != 'UNKNOWN' and pos.mate_in
                         else None),
        'verdict_css': _move_css(pos.status, eval_stm, win)})


def method(request):
    return render(request, 'atomicdb/method.html')


def conquest_map(request):
    """Public shell for the snapshot-backed Atomic move tree.

    Rendering this page never walks or mutates the solver graph.  The
    versioned map endpoint supplies the bounded display-tree projection.
    """
    root_fen = logic.start_fen()
    return render(request, 'atomicdb/map.html', {
        'root_key': logic.key_of(root_fen),
        'root_fen': root_fen,
    })
