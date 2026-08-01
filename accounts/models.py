from django.db import models
from django.contrib.auth.models import AbstractUser, BaseUserManager

from django.conf import settings

# Create your models here.    



class UserManager(BaseUserManager):
    
    def create_user(self, email, password, **extra_fields):
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)

        user.set_password(password)
        user.save(using=self._db)

        return user
    
    
    def create_superuser(self, email, password, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        
        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self.create_user(email, password, **extra_fields)
    

class User(AbstractUser):
    
    class Role(models.TextChoices):
        CLIENT = 'CLIENT', 'Client'
        COORDINATOR = 'COORDINATOR', 'Coordinator'
        PERSONNEL = 'PERSONNEL', 'Personnel'

    username = None
    email = models.EmailField(unique=True)
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CLIENT)
    
    objects = UserManager()
    

class ClientProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE, 
        related_name='client_profile'
        )
    # Add any additional fields specific to ClientProfile here
    organization_name = models.CharField(max_length=255, blank=False, null=False)
    phone_number = models.CharField(max_length=20, blank=False, null=False)
    
    def __str__(self):
        return f"ClientProfile for {self.user.email}"
    
    
class CoordinatorProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE, 
        related_name='coordinator_profile'
        )
    # Add any additional fields specific to CoordinatorProfile here
    department = models.CharField(max_length=255, blank=False, null=False)
    region = models.CharField(max_length=255, blank=False, null=False)
    
    def __str__(self):
        return f"CoordinatorProfile for {self.user.email}"
    

class PersonnelProfile(models.Model):
    class AvailabilityStatus(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        UNAVAILABLE = "UNAVAILABLE", "Unavailable"
        ON_SHIFT = "ON_SHIFT", "On shift"
        ON_LEAVE = "ON_LEAVE", "On leave"
    
    class SectorCategory(models.TextChoices):
        HEALTHCARE = "HEALTHCARE", "Healthcare"
        EDUCATION = "EDUCATION", "Education"
        LOGISTICS = "LOGISTICS", "Logistics"
        SECURITY = "SECURITY", "Security"
        
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE, 
        related_name='personnel_profile'
        )
    # Add any additional fields specific to PersonnelProfile here
    sector = models.CharField(
        max_length=20,
        choices=SectorCategory.choices,
        blank=False,
        null=False,
    )
    availability_status = models.CharField(
        max_length=20,
        choices=AvailabilityStatus.choices,
        blank=False,
        null=False,
        default=AvailabilityStatus.UNAVAILABLE
    )
    
    def __str__(self):
        return f"PersonnelProfile for {self.user.email}"