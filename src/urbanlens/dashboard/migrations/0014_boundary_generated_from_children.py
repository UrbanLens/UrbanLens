from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0013_fix_visit_suggestion_origin_cascade"),
    ]

    operations = [
        migrations.AddField(
            model_name="boundary",
            name="generated_from_children",
            field=models.BooleanField(
                default=False,
                help_text="Marks a pin boundary that may be automatically refitted as its child pins change.",
            ),
        ),
    ]
