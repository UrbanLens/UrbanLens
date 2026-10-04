"""Fold a burst of trip or wiki changes into one notification with a count (P197)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0052_owner_care_of"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationlog",
            name="fold_key",
            field=models.CharField(blank=True, default="", db_default="", max_length=64),
        ),
        migrations.AddField(
            model_name="notificationlog",
            name="fold_count",
            field=models.PositiveIntegerField(default=1, db_default=1),
        ),
        migrations.AddIndex(
            model_name="notificationlog",
            index=models.Index(
                condition=models.Q(("fold_key", ""), _negated=True),
                fields=["fold_key", "source_profile"],
                name="idxdb_notif_fold",
            ),
        ),
    ]
