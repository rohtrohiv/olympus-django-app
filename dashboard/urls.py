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
    path('drillthrough/delinquency/', views.delinquency_drillthrough, name='delinquency_drillthrough'),
    path('drillthrough/delinquency/export/', views.delinquency_drillthrough_export, name='delinquency_drillthrough_export'),
    path('drillthrough/service-request/', views.service_request_drillthrough, name='service_request_drillthrough'),
    path('drillthrough/service-request/export/', views.service_request_drillthrough_export, name='service_request_drillthrough_export'),
    path('drillthrough/avg-turn-time/', views.avg_turn_time_drillthrough_view, name='avg_turn_time_drillthrough'),
    path('drillthrough/avg-turn-time/export/', views.avg_turn_time_drillthrough_csv, name='avg_turn_time_drillthrough_export'),
    path('drillthrough/occupancy-eom/', views.occupancy_eom_drillthrough_view, name='occupancy_eom_drillthrough'),
    path('drillthrough/occupancy-eom/export/', views.occupancy_eom_drillthrough_csv, name='occupancy_eom_drillthrough_csv'),
    path('sample/', views.sample_page, name='sample_page'),
]
