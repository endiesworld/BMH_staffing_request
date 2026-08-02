from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from accounts import services as account_services
from accounts.models import PersonnelProfile
from servicing import services
from servicing.models import RequestType, ServiceRequest


class ServicingTestCase(TestCase):
    """Shared cast: one client, one coordinator, one request type.

    setUpTestData runs once per class rather than once per test, and the rows
    are rolled back at the end of the class. Note the four seeded types from
    migration 0002 also exist here (the test DB is built by running migrations),
    so tests create their own type rather than relying on the seed.
    """

    @classmethod
    def setUpTestData(cls):
        cls.client_user = account_services.create_client(
            email="client@example.com",
            password="s3cret-pass",
            organization_name="Acme",
            phone_number="+10000000000",
        )
        cls.coordinator = account_services.create_coordinator(
            email="coordinator@example.com",
            password="s3cret-pass",
            department="Operations",
            region="North",
        )
        cls.request_type = RequestType.objects.create(
            code="test-nursing-visit",
            name="Test Nursing Visit",
            required_sector=PersonnelProfile.SectorCategory.HEALTHCARE,
        )

    def submit(self, title="Nursing visit"):
        return services.submit_request(
            client=self.client_user,
            request_type=self.request_type,
            title=title,
            description="Weekly check-in",
        )


class SubmitRequestTests(ServicingTestCase):
    def test_creates_request_in_submitted_awaiting_review(self):
        """A new request starts at SUBMITTED with the review fields untouched."""
        service_request = self.submit()

        self.assertEqual(service_request.status, ServiceRequest.Status.SUBMITTED)
        self.assertEqual(service_request.client, self.client_user)
        self.assertEqual(service_request.request_type, self.request_type)

        # The review fields must be unset -- this is the state the CheckConstraint
        # pins down, and what the D4 cardinality argument depends on.
        self.assertIsNone(service_request.reviewed_by)
        self.assertIsNone(service_request.reviewed_at)
        self.assertEqual(service_request.rejection_reason, "")

    def test_rejects_a_retired_request_type(self):
        """A retired type accepts no new requests (this is what is_active is for)."""
        retired = RequestType.objects.create(
            code="retired-type",
            name="Retired Type",
            required_sector=PersonnelProfile.SectorCategory.LOGISTICS,
            is_active=False,
        )

        with self.assertRaises(ValueError):
            services.submit_request(
                client=self.client_user,
                request_type=retired,
                title="Nope",
                description="d",
            )

        self.assertEqual(ServiceRequest.objects.count(), 0)


class ApproveRequestTests(ServicingTestCase):
    def test_approval_records_reviewer_and_opens_for_assignment(self):
        """Approving moves the request into the pool and stamps who decided it."""
        approved = services.approve_request(self.submit(), self.coordinator)

        self.assertEqual(approved.status, ServiceRequest.Status.READY_FOR_ASSIGNMENT)
        self.assertEqual(approved.reviewed_by, self.coordinator)
        self.assertIsNotNone(approved.reviewed_at)
        # An approval is not a rejection, so it carries no reason.
        self.assertEqual(approved.rejection_reason, "")

    def test_returns_fresh_instance_and_leaves_the_callers_copy_stale(self):
        """_transition works on its own locked re-read: use the return value."""
        service_request = self.submit()

        services.approve_request(service_request, self.coordinator)

        # The caller's in-memory object was never touched...
        self.assertEqual(service_request.status, ServiceRequest.Status.SUBMITTED)
        # ...but the database was.
        service_request.refresh_from_db()
        self.assertEqual(
            service_request.status, ServiceRequest.Status.READY_FOR_ASSIGNMENT
        )

    def test_cannot_approve_a_request_twice(self):
        """READY_FOR_ASSIGNMENT -> READY_FOR_ASSIGNMENT is not an edge."""
        approved = services.approve_request(self.submit(), self.coordinator)

        with self.assertRaises(services.IllegalTransition) as ctx:
            services.approve_request(approved, self.coordinator)

        self.assertEqual(
            ctx.exception.from_status, ServiceRequest.Status.READY_FOR_ASSIGNMENT
        )


class RejectRequestTests(ServicingTestCase):
    def test_rejection_records_reason_and_reviewer(self):
        """Rejecting stamps the reviewer and stores an explanation for the client."""
        rejected = services.reject_request(
            self.submit(), self.coordinator, "Out of catchment area"
        )

        self.assertEqual(rejected.status, ServiceRequest.Status.REJECTED)
        self.assertEqual(rejected.reviewed_by, self.coordinator)
        self.assertEqual(rejected.rejection_reason, "Out of catchment area")

    def test_blank_reason_is_refused_before_anything_is_written(self):
        """A whitespace-only reason is caught by the service, not left to the DB."""
        service_request = self.submit()

        with self.assertRaises(ValueError):
            services.reject_request(service_request, self.coordinator, "   ")

        service_request.refresh_from_db()
        self.assertEqual(service_request.status, ServiceRequest.Status.SUBMITTED)

    def test_rejection_is_terminal(self):
        """Nothing leaves REJECTED -- the fact D4 and the constraint both rest on."""
        rejected = services.reject_request(
            self.submit(), self.coordinator, "Out of catchment area"
        )

        with self.assertRaises(services.IllegalTransition):
            services.approve_request(rejected, self.coordinator)

        with self.assertRaises(services.IllegalTransition):
            services.reject_request(rejected, self.coordinator, "again")


