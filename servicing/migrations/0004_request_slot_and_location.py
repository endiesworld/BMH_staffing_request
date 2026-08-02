import datetime

from django.db import migrations, models
from django.utils import timezone

# Hand-written rather than prompted for: adding non-nullable columns makes
# makemigrations ask for a one-off default interactively. The table is empty,
# so the defaults below are never actually applied to any row --
# preserve_default=False keeps them out of the model afterwards.
#
# `title` goes because it duplicated `description`: with request type, slot,
# duration and location all structured, a row identifies itself and the free
# text is for detail.


class Migration(migrations.Migration):

    dependencies = [
        ("servicing", "0003_servicerequest"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="servicerequest",
            name="title",
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="scheduled_start",
            field=models.DateTimeField(default=timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="expected_duration",
            field=models.DurationField(default=datetime.timedelta(hours=1)),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="servicerequest",
            name="location",
            field=models.CharField(default="", max_length=255),
            preserve_default=False,
        ),
    ]
