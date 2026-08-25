"""Give a floor a designation of its own, and make its level unique.

A storey's position in the stack and what people call it were the same field,
so renaming a floor destroyed the only record of which storey it was. Splitting
them is what lets a building skip its thirteenth floor by designation while the
levels underneath stay contiguous - which everything structural (stacking, the
floor-below underlay, stair and lift connectors) needs them to be.

Ordered data-before-schema, with the constraint dead last: existing plans can
hold duplicate or sparse levels, so they are repaired before anything is asked
to enforce uniqueness over them.
"""

from __future__ import annotations

import re

from django.db import migrations, models

#: Names the client used to write into the database as though the user had
#: typed them. Blank means "derive it", so these become blank.
_AUTO_NAME = re.compile(r"^(?:Ground floor|Level -?\d+)$")


def clear_generated_names(apps, schema_editor) -> None:
    """Blank the floor names no one actually chose.

    The editor persisted ``"Ground floor"`` and ``"Level N"`` as real values,
    which is why a floor could not tell you which storey it was once renamed:
    there was nothing to fall back to. A derived label now covers that case, so
    these stop being data.
    """
    floor_model = apps.get_model("dashboard", "FloorplanFloor")
    for floor in floor_model.objects.exclude(name="").iterator():
        if _AUTO_NAME.match(floor.name or ""):
            floor_model.objects.filter(pk=floor.pk).update(name="")


def renumber_levels(apps, schema_editor) -> None:
    """Make every plan's levels contiguous, holding its ground datum.

    Required before the unique constraint below: a mid-stack delete used to
    leave a gap, and nothing ever stopped two floors sharing a level. Whichever
    floor sits nearest the old datum keeps level 0, so a repair never silently
    moves which storey the author considers the ground.
    """
    floor_model = apps.get_model("dashboard", "FloorplanFloor")
    floorplan_ids = floor_model.objects.values_list("floorplan_id", flat=True).distinct()
    for floorplan_id in floorplan_ids:
        floors = list(floor_model.objects.filter(floorplan_id=floorplan_id).order_by("level", "sort_order", "id"))
        if not floors:
            continue
        ground = min(range(len(floors)), key=lambda index: abs(floors[index].level))
        for index, floor in enumerate(floors):
            target = index - ground
            if floor.level != target:
                floor_model.objects.filter(pk=floor.pk).update(level=target)


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0061_floorplanmarker_linked_pin"),
    ]

    operations = [
        migrations.AddField(
            model_name="floorplanfloor",
            name="designation",
            field=models.CharField(blank=True, default="", max_length=8),
        ),
        migrations.RunPython(clear_generated_names, migrations.RunPython.noop),
        migrations.RunPython(renumber_levels, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="floorplanfloor",
            constraint=models.UniqueConstraint(
                fields=["floorplan", "level"],
                name="floorplan_floor_unique_level",
                deferrable=models.Deferrable.DEFERRED,
            ),
        ),
    ]
