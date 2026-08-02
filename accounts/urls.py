from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("register/", views.register_client, name="register_client"),
]
