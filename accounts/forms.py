from django import forms
from django.contrib.auth.forms import UserCreationForm

from .models import PersonnelProfile, User
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


class PersonnelRegistrationForm(UserCreationForm):
    """Self-registration for personnel (ADR-009: personnel self-register).

    Sector is collected here because it is what makes someone eligible for a
    request: eligibility matches PersonnelProfile.sector against
    RequestType.required_sector.

    Availability is deliberately NOT collected. It defaults to UNAVAILABLE
    (ADR-009 D3), so a newly registered person is not assignable until they
    opt in -- registering is not the same as being ready to work.
    """

    sector = forms.ChoiceField(
        choices=PersonnelProfile.SectorCategory.choices,
        label="Which sector do you work in?",
        help_text="This decides which kinds of request you can be assigned.",
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("email",)

    field_order = ("email", "sector", "password1", "password2")

    def save(self, commit=True):
        # Would create the User alone, leaving personnel with no
        # PersonnelProfile -- the ADR-009 invariant create_personnel() owns.
        raise NotImplementedError(
            "Use accounts.services.create_personnel() with this form's "
            "cleaned_data instead of PersonnelRegistrationForm.save()."
        )


class AvailabilityForm(forms.ModelForm):
    """The opt-in that makes a personnel member assignable."""

    class Meta:
        model = PersonnelProfile
        fields = ("availability_status",)
        labels = {"availability_status": "Your current availability"}
