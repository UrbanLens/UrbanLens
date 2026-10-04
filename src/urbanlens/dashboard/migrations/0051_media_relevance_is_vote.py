"""Let a media relevance mark be a private "remove from my results" rather than a vote (P233); every existing mark is a vote."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0050_location_cache_relevance_rule"),
    ]

    operations = [
        migrations.AddField(
            model_name="mediarelevance",
            name="is_vote",
            field=models.BooleanField(default=True),
        ),
        migrations.AddConstraint(
            model_name="mediarelevance",
            constraint=models.CheckConstraint(
                condition=models.Q(("is_vote", True), ("is_relevant", False), _connector="OR"),
                name="db_media_relevance_private_hides",
            ),
        ),
    ]
