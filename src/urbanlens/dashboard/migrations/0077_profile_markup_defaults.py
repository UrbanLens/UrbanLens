from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0076_pinmarkup_border_color_new_shape_types"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="markup_fill_color",
            field=models.CharField(default="#e53e3e", max_length=20),
        ),
        migrations.AddField(
            model_name="profile",
            name="markup_fill_opacity",
            field=models.IntegerField(default=87),
        ),
        migrations.AddField(
            model_name="profile",
            name="markup_border_color",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="profile",
            name="markup_border_opacity",
            field=models.IntegerField(default=100),
        ),
    ]
