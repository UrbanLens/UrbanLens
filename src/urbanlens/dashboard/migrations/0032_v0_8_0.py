import datetime
from datetime import date
from decimal import Decimal
import hashlib
import logging
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from django.conf import settings
import django.contrib.gis.db.models.fields
import django.contrib.postgres.indexes
import django.core.serializers.json
import django.core.validators
from django.db import IntegrityError, migrations, models, transaction
import django.db.migrations.operations.special
from django.db.models import Count, F, Q
import django.db.models.deletion
from django.db.models.functions import Greatest, Least, Left, Length, TruncDate
import django.db.models.functions.comparison
import django.db.models.functions.datetime
import django.db.models.functions.text

import urbanlens.dashboard.models.fields
import urbanlens.dashboard.models.images.model
import urbanlens.dashboard.models.remote_image_copy.model
import urbanlens.dashboard.models.remote_tiles.model
from urbanlens.dashboard.services.auth.email_normalization import normalize_email
from urbanlens.dashboard.services.auth.username import normalize_username_key


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


BATCH = 1000


def _extract(text):
    """Location uuids named by one comment's text.

    Deliberately the same parser the application uses rather than a copy: the
    rows this backfill writes are an index of the text, and an index built by a
    second implementation of the rule would be wrong wherever the two differ.
    """
    from urbanlens.dashboard.services.notifications.mentions import extract_location_uuids

    return extract_location_uuids(text or "")


def _0037_backfill(apps, schema_editor):
    """Record what every existing comment already names."""
    from urbanlens.dashboard.services.notifications.mentions import LOCATION_MENTION_MARKER

    Comment = apps.get_model("dashboard", "Comment")
    Mention = apps.get_model("dashboard", "CommentLocationMention")
    rows = []
    for comment_id, text in Comment.objects.filter(text__contains=LOCATION_MENTION_MARKER).values_list("id", "text").iterator(chunk_size=BATCH):
        rows.extend(Mention(comment_id=comment_id, location_uuid=value) for value in set(_extract(text)))
        if len(rows) >= BATCH:
            Mention.objects.bulk_create(rows, ignore_conflicts=True)
            rows = []
    if rows:
        Mention.objects.bulk_create(rows, ignore_conflicts=True)


def _0037_drop(apps, schema_editor):
    """Remove every derived row, so the migration reverses cleanly."""
    apps.get_model("dashboard", "CommentLocationMention").objects.all().delete()


_0039_BATCH = 1000


def _0039_backfill(apps, schema_editor):
    """Record what every existing trip comment already names."""
    from urbanlens.dashboard.services.notifications.mentions import LOCATION_MENTION_MARKER, extract_location_uuids

    TripComment = apps.get_model("dashboard", "TripComment")
    Mention = apps.get_model("dashboard", "CommentLocationMention")
    rows = []
    for comment_id, text in TripComment.objects.filter(text__contains=LOCATION_MENTION_MARKER).values_list("id", "text").iterator(chunk_size=_0039_BATCH):
        rows.extend(Mention(trip_comment_id=comment_id, location_uuid=value) for value in set(extract_location_uuids(text or "")))
        if len(rows) >= _0039_BATCH:
            Mention.objects.bulk_create(rows, ignore_conflicts=True)
            rows = []
    if rows:
        Mention.objects.bulk_create(rows, ignore_conflicts=True)


def _0039_drop(apps, schema_editor):
    """Remove only the trip-side rows, leaving 0037's alone."""
    apps.get_model("dashboard", "CommentLocationMention").objects.filter(trip_comment__isnull=False).delete()


def _0058_trust_verified_signups(apps, schema_editor):
    """Treat the current primary of every account that verified its signup address as verified.

    Nothing recorded which address was verified, so an address changed since signup is trusted too.
    """
    Profile = apps.get_model("dashboard", "Profile")
    Profile.objects.filter(user__email_verification__verified_at__isnull=False).update(verified_primary_email=F("primary_email_normalized"))


def _0062_backfill_username_keys(apps, schema_editor):
    Profile = apps.get_model("dashboard", "Profile")
    for pk, key, username in Profile.objects.values_list("pk", "username_key", "user__username").iterator():
        current = normalize_username_key(username or "")
        if current != key:
            Profile.objects.filter(pk=pk).update(username_key=current)


def _refold_googlemail(apps, normalize) -> None:
    """Recompute every stored form of a googlemail.com address from the address itself, under ``normalize``."""
    Profile = apps.get_model("dashboard", "Profile")
    ProfileEmail = apps.get_model("dashboard", "ProfileEmail")
    FriendInvitation = apps.get_model("dashboard", "FriendInvitation")
    TripInvitation = apps.get_model("dashboard", "TripInvitation")
    for pk, email, verified in list(Profile.objects.filter(user__email__iendswith="@googlemail.com").values_list("pk", "user__email", "verified_primary_email")):
        fields = {"primary_email_normalized": normalize(email)}
        if verified in {normalize_email(email), _pre_0062_normalize(email)}:
            fields["verified_primary_email"] = normalize(email)
        _update(Profile, pk, **fields)
    for pk, email in list(FriendInvitation.objects.filter(email__iendswith="@googlemail.com").values_list("pk", "email")):
        _update(FriendInvitation, pk, email_normalized=normalize(email))
    for row in list(ProfileEmail.objects.only("pk", "email")):
        if _is_googlemail(row.email):
            _update(ProfileEmail, row.pk, normalized_email=normalize(row.email))
    for row in list(TripInvitation.objects.only("pk", "email")):
        if _is_googlemail(row.email):
            _update(TripInvitation, row.pk, email_hash=hashlib.sha256(normalize(row.email).encode("utf-8")).hexdigest())


