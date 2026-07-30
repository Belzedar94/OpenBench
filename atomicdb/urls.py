from django.urls import path
from django.views.decorators.cache import cache_page
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.vary import vary_on_cookie

from . import views
from .conquest_map import map_api

# Cache corta sobre las vistas de LECTURA calientes, con el mismo patron que
# el index de OpenBench ("perf: 15s cookie-varied cache"): absorbe tormentas
# de F5 sin que nadie llegue a ver datos de hace un minuto.
#
# ``home`` lleva el cajetin de FEN y, con el, un token CSRF por visitante:
# varia por cookie para no servirle a nadie el token de otro.  Django ademas
# se niega a cachear una respuesta que ESTRENA cookie ante una peticion sin
# cookies... pero esa negativa mira ``response.cookies``, y con el CSRF puesto
# por MIDDLEWARE ese diccionario todavia esta vacio cuando el cache guarda:
# ``cache_page`` es un decorador, asi que cierra ANTES que
# CsrfViewMiddleware.process_response.  Medido: el segundo visitante sin
# cookies recibia el cuerpo del primero, token incluido, y ninguna cookie
# propia — su POST del cajetin de FEN se rechazaba con 403.
#
# ``csrf_protect`` va POR DENTRO justamente por eso: pone la cookie antes de
# que el cache decida, y entonces la guarda de Django dispara de verdad.  El
# precio, deliberado: la primera visita SIN cookies no se cachea ni anuncia
# cache — es una respuesta personal y no debe guardarla nadie, aqui ni aguas
# abajo.  A partir de la segunda peticion ese visitante ya trae cookie y entra
# al cache con su propia entrada, que es donde estan las tormentas de F5.
#
# ``map`` y ``method`` VARIAN POR COOKIE desde que la cabecera lleva zona de
# identidad.  Antes no tenian nada por visitante y compartian una sola entrada;
# ahora su HTML dice quien eres — nombre, campana, recuento — y una entrada
# compartida seria servirle a un visitante la sesion de otro.  El precio esta
# medido y es el que ya paga la home: quien NO ha iniciado sesion sigue
# compartiendo entrada (la cabecera anonima no lleva formulario, asi que no
# estrena cookie CSRF por venir aqui), y quien la ha iniciado tiene la suya,
# que es justo lo que hace falta.
#
# El tema no entra en esta ecuacion: la eleccion vive en localStorage y la
# aplica un script en el propio <html>, asi que el HTML es identico en claro y
# en oscuro y una entrada cacheada sirve para ambos.
#
# Deliberadamente SIN cache:
#   * ``explore``: su poller compara los ``visits``/``status`` que la pagina
#     trajo renderizados contra /api/query, y servir un cuerpo viejo le daria
#     una linea base falsa (recargas fantasma, o recargas que no llegan).
#     Ademas cambia de estado en cuanto alguien pide analisis ahi.
#   * ``goto`` y ``fen``: escriben en el arbol.
#   * ``request``, ``api/query`` y ``api/frontier``: estado vivo, y son
#     justo lo que el explorador consulta para saber si algo cambio.
#   * todo el protocolo de workers (``lease``/``heartbeat``/``submit``).
#   * ``api/map/v1``: ya negocia su propio ETag sobre un snapshot publicado y
#     no toca la base viva.
_home_cached = cache_page(15)(vary_on_cookie(csrf_protect(views.home)))
_map_cached = cache_page(30)(vary_on_cookie(csrf_protect(views.conquest_map)))
_method_cached = cache_page(30)(vary_on_cookie(csrf_protect(views.method)))

urlpatterns = [
    path('', _home_cached),
    path('map/', _map_cached, name='atomicdb-map'),
    path('explore/<str:key>/', views.explore),
    path('goto/<str:key>/<str:uci>/', views.goto),
    # Salto al origen de un valor respaldado: camina la espina de soporte y
    # redirige.  Solo LEE (a diferencia de ``goto``, que crea la arista), pero
    # el destino se mueve con cada analisis que cae debajo, asi que tampoco
    # lleva cache.
    path('backed-source/<str:key>/', views.backed_source),
    path('method/', _method_cached),
    path('suggest/<str:key>/', views.suggest_opening_name),
    path('suggestions/', views.suggestions, name='atomicdb-suggestions'),
    # Avisos: la lista entera y el POST que los marca vistos, en la misma
    # ruta.  Escribe, y ademas es de una sola persona: sin cache.
    path('notifications/', views.notifications_page,
         name='atomicdb-notifications'),
    # Campanas de exploracion: las dos primeras son publicas, la tercera es
    # del propietario y lo comprueba ella misma (no basta con esconder el
    # boton).  Las tres escriben, asi que ninguna lleva cache.
    path('campaign/propose/', views.campaign_propose),
    path('campaign/<int:campaign_id>/vote/', views.campaign_vote),
    path('campaign/<int:campaign_id>/state/', views.campaign_state),
    path('request/<str:key>/', views.api_request),
    path('request-unexplored/<str:key>/', views.api_request_unexplored),
    path('pv-verify/<str:key>/', views.api_pv_verify),
    path('fen/', views.fen_jump),
    path('api/query', views.api_query),
    path('api/frontier/<str:key>/', views.api_frontier),
    path('api/map/v1', map_api),
    path('api/lease', views.api_lease),
    path('api/heartbeat', views.api_heartbeat),
    path('api/submit', views.api_submit),
    # Protocolo SOLVE: aditivo. Un worker anterior no conoce estas rutas.
    path('api/solve/acquire', views.api_solve_acquire),
    path('api/solve/heartbeat', views.api_solve_heartbeat),
    path('api/solve/submit', views.api_solve_submit),
]
