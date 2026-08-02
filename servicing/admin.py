from django.contrib import admin, messages
from django.db.models import Prefetch
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.template.response import TemplateResponse

from . import services
from .models import Assignment, RequestType, ServiceRequest
from .recommendation import recommend_personnel


# --- Request types: the one thing here that is meant to be edited by hand ---

class RequestTypeAdmin(admin.ModelAdmin):
    """The vocabulary. Seeded once by migration 0002, owned by admins after that."""

    list_display = ("name", "code", "required_sector", "is_active")
    list_filter = ("required_sector", "is_active")
    search_fields = ("name", "code")
    ordering = ("name",)
    prepopulated_fields = {"code": ("name",)}

    def get_readonly_fields(self, request, obj=None):
        # `code` is the stable identity (what external payloads and any future
        # lookup use). Settable when creating, immutable afterwards -- renaming
        # the display label must never change what identifies the row.
        return ("code",) if obj else ()

    def get_prepopulated_fields(self, request, obj=None):
        # Slug from the name on the ADD form only. Prepopulating on edit would
        # re-slug on every rename, which is exactly what `code` exists to avoid.
        return self.prepopulated_fields if obj is None else {}


# --- Assignments: who a request went to, and what they said ---

LIVE_ASSIGNMENT_STATUSES = (Assignment.Status.PENDING, Assignment.Status.ACCEPTED)

ASSIGNMENT_FIELDS = (
    "personnel",
    "status",
    "assigned_by",
    "created_at",
    "responded_at",
)


class AssignmentInline(admin.TabularInline):
    """The full attempt history on the request's own page, declines included.

    Read-only: accepting and declining are the personnel's transitions, made
    through servicing.services, never typed in here.
    """

    model = Assignment
    extra = 0
    can_delete = False
    readonly_fields = ASSIGNMENT_FIELDS
    fields = ASSIGNMENT_FIELDS

    def has_add_permission(self, request, obj=None):
        # Assigning goes through the changelist action, which enforces
        # eligibility and moves the request's status with it.
        return False


class AssignmentAdmin(admin.ModelAdmin):
    """Standalone view: every assignment across all requests."""

    list_display = (
        "service_request",
        "personnel",
        "status",
        "assigned_by",
        "created_at",
        "responded_at",
    )
    list_filter = ("status",)
    search_fields = ("personnel__email", "service_request__city")
    date_hierarchy = "created_at"

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# --- Service requests: a read-only record, changed only through transitions ---

class ServiceRequestAdmin(admin.ModelAdmin):
    """Deliberately not editable.

    Every status change must go through servicing.services so the transition
    map, the locking and the history write all apply (ADR-010 D3). A free-text
    status dropdown in the admin would bypass all three, so the fields are
    read-only and the legal moves are offered as actions instead.
    """

    list_display = (
        "request_type",
        "client",
        "scheduled_start",
        "location_display",
        "status",
        "assigned_to",
        "reviewed_by",
    )
    list_filter = ("status", "request_type")
    search_fields = ("city", "postal_code", "description", "client__email")
    date_hierarchy = "scheduled_start"
    ordering = ("-created_at",)
    actions = ("approve_requests", "assign_personnel")
    inlines = (AssignmentInline,)

    def get_queryset(self, request):
        # Prefetched so the assigned_to column costs one extra query for the
        # whole page rather than one per row.
        return (
            super()
            .get_queryset(request)
            .select_related("client", "request_type", "reviewed_by")
            .prefetch_related(
                Prefetch(
                    "assignments",
                    queryset=Assignment.objects.select_related("personnel"),
                )
            )
        )

    @admin.display(description="Assigned to")
    def assigned_to(self, obj):
        """Who currently holds this request, if anyone.

        Filtered in Python rather than with another query, so it uses the rows
        already prefetched above.
        """
        live = [
            assignment
            for assignment in obj.assignments.all()
            if assignment.status in LIVE_ASSIGNMENT_STATUSES
        ]
        if not live:
            return "—"

        assignment = live[0]
        return f"{assignment.personnel.email} ({assignment.get_status_display()})"

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]

    def has_add_permission(self, request):
        # Requests are raised by clients through submit_request(), which checks
        # the request type is still offered and records the creation. An admin
        # "add" form would skip both.
        return False

    def has_delete_permission(self, request, obj=None):
        # Service history is not deletable (the same stance as PROTECT on the
        # client FK, ADR-010 D5).
        return False

    @admin.action(
        description="Approve selected requests (release for assignment)",
        # Approving is a change. Without this the action would be offered to
        # anyone who can merely view the list.
        permissions=["change"],
    )
    def approve_requests(self, request, queryset):
        approved = 0
        for service_request in queryset:
            try:
                services.approve_request(service_request, request.user)
            except services.IllegalTransition as exc:
                self.message_user(
                    request,
                    f"{service_request}: {exc}",
                    level=messages.WARNING,
                )
            else:
                approved += 1

        if approved:
            self.message_user(
                request,
                f"{approved} request(s) approved and released for assignment.",
                level=messages.SUCCESS,
            )


    @admin.action(
        description="Assign personnel to the selected request",
        permissions=["change"],
    )
    def assign_personnel(self, request, queryset):
        """Two-step action: pick a candidate, then assign.

        Django actions get a queryset, not a form, so anything needing extra
        input renders an intermediate page and posts back to the same action.
        The `apply` flag distinguishes the two passes.
        """
        # Candidates differ per request, so this only makes sense one at a time.
        if queryset.count() != 1:
            self.message_user(
                request,
                "Select exactly one request to assign.",
                level=messages.WARNING,
            )
            return None

        service_request = queryset.get()

        if request.POST.get("apply"):
            personnel_id = request.POST.get("personnel")
            candidates = recommend_personnel(service_request)
            profile = candidates.filter(user_id=personnel_id).first()

            if profile is None:
                # Eligibility is re-checked here, not trusted from the page the
                # coordinator was looking at: availability can change between
                # rendering the list and choosing from it.
                self.message_user(
                    request,
                    "That person is no longer eligible. Try again.",
                    level=messages.WARNING,
                )
                return None

            try:
                services.assign_request(service_request, profile.user, request.user)
            except (services.IllegalTransition, ValueError) as exc:
                self.message_user(request, str(exc), level=messages.WARNING)
            else:
                self.message_user(
                    request,
                    f"Assigned to {profile.user.email}, awaiting their response.",
                    level=messages.SUCCESS,
                )
            return None

        if service_request.status != ServiceRequest.Status.READY_FOR_ASSIGNMENT:
            self.message_user(
                request,
                f"Only approved requests can be assigned. This one is "
                f"{service_request.get_status_display()}.",
                level=messages.WARNING,
            )
            return None

        return TemplateResponse(
            request,
            "admin/servicing/assign_personnel.html",
            {
                **self.admin_site.each_context(request),
                "title": "Assign personnel",
                "service_request": service_request,
                "candidates": recommend_personnel(service_request),
                "declined": service_request.assignments.filter(
                    status=Assignment.Status.DECLINED
                ).select_related("personnel"),
                "action_checkbox_name": ACTION_CHECKBOX_NAME,
            },
        )


admin.site.register(Assignment, AssignmentAdmin)
admin.site.register(RequestType, RequestTypeAdmin)
admin.site.register(ServiceRequest, ServiceRequestAdmin)
