from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("dashboard", "0042_held_icon_and_avatar_uploads"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="pin",
            index=models.Index(condition=models.Q(("custom_icon_upload", ""), _negated=True), fields=["custom_icon_upload"], name="idxdb_pin_held_icon"),
        ),
        AddIndexConcurrently(
            model_name="label",
            index=models.Index(condition=models.Q(("custom_icon_upload", ""), _negated=True), fields=["custom_icon_upload"], name="idxdb_label_held_icon"),
        ),
        AddIndexConcurrently(
            model_name="achievement",
            index=models.Index(condition=models.Q(("custom_icon_upload", ""), _negated=True), fields=["custom_icon_upload"], name="idxdb_achv_held_icon"),
        ),
        AddIndexConcurrently(
            model_name="profile",
            index=models.Index(condition=models.Q(("avatar_upload", ""), _negated=True), fields=["avatar_upload"], name="idxdb_profile_held_avatar"),
        ),
    ]
