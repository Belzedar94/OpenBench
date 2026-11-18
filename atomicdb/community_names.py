"""Nombres de apertura propuestos por la comunidad y ya moderados.

DONDE VIVE EL CATALOGO REAL, Y POR QUE ESTO VA APARTE.  El catalogo de
aperturas de AtomicDB es un artefacto ESTATICO y auditado
(``atomicdb/data/atomic_openings_v1.json``): ``openings.validate_catalog``
comprueba recuentos exactos por fuente, una lista blanca de identidades
modernas fijadas por (id, nombre, posicion) y un digest exterior sobre todo el
documento.  Reescribirlo en caliente romperia justo la propiedad por la que
existe.  Asi que un nombre aprobado NO se mete en el JSON: se aplica ENCIMA,
desde la base de datos, y solo donde el catalogo auditado no dice nada.

Reglas:

* el catalogo auditado manda por DEFECTO.  Una propuesta de nombre nuevo
  (``NEW``) nunca lo pisa: si el catalogo nombra la posicion, la propuesta
  simplemente no se aplica;
* la unica excepcion es una CORRECCION (``EDIT``) aprobada — ver abajo;
* un nombre comunitario aprobado se pinta marcado como tal, nunca disfrazado
  de nombre auditado;
* la sustitucion es por POSICION, igual que el catalogo, asi que las
  transposiciones lo reciben gratis;
* y a lo largo de una linea un nombre comunitario en vigor HEREDA igual que
  uno auditado: una posicion sin nombre propio lleva el de la posicion con
  nombre mas profunda de su ruta, sea cual sea la capa que la nombro
  (``deepest_on_line``);
* fallo abierto: si esta capa no puede leerse, el explorador se comporta
  exactamente como antes de existir.

POR QUE UNA EDICION SI PUEDE DESPLAZAR AL CATALOGO (28-jul).  El propietario
pidio poder corregir nombres existentes, no solo proponer los que faltan, y su
propio ejemplo era un nombre auditado ("King Knight" -> "King's Knight
Attack").  Editar el JSON en caliente esta descartado: su identidad esta fijada
por digest y ``validate_catalog`` la comprueba, asi que reescribirlo rompe
justo la propiedad por la que existe.  Asi que no se reescribe NADA: el JSON
sigue byte a byte igual y sigue siendo la respuesta de ``openings.match_key``.
Lo que cambia es solo lo que se PINTA, y solo cuando se cumplen las tres cosas
a la vez:

1. la fila es ``kind=EDIT``, es decir nacio diciendo "esto corrige un nombre
   que ya existe" y guardo cual;
2. un moderador leyo en la cola "actual -> propuesto" y pulso Approve.  No hay
   camino automatico: la decision es humana, atribuida (``resolved_by``) y
   fechada, exactamente la misma aprobacion que ya existia;
3. la ficha de la posicion lo dice en voz alta: es una correccion comunitaria,
   y el catalogo auditado sigue leyendo lo otro.

Lo que el catalogo protege es que nadie le cambie el contenido a sus espaldas.
Una correccion aprobada a mano y etiquetada como tal no es eso.  Un nombre
NUEVO si lo seria — nadie lo miro contra un nombre oficial — y por eso ese
camino sigue cerrado.

El mapa completo de nombres aprobados cabe de sobra en memoria (son decenas),
se lee de una sola consulta y se cachea unos segundos, con invalidacion
explicita en cuanto un moderador aprueba o rechaza algo.

HIDDEN NAMES (math_god, 18-Nov-2026, with the owner's standing endorsement).
An approver can hide the name a position shows, whichever layer it comes
from, and a visitor can ask for it by proposing an empty name with a reason.
Both end as an approved ``kind=HIDE`` row, and the rules are:

* the catalogue JSON is untouched.  A hide is one more community-layer
  override, read from the database like every other approval;
* the latest approval on a position still wins.  A HIDE takes the position's
  community name off AND keeps the catalogue from naming it.  A later EDIT
  names it again; a later NEW does too, because approving a NEW on a
  position whose catalogue name is hidden turns it into an EDIT
  (``name_votes.resolve_suggestion``: it does replace the catalogue there);
  a later REMOVE ("show again") lifts the hide;
* a hidden position behaves exactly as an unnamed one.  It shows no name of
  its own, and a lookup along a line goes on to the next named position
  further up, the same as for any unnamed node: its descendants inherit from
  that shallower name, not from the hidden one.  The catalogue readers that
  name positions take ``hidden=hidden_keys()`` for that
  (§ ``openings.match_line_keys_object``).
"""

