# Generated manually (not via makemigrations - GDAL/PostGIS unavailable outside Docker).
#
# PinVisit inherited PublicDashboardModel (and its ``slug`` field) since commit
# ed4d18af without ever implementing the required ``_slugify_base()``, so any
# save() that needed to auto-generate a slug crashed with NotImplementedError.
# Visits have no slug-routed detail page anywhere in the app - they're always
# referenced by pk/uuid, nested under a pin. Dropping PinVisit down to
# FrontendDashboardModel (uuid, no slug) is the root-cause fix; this migration
# removes the now-unused column.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0007_remove_pin_db_pin_unique_location_per_profile_and_more"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="pinvisit",
            name="slug",
        ),
    ]
