import django.db.migrations.operations.special
import django.db.models.deletion
import django.db.models.functions.comparison
import urbanlens.dashboard.models.fields
import urbanlens.dashboard.models.images.model
from decimal import Decimal
from django.db import migrations, models
from typing import Any
from django.db.models.functions import Greatest, Least
from django.db.models import Count
import logging


def _0049_backfill_friendinvitation_email_normalized(apps, schema_editor):
    from urbanlens.dashboard.services.auth.email_normalization import normalize_email

    FriendInvitation = apps.get_model("dashboard", "FriendInvitation")
    updated = []
    for invitation in FriendInvitation.objects.only("id", "email", "email_normalized").iterator():
        normalized = normalize_email(invitation.email) if invitation.email else ""
        if normalized != invitation.email_normalized:
            invitation.email_normalized = normalized
            updated.append(invitation)
    if updated:
        FriendInvitation.objects.bulk_update(updated, ["email_normalized"], batch_size=500)


def _0049_noop(apps, schema_editor):
    pass


def _0052__backfill(apps, schema_editor):
    """Record the historical award and flag existing reverts.

    Args:
        apps: The historical app registry.
        schema_editor: Unused; required by ``RunPython``.
    """
    from urbanlens.dashboard.services.consensus.points import backfill_wiki_edit_points

    backfill_wiki_edit_points(apps.get_model("dashboard", "WikiEdit"))


def _duplicated_pairs(friendship: Any) -> list[tuple[int, int]]:
    """The `(lower id, higher id)` pairs that have more than one row.

    Asked of the database rather than derived while walking every row: the
    accumulate-as-you-go version holds one model instance per *distinct pair*
    for the length of the table, so `.iterator()` bounds nothing and a large
    friendships table is loaded into the migration's memory to find what is
    usually a handful of duplicates.

    Args:
        friendship: The historical ``Friendship`` model.

    Returns:
        One tuple per duplicated pair.
    """
    duplicated = friendship.objects.annotate(low=Least("from_profile_id", "to_profile_id"), high=Greatest("from_profile_id", "to_profile_id")).values("low", "high").annotate(rows=Count("pk")).filter(rows__gt=1)
    return [(entry["low"], entry["high"]) for entry in duplicated]


def _rank(status: str) -> int:
    """How restrictive `status` is; lower wins.

    Args:
        status: A stored ``FriendshipStatus`` value.

    Returns:
        Its index in the precedence order, or one past the end when unknown.
    """
    try:
        return _STATUS_PRECEDENCE.index(status)
    except ValueError:
        return len(_STATUS_PRECEDENCE)


_STATUS_PRECEDENCE = ("Blocked", "Removed", "Declined", "Ignored", "Accepted", "Requested", "Pending", "Muted")
logger = logging.getLogger(__name__)


