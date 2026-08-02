from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from accounts import services
from accounts.models import ClientProfile, CoordinatorProfile, PersonnelProfile, User


class CreateClientTests(TestCase):
    def test_creates_user_and_linked_profile(self):
        """Happy path: one user + one linked ClientProfile, with correct data."""
        user = services.create_client(
            email="Client@Example.com",
            password="s3cret-pass",
            organization_name="Acme",
            phone_number="+10000000000",
        )

        # --- user side ---
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(user.role, User.Role.CLIENT)
        # normalize_email() lowercases the domain but leaves the local part alone.
        self.assertEqual(user.email, "Client@example.com")
        # password is stored hashed, never in plain text.
        self.assertTrue(user.check_password("s3cret-pass"))

        # --- profile side ---
        self.assertEqual(ClientProfile.objects.count(), 1)
        self.assertEqual(user.client_profile.organization_name, "Acme")
        self.assertEqual(user.client_profile.user_id, user.id)

    def test_rolls_back_user_when_profile_creation_fails(self):
        """Atomicity: if the profile write fails, the user write is undone too."""
        # Force the *second* step (profile creation) to blow up.
        with patch.object(
            ClientProfile.objects, "create", side_effect=Exception("boom")
        ):
            with self.assertRaises(Exception):
                services.create_client(
                    email="ghost@example.com",
                    password="s3cret-pass",
                    organization_name="Acme",
                    phone_number="+10000000000",
                )

        # The user must NOT survive a failed profile step.
        self.assertEqual(User.objects.count(), 0)


class CreatePersonnelTests(TestCase):
    def test_availability_defaults_to_unavailable(self):
        """A new personnel is not assignable until they opt in."""
        user = services.create_personnel(
            email="nurse@example.com",
            password="s3cret-pass",
            sector=PersonnelProfile.SectorCategory.HEALTHCARE,
        )

        self.assertEqual(user.role, User.Role.PERSONNEL)
        self.assertEqual(
            user.personnel_profile.availability_status,
            PersonnelProfile.AvailabilityStatus.UNAVAILABLE,
        )

class CreateCoordinatorTests(TestCase):
    def test_creates_coordinator_and_linked_profile(self):
        """Happy path: one user + one linked CoordinatorProfile, with correct data."""
        user = services.create_coordinator(
            email="emmanuel@example.com",
            password="s3cret-pass",
            department="Logistics",
            region="North",
        )
        
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(user.role, User.Role.COORDINATOR)
        self.assertEqual(user.email, "emmanuel@example.com")
        self.assertTrue(user.check_password("s3cret-pass"))
        
        self.assertEqual(user.coordinator_profile.department, "Logistics")
        self.assertEqual(user.coordinator_profile.region, "North")
        
    
    def test_coordinator_gets_admin_access_and_the_coordinator_group(self):
        """ADR-006 role<->Group sync: the role alone opens no doors.

        is_staff gets them through the admin door; the group decides what is
        behind it. Without both, a coordinator either cannot log in or logs in
        to an empty admin.
        """
        user = services.create_coordinator(
            email="coord2@example.com",
            password="s3cret-pass",
            department="Logistics",
            region="South",
        )

        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(
            [group.name for group in user.groups.all()], ["COORDINATOR"]
        )

        # Permissions arrive via the group, not the user directly.
        self.assertTrue(user.has_perm("servicing.view_servicerequest"))
        self.assertTrue(user.has_perm("servicing.change_servicerequest"))
        # ...and stop where they should: coordinators work requests, they do
        # not provision accounts or delete history.
        self.assertFalse(user.has_perm("servicing.delete_servicerequest"))
        self.assertFalse(user.has_perm("accounts.add_user"))

    def test_clients_get_no_admin_access(self):
        """Only coordinators are staff. A client must never reach the admin."""
        user = services.create_client(
            email="plain-client@example.com",
            password="s3cret-pass",
            organization_name="Acme",
            phone_number="+10000000000",
        )

        self.assertFalse(user.is_staff)
        self.assertEqual(user.groups.count(), 0)
        self.assertFalse(user.has_perm("servicing.view_servicerequest"))

    def test_rolls_back_user_when_profile_creation_fails(self):
        """Atomicity: if the profile write fails, the user write is undone too."""
        # Force the *second* step (profile creation) to blow up.
        with patch.object(
            CoordinatorProfile.objects, "create", side_effect=Exception("boom")
        ):
            with self.assertRaises(Exception):
                services.create_coordinator(
                    email="emmanuel@example.com",
                    password="s3cret-pass",
                    department="Logistics",
                    region="North",
                )

        # The user must NOT survive a failed profile step.
        self.assertEqual(User.objects.count(), 0)