import logging
import threading
import time

from django.core.cache import cache

from . import openings

logger = logging.getLogger(__name__)

# v2: each entry gained ``suggested_by``.  A new key, so no process reads a
# v1 map without it from a shared cache during a deploy.
CACHE_KEY = 'atomicdb.community-opening-names.v2'
# {position_key: {'previous', 'approved_by', 'approved_at'}} of the positions
# whose latest approval is a HIDE.  Written by the same read as the map.
HIDDEN_CACHE_KEY = 'atomicdb.community-hidden-names.v1'
# El backend por defecto es LocMem: UNA cache POR PROCESO.  Con varios workers
# de gunicorn, ``invalidate()`` solo borra la entrada del proceso que atendio
# la moderacion — los demas ni se enteran.  El limite real de obsolescencia
# entre procesos es este TTL, asi que se mantiene CORTO: el mapa son decenas
# de filas en una consulta, y pagarla cada pocos segundos por proceso cuesta
# menos que explicar por que un nombre aprobado tarda un minuto en aparecer
# segun a que worker le toque tu peticion.
CACHE_SECONDS = 5


def approved_map():
    """{position_key: {'name', 'kind', 'corrects', 'approved_by',
    'approved_at', 'suggested_by'}} — fail-open.

    UNA VEZ POR RENDER, NO UNA POR FILA.  El mapa entero es la unidad: se pide
    completo, cabe en memoria y responde por cualquier posicion.  La tabla del
    explorador preguntaba por CADA hijo, y con la cache compartida cada una de
    esas preguntas es un viaje a Redis — cuarenta y tantos por pagina para
    devolver siempre el mismo diccionario.  Por eso los consumidores aceptan
    un ``names`` ya resuelto: quien pinta una lista lo pide una vez y lo
    reparte.
    """
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    loaded = _load()
    return {} if loaded is None else loaded[0]


def _load():
    """``(names, hidden)`` from one read of the approvals, both cached; or
    ``None`` when the table cannot be read."""
    from .models import OpeningNameSuggestion
    try:
        rows = (OpeningNameSuggestion.objects
                .filter(status=OpeningNameSuggestion.SState.APPROVED)
                .order_by('resolved_at', 'id')
                .values_list('position_id', 'proposed_name', 'resolved_by',
                             'resolved_at', 'kind', 'previous_name',
                             'suggested_by'))
        names, hidden = {}, {}
        for (key, name, approved_by, approved_at, kind, previous,
             suggested_by) in rows:
            # A hide wins like any later approval, and leaves no community
            # name: the position goes into ``hidden`` instead, which also
            # keeps the catalogue from naming it.  Any later approval lifts
            # it again.
            if kind == OpeningNameSuggestion.SKind.HIDE:
                names.pop(key, None)
                hidden[key] = {'previous': previous,
                               'approved_by': approved_by,
                               'approved_at': approved_at}
                continue
            hidden.pop(key, None)
            # Una aprobacion posterior sobre la misma posicion manda.  Y eso
            # ya era, por si solo, el camino de escritura de una edicion de
            # nombre COMUNITARIO: aprobar la siguiente sustituye a la anterior.
            names[key] = {'name': name, 'kind': kind, 'corrects': previous,
                          'approved_by': approved_by,
                          'approved_at': approved_at,
                          # Who proposed the name in force, shown with it
                          # (owner, 11-Nov-2026).  It rides on the same
                          # latest-approval-wins row as the name, so the
                          # credit can never belong to a superseded one, and
                          # it expires with the name in ``invalidate``.
                          'suggested_by': suggested_by}
            # An approved removal (opening-name votes, 11-Nov-2026) wins like
            # any later approval, and what it leaves is no community name at
            # all: the position goes back to its catalogue name, or to the
            # name it inherits from its line.  Dropped here, so no reader of
            # the map ever sees it.
            if kind == OpeningNameSuggestion.SKind.REMOVE:
                del names[key]
    except Exception:
        logger.exception('community opening names unavailable')
        return None
    cache.set(CACHE_KEY, names, CACHE_SECONDS)
    cache.set(HIDDEN_CACHE_KEY, hidden, CACHE_SECONDS)
    return names, hidden


