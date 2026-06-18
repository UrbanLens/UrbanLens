from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0079_pin_detail_circle_style"),
    ]

    operations = [
        migrations.AddField(
            model_name="badge",
            name="is_protected",
            field=models.BooleanField(default=False),
        ),
    ]
