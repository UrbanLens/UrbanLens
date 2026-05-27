from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0041_tag_customization"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="tag",
            options={
                "get_latest_by": "updated",
                "ordering": ["-order", "name"],
                "permissions": [("edit_global_tag", "Can edit global tags")],
            },
        ),
    ]
