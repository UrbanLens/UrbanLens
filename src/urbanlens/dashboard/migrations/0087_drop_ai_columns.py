"""
Drop the site settings, profile and trivia columns that now belong to a plugin.

The plugin's data migration copies them into its own tables before this runs.
"""

from django.db import migrations, models

import urbanlens.dashboard.models.trivia.model


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0086_hand_ai_tables_to_plugin"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="profile",
            name="ai_enabled",
        ),
        migrations.RemoveField(
            model_name="profile",
            name="ai_label_categories",
        ),
        migrations.RemoveField(
            model_name="profile",
            name="ai_label_statuses",
        ),
        migrations.RemoveField(
            model_name="profile",
            name="ai_label_tags",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="ai_article_expansion_enabled",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="ai_article_safety_enabled",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="ai_category_suggestions_enabled",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="ai_document_import_enabled",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="ai_document_import_max_chars",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="ai_enabled",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="ai_link_extraction_daily_limit",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="ai_link_extraction_enabled",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="ai_provider",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="ai_trip_suggestions_enabled",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="ai_trivia_answer_check_enabled",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="ai_trivia_generation_enabled",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="ai_trivia_moderation_enabled",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="ai_trivia_wiki_incorporation_enabled",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="anthropic_model",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="cloudflare_model",
        ),
        migrations.RemoveField(
            model_name="sitesettings",
            name="openai_model",
        ),
        migrations.RemoveField(
            model_name="triviaquestion",
            name="wiki_incorporated_at",
        ),
        migrations.AlterField(
            model_name="triviaanswer",
            name="matched_via",
            field=models.CharField(choices=urbanlens.dashboard.models.trivia.model.answer_match_kind_choices, default="exact", max_length=10),
        ),
        migrations.AlterField(
            model_name="triviaquestion",
            name="source",
            field=models.CharField(choices=urbanlens.dashboard.models.trivia.model.question_source_choices, max_length=20),
        ),
    ]
