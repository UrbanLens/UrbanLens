from django.db import migrations, models

_AUTO_SOURCES = [
    ("openstreetmap", "OpenStreetMap"),
    ("epa_echo", "EPA ECHO"),
    ("wikipedia", "Wikipedia"),
    ("nrhp", "National Register of Historic Places"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0042_location_cache_drop_nearby_reference_documents"),
    ]

    operations = [
        migrations.AddField(
            model_name="pinlink",
            name="auto_source",
            field=models.CharField(blank=True, choices=_AUTO_SOURCES, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="wikilink",
            name="auto_source",
            field=models.CharField(blank=True, choices=_AUTO_SOURCES, default="", max_length=32),
        ),
    ]
