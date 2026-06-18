from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0085_alter_badge_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="tripcomment",
            name="map_data",
            field=models.JSONField(blank=True, null=True),
        ),
    ]
