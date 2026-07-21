from django.urls import path

from . import views

urlpatterns = [
    path('', views.home),
    path('explore/<str:key>/', views.explore),
    path('goto/<str:key>/<str:uci>/', views.goto),
    path('method/', views.method),
    path('request/<str:key>/', views.api_request),
    path('fen/', views.fen_jump),
    path('api/query', views.api_query),
    path('api/lease', views.api_lease),
    path('api/submit', views.api_submit),
]
