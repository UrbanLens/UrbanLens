from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0080_badge_is_protected"),
    ]

    operations = [
        migrations.AddField(
            model_name="pin",
            name="statuses",
            field=models.ManyToManyField(
                blank=True,
                limit_choices_to={"kind": "status"},
                related_name="status_pins",
                to="dashboard.badge",
            ),
        ),
    ]
