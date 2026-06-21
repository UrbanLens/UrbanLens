from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0100_notificationlog_source_profile"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="search_provider",
            field=models.CharField(
                choices=[("brave", "Brave Search"), ("google", "Google Custom Search")],
                default="brave",
                help_text="Which web search provider to use for pin news/search results.",
                max_length=20,
                verbose_name="Search provider",
            ),
        ),
    ]
