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

from urllib.parse import quote

from django.utils.timesince import timesince

from .models import RequestNotification


# Diez filas en el desplegable: es lo que cabe sin convertirlo en una pagina.
DROPDOWN_ROWS = 10
# La pagina completa muestra mas, pero sigue estando acotada: esto es un
# historial de cortesia, no un registro de auditoria.
PAGE_ROWS = 50
# Por encima de esto el numero exacto deja de decir nada util y estropea la
# forma de la insignia.
BADGE_CAP = 99


def _rows(username, limit):
    return list(RequestNotification.objects.filter(username=username)
                .order_by('-created', '-id')[:limit])


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
        items.append({
            'key': row.position_id,
            'san': preview or 'the start position',
            'full': full or 'the start position',
            'created': row.created,
            'ago': ago(row.created),
            'seen': row.seen,
        })
    return items


def mark_seen(username):
    """Marca como vistos los avisos de esta cuenta. Devuelve cuantos cambio.

    Un UPDATE en bloque y filtrado por ``seen=False``: no lee para escribir,
    asi que dos pestanas abriendo la campana a la vez no se pisan y la segunda
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


def identity(request):
    """Contexto de la zona de identidad. Vacio fuera de AtomicDB."""
    if not request.path.startswith('/atomicdb/'):
        return {}
    user = getattr(request, 'user', None)
    context = {'identity_next': _return_path(request)}
    if user is None or not user.is_authenticated:
        return context
    count = unseen_count(user.username)
    context.update({
        'notifications': presented(user.username),
        'notifications_unseen': count,
        'notifications_badge': badge(count),
    })
    return context
