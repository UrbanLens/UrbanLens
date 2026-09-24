from django.db import migrations


def backfill(apps, schema_editor):
    from urbanlens.dashboard.services.auth.email_normalization import normalize_email

    Contact = apps.get_model("dashboard", "SafetyCheckinContact")
    changed = []
    for contact in Contact.objects.exclude(email__isnull=True).exclude(email="").only("pk", "email").iterator():
        contact.email_normalized = normalize_email(contact.email)
        changed.append(contact)
    Contact.objects.bulk_update(changed, ["email_normalized"], batch_size=1000)


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0064_safetycheckincontact_email_normalized")]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
