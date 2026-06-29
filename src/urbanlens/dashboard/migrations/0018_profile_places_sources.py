from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0017_badge_allow_auto_tag_keywords"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="places_google_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Show Google historical landmarks in the Places layer.",
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="places_nps_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Show National Park Service locations in the Places layer.",
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="places_wikipedia_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Show Wikipedia-linked places in the Places layer.",
            ),
        ),
    ]
