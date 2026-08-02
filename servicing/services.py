import logging

from django.db import transaction
from django.utils import timezone

from accounts.models import User
from .models import Assignment, RequestType, ServiceRequest
from .recommendation import recommend_personnel
from . import tasks

logger = logging.getLogger(__name__)

Status = ServiceRequest.Status
AssignmentStatus = Assignment.Status

# The brief section 6 state machine, encoded so it can be enforced rather than
# just documented. Every status write in this module is checked against it.
TRANSITIONS = {
    Status.SUBMITTED: {Status.REJECTED, Status.READY_FOR_ASSIGNMENT},
    Status.READY_FOR_ASSIGNMENT: {Status.ASSIGNED},
    # A declining personnel member returns the request to the assignable pool.
    Status.ASSIGNED: {Status.SCHEDULED, Status.READY_FOR_ASSIGNMENT},
    # Accepted and booked in, but nobody has commenced yet.
    Status.SCHEDULED: {Status.IN_PROGRESS},
    Status.IN_PROGRESS: {Status.FULFILLED},
    Status.REJECTED: set(),
    Status.FULFILLED: set(),
}


# The SECOND state machine (brief section 3, insight 1). Kept separate from
# TRANSITIONS on purpose: an Assignment's lifecycle is not a ServiceRequest's,
# and conflating them is the mistake the brief calls out.
ASSIGNMENT_TRANSITIONS = {
    AssignmentStatus.PENDING: {AssignmentStatus.ACCEPTED, AssignmentStatus.DECLINED},
    AssignmentStatus.ACCEPTED: set(),
    AssignmentStatus.DECLINED: set(),
}


class IllegalTransition(Exception):
    """Raised when a caller asks for a status change a state machine forbids."""

    def __init__(self, from_status: str, to_status: str, subject: str = "request"):
        self.from_status = from_status
        self.to_status = to_status
        self.subject = subject
        super().__init__(
            f"Cannot move a {subject} from {from_status} to {to_status}."
        )


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


