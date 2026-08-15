from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0043_label_unique_constraint"),
    ]

    operations = [
        migrations.AddField(
            model_name="mapimageoverlay",
            name="tile_url_template",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
    ]
