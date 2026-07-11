# v0.4.0 upgrade, part 3 of 4: destructive drops and NOT NULL enforcement.
# Every column dropped here was either migrated into its new home by the 0003
# backfills or deliberately retired. Runs in its own transaction with no
# RunPython, so PostgreSQL's pending-trigger-event restriction cannot bite.
# Also installs the Location coordinate-freeze trigger (RunSQL - makemigrations
# cannot regenerate it; carried over from the abandoned chain in old/).
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0003_v0_4_0_data"),
    ]

    operations = [
        # --- Reviews: legacy user FK out, backfilled profile FK required.
        migrations.AlterUniqueTogether(
            name="review",
            unique_together=set(),
        ),
        migrations.RemoveField(
            model_name="review",
            name="user",
        ),
        migrations.RemoveField(
            model_name="review",
            name="review",
        ),
        migrations.AlterField(
            model_name="review",
            name="profile",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="reviews",
                to="dashboard.profile",
            ),
        ),
        # --- Pins: location becomes the single source of place identity.
        migrations.AlterField(
            model_name="pin",
            name="location",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="pins",
                to="dashboard.location",
            ),
        ),
        migrations.RemoveField(
            model_name="pin",
            name="administrative_area_level_1",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="administrative_area_level_2",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="administrative_area_level_3",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="country",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="google_place",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="is_private",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="latitude",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="locality",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="longitude",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="official_name",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="parent_location",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="point",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="route",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="street_number",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="zipcode",
        ),
        migrations.RemoveField(
            model_name="pin",
            name="zipcode_suffix",
        ),
        # --- Markup: snapshot blobs and legacy parents were converted in 0003.
        migrations.RemoveField(
            model_name="pinmarkup",
            name="parent_location",
        ),
        migrations.RemoveField(
            model_name="pinmarkup",
            name="parent_safety_checkin",
        ),
        migrations.RemoveField(
            model_name="comment",
            name="location",
        ),
        migrations.RemoveField(
            model_name="comment",
            name="map_data",
        ),
        migrations.RemoveField(
            model_name="tripcomment",
            name="map_data",
        ),
        # --- Site settings: replaced by external_data_cache_days.
        migrations.RemoveField(
            model_name="sitesettings",
            name="search_cache_hours",
        ),
        # --- Location: community fields moved onto Wiki in 0003.
        migrations.RemoveField(
            model_name="location",
            name="alarms",
        ),
        migrations.RemoveField(
            model_name="location",
            name="badges",
        ),
        migrations.RemoveField(
            model_name="location",
            name="cameras",
        ),
        migrations.RemoveField(
            model_name="location",
            name="date_abandoned",
        ),
        migrations.RemoveField(
            model_name="location",
            name="date_last_active",
        ),
        migrations.RemoveField(
            model_name="location",
            name="description",
        ),
        migrations.RemoveField(
            model_name="location",
            name="fences",
        ),
        migrations.RemoveField(
            model_name="location",
            name="locked",
        ),
        migrations.RemoveField(
            model_name="location",
            name="name",
        ),
        migrations.RemoveField(
            model_name="location",
            name="plywood",
        ),
        migrations.RemoveField(
            model_name="location",
            name="security",
        ),
        migrations.RemoveField(
            model_name="location",
            name="signs",
        ),
        migrations.RemoveField(
            model_name="location",
            name="vps",
        ),
        # --- Retired models (data converted in 0003: Campus -> Boundary,
        #     LocationAlias -> WikiAlias, LocationEdit -> WikiEdit).
        migrations.RemoveField(
            model_name="campus",
            name="location",
        ),
        migrations.RemoveField(
            model_name="campus",
            name="pin",
        ),
        migrations.RemoveField(
            model_name="campus",
            name="profile",
        ),
        migrations.RemoveField(
            model_name="locationalias",
            name="created_by",
        ),
        migrations.RemoveField(
            model_name="locationalias",
            name="location",
        ),
        migrations.RemoveField(
            model_name="locationedit",
            name="editor",
        ),
        migrations.RemoveField(
            model_name="locationedit",
            name="location",
        ),
        migrations.RemoveField(
            model_name="locationedit",
            name="reverted_by",
        ),
        migrations.DeleteModel(
            name="Campus",
        ),
        migrations.DeleteModel(
            name="LocationAlias",
        ),
        migrations.DeleteModel(
            name="LocationEdit",
        ),
        # --- Location coordinates are immutable after insert; enforced at the
        #     DB level so no code path can drift a shared Location.
        migrations.RunSQL(
            sql="""
CREATE OR REPLACE FUNCTION dashboard_locations_freeze_identity() RETURNS trigger AS $$
BEGIN
    IF NEW.latitude IS DISTINCT FROM OLD.latitude
       OR NEW.longitude IS DISTINCT FROM OLD.longitude
    THEN
        RAISE EXCEPTION 'dashboard_locations coordinates are immutable (id=%). '
            'Get-or-create a new Location for the changed coordinates instead of mutating this row.',
            OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
""",
            reverse_sql="DROP FUNCTION IF EXISTS dashboard_locations_freeze_identity();",
        ),
        migrations.RunSQL(
            sql="""
DROP TRIGGER IF EXISTS dashboard_locations_freeze_identity ON dashboard_locations;
CREATE TRIGGER dashboard_locations_freeze_identity
    BEFORE UPDATE ON dashboard_locations
    FOR EACH ROW EXECUTE FUNCTION dashboard_locations_freeze_identity();
""",
            reverse_sql="""
DROP TRIGGER IF EXISTS dashboard_locations_freeze_identity ON dashboard_locations;
DROP FUNCTION IF EXISTS dashboard_locations_freeze_identity();
""",
        ),
    ]
