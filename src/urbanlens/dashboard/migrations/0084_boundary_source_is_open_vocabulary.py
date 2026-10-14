"""A boundary candidate's source is whatever the provider that produced it names, so the column no longer lists choices."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0083_boundary_cache_help_text"),
    ]

    operations = [
        migrations.AlterField(
            model_name="boundary",
            name="source",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
    ]