def hidden_map():
    """``{position_key: {'previous', 'approved_by', 'approved_at'}}`` of the
    positions whose name is hidden — fail-open (``{}``)."""
    cached = cache.get(HIDDEN_CACHE_KEY)
    if cached is not None:
        return cached
    loaded = _load()
    return {} if loaded is None else loaded[1]


# The hidden keys, memoised IN THE PROCESS for ``CACHE_SECONDS``.  The
# catalogue readers ask per position (each child of an explorer table, each
# step of a line), and a trip to the shared cache per question is exactly what
# ``approved_map`` warns against.  Same staleness bound as the map: the
# process that approved a hide forgets at once (``invalidate``), the others
# within ``CACHE_SECONDS``.
_hidden_memo = {'keys': None, 'until': 0.0}
_hidden_lock = threading.Lock()


def hidden_keys():
    """The keys of every position whose name is hidden, as a frozenset."""
    now = time.monotonic()
    keys = _hidden_memo['keys']
    if keys is not None and now < _hidden_memo['until']:
        return keys
    keys = frozenset(hidden_map())
    with _hidden_lock:
        _hidden_memo['keys'] = keys
        _hidden_memo['until'] = now + CACHE_SECONDS
    return keys


def reset_hidden_memo():
    """Forget the process memo of ``hidden_keys`` (tests, ``invalidate``)."""
    with _hidden_lock:
        _hidden_memo['keys'] = None
        _hidden_memo['until'] = 0.0


def is_hidden(position_key):
    """True when the latest approval on the position is a HIDE, read from
    the database and not from any cache: the approval path decides on it."""
    from django.db.models import F
    from .models import OpeningNameSuggestion
    latest = (OpeningNameSuggestion.objects
              .filter(position_id=position_key,
                      status=OpeningNameSuggestion.SState.APPROVED)
              .order_by(F('resolved_at').desc(nulls_last=True), '-id')
              .values_list('kind', flat=True).first())
    return latest == OpeningNameSuggestion.SKind.HIDE


ANONYMOUS_CREDIT = 'anonymous'


def credit(entry):
    """Who a community name is credited to: the account that proposed the
    approved suggestion in force, or ``'anonymous'`` when it was proposed
    without one.  Only the account name is public; the rest of the
    suggestion row (address, comment) never leaves the moderation queue.
    """
    return (entry.get('suggested_by') or '').strip() or ANONYMOUS_CREDIT


def invalidate():
    """Borra la entrada DE ESTE PROCESO.

    Con LocMem eso es todo lo que puede prometer: el proceso que sirvio la
    aprobacion repinta al instante, y el resto converge en ``CACHE_SECONDS``
    como mucho.  Si algun dia el backend pasa a ser compartido (Redis,
    memcached), esta misma llamada se vuelve global sin tocar nada mas.
    """
    cache.delete(CACHE_KEY)
    cache.delete(HIDDEN_CACHE_KEY)
    reset_hidden_memo()


def in_force(position_key, names=None):
    """La entrada comunitaria que MANDA en esta posicion, o ``None``.

    Aqui vive la unica excepcion a "el catalogo auditado manda": una edicion
    aprobada.  Todo lo demas cede ante el catalogo, igual que siempre.

    ``names`` es el mapa completo ya resuelto por quien pinta una lista de
    posiciones (§ ``approved_map``).  Sin el se pide, que es el comportamiento
    de siempre para quien pregunta por una sola.
    """
    if names is None:
        names = approved_map()
    entry = names.get(position_key)
    if entry is None:
        return None
    from .models import OpeningNameSuggestion
    official = catalog_name(position_key)
    if (entry['kind'] != OpeningNameSuggestion.SKind.EDIT
            and official is not None):
        return None
    # Y asi se deshace una correccion: proponer de vuelta el nombre auditado y
    # aprobarlo.  La capa se aparta en vez de pintar "correccion comunitaria:
    # King Knight Opening" sobre "King Knight Opening", que es lo mismo dicho
    # dos veces y con peor letra.
    if entry['name'] == official:
        return None
    return entry


