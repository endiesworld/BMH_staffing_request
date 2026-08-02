from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models

from accounts.models import PersonnelProfile


class USState(models.TextChoices):
    """US states + DC. Service is US-only for now (see ServiceRequest.country)."""

    AL = "AL", "Alabama"
    AK = "AK", "Alaska"
    AZ = "AZ", "Arizona"
    AR = "AR", "Arkansas"
    CA = "CA", "California"
    CO = "CO", "Colorado"
    CT = "CT", "Connecticut"
    DE = "DE", "Delaware"
    DC = "DC", "District of Columbia"
    FL = "FL", "Florida"
    GA = "GA", "Georgia"
    HI = "HI", "Hawaii"
    ID = "ID", "Idaho"
    IL = "IL", "Illinois"
    IN = "IN", "Indiana"
    IA = "IA", "Iowa"
    KS = "KS", "Kansas"
    KY = "KY", "Kentucky"
    LA = "LA", "Louisiana"
    ME = "ME", "Maine"
    MD = "MD", "Maryland"
    MA = "MA", "Massachusetts"
    MI = "MI", "Michigan"
    MN = "MN", "Minnesota"
    MS = "MS", "Mississippi"
    MO = "MO", "Missouri"
    MT = "MT", "Montana"
    NE = "NE", "Nebraska"
    NV = "NV", "Nevada"
    NH = "NH", "New Hampshire"
    NJ = "NJ", "New Jersey"
    NM = "NM", "New Mexico"
    NY = "NY", "New York"
    NC = "NC", "North Carolina"
    ND = "ND", "North Dakota"
    OH = "OH", "Ohio"
    OK = "OK", "Oklahoma"
    OR = "OR", "Oregon"
    PA = "PA", "Pennsylvania"
    RI = "RI", "Rhode Island"
    SC = "SC", "South Carolina"
    SD = "SD", "South Dakota"
    TN = "TN", "Tennessee"
    TX = "TX", "Texas"
    UT = "UT", "Utah"
    VT = "VT", "Vermont"
    VA = "VA", "Virginia"
    WA = "WA", "Washington"
    WV = "WV", "West Virginia"
    WI = "WI", "Wisconsin"
    WY = "WY", "Wyoming"


# 12345 or 12345-6789. Enforced on the model so it holds for every writer,
# not only the client form.
ZIP_CODE_VALIDATOR = RegexValidator(
    regex=r"^\d{5}(-\d{4})?$",
    message="Enter a ZIP code as 12345 or 12345-6789.",
)

# Stored normalised as +1 followed by ten digits; the form does the
# normalising, so anything a client types is accepted and cleaned up.
PHONE_VALIDATOR = RegexValidator(
    regex=r"^\+1\d{10}$",
    message="Enter a 10-digit US phone number.",
)


