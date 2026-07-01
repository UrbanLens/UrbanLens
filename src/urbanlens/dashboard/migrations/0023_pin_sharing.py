"""Add one-to-one pin sharing between friends."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0022_pin_name_is_user_provided"),
    ]

    operations = [
        migrations.CreateModel(
            name="PinShare",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("accepted", "Accepted"), ("rejected", "Rejected"), ("already_pinned", "Already pinned")], default="pending", max_length=20)),
                ("from_profile", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sent_pin_shares", to="dashboard.profile")),
                ("notification", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="pin_share", to="dashboard.notificationlog")),
                ("pin", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="shares", to="dashboard.pin")),
                ("to_profile", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="received_pin_shares", to="dashboard.profile")),
            ],
            options={
                "db_table": "dashboard_pin_shares",
                "get_latest_by": "updated",
                "abstract": False,
            },
        ),
        migrations.AddIndex(model_name="pinshare", index=models.Index(fields=["to_profile", "status"], name="dashboard_p_to_prof_d9fbff_idx")),
        migrations.AddIndex(model_name="pinshare", index=models.Index(fields=["from_profile", "created"], name="dashboard_p_from_pr_27c1ce_idx")),
        migrations.AddConstraint(
            model_name="pinshare",
            constraint=models.UniqueConstraint(condition=models.Q(("status", "pending")), fields=("pin", "to_profile"), name="dashboard_pin_share_one_pending_per_pin_user"),
        ),
    ]
