"""Campus boundary scoping: add pin FK and generated_polygon, re-key boundaries.

Changes:
- Add generated_polygon: API-cached boundary that survives user clearing their drawing.
- Add pin FK: pin-scoped campuses are now keyed by pin, not (location, profile).
  This fixes boundaries surviving a pin.location reassignment and ensures two
  pins at the same location each get their own independent boundary.
- Data migration: existing (location, profile) user campuses are matched to their
  pin and migrated; unresolvable rows are deleted.
- Replace constraints campus_unique_user_location + campus_unique_default_location
  with campus_unique_location_default + campus_unique_pin.
"""

import django.contrib.gis.db.models.fields
from django.db import migrations, models
import django.db.models.deletion


def migrate_campus_pin_scoping(apps, schema_editor):
    """Re-key existing user campuses from (location, profile) to (pin).

    For each user campus (profile set, pin not yet set):
      - Copy polygon → generated_polygon so clearing doesn't lose the cached boundary.
      - Find the unique pin for (location, profile).  If exactly one exists, set it.
      - If zero or multiple pins match (ambiguous / orphaned), delete the campus.

    Location-default campuses (profile=None) are left untouched except for the
    polygon → generated_polygon copy.
    """
    Campus = apps.get_model("dashboard", "Campus")
    Pin = apps.get_model("dashboard", "Pin")

    # Copy polygon → generated_polygon for all existing rows so the cached
    # boundary is preserved even after users clear their custom drawing.
    for campus in Campus.objects.all():
        if campus.polygon and campus.generated_polygon is None:
            campus.generated_polygon = campus.polygon
            campus.save(update_fields=["generated_polygon"])

    # Re-key user campuses by pin.
    to_delete = []
    for campus in Campus.objects.filter(profile__isnull=False, pin__isnull=True):
        matching = list(Pin.objects.filter(location_id=campus.location_id, profile=campus.profile))
        if len(matching) == 1:
            campus.pin = matching[0]
            campus.save(update_fields=["pin"])
        else:
            # Ambiguous (multiple pins) or orphaned (no pin) — discard.
            to_delete.append(campus.pk)

    Campus.objects.filter(pk__in=to_delete).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0025_alter_pinshare_options_and_more"),
    ]

    operations = [
        # 1. Add generated_polygon column.
        migrations.AddField(
            model_name="campus",
            name="generated_polygon",
            field=django.contrib.gis.db.models.fields.MultiPolygonField(
                blank=True, geography=True, null=True, srid=4326,
            ),
        ),
        # 2. Add pin FK column (nullable; filled by data migration below).
        migrations.AddField(
            model_name="campus",
            name="pin",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="campus",
                to="dashboard.pin",
            ),
        ),
        # 3. Backfill generated_polygon and re-key user campuses to pin.
        migrations.RunPython(migrate_campus_pin_scoping, migrations.RunPython.noop),
        # Flush deferred FK trigger events before creating new partial indexes.
        # PostgreSQL refuses CREATE INDEX when there are pending trigger events in
        # the same transaction (from the deletes above touching deferrable FKs).
        migrations.RunSQL("SET CONSTRAINTS ALL IMMEDIATE", migrations.RunSQL.noop),
        # 4. Drop old constraints.
        migrations.RemoveConstraint(
            model_name="campus",
            name="campus_unique_user_location",
        ),
        migrations.RemoveConstraint(
            model_name="campus",
            name="campus_unique_default_location",
        ),
        # 5. Add new constraints.
        migrations.AddConstraint(
            model_name="campus",
            constraint=models.UniqueConstraint(
                condition=models.Q(profile__isnull=True, pin__isnull=True),
                fields=["location"],
                name="campus_unique_location_default",
            ),
        ),
        migrations.AddConstraint(
            model_name="campus",
            constraint=models.UniqueConstraint(
                condition=models.Q(pin__isnull=False),
                fields=["pin"],
                name="campus_unique_pin",
            ),
        ),
        migrations.RemoveField(
            model_name="location",
            name="bounding_box",
        ),
    ]
