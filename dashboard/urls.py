from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('dashboard/', views.dashboard, name='dashboard_explicit'),
    #path('analytics/', views.property_analytics, name='property_analytics'),
    path('analytics/', views.advanced_analytics, name='advanced_analytics'),
    path('analytics/query/', views.analytics_query, name='analytics_query'),
    path('financial-reporting/', views.financial_reporting, name='financial_reporting'),
    path('drillthrough/units/', views.total_units_drillthrough, name='total_units_drillthrough'),
    path('drillthrough/units/export/', views.total_units_drillthrough_export, name='total_units_drillthrough_export'),
    path('drillthrough/occupancy/', views.occupancy_drillthrough, name='occupancy_drillthrough'),
    path('drillthrough/occupancy/export/', views.occupancy_drillthrough_export, name='occupancy_drillthrough_export'),
    path('drillthrough/exposure/', views.exposure_drillthrough, name='exposure_drillthrough'),
    path('drillthrough/exposure/export/', views.exposure_drillthrough_export, name='exposure_drillthrough_export'),
    path('sample/', views.sample_page, name='sample_page'),
]
