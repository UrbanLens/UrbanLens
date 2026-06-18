from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0078_pinmarkup_opacity_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="pin",
            name="detail_bg_color",
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name="pin",
            name="detail_bg_opacity",
            field=models.IntegerField(default=80),
        ),
        migrations.AddField(
            model_name="pin",
            name="detail_border_color",
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name="pin",
            name="detail_border_opacity",
            field=models.IntegerField(default=100),
        ),
    ]
