from django.contrib import messages
from django.contrib.auth import login
from django.shortcuts import redirect, render

from . import services
from .forms import ClientRegistrationForm

# create_client() builds the user directly rather than going through
# authenticate(), so login() cannot infer which backend verified them and needs
# telling. Only one backend is configured.
AUTH_BACKEND = "django.contrib.auth.backends.ModelBackend"


def register_client(request):
    """Client self-registration (ADR-009: clients and personnel self-register).

    The form validates; accounts.services.create_client() writes the User and
    the ClientProfile together in one transaction, so a half-registered client
    cannot exist.
    """
    if request.user.is_authenticated:
        # Already signed in -- nothing to register.
        return redirect("servicing:my_requests")

    if request.method == "POST":
        form = ClientRegistrationForm(data=request.POST)

        if form.is_valid():
            user = services.create_client(
                email=form.cleaned_data["email"],
                password=form.cleaned_data["password1"],
                organization_name=form.cleaned_data["organization_name"],
                phone_number=form.cleaned_data["phone_number"],
            )
            # Straight in, rather than bouncing them to a login form they have
            # just proved they can pass.
            login(request, user, backend=AUTH_BACKEND)
            messages.success(
                request,
                f"Welcome, {user.email}. You can raise your first request now.",
            )
            return redirect("servicing:my_requests")
    else:
        form = ClientRegistrationForm()

    return render(request, "accounts/register.html", {"form": form})
