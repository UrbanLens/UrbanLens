"""Migrate MarkupMap.layer_mode to the canonical layer keys.

The shared frontend layers engine (frontend/ts/shared/map-layers.ts) uses
``street`` / ``topographic`` / ``satellite`` / ``dark``; the column previously
stored ``standard`` / ``topo`` for the first two. Rewrites existing rows and
updates the field's choices/default to match.
"""

from django.db import migrations, models

LEGACY_TO_CANONICAL = {
    "standard": "street",
    "topo": "topographic",
}


def forwards(apps, schema_editor):
    """Rewrite legacy layer_mode values to their canonical replacements."""
    markup_map = apps.get_model("dashboard", "MarkupMap")
    for legacy, canonical in LEGACY_TO_CANONICAL.items():
        markup_map.objects.filter(layer_mode=legacy).update(layer_mode=canonical)


def backwards(apps, schema_editor):
    """Restore the legacy layer_mode values."""
    markup_map = apps.get_model("dashboard", "MarkupMap")
    for legacy, canonical in LEGACY_TO_CANONICAL.items():
        markup_map.objects.filter(layer_mode=canonical).update(layer_mode=legacy)


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0006_remove_review_text"),
    ]

    operations = [
        migrations.AlterField(
            model_name="markupmap",
            name="layer_mode",
            field=models.CharField(
                choices=[
                    ("street", "Street"),
                    ("satellite", "Satellite"),
                    ("topographic", "Topographic"),
                    ("dark", "Dark"),
                ],
                default="street",
                max_length=20,
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
