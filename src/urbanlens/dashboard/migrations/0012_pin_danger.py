# Generated manually - add per-pin danger rating (1-5 stars, 0 = unset).

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0011_public_uuids"),
    ]

    operations = [
        migrations.AddField(
            model_name="pin",
            name="danger",
            field=models.IntegerField(default=0),
        ),
    ]
