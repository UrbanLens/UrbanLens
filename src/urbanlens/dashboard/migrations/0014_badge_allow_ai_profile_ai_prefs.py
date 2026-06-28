from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0013_location_cache"),
    ]

    operations = [
        migrations.AddField(
            model_name="badge",
            name="allow_ai",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="ai_enabled",
            field=models.BooleanField(default=True, help_text="Allow AI features on your account."),
        ),
        migrations.AddField(
            model_name="profile",
            name="ai_badge_tags",
            field=models.BooleanField(default=True, help_text="AI can automatically suggest and add tags when a pin is created."),
        ),
        migrations.AddField(
            model_name="profile",
            name="ai_badge_categories",
            field=models.BooleanField(default=True, help_text="AI can automatically suggest and add categories when a pin is created."),
        ),
        migrations.AddField(
            model_name="profile",
            name="ai_badge_statuses",
            field=models.BooleanField(default=True, help_text="AI can automatically suggest and add statuses when a pin is created."),
        ),
    ]