def _update(model, pk: int, **fields) -> None:
    """Update one row, leaving it as it was when the new value would collide with another row's."""
    try:
        with transaction.atomic():
            model.objects.filter(pk=pk).update(**fields)
    except IntegrityError:
        pass


def _pre_0062_normalize(email: str) -> str:
    """normalize_email as it was before this migration: googlemail.com kept its own domain."""
    local, _, domain = email.strip().lower().rpartition("@")
    if not local or domain not in {"gmail.com", "googlemail.com"}:
        return email.strip().lower()
    mailbox = local.split("+", 1)[0].replace(".", "")
    return f"{mailbox}@{domain}" if mailbox else email.strip().lower()


def _is_googlemail(email: str | None) -> bool:
    return (email or "").strip().lower().rstrip(".").endswith("@googlemail.com")


def _0062_fold_googlemail_into_gmail(apps, schema_editor):
    """googlemail.com is the same mailbox as gmail.com, and now normalizes to it."""
    _refold_googlemail(apps, normalize_email)


def _0062_unfold_googlemail(apps, schema_editor):
    _refold_googlemail(apps, _pre_0062_normalize)


def _0065_backfill(apps, schema_editor):
    from urbanlens.dashboard.services.auth.email_normalization import normalize_email

    Contact = apps.get_model("dashboard", "SafetyCheckinContact")
    changed = []
    for contact in Contact.objects.exclude(email__isnull=True).exclude(email="").only("pk", "email").iterator():
        contact.email_normalized = normalize_email(contact.email)
        changed.append(contact)
    Contact.objects.bulk_update(changed, ["email_normalized"], batch_size=1000)


def _0067_release_stale_and_duplicate_proofs(apps, schema_editor):
    Profile = apps.get_model("dashboard", "Profile")
    Profile.objects.exclude(verified_primary_email="").exclude(verified_primary_email=F("primary_email_normalized")).update(verified_primary_email="")
    duplicated = Profile.objects.exclude(verified_primary_email="").values("verified_primary_email").annotate(n=Count("pk")).filter(n__gt=1).values_list("verified_primary_email", flat=True)
    for address in list(duplicated):
        holders = Profile.objects.filter(verified_primary_email=address).order_by("-user__is_active", "pk")
        keep = holders.values_list("pk", flat=True).first()
        holders.exclude(pk=keep).update(verified_primary_email="")


def _later_duplicates(rows):
    """Ids after the first of each key, from ``(id, *key)`` tuples ordered by key then id."""
    seen = set()
    extra = []
    for row_id, *parts in rows:
        key = tuple(parts)
        if key in seen:
            extra.append(row_id)
        else:
            seen.add(key)
    return extra


def _0073_dedupe(apps, schema_editor):
    PinVisit = apps.get_model("dashboard", "PinVisit")
    PinSuggestion = apps.get_model("dashboard", "PinSuggestion")
    MarkupMapShare = apps.get_model("dashboard", "MarkupMapShare")
    DeviceScanUpload = apps.get_model("dashboard", "DeviceScanUpload")
    visits = PinVisit.objects.filter(source="geolocation").annotate(day=TruncDate("visited_at", tzinfo=ZoneInfo("UTC"))).order_by("pin_id", "day", "id").values_list("id", "pin_id", "day")
    suggestions = PinSuggestion.objects.filter(origin="community", location__isnull=False).order_by("profile_id", "location_id", "id").values_list("id", "profile_id", "location_id")
    shares = MarkupMapShare.objects.order_by("markup_map_id", "from_profile_id", "to_profile_id", "id").values_list("id", "markup_map_id", "from_profile_id", "to_profile_id")
    uploads = DeviceScanUpload.objects.exclude(client_session_uuid="").order_by("client_session_uuid", "id").values_list("id", "client_session_uuid")
    for model, rows in ((PinVisit, visits), (PinSuggestion, suggestions), (MarkupMapShare, shares), (DeviceScanUpload, uploads)):
        extra = _later_duplicates(rows.iterator())
        if extra:
            model.objects.filter(pk__in=extra).delete()


_SOURCE = "redata_weather_history"