def submit_request(
    client: User,
    request_type: RequestType,
    *,
    scheduled_start,
    expected_duration,
    description: str,
    address_line1: str,
    city: str,
    state: str,
    postal_code: str,
    contact_phone: str,
    address_line2: str = "",
):
    """Create a new request in SUBMITTED, awaiting coordinator review.

    Creation rather than a transition -- there is no prior state to check.
    `scheduled_start` is a datetime and `expected_duration` a timedelta; together
    they are the window starting and completing are checked against (ADR-011 D5).

    Keyword-only after request_type: with this many fields, positional calls
    would be a silent way to swap city and state.
    """
    if not request_type.is_active:
        raise ValueError(f"Request type '{request_type.code}' is no longer offered.")

    with transaction.atomic():
        service_request = ServiceRequest.objects.create(
            client=client,
            request_type=request_type,
            scheduled_start=scheduled_start,
            expected_duration=expected_duration,
            description=description,
            address_line1=address_line1,
            address_line2=address_line2,
            city=city,
            state=state,
            postal_code=postal_code,
            contact_phone=contact_phone,
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
        rejected = _transition(
            service_request,
            Status.REJECTED,
            actor=coordinator,
            reviewed_by=coordinator,
            reviewed_at=timezone.now(),
            rejection_reason=rejection_reason,
        )
        _notify_after_commit(tasks.notify_client_of_rejection, rejected.pk)

    return rejected


def assign_request(service_request: ServiceRequest, personnel: User, coordinator: User):
    """Give an approved request to a specific personnel member.

    Two state machines move together here (brief section 3, insight 1): the
    request goes READY_FOR_ASSIGNMENT -> ASSIGNED, and a fresh Assignment is
    created in PENDING. Both inside one transaction, so a request can never be
    ASSIGNED with nothing assigned to it.
    """
    with transaction.atomic():
        # Transition first: it locks the row and validates the move, so an
        # out-of-state request fails with IllegalTransition rather than the
        # confusing unique-constraint error the Assignment insert would give.
        locked = _transition(service_request, Status.ASSIGNED, actor=coordinator)

        # Eligibility is enforced, not merely suggested -- otherwise a
        # coordinator could assign someone in the wrong sector, or someone who
        # never made themselves available.
        eligible = recommend_personnel(locked).values_list("user_id", flat=True)
        if personnel.pk not in eligible:
            raise ValueError(
                f"{personnel} is not eligible for this request: they must work in "
                f"{locked.request_type.required_sector}, be available, and not "
                f"have already declined it."
            )

        assignment = Assignment.objects.create(
            service_request=locked,
            personnel=personnel,
            assigned_by=coordinator,
        )
        _notify_after_commit(
            tasks.notify_personnel_of_assignment, assignment.pk
        )

    return assignment


class OutsideServiceWindow(ValueError):
    """Right state, wrong time -- a third failure mode alongside
    IllegalTransition (wrong state) and plain ValueError (bad argument).

    Subclasses ValueError deliberately, so callers that only catch ValueError
    keep working while callers that want the window can catch this and show
    the actual times.
    """

    def __init__(self, message, opens_at, closes_at):
        self.opens_at = opens_at
        self.closes_at = closes_at
        super().__init__(message)


def _notify_after_commit(task, *args):
    """Queue a notification only once the transaction has actually committed.

    NOT task.delay() inline. Enqueuing inside the transaction gives you one of
    two classic bugs: a worker picks the task up before the commit lands and
    reads a row that does not exist yet, or the transaction rolls back and the
    client has already been told about something that never happened
    (ADR-011 D3).

    The audit write is the opposite -- it belongs INSIDE, so history and status
    commit together. Same seam, deliberately different moments.
    """
    transaction.on_commit(lambda: task.delay(*args))


def _transition_assignment(assignment: Assignment, to_status: str):
    """Move an assignment to `to_status`, stamping when it was answered.

    Must be called inside transaction.atomic(). Same shape as _transition:
    re-read under a lock, validate against the map, then write -- so two taps
    on Accept cannot both pass the check.
    """
    locked = Assignment.objects.select_for_update().get(pk=assignment.pk)

    if to_status not in ASSIGNMENT_TRANSITIONS[locked.status]:
        raise IllegalTransition(locked.status, to_status, subject="assignment")

    locked.status = to_status
    locked.responded_at = timezone.now()
    locked.save(update_fields=["status", "responded_at", "updated_at"])
    return locked


def _guard_owner(assignment: Assignment, personnel: User):
    """Only the person an assignment was given to may answer it.

    The views also filter by owner, so this is the second layer -- it keeps the
    service safe for callers that have no queryset in front of them.
    """
    if assignment.personnel_id != personnel.pk:
        raise ValueError("This assignment belongs to someone else.")


def accept_assignment(assignment: Assignment, personnel: User):
    """Personnel accepts: the work is theirs and the slot is booked in."""
    _guard_owner(assignment, personnel)

    with transaction.atomic():
        accepted = _transition_assignment(assignment, AssignmentStatus.ACCEPTED)
        # SCHEDULED, not IN_PROGRESS: they have agreed to do it, they have not
        # started it. The client must not be told work is under way yet.
        scheduled = _transition(
            accepted.service_request, Status.SCHEDULED, actor=personnel
        )
        _notify_after_commit(tasks.notify_client_of_acceptance, scheduled.pk)

    return accepted


def decline_assignment(assignment: Assignment, personnel: User):
    """Personnel declines: the request returns to the pool for someone else.

    The declined row is kept deliberately -- it is what stops eligibility
    offering this request back to the same person (recommendation.py rule 3).
    """
    _guard_owner(assignment, personnel)

    with transaction.atomic():
        declined = _transition_assignment(assignment, AssignmentStatus.DECLINED)
        _transition(
            declined.service_request, Status.READY_FOR_ASSIGNMENT, actor=personnel
        )

    return declined


def _guard_service_window(service_request: ServiceRequest, too_early: str, too_late: str):
    """Both commencing and completing must happen inside the booked slot.

    A transition guard rather than a CheckConstraint: it depends on now() and
    on the prior state, neither of which a constraint can see (ADR-010 D3).
    """
    now = timezone.now()

    if now < service_request.service_window_opens_at:
        raise OutsideServiceWindow(
            too_early,
            service_request.service_window_opens_at,
            service_request.service_window_closes_at,
        )

    if now > service_request.service_window_closes_at:
        raise OutsideServiceWindow(
            too_late,
            service_request.service_window_opens_at,
            service_request.service_window_closes_at,
        )


def start_work(assignment: Assignment, personnel: User):
    """Personnel commences the work they accepted.

    The transition the model was missing: accepting books the slot, starting is
    what actually puts the request in progress.
    """
    _guard_owner(assignment, personnel)

    if assignment.status != AssignmentStatus.ACCEPTED:
        raise ValueError("You can only start work you accepted.")

    service_request = assignment.service_request
    _guard_service_window(
        service_request,
        too_early="This work is not due to start yet.",
        too_late="The window for this work has closed. Contact the coordinator.",
    )

    with transaction.atomic():
        started = _transition(
            service_request,
            Status.IN_PROGRESS,
            actor=personnel,
            started_at=timezone.now(),
        )
        _notify_after_commit(tasks.notify_work_started, started.pk)

    return started


def fulfil_request(assignment: Assignment, personnel: User):
    """The assigned personnel records the work as done (ADR-011 D4).

    Requires the work to have been started -- the transition map allows
    FULFILLED only from IN_PROGRESS, so completing something never commenced
    is refused as an IllegalTransition.
    """
    _guard_owner(assignment, personnel)

    if assignment.status != AssignmentStatus.ACCEPTED:
        raise ValueError("You can only complete work you accepted.")

    service_request = assignment.service_request
    _guard_service_window(
        service_request,
        too_early="This work is not due to start yet, so it cannot be marked complete.",
        too_late=(
            "The window for recording this work has closed. Contact the coordinator."
        ),
    )

    with transaction.atomic():
        fulfilled = _transition(service_request, Status.FULFILLED, actor=personnel)
        _notify_after_commit(tasks.notify_work_completed, fulfilled.pk)

    return fulfilled
