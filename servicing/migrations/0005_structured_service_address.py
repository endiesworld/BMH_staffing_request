import django.core.validators
from django.db import migrations, models

# Replaces the single free-text `location` with a structured US address plus a
# contact number. Hand-written for the same reason as 0004: non-nullable
# columns make makemigrations prompt for a one-off default. The table is empty,
# so preserve_default=False keeps those defaults out of the model.


class Migration(migrations.Migration):

    dependencies = [
        ("servicing", "0004_request_slot_and_location"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="servicerequest",
            name="location",
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="address_line1",
            field=models.CharField(default="", max_length=255, verbose_name="address"),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="address_line2",
            field=models.CharField(
                blank=True, max_length=255, verbose_name="apartment, suite, etc."
            ),
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="city",
            field=models.CharField(default="", max_length=100),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="state",
            field=models.CharField(
                choices=[
                    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"),
                    ("AR", "Arkansas"), ("CA", "California"), ("CO", "Colorado"),
                    ("CT", "Connecticut"), ("DE", "Delaware"),
                    ("DC", "District of Columbia"), ("FL", "Florida"),
                    ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"),
                    ("IL", "Illinois"), ("IN", "Indiana"), ("IA", "Iowa"),
                    ("KS", "Kansas"), ("KY", "Kentucky"), ("LA", "Louisiana"),
                    ("ME", "Maine"), ("MD", "Maryland"), ("MA", "Massachusetts"),
                    ("MI", "Michigan"), ("MN", "Minnesota"), ("MS", "Mississippi"),
                    ("MO", "Missouri"), ("MT", "Montana"), ("NE", "Nebraska"),
                    ("NV", "Nevada"), ("NH", "New Hampshire"), ("NJ", "New Jersey"),
                    ("NM", "New Mexico"), ("NY", "New York"),
                    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"),
                    ("OK", "Oklahoma"), ("OR", "Oregon"), ("PA", "Pennsylvania"),
                    ("RI", "Rhode Island"), ("SC", "South Carolina"),
                    ("SD", "South Dakota"), ("TN", "Tennessee"), ("TX", "Texas"),
                    ("UT", "Utah"), ("VT", "Vermont"), ("VA", "Virginia"),
                    ("WA", "Washington"), ("WV", "West Virginia"),
                    ("WI", "Wisconsin"), ("WY", "Wyoming"),
                ],
                default="",
                max_length=2,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="postal_code",
            field=models.CharField(
                default="",
                max_length=10,
                validators=[
                    django.core.validators.RegexValidator(
                        message="Enter a ZIP code as 12345 or 12345-6789.",
                        regex="^\\d{5}(-\\d{4})?$",
                    )
                ],
                verbose_name="ZIP code",
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="country",
            field=models.CharField(
                choices=[("US", "United States")], default="US", max_length=2
            ),
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="contact_phone",
            field=models.CharField(
                default="",
                max_length=12,
                validators=[
                    django.core.validators.RegexValidator(
                        message="Enter a 10-digit US phone number.",
                        regex="^\\+1\\d{10}$",
                    )
                ],
            ),
            preserve_default=False,
        ),
    ]