def _0091__move_to_day_rows(apps, schema_editor) -> None:
    LocationCache = apps.get_model("dashboard", "LocationCache")
    RecordedWeatherDay = apps.get_model("dashboard", "RecordedWeatherDay")
    rows: dict[tuple[int, int, date], dict] = {}
    entries = LocationCache.objects.filter(source=_SOURCE).select_related("location")
    for entry in entries.iterator(chunk_size=200):
        location = entry.location
        if location.latitude is None or location.longitude is None or (not isinstance(entry.data, dict)):
            continue
        cell = (round(float(location.latitude) * 100), round(float(location.longitude) * 100))
        for iso, record in entry.data.items():
            try:
                day = date.fromisoformat(iso)
            except (TypeError, ValueError):
                continue
            if isinstance(record, dict):
                rows.setdefault((*cell, day), record)
    RecordedWeatherDay.objects.bulk_create([RecordedWeatherDay(cell_lat=lat, cell_lng=lng, day=day, data=data) for (lat, lng, day), data in rows.items()], batch_size=1000)
    LocationCache.objects.filter(source=_SOURCE).delete()


_EARLIEST = datetime.datetime(1900, 1, 1, tzinfo=datetime.UTC)
_LATEST = datetime.datetime(2200, 1, 1, tzinfo=datetime.UTC)


def _0092__clear_out_of_span(apps, schema_editor) -> None:
    TripActivity = apps.get_model("dashboard", "TripActivity")
    for field in ("scheduled_at", "scheduled_end"):
        TripActivity.objects.filter(Q(**{f"{field}__lt": _EARLIEST}) | Q(**{f"{field}__gte": _LATEST})).update(**{field: None})


_MAX_CUSTOM_FIELD_TEXT_LENGTH = 5000


def _repair(queryset, column):
    from urbanlens.dashboard.services.security.link_urls import InvalidLinkUrlError, clean_link_url

    unusable = []
    for pk, url in queryset.values_list("pk", column).iterator():
        try:
            cleaned = clean_link_url(url, max_length=_MAX_LINK_URL_LENGTH)
        except InvalidLinkUrlError:
            unusable.append(pk)
            continue
        if cleaned != url:
            queryset.filter(pk=pk).update(**{column: cleaned})
    return unusable


_MAX_LINK_URL_LENGTH = 2000


def _repair_owned_links(model, owner):
    """Repair each link's url; one that becomes a url its owner already links folds into that link."""
    from urbanlens.dashboard.services.security.link_urls import InvalidLinkUrlError, clean_link_url

    for link in model.objects.all().iterator():
        try:
            cleaned = clean_link_url(link.url, max_length=_MAX_LINK_URL_LENGTH)
        except InvalidLinkUrlError:
            link.delete()
            continue
        if cleaned == link.url:
            continue
        existing = model.objects.filter(**{owner: getattr(link, f"{owner}_id")}, url=cleaned).exclude(pk=link.pk).first()
        if existing is None:
            model.objects.filter(pk=link.pk).update(url=cleaned)
            continue
        filled = {field: getattr(link, field) for field in ("name", "wayback_url") if not getattr(existing, field) and getattr(link, field)}
        if filled:
            model.objects.filter(pk=existing.pk).update(**filled)
        link.delete()


def _0098_repair_links(apps, schema_editor):
    for model_name, owner in (("PinLink", "pin"), ("WikiLink", "wiki")):
        model = apps.get_model("dashboard", model_name)
        _repair_owned_links(model, owner)
        model.objects.filter(pk__in=_repair(model.objects.exclude(wayback_url=""), "wayback_url")).update(wayback_url="")
    CustomFieldValue = apps.get_model("dashboard", "CustomFieldValue")
    url_values = CustomFieldValue.objects.filter(field__field_type="url")
    CustomFieldValue.objects.filter(pk__in=_repair(url_values, "value_text")).delete()
    CustomFieldValue.objects.annotate(text_length=Length("value_text")).filter(text_length__gt=_MAX_CUSTOM_FIELD_TEXT_LENGTH).update(value_text=Left("value_text", _MAX_CUSTOM_FIELD_TEXT_LENGTH))


_LEGACY_KEY = "bounding_box"
_PREFIX = "boundary_"
_TOLERANCE = 1e-08


def _outline(wkt):
    from django.contrib.gis.geos import GEOSGeometry, MultiPolygon, Polygon
    from django.contrib.gis.geos.error import GEOSException

    if not isinstance(wkt, str) or not wkt.strip():
        return None
    try:
        geometry = GEOSGeometry(wkt, srid=4326)
    except (GEOSException, ValueError, TypeError):
        return None
    if isinstance(geometry, Polygon):
        geometry = MultiPolygon(geometry, srid=4326)
    return geometry if isinstance(geometry, MultiPolygon) and (not geometry.empty) else None


