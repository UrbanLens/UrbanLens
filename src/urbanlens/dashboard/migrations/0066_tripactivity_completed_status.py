from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0065_profile_hide_pin_locations_in_trips"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tripactivity",
            name="status",
            field=models.CharField(
                choices=[
                    ("proposed", "Proposed"),
                    ("confirmed", "Confirmed"),
                    ("completed", "Completed"),
                ],
                default="proposed",
                max_length=20,
            ),
        ),
    ]
