# Generated by Django 4.2.8 on 2024-01-17 14:53

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        (
            "dashboard",
            "0007_alter_review_options_alter_geocodedlocation_latitude_and_more",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="geocodedlocation",
            name="json_response",
            field=models.CharField(blank=True, max_length=10000, null=True),
        ),
        migrations.AddField(
            model_name="location",
            name="administrative_area_level_1",
            field=models.CharField(blank=True, max_length=30, null=True),
        ),
        migrations.AddField(
            model_name="location",
            name="administrative_area_level_2",
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name="location",
            name="administrative_area_level_3",
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name="location",
            name="country",
            field=models.CharField(default="United States", max_length=20),
        ),
        migrations.AddField(
            model_name="location",
            name="locality",
            field=models.CharField(blank=True, max_length=80, null=True),
        ),
        migrations.AddField(
            model_name="location",
            name="route",
            field=models.CharField(blank=True, max_length=80, null=True),
        ),
        migrations.AddField(
            model_name="location",
            name="street_number",
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name="location",
            name="zipcode",
            field=models.CharField(blank=True, max_length=10, null=True),
        ),
        migrations.AddField(
            model_name="location",
            name="zipcode_suffix",
            field=models.CharField(blank=True, max_length=10, null=True),
        ),
        migrations.AddIndex(
            model_name="geocodedlocation",
            index=models.Index(
                fields=["place_name"], name="dashboard_g_place_n_efe18b_idx"
            ),
        ),
    ]