def name_for(position_key, names=None):
    entry = in_force(position_key, names=names)
    return None if entry is None else entry['name']


def deepest_on_line(position_keys, names=None, floor=0):
    """``(ply, entry)`` of the deepest community name in force on a line, or
    ``(None, None)``.

    ``position_keys[i]`` is the position after ``i`` plies of a line the
    caller already replayed, the shape ``openings.match_line_keys`` reads.
    Only the plies from ``floor`` on are asked: the caller passes the ply its
    catalogue match names, and a community name shallower than that loses to
    it.  At ``floor`` itself only an approved EDIT can answer, because a NEW
    name never stands where the catalogue names the position (``in_force``),
    and that is how a correction goes on replacing the name it corrects
    further down the line.
    """
    if names is None:
        names = approved_map()
    for ply in range(len(position_keys) - 1, max(floor, 0) - 1, -1):
        entry = in_force(position_keys[ply], names=names)
        if entry is not None:
            return ply, entry
    return None, None


def catalog_names(position_key):
    """True si el catalogo AUDITADO ya nombra exactamente esta posicion."""
    return catalog_name(position_key) is not None


def catalog_name(position_key):
    """El nombre AUDITADO de esta posicion exacta, o ``None`` — fallo abierto.

    Sigue siendo la verdad del artefacto estatico aunque una correccion
    comunitaria lo tape en pantalla: es lo que la ficha ensena para que nadie
    confunda una cosa con la otra.
    """
    try:
        opening = openings.match_key(position_key)
    except openings.OpeningCatalogError:
        logger.exception('opening catalogue unavailable')
        return None
    return None if opening is None else opening.name


def opening_for(position_key, fen='', names=None):
    """Registro con la forma que consumen las vistas, o ``None``.

    Deliberadamente sin ``records``/``sources``: un nombre comunitario no
    tiene procedencia documental que ensenar, y fingir una seria peor que no
    tener ninguna.  ``community`` es lo que la plantilla usa para decir de
    donde sale, y ``catalog_name`` lo que sigue leyendo el catalogo auditado
    cuando esto lo esta corrigiendo.
    """
    entry = in_force(position_key, names=names)
    if entry is None:
        return None
    return _record(position_key, entry, fen)


def inherited_opening(position_key, entry, *, current_key, matched_ply,
                      line_uci=(), line_san=''):
    """The record of the name of ``position_key``, read further down a line.

    The shape ``opening_for`` gives, with what a catalogue match carries when
    the page is past its named position: ``exact`` is False, ``current_key``
    is the page and ``matched_ply`` the ply of the named position.  The
    reference line is the route's own way into that position, the only line a
    community name has.  ``entry`` is what ``deepest_on_line`` returned for
    that position.
    """
    record = _record(position_key, entry)
    record.update({
        'reference_line_uci': list(line_uci),
        'reference_line_san': line_san,
        'ply': matched_ply,
        'matched_ply': matched_ply,
        'current_key': current_key,
        'exact': False,
    })
    return record


def _record(position_key, entry, fen=''):
    return {
        'position_key': position_key,
        'fen': fen,
        'name': entry['name'],
        'status': 'community_approved',
        'confidence': 'community',
        'names': [entry['name']],
        'aliases': [],
        'reference_line_uci': [],
        'reference_line_san': '',
        'ply': 0,
        'sources': [],
        'evidence': [],
        'matched_ply': 0,
        'current_key': position_key,
        'exact': True,
        'community': True,
        'corrects': entry['corrects'],
        'catalog_name': catalog_name(position_key) or '',
        'approved_by': entry['approved_by'],
        'approved_at': entry['approved_at'],
        'named_by': credit(entry),
    }
