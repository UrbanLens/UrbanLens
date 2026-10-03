"""Drop cached searches that may have been built from a pin owner's own names (P188).

Every viewer of a Location read these rows, whoever's names built them. From 0039 on, a search built from a pin's own
names is cached under that name set's audience, and the shared row is built from public names only. The rows are a
cache: each source fetches again the next time a page asks for it.
"""

from django.db import migrations

NAME_BUILT_SOURCES = (
    "web_search",
    "flickr",
    "searxng_images",
    "gdelt_v2",
    "smithsonian",
    "wikimedia",
    "library_of_congress",
    "digital_commonwealth",
    "internet_archive",
    "chronicling_america",
)


def drop_name_built_searches(apps, schema_editor):
    LocationCache = apps.get_model("dashboard", "LocationCache")
    LocationCache.objects.filter(source__in=NAME_BUILT_SOURCES).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0037_spotguessr_participant_left"),
    ]

    operations = [
        migrations.RunPython(drop_name_built_searches, migrations.RunPython.noop),
    ]
