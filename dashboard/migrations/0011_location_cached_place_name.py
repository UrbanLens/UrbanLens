# Generated by Django 5.0.1 on 2024-03-22 17:26

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0010_remove_location_dashboard_l_name_11ee30_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="location",
            name="cached_place_name",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
