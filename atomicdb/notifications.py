"""Los avisos, tal y como los ve quien pidio el analisis.

QUE PINTA ESTO.  La cabecera de todas las paginas de AtomicDB lleva una zona
de identidad, y dentro de ella una campana con el recuento de avisos sin ver.
El recuento y las diez ultimas filas se renderizan EN EL SERVIDOR en cada
carga: no hay temporizador, ni sondeo, ni un endpoint que devuelva JSON cada
pocos segundos.  Una web que ya se recarga sola con cada click no necesita
descubrir novedades por su cuenta.

POR QUE UN CONTEXT PROCESSOR.  El buzon de sugerencias resuelve lo suyo con
un diccionario que cada vista fusiona a mano (``views._suggestions_badge``),
y hasta hoy tenia razon: ``map`` y ``method`` iban con ``cache_page`` PLANO y
un render con identidad dentro se le habria servido a cualquiera.  Eso deja
de ser cierto en este mismo cambio — las dos pasan a variar por cookie — y a
partir de ahi la alternativa es peor: la cabecera es de TODAS las paginas, y
una vista que se olvidara de pasar el contexto pintaria la campana vacia a
alguien que si tiene avisos.  Una cabecera que miente segun la pagina es peor
que una consulta de mas.

COSTE.  Para quien no ha iniciado sesion, cero consultas: se decide con
``request.user.is_authenticated`` y no se toca la tabla.  Para quien la ha
iniciado, un recuento por indice y una pagina de diez filas.
"""

import functools
import hashlib
import os
from urllib.parse import quote

from django.utils.timesince import timesince

from . import preferences
from .models import RequestNotification

# Version de cache de los estaticos propios, calculada al importar.  Cada
# deploy que toque uno cambia el numero y los navegadores dejan de casar su
# copia vieja — el estreno de la cabecera se vio ROTO (insignia debajo de la
# campana, pila vertical) exactamente por una hoja cacheada de antes del
# deploy.  Antes era el mtime de atomicdb.css a secas, asi que un cambio que
# solo tocara un .js salia con la URL de siempre; ahora es un digest del
# CONTENIDO de todos los ficheros de primer nivel (hoja y scripts propios, sin
# vendor), que tampoco depende de que el deploy conserve o no los mtimes.
def _static_version(folder=os.path.join(
        os.path.dirname(__file__), 'static', 'atomicdb')):
    digest = hashlib.sha256()
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            digest.update(name.encode('utf-8'))
            with open(path, 'rb') as handle:
                digest.update(handle.read())
    return digest.hexdigest()[:10]


STATIC_VERSION = _static_version()


# Diez filas en el desplegable: es lo que cabe sin convertirlo en una pagina.
DROPDOWN_ROWS = 10
# La pagina completa muestra mas, pero sigue estando acotada: esto es un
# historial de cortesia, no un registro de auditoria.
PAGE_ROWS = 50
# Por encima de esto el numero exacto deja de decir nada util y estropea la
# forma de la insignia.
BADGE_CAP = 99


def _rows(username, limit):
    # Sin leer PRIMERO, y dentro de cada grupo lo mas nuevo arriba: la lista
    # existe para lo pendiente; lo ya visitado es archivo.
    return list(RequestNotification.objects.filter(username=username)
                .order_by('seen', '-created', '-id')[:limit])


def unseen_count(username):
    if not username:
        return 0
    return RequestNotification.objects.filter(
        username=username, seen=False).count()


def badge(count):
    """El texto de la insignia. Sin avisos no hay insignia (cadena vacia)."""
    if not count:
        return ''
    return f'{BADGE_CAP}+' if count > BADGE_CAP else str(count)


def ago(moment):
    """La antiguedad en UNA sola unidad, mas un caso especial.

    El filtro ``timesince`` sin acotar devuelve "3 hours, 12 minutes", que en
    una fila de desplegable compite con lo unico que importa de la fila, que
    es la linea.  Y "0 minutes ago" es una forma rara de decir que acaba de
    pasar.
    """
    text = timesince(moment, depth=1)
    # ``timesince`` une numero y unidad con un espacio DURO para que no se
    # partan al final de una linea, asi que el numero se saca partiendo por
    # espacios — que en Python incluye el duro — y no comparando un prefijo
    # contra un espacio normal, que no casaria nunca.  Lo que se devuelve
    # conserva el espacio duro, que es lo que se quiere al pintarlo.
    if text.split()[:1] == ['0']:
        return 'just now'
    return f'{text} ago'


