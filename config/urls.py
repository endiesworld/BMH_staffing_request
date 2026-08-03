"""
URL configuration for config project.

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
from django.urls import include, path

from . import views

# Note: /healthz/live and /healthz/ready are deliberately NOT routed here.
# HealthCheckMiddleware answers them ahead of Host-header validation, which is
# what keeps kubelet probes (addressed to the pod IP) from 400ing. See
# config/health.py.
urlpatterns = [
    path('admin/', admin.site.urls),
    # Django's built-in auth views: login, logout, password change/reset.
    # Gives us the url names 'login' and 'logout' that base.html references.
    # Ours first: both mount under accounts/, and the first match wins.
    path('accounts/', include('accounts.urls')),
    path('accounts/', include('django.contrib.auth.urls')),
    path('requests/', include('servicing.urls')),
    # Front door: dispatches to whichever page suits the user's role, and to
    # the login page if they are not signed in.
    path('', views.home, name='home'),
]
