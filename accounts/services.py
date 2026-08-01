

from django.db import transaction

from .models import ClientProfile, CoordinatorProfile, PersonnelProfile, User


def create_client(email: str, password: str, organization_name: str, phone_number: str):
    """
    Create a Client user and a ClientProfile for the given user.
    """
    with transaction.atomic():
        user = User.objects.create_user(email=email, password=password, role=User.Role.CLIENT)
        
        client_profile = ClientProfile.objects.create(
            user=user,
            organization_name=organization_name,
            phone_number=phone_number
        )
    return user

def create_coordinator(email: str, password: str, department: str, region: str):
    """
    Create a Coordinator user and a CoordinatorProfile for the given user.
    """
    
    with transaction.atomic():
        user = User.objects.create_user(email=email, password=password, role=User.Role.COORDINATOR)
        
        coordinator_profile = CoordinatorProfile.objects.create(
            user=user,
            department=department,
            region=region
        )
    return user


def create_personnel(email: str, password: str, sector: str, availability_status=PersonnelProfile.AvailabilityStatus.UNAVAILABLE):
    """
    Create a Personnel user and a PersonnelProfile for the given user.
    """
    
    with transaction.atomic():
        user = User.objects.create_user(email=email, password=password, role=User.Role.PERSONNEL)
    
        personnel_profile = PersonnelProfile.objects.create(
            user=user,
            sector=sector,
            availability_status=availability_status
        )
    return user