import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0010_boundaryvote_consensusanswer_consensusprofile_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="boundary_cache_days",
            field=models.IntegerField(
                default=60,
                help_text=(
                    "How many days to cache a location's generated property/building boundary before refreshing it "
                    "in the background (REData, Overpass, Overture, Microsoft, Google). Parcel geometry rarely "
                    "changes, and REData already caches upstream, so a long cache avoids unnecessary provider "
                    "calls. A stale boundary is still served immediately while the refresh runs in the background."
                ),
                validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(365)],
                verbose_name="Boundary cache (days)",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(condition=models.Q(("boundary_cache_days__gte", 1)), name="boundary_cache_days_gte_1"),
        ),
    ]
