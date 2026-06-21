"""Extend Category model: add description, color, icon (free-text), order, parents hierarchy.

Data migration: existing icon values (e.g. "church", "factory") were the only semantic
description of each category. They are concatenated into the description field before the
icon field is freed from its choices constraint.
"""
from django.db import migrations, models


def _migrate_icon_to_description(apps, schema_editor):
    """Copy old icon choice value into description when description is not already set."""
    Category = apps.get_model("dashboard", "Category")
    for cat in Category.objects.exclude(icon__isnull=True).exclude(icon=""):
        if not cat.description:
            # Turn "office_building" → "Office building" for readability
            cat.description = cat.icon.replace("_", " ").capitalize()
            cat.save(update_fields=["description"])


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0036_security_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="category",
            name="description",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="category",
            name="color",
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name="category",
            name="order",
            field=models.IntegerField(default=0),
        ),
        # Preserve semantic content of old icon choices before removing the constraint.
        migrations.RunPython(_migrate_icon_to_description, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="category",
            name="icon",
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name="category",
            name="parents",
            field=models.ManyToManyField(
                blank=True,
                related_name="children",
                symmetrical=False,
                to="dashboard.category",
            ),
        ),
        migrations.AlterModelOptions(
            name="category",
            options={"get_latest_by": "updated", "ordering": ["-order", "name"]},
        ),
        migrations.AddIndex(
            model_name="category",
            index=models.Index(fields=["order"], name="dashboard_category_order_idx"),
        ),
    ]
