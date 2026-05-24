"""Merge migration: reconciles the auto-generated index/uuid branch with the
color + location-detail-pin branch."""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0028_alter_locationalias_id_alter_pinalias_id"),
        ("dashboard", "0026_pin_color_and_location_detail_pins"),
    ]

    operations = []
