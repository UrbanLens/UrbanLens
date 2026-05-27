"""Add view_site_admin permission to SiteSettings and assign to site_admin group."""

from __future__ import annotations

from django.db import migrations


def add_site_admin_permission(apps, schema_editor):
    """Create view_site_admin permission and add it to the site_admin group."""
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    ContentType = apps.get_model("contenttypes", "ContentType")
    db = schema_editor.connection.alias

    ct, _ = ContentType.objects.using(db).get_or_create(
        app_label="dashboard",
        model="sitesettings",
    )
    perm, _ = Permission.objects.using(db).get_or_create(
        codename="view_site_admin",
        content_type=ct,
        defaults={"name": "Can access site admin panel"},
    )
    group, _ = Group.objects.using(db).get_or_create(name="site_admin")
    group.permissions.add(perm)


def remove_site_admin_permission(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    db = schema_editor.connection.alias
    Permission.objects.using(db).filter(codename="view_site_admin").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0048_trip_membership_activity_status_site_settings"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(add_site_admin_permission, remove_site_admin_permission),
    ]
