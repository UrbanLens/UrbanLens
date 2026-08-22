"""Add the fence wall kind and the gate opening kind.

A site is often a fence before it is a building - a yard, a compound, the line
you have to get past first - and neither the wall kinds nor the opening kinds
could say so. A gap in a fence stays a ``virtual`` span rather than becoming a
new opening kind: an opening is fitted into fabric that continues, which is what
a gate is, while a missing run is a stretch where nothing is built.

Choices only. Both columns already hold their values as text and the serializer
validates against the enum, so nothing stored has to change.
"""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0062_floorplan_floor_designation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="floorplanwall",
            name="kind",
            field=models.CharField(
                choices=[
                    ("exterior", "Exterior wall"),
                    ("interior", "Interior wall"),
                    ("fence", "Fence"),
                    ("virtual", "Virtual (open edge)"),
                    ("collapsed", "Collapsed / ruined"),
                ],
                default="interior",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="floorplanopening",
            name="kind",
            field=models.CharField(
                choices=[
                    ("door", "Door"),
                    ("doorway", "Doorway (no door)"),
                    ("gate", "Gate"),
                    ("window", "Window"),
                    ("hatch", "Hatch"),
                ],
                default="door",
                max_length=16,
            ),
        ),
    ]
