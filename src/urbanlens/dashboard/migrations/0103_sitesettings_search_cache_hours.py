from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0102_sitesettings_search_provider"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="search_cache_hours",
            field=models.IntegerField(
                default=24,
                help_text="How many hours to cache web search results per pin before re-fetching. Set to 0 to disable caching.",
                verbose_name="Search cache duration (hours)",
            ),
        ),
    ]
