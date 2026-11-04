"""Key existing pins whose Location carries a Google cid as Takeout pins, outside the transaction that added the columns."""

from django.db import migrations, transaction

#: Profiles handled per statement; each chunk commits on its own, so no lock outlives it.
PROFILE_CHUNK = 500

#: Created before this, a Location may sit on an S2-decoded guess (see services.apis.locations.legacy_cid_coordinate_fix).
LEGACY_CUTOFF = "2026-07-25T00:00:00+00:00"

BACKFILL = """
UPDATE dashboard_user_pins AS pin
SET import_source = 'google_takeout', import_key = picked.key
FROM (
    SELECT DISTINCT ON (p.profile_id, gp.cid) p.id, 'cid:' || gp.cid::text AS key
    FROM dashboard_user_pins p
    JOIN dashboard_locations l ON l.id = p.location_id
    JOIN dashboard_google_places gp ON gp.id = l.google_place_id
    WHERE p.profile_id >= %s AND p.profile_id < %s
      AND p.parent_pin_id IS NULL AND p.import_key IS NULL AND gp.cid IS NOT NULL
    ORDER BY p.profile_id, gp.cid, (l.created >= %s) DESC, p.id
) AS picked
WHERE pin.id = picked.id
"""


def key_takeout_pins(apps, schema_editor):
    """
    Key root pins by the cid on their Location, once per cid per profile.
    A profile with two pins on same-cid Locations keeps the key on the one whose Location is not pre-cutoff (a possible mis-geocode), else the lower pk. Pins nested under another pin are skipped.
    """
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT COALESCE(MAX(profile_id), 0) FROM dashboard_user_pins")
        highest = cursor.fetchone()[0]
    for start in range(0, highest + 1, PROFILE_CHUNK):
        with transaction.atomic(using=connection.alias), connection.cursor() as cursor:
            cursor.execute(BACKFILL, [start, start + PROFILE_CHUNK, LEGACY_CUTOFF])


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("dashboard", "0003_pin_import_identity")]
    operations = [migrations.RunPython(key_takeout_pins, migrations.RunPython.noop)]
