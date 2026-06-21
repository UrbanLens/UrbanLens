"""Extend Tag model with profile, color, icon, custom_icon, description, order, and hierarchy.
Data-migrate all PinList rows into Tag rows, then drop PinList.
"""

from __future__ import annotations

from django.db import migrations, models
import django.db.models.deletion


def migrate_pin_lists_to_tags(apps, schema_editor):
    """Convert each PinList into a Tag and transfer pin memberships to Pin.tags."""
    PinList = apps.get_model("dashboard", "PinList")
    Tag = apps.get_model("dashboard", "Tag")

    for pin_list in PinList.objects.select_related("profile").prefetch_related("pins"):
        tag, created = Tag.objects.get_or_create(
            profile=pin_list.profile,
            name=pin_list.name,
            defaults={
                "description": pin_list.description,
                "icon": pin_list.icon,
                "order": pin_list.order,
            },
        )
        if not created:
            # A tag with this name already exists for this user; merge order upward.
            if not tag.description and pin_list.description:
                tag.description = pin_list.description
            if not tag.icon and pin_list.icon:
                tag.icon = pin_list.icon
            tag.order = max(pin_list.order, tag.order)
            tag.save()

        for pin in pin_list.pins.all():
            pin.tags.add(tag)


def create_default_tags_for_existing_profiles(apps, schema_editor):
    """Backfill default tags for profiles that had default PinLists.

    The PinList migration already created Visited/Want-to-Go lists; they were
    just migrated above. This step is a no-op unless a profile somehow had no
    matching lists.
    """


def reverse_migrate(apps, schema_editor):
    """Reverse is a no-op - we cannot reliably reconstruct PinList rows."""


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0012_pin_list"),
    ]

    operations = [
        # ── Step 1: add new fields to Tag ────────────────────────────────────
        migrations.AddField(
            model_name="tag",
            name="profile",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="custom_tags",
                to="dashboard.profile",
            ),
        ),
        migrations.AddField(
            model_name="tag",
            name="description",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="tag",
            name="color",
            field=models.CharField(
                blank=True,
                null=True,
                max_length=50,
                choices=[
                    ("#F44336", "Red"),
                    ("#E91E63", "Pink"),
                    ("#9C27B0", "Purple"),
                    ("#673AB7", "Deep Purple"),
                    ("#3F51B5", "Indigo"),
                    ("#2196F3", "Blue"),
                    ("#03A9F4", "Light Blue"),
                    ("#00BCD4", "Cyan"),
                    ("#009688", "Teal"),
                    ("#4CAF50", "Green"),
                    ("#8BC34A", "Light Green"),
                    ("#CDDC39", "Lime"),
                    ("#FFEB3B", "Yellow"),
                    ("#FFC107", "Amber"),
                    ("#FF9800", "Orange"),
                    ("#FF5722", "Deep Orange"),
                    ("#795548", "Brown"),
                    ("#607D8B", "Blue Grey"),
                    ("#9E9E9E", "Grey"),
                ],
            ),
        ),
        migrations.AddField(
            model_name="tag",
            name="icon",
            field=models.CharField(
                blank=True,
                null=True,
                max_length=50,
                choices=[
                    ("bookmark", "Bookmark"),
                    ("star", "Star"),
                    ("heart", "Heart"),
                    ("flag", "Flag"),
                    ("camera", "Camera"),
                    ("home", "Home"),
                    ("place", "Place"),
                    ("explore", "Explore"),
                    ("hiking", "Hiking"),
                    ("warning", "Warning"),
                    ("check_circle", "Check Circle"),
                    ("schedule", "Schedule"),
                    ("visibility", "Visibility"),
                    ("lock", "Private"),
                    ("archive", "Archive"),
                    ("label", "Label"),
                    ("local_offer", "Tag"),
                    ("category", "Category"),
                ],
            ),
        ),
        migrations.AddField(
            model_name="tag",
            name="custom_icon",
            field=models.ImageField(blank=True, null=True, upload_to="tag_icons/"),
        ),
        migrations.AddField(
            model_name="tag",
            name="order",
            field=models.IntegerField(default=0),
        ),
        # ── Step 2: self-referential M2M for hierarchy ───────────────────────
        migrations.AddField(
            model_name="tag",
            name="parents",
            field=models.ManyToManyField(
                blank=True,
                related_name="children",
                symmetrical=False,
                to="dashboard.tag",
            ),
        ),
        # ── Step 3: add new indexes to Tag ───────────────────────────────────
        migrations.AddIndex(
            model_name="tag",
            index=models.Index(fields=["profile"], name="dashboard_tag_profile_idx"),
        ),
        migrations.AddIndex(
            model_name="tag",
            index=models.Index(fields=["profile", "order"], name="dashboard_tag_profile_order_idx"),
        ),
        # ── Step 4: data migration ────────────────────────────────────────────
        migrations.RunPython(migrate_pin_lists_to_tags, reverse_migrate),
        # ── Step 5: drop PinList ──────────────────────────────────────────────
        migrations.DeleteModel(name="PinList"),
    ]
