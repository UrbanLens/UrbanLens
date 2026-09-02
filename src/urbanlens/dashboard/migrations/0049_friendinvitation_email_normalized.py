"""Add FriendInvitation.email_normalized and backfill it for existing rows.

Mirrors ``Profile.primary_email_normalized`` (see ``0001_initial.py``'s
``backfill_primary_email_normalized``) - same reasoning: matching a pending
invitation at signup, and deduplicating a re-invite, both need to treat Gmail
dot/``+suffix`` variants as the same address, which a raw or
case-insensitive-only comparison against ``email`` can't do.
"""

from __future__ import annotations

from django.db import migrations, models


def noop(apps, schema_editor):
    pass


def backfill_friendinvitation_email_normalized(apps, schema_editor):
    from urbanlens.dashboard.services.auth.email_normalization import normalize_email

    FriendInvitation = apps.get_model("dashboard", "FriendInvitation")
    updated = []
    for invitation in FriendInvitation.objects.only("id", "email", "email_normalized").iterator():
        normalized = normalize_email(invitation.email) if invitation.email else ""
        if normalized != invitation.email_normalized:
            invitation.email_normalized = normalized
            updated.append(invitation)
    if updated:
        FriendInvitation.objects.bulk_update(updated, ["email_normalized"], batch_size=500)


class Migration(migrations.Migration):
    """Add FriendInvitation.email_normalized and backfill existing rows."""

    dependencies = [
        ("dashboard", "0048_image_upload_processed_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="friendinvitation",
            name="email_normalized",
            field=models.CharField(blank=True, db_index=True, default="", max_length=254),
        ),
        migrations.RunPython(backfill_friendinvitation_email_normalized, noop),
    ]
