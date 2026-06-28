from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0016_vip_add_places_feature"),
    ]

    operations = [
        migrations.RenameField(
            model_name="badge",
            old_name="allow_ai",
            new_name="allow_auto_tag",
        ),
        migrations.AddField(
            model_name="badge",
            name="keywords",
            field=models.TextField(blank=True, null=True),
        ),
    ]
