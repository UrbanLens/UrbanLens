from django.db import migrations, models

_INDEXES = (
    ("pin", "custom_icon_upload", "idxdb_pin_held_icon"),
    ("label", "custom_icon_upload", "idxdb_label_held_icon"),
    ("achievement", "custom_icon_upload", "idxdb_achv_held_icon"),
    ("profile", "avatar_upload", "idxdb_profile_held_avatar"),
)


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0042_held_icon_and_avatar_uploads"),
    ]

    operations = [
        migrations.AddIndex(
            model_name=model_name,
            index=models.Index(condition=models.Q((column, ""), _negated=True), fields=[column], name=name),
        )
        for model_name, column, name in _INDEXES
    ]
