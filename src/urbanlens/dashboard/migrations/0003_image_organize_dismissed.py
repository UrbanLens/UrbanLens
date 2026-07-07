from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0002_remove_visitsuggestion_db_visit_suggestion_exactly_one_origin_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="image",
            name="organize_dismissed",
            field=models.BooleanField(default=False),
        ),
    ]
