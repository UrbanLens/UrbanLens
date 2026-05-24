from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0032_pinnote"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="default_map_view",
            field=models.CharField(
                choices=[("street", "Street"), ("satellite", "Satellite"), ("topographic", "Topographic")],
                default="satellite",
                max_length=20,
            ),
        ),
    ]
