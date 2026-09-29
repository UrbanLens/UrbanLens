from django.db import migrations

# The row was seeded at one call a minute while its own note says one a second, Nominatim's published limit.
_OLD = {"calls_per_minute": 1, "min_interval_seconds": None}
_NEW = {"calls_per_minute": 60, "min_interval_seconds": 1.0}


def forwards(apps, schema_editor):
    apps.get_model("dashboard", "ApiRateLimit").objects.filter(service="nominatim", **_OLD).update(**_NEW)


def backwards(apps, schema_editor):
    apps.get_model("dashboard", "ApiRateLimit").objects.filter(service="nominatim", **_NEW).update(**_OLD)


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0112_sitesettings_max_group_chats_per_user")]

    operations = [migrations.RunPython(forwards, backwards)]
