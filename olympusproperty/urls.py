"""
URL configuration for olympusproperty project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic.base import RedirectView
#from dashboard import views as dashboard_views

urlpatterns = []

if getattr(settings, 'ENABLE_AZURE_SSO', False):
    urlpatterns.append(
        path(
            'accounts/login/',
            RedirectView.as_view(pattern_name='django_auth_adfs:login', permanent=False),
            name='login',
        )
    )

urlpatterns += [
    path('admin/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),
    path('', include('dashboard.urls')),
    # Alias so /dashboard/sample/ also works (maps to sample_page)
    #path('dashboard/sample/', dashboard_views.sample_page, name='dashboard_sample'),
]

if getattr(settings, 'ENABLE_AZURE_SSO', False):
    urlpatterns.append(path('auth/', include('django_auth_adfs.urls')))

# Serve static files in development
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT or settings.BASE_DIR / 'static')
