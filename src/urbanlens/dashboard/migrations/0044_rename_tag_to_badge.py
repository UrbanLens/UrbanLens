"""Rename Tag model → Badge and TagCustomization → BadgeCustomization.

Physical DB tables (dashboard_tags, dashboard_tag_customizations) and the
tag_id FK column are unchanged.  Only Django's migration state, ContentType
rows, and the custom permission codename are updated.
"""

from __future__ import annotations

from django.db import migrations


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
        # State-only rename: Tag → Badge, TagCustomization → BadgeCustomization,
        # and the 'tag' FK field on BadgeCustomization → 'badge'.
        # DB tables already carry explicit db_table values so nothing changes on disk.
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RenameModel("Tag", "Badge"),
                migrations.RenameModel("TagCustomization", "BadgeCustomization"),
                migrations.RenameField(
                    model_name="BadgeCustomization",
                    old_name="tag",
                    new_name="badge",
                ),
            ],
            database_operations=[],
        ),
        # Update the custom permission declaration on the Badge model options.
        migrations.AlterModelOptions(
            name="badge",
            options={
                "get_latest_by": "updated",
                "ordering": ["-order", "name"],
                "permissions": [("edit_global_badge", "Can edit global badges")],
            },
        ),
        # Update the django_content_type rows first, then update auth_permission.
        migrations.RunPython(_update_contenttypes, _reverse_contenttypes),
        migrations.RunPython(_update_permissions, _reverse_permissions),
    ]