def _0101_convert_inline_boundaries(apps, schema_editor):
    """Move inline WKT out of WikiEdit.changes into BoundaryRevision rows named by id."""
    WikiEdit = apps.get_model("dashboard", "WikiEdit")
    BoundaryRevision = apps.get_model("dashboard", "BoundaryRevision")
    latest = {}

    def revision_id(wiki_id, boundary_type, value):
        if value is None or (isinstance(value, int) and (not isinstance(value, bool))):
            return value
        polygon = _outline(value)
        if polygon is None:
            return None
        previous = latest.get((wiki_id, boundary_type))
        if previous is not None and previous.polygon.equals_exact(polygon, _TOLERANCE):
            return previous.pk
        revision = BoundaryRevision.objects.create(wiki_id=wiki_id, boundary_type=boundary_type, polygon=polygon)
        latest[wiki_id, boundary_type] = revision
        return revision.pk

    for edit in WikiEdit.objects.order_by("wiki_id", "pk").iterator():
        changes = edit.changes if isinstance(edit.changes, dict) else {}
        if not any(key == _LEGACY_KEY or key.startswith(_PREFIX) for key in changes):
            continue
        converted = {}
        for key, diff in changes.items():
            if key != _LEGACY_KEY and (not key.startswith(_PREFIX)):
                converted[key] = diff
                continue
            if not isinstance(diff, dict):
                continue
            new_key = f"{_PREFIX}property" if key == _LEGACY_KEY else key
            boundary_type = new_key.removeprefix(_PREFIX)
            converted[new_key] = {"from": revision_id(edit.wiki_id, boundary_type, diff.get("from")), "to": revision_id(edit.wiki_id, boundary_type, diff.get("to"))}
        WikiEdit.objects.filter(pk=edit.pk).update(changes=converted)


def _0101_restore_inline_boundaries(apps, schema_editor):
    """Put each referenced outline back into WikiEdit.changes as WKT, the shape the pre-migration code reads."""
    WikiEdit = apps.get_model("dashboard", "WikiEdit")
    BoundaryRevision = apps.get_model("dashboard", "BoundaryRevision")

    def is_id(value):
        return isinstance(value, int) and (not isinstance(value, bool))

    for edit in WikiEdit.objects.order_by("pk").iterator():
        changes = edit.changes if isinstance(edit.changes, dict) else {}
        boundary_keys = [key for key, diff in changes.items() if key.startswith(_PREFIX) and isinstance(diff, dict)]
        if not boundary_keys:
            continue
        ids = {value for key in boundary_keys for value in changes[key].values() if is_id(value)}
        wkt = {revision.pk: revision.polygon.wkt for revision in BoundaryRevision.objects.filter(pk__in=ids)}
        restored = dict(changes)
        for key in boundary_keys:
            restored[key] = {side: wkt.get(value) if is_id(value) else value for side, value in changes[key].items()}
        WikiEdit.objects.filter(pk=edit.pk).update(changes=restored)


def _0116_mark_linked(apps, schema_editor):
    Image = apps.get_model("dashboard", "Image")
    Image.objects.filter(media_source_key="external_url", source="upload").update(source="linked_url")


def _0117_date_existing_blocks(apps, schema_editor):
    """Give every block already in place a time; its row's last update is the closest record of when it began."""
    Friendship = apps.get_model("dashboard", "Friendship")
    Friendship.objects.filter(status="Blocked", blocked_at__isnull=True).update(blocked_at=models.F("updated"))


