from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0038_location_cache_drop_name_built_searches"),
    ]

    operations = [
        migrations.AddField(
            model_name="locationcache",
            name="audience",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AlterUniqueTogether(
            name="locationcache",
            unique_together={("location", "source", "audience")},
        ),
    ]
