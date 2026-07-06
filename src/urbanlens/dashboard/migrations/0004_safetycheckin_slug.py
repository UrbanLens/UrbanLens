from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0003_alter_safetypreference_default_message"),
    ]

    operations = [
        migrations.AddField(
            model_name="safetycheckin",
            name="slug",
            field=models.SlugField(max_length=255, null=True, blank=True),
        ),
    ]