def _flush_deferred_constraints(apps, schema_editor):
    schema_editor.connection.check_constraints()


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0031_v0_7_0_indexes"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.AddField(model_name="image", name="thumbnail", field=models.ImageField(blank=True, max_length=255, null=True, upload_to=urbanlens.dashboard.models.images.model.pin_image_thumbnail_path)),
        migrations.AddField(model_name="image", name="map_hidden", field=models.BooleanField(default=False)),
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
        migrations.AddField(model_name="image", name="upload_processed_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="friendinvitation", name="email_normalized", field=models.CharField(blank=True, db_index=True, default="", max_length=254)),
        migrations.RunPython(code=_0049_backfill_friendinvitation_email_normalized, reverse_code=_0049_noop),
        migrations.RunPython(_flush_deferred_constraints, _flush_deferred_constraints),
        migrations.AddField(model_name="image", name="analysis_thumbnail", field=models.ImageField(blank=True, max_length=255, null=True, upload_to=urbanlens.dashboard.models.images.model.pin_image_analysis_thumbnail_path)),
        migrations.AddField(model_name="wikiedit", name="is_revert", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="wikiedit", name="consensus_points", field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(model_name="wikiedit", name="consensus_points_retracted", field=models.BooleanField(default=False)),
        migrations.RunPython(code=_0052__backfill, reverse_code=django.db.migrations.operations.special.RunPython.noop),
        migrations.RunPython(_flush_deferred_constraints, _flush_deferred_constraints),
        migrations.AddField(model_name="image", name="upload_failed_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="image", name="upload_sweep_attempts", field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(model_name="photouploadfailure", name="image", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="upload_failures", to="dashboard.image")),
        migrations.AddField(model_name="photouploadfailure", name="kind", field=models.CharField(choices=[("upload_rejected", "Upload rejected"), ("processing_failed", "Processing failed")], default="upload_rejected", max_length=20)),
        migrations.AddField(model_name="photouploadfailure", name="user_retries", field=models.PositiveSmallIntegerField(default=0)),
        migrations.RunPython(code=_0054_merge_reciprocal_rows, reverse_code=django.db.migrations.operations.special.RunPython.noop),
        migrations.RunPython(_flush_deferred_constraints, _flush_deferred_constraints),
        migrations.AddField(model_name="reputationevent", name="weight", field=models.DecimalField(decimal_places=4, default=Decimal(1), max_digits=6)),
        migrations.AddField(model_name="reputationevent", name="weight_reason", field=models.CharField(blank=True, default="", max_length=64)),
        migrations.AddField(model_name="tripcomment", name="parent_deleted", field=models.BooleanField(default=False)),
        migrations.AddField(
            model_name="apicalllog",
            name="profile",
            field=models.ForeignKey(
                blank=True,
                help_text="Whose behalf this call was made on, from the actor bound for the request. Null for the site's own scheduled work, which is nobody's consumption, and for a call whose account has since been deleted - the usage stays in the record, unattributed.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="api_calls",
                to="dashboard.profile",
            ),
        ),
        migrations.CreateModel(
            name="CommentLocationMention",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("location_uuid", models.UUIDField()),
                ("comment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="location_mentions", to="dashboard.comment")),
            ],
            options={
                "db_table": "dashboard_comment_location_mentions",
                "ordering": ["id"],
                "abstract": False,
                "indexes": [models.Index(fields=["location_uuid"], name="idxdb_cmtloc_uuid")],
                "constraints": [models.UniqueConstraint(fields=("comment", "location_uuid"), name="uq_cmtloc_one_per_comment")],
            },
        ),
        migrations.RunPython(code=_0037_backfill, reverse_code=_0037_drop),
        migrations.RunPython(_flush_deferred_constraints, _flush_deferred_constraints),
        migrations.AddField(model_name="commentlocationmention", name="trip_comment", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="location_mentions", to="dashboard.tripcomment")),
        migrations.AlterField(model_name="commentlocationmention", name="comment", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="location_mentions", to="dashboard.comment")),
        migrations.AddConstraint(model_name="commentlocationmention", constraint=models.UniqueConstraint(fields=("trip_comment", "location_uuid"), name="uq_cmtloc_one_per_trip_comment")),
        migrations.RunPython(code=_0039_backfill, reverse_code=_0039_drop),
        migrations.RunPython(_flush_deferred_constraints, _flush_deferred_constraints),
        migrations.AddField(model_name="image", name="media_unreadable_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="image", name="embedded_keywords", field=urbanlens.dashboard.models.fields.EncryptedJSONField(blank=True, fail_soft=True, null=True)),
        migrations.AddField(model_name="achievement", name="custom_icon_upload", field=models.CharField(blank=True, default="", max_length=255)),
        migrations.AddField(model_name="label", name="custom_icon_upload", field=models.CharField(blank=True, default="", max_length=255)),
        migrations.AddField(model_name="pin", name="custom_icon_upload", field=models.CharField(blank=True, default="", max_length=255)),
        migrations.AddField(model_name="profile", name="avatar_upload", field=models.CharField(blank=True, default="", max_length=255)),
        migrations.AddField(
            model_name="sitesettings",
            name="notify_stuck_uploads_email",
            field=models.BooleanField(default=True, help_text="Email the admin notification address when an upload has kept failing for a day while storage accepts others.", verbose_name="Stuck uploads (email)"),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="notify_stuck_uploads_gotify",
            field=models.BooleanField(default=False, help_text="Send a Gotify push notification when an upload has kept failing for a day while storage accepts others.", verbose_name="Stuck uploads (Gotify)"),
        ),
        migrations.CreateModel(
            name="UploadRetry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("target", models.CharField(max_length=100)),
                ("object_id", models.PositiveBigIntegerField()),
                ("name", models.CharField(max_length=255)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("next_attempt_at", models.DateTimeField()),
                ("last_error", models.CharField(blank=True, default="", max_length=255)),
                ("gone_since", models.DateTimeField(blank=True, null=True)),
                ("admin_notified_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "db_table": "dashboard_upload_retries",
                "ordering": ["next_attempt_at"],
                "abstract": False,
                "constraints": [models.UniqueConstraint(fields=("target", "object_id"), name="uq_uploadretry_target_object")],
                "indexes": [models.Index(fields=["next_attempt_at"], name="idxdb_uploadretry_next")],
            },
        ),
        migrations.AlterField(model_name="groupkeyenvelope", name="profile", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="group_key_envelopes", to="dashboard.profile")),
        migrations.AddField(model_name="profile", name="map_center_stale_since", field=models.DateTimeField(blank=True, null=True)),
        migrations.RunSQL(sql="CREATE EXTENSION IF NOT EXISTS vector", reverse_sql=""),
        migrations.AddField(model_name="notificationlog", name="direct_message", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="notifications", to="dashboard.directmessage")),
        migrations.AddField(model_name="notificationlog", name="group_message", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="notifications", to="dashboard.groupmessage")),
        migrations.AlterField(model_name="tripcomment", name="author", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="trip_comments", to="dashboard.profile")),
        migrations.RunSQL(sql="CREATE EXTENSION IF NOT EXISTS pg_stat_statements", reverse_sql=""),
        migrations.AddField(model_name="pin", name="auto_nested_buildings", field=models.JSONField(blank=True, default=list)),
        migrations.AlterField(
            model_name="profile",
            name="common_pins_visibility",
            field=models.CharField(
                choices=[
                    ("anyone", "Anyone (Logged In)"),
                    ("anything_in_common", "Users with anything in common"),
                    ("common_pin", "Users with a pin in common"),
                    ("common_friend", "Users with a friend in common"),
                    ("common_trip", "Users with a trip in common"),
                    ("friends", "Friends Only"),
                    ("no_one", "No one"),
                ],
                default="no_one",
                help_text="Who can see the specific pins you have in common with them. Requires both of you to allow it.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="apicalllog",
            name="status_code",
            field=models.PositiveSmallIntegerField(blank=True, help_text="The upstream's HTTP status. Null when no response arrived (refused before sending, a network error) or the caller had none to record.", null=True),
        ),
        migrations.AlterField(
            model_name="emailsendlog",
            name="email_type",
            field=models.CharField(choices=[("join_invite", "Friend invitation"), ("visit_invite", "Visit participant invitation"), ("email_verification", "Secondary-email verification"), ("trip_invite", "Trip invitation")], max_length=20),
        ),
        migrations.AddField(model_name="profile", name="verified_primary_email", field=models.CharField(blank=True, default="", max_length=254)),
        migrations.CreateModel(
            name="TripInvitation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("uuid", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("email", urbanlens.dashboard.models.fields.EncryptedTextField(blank=True, default="", fail_soft=True)),
                ("email_hash", models.CharField(db_index=True, max_length=64)),
                ("token", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("expires_at", models.DateTimeField()),
                ("trip_response", models.CharField(choices=[("pending", "Pending"), ("accepted", "Accepted"), ("declined", "Declined")], default="pending", max_length=10)),
                ("friend_response", models.CharField(choices=[("pending", "Pending"), ("accepted", "Accepted"), ("declined", "Declined")], default="pending", max_length=10)),
                ("invitee", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="received_trip_invitations", to="dashboard.profile")),
                ("inviter", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sent_trip_invitations", to="dashboard.profile")),
                ("trip", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="email_invitations", to="dashboard.trip")),
            ],
            options={"db_table": "dashboard_trip_invitations", "abstract": False, "constraints": [models.UniqueConstraint(fields=("trip", "inviter", "email_hash"), name="trip_invitation_one_per_inviter_address")]},
        ),
        migrations.RunPython(code=_0058_trust_verified_signups, reverse_code=django.db.migrations.operations.special.RunPython.noop),
        migrations.RunPython(_flush_deferred_constraints, _flush_deferred_constraints),
        migrations.AddField(model_name="emailsendlog", name="delivered", field=models.BooleanField(default=True)),
        migrations.AddField(model_name="friendinvitation", name="declined_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="friendinvitation", name="invitee", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="received_friend_invitations", to="dashboard.profile")),
        migrations.AddField(model_name="profileemail", name="promote_on_verify", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="profile", name="username_key", field=models.TextField(blank=True, db_default="", default="")),
        migrations.RunPython(code=_0062_backfill_username_keys, reverse_code=django.db.migrations.operations.special.RunPython.noop),
        migrations.RunPython(code=_0062_fold_googlemail_into_gmail, reverse_code=_0062_unfold_googlemail),
        migrations.RunPython(_flush_deferred_constraints, _flush_deferred_constraints),
        migrations.AddField(model_name="safetycheckincontact", name="email_normalized", field=models.CharField(blank=True, default="", max_length=254)),
        migrations.RunPython(code=_0065_backfill, reverse_code=django.db.migrations.operations.special.RunPython.noop),
        migrations.RunPython(code=_0067_release_stale_and_duplicate_proofs, reverse_code=django.db.migrations.operations.special.RunPython.noop),
        migrations.RunPython(_flush_deferred_constraints, _flush_deferred_constraints),
        migrations.AlterField(
            model_name="emailsendlog",
            name="email_type",
            field=models.CharField(choices=[("join_invite", "Friend invitation"), ("visit_invite", "Visit participant invitation"), ("email_verification", "Email verification"), ("trip_invite", "Trip invitation")], max_length=20),
        ),
        migrations.RemoveIndex(model_name="image", name="idxdb_image_profile_quota"),
        migrations.AddField(
            model_name="rolesubscription",
            name="stripe_state_at",
            field=models.DateTimeField(blank=True, help_text="Stripe-side time of the latest subscription state applied here: an event's created time, or when a live retrieve was sent. Older subscription events are ignored.", null=True),
        ),
        migrations.RemoveConstraint(model_name="rolesubscription", name="unique_active_role_subscription"),
        migrations.AlterField(
            model_name="sitesettings",
            name="environment_override",
            field=models.CharField(
                choices=[("default", "Default (from environment variable)"), ("production", "Production"), ("development", "Development"), ("testing", "Testing"), ("staging", "Staging")],
                default="default",
                help_text="Override the deployment environment. Default uses the UL_ENVIRONMENT variable (production when unset).",
                max_length=20,
                verbose_name="Environment",
            ),
        ),
        migrations.RunPython(code=_0073_dedupe, reverse_code=django.db.migrations.operations.special.RunPython.noop),
        migrations.RunPython(_flush_deferred_constraints, _flush_deferred_constraints),
        migrations.CreateModel(
            name="RecordedWeatherDay",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("cell_lat", models.IntegerField()),
                ("cell_lng", models.IntegerField()),
                ("day", models.DateField()),
                ("data", models.JSONField(default=dict)),
            ],
            options={"db_table": "dashboard_recorded_weather_day", "abstract": False},
        ),
        migrations.RunPython(code=_0091__move_to_day_rows, reverse_code=django.db.migrations.operations.special.RunPython.noop),
        migrations.RunPython(code=_0092__clear_out_of_span, reverse_code=django.db.migrations.operations.special.RunPython.noop),
        migrations.RunPython(_flush_deferred_constraints, _flush_deferred_constraints),
        migrations.CreateModel(
            name="TaskOutboxEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("task_name", models.CharField(max_length=200)),
                ("args", models.JSONField(default=list, encoder=django.core.serializers.json.DjangoJSONEncoder)),
                ("kwargs", models.JSONField(default=dict, encoder=django.core.serializers.json.DjangoJSONEncoder)),
                ("queue", models.CharField(blank=True, default="", max_length=64)),
                ("not_before", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("next_attempt_at", models.DateTimeField()),
                ("last_error", models.CharField(blank=True, default="", max_length=255)),
            ],
            options={"db_table": "dashboard_task_outbox", "ordering": ["next_attempt_at"], "abstract": False, "indexes": [models.Index(fields=["next_attempt_at"], name="idxdb_taskoutbox_next")]},
        ),
        migrations.AddField(model_name="devicescanupload", name="attempts", field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="devicescanupload", name="claimed_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AlterField(model_name="devicescanupload", name="status", field=models.CharField(choices=[("pending", "Pending"), ("processing", "Processing"), ("processed", "Processed"), ("failed", "Failed")], default="pending", max_length=20)),
        migrations.AddField(model_name="fact", name="needs_recompute", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="tripcalendarlink", name="push_attempts", field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(model_name="tripcalendarlink", name="push_requested_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.CreateModel(
            name="TriviaGenerationAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("attempted_at", models.DateTimeField()),
                ("questions_created", models.PositiveSmallIntegerField(default=0)),
                ("wiki", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="trivia_generation_attempt", to="dashboard.wiki")),
            ],
            options={"db_table": "dashboard_trivia_generation_attempts", "abstract": False, "indexes": [models.Index(fields=["attempted_at"], name="idxdb_trivia_gen_attempted")]},
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="device_scan_retention_days",
            field=models.IntegerField(
                default=730,
                help_text="Delete device-scan uploads, with their entries and signal readings, once they are this many days old. Marker clustering ignores scans older than 720 days, so a shorter period also drops scans it still uses. 0 keeps them forever.",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(36500)],
                verbose_name="Device scan retention (days)",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="notification_retention_days",
            field=models.IntegerField(
                default=365,
                help_text="Delete notifications a user has read once they are this many days old. Unread ones are kept. 0 keeps them forever.",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(36500)],
                verbose_name="Read notification retention (days)",
            ),
        ),
        migrations.RunPython(code=_0098_repair_links, reverse_code=django.db.migrations.operations.special.RunPython.noop),
        migrations.RunPython(_flush_deferred_constraints, _flush_deferred_constraints),
        migrations.AlterField(model_name="devicescanupload", name="profile", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="device_scan_uploads", to="dashboard.profile")),
        migrations.CreateModel(
            name="BoundaryRevision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("boundary_type", models.CharField(choices=[("property", "Property"), ("building", "Building")], max_length=20)),
                ("polygon", django.contrib.gis.db.models.fields.MultiPolygonField(geography=True, srid=4326)),
                ("wiki", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="boundary_revisions", to="dashboard.wiki")),
            ],
            options={"db_table": "dashboard_boundary_revisions", "abstract": False},
        ),
        migrations.RunPython(code=_0101_convert_inline_boundaries, reverse_code=_0101_restore_inline_boundaries),
        migrations.RunPython(_flush_deferred_constraints, _flush_deferred_constraints),
        migrations.AddField(
            model_name="sitesettings",
            name="max_saved_filters_per_user",
            field=models.IntegerField(
                default=100,
                help_text="Maximum number of saved filters a user may keep. Set to 0 for unlimited.",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(10000)],
                verbose_name="Max saved filters per user",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="max_pin_lists_per_user",
            field=models.IntegerField(
                default=500,
                help_text="Maximum number of pin lists a user may own. Set to 0 for unlimited.",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100000)],
                verbose_name="Max lists per user",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="max_labels_per_user",
            field=models.IntegerField(
                default=2000,
                help_text="Maximum number of personal labels (of every kind) a user may own. Site-wide labels do not count. Set to 0 for unlimited.",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(100000)],
                verbose_name="Max labels per user",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="max_custom_fields_per_user",
            field=models.IntegerField(
                default=100,
                help_text="Maximum number of custom fields a user may define, across every entity type. Set to 0 for unlimited.",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(10000)],
                verbose_name="Max custom fields per user",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="max_push_devices_per_user",
            field=models.IntegerField(
                default=10,
                help_text="Maximum number of active push devices a user may register. Set to 0 for unlimited.",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(1000)],
                verbose_name="Max push devices per user",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="max_photos_per_album",
            field=models.IntegerField(
                default=5000,
                help_text="Maximum number of photos one album may hold. Set to 0 for unlimited.",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(1000000)],
                verbose_name="Max photos per album",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="max_group_chats_per_user",
            field=models.IntegerField(
                default=100,
                help_text="Maximum number of group chats one user may belong to at once. Nobody can add them to another past this. Set to 0 for unlimited.",
                validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(10000)],
                verbose_name="Max group chats per user",
            ),
        ),
        migrations.RemoveField(model_name="sitesettings", name="device_scan_retention_days"),
        migrations.AlterField(model_name="devicescanupload", name="profile", field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="device_scan_uploads", to="dashboard.profile")),
        migrations.AddField(model_name="devicescanentry", name="device_name", field=models.CharField(blank=True, default="", max_length=255)),
        migrations.AddField(model_name="devicescanupload", name="routable_wikis", field=models.ManyToManyField(blank=True, related_name="+", to="dashboard.wiki")),
        migrations.AddField(model_name="devicescanupload", name="routing_recorded", field=models.BooleanField(default=False)),
        migrations.CreateModel(
            name="RemoteImageCopy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("url_digest", models.CharField(editable=False, help_text="SHA-256 of source_url.", max_length=64, unique=True)),
                ("source_url", models.TextField(help_text="The image's address at its provider.")),
                ("edition", models.CharField(blank=True, default="", help_text="Which edition of a changing picture this is, e.g. the month of a current-imagery export.", max_length=16)),
                ("provider", models.CharField(blank=True, default="", help_text="Which feature or provider the image came from.", max_length=64)),
                ("page_url", models.TextField(blank=True, default="", help_text="The provider's page for the image, when known.")),
                ("file", models.FileField(blank=True, default="", max_length=255, upload_to=urbanlens.dashboard.models.remote_image_copy.model.remote_image_copy_path)),
                ("content_type", models.CharField(blank=True, default="", max_length=50)),
                ("file_size", models.PositiveIntegerField(blank=True, null=True)),
                ("checksum", models.CharField(blank=True, default="", help_text="SHA-256 of the stored file.", max_length=64)),
                ("fetched_at", models.DateTimeField(blank=True, help_text="When the source was downloaded and stored.", null=True)),
                ("failed_attempts", models.PositiveSmallIntegerField(default=0)),
                ("last_failed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"db_table": "dashboard_remote_image_copies", "abstract": False},
        ),
        migrations.RunPython(code=_0116_mark_linked, reverse_code=django.db.migrations.operations.special.RunPython.noop),
        migrations.RunPython(_flush_deferred_constraints, _flush_deferred_constraints),
        migrations.AddField(model_name="friendship", name="blocked_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.RunPython(code=_0117_date_existing_blocks, reverse_code=django.db.migrations.operations.special.RunPython.noop),
        migrations.RunPython(_flush_deferred_constraints, _flush_deferred_constraints),
        migrations.CreateModel(
            name="RemoteTileSource",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("template_digest", models.CharField(editable=False, help_text="SHA-256 of template.", max_length=64, unique=True)),
                ("template", models.TextField(help_text="The XYZ template at its host, as it was given.")),
                ("provider", models.CharField(blank=True, default="", help_text="Where the template came from, e.g. an archive import.", max_length=64)),
                ("kept_tiles", models.PositiveIntegerField(default=0)),
            ],
            options={"db_table": "dashboard_remote_tile_sources", "abstract": False},
        ),
        migrations.CreateModel(
            name="RemoteTile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("z", models.PositiveSmallIntegerField()),
                ("x", models.PositiveIntegerField()),
                ("y", models.PositiveIntegerField()),
                ("file", models.FileField(blank=True, default="", max_length=255, upload_to=urbanlens.dashboard.models.remote_tiles.model.remote_tile_path)),
                ("content_type", models.CharField(blank=True, default="", max_length=50)),
                ("absent", models.BooleanField(default=False, help_text="The host answered that it has no tile here.")),
                ("fetched_at", models.DateTimeField(blank=True, null=True)),
                ("failed_attempts", models.PositiveSmallIntegerField(default=0)),
                ("last_failed_at", models.DateTimeField(blank=True, null=True)),
                ("source", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tiles", to="dashboard.remotetilesource")),
            ],
            options={"db_table": "dashboard_remote_tiles", "abstract": False, "constraints": [models.UniqueConstraint(fields=("source", "z", "x", "y"), name="remote_tile_one_row_per_coordinate")]},
        ),
    ]
