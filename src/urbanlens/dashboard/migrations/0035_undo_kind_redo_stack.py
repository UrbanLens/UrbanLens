"""Undo entries can reverse mutations, and stay around after undo so they can be redone."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0034_album_sort_nullable_order"),
    ]

    operations = [
        migrations.AddField(
            model_name="undoaction",
            name="kind",
            field=models.CharField(
                choices=[("delete", "Delete"), ("mutate", "Mutate")],
                default="delete",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="undoaction",
            name="undone_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="undoaction",
            index=models.Index(fields=["profile", "undone_at", "created"], name="idxdb_undo_profile_undone"),
        ),
    ]
