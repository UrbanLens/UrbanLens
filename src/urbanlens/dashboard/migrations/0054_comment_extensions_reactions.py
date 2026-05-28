"""Extend Comment with location FK, parent, image; extend TripComment with parent, image; add Reaction."""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0053_notification_redesign"),
    ]

    operations = [
        # ── 1. Comment: make pin nullable ────────────────────────────────────
        migrations.AlterField(
            model_name="comment",
            name="pin",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="comments",
                to="dashboard.pin",
            ),
        ),

        # ── 2. Comment: add location FK ──────────────────────────────────────
        migrations.AddField(
            model_name="comment",
            name="location",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="comments",
                to="dashboard.location",
            ),
        ),

        # ── 3. Comment: add parent FK (self) ─────────────────────────────────
        migrations.AddField(
            model_name="comment",
            name="parent",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="replies",
                to="dashboard.comment",
            ),
        ),

        # ── 4. Comment: add image field ──────────────────────────────────────
        migrations.AddField(
            model_name="comment",
            name="image",
            field=models.ImageField(blank=True, null=True, upload_to="comment_images/"),
        ),

        # ── 5. Comment: widen text field ─────────────────────────────────────
        migrations.AlterField(
            model_name="comment",
            name="text",
            field=models.TextField(),
        ),

        # ── 6. TripComment: add parent FK (self) ─────────────────────────────
        migrations.AddField(
            model_name="tripcomment",
            name="parent",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="replies",
                to="dashboard.tripcomment",
            ),
        ),

        # ── 7. TripComment: add image field ──────────────────────────────────
        migrations.AddField(
            model_name="tripcomment",
            name="image",
            field=models.ImageField(blank=True, null=True, upload_to="comment_images/"),
        ),

        # ── 8. Create Reaction ────────────────────────────────────────────────
        migrations.CreateModel(
            name="Reaction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("emoji", models.CharField(max_length=10)),
                (
                    "comment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reactions",
                        to="dashboard.comment",
                    ),
                ),
                (
                    "trip_comment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reactions",
                        to="dashboard.tripcomment",
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reactions",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_reactions",
                "abstract": False,
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(comment__isnull=False),
                        fields=["profile", "emoji", "comment"],
                        name="unique_reaction_comment",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(trip_comment__isnull=False),
                        fields=["profile", "emoji", "trip_comment"],
                        name="unique_reaction_trip_comment",
                    ),
                ],
                "indexes": [
                    models.Index(fields=["comment"], name="reaction_comment_idx"),
                    models.Index(fields=["trip_comment"], name="reaction_trip_comment_idx"),
                ],
            },
        ),
    ]