def presented(username, limit=DROPDOWN_ROWS):
    """Las ultimas filas con su linea SAN ya resuelta, listas para pintar.

    La etiqueta se calcula al PINTAR y no se guarda en la fila: es la misma
    lectura del linaje que usa el resto del sitio, comparte su cache de dos
    minutos, y asi un aviso viejo nunca ensena una linea que ya no es la de
    esa posicion.
    """
    from . import views     # el formateador de lineas vive con el explorador

    rows = _rows(username, limit)
    if not rows:
        return []
    labels = views._line_labels_many([row.position_id for row in rows],
                                     preview_plies=8)
    items = []
    for row in rows:
        preview, full = labels.get(row.position_id, ('', ''))
        play = ''
        if row.route:
            # El aviso habla en el ORDEN DE JUGADAS de quien pidio, no en el
            # linaje canonico del DAG: la misma posicion tiene tantas
            # historias como transposiciones, y el enlace debe volver a LA
            # SUYA (pidio 1.Nf3 d6... y el aviso le ensenaba 1.Nf3 f6...).
            # La ruta se re-valida contra las reglas al pintar: si dejo de
            # ser legal (rekey, PV vieja) o ya no llega a este nodo, el aviso
            # cae al linaje canonico sin romper la campana.
            try:
                labelled = views._route_labels(row.route, row.position_id,
                                               preview_plies=8)
            except views.PlayRouteError:
                labelled = None
            if labelled and labelled[0]:
                preview, full = labelled
                play = row.route
        items.append({
            'key': row.position_id,
            'san': preview or 'the start position',
            'full': full or 'the start position',
            'play': play,
            'created': row.created,
            'ago': ago(row.created),
            'seen': row.seen,
        })
    return items


def mark_position_seen(username, position_key):
    """Leida = VISITADA: llegar a la posicion apaga SU aviso y solo el suyo.

    Da igual el camino — el click en el propio aviso o llegar por su pie: la
    vista de explore llama aqui en cada carga con sesion.  Abrir el panel NO
    marca nada; para eso esta el boton explicito de ``mark_seen``.
    """
    if not username:
        return 0
    return RequestNotification.objects.filter(
        username=username, position_id=position_key,
        seen=False).update(seen=True)


def mark_seen(username):
    """Marca como vistos TODOS los avisos de esta cuenta (boton explicito).

    Un UPDATE en bloque y filtrado por ``seen=False``: no lee para escribir,
    asi que dos pestanas pulsando a la vez no se pisan y la segunda
    simplemente no cambia nada.
    """
    if not username:
        return 0
    return RequestNotification.objects.filter(
        username=username, seen=False).update(seen=True)


def _return_path(request):
    """A donde vuelve un login que empieza en AtomicDB.

    Solo la ruta y su query, nunca una URL absoluta: lo que se manda en
    ``?next=`` acaba siendo un redirect, y componerlo con algo que venga de
    fuera es como se construye un redirect abierto.  El destino sale del
    ``path`` que Django ya resolvio, no de una cabecera del cliente.
    """
    path = request.path
    query = request.META.get('QUERY_STRING', '')
    return quote(f'{path}?{query}' if query else path, safe='')


@functools.lru_cache(maxsize=1)
def start_url():
    """The explorer page of the start position, for the "Analyze" nav link.

    A constant of the variant, so it is computed once per process.
    """
    from . import logic
    return f'/atomicdb/explore/{logic.key_of(logic.start_fen())}/'


def identity(request):
    """Contexto de la zona de identidad. Vacio fuera de AtomicDB."""
    if not request.path.startswith('/atomicdb/'):
        return {}
    user = getattr(request, 'user', None)
    context = {'identity_next': _return_path(request),
               'static_version': STATIC_VERSION,
               'start_url': start_url()}
    if user is None or not user.is_authenticated:
        return context
    count = unseen_count(user.username)
    context.update({
        'notifications': presented(user.username),
        'notifications_unseen': count,
        'notifications_badge': badge(count),
        # The settings panel: its queue option, and where its POST returns.
        'settings_lifo': preferences.lifo_on(user.username),
        'identity_path': request.get_full_path(),
    })
    return context
