from django.db import migrations


def create_site_admin_group(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    ContentType = apps.get_model("contenttypes", "ContentType")
    db = schema_editor.connection.alias

    tag_ct, _ = ContentType.objects.using(db).get_or_create(
        app_label="dashboard",
        model="tag",
    )
    perm, _ = Permission.objects.using(db).get_or_create(
        codename="edit_global_tag",
        content_type=tag_ct,
        defaults={"name": "Can edit global tags"},
    )
    group, _ = Group.objects.using(db).get_or_create(name="site_admin")
    group.permissions.add(perm)


def remove_site_admin_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    db = schema_editor.connection.alias
    Group.objects.using(db).filter(name="site_admin").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0042_tag_edit_global_tag_permission"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(create_site_admin_group, remove_site_admin_group),
    ]
