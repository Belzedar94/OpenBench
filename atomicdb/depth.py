"""Quien puede elegir el peldano de una peticion, y cual eligio.

QUE ES.  El boton "Request analysis" del explorador compra SIEMPRE el peldano
que le toca a esa posicion (§ ``ingest._request_rung``): 128M la primera vez,
512M cuando el 128M ya esta completado, y asi hasta 10B.  Esto anade la
posibilidad de saltarse la escalera hacia ARRIBA — y solo hacia arriba — a
quien sostiene la flota: el propietario del sitio y quien haya prestado una
maquina esta semana.

POR QUE UN MODULO Y NO UNA COMPROBACION EN LA PLANTILLA.  Esconder el control
no es negar el permiso: el endpoint de peticion acepta un POST de cualquiera,
asi que la unica comprobacion que cuenta es la del servidor.  La plantilla
consume ``context()`` y el endpoint consume ``chosen_rung()``, y las dos
preguntan a la MISMA funcion (``may_choose``) — una sola definicion de "tiene
derecho a elegir", que es lo que impide que la pagina prometa algo que la vista
no vaya a conceder.

QUIEN.  El propietario se identifica por ``is_staff``, igual que en las vistas
de campanas (§ ``views.campaign_state``).  Un CONTRIBUIDOR es cualquier cuenta
con un ``WorkerPing`` visto en los ultimos siete dias: la maquina prestada es
lo que paga la profundidad que se elige, y una semana cubre de sobra a quien
apaga el ordenador el fin de semana sin dejar dentro a quien presto una tarde
hace tres meses.
"""

from . import lanes
from .ingest import REQUEST_BUDGET_LADDER, request_ladder_state

# La ventana de contribuidor vive en ``lanes`` desde que la comunidad voto los
# carriles: la misma pregunta ("corrio un worker esta semana") decide quien
# elige peldano y quien tiene carril propio, y dos implementaciones de una sola
# pregunta acaban discrepando el dia que una cambie.  Se reexporta el nombre
# porque este modulo lo publicaba y hay quien lo importa de aqui.
CONTRIBUTOR_WINDOW_DAYS = lanes.CONTRIBUTOR_WINDOW_DAYS
# Nombre del campo en el POST de ``/atomicdb/request/<key>/``.
BUDGET_FIELD = 'budget'


def may_choose(user):
    """Tiene derecho a elegir peldano?  Propietario o contribuidor reciente.

    Un anonimo sale por la primera linea SIN tocar la base: el explorador lo
    llama en cada render y la inmensa mayoria de las visitas no tienen sesion.

    Una cuenta REVOCADA no elige aunque su maquina siguiera pingando hace un
    rato: ``Profile.enabled`` es la misma puerta que cierra el protocolo de
    workers (§ ``views._worker_account``), y un permiso que sobrevive a la
    revocacion no es un permiso.  El propietario no pasa por ahi porque
    ``is_staff`` ya es la llave del sitio entero.
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_staff:
        return True
    from OpenBench.models import Profile
    if not Profile.objects.filter(user=user, enabled=True).exists():
        return False
    return lanes.ran_a_worker(user.username)


def chosen_rung(request):
    """El peldano que pide este POST: ``(nodos, invalido)``.

    ``(None, False)``  no eligio nada — el boton de siempre, sin un solo
                       cambio de comportamiento.
    ``(None, True)``   eligio algo que NO es un peldano de la escalera: se
                       rechaza entero y no se encola nada.  Un valor que la
                       escalera no contiene no tiene un "por defecto razonable"
                       — 999 no es 128M ni 10B — y tragarselo en silencio
                       esconderia tanto una manipulacion como un despliegue a
                       medias (pagina vieja, escalera nueva).
    ``(nodos, False)`` eligio un peldano y tiene derecho a elegirlo.

    SIN derecho, la eleccion se DESCARTA y la peticion sigue por el peldano por
    defecto — no se rechaza.  Pedir analisis lo puede hacer cualquiera; lo que
    no puede es decidir cuanto se gasta.  Devolver 403 convertiria una peticion
    legitima en un boton roto y, de paso, seria un oraculo de quien tiene
    derecho y quien no.
    """
    return chosen_rung_for(request.POST.get(BUDGET_FIELD),
                           getattr(request, 'user', None))


def chosen_rung_for(raw, user):
    """La misma regla, con la cuenta DICHA en vez de leida de la sesion.

    La API oficial de peticion (§ ``views.api_public_request``) no mira la
    cookie de sesion a proposito, asi que su cuenta no esta en
    ``request.user``: le llega en el cuerpo, autenticada con credenciales.  El
    derecho a elegir peldano es el mismo para las dos puertas, y por eso la
    regla se lee de un solo sitio en vez de copiarse con otra fuente de
    identidad al lado.
    """
    raw = (raw or '').strip()
    if not raw:
        return None, False
    try:
        budget = int(raw)
    except ValueError:
        return None, True
    if budget not in REQUEST_BUDGET_LADDER:
        return None, True
    if not may_choose(user):
        return None, False
    return budget, False


def context(request, pos):
    """Lo que la plantilla pinta del selector, o ``{}`` si no hay selector.

    Vacio para todo el que no puede elegir, y el explorador queda EXACTAMENTE
    como estaba: la plantilla no pinta ni el hueco (§ ``_suggestions_badge``,
    el mismo patron).

    Vacio tambien cuando queda UN solo peldano por comprar, aunque quien mire
    sea el propietario: un slider con una sola parada no elige nada, y el boton
    ya compra exactamente eso.  Ahi entran los dos casos de final de escalera —
    el 2B gastado (solo queda 10B) y el 10B gastado (la peticion se convierte
    en anchura un ply mas abajo, § ``ingest._expand_frontier``), que es
    justamente donde un selector de PROFUNDIDAD prometeria algo que la vista no
    hace.

    Los peldanos ya COMPLETADOS en esta posicion no se ofrecen: comprarlos otra
    vez seria repetir una busqueda que ya tenemos.  Por eso la lista empieza en
    el peldano por defecto — el indice 0 del slider ES el comportamiento de hoy
    — y solo se puede viajar hacia arriba.
    """
    if pos.status != 'UNKNOWN':
        return {}
    if not may_choose(getattr(request, 'user', None)):
        return {}
    from .views import _human

    completed_max, due = request_ladder_state(pos)
    rungs = [rung for rung in REQUEST_BUDGET_LADDER if rung >= due]
    if len(rungs) < 2:
        return {}
    return {
        'depth_rungs': [{'nodes': rung, 'label': _human(rung)}
                        for rung in rungs],
        'depth_max_index': len(rungs) - 1,
        'depth_default_label': _human(due),
        # Lo mas profundo que esta posicion tiene ya COMPLETADO, que es lo que
        # explica por que la escalera no empieza en 128M.  No tiene por que ser
        # un peldano de la escalera (una sonda automatica de 8M tambien cuenta),
        # asi que se dice el numero real y no el peldano de al lado.
        'depth_spent_label': _human(completed_max) if completed_max else '',
    }