class RegisterClientViewTests(TestCase):
    """Self-registration through the real stack: middleware, session, CSRF."""

    URL = reverse("accounts:register_client")
    VALID = {
        "email": "newclient@example.com",
        "organization_name": "Acme Care Services",
        "phone_number": "(555) 123-4567",
        "password1": "correct-horse-battery",
        "password2": "correct-horse-battery",
    }

    def test_get_renders_the_form(self):
        response = self.client.get(self.URL)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create a client account")

    def test_registration_creates_user_and_profile_and_logs_them_in(self):
        """One post produces a complete client, signed in and ready to work."""
        response = self.client.post(self.URL, self.VALID)

        self.assertRedirects(response, reverse("servicing:my_requests"))

        user = User.objects.get(email="newclient@example.com")
        self.assertEqual(user.role, User.Role.CLIENT)
        # Clients never reach the admin (contrast with create_coordinator).
        self.assertFalse(user.is_staff)
        self.assertTrue(user.check_password("correct-horse-battery"))

        # The profile half of the ADR-009 invariant.
        self.assertEqual(user.client_profile.organization_name, "Acme Care Services")
        # Normalised on the way in, shared with servicing's contact_phone.
        self.assertEqual(user.client_profile.phone_number, "+15551234567")

        # Logged in, not merely created.
        self.assertEqual(
            int(self.client.session["_auth_user_id"]), user.pk
        )

    def test_duplicate_email_is_refused_and_writes_nothing(self):
        services.create_client(
            email="newclient@example.com",
            password="s3cret-pass",
            organization_name="Existing",
            phone_number="+15550000000",
        )

        response = self.client.post(self.URL, self.VALID)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.filter(email="newclient@example.com").count(), 1)
        self.assertEqual(ClientProfile.objects.count(), 1)

    def test_mismatched_passwords_are_refused(self):
        response = self.client.post(
            self.URL, {**self.VALID, "password2": "something-else"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.count(), 0)

    def test_weak_password_is_refused(self):
        """AUTH_PASSWORD_VALIDATORS come free with UserCreationForm."""
        response = self.client.post(
            self.URL, {**self.VALID, "password1": "12345678", "password2": "12345678"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.count(), 0)

    def test_bad_phone_number_is_refused(self):
        response = self.client.post(self.URL, {**self.VALID, "phone_number": "555 12"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10-digit US phone number")
        self.assertEqual(User.objects.count(), 0)

    def test_already_signed_in_users_are_sent_away(self):
        user = services.create_client(
            email="existing@example.com",
            password="s3cret-pass",
            organization_name="Existing",
            phone_number="+15550000000",
        )
        self.client.force_login(user)

        response = self.client.get(self.URL)

        self.assertRedirects(response, reverse("servicing:my_requests"))


class RegisterPersonnelViewTests(TestCase):
    URL = reverse("accounts:register_personnel")
    VALID = {
        "email": "nurse@example.com",
        "sector": PersonnelProfile.SectorCategory.HEALTHCARE,
        "password1": "correct-horse-battery",
        "password2": "correct-horse-battery",
    }

    def test_registration_creates_personnel_unavailable_by_default(self):
        """Registering is not the same as being ready to work (ADR-009 D3).

        A new personnel must NOT be assignable until they opt in, or a
        coordinator could assign work to someone who never said they were free.
        """
        response = self.client.post(self.URL, self.VALID)

        self.assertRedirects(response, reverse("accounts:availability"))

        user = User.objects.get(email="nurse@example.com")
        self.assertEqual(user.role, User.Role.PERSONNEL)
        self.assertFalse(user.is_staff)
        self.assertEqual(
            user.personnel_profile.sector,
            PersonnelProfile.SectorCategory.HEALTHCARE,
        )
        self.assertEqual(
            user.personnel_profile.availability_status,
            PersonnelProfile.AvailabilityStatus.UNAVAILABLE,
        )

    def test_availability_cannot_be_set_during_registration(self):
        """The field is not on the form, so a crafted POST cannot reach it."""
        self.client.post(
            self.URL,
            {**self.VALID, "availability_status": "AVAILABLE"},
        )

        user = User.objects.get(email="nurse@example.com")
        self.assertEqual(
            user.personnel_profile.availability_status,
            PersonnelProfile.AvailabilityStatus.UNAVAILABLE,
        )


class AvailabilityViewTests(TestCase):
    URL = reverse("accounts:availability")

    def setUp(self):
        self.personnel = services.create_personnel(
            email="nurse@example.com",
            password="s3cret-pass",
            sector=PersonnelProfile.SectorCategory.HEALTHCARE,
        )

    def test_personnel_can_opt_in(self):
        self.client.force_login(self.personnel)

        response = self.client.post(
            self.URL,
            {"availability_status": PersonnelProfile.AvailabilityStatus.AVAILABLE},
            follow=True,
        )

        self.personnel.personnel_profile.refresh_from_db()
        self.assertEqual(
            self.personnel.personnel_profile.availability_status,
            PersonnelProfile.AvailabilityStatus.AVAILABLE,
        )
        self.assertContains(response, "Availability updated")

    def test_clients_cannot_reach_the_availability_page(self):
        """role_required: a client has no availability to set."""
        client_user = services.create_client(
            email="client@example.com",
            password="s3cret-pass",
            organization_name="Acme",
            phone_number="+15550000000",
        )
        self.client.force_login(client_user)

        response = self.client.get(self.URL)

        self.assertEqual(response.status_code, 403)

    def test_anonymous_visitors_are_redirected_to_login(self):
        """@login_required runs before @role_required, so no AttributeError."""
        response = self.client.get(self.URL)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)