class TransitionMapTests(TestCase):
    def test_every_status_is_a_key(self):
        """A status missing from the map would raise KeyError, not IllegalTransition.

        This is the test that catches someone adding a state and forgetting the map.
        """
        self.assertEqual(set(services.TRANSITIONS), set(ServiceRequest.Status.values))

    def test_terminal_states_have_no_exits(self):
        """REJECTED and FULFILLED are dead ends by design."""
        self.assertEqual(services.TRANSITIONS[ServiceRequest.Status.REJECTED], set())
        self.assertEqual(services.TRANSITIONS[ServiceRequest.Status.FULFILLED], set())


class TransitionAtomicityTests(ServicingTestCase):
    def test_status_change_rolls_back_when_the_history_write_fails(self):
        """The status change and its history entry commit together or not at all."""
        service_request = self.submit()

        with patch.object(services, "_record_transition", side_effect=Exception("boom")):
            with self.assertRaises(Exception):
                services.approve_request(service_request, self.coordinator)

        # The approval must NOT survive a failed history write.
        service_request.refresh_from_db()
        self.assertEqual(service_request.status, ServiceRequest.Status.SUBMITTED)
        self.assertIsNone(service_request.reviewed_by)


class ReviewFieldsConstraintTests(ServicingTestCase):
    """The DB refuses rows where the review fields disagree with the status.

    These deliberately bypass the service layer -- that is the point of a
    CheckConstraint (ADR-010 D4). Every write here goes through the ORM
    directly, which never calls full_clean(), so only the database is left
    to catch the bad row.

    Each failing write is wrapped in its own transaction.atomic() block: an
    IntegrityError breaks the surrounding transaction, and the savepoint lets
    the test continue afterwards.
    """

    CONSTRAINT = "servicerequest_review_fields_match_status"

    def create(self, **overrides):
        return ServiceRequest.objects.create(
            client=self.client_user,
            request_type=self.request_type,
            title="Nursing visit",
            description="Weekly check-in",
            **overrides,
        )

    def test_submitted_cannot_have_a_reviewer(self):
        """Not yet reviewed, yet somebody is recorded as having reviewed it."""
        with self.assertRaisesMessage(IntegrityError, self.CONSTRAINT):
            with transaction.atomic():
                self.create(
                    status=ServiceRequest.Status.SUBMITTED,
                    reviewed_by=self.coordinator,
                    reviewed_at=timezone.now(),
                )

    def test_a_reviewed_status_requires_a_reviewer(self):
        """Past the review gate with no record of who let it through."""
        with self.assertRaisesMessage(IntegrityError, self.CONSTRAINT):
            with transaction.atomic():
                self.create(status=ServiceRequest.Status.IN_PROGRESS)

    def test_reviewer_and_timestamp_must_be_set_together(self):
        """Half-filled review fields are as meaningless as none at all."""
        with self.assertRaisesMessage(IntegrityError, self.CONSTRAINT):
            with transaction.atomic():
                self.create(
                    status=ServiceRequest.Status.READY_FOR_ASSIGNMENT,
                    reviewed_by=self.coordinator,
                    reviewed_at=None,
                )

    def test_queryset_update_cannot_bypass_the_constraint(self):
        """.update() skips save(), signals and full_clean() -- the DB still holds.

        This is the scenario the constraint exists for: a code path that never
        touches the service layer at all.
        """
        service_request = self.create()

        with self.assertRaisesMessage(IntegrityError, self.CONSTRAINT):
            with transaction.atomic():
                ServiceRequest.objects.filter(pk=service_request.pk).update(
                    status=ServiceRequest.Status.IN_PROGRESS
                )

    def test_full_clean_reports_the_friendly_message(self):
        """Surfaced through a form, the violation reads as prose, not a DB error."""
        service_request = ServiceRequest(
            client=self.client_user,
            request_type=self.request_type,
            title="Nursing visit",
            description="Weekly check-in",
            status=ServiceRequest.Status.SUBMITTED,
            reviewed_by=self.coordinator,
            reviewed_at=timezone.now(),
        )

        with self.assertRaisesMessage(ValidationError, "must be set if and only if"):
            service_request.full_clean()


class RejectionReasonConstraintTests(ServicingTestCase):
    """A rejection must be explainable, and nothing else may carry a reason."""

    CONSTRAINT = "servicerequest_rejection_reason_only_when_rejected"

    def create(self, **overrides):
        return ServiceRequest.objects.create(
            client=self.client_user,
            request_type=self.request_type,
            title="Nursing visit",
            description="Weekly check-in",
            reviewed_by=self.coordinator,
            reviewed_at=timezone.now(),
            **overrides,
        )

    def test_rejection_requires_a_reason(self):
        """A rejection the client cannot be given an explanation for."""
        with self.assertRaisesMessage(IntegrityError, self.CONSTRAINT):
            with transaction.atomic():
                self.create(status=ServiceRequest.Status.REJECTED)

    def test_only_a_rejection_may_carry_a_reason(self):
        """Safe to enforce only because REJECTED is terminal: no stale leftovers."""
        with self.assertRaisesMessage(IntegrityError, self.CONSTRAINT):
            with transaction.atomic():
                self.create(
                    status=ServiceRequest.Status.READY_FOR_ASSIGNMENT,
                    rejection_reason="stale leftover",
                )

    def test_whitespace_only_reason_slips_past_the_database(self):
        """Known limitation: the DB checks != '', so only the service catches '   '.

        Documents why reject_request() strips before checking rather than
        trusting the constraint alone.
        """
        service_request = self.create(
            status=ServiceRequest.Status.REJECTED, rejection_reason="   "
        )

        self.assertEqual(service_request.rejection_reason, "   ")
