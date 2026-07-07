import uuid

import django.db.models.deletion
from django.db import migrations, models


def backfill_primary_email_normalized(apps, schema_editor):
    from urbanlens.dashboard.services.email_normalization import normalize_email

    Profile = apps.get_model("dashboard", "Profile")
    updated = []
    for profile in Profile.objects.select_related("user").only("id", "primary_email_normalized", "user__email").iterator():
        normalized = normalize_email(profile.user.email) if profile.user.email else ""
        if normalized != profile.primary_email_normalized:
            profile.primary_email_normalized = normalized
            updated.append(profile)
    if updated:
        Profile.objects.bulk_update(updated, ["primary_email_normalized"], batch_size=500)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0002_emergencycontactdefault_route_safetycheckin_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="primary_email_normalized",
            field=models.CharField(blank=True, db_index=True, default="", max_length=254),
        ),
        migrations.CreateModel(
            name="ProfileEmail",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("email", models.EmailField(max_length=254)),
                ("normalized_email", models.CharField(db_index=True, max_length=254)),
                ("is_verified", models.BooleanField(default=False)),
                ("verification_token", models.UUIDField(default=uuid.uuid4, editable=False)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="secondary_emails",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "app_label": "dashboard",
                "ordering": ["created"],
                "abstract": False,
            },
        ),
        migrations.AddConstraint(
            model_name="profileemail",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_verified", True)),
                fields=("normalized_email",),
                name="uniq_verified_normalized_email",
            ),
        ),
        migrations.RunPython(backfill_primary_email_normalized, noop),
    ]
