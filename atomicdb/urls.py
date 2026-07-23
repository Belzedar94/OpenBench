from django.urls import path

from . import views
from .conquest_map import map_api

urlpatterns = [
    path('', views.home),
    path('map/', views.conquest_map, name='atomicdb-map'),
    path('explore/<str:key>/', views.explore),
    path('goto/<str:key>/<str:uci>/', views.goto),
    path('method/', views.method),
    path('request/<str:key>/', views.api_request),
    path('fen/', views.fen_jump),
    path('api/query', views.api_query),
    path('api/map/v1', map_api),
    path('api/lease', views.api_lease),
    path('api/heartbeat', views.api_heartbeat),
    path('api/submit', views.api_submit),
]
