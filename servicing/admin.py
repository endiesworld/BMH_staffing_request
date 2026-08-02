from django.contrib import admin, messages

from . import services
from .models import RequestType, ServiceRequest


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
        "reviewed_by",
    )
    list_filter = ("status", "request_type")
    search_fields = ("city", "postal_code", "description", "client__email")
    date_hierarchy = "scheduled_start"
    ordering = ("-created_at",)
    actions = ("approve_requests",)

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
                    f"{service_request.title}: {exc}",
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


admin.site.register(RequestType, RequestTypeAdmin)
admin.site.register(ServiceRequest, ServiceRequestAdmin)
