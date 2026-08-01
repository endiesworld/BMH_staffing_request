from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from .models import ClientProfile, CoordinatorProfile, PersonnelProfile, User


# --- Forms: "add a user" vs "edit a user" are different situations ---

class CustomUserCreationForm(UserCreationForm):
    """The 'add user' form. Collects email + role + two password boxes."""
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("email", "role")


class CustomUserChangeForm(UserChangeForm):
    """The 'edit user' form. Shows the password as a read-only hash, never editable text."""
    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"


# --- Profile inlines: one per role, edited right on the user's own page ---

class ClientProfileInline(admin.StackedInline):
    model = ClientProfile
    can_delete = False
    max_num = 1
    extra = 1
    verbose_name_plural = "Client profile"


class CoordinatorProfileInline(admin.StackedInline):
    model = CoordinatorProfile
    can_delete = False
    max_num = 1
    extra = 1
    verbose_name_plural = "Coordinator profile"


class PersonnelProfileInline(admin.StackedInline):
    model = PersonnelProfile
    can_delete = False
    max_num = 1
    extra = 1
    verbose_name_plural = "Personnel profile"


# --- The password-aware admin, adjusted for email login + our role field ---

class CustomUserAdmin(BaseUserAdmin):
    add_form = CustomUserCreationForm
    form = CustomUserChangeForm
    model = User

    ordering = ("email",)
    search_fields = ("email",)
    list_display = ("email", "role", "is_staff", "is_active")
    list_filter = ("role", "is_staff", "is_superuser", "is_active")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name")}),
        ("Role", {"fields": ("role",)}),
        (
            "Permissions",
            {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")},
        ),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "role", "password1", "password2"),
            },
        ),
    )

    # Map each role to the single profile inline it should show.
    _role_inlines = {
        User.Role.CLIENT: [ClientProfileInline],
        User.Role.COORDINATOR: [CoordinatorProfileInline],
        User.Role.PERSONNEL: [PersonnelProfileInline],
    }

    def get_inlines(self, request, obj=None):
        # On the ADD page (obj is None) the role isn't saved yet, so show no inline.
        # On the EDIT page, show only the inline matching this user's role.
        if obj is None:
            return []
        return self._role_inlines.get(obj.role, [])


admin.site.register(User, CustomUserAdmin)
admin.site.register(ClientProfile)
admin.site.register(CoordinatorProfile)
admin.site.register(PersonnelProfile)
