from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0077_profile_markup_defaults"),
    ]

    operations = [
        migrations.AddField(
            model_name="pinmarkup",
            name="fill_opacity",
            field=models.IntegerField(default=87),
        ),
        migrations.AddField(
            model_name="pinmarkup",
            name="border_opacity",
            field=models.IntegerField(default=100),
        ),
    ]