def _0054_merge_reciprocal_rows(apps, schema_editor) -> None:
    """Collapse every `A->B` / `B->A` pair into the one row that survives."""
    friendship = apps.get_model("dashboard", "Friendship")
    pairs = _duplicated_pairs(friendship)
    if not pairs:
        return
    seen: dict[tuple[int, int], Any] = {}
    merged = 0
    pair_set = set(pairs)
    involved = {profile for pair in pairs for profile in pair}
    candidates = friendship.objects.filter(from_profile_id__in=involved, to_profile_id__in=involved)
    for row in candidates.order_by("pk").iterator():
        key = (min(row.from_profile_id, row.to_profile_id), max(row.from_profile_id, row.to_profile_id))
        if key not in pair_set:
            continue
        keeper = seen.get(key)
        if keeper is None:
            seen[key] = row
            continue
        reversed_row = row.from_profile_id != keeper.from_profile_id
        fields = []
        if _rank(row.status) < _rank(keeper.status):
            logger.warning("Merging reciprocal friendships %s (%s) and %s (%s): keeping the more restrictive status", keeper.pk, keeper.status, row.pk, row.status)
            if reversed_row:
                keeper.from_profile_id, keeper.to_profile_id = (keeper.to_profile_id, keeper.from_profile_id)
                keeper.muted_by_from_profile, keeper.muted_by_to_profile = (keeper.muted_by_to_profile, keeper.muted_by_from_profile)
                fields += ["from_profile", "to_profile", "muted_by_from_profile", "muted_by_to_profile"]
                reversed_row = False
            keeper.status = row.status
            keeper.request_message = row.request_message
            fields += ["status", "request_message"]
        loser_from = row.muted_by_to_profile if reversed_row else row.muted_by_from_profile
        loser_to = row.muted_by_from_profile if reversed_row else row.muted_by_to_profile
        if loser_from and (not keeper.muted_by_from_profile):
            keeper.muted_by_from_profile = True
            fields.append("muted_by_from_profile")
        if loser_to and (not keeper.muted_by_to_profile):
            keeper.muted_by_to_profile = True
            fields.append("muted_by_to_profile")
        logger.warning("Deleting reciprocal friendship row %s (%s -> %s), merged into %s", row.pk, row.from_profile_id, row.to_profile_id, keeper.pk)
        row.delete()
        if fields:
            keeper.save(update_fields=sorted(set(fields)))
        merged += 1
    if merged:
        logger.warning("Merged %s reciprocal friendship row(s)", merged)


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0031_v0_7_0_indexes")]
    operations = [
        migrations.AddField(model_name="image", name="thumbnail", field=models.ImageField(blank=True, max_length=255, null=True, upload_to=urbanlens.dashboard.models.images.model.pin_image_thumbnail_path)),
        migrations.AddField(model_name="image", name="map_hidden", field=models.BooleanField(default=False)),
        migrations.AlterField(
            model_name="image",
            name="quota_exempt_reason",
            field=models.CharField(
                blank=True,
                choices=[("external_media", "Cached external media"), ("community", "Community-valued contribution"), ("shared_copy", "Copy of a shared photo"), ("deduplicated", "Same file already stored for this user")],
                default="",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="PhotoUploadFailure",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("filename", models.CharField(max_length=255)),
                ("error", models.TextField()),
                ("status", models.CharField(choices=[("pending", "Pending"), ("resolved", "Resolved"), ("dismissed", "Dismissed")], default="pending", max_length=20)),
                ("album", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="photo_upload_failures", to="dashboard.album")),
                ("pin", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="photo_upload_failures", to="dashboard.pin")),
                ("profile", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="photo_upload_failures", to="dashboard.profile")),
            ],
            options={"db_table": "dashboard_photo_upload_failure", "indexes": [models.Index(fields=["profile", "status"], name="idx_photo_fail_profile_status")]},
        ),
        migrations.CreateModel(
            name="PhotoMetadataConflict",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("fields", models.JSONField(default=dict)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("resolved", "Resolved"), ("dismissed", "Dismissed")], default="pending", max_length=20)),
                ("existing_image", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="metadata_conflicts_as_existing", to="dashboard.image")),
                ("new_image", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="metadata_conflicts_as_new", to="dashboard.image")),
                ("profile", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="photo_metadata_conflicts", to="dashboard.profile")),
            ],
            options={"db_table": "dashboard_photo_metadata_conflict", "indexes": [models.Index(fields=["profile", "status"], name="idx_photo_meta_profile_status")]},
        ),
        migrations.AddField(model_name="album", name="sort", field=models.CharField(choices=[("uploaded", "Date uploaded"), ("taken", "Date taken"), ("name", "Name"), ("custom", "Custom")], default="uploaded", max_length=20)),
        migrations.RemoveField(model_name="album", name="manual_order"),
        migrations.AlterField(model_name="albumitem", name="order", field=models.IntegerField(blank=True, default=None, null=True)),
        migrations.AddField(model_name="undoaction", name="kind", field=models.CharField(choices=[("delete", "Delete"), ("mutate", "Mutate")], default="delete", max_length=12)),
        migrations.AddField(model_name="undoaction", name="undone_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AlterModelOptions(name="albumitem", options={"ordering": ["pk"]}),
        migrations.AddField(model_name="album", name="parent_profile", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="vault_albums", to="dashboard.profile")),
        migrations.AddField(model_name="image", name="filename_taken_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="image", name="marker_thumbnail", field=models.ImageField(blank=True, max_length=255, null=True, upload_to=urbanlens.dashboard.models.images.model.pin_image_marker_thumbnail_path)),
        migrations.AddField(model_name="image", name="original_filename", field=urbanlens.dashboard.models.fields.EncryptedTextField(blank=True, default="", fail_soft=True)),
        migrations.CreateModel(
            name="PlaceExternalTag",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("source", models.CharField(choices=[("overture", "Overture Maps"), ("osm", "OpenStreetMap")], max_length=20)),
                ("key", models.CharField(max_length=100)),
                ("value", models.CharField(max_length=255)),
                ("is_primary", models.BooleanField(default=False)),
                ("confidence", models.FloatField(blank=True, null=True)),
                ("place", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="external_tags", to="dashboard.place")),
            ],
            options={
                "db_table": "dashboard_place_external_tags",
                "ordering": ["-is_primary", "source", "key"],
                "abstract": False,
                "indexes": [models.Index(fields=["place", "source"], name="idxdb_place_exttag_placesrc")],
                "constraints": [models.UniqueConstraint(fields=("place", "source", "key", "value"), name="place_external_tag_unique")],
            },
        ),
        migrations.AddField(model_name="image", name="pending_scan", field=models.BooleanField(default=False)),
        migrations.AlterField(
            model_name="notificationlog",
            name="notification_type",
            field=models.CharField(
                choices=[
                    ("trip_updated", "Trip Updated"),
                    ("friend_request", "New Friend Request"),
                    ("message", "New Message"),
                    ("comment_reply", "Reply to Comment"),
                    ("comment_liked", "Comment Likes"),
                    ("comment_upload_failed", "Comment Upload Failed"),
                    ("photo_upload_failed", "Photo Upload Failed"),
                    ("friend_accepted", "Friend Request Accepted"),
                    ("added_to_trip", "Trip Invitation"),
                    ("wiki_updated", "Community Wiki Updated"),
                    ("pin_shared", "Pin Shared"),
                    ("map_shared", "Map Shared"),
                    ("visit_suggested", "Visit Suggested"),
                    ("safety_ci_due", "Safety Check-in Due"),
                    ("safety_ci_final_warning", "Safety Check-in Final Warning"),
                    ("safety_ci_overdue", "Safety Check-in Overdue"),
                    ("safety_ci_resolved", "Safety Check-in Resolved"),
                    ("safety_ci_plan_updated", "Safety Check-in Plan Updated"),
                    ("wiki_safety_checkin", "Community Wiki Safety Check-in"),
                    ("safety_ci_partner_invite", "Safety Check-in Partner Request"),
                    ("safety_ci_partner_accepted", "Safety Check-in Partner Accepted"),
                    ("account_deletion_requested", "Account Deletion Requested"),
                    ("account_deletion_reminder", "Account Deletion Reminder"),
                    ("ai_extraction", "AI Link Analysis Complete"),
                    ("friend_suggestion", "Friend Suggestion"),
                    ("spotguessr_invite", "SpotGuessr Invitation"),
                    ("trivia_invite", "Trivia Invitation"),
                    ("consensus_invite", "Consensus Invitation"),
                    ("pin_import_complete", "Pin Import Complete"),
                    ("achievement_earned", "Achievement Unlocked"),
                    ("error", "Error"),
                    ("warning", "Warning"),
                    ("info", "Info"),
                ],
                default="info",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="placeaccessgrant",
            name="reason",
            field=models.CharField(
                choices=[("backfill", "Held before places existed"), ("split", "Held the split family - originally, or by earning every current member"), ("engagement", "Viewed the wiki or shared content to it while access was held")], max_length=20
            ),
        ),
        migrations.CreateModel(
            name="ExternalTagGroup",
            fields=[("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")), ("created", models.DateTimeField(auto_now_add=True)), ("updated", models.DateTimeField(auto_now=True))],
            options={"db_table": "dashboard_external_tag_groups", "abstract": False},
        ),
        migrations.CreateModel(
            name="ExternalTagVocabularyEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("source", models.CharField(choices=[("overture", "Overture Maps"), ("osm", "OpenStreetMap")], max_length=20)),
                ("key", models.CharField(max_length=100)),
                ("value", models.CharField(max_length=255)),
                ("is_preferred", models.BooleanField(default=False)),
                ("group", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="members", to="dashboard.externaltaggroup")),
            ],
            options={
                "db_table": "dashboard_external_tag_vocabulary",
                "ordering": ["source", "key", "value"],
                "abstract": False,
                "indexes": [models.Index(fields=["group"], name="idxdb_exttag_vocab_group")],
                "constraints": [
                    models.UniqueConstraint(fields=("source", "key", "value"), name="external_tag_vocabulary_unique"),
                    models.UniqueConstraint(condition=models.Q(("is_preferred", True)), fields=("group",), name="external_tag_vocabulary_one_preferred_per_group"),
                ],
            },
        ),
        migrations.AddField(model_name="image", name="exif_latitude", field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True)),
        migrations.AddField(model_name="image", name="exif_longitude", field=models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True)),
        migrations.AddField(model_name="image", name="exif_altitude", field=models.DecimalField(blank=True, decimal_places=2, max_digits=7, null=True)),
        migrations.AddField(model_name="image", name="exif_pitch", field=models.DecimalField(blank=True, decimal_places=2, max_digits=6, null=True)),
        migrations.AddField(model_name="image", name="exif_roll", field=models.DecimalField(blank=True, decimal_places=2, max_digits=6, null=True)),
        migrations.AddField(model_name="image", name="exif_camera_make", field=models.CharField(blank=True, max_length=255, null=True)),
        migrations.AddField(model_name="image", name="exif_camera_model", field=models.CharField(blank=True, max_length=255, null=True)),
        migrations.AddField(model_name="image", name="exif_lens_model", field=models.CharField(blank=True, max_length=255, null=True)),
        migrations.AddField(model_name="image", name="exif_shutter_speed", field=models.CharField(blank=True, max_length=32, null=True)),
        migrations.AddField(model_name="image", name="exif_aperture", field=models.DecimalField(blank=True, decimal_places=1, max_digits=4, null=True)),
        migrations.AddField(model_name="image", name="exif_focal_length", field=models.DecimalField(blank=True, decimal_places=1, max_digits=6, null=True)),
        migrations.AddField(model_name="image", name="exif_floor", field=models.IntegerField(blank=True, null=True)),
        migrations.AddField(model_name="profile", name="keyboard_shortcuts", field=models.JSONField(blank=True, default=dict)),
        migrations.AddField(model_name="image", name="copied_from", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="copies", to="dashboard.image")),
        migrations.AddField(model_name="image", name="copied_from_label", field=models.CharField(blank=True, default="", max_length=255)),
        migrations.AddField(model_name="image", name="copied_from_location", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="dashboard.location")),
        migrations.AddField(model_name="image", name="copied_from_profile", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="dashboard.profile")),
        migrations.AlterField(
            model_name="image",
            name="quota_exempt_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("external_media", "Cached external media"),
                    ("community", "Community-valued contribution"),
                    ("shared_copy", "Copy of a shared photo"),
                    ("deduplicated", "Same file already stored for this user"),
                    ("wiki_copy", "Copy of a wiki photo"),
                ],
                default="",
                max_length=20,
            ),
        ),
        migrations.AddField(model_name="image", name="upload_processed_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="friendinvitation", name="email_normalized", field=models.CharField(blank=True, db_index=True, default="", max_length=254)),
        migrations.RunPython(code=_0049_backfill_friendinvitation_email_normalized, reverse_code=_0049_noop),
        migrations.AddField(model_name="image", name="analysis_thumbnail", field=models.ImageField(blank=True, max_length=255, null=True, upload_to=urbanlens.dashboard.models.images.model.pin_image_analysis_thumbnail_path)),
        migrations.AddField(model_name="wikiedit", name="is_revert", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="wikiedit", name="consensus_points", field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(model_name="wikiedit", name="consensus_points_retracted", field=models.BooleanField(default=False)),
        migrations.RunPython(code=_0052__backfill, reverse_code=django.db.migrations.operations.special.RunPython.noop),
        migrations.AddField(model_name="image", name="upload_failed_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="image", name="upload_sweep_attempts", field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(model_name="photouploadfailure", name="image", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="upload_failures", to="dashboard.image")),
        migrations.AddField(model_name="photouploadfailure", name="kind", field=models.CharField(choices=[("upload_rejected", "Upload rejected"), ("processing_failed", "Processing failed")], default="upload_rejected", max_length=20)),
        migrations.AddField(model_name="photouploadfailure", name="user_retries", field=models.PositiveSmallIntegerField(default=0)),
        migrations.RunPython(code=_0054_merge_reciprocal_rows, reverse_code=django.db.migrations.operations.special.RunPython.noop),
        migrations.AddField(model_name="reputationevent", name="weight", field=models.DecimalField(decimal_places=4, default=Decimal("1"), max_digits=6)),
        migrations.AddField(model_name="reputationevent", name="weight_reason", field=models.CharField(blank=True, default="", max_length=64)),
    ]
