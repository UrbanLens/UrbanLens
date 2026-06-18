from django.db import migrations


def clear_stale_centroids(apps, schema_editor):
    """Erase cached pin centroids computed by the old naive-averaging algorithm.

    The centroid is now computed via density-based clustering, which returns the
    centre of the largest geographic cluster rather than a straight average across
    all pins (which often lands in the ocean for intercontinental collections).

    Clearing the cache forces every profile to recompute on the next map load
    using the new algorithm.
    """
    Profile = apps.get_model("dashboard", "Profile")
    Profile.objects.update(map_center_latitude=None, map_center_longitude=None)


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0072_profile_map_center_mode_default_gps"),
    ]

    operations = [
        migrations.RunPython(clear_stale_centroids, migrations.RunPython.noop),
    ]
