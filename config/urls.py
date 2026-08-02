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
from django.views.generic import RedirectView

urlpatterns = [
    path('admin/', admin.site.urls),
    # Django's built-in auth views: login, logout, password change/reset.
    # Gives us the url names 'login' and 'logout' that base.html references.
    # Ours first: both mount under accounts/, and the first match wins.
    path('accounts/', include('accounts.urls')),
    path('accounts/', include('django.contrib.auth.urls')),
    path('requests/', include('servicing.urls')),
    # Landing on the site drops a client straight into their own requests
    # (and, if they are not logged in, into the login page).
    path('', RedirectView.as_view(pattern_name='servicing:my_requests')),
]
