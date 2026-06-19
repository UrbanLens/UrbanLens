from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0087_backfill_missing_default_statuses"),
    ]

    operations = [
        migrations.AddField(
            model_name="location",
            name="statuses",
            field=models.ManyToManyField(
                blank=True,
                limit_choices_to={"kind": "status"},
                related_name="status_locations",
                to="dashboard.badge",
            ),
        ),
    ]
