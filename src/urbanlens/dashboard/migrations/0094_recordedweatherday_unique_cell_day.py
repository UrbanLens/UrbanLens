from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0093_tripactivity_schedule_span"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="recordedweatherday",
            constraint=models.UniqueConstraint(fields=("cell_lat", "cell_lng", "day"), name="db_weatherday_cell_day_uniq"),
        ),
    ]
