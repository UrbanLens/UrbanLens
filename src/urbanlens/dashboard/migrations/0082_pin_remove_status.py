from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0081_pin_statuses"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="pin",
            name="status",
        ),
    ]
