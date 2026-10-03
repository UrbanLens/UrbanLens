"""A grant outlives the admin who made it (P246), and records who revoked it (P245)."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("dashboard", "0047_cris_site_rows_refetch"),
    ]

    operations = [
        migrations.AlterField(
            model_name="usersubscription",
            name="granted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="granted_subscriptions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="usersubscription",
            name="revoked_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="revoked_subscriptions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
