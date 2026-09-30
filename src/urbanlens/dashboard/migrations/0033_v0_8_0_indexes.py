import datetime
import zoneinfo

from django.db import migrations, models
import django.db.models.functions.text


def _0096_refuse_to_drop_url_only_overlays(apps, schema_editor):
    MapImageOverlay = apps.get_model("dashboard", "MapImageOverlay")
    url_only = list(MapImageOverlay.objects.filter(image__isnull=True, tile_url_template="").values_list("pk", flat=True))
    if url_only:
        raise RuntimeError(f"{len(url_only)} map overlay(s) draw only from an external image_url, which this migration removes: {url_only[:20]}. Run `manage.py download_overlay_image_urls` to store each image locally, then migrate again.")
    MapImageOverlay.objects.filter(image__isnull=False).exclude(tile_url_template="").update(tile_url_template="")


def _flush_deferred_constraints(apps, schema_editor):
    schema_editor.connection.check_constraints()


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0032_v0_8_0")]
    operations = [
        # Here rather than in 0032 so the command this refusal names runs against the whole v0.8.0 schema.
        migrations.RunPython(code=_0096_refuse_to_drop_url_only_overlays, reverse_code=migrations.RunPython.noop),
        migrations.RunPython(_flush_deferred_constraints, _flush_deferred_constraints),
        migrations.RemoveField(model_name="mapimageoverlay", name="image_url"),
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
        migrations.AddIndex(model_name="image", index=models.Index(fields=["profile", "copied_from_profile"], name="idxdb_img_profile_copied_from")),
        migrations.AddIndex(model_name="image", index=models.Index(fields=["profile", "media_type", "-created", "-id"], name="idxdb_img_profile_kind_recent")),
        migrations.AddConstraint(model_name="photouploadfailure", constraint=models.UniqueConstraint(condition=models.Q(("status", "pending")), fields=("image",), name="uq_photo_fail_pending_image")),
        migrations.AddConstraint(
            model_name="friendship",
            constraint=models.UniqueConstraint(django.db.models.functions.comparison.Least("from_profile_id", "to_profile_id"), django.db.models.functions.comparison.Greatest("from_profile_id", "to_profile_id"), name="friendship_one_row_per_pair"),
        ),
        migrations.AddIndex(model_name="apicalllog", index=models.Index(fields=["service", "created", "profile"], name="idxdb_apilog_svc_cdt_prf")),
        migrations.AddConstraint(model_name="commentlocationmention", constraint=models.UniqueConstraint(fields=("trip_comment", "location_uuid"), name="uq_cmtloc_one_per_trip_comment")),
        migrations.AddConstraint(
            model_name="commentlocationmention",
            constraint=models.CheckConstraint(
                condition=models.Q(models.Q(("comment__isnull", False), ("trip_comment__isnull", True)), models.Q(("comment__isnull", True), ("trip_comment__isnull", False)), _connector="OR"), name="ck_cmtloc_exactly_one_owner"
            ),
        ),
        migrations.AddIndex(model_name="pin", index=models.Index(condition=models.Q(("custom_icon_upload", ""), _negated=True), fields=["custom_icon_upload"], name="idxdb_pin_held_icon")),
        migrations.AddIndex(model_name="label", index=models.Index(condition=models.Q(("custom_icon_upload", ""), _negated=True), fields=["custom_icon_upload"], name="idxdb_label_held_icon")),
        migrations.AddIndex(model_name="achievement", index=models.Index(condition=models.Q(("custom_icon_upload", ""), _negated=True), fields=["custom_icon_upload"], name="idxdb_achv_held_icon")),
        migrations.AddIndex(model_name="profile", index=models.Index(condition=models.Q(("avatar_upload", ""), _negated=True), fields=["avatar_upload"], name="idxdb_profile_held_avatar")),
        migrations.AddIndex(model_name="pin", index=models.Index(fields=["profile", "created"], name="idxdb_pin_pfile_created")),
        migrations.AddIndex(
            model_name="label",
            index=django.contrib.postgres.indexes.GinIndex(
                django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper(django.db.models.functions.comparison.Cast("name", output_field=models.TextField())), name="gin_trgm_ops"), name="idxdb_label_name_upper_trgm"
            ),
        ),
        migrations.AddIndex(model_name="pin", index=models.Index(fields=["profile", "id"], name="idxdb_pin_pfile_id")),
        migrations.AddIndex(model_name="profile", index=models.Index(fields=["username_key"], name="idxdb_profile_username_key")),
        migrations.AddIndex(model_name="safetycheckincontact", index=models.Index(fields=["email_normalized"], name="idxdb_scc_email_normalized")),
        migrations.AddConstraint(model_name="profile", constraint=models.UniqueConstraint(condition=models.Q(("verified_primary_email", ""), _negated=True), fields=("verified_primary_email",), name="uniq_profile_verified_primary_email")),
        migrations.AddIndex(model_name="profile", index=models.Index(condition=models.Q(("avatar__isnull", False), models.Q(("avatar", ""), _negated=True)), fields=["avatar"], name="idxdb_profile_avatar")),
        migrations.AddIndex(model_name="image", index=models.Index(fields=["profile", "quota_exempt_reason"], include=("file_size",), name="idxdb_image_quota_usage")),
        migrations.AddConstraint(
            model_name="rolesubscription", constraint=models.UniqueConstraint(condition=models.Q(("status__in", ("canceled", "incomplete_expired")), _negated=True), fields=("user", "role"), name="unique_active_role_subscription")
        ),
        migrations.AddConstraint(model_name="devicescanupload", constraint=models.UniqueConstraint(condition=models.Q(("client_session_uuid", ""), _negated=True), fields=("client_session_uuid",), name="db_scanupload_one_per_client_session")),
        migrations.AddConstraint(model_name="markupmapshare", constraint=models.UniqueConstraint(fields=("markup_map", "from_profile", "to_profile"), name="db_mapshare_one_per_map_pair")),
        migrations.AddConstraint(model_name="pinsuggestion", constraint=models.UniqueConstraint(condition=models.Q(("origin", "community")), fields=("profile", "location"), name="db_pin_sugg_one_community_per_loc")),
        migrations.AddConstraint(
            model_name="pinvisit",
            constraint=models.UniqueConstraint(models.F("pin"), django.db.models.functions.datetime.TruncDate("visited_at", tzinfo=zoneinfo.ZoneInfo(key="UTC")), condition=models.Q(("source", "geolocation")), name="db_pv_one_geo_per_pin_day"),
        ),
        migrations.AddConstraint(
            model_name="tripactivity",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("scheduled_at__isnull", True),
                    models.Q(("scheduled_at__gte", datetime.datetime(1900, 1, 1, 0, 0, tzinfo=datetime.UTC)), ("scheduled_at__lt", datetime.datetime(2200, 1, 1, 0, 0, tzinfo=datetime.UTC))),
                    _connector="OR",
                ),
                name="db_ta_scheduled_at_span",
            ),
        ),
        migrations.AddConstraint(
            model_name="tripactivity",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("scheduled_end__isnull", True),
                    models.Q(("scheduled_end__gte", datetime.datetime(1900, 1, 1, 0, 0, tzinfo=datetime.UTC)), ("scheduled_end__lt", datetime.datetime(2200, 1, 1, 0, 0, tzinfo=datetime.UTC))),
                    _connector="OR",
                ),
                name="db_ta_scheduled_end_span",
            ),
        ),
        migrations.AddConstraint(model_name="recordedweatherday", constraint=models.UniqueConstraint(fields=("cell_lat", "cell_lng", "day"), name="db_weatherday_cell_day_uniq")),
        migrations.AddIndex(model_name="devicescanupload", index=models.Index(condition=models.Q(("status__in", ["pending", "processing"])), fields=["status", "created"], name="idxdb_scanupload_unfinished")),
        migrations.AddIndex(model_name="fact", index=models.Index(condition=models.Q(("needs_recompute", True)), fields=["updated"], name="idxdb_fact_needs_recompute")),
        migrations.AddIndex(model_name="tripcalendarlink", index=models.Index(condition=models.Q(("push_requested_at__isnull", False)), fields=["push_requested_at"], name="idxdb_tcl_push_requested")),
        migrations.AddConstraint(model_name="sitesettings", constraint=models.CheckConstraint(condition=models.Q(("notification_retention_days__gte", 0)), name="notification_retention_days_gte_0")),
        migrations.AddConstraint(
            model_name="mapimageoverlay",
            constraint=models.CheckConstraint(
                condition=models.Q(models.Q(("image__isnull", False), ("tile_url_template", "")), models.Q(("image__isnull", True), models.Q(("tile_url_template", ""), _negated=True)), _connector="OR"), name="db_overlay_one_source"
            ),
        ),
        migrations.AddIndex(model_name="boundaryrevision", index=models.Index(fields=["wiki", "boundary_type", "-id"], name="idxdb_bndrev_wiki_type")),
        migrations.AddConstraint(model_name="sitesettings", constraint=models.CheckConstraint(condition=models.Q(("max_saved_filters_per_user__gte", 0)), name="max_saved_filters_per_user_gte_0")),
        migrations.AddConstraint(model_name="sitesettings", constraint=models.CheckConstraint(condition=models.Q(("max_pin_lists_per_user__gte", 0)), name="max_pin_lists_per_user_gte_0")),
        migrations.AddConstraint(model_name="sitesettings", constraint=models.CheckConstraint(condition=models.Q(("max_labels_per_user__gte", 0)), name="max_labels_per_user_gte_0")),
        migrations.AddConstraint(model_name="sitesettings", constraint=models.CheckConstraint(condition=models.Q(("max_custom_fields_per_user__gte", 0)), name="max_custom_fields_per_user_gte_0")),
        migrations.AddConstraint(model_name="sitesettings", constraint=models.CheckConstraint(condition=models.Q(("max_push_devices_per_user__gte", 0)), name="max_push_devices_per_user_gte_0")),
        migrations.AddConstraint(model_name="sitesettings", constraint=models.CheckConstraint(condition=models.Q(("max_photos_per_album__gte", 0)), name="max_photos_per_album_gte_0")),
        migrations.AddIndex(model_name="image", index=models.Index(condition=models.Q(("pending_scan", True), ("upload_failed_at__isnull", True)), fields=["created"], name="idxdb_image_pending_created")),
        migrations.AddConstraint(model_name="sitesettings", constraint=models.CheckConstraint(condition=models.Q(("max_group_chats_per_user__gte", 0)), name="max_group_chats_per_user_gte_0")),
        migrations.AddConstraint(
            model_name="friendship",
            constraint=models.CheckConstraint(
                condition=models.Q(models.Q(("blocked_at__isnull", False), ("status", "Blocked")), models.Q(models.Q(("status", "Blocked"), _negated=True), ("blocked_at__isnull", True)), _connector="OR"),
                name="friendship_blocked_at_only_while_blocked",
            ),
        ),
    ]
