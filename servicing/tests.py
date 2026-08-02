from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import services as account_services
from accounts.models import PersonnelProfile
from servicing import services
from servicing.recommendation import recommend_personnel
from servicing.models import Assignment, RequestType, ServiceRequest


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

    def submit(self, **overrides):
        return services.submit_request(
            client=self.client_user,
            request_type=self.request_type,
            **{**self.request_details(), **overrides},
        )

    @staticmethod
    def request_details():
        """The client-supplied half of a request (ADR-011 D5)."""
        return {
            "scheduled_start": timezone.now() + timedelta(days=3),
            "expected_duration": timedelta(hours=2),
            "description": "Weekly check-in",
            "address_line1": "1600 Pennsylvania Ave NW",
            "city": "Washington",
            "state": "DC",
            "postal_code": "20500",
            "contact_phone": "+15551234567",
        }


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
                **self.request_details(),
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
            **self.request_details(),
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
            **self.request_details(),
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
            **self.request_details(),
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


class ApproveActionAdminTests(ServicingTestCase):
    """The admin action, driven the way a coordinator's browser drives it.

    Covers what `manage.py check` cannot: attribute access inside the action
    body, which only fails at runtime.
    """

    def setUp(self):
        self.url = reverse("admin:servicing_servicerequest_changelist")
        self.client.force_login(self.coordinator)

    def approve(self, *service_requests):
        return self.client.post(
            self.url,
            {
                "action": "approve_requests",
                "_selected_action": [str(sr.pk) for sr in service_requests],
                "index": "0",
            },
            follow=True,
        )

    def test_coordinator_can_reach_the_changelist(self):
        """Group permissions, not the role field, are what open this page."""
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)

    def test_action_approves_and_records_the_coordinator(self):
        service_request = self.submit()

        response = self.approve(service_request)

        service_request.refresh_from_db()
        self.assertEqual(
            service_request.status, ServiceRequest.Status.READY_FOR_ASSIGNMENT
        )
        self.assertEqual(service_request.reviewed_by, self.coordinator)
        self.assertContains(response, "1 request(s) approved")

    def test_illegal_transition_warns_instead_of_crashing(self):
        """The branch that referenced a removed field and blew up at runtime."""
        service_request = services.approve_request(self.submit(), self.coordinator)

        response = self.approve(service_request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cannot move a request from")

    def test_requests_cannot_be_added_through_the_admin(self):
        """Creation belongs to submit_request(), not an admin form."""
        response = self.client.get(
            reverse("admin:servicing_servicerequest_add"), follow=True
        )

        self.assertEqual(response.status_code, 403)


class AssignmentConstraintTests(ServicingTestCase):
    """The two invariants the database holds for the second state machine."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.nurse = account_services.create_personnel(
            email="nurse@example.com",
            password="s3cret-pass",
            sector=PersonnelProfile.SectorCategory.HEALTHCARE,
        )
        cls.other_nurse = account_services.create_personnel(
            email="nurse2@example.com",
            password="s3cret-pass",
            sector=PersonnelProfile.SectorCategory.HEALTHCARE,
        )

    def make(self, service_request, personnel, **overrides):
        return Assignment.objects.create(
            service_request=service_request,
            personnel=personnel,
            assigned_by=self.coordinator,
            **overrides,
        )

    def test_a_request_can_only_have_one_live_assignment(self):
        """Two people must never both think a job is theirs."""
        service_request = self.submit()
        self.make(service_request, self.nurse)

        # assertRaises, not assertRaisesMessage: SQLite names CHECK constraints
        # in its errors but reports a partial-unique violation as
        # "UNIQUE constraint failed: <table>.<column>" with no name. Postgres
        # does include it. The readable message is asserted separately below,
        # via validate_constraints(), which is portable.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make(service_request, self.other_nurse)

    def test_declined_assignments_do_not_block_reassignment(self):
        """What the partial constraint buys: the reassign loop needs history.

        A plain unique constraint on service_request would make declining a
        dead end.
        """
        service_request = self.submit()
        self.make(
            service_request,
            self.nurse,
            status=Assignment.Status.DECLINED,
            responded_at=timezone.now(),
        )

        # Same request, someone else, no conflict.
        self.make(service_request, self.other_nurse)

        self.assertEqual(service_request.assignments.count(), 2)

    def test_accepted_assignment_also_blocks_a_second_one(self):
        """"Live" is PENDING or ACCEPTED, not just PENDING."""
        service_request = self.submit()
        self.make(
            service_request,
            self.nurse,
            status=Assignment.Status.ACCEPTED,
            responded_at=timezone.now(),
        )

        # assertRaises, not assertRaisesMessage: SQLite names CHECK constraints
        # in its errors but reports a partial-unique violation as
        # "UNIQUE constraint failed: <table>.<column>" with no name. Postgres
        # does include it. The readable message is asserted separately below,
        # via validate_constraints(), which is portable.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self.make(service_request, self.other_nurse)

    def test_pending_assignment_cannot_carry_a_response_time(self):
        with self.assertRaisesMessage(
            IntegrityError, "assignment_responded_at_matches_status"
        ):
            with transaction.atomic():
                self.make(
                    self.submit(), self.nurse, responded_at=timezone.now()
                )

    def test_answered_assignment_must_record_when(self):
        with self.assertRaisesMessage(
            IntegrityError, "assignment_responded_at_matches_status"
        ):
            with transaction.atomic():
                self.make(
                    self.submit(), self.nurse, status=Assignment.Status.ACCEPTED
                )

    def test_live_assignment_conflict_reports_a_readable_message(self):
        """Portable half: validate_constraints() surfaces the wording we set."""
        service_request = self.submit()
        self.make(service_request, self.nurse)

        clash = Assignment(
            service_request=service_request,
            personnel=self.other_nurse,
            assigned_by=self.coordinator,
        )

        with self.assertRaisesMessage(
            ValidationError, "already has an assignment awaiting a response"
        ):
            clash.validate_constraints()


class PersonnelFixtureMixin:
    """Shared personnel cast -- a MIXIN, deliberately not a TestCase subclass.

    Subclassing a TestCase inherits its test methods as well as its fixtures,
    so they re-run under the subclass's setUp. That is exactly how
    test_unavailable_personnel_are_invisible came to fail inside
    AssignmentViewTests, whose setUp makes that very person available.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.matching = account_services.create_personnel(
            email="healthcare@example.com",
            password="s3cret-pass",
            sector=PersonnelProfile.SectorCategory.HEALTHCARE,
        )
        cls.wrong_sector = account_services.create_personnel(
            email="logistics@example.com",
            password="s3cret-pass",
            sector=PersonnelProfile.SectorCategory.LOGISTICS,
        )

    @staticmethod
    def make_available(user):
        profile = user.personnel_profile
        profile.availability_status = PersonnelProfile.AvailabilityStatus.AVAILABLE
        profile.save(update_fields=["availability_status"])
        return user


class EligibilityTests(PersonnelFixtureMixin, ServicingTestCase):
    """Eligibility is a query, not a stored state (brief section 3)."""

    def test_unavailable_personnel_are_invisible(self):
        """Registering is not opting in -- the demo dead-end if you forget."""
        self.assertEqual(list(recommend_personnel(self.submit())), [])

    def test_only_matching_sector_is_offered(self):
        self.make_available(self.matching)
        self.make_available(self.wrong_sector)

        candidates = recommend_personnel(self.submit())

        self.assertEqual([p.user for p in candidates], [self.matching])

    def test_someone_who_declined_is_not_offered_again(self):
        """Otherwise the reassign loop hands it straight back to them."""
        self.make_available(self.matching)
        service_request = self.submit()
        Assignment.objects.create(
            service_request=service_request,
            personnel=self.matching,
            assigned_by=self.coordinator,
            status=Assignment.Status.DECLINED,
            responded_at=timezone.now(),
        )

        self.assertEqual(list(recommend_personnel(service_request)), [])


class AssignRequestTests(PersonnelFixtureMixin, ServicingTestCase):
    def approved(self):
        return services.approve_request(self.submit(), self.coordinator)

    def test_assigning_moves_both_state_machines(self):
        self.make_available(self.matching)
        service_request = self.approved()

        assignment = services.assign_request(
            service_request, self.matching, self.coordinator
        )

        service_request.refresh_from_db()
        self.assertEqual(service_request.status, ServiceRequest.Status.ASSIGNED)
        self.assertEqual(assignment.status, Assignment.Status.PENDING)
        self.assertEqual(assignment.assigned_by, self.coordinator)
        self.assertIsNone(assignment.responded_at)

    def test_cannot_assign_a_request_that_was_not_approved(self):
        self.make_available(self.matching)
        service_request = self.submit()

        with self.assertRaises(services.IllegalTransition):
            services.assign_request(service_request, self.matching, self.coordinator)

        self.assertEqual(Assignment.objects.count(), 0)

    def test_cannot_assign_an_ineligible_person(self):
        """Eligibility is enforced by the service, not just suggested by the UI."""
        self.make_available(self.wrong_sector)
        service_request = self.approved()

        with self.assertRaises(ValueError):
            services.assign_request(service_request, self.wrong_sector, self.coordinator)

        # And the request must not have been left ASSIGNED with nothing assigned.
        service_request.refresh_from_db()
        self.assertEqual(
            service_request.status, ServiceRequest.Status.READY_FOR_ASSIGNMENT
        )
        self.assertEqual(Assignment.objects.count(), 0)


class RespondToAssignmentTests(PersonnelFixtureMixin, ServicingTestCase):
    """The second state machine, and how it drives the first."""

    def assigned(self):
        self.make_available(self.matching)
        approved = services.approve_request(self.submit(), self.coordinator)
        return services.assign_request(approved, self.matching, self.coordinator)

    def test_accepting_schedules_but_does_not_start_the_work(self):
        """Accepting and commencing are different events.

        The client must not be told work is under way just because someone
        agreed to do it.
        """
        assignment = self.assigned()

        accepted = services.accept_assignment(assignment, self.matching)

        self.assertEqual(accepted.status, Assignment.Status.ACCEPTED)
        self.assertIsNotNone(accepted.responded_at)
        accepted.service_request.refresh_from_db()
        self.assertEqual(
            accepted.service_request.status, ServiceRequest.Status.SCHEDULED
        )
        self.assertIsNone(accepted.service_request.started_at)

    def test_declining_returns_the_request_to_the_pool(self):
        assignment = self.assigned()

        declined = services.decline_assignment(assignment, self.matching)

        self.assertEqual(declined.status, Assignment.Status.DECLINED)
        declined.service_request.refresh_from_db()
        self.assertEqual(
            declined.service_request.status,
            ServiceRequest.Status.READY_FOR_ASSIGNMENT,
        )

    def test_a_declined_request_can_be_reassigned_to_someone_else(self):
        """The whole point of the loop, and of keeping declined rows."""
        assignment = self.assigned()
        services.decline_assignment(assignment, self.matching)

        second = self.make_available(
            account_services.create_personnel(
                email="nurse-two@example.com",
                password="s3cret-pass",
                sector=PersonnelProfile.SectorCategory.HEALTHCARE,
            )
        )
        service_request = assignment.service_request
        service_request.refresh_from_db()

        reassigned = services.assign_request(
            service_request, second, self.coordinator
        )

        self.assertEqual(reassigned.personnel, second)
        self.assertEqual(service_request.assignments.count(), 2)

    def test_cannot_answer_the_same_assignment_twice(self):
        assignment = self.assigned()
        services.accept_assignment(assignment, self.matching)

        with self.assertRaises(services.IllegalTransition) as ctx:
            services.decline_assignment(assignment, self.matching)

        self.assertEqual(ctx.exception.subject, "assignment")

    def test_cannot_answer_someone_elses_assignment(self):
        assignment = self.assigned()

        with self.assertRaises(ValueError):
            services.accept_assignment(assignment, self.wrong_sector)

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, Assignment.Status.PENDING)