class RequestType(models.Model):
    """The kind of help a client is asking for (e.g. "Home Nursing Visit").

    A reference table rather than a TextChoices enum (ADR-010 D1): coordinator
    routing needs a many-to-many against it, and eligibility needs data attached
    to each type -- neither of which an enum can carry.
    """

    # Stable machine identifier -- what code and external integrations refer to.
    # Set once at creation and never re-worded, so renaming the display label
    # can never break a payload contract or a lookup.
    code = models.SlugField(max_length=50, unique=True)
    # Display label, freely editable. Kept unique so two types can't appear
    # under the same name in a dropdown.
    name = models.CharField(max_length=100, unique=True)
    # The sector a personnel member must belong to in order to fulfil this type.
    # Deliberately reuses the accounts vocabulary: eligibility compares this
    # against PersonnelProfile.sector, so the two must never drift apart.
    required_sector = models.CharField(
        max_length=20,
        choices=PersonnelProfile.SectorCategory.choices,
        blank=False,
        null=False,
    )
    # Retirement flag, not a soft delete of history: ServiceRequest.request_type
    # is PROTECTed (ADR-010 D5), so a type in use can never be deleted. This is
    # how a type stops being offered without breaking existing requests.
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ServiceRequest(models.Model):
    """A single request for help, raised by a client and worked through the
    lifecycle in brief section 6.

    `status` is the read model -- the current position in the state machine,
    indexed because it is what coordinator dashboards filter on. The history of
    how it got there lives in the append-only audit log (ADR-010 D2). Nothing
    outside servicing/services.py may write to it (ADR-010 D3).
    """

    class Status(models.TextChoices):
        SUBMITTED = "SUBMITTED", "Submitted"
        REJECTED = "REJECTED", "Rejected"
        READY_FOR_ASSIGNMENT = "READY_FOR_ASSIGNMENT", "Ready for assignment"
        ASSIGNED = "ASSIGNED", "Assigned"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        FULFILLED = "FULFILLED", "Fulfilled"

    # PROTECT, not CASCADE: deleting a client must never silently erase their
    # service history (ADR-010 D5). De-identifying a person is a separate,
    # deliberate operation.
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="service_requests",
    )
    request_type = models.ForeignKey(
        RequestType,
        on_delete=models.PROTECT,
        related_name="service_requests",
    )
    # --- What the client is asking for, and when ------------------------
    # ADR-011 D5: the client supplies the slot at submission. fulfil_request()
    # will only accept a completion between scheduled_start and
    # scheduled_start + expected_duration (+ grace), once Assignment exists.
    scheduled_start = models.DateTimeField()
    expected_duration = models.DurationField()
    description = models.TextField()

    # --- Where the work happens -----------------------------------------
    # Structured rather than one free-text line: the address is what personnel
    # travel to, and "state" will drive coordinator routing by region later.
    # Held on the request, not the client, because a client can raise requests
    # for different sites.
    address_line1 = models.CharField("address", max_length=255)
    address_line2 = models.CharField(
        "apartment, suite, etc.", max_length=255, blank=True
    )
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2, choices=USState.choices)
    postal_code = models.CharField(
        "ZIP code", max_length=10, validators=[ZIP_CODE_VALIDATOR]
    )
    # Stored though only one value is offered, so opening up to other countries
    # later is a choices change rather than a backfill of existing rows.
    country = models.CharField(
        max_length=2, choices=[("US", "United States")], default="US"
    )
    contact_phone = models.CharField(max_length=12, validators=[PHONE_VALIDATOR])
    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.SUBMITTED,
        db_index=True,
    )

    # --- Review outcome (ADR-010 D4) -------------------------------------
    # Inline rather than a Review model: no transition returns to SUBMITTED, so
    # a request is reviewed exactly 0 or 1 times. These three are unset together
    # and set together; unit 3 adds the CheckConstraint that enforces it.
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reviewed_requests",
        null=True,
        blank=True,
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        # Status values appear as literals below rather than as Status.SUBMITTED:
        # a nested class body cannot see the enclosing class's namespace, so the
        # symbolic reference would raise NameError at import time.
        constraints = [
            # The review fields are unset together and set together. SUBMITTED is
            # the only pre-review state (no transition returns to it, ADR-010 D4),
            # so "has been reviewed" is exactly "status is not SUBMITTED".
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="SUBMITTED",
                        reviewed_by__isnull=True,
                        reviewed_at__isnull=True,
                    )
                    | (
                        ~models.Q(status="SUBMITTED")
                        & models.Q(
                            reviewed_by__isnull=False,
                            reviewed_at__isnull=False,
                        )
                    )
                ),
                name="servicerequest_review_fields_match_status",
                violation_error_message=(
                    "reviewed_by and reviewed_at must be set if and only if the "
                    "request has left SUBMITTED."
                ),
            ),
            # A rejection must be explainable to the client, and nothing else may
            # carry a rejection reason. REJECTED is terminal, so a request can
            # never move out of it and leave a stale reason behind.
            models.CheckConstraint(
                condition=(
                    (models.Q(status="REJECTED") & ~models.Q(rejection_reason=""))
                    | (~models.Q(status="REJECTED") & models.Q(rejection_reason=""))
                ),
                name="servicerequest_rejection_reason_only_when_rejected",
                violation_error_message=(
                    "rejection_reason must be non-empty if and only if the "
                    "request is REJECTED."
                ),
            ),
        ]

    def __str__(self):
        return (
            f"{self.request_type} on "
            f"{self.scheduled_start:%d %b %Y, %H:%M} "
            f"({self.get_status_display()})"
        )

    @property
    def location_display(self):
        """One-line address for tables and the admin."""
        street = ", ".join(filter(None, [self.address_line1, self.address_line2]))
        return f"{street}, {self.city}, {self.state} {self.postal_code}"

    @property
    def contact_phone_display(self):
        """+15551234567 -> (555) 123-4567."""
        digits = self.contact_phone.removeprefix("+1")
        if len(digits) != 10:
            return self.contact_phone
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"

    @property
    def duration_display(self):
        """"2 hours" rather than "2:00:00", for templates and the admin."""
        minutes = int(self.expected_duration.total_seconds() // 60)
        hours, remainder = divmod(minutes, 60)

        parts = []
        if hours:
            parts.append(f"{hours} hour{'' if hours == 1 else 's'}")
        if remainder:
            parts.append(f"{remainder} minutes")
        return " ".join(parts) or "0 minutes"
