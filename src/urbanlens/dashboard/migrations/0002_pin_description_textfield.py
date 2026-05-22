from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="pin",
            name="description",
            field=models.TextField(blank=True, null=True),
        ),
    ]
