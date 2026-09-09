import django.db.models.functions.comparison
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0032_v0_8_0")]
    operations = [
        migrations.AddIndex(model_name="undoaction", index=models.Index(fields=["profile", "undone_at", "created"], name="idxdb_undo_profile_undone")),
        migrations.AddConstraint(model_name="album", constraint=models.UniqueConstraint(fields=("parent_profile", "slug"), name="uq_album_profile_slug")),
        migrations.AddConstraint(
            model_name="album",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("parent_pin__isnull", False), ("parent_profile__isnull", True), ("parent_wiki__isnull", True)),
                    models.Q(("parent_pin__isnull", True), ("parent_profile__isnull", True), ("parent_wiki__isnull", False)),
                    models.Q(("parent_pin__isnull", True), ("parent_profile__isnull", False), ("parent_wiki__isnull", True)),
                    _connector="OR",
                ),
                name="ck_album_exactly_one_owner",
            ),
        ),
        migrations.AddIndex(model_name="image", index=models.Index(condition=models.Q(("pending_scan", True)), fields=["created"], name="idxdb_image_pending_created")),
        migrations.AddIndex(model_name="image", index=models.Index(fields=["profile", "copied_from_profile"], name="idxdb_img_profile_copied_from")),
        migrations.AddIndex(model_name="image", index=models.Index(fields=["profile", "media_type", "-created", "-id"], name="idxdb_img_profile_kind_recent")),
        migrations.AddConstraint(model_name="photouploadfailure", constraint=models.UniqueConstraint(condition=models.Q(("status", "pending")), fields=("image",), name="uq_photo_fail_pending_image")),
        migrations.AddConstraint(
            model_name="friendship",
            constraint=models.UniqueConstraint(django.db.models.functions.comparison.Least("from_profile_id", "to_profile_id"), django.db.models.functions.comparison.Greatest("from_profile_id", "to_profile_id"), name="friendship_one_row_per_pair"),
        ),
    ]
