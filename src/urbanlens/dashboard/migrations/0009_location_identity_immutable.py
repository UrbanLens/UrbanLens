# Generated manually (not via makemigrations - GDAL/PostGIS unavailable outside Docker).
#
# A Location's identity is its coordinates: rows are deduplicated by
# (latitude, longitude), and when a pin's/wiki's coordinates change the app
# get-or-creates a *different* Location rather than mutating an existing one
# (see Location.save()). This trigger is the unbypassable enforcement floor - it
# also rejects mutations that skip save(), i.e. QuerySet.update(), bulk_update(),
# the admin, and raw SQL.
#
# Only the coordinate columns are frozen. Address components are metadata
# geocoded from the coordinates and are backfilled after creation, so they stay
# writable - as do the cache/routing columns (google_place_id, slug, point,
# official_name, updated).
from django.db import migrations

_FREEZE_FUNCTION = """
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
"""

_CREATE_TRIGGER = """
DROP TRIGGER IF EXISTS dashboard_locations_freeze_identity ON dashboard_locations;
CREATE TRIGGER dashboard_locations_freeze_identity
    BEFORE UPDATE ON dashboard_locations
    FOR EACH ROW EXECUTE FUNCTION dashboard_locations_freeze_identity();
"""

_DROP = """
DROP TRIGGER IF EXISTS dashboard_locations_freeze_identity ON dashboard_locations;
DROP FUNCTION IF EXISTS dashboard_locations_freeze_identity();
"""


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0008_remove_pinvisit_slug"),
    ]

    operations = [
        migrations.RunSQL(sql=_FREEZE_FUNCTION, reverse_sql="DROP FUNCTION IF EXISTS dashboard_locations_freeze_identity();"),
        migrations.RunSQL(sql=_CREATE_TRIGGER, reverse_sql=_DROP),
    ]
