from django.conf import settings
from django.db import models

from accounts.models import PersonnelProfile


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
    title = models.CharField(max_length=200)
    description = models.TextField()
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
        return f"{self.title} ({self.get_status_display()})"
