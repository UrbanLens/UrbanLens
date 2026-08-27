from django.conf import settings
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0030_v0_7_0"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AddIndex(
            model_name="album",
            index=models.Index(fields=["parent_pin"], name="idxdb_album_pin"),
        ),
        migrations.AddIndex(
            model_name="album",
            index=models.Index(fields=["parent_wiki"], name="idxdb_album_wiki"),
        ),
        migrations.AddIndex(
            model_name="albumitem",
            index=models.Index(fields=["album"], name="idxdb_albumitem_album"),
        ),
        migrations.AddIndex(
            model_name="albumitem",
            index=models.Index(fields=["image"], name="idxdb_albumitem_image"),
        ),
        migrations.AddIndex(
            model_name="image",
            index=models.Index(
                fields=["profile", "quota_exempt_reason"],
                name="idxdb_image_profile_quota",
            ),
        ),
        migrations.AddIndex(
            model_name="floorplan",
            index=models.Index(
                fields=["place", "profile", "valid_from"],
                name="idx_floorplan_place_owner_date",
            ),
        ),
        migrations.AddIndex(
            model_name="floorplanelement",
            index=models.Index(
                fields=["floorplan", "kind"], name="idx_floorplan_element_kind"
            ),
        ),
        migrations.AddIndex(
            model_name="floorplanmarker",
            index=models.Index(
                fields=["connector_id"], name="idx_floorplan_marker_connector"
            ),
        ),
        migrations.AddIndex(
            model_name="floorplanwall",
            index=models.Index(
                fields=["floor", "kind"], name="idx_floorplan_wall_kind"
            ),
        ),
    ]
