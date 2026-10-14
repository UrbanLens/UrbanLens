"""
Hand the link extraction and trivia generation tables to the AI plugin's app, which adopts them.

State only: the tables stay where they are until that app's first migration renames them, and are left alone where it
is not installed.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0085_photo_relevance_scores_leave_image"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(
                    name="LinkExtraction",
                ),
                migrations.DeleteModel(
                    name="TriviaGenerationAttempt",
                ),
            ],
        ),
    ]
