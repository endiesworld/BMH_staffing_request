from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("register/", views.register_client, name="register_client"),
    path("register/personnel/", views.register_personnel, name="register_personnel"),
    path("availability/", views.availability, name="availability"),
]
