from django.db import migrations

# ADR-006's role <-> Group sync, minimal first slice: the COORDINATOR group and
# the permissions it needs to work requests in the admin.
#
# The group is named after the Role value so the mapping stays mechanical --
# User.Role.COORDINATOR -> Group "COORDINATOR".
COORDINATOR_GROUP = "COORDINATOR"

# Approving is a change, so the coordinator needs change_servicerequest even
# though the admin renders the record read-only: the approve action declares
# permissions=["change"], and the changelist itself needs view.
COORDINATOR_PERMISSIONS = [
    ("servicing", "view_servicerequest"),
    ("servicing", "change_servicerequest"),
    ("servicing", "view_requesttype"),
]


def _ensure_permissions_exist(apps):
    """Create the auth.Permission rows this migration is about to look up.

    Permissions are normally created by a post_migrate signal, which has not
    fired yet while migrations are still running. On a fresh database the rows
    would not exist, so we create them early. Safe to call repeatedly.
    """
    from django.apps import apps as global_apps
    from django.contrib.auth.management import create_permissions

    for app_config in global_apps.get_app_configs():
        app_config.models_module = True
        create_permissions(app_config, apps=global_apps, verbosity=0)
        app_config.models_module = None


def create_coordinator_group(apps, schema_editor):
    _ensure_permissions_exist(apps)

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    User = apps.get_model("accounts", "User")

    group, _ = Group.objects.get_or_create(name=COORDINATOR_GROUP)

    permissions = []
    for app_label, codename in COORDINATOR_PERMISSIONS:
        permission = Permission.objects.filter(
            content_type__app_label=app_label, codename=codename
        ).first()
        if permission is None:
            # Fail loudly: a silently under-powered group means a coordinator
            # logs in to an empty admin and nobody knows why.
            raise RuntimeError(f"Missing permission {app_label}.{codename}")
        permissions.append(permission)

    group.permissions.set(permissions)

    # Backfill: coordinators provisioned before this migration have neither
    # admin access nor the group.
    coordinators = User.objects.filter(role="COORDINATOR")
    coordinators.update(is_staff=True)
    for coordinator in coordinators:
        coordinator.groups.add(group)


def delete_coordinator_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("accounts", "User")

    User.objects.filter(role="COORDINATOR").update(is_staff=False)
    Group.objects.filter(name=COORDINATOR_GROUP).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_clientprofile_coordinatorprofile_personnelprofile"),
        ("auth", "0012_alter_user_first_name_max_length"),
        # The permissions being granted belong to servicing's models, so those
        # tables and content types must exist first.
        ("servicing", "0003_servicerequest"),
    ]

    operations = [
        migrations.RunPython(create_coordinator_group, delete_coordinator_group),
    ]
