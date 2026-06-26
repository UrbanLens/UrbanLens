from django.contrib.postgres.indexes import GistIndex
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0004_remove_location_alarms_remove_location_cameras_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="pin",
            index=models.Index(
                fields=["profile", "parent_pin", "parent_location", "id"],
                name="dashboard_pin_map_page_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="pin",
            index=GistIndex(fields=["point"], name="dashboard_pin_point_gist"),
        ),
        migrations.AddIndex(
            model_name="review",
            index=models.Index(fields=["pin", "-created"], name="dashboard_review_pin_latest"),
        ),
    ]
