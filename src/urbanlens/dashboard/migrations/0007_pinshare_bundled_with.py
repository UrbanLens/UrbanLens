"""Add PinShare.bundled_with - links child-pin shares to their bundle's root share."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0006_v0_4_0_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="pinshare",
            name="bundled_with",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="bundled_shares",
                to="dashboard.pinshare",
            ),
        ),
    ]
