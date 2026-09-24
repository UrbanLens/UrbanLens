from django.db import migrations
from django.db.models import Count, F


def release_stale_and_duplicate_proofs(apps, schema_editor):
    Profile = apps.get_model("dashboard", "Profile")
    Profile.objects.exclude(verified_primary_email="").exclude(verified_primary_email=F("primary_email_normalized")).update(verified_primary_email="")
    duplicated = Profile.objects.exclude(verified_primary_email="").values("verified_primary_email").annotate(n=Count("pk")).filter(n__gt=1).values_list("verified_primary_email", flat=True)
    for address in list(duplicated):
        holders = Profile.objects.filter(verified_primary_email=address).order_by("-user__is_active", "pk")
        keep = holders.values_list("pk", flat=True).first()
        holders.exclude(pk=keep).update(verified_primary_email="")


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0066_safetycheckincontact_email_normalized_index")]

    operations = [migrations.RunPython(release_stale_and_duplicate_proofs, migrations.RunPython.noop)]
