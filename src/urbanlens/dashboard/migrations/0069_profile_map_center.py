from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0068_alter_trip_allow_add_activities_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="map_center_latitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="map_center_longitude",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True),
        ),
    ]
