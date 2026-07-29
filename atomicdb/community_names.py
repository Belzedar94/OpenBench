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
"""

import logging

from django.core.cache import cache

from . import openings

logger = logging.getLogger(__name__)

CACHE_KEY = 'atomicdb.community-opening-names.v1'
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
    'approved_at'}} — fail-open."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    from .models import OpeningNameSuggestion
    try:
        rows = (OpeningNameSuggestion.objects
                .filter(status=OpeningNameSuggestion.SState.APPROVED)
                .order_by('resolved_at', 'id')
                .values_list('position_id', 'proposed_name', 'resolved_by',
                             'resolved_at', 'kind', 'previous_name'))
        names = {}
        for key, name, approved_by, approved_at, kind, previous in rows:
            # Una aprobacion posterior sobre la misma posicion manda.  Y eso
            # ya era, por si solo, el camino de escritura de una edicion de
            # nombre COMUNITARIO: aprobar la siguiente sustituye a la anterior.
            names[key] = {'name': name, 'kind': kind, 'corrects': previous,
                          'approved_by': approved_by,
                          'approved_at': approved_at}
    except Exception:
        logger.exception('community opening names unavailable')
        return {}
    cache.set(CACHE_KEY, names, CACHE_SECONDS)
    return names


def invalidate():
    """Borra la entrada DE ESTE PROCESO.

    Con LocMem eso es todo lo que puede prometer: el proceso que sirvio la
    aprobacion repinta al instante, y el resto converge en ``CACHE_SECONDS``
    como mucho.  Si algun dia el backend pasa a ser compartido (Redis,
    memcached), esta misma llamada se vuelve global sin tocar nada mas.
    """
    cache.delete(CACHE_KEY)


def in_force(position_key):
    """La entrada comunitaria que MANDA en esta posicion, o ``None``.

    Aqui vive la unica excepcion a "el catalogo auditado manda": una edicion
    aprobada.  Todo lo demas cede ante el catalogo, igual que siempre.
    """
    entry = approved_map().get(position_key)
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


def name_for(position_key):
    entry = in_force(position_key)
    return None if entry is None else entry['name']


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


def opening_for(position_key, fen=''):
    """Registro con la forma que consumen las vistas, o ``None``.

    Deliberadamente sin ``records``/``sources``: un nombre comunitario no
    tiene procedencia documental que ensenar, y fingir una seria peor que no
    tener ninguna.  ``community`` es lo que la plantilla usa para decir de
    donde sale, y ``catalog_name`` lo que sigue leyendo el catalogo auditado
    cuando esto lo esta corrigiendo.
    """
    entry = in_force(position_key)
    if entry is None:
        return None
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
    }
