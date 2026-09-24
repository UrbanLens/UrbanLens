from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0070_profile_avatar_index"),
    ]

    operations = [
        migrations.CreateModel(
            name="RecordedWeatherDay",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("cell_lat", models.IntegerField()),
                ("cell_lng", models.IntegerField()),
                ("day", models.DateField()),
                ("data", models.JSONField(default=dict)),
            ],
            options={
                "db_table": "dashboard_recorded_weather_day",
                "abstract": False,
            },
        ),
    ]
