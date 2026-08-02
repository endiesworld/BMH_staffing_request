import logging

from django.db import transaction
from django.utils import timezone

from accounts.models import User
from .models import RequestType, ServiceRequest

logger = logging.getLogger(__name__)

Status = ServiceRequest.Status

# The brief section 6 state machine, encoded so it can be enforced rather than
# just documented. Every status write in this module is checked against it.
TRANSITIONS = {
    Status.SUBMITTED: {Status.REJECTED, Status.READY_FOR_ASSIGNMENT},
    Status.READY_FOR_ASSIGNMENT: {Status.ASSIGNED},
    # A declining personnel member returns the request to the assignable pool.
    Status.ASSIGNED: {Status.IN_PROGRESS, Status.READY_FOR_ASSIGNMENT},
    Status.IN_PROGRESS: {Status.FULFILLED},
    Status.REJECTED: set(),
    Status.FULFILLED: set(),
}


class IllegalTransition(Exception):
    """Raised when a caller asks for a status change the state machine forbids."""

    def __init__(self, from_status: str, to_status: str):
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"Cannot move a request from {from_status} to {to_status}.")


def _record_transition(service_request: ServiceRequest, from_status: str, to_status: str, actor: User):
    """Write the append-only history entry for a transition (ADR-010 D2).

    TODO: replace the log line with an AuditEvent row once the audit app exists.
    Called inside the caller's transaction.atomic() block, so the history entry
    and the status change commit together or not at all.

    Until then transitions are emitted to the application log, so history is
    degraded rather than silently discarded.
    """
    logger.info(
        "servicerequest=%s %s -> %s actor=%s",
        service_request.pk,
        from_status,
        to_status,
        actor.pk,
    )


def _transition(service_request: ServiceRequest, to_status: str, actor: User, **updates):
    """Move a request to `to_status`, applying `updates` in the same write.

    Must be called inside transaction.atomic(). Re-reads the row under
    select_for_update() so that two coordinators acting at the same moment
    cannot both pass the transition check against the same stale status
    (ADR-010 D3). Note this lock is a no-op on SQLite and only becomes real
    on Postgres.
    """
    locked = ServiceRequest.objects.select_for_update().get(pk=service_request.pk)

    if to_status not in TRANSITIONS[locked.status]:
        raise IllegalTransition(locked.status, to_status)

    from_status = locked.status
    locked.status = to_status
    for field, value in updates.items():
        setattr(locked, field, value)
    locked.save(update_fields=[*updates, "status", "updated_at"])

    _record_transition(locked, from_status, to_status, actor)
    return locked


def submit_request(client: User, request_type: RequestType, title: str, description: str):
    """Create a new request in SUBMITTED, awaiting coordinator review.

    Creation rather than a transition -- there is no prior state to check.
    """
    if not request_type.is_active:
        raise ValueError(f"Request type '{request_type.code}' is no longer offered.")

    with transaction.atomic():
        service_request = ServiceRequest.objects.create(
            client=client,
            request_type=request_type,
            title=title,
            description=description,
        )
        _record_transition(service_request, "", Status.SUBMITTED, client)

    return service_request


def approve_request(service_request: ServiceRequest, coordinator: User):
    """Coordinator approves a request, releasing it into the assignable pool."""
    with transaction.atomic():
        return _transition(
            service_request,
            Status.READY_FOR_ASSIGNMENT,
            actor=coordinator,
            reviewed_by=coordinator,
            reviewed_at=timezone.now(),
        )


def reject_request(service_request: ServiceRequest, coordinator: User, rejection_reason: str):
    """Coordinator rejects a request. Terminal -- there is no path back."""
    # Checked here as well as by the DB constraint: the constraint is the last
    # line of defence and raises IntegrityError, which is no use to a caller
    # that wants to show the coordinator a readable message.
    if not rejection_reason.strip():
        raise ValueError("A rejection must include a reason.")

    with transaction.atomic():
        return _transition(
            service_request,
            Status.REJECTED,
            actor=coordinator,
            reviewed_by=coordinator,
            reviewed_at=timezone.now(),
            rejection_reason=rejection_reason,
        )
