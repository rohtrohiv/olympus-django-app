from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('dashboard/', views.dashboard, name='dashboard_explicit'),
    #path('analytics/', views.property_analytics, name='property_analytics'),
    path('analytics/', views.advanced_analytics, name='advanced_analytics'),
    path('analytics/query/', views.analytics_query, name='analytics_query'),
    path('sample/', views.sample_page, name='sample_page'),
]
