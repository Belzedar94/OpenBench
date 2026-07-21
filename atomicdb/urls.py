from django.urls import path

from . import views

urlpatterns = [
    path('', views.home),
    path('explore/<str:key>/', views.explore),
    path('walls/', views.walls),
    path('method/', views.method),
    path('api/lease', views.api_lease),
    path('api/submit', views.api_submit),
]
