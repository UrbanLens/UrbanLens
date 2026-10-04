"""An owner's care-of line, which county records and REData publish beside the mailing address (P229)."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0051_media_relevance_is_vote"),
    ]

    operations = [
        migrations.AddField(
            model_name="pinowner",
            name="care_of",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="wikiowner",
            name="care_of",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
    ]
