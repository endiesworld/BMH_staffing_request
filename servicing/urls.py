from django.urls import path

from . import views

# Namespaced so reverse() calls read as servicing:my_requests and cannot
# collide with a same-named url in another app.
app_name = "servicing"

urlpatterns = [
    path("", views.my_requests, name="my_requests"),
    path("new/", views.submit_request, name="submit_request"),
    path("assignments/", views.my_assignments, name="my_assignments"),
    # POST-only: accepting is a state change, so it must not be reachable by a
    # link, a prefetch or an <img> tag.
    path(
        "assignments/<int:pk>/accept/",
        views.accept_assignment,
        name="accept_assignment",
    ),
    path(
        "assignments/<int:pk>/decline/",
        views.decline_assignment,
        name="decline_assignment",
    ),
    path(
        "assignments/<int:pk>/start/",
        views.start_assignment,
        name="start_assignment",
    ),
    path(
        "assignments/<int:pk>/complete/",
        views.fulfil_assignment,
        name="fulfil_assignment",
    ),
]