class AssignmentViewTests(PersonnelFixtureMixin, ServicingTestCase):
    def setUp(self):
        self.make_available(self.matching)
        approved = services.approve_request(self.submit(), self.coordinator)
        self.assignment = services.assign_request(
            approved, self.matching, self.coordinator
        )
        self.client.force_login(self.matching)

    def test_personnel_sees_only_their_own_assignments(self):
        other = self.make_available(
            account_services.create_personnel(
                email="stranger@example.com",
                password="s3cret-pass",
                sector=PersonnelProfile.SectorCategory.HEALTHCARE,
            )
        )
        self.client.force_login(other)

        response = self.client.get(reverse("servicing:my_assignments"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nothing assigned to you yet")

    def test_accept_through_the_view(self):
        response = self.client.post(
            reverse("servicing:accept_assignment", args=[self.assignment.pk]),
            follow=True,
        )

        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, Assignment.Status.ACCEPTED)
        self.assertContains(response, "Accepted.")

    def test_accepting_is_post_only(self):
        """A state change must not be reachable by a link or a prefetch."""
        response = self.client.get(
            reverse("servicing:accept_assignment", args=[self.assignment.pk])
        )

        self.assertEqual(response.status_code, 405)

    def test_another_persons_assignment_is_a_404(self):
        stranger = account_services.create_personnel(
            email="stranger@example.com",
            password="s3cret-pass",
            sector=PersonnelProfile.SectorCategory.HEALTHCARE,
        )
        self.client.force_login(stranger)

        response = self.client.post(
            reverse("servicing:accept_assignment", args=[self.assignment.pk])
        )

        self.assertEqual(response.status_code, 404)


class FulfilRequestTests(PersonnelFixtureMixin, ServicingTestCase):
    """ADR-011 D4/D5: the assigned personnel completes it, inside the window."""

    def accepted(self, **request_overrides):
        self.make_available(self.matching)
        approved = services.approve_request(
            self.submit(**request_overrides), self.coordinator
        )
        assignment = services.assign_request(
            approved, self.matching, self.coordinator
        )
        return services.accept_assignment(assignment, self.matching)

    def started(self, **request_overrides):
        assignment = self.accepted(**request_overrides)
        services.start_work(assignment, self.matching)
        assignment.refresh_from_db()
        return assignment

    def test_completing_inside_the_window(self):
        """Slot started an hour ago, two hours long -- we are mid-job."""
        assignment = self.started(
            scheduled_start=timezone.now() - timedelta(hours=1)
        )

        fulfilled = services.fulfil_request(assignment, self.matching)

        self.assertEqual(fulfilled.status, ServiceRequest.Status.FULFILLED)

    def test_cannot_complete_before_the_slot_starts(self):
        assignment = self.accepted(
            scheduled_start=timezone.now() + timedelta(days=2)
        )

        with self.assertRaises(services.OutsideServiceWindow) as ctx:
            services.fulfil_request(assignment, self.matching)

        self.assertIn("not due to start", str(ctx.exception))
        # Untouched: still only scheduled, since it was never started either.
        assignment.service_request.refresh_from_db()
        self.assertEqual(
            assignment.service_request.status, ServiceRequest.Status.SCHEDULED
        )

    def test_cannot_complete_after_the_grace_period(self):
        """Slot ended two hours ago + 24h grace, so we are a day too late."""
        assignment = self.accepted(
            scheduled_start=timezone.now() - timedelta(hours=30)
        )

        with self.assertRaises(services.OutsideServiceWindow) as ctx:
            services.fulfil_request(assignment, self.matching)

        self.assertIn("window", str(ctx.exception))

    def test_grace_period_still_allows_a_late_finish(self):
        """Slot ended three hours ago -- inside the 24h grace."""
        assignment = self.started(
            scheduled_start=timezone.now() - timedelta(hours=5)
        )

        fulfilled = services.fulfil_request(assignment, self.matching)

        self.assertEqual(fulfilled.status, ServiceRequest.Status.FULFILLED)

    def test_window_errors_are_catchable_as_value_errors(self):
        """OutsideFulfilmentWindow subclasses ValueError on purpose."""
        assignment = self.accepted(
            scheduled_start=timezone.now() + timedelta(days=2)
        )

        with self.assertRaises(ValueError):
            services.fulfil_request(assignment, self.matching)

    def test_cannot_complete_work_you_did_not_accept(self):
        self.make_available(self.matching)
        approved = services.approve_request(self.submit(), self.coordinator)
        pending = services.assign_request(approved, self.matching, self.coordinator)

        with self.assertRaises(ValueError):
            services.fulfil_request(pending, self.matching)

    def test_cannot_complete_someone_elses_work(self):
        assignment = self.started(
            scheduled_start=timezone.now() - timedelta(hours=1)
        )

        with self.assertRaises(ValueError):
            services.fulfil_request(assignment, self.wrong_sector)

    def test_completing_through_the_view(self):
        assignment = self.started(
            scheduled_start=timezone.now() - timedelta(hours=1)
        )
        self.client.force_login(self.matching)

        response = self.client.post(
            reverse("servicing:fulfil_assignment", args=[assignment.pk]),
            follow=True,
        )

        assignment.service_request.refresh_from_db()
        self.assertEqual(
            assignment.service_request.status, ServiceRequest.Status.FULFILLED
        )
        self.assertContains(response, "Marked complete")


class StartWorkTests(PersonnelFixtureMixin, ServicingTestCase):
    """The transition the model was missing: accepted is not commenced."""

    def accepted(self, **request_overrides):
        self.make_available(self.matching)
        approved = services.approve_request(
            self.submit(**request_overrides), self.coordinator
        )
        assignment = services.assign_request(approved, self.matching, self.coordinator)
        return services.accept_assignment(assignment, self.matching)

    def test_starting_moves_to_in_progress_and_stamps_the_time(self):
        assignment = self.accepted(
            scheduled_start=timezone.now() - timedelta(minutes=10)
        )

        started = services.start_work(assignment, self.matching)

        self.assertEqual(started.status, ServiceRequest.Status.IN_PROGRESS)
        self.assertIsNotNone(started.started_at)

    def test_cannot_start_before_the_slot(self):
        assignment = self.accepted(scheduled_start=timezone.now() + timedelta(days=2))

        with self.assertRaises(services.OutsideServiceWindow):
            services.start_work(assignment, self.matching)

    def test_cannot_start_twice(self):
        assignment = self.accepted(
            scheduled_start=timezone.now() - timedelta(minutes=10)
        )
        services.start_work(assignment, self.matching)

        with self.assertRaises(services.IllegalTransition):
            services.start_work(assignment, self.matching)

    def test_cannot_complete_work_that_was_never_started(self):
        """FULFILLED is reachable only from IN_PROGRESS."""
        assignment = self.accepted(
            scheduled_start=timezone.now() - timedelta(minutes=10)
        )

        with self.assertRaises(services.IllegalTransition):
            services.fulfil_request(assignment, self.matching)

    def test_starting_through_the_view(self):
        assignment = self.accepted(
            scheduled_start=timezone.now() - timedelta(minutes=10)
        )
        self.client.force_login(self.matching)

        response = self.client.post(
            reverse("servicing:start_assignment", args=[assignment.pk]), follow=True
        )

        assignment.service_request.refresh_from_db()
        self.assertEqual(
            assignment.service_request.status, ServiceRequest.Status.IN_PROGRESS
        )
        self.assertContains(response, "Work started")


class AssignmentVisibilityAdminTests(PersonnelFixtureMixin, ServicingTestCase):
    """Coordinators must be able to SEE who holds a request, not just assign it.

    Visibility is a pull question -- no notification answers "show me the queue
    and who is on what".
    """

    def setUp(self):
        self.client.force_login(self.coordinator)

    def scheduled(self):
        self.make_available(self.matching)
        approved = services.approve_request(self.submit(), self.coordinator)
        assignment = services.assign_request(
            approved, self.matching, self.coordinator
        )
        return services.accept_assignment(assignment, self.matching)

    def test_changelist_names_the_assignee(self):
        self.scheduled()

        response = self.client.get(
            reverse("admin:servicing_servicerequest_changelist")
        )

        self.assertContains(response, "Assigned to")
        self.assertContains(response, self.matching.email)

    def test_unassigned_request_shows_a_dash(self):
        self.submit()

        response = self.client.get(
            reverse("admin:servicing_servicerequest_changelist")
        )

        self.assertContains(response, "—")

    def test_declined_attempts_are_not_shown_as_current(self):
        """A declined assignment is history, not who holds it now."""
        self.make_available(self.matching)
        approved = services.approve_request(self.submit(), self.coordinator)
        assignment = services.assign_request(
            approved, self.matching, self.coordinator
        )
        services.decline_assignment(assignment, self.matching)

        response = self.client.get(
            reverse("admin:servicing_servicerequest_changelist")
        )

        self.assertContains(response, "—")

    def test_coordinator_can_reach_the_assignment_list(self):
        """Needs view_assignment, granted by accounts migration 0004."""
        self.scheduled()

        response = self.client.get(reverse("admin:servicing_assignment_changelist"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.matching.email)

    def test_request_page_shows_the_full_attempt_history(self):
        """Including who declined -- the inline, not just the live assignment."""
        accepted = self.scheduled()

        response = self.client.get(
            reverse(
                "admin:servicing_servicerequest_change",
                args=[accepted.service_request_id],
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.matching.email)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class NotificationTests(PersonnelFixtureMixin, ServicingTestCase):
    """Notifications fire after commit, to the right people (ADR-011 D2/D3).

    CELERY_TASK_ALWAYS_EAGER runs tasks inline instead of needing a broker and
    a worker in the test run. captureOnCommitCallbacks is the other half: a
    TestCase wraps each test in a transaction that never commits, so
    on_commit callbacks would otherwise NEVER run and every assertion here
    would pass vacuously.
    """

    def setUp(self):
        mail.outbox = []

    def recipients(self):
        return sorted(address for message in mail.outbox for address in message.to)

    def approved(self):
        return services.approve_request(self.submit(), self.coordinator)

    def test_assigning_notifies_the_personnel(self):
        self.make_available(self.matching)
        approved = self.approved()

        with self.captureOnCommitCallbacks(execute=True):
            services.assign_request(approved, self.matching, self.coordinator)

        self.assertEqual(self.recipients(), [self.matching.email])
        self.assertIn("offered a job", mail.outbox[0].subject)

    def test_accepting_notifies_the_client(self):
        self.make_available(self.matching)
        assignment = services.assign_request(
            self.approved(), self.matching, self.coordinator
        )
        mail.outbox = []

        with self.captureOnCommitCallbacks(execute=True):
            services.accept_assignment(assignment, self.matching)

        self.assertEqual(self.recipients(), [self.client_user.email])

    def test_starting_notifies_client_and_coordinator(self):
        self.make_available(self.matching)
        assignment = services.assign_request(
            services.approve_request(
                self.submit(scheduled_start=timezone.now() - timedelta(minutes=10)),
                self.coordinator,
            ),
            self.matching,
            self.coordinator,
        )
        # accept_assignment returns a fresh instance; the local one is stale.
        assignment = services.accept_assignment(assignment, self.matching)
        mail.outbox = []

        with self.captureOnCommitCallbacks(execute=True):
            services.start_work(assignment, self.matching)

        self.assertEqual(
            self.recipients(),
            sorted([self.client_user.email, self.coordinator.email]),
        )

    def test_completing_notifies_client_and_coordinator(self):
        self.make_available(self.matching)
        assignment = services.assign_request(
            services.approve_request(
                self.submit(scheduled_start=timezone.now() - timedelta(minutes=10)),
                self.coordinator,
            ),
            self.matching,
            self.coordinator,
        )
        assignment = services.accept_assignment(assignment, self.matching)
        services.start_work(assignment, self.matching)
        mail.outbox = []

        with self.captureOnCommitCallbacks(execute=True):
            services.fulfil_request(assignment, self.matching)

        self.assertEqual(
            self.recipients(),
            sorted([self.client_user.email, self.coordinator.email]),
        )

    def test_rejecting_notifies_the_client_with_the_reason(self):
        with self.captureOnCommitCallbacks(execute=True):
            services.reject_request(
                self.submit(), self.coordinator, "Out of catchment area"
            )

        self.assertEqual(self.recipients(), [self.client_user.email])
        self.assertIn("Out of catchment area", mail.outbox[0].body)

    def test_nothing_is_sent_when_the_transaction_rolls_back(self):
        """The bug on_commit exists to prevent: telling a client about a
        rejection that never happened."""
        service_request = self.submit()

        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    services.reject_request(
                        service_request, self.coordinator, "Out of catchment"
                    )
                    raise RuntimeError("something later blew up")

        self.assertEqual(mail.outbox, [])
        service_request.refresh_from_db()
        self.assertEqual(service_request.status, ServiceRequest.Status.SUBMITTED)
