"""Rename Tag model → Badge and TagCustomization → BadgeCustomization.

Physical DB tables (dashboard_tags, dashboard_tag_customizations) and the
tag_id FK column are unchanged.  Only Django's migration state, ContentType
rows, and the custom permission codename are updated.

Also syncs several state discrepancies on BadgeCustomization that accumulated
between migration 0041 (which used explicit AutoField and the old related_name)
and the current model code.  All are state-only — the DB already matches.
"""

from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models


def _update_contenttypes(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    ContentType.objects.filter(app_label="dashboard", model="tag").update(model="badge")
    ContentType.objects.filter(app_label="dashboard", model="tagcustomization").update(model="badgecustomization")


def _reverse_contenttypes(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    ContentType.objects.filter(app_label="dashboard", model="badge").update(model="tag")
    ContentType.objects.filter(app_label="dashboard", model="badgecustomization").update(model="tagcustomization")


def _update_permissions(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    try:
        ct = ContentType.objects.get(app_label="dashboard", model="badge")
        Permission.objects.filter(content_type=ct, codename="edit_global_tag").update(
            codename="edit_global_badge",
            name="Can edit global badges",
        )
    except ContentType.DoesNotExist:
        pass


def _reverse_permissions(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    try:
        ct = ContentType.objects.get(app_label="dashboard", model="badge")
        Permission.objects.filter(content_type=ct, codename="edit_global_badge").update(
            codename="edit_global_tag",
            name="Can edit global tags",
        )
    except ContentType.DoesNotExist:
        pass


class Migration(migrations.Migration):
    dependencies = [
        ("contenttypes", "0002_remove_content_type_name"),
        ("dashboard", "0043_site_admin_group"),
    ]

    operations = [
        # ------------------------------------------------------------------ #
        # Part 1: State-only rename Tag → Badge, TagCustomization →          #
        # BadgeCustomization, and the 'tag' FK field → 'badge'.              #
        # DB tables already carry explicit db_table values; nothing changes. #
        # ------------------------------------------------------------------ #
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RenameModel("Tag", "Badge"),
                migrations.RenameModel("TagCustomization", "BadgeCustomization"),
                migrations.RenameField(
                    model_name="BadgeCustomization",
                    old_name="tag",
                    new_name="badge",
                ),
                # Sync the 'badge' FK to include db_column="tag_id" so Django's
                # state knows the physical column is tag_id, not badge_id.
                # Without this Django would generate a RENAME COLUMN migration.
                migrations.AlterField(
                    model_name="badgecustomization",
                    name="badge",
                    field=models.ForeignKey(
                        db_column="tag_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="customizations",
                        to="dashboard.badge",
                    ),
                ),
                # Sync related_name on the profile FK (model uses badge_customizations).
                migrations.AlterField(
                    model_name="badgecustomization",
                    name="profile",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="badge_customizations",
                        to="dashboard.profile",
                    ),
                ),
                # Sync the id pk type to BigAutoField to match DEFAULT_AUTO_FIELD.
                # Migration 0041 used AutoField explicitly; DB column is already
                # integer and works fine — this is purely a state sync.
                migrations.AlterField(
                    model_name="badgecustomization",
                    name="id",
                    field=models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
            ],
            database_operations=[],
        ),
        # ------------------------------------------------------------------ #
        # Part 2: Update the custom permission declaration on Badge.         #
        # ------------------------------------------------------------------ #
        migrations.AlterModelOptions(
            name="badge",
            options={
                "get_latest_by": "updated",
                "ordering": ["-order", "name"],
                "permissions": [("edit_global_badge", "Can edit global badges")],
            },
        ),
        # ------------------------------------------------------------------ #
        # Part 3: Update django_content_type rows, then auth_permission.     #
        # ------------------------------------------------------------------ #
        migrations.RunPython(_update_contenttypes, _reverse_contenttypes),
        migrations.RunPython(_update_permissions, _reverse_permissions),
    ]
