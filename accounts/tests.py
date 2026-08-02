from unittest.mock import patch

from django.test import TestCase

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
