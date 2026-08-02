from django.db import migrations

# Bootstrap vocabulary: without at least one RequestType a client cannot submit
# anything, so a fresh environment would deploy successfully and still be unusable.
# Seeded here rather than by hand so every environment (dev, CI, staging, prod)
# converges on the same starting set with no manual step.
#
# This is a ONE-TIME bootstrap, not ongoing management -- from here on, request
# types are added, re-worded and retired through the admin (ADR-010 D1). Do not
# edit this migration to change the vocabulary later.
SEED_REQUEST_TYPES = [
    {
        "code": "home-nursing-visit",
        "name": "Home Nursing Visit",
        "required_sector": "HEALTHCARE",
    },
    {
        "code": "patient-education-session",
        "name": "Patient Education Session",
        "required_sector": "EDUCATION",
    },
    {
        "code": "medical-transport",
        "name": "Medical Transport",
        "required_sector": "LOGISTICS",
    },
    {
        "code": "site-security-detail",
        "name": "Site Security Detail",
        "required_sector": "SECURITY",
    },
]


def seed_request_types(apps, schema_editor):
    # get_model, not a direct import: migrations run against the historical
    # state of the model, so importing servicing.models would break this
    # migration the moment RequestType gains a field.
    RequestType = apps.get_model("servicing", "RequestType")

    for request_type in SEED_REQUEST_TYPES:
        # get_or_create, not update_or_create: if a row with this code already
        # exists it was edited by an admin, and their wording wins over ours.
        RequestType.objects.get_or_create(
            code=request_type["code"],
            defaults={
                "name": request_type["name"],
                "required_sector": request_type["required_sector"],
            },
        )


def unseed_request_types(apps, schema_editor):
    # Reverse by code only, so request types added through the admin survive a
    # rollback. A seeded type already referenced by a ServiceRequest is PROTECTed
    # and will (correctly) refuse to be removed.
    RequestType = apps.get_model("servicing", "RequestType")

    RequestType.objects.filter(
        code__in=[request_type["code"] for request_type in SEED_REQUEST_TYPES]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("servicing", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_request_types, unseed_request_types),
    ]
