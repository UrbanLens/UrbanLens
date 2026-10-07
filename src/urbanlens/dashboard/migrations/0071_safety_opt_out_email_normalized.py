"""Match safety contact opt-outs on the normalized address, so any spelling of an opted-out mailbox stays opted out."""

from django.db import migrations, models


def _backfill(apps, schema_editor):
    from urbanlens.dashboard.services.auth.email_normalization import normalize_email

    OptOut = apps.get_model("dashboard", "SafetyContactOptOut")
    changed = []
    for opt_out in OptOut.objects.exclude(email__isnull=True).exclude(email="").only("pk", "email").iterator():
        opt_out.email_normalized = normalize_email(opt_out.email)
        changed.append(opt_out)
    OptOut.objects.bulk_update(changed, ["email_normalized"], batch_size=1000)


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0070_withdraw_year_built_trivia"),
    ]

    operations = [
        migrations.AddField(
            model_name="safetycontactoptout",
            name="email_normalized",
            field=models.CharField(blank=True, db_default="", default="", max_length=254),
        ),
        migrations.RunPython(_backfill, migrations.RunPython.noop),
        migrations.RemoveIndex(
            model_name="safetycontactoptout",
            name="idxdb_scoo_email",
        ),
        migrations.AddIndex(
            model_name="safetycontactoptout",
            index=models.Index(fields=["email_normalized"], name="idxdb_scoo_email_normalized"),
        ),
    ]
