from django.db import migrations

# Coordinators must be able to SEE assignments, not just make them: knowing who
# a request went to is a pull question ("show me the queue"), which no
# notification answers. Added here rather than by editing 0003, which has
# already been applied everywhere.
COORDINATOR_GROUP = "COORDINATOR"
ADDED_PERMISSIONS = [("servicing", "view_assignment")]


def grant(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    group = Group.objects.filter(name=COORDINATOR_GROUP).first()
    if group is None:
        # Nothing to grant to. 0003 creates it, so this only happens if someone
        # deleted the group by hand.
        return

    for app_label, codename in ADDED_PERMISSIONS:
        permission = Permission.objects.filter(
            content_type__app_label=app_label, codename=codename
        ).first()
        if permission is None:
            raise RuntimeError(f"Missing permission {app_label}.{codename}")
        group.permissions.add(permission)


def revoke(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    group = Group.objects.filter(name=COORDINATOR_GROUP).first()
    if group is None:
        return

    for app_label, codename in ADDED_PERMISSIONS:
        permission = Permission.objects.filter(
            content_type__app_label=app_label, codename=codename
        ).first()
        if permission is not None:
            group.permissions.remove(permission)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_coordinator_group"),
        # The Assignment model must exist before its permissions do.
        ("servicing", "0006_assignment"),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
