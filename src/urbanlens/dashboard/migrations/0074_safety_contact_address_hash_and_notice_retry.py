"""Keep a keyed hash of an archived contact's address for its opt-out link, and record a failed end-of-check-in email."""

from django.db import migrations, models
from django.db.models import OuterRef, Subquery


def _settle_earlier_resolutions(apps, schema_editor):
    """Mark contacts of check-ins already resolved as told, so the new retry sweep only finishes notices it began.

    Without this, a deploy would send contacts of a check-in resolved in the hour before it a notice the old code
    already sent them, or one hours late.
    """
    SafetyCheckin = apps.get_model("dashboard", "SafetyCheckin")
    SafetyCheckinContact = apps.get_model("dashboard", "SafetyCheckinContact")
    resolved_at = SafetyCheckin.objects.filter(pk=OuterRef("checkin_id")).values("resolved_at")[:1]
    SafetyCheckinContact.objects.filter(notified_at__isnull=False, resolution_notified_at__isnull=True, checkin__resolved_at__isnull=False).update(resolution_notified_at=Subquery(resolved_at))


def _drop_hash_only_opt_outs(apps, schema_editor):
    """Before the reverse restores the old exactly-one-of(profile, email) constraint, drop the rows it cannot hold."""
    SafetyContactOptOut = apps.get_model("dashboard", "SafetyContactOptOut")
    SafetyContactOptOut.objects.filter(contact_profile__isnull=True, email__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0073_added_calendar_columns_database_defaults"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="safetycontactoptout",
            name="db_safety_contact_optout_exactly_one_target",
        ),
        migrations.RemoveConstraint(
            model_name="safetycontactoptout",
            name="uq_safety_contact_optout_target_scope",
        ),
        migrations.AddField(
            model_name="safetycheckincontact",
            name="email_hmac",
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
        migrations.AddField(
            model_name="safetycheckincontact",
            name="resolution_email_failed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="safetycontactoptout",
            name="email_hmac",
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
        migrations.AddConstraint(
            model_name="safetycontactoptout",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("contact_profile__isnull", False), ("email__isnull", True), ("email_hmac", "")),
                    models.Q(("contact_profile__isnull", True), ("email__isnull", False), ("email_hmac", "")),
                    models.Q(("contact_profile__isnull", True), ("email__isnull", True), models.Q(("email_hmac", ""), _negated=True)),
                    _connector="OR",
                ),
                name="db_safety_contact_optout_exactly_one_target",
            ),
        ),
        migrations.AddConstraint(
            model_name="safetycontactoptout",
            constraint=models.UniqueConstraint(models.F("contact_profile"), models.F("email"), models.F("email_hmac"), models.F("scope"), models.F("owner"), models.F("checkin"), name="uq_safety_contact_optout_target_scope", nulls_distinct=False),
        ),
        migrations.RunPython(_settle_earlier_resolutions, migrations.RunPython.noop),
        migrations.RunPython(migrations.RunPython.noop, _drop_hash_only_opt_outs),
        # Index creation goes dead last.
        migrations.AddIndex(
            model_name="safetycontactoptout",
            index=models.Index(fields=["email_hmac"], name="idxdb_scoo_email_hmac"),
        ),
    ]
