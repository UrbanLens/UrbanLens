from django.db import migrations, models

_AUTO_SOURCES = [
    ("openstreetmap", "OpenStreetMap"),
    ("epa_echo", "EPA ECHO"),
    ("wikipedia", "Wikipedia"),
    ("nrhp", "National Register of Historic Places"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0044_session_participant_departure"),
    ]

    operations = [
        migrations.AddField(
            model_name="pinlink",
            name="auto_source",
            field=models.CharField(blank=True, choices=_AUTO_SOURCES, default="", db_default="", max_length=32),
        ),
        migrations.AddField(
            model_name="wikilink",
            name="auto_source",
            field=models.CharField(blank=True, choices=_AUTO_SOURCES, default="", db_default="", max_length=32),
        ),
    ]
