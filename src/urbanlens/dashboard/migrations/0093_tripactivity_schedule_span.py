import datetime

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0092_clear_unschedulable_activity_times"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="tripactivity",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("scheduled_at__isnull", True),
                    models.Q(
                        ("scheduled_at__gte", datetime.datetime(1900, 1, 1, 0, 0, tzinfo=datetime.UTC)),
                        ("scheduled_at__lt", datetime.datetime(2200, 1, 1, 0, 0, tzinfo=datetime.UTC)),
                    ),
                    _connector="OR",
                ),
                name="db_ta_scheduled_at_span",
            ),
        ),
        migrations.AddConstraint(
            model_name="tripactivity",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("scheduled_end__isnull", True),
                    models.Q(
                        ("scheduled_end__gte", datetime.datetime(1900, 1, 1, 0, 0, tzinfo=datetime.UTC)),
                        ("scheduled_end__lt", datetime.datetime(2200, 1, 1, 0, 0, tzinfo=datetime.UTC)),
                    ),
                    _connector="OR",
                ),
                name="db_ta_scheduled_end_span",
            ),
        ),
    ]
