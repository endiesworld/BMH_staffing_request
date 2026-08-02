from django import forms
from django.contrib.auth.forms import UserCreationForm

from .models import User
from .validators import normalize_us_phone


class ClientRegistrationForm(UserCreationForm):
    """Self-registration for clients (ADR-009: clients self-register).

    Spans two models -- it collects the User fields *and* the ClientProfile
    fields -- so the two are created together by accounts.services.create_client
    inside one transaction. The form only validates; it never writes.

    Subclassing UserCreationForm rather than starting from forms.Form buys the
    password1/password2 confirmation and AUTH_PASSWORD_VALIDATORS enforcement,
    both of which are easy to get subtly wrong by hand.
    """

    organization_name = forms.CharField(
        max_length=255,
        label="Organisation name",
        widget=forms.TextInput(attrs={"placeholder": "Acme Care Services"}),
    )
    # Wider than ClientProfile.phone_number needs, because this limit applies to
    # what is typed, not what is stored -- "(555) 123-4567" is 14 characters
    # before clean_phone_number strips the formatting.
    phone_number = forms.CharField(
        max_length=20,
        label="Phone number",
        widget=forms.TextInput(
            attrs={"placeholder": "(555) 123-4567", "autocomplete": "tel"}
        ),
    )

    class Meta(UserCreationForm.Meta):
        model = User
        # Not "username": USERNAME_FIELD is email (ADR-006).
        fields = ("email",)

    # Declared fields would otherwise land after the password boxes.
    field_order = ("email", "organization_name", "phone_number", "password1", "password2")

    def clean_phone_number(self):
        try:
            return normalize_us_phone(self.cleaned_data["phone_number"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc

    def save(self, commit=True):
        # UserCreationForm.save() would create the User alone, leaving a client
        # with no ClientProfile -- the exact invariant ADR-009 makes
        # create_client() responsible for.
        raise NotImplementedError(
            "Use accounts.services.create_client() with this form's cleaned_data "
            "instead of ClientRegistrationForm.save()."
        )
