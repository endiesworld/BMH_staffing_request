from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from . import services
from .decorators import role_required
from .forms import AvailabilityForm, ClientRegistrationForm, PersonnelRegistrationForm
from .models import User

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
        # Already signed in -- nothing to register. `home` works out where they
        # belong; hard-coding a page here would 403 the wrong role.
        return redirect("home")

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
            return redirect("home")
    else:
        form = ClientRegistrationForm()

    return render(
        request,
        "accounts/register.html",
        {"form": form, "heading": "Create a client account"},
    )


def register_personnel(request):
    """Personnel self-registration (ADR-009).

    They land on the availability page rather than a request list, because a
    new registration is UNAVAILABLE and therefore not yet assignable -- opting
    in is the next thing they need to do.
    """
    if request.user.is_authenticated:
        return redirect("home")

    if request.method == "POST":
        form = PersonnelRegistrationForm(data=request.POST)

        if form.is_valid():
            user = services.create_personnel(
                email=form.cleaned_data["email"],
                password=form.cleaned_data["password1"],
                sector=form.cleaned_data["sector"],
            )
            login(request, user, backend=AUTH_BACKEND)
            messages.success(
                request,
                "Account created. You are marked unavailable, so you will not "
                "be assigned anything until you say you are available.",
            )
            return redirect("accounts:availability")
    else:
        form = PersonnelRegistrationForm()

    return render(
        request,
        "accounts/register.html",
        {"form": form, "heading": "Create a personnel account"},
    )


@login_required
@role_required(User.Role.PERSONNEL, "Only personnel have an availability status.")
def availability(request):
    """The opt-in that decides whether eligibility can see this person at all."""
    profile = request.user.personnel_profile

    if request.method == "POST":
        form = AvailabilityForm(data=request.POST, instance=profile)
        if form.is_valid():
            # A plain profile field, no workflow attached -- unlike a
            # ServiceRequest status, this has no transition rules to enforce,
            # so the ModelForm may save it directly.
            form.save()
            messages.success(
                request, f"Availability updated to {profile.get_availability_status_display()}."
            )
            return redirect("accounts:availability")
    else:
        form = AvailabilityForm(instance=profile)

    return render(
        request,
        "accounts/availability.html",
        {"form": form, "profile": profile},
    )
