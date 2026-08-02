from django.urls import path

from . import views

# Namespaced so reverse() calls read as servicing:my_requests and cannot
# collide with a same-named url in another app.
app_name = "servicing"

urlpatterns = [
    path("", views.my_requests, name="my_requests"),
    path("new/", views.submit_request, name="submit_request"),
]
