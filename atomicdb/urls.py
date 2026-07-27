from django.urls import path
from django.views.decorators.cache import cache_page
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
# cookies, asi que el primer visitante no envenena la entrada compartida.
#
# ``map`` y ``method`` no tienen formularios ni estado por visitante: una
# unica entrada compartida para todo el mundo.
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
_home_cached = cache_page(15)(vary_on_cookie(views.home))
_map_cached = cache_page(30)(views.conquest_map)
_method_cached = cache_page(30)(views.method)

urlpatterns = [
    path('', _home_cached),
    path('map/', _map_cached, name='atomicdb-map'),
    path('explore/<str:key>/', views.explore),
    path('goto/<str:key>/<str:uci>/', views.goto),
    path('method/', _method_cached),
    path('suggest/<str:key>/', views.suggest_opening_name),
    path('suggestions/', views.suggestions, name='atomicdb-suggestions'),
    path('request/<str:key>/', views.api_request),
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
