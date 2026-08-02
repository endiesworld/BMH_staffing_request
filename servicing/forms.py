from datetime import timedelta

from django import forms
from django.utils import timezone

from accounts.validators import normalize_us_phone

from .models import RequestType, ServiceRequest

# Offered as a dropdown rather than a raw DurationField: Django's duration
# widget expects "HH:MM:SS", which no client should have to know.
DURATION_CHOICES = [
    ("", "Choose a duration"),
    (30, "30 minutes"),
    (60, "1 hour"),
    (120, "2 hours"),
    (240, "Half a day (4 hours)"),
    (480, "Full day (8 hours)"),
]


class ServiceRequestForm(forms.ModelForm):
    """What a client fills in when raising a request.

    `fields` lists only what the client supplies. status, reviewed_by,
    reviewed_at and rejection_reason are absent by construction, so no crafted
    POST can set them -- a field the form does not declare cannot be submitted.
    That is a stronger guarantee than validating them away afterwards.
    """

    # Declared here, so it replaces the DurationField the ModelForm would have
    # generated. coerce turns the submitted "120" into timedelta(minutes=120),
    # so cleaned_data still holds what the model column expects.
    expected_duration = forms.TypedChoiceField(
        label="How long will it take?",
        choices=DURATION_CHOICES,
        coerce=lambda minutes: timedelta(minutes=int(minutes)),
    )

    # Declared so the form accepts punctuation the model column has no room for.
    # The model's max_length=12 fits the stored "+15551234567" exactly, and
    # ModelForm would copy that limit onto the input -- where it would reject
    # "(555) 123-4567" (14 characters) before clean_contact_phone ever strips
    # the formatting. The wider limit applies to what is typed, not what is
    # stored.
    contact_phone = forms.CharField(
        label="Contact phone number",
        max_length=20,
        widget=forms.TextInput(
            attrs={
                "placeholder": "(555) 123-4567",
                "autocomplete": "tel",
                "inputmode": "tel",
            }
        ),
    )

    class Meta:
        model = ServiceRequest
        fields = (
            "request_type",
            "scheduled_start",
            "expected_duration",
            "address_line1",
            "address_line2",
            "city",
            "state",
            "postal_code",
            "contact_phone",
            "description",
        )
        # `country` is deliberately absent: only one value is offered, so the
        # model default fills it and the client is not shown a pointless menu.
        labels = {
            "request_type": "What kind of help do you need?",
            "scheduled_start": "When do you need it?",
            "address_line1": "Street address",
            "address_line2": "Apartment, suite, etc. (optional)",
            "postal_code": "ZIP code",
            "description": "Tell us what you need",
        }
        widgets = {
            "scheduled_start": forms.DateTimeInput(
                # datetime-local gives a native date+time picker. The format
                # must match what that input type emits and expects.
                attrs={"type": "datetime-local"},
                format="%Y-%m-%dT%H:%M",
            ),
            # autocomplete tokens let browsers fill a saved address in one tap.
            "address_line1": forms.TextInput(
                attrs={"placeholder": "1600 Pennsylvania Ave NW",
                       "autocomplete": "address-line1"}
            ),
            "address_line2": forms.TextInput(
                attrs={"placeholder": "Apt 4B", "autocomplete": "address-line2"}
            ),
            "city": forms.TextInput(
                attrs={"placeholder": "Washington", "autocomplete": "address-level2"}
            ),
            "postal_code": forms.TextInput(
                attrs={"placeholder": "20500", "autocomplete": "postal-code",
                       "inputmode": "numeric"}
            ),
            "description": forms.Textarea(
                attrs={
                    "rows": 5,
                    "placeholder": "Anything the assigned person should know.",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Retired types must not be offerable. The authoritative check is still
        # the one in submit_request() -- this is the UI half, so a client never
        # sees an option that would be refused on submit.
        self.fields["request_type"].queryset = RequestType.objects.filter(
            is_active=True
        )
        self.fields["request_type"].empty_label = "Choose a service"
        # ModelChoiceField re-runs that queryset when cleaning, so a type
        # retired mid-session lands here. The stock wording ("Select a valid
        # choice") reads like the client did something wrong.
        self.fields["request_type"].error_messages["invalid_choice"] = (
            "That service is no longer offered. Please choose another."
        )
        # A datetime-local input only submits this format, so it must be the
        # one the field parses.
        self.fields["scheduled_start"].input_formats = [
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M",
        ]
        # Prefill a valid, immediately workable slot so raising a request is a
        # few clicks rather than a typing exercise. Unbound forms only -- never
        # overwrite what someone just submitted.
        if not self.is_bound:
            self.fields["scheduled_start"].initial = timezone.now() + timedelta(
                minutes=5
            )

    def clean_scheduled_start(self):
        """From now onwards. A slot in the past cannot be worked.

        Compared against the start of the CURRENT MINUTE, not the exact instant:
        a datetime-local input only has minute precision, so picking the current
        minute and taking ten seconds to submit must not be rejected as "past".
        """
        scheduled_start = self.cleaned_data["scheduled_start"]
        this_minute = timezone.now().replace(second=0, microsecond=0)

        if scheduled_start < this_minute:
            raise forms.ValidationError(
                "Choose a time from now onwards -- a past slot cannot be worked."
            )
        return scheduled_start

    def clean_contact_phone(self):
        """Accept whatever the client types; store one canonical shape.

        Shared with client registration so the system only ever holds one phone
        format. servicing -> accounts is the allowed direction (ADR-007).
        """
        try:
            return normalize_us_phone(self.cleaned_data["contact_phone"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc

    def save(self, commit=True):
        # A ModelForm would happily write the row directly, skipping the
        # is_active check and the history record. Creation goes through
        # servicing.services.submit_request() only (ADR-010 D3).
        raise NotImplementedError(
            "Use servicing.services.submit_request() with this form's "
            "cleaned_data instead of ServiceRequestForm.save()."
        )
