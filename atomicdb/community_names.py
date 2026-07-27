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

* el catalogo auditado siempre manda.  Una posicion que ya tiene nombre
  oficial no admite propuestas (el endpoint las rechaza), asi que aqui nunca
  hay conflicto que resolver;
* un nombre comunitario aprobado se pinta marcado como tal, nunca disfrazado
  de nombre auditado;
* la sustitucion es por POSICION, igual que el catalogo, asi que las
  transposiciones lo reciben gratis;
* fallo abierto: si esta capa no puede leerse, el explorador se comporta
  exactamente como antes de existir.

El mapa completo de nombres aprobados cabe de sobra en memoria (son decenas),
se lee de una sola consulta y se cachea unos segundos, con invalidacion
explicita en cuanto un moderador aprueba o rechaza algo.
"""

import logging

from django.core.cache import cache

from . import openings

logger = logging.getLogger(__name__)

CACHE_KEY = 'atomicdb.community-opening-names.v1'
CACHE_SECONDS = 60


def approved_map():
    """{position_key: {'name', 'approved_by', 'approved_at'}} — fail-open."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    from .models import OpeningNameSuggestion
    try:
        rows = (OpeningNameSuggestion.objects
                .filter(status=OpeningNameSuggestion.SState.APPROVED)
                .order_by('resolved_at', 'id')
                .values_list('position_id', 'proposed_name', 'resolved_by',
                             'resolved_at'))
        names = {}
        for key, name, approved_by, approved_at in rows:
            # Una aprobacion posterior sobre la misma posicion manda.
            names[key] = {'name': name, 'approved_by': approved_by,
                          'approved_at': approved_at}
    except Exception:
        logger.exception('community opening names unavailable')
        return {}
    cache.set(CACHE_KEY, names, CACHE_SECONDS)
    return names


def invalidate():
    cache.delete(CACHE_KEY)


def name_for(position_key):
    entry = approved_map().get(position_key)
    return None if entry is None else entry['name']


def catalog_names(position_key):
    """True si el catalogo AUDITADO ya nombra exactamente esta posicion."""
    try:
        return openings.match_key(position_key) is not None
    except openings.OpeningCatalogError:
        logger.exception('opening catalogue unavailable')
        return False


def opening_for(position_key, fen=''):
    """Registro con la forma que consumen las vistas, o ``None``.

    Deliberadamente sin ``records``/``sources``: un nombre comunitario no
    tiene procedencia documental que ensenar, y fingir una seria peor que no
    tener ninguna.  ``community`` es lo que la plantilla usa para decir de
    donde sale.
    """
    entry = approved_map().get(position_key)
    if entry is None or catalog_names(position_key):
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
        'approved_by': entry['approved_by'],
        'approved_at': entry['approved_at'],
    }
