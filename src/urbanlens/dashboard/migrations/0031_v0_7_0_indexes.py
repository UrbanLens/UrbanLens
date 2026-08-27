from django.conf import settings
from django.db import migrations, models
import django.db.models.constraints
import django.db.models.functions.text

class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0030_v0_7_0"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
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
        migrations.AddConstraint(
            model_name="label",
            constraint=models.UniqueConstraint(
                django.db.models.functions.text.Lower("name"),
                models.F("profile"),
                models.F("kind"),
                name="uq_label_profile_name_kind_ci",
                nulls_distinct=False,
            ),
        ),
        migrations.AddConstraint(
            model_name="tripcalendarlink",
            constraint=models.UniqueConstraint(
                condition=models.Q(("google_event_id", ""), _negated=True),
                fields=("profile", "google_event_id"),
                name="db_tcl_profile_event_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="pinlink",
            constraint=models.UniqueConstraint(
                models.F("pin"),
                django.db.models.functions.text.MD5("url"),
                name="db_plink_pin_url_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="wikilink",
            constraint=models.UniqueConstraint(
                models.F("wiki"),
                django.db.models.functions.text.MD5("url"),
                name="db_wlink_wiki_url_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="floorplanfloor",
            constraint=models.UniqueConstraint(
                deferrable=django.db.models.constraints.Deferrable["DEFERRED"],
                fields=("floorplan", "level"),
                name="floorplan_floor_unique_level",
            ),
        ),
    ]
