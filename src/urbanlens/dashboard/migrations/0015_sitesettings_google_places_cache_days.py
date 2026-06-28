from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0014_badge_allow_ai_profile_ai_prefs"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="google_places_cache_days",
            field=models.IntegerField(
                default=90,
                help_text="How many days to cache Google Places nearby-search results before re-fetching. Historical landmarks rarely change.",
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(365),
                ],
                verbose_name="Places layer cache (days)",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(
                condition=models.Q(google_places_cache_days__gte=1),
                name="google_places_cache_days_gte_1",
            ),
        ),
    ]
