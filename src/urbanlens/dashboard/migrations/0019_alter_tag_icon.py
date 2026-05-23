from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0018_profile_dark_mode"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tag",
            name="icon",
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
    ]
