"""Add ``Pin.subject_place``: the place a pin is about, which only ever picks among wikis its viewer can already see."""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0002_describe_seeded_labels"),
    ]

    operations = [
        migrations.AddField(
            model_name="pin",
            name="subject_place",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="subject_pins",
                to="dashboard.place",
            ),
        ),
    ]
