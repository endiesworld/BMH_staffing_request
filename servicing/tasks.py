"""Notification delivery (ADR-008, ADR-011 D2/D3).

The only work in this system that belongs on a queue: external, slow-ish, and
retry-prone. Everything else -- the state machine, eligibility -- is a
millisecond database operation and runs synchronously.

Two rules every task here follows:

1. **Arguments are IDs, never model instances.** Arguments are serialised
   through the broker as JSON, so an instance could not be sent at all, and a
   snapshot would be stale by the time a worker picked it up. Tasks re-fetch.

2. **Tasks are queued from transaction.on_commit(), never inside the
   transaction.** See the call sites in services.py.
"""

import logging

from celery import shared_task
from django.core.mail import send_mail

from .models import Assignment, ServiceRequest

logger = logging.getLogger(__name__)

# A failed notification is worth retrying -- a provider blip should not silently
# lose a client's only word that their request was rejected.
RETRY = {
    "bind": True,
    "autoretry_for": (Exception,),
    "retry_backoff": True,
    "max_retries": 3,
}


def _send(subject, body, recipients):
    """One place for delivery, so swapping the channel is one edit."""
    recipients = [address for address in recipients if address]
    if not recipients:
        return 0

    send_mail(
        subject=f"[BMH Service Hub] {subject}",
        message=body,
        from_email=None,  # falls back to DEFAULT_FROM_EMAIL
        recipient_list=recipients,
        fail_silently=False,
    )
    logger.info("notified %s: %s", ", ".join(recipients), subject)
    return len(recipients)


def _describe(service_request):
    return (
        f"{service_request.request_type.name}\n"
        f"When:  {service_request.scheduled_start:%d %b %Y, %H:%M} "
        f"({service_request.duration_display})\n"
        f"Where: {service_request.location_display}\n"
    )


def _coordinator_email(service_request):
    """The coordinator who reviewed it owns it from then on."""
    return getattr(service_request.reviewed_by, "email", None)


@shared_task(**RETRY)
def notify_personnel_of_assignment(self, assignment_id):
    """Without this, personnel only learn of work by refreshing a page."""
    assignment = Assignment.objects.filter(pk=assignment_id).select_related(
        "personnel", "service_request__request_type"
    ).first()
    if assignment is None:
        logger.warning("assignment %s vanished before notifying", assignment_id)
        return 0

    service_request = assignment.service_request
    return _send(
        "You have been offered a job",
        f"{_describe(service_request)}\n"
        "Open your assignments to accept or decline.",
        [assignment.personnel.email],
    )


@shared_task(**RETRY)
def notify_client_of_acceptance(self, service_request_id):
    service_request = _fetch(service_request_id)
    if service_request is None:
        return 0

    return _send(
        "Someone has been confirmed for your request",
        f"{_describe(service_request)}\n"
        "A member of personnel has accepted. They will start at the scheduled "
        "time.",
        [service_request.client.email],
    )


@shared_task(**RETRY)
def notify_client_of_rejection(self, service_request_id):
    service_request = _fetch(service_request_id)
    if service_request is None:
        return 0

    return _send(
        "Your request was not approved",
        f"{_describe(service_request)}\n"
        f"Reason: {service_request.rejection_reason}",
        [service_request.client.email],
    )


@shared_task(**RETRY)
def notify_work_started(self, service_request_id):
    """Client and coordinator both need to know work actually commenced."""
    service_request = _fetch(service_request_id)
    if service_request is None:
        return 0

    return _send(
        "Work has started on your request",
        f"{_describe(service_request)}\n"
        f"Started at {service_request.started_at:%d %b %Y, %H:%M}.",
        [service_request.client.email, _coordinator_email(service_request)],
    )


@shared_task(**RETRY)
def notify_work_completed(self, service_request_id):
    service_request = _fetch(service_request_id)
    if service_request is None:
        return 0

    return _send(
        "Your request has been completed",
        f"{_describe(service_request)}\nThis request is now closed.",
        [service_request.client.email, _coordinator_email(service_request)],
    )


def _fetch(service_request_id):
    service_request = (
        ServiceRequest.objects.filter(pk=service_request_id)
        .select_related("client", "request_type", "reviewed_by")
        .first()
    )
    if service_request is None:
        logger.warning("request %s vanished before notifying", service_request_id)
    return service_request
