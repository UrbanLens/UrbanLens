from decimal import Decimal
import re
import uuid

from cryptography.fernet import InvalidToken
from django.conf import settings
import django.contrib.gis.db.models.fields
import django.core.validators
from django.db import migrations, models
import django.db.migrations.operations.special
import django.db.models.deletion
import django.utils.timezone

import urbanlens.dashboard.models.fields
from urbanlens.dashboard.models.fields import EncryptedTextField, _fernet
import urbanlens.dashboard.models.images.model
import urbanlens.dashboard.models.reputation.model


def _0033_mark_existing_external_media_exempt(apps, schema_editor):
    """Backfill the exemption onto already-cached external media.

    Every existing row carrying the (media_source_key, media_item_key) gallery
    identity was materialized by services.media.media_materialize, which from
    now on stamps the exemption at creation. Without this, photos cached
    before the rule existed would keep counting against their upvoter's quota
    while identical ones cached afterwards did not.

    The community-contribution exemption is deliberately NOT backfilled: it's
    a forward-looking reward, and granting it retroactively would require
    replaying vote history against a threshold that didn't exist yet.
    """
    Image = apps.get_model("dashboard", "Image")
    Image.objects.exclude(media_source_key__isnull=True).exclude(
        media_source_key=""
    ).exclude(media_item_key__isnull=True).exclude(media_item_key="").update(
        quota_exempt_reason="external_media"
    )


_ENCRYPTED_COLUMNS = (
    ("dashboard_safety_contact_defaults", "email"),
    ("dashboard_google_calendar_accounts", "google_email"),
    ("dashboard_google_photos_accounts", "google_email"),
    ("dashboard_profiles", "area"),
    ("dashboard_profiles", "bio"),
    ("dashboard_profiles", "discord_username"),
    ("dashboard_profiles", "matrix_handle"),
    ("dashboard_profiles", "phone_number"),
    ("dashboard_profiles", "signal_username"),
    ("dashboard_profiles", "telegram_username"),
    ("dashboard_profiles", "whatsapp_number"),
    ("dashboard_profileemail", "email"),
    ("dashboard_profilenote", "content"),
)


def _decrypt_column(cursor, table: str, column: str) -> None:
    """Decrypt every Fernet-encrypted value in ``table.column`` in place.

    A value not shaped like a Fernet token (they always begin ``gAAAA`` - a
    version byte of 0x80, base64'd) is left untouched: it was written before
    the forward pass ran, or the forward pass never reached it, and
    re-processing it would corrupt real plaintext. A token-shaped value that
    no configured key can decrypt raises, aborting (and rolling back) the
    reverse rather than writing garbage where the pre-0039 code expects
    plaintext.
    """
    cursor.execute(f"SELECT id, {column} FROM {table} WHERE {column} LIKE 'gAAAA%%'")
    rows = cursor.fetchall()
    for pk, ciphertext in rows:
        plaintext = _field.from_db_value(ciphertext, None, None)
        cursor.execute(
            f"UPDATE {table} SET {column} = %s WHERE id = %s", [plaintext, pk]
        )


_field = EncryptedTextField()


def _0039_decrypt_existing_contact_and_note_fields(apps, schema_editor) -> None:
    """Real reverse for the in-place encryption above.

    ``RunPython.noop`` here would let ``migrate dashboard 0038`` *succeed*
    while leaving ciphertext in columns the pre-0039 code reads as plaintext -
    a silent-corruption rollback. Decrypting is symmetric and cheap, so the
    reverse does it properly (and raises, wholesale, if any value cannot be
    decrypted under the configured keys).
    """
    with schema_editor.connection.cursor() as cursor:
        for table, column in _ENCRYPTED_COLUMNS:
            _decrypt_column(cursor, table, column)


def _encrypt_column(cursor, table: str, column: str) -> None:
    """Encrypt every existing non-empty value in ``table.column`` in place.

    Mirrors ``encrypt_existing_tokens``/``_encrypt_column`` in
    ``0007_pinshare_bundled_with_markup_map_removed_flags.py``. Uses a raw
    cursor rather than the ORM/historical model so it works regardless of
    operation order relative to the ``AlterField``s above - the historical
    model's field type at this point in the migration is irrelevant since
    nothing here goes through Django's field conversion.
    """
    cursor.execute(
        f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL AND {column} != ''"
    )
    rows = cursor.fetchall()
    for pk, plaintext in rows:
        ciphertext = _field.get_prep_value(plaintext)
        cursor.execute(
            f"UPDATE {table} SET {column} = %s WHERE id = %s", [ciphertext, pk]
        )


def _0039_encrypt_existing_contact_and_note_fields(apps, schema_editor) -> None:
    with schema_editor.connection.cursor() as cursor:
        for table, column in _ENCRYPTED_COLUMNS:
            _encrypt_column(cursor, table, column)


def _merge(cursor, *, keep_id: int, drop_ids: list[int]) -> None:
    """Repoint everything attached to *drop_ids* onto *keep_id*, then delete them.

    Each statement is an idempotent "move what is not already there, delete the
    rest" pair rather than a bare UPDATE: a pin carrying both the surviving and a
    duplicated label would otherwise violate the through table's own
    (pin, label) uniqueness the moment the second row was repointed.
    """
    if not drop_ids:
        return
    through = (
        ("dashboard_user_pins_labels", "pin"),
        ("dashboard_wikis_labels", "wiki"),
        ("dashboard_images_labels", "image"),
    )
    for table, owner in through:
        cursor.execute(
            f"UPDATE {table} SET label_id = %s WHERE label_id = ANY(%s) AND {owner}_id NOT IN (SELECT {owner}_id FROM {table} WHERE label_id = %s)",
            [keep_id, drop_ids, keep_id],
        )
        cursor.execute(f"DELETE FROM {table} WHERE label_id = ANY(%s)", [drop_ids])
    cursor.execute(
        "UPDATE dashboard_profile_label_assignments SET label_id = %s WHERE label_id = ANY(%s) AND (author_id, subject_id) NOT IN (SELECT author_id, subject_id FROM dashboard_profile_label_assignments WHERE label_id = %s)",
        [keep_id, drop_ids, keep_id],
    )
    cursor.execute(
        "DELETE FROM dashboard_profile_label_assignments WHERE label_id = ANY(%s)",
        [drop_ids],
    )
    cursor.execute(
        "UPDATE dashboard_labels_parents SET to_label_id = %s WHERE to_label_id = ANY(%s) AND from_label_id <> %s AND from_label_id NOT IN (SELECT from_label_id FROM dashboard_labels_parents WHERE to_label_id = %s)",
        [keep_id, drop_ids, keep_id, keep_id],
    )
    cursor.execute(
        "UPDATE dashboard_labels_parents SET from_label_id = %s WHERE from_label_id = ANY(%s) AND to_label_id <> %s AND to_label_id NOT IN (SELECT to_label_id FROM dashboard_labels_parents WHERE from_label_id = %s)",
        [keep_id, drop_ids, keep_id, keep_id],
    )
    cursor.execute(
        "DELETE FROM dashboard_labels_parents WHERE from_label_id = ANY(%s) OR to_label_id = ANY(%s)",
        [drop_ids, drop_ids],
    )
    cursor.execute(
        "UPDATE dashboard_label_customizations SET label_id = %s WHERE label_id = ANY(%s) AND profile_id NOT IN (SELECT profile_id FROM dashboard_label_customizations WHERE label_id = %s)",
        [keep_id, drop_ids, keep_id],
    )
    cursor.execute(
        "DELETE FROM dashboard_label_customizations WHERE label_id = ANY(%s)",
        [drop_ids],
    )
    cursor.execute("DELETE FROM dashboard_labels WHERE id = ANY(%s)", [drop_ids])


def _0042_merge_duplicate_labels(apps, schema_editor) -> None:
    """Collapse every group that would violate the new constraint."""
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        cursor.execute(
            "\n            SELECT g.id, array_agg(p.id)\n            FROM dashboard_labels g\n            JOIN dashboard_labels p\n              ON lower(p.name) = lower(g.name) AND p.kind = g.kind AND p.profile_id IS NOT NULL\n            WHERE g.profile_id IS NULL\n            GROUP BY g.id\n            "
        )
        for keep_id, drop_ids in cursor.fetchall():
            _merge(cursor, keep_id=keep_id, drop_ids=list(drop_ids))
        cursor.execute(
            "\n            SELECT array_agg(id ORDER BY created, id)\n            FROM dashboard_labels\n            GROUP BY lower(name), profile_id, kind\n            HAVING count(*) > 1\n            "
        )
        for (ids,) in cursor.fetchall():
            _merge(cursor, keep_id=ids[0], drop_ids=list(ids[1:]))


def _0042_noop_reverse(apps, schema_editor) -> None:
    """Merging cannot be undone - the dropped rows are gone.

    Reversing the migration removes the constraint (the operation below), which
    is enough to get the schema back; the merged data stays merged.
    """


def _0046_drop_duplicate_event_links(apps, schema_editor):
    """Unlink all but the oldest link per (profile, non-empty google_event_id).

    Args:
        apps: Historical app registry.
        schema_editor: Unused; required by the RunPython signature.
    """
    TripCalendarLink = apps.get_model("dashboard", "TripCalendarLink")
    seen: set[tuple[int, str]] = set()
    doomed: list[int] = []
    rows = (
        TripCalendarLink.objects.exclude(google_event_id="")
        .order_by("created", "pk")
        .values_list("pk", "profile_id", "google_event_id")
    )
    for pk, profile_id, event_id in rows.iterator():
        key = (profile_id, event_id)
        if key in seen:
            doomed.append(pk)
        else:
            seen.add(key)
    if doomed:
        TripCalendarLink.objects.filter(pk__in=doomed).delete()


def _drop_duplicate_links(model, owner_field: str) -> None:
    """Delete all but the lowest-pk row per (owner, url).

    Args:
        model: The historical PinLink or WikiLink model.
        owner_field: Name of the owning FK column - "pin" or "wiki".
    """
    seen: set[tuple[int, str]] = set()
    doomed: list[int] = []
    rows = model.objects.order_by("pk").values_list("pk", f"{owner_field}_id", "url")
    for pk, owner_id, url in rows.iterator():
        key = (owner_id, url)
        if key in seen:
            doomed.append(pk)
        else:
            seen.add(key)
    if doomed:
        model.objects.filter(pk__in=doomed).delete()


def _0047_drop_duplicate_links(apps, schema_editor):
    """Deduplicate both link tables ahead of their unique constraints.

    Args:
        apps: Historical app registry.
        schema_editor: Unused; required by the RunPython signature.
    """
    _drop_duplicate_links(apps.get_model("dashboard", "PinLink"), "pin")
    _drop_duplicate_links(apps.get_model("dashboard", "WikiLink"), "wiki")


def _0055_protect(apps, schema_editor):
    """Set is_protected on every profile-owned "Want to Go" status label."""
    Label = apps.get_model("dashboard", "Label")
    Label.objects.filter(
        kind="status", name="Want to Go", profile__isnull=False, is_protected=False
    ).update(is_protected=True)


def _0055_unprotect(apps, schema_editor):
    """Reverse: clear the flag again.

    Reversible on purpose - this migration only flips a boolean, so the
    reverse is exact rather than the silent no-op an irreversible data
    migration would leave behind.
    """
    Label = apps.get_model("dashboard", "Label")
    Label.objects.filter(
        kind="status", name="Want to Go", profile__isnull=False, is_protected=True
    ).update(is_protected=False)


_SOURCE = "searxng_images"


def _0056_clear_empty_image_caches(apps, schema_editor):
    """Delete empty ``searxng_images`` cache rows so they are fetched again.

    Deliberately narrow: only this source, and only rows with no results. A row
    with results is real data. A *genuine* empty result is indistinguishable
    from a cached outage at this distance, so the trade is re-running a cheap
    search for the pins that legitimately have no images, rather than leaving
    the ones that do permanently blank.
    """
    LocationCache = apps.get_model("dashboard", "LocationCache")
    doomed = [
        row_id
        for row_id, data in LocationCache.objects.filter(source=_SOURCE)
        .values_list("id", "data")
        .iterator(chunk_size=2000)
        if not (isinstance(data, dict) and data.get("items"))
    ]
    for start in range(0, len(doomed), 1000):
        LocationCache.objects.filter(id__in=doomed[start : start + 1000]).delete()


def _0056_keep_cleared_caches_cleared(apps, schema_editor):
    """Nothing to restore.

    The deleted rows held no information - that was the defect. Recreating them
    would re-assert "no photographs here", the state this clears. Written as a
    named function rather than ``RunPython.noop`` so the reverse is an explicit
    statement rather than an oversight (see the noop-reverse guard in
    ``test_migration_noop_reverse_guard.py``).
    """


_AUTO_NAME = re.compile("^(?:Ground floor|Level -?\\d+)$")


def _0062_clear_generated_names(apps, schema_editor) -> None:
    """Blank the floor names no one actually chose.

    The editor persisted ``"Ground floor"`` and ``"Level N"`` as real values,
    which is why a floor could not tell you which storey it was once renamed:
    there was nothing to fall back to. A derived label now covers that case, so
    these stop being data.
    """
    floor_model = apps.get_model("dashboard", "FloorplanFloor")
    for floor in floor_model.objects.exclude(name="").iterator():
        if _AUTO_NAME.match(floor.name or ""):
            floor_model.objects.filter(pk=floor.pk).update(name="")


def _0062_renumber_levels(apps, schema_editor) -> None:
    """Make every plan's levels contiguous, holding its ground datum.

    Required before the unique constraint below: a mid-stack delete used to
    leave a gap, and nothing ever stopped two floors sharing a level. Whichever
    floor sits nearest the old datum keeps level 0, so a repair never silently
    moves which storey the author considers the ground.
    """
    floor_model = apps.get_model("dashboard", "FloorplanFloor")
    floorplan_ids = floor_model.objects.values_list(
        "floorplan_id", flat=True
    ).distinct()
    for floorplan_id in floorplan_ids:
        floors = list(
            floor_model.objects.filter(floorplan_id=floorplan_id).order_by(
                "level", "sort_order", "id"
            )
        )
        if not floors:
            continue
        ground = min(range(len(floors)), key=lambda index: abs(floors[index].level))
        for index, floor in enumerate(floors):
            target = index - ground
            if floor.level != target:
                floor_model.objects.filter(pk=floor.pk).update(level=target)


_TABLE = "dashboard_images"
_0066__field = EncryptedTextField()
_COLUMN = "exif_data"


def _0066_decrypt_existing_exif_data(apps, schema_editor) -> None:
    """Real reverse: decrypt back to plaintext JSON text before the column returns to jsonb.

    Per "Migration rollbacks decrypt" in docs/DATA_ENCRYPTION.md. A value not
    shaped like a Fernet token (they begin ``gAAAA``) is left alone - it was
    written before the forward pass, and re-processing it would corrupt real
    plaintext. A token-shaped value no key can decrypt raises, aborting and
    rolling back the reverse rather than leaving something the pre-0066 code
    would try to parse as JSON.

    Runs before the ``AlterField`` is undone, because Django reverses operations
    in reverse order - so the column is still ``text`` here.
    """
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f"SELECT id, {_COLUMN} FROM {_TABLE} WHERE {_COLUMN} LIKE 'gAAAA%%'"
        )
        for pk, ciphertext in cursor.fetchall():
            cursor.execute(
                f"UPDATE {_TABLE} SET {_COLUMN} = %s WHERE id = %s",
                [_0066__field.from_db_value(ciphertext, None, None), pk],
            )


def _0066__already_encrypted(value: str) -> bool:
    """True when ``value`` is already ciphertext this install can read.

    Keeps the pass idempotent, as in 0048 - re-encrypting ciphertext yields a
    value whose single decrypt returns the inner ciphertext, which looks like
    permanent corruption.

    Args:
        value: The raw stored column value.

    Returns:
        True when the value decrypts under some configured key.
    """
    try:
        _fernet().decrypt(value.encode())
    except (InvalidToken, UnicodeEncodeError):
        return False
    return True


def _0066_encrypt_existing_exif_data(apps, schema_editor) -> None:
    """Encrypt every pre-existing EXIF snapshot in place.

    Raw cursor rather than the ORM, as in 0039/0048, so the pass is unaffected
    by the historical model's field type and by its order relative to the
    ``AlterField``. Without it every pre-existing row would read as an
    undecryptable value on first load after deploy.
    """
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            f"SELECT id, {_COLUMN} FROM {_TABLE} WHERE {_COLUMN} IS NOT NULL AND {_COLUMN} != ''"
        )
        for pk, stored in cursor.fetchall():
            if _0066__already_encrypted(stored):
                continue
            cursor.execute(
                f"UPDATE {_TABLE} SET {_COLUMN} = %s WHERE id = %s",
                [_0066__field.get_prep_value(stored), pk],
            )


class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0029_saved_filter_color_opacity"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.AlterField(
            model_name="image",
            name="image",
            field=models.ImageField(
                max_length=255,
                upload_to=urbanlens.dashboard.models.images.model.pin_image_upload_path,
            ),
        ),
        migrations.AddField(
            model_name="profile",
            name="show_supporter_badge",
            field=models.BooleanField(
                default=True,
                help_text="Show a small supporter badge next to your name when you have an active subscription.",
            ),
        ),
        migrations.CreateModel(
            name="Album",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "uuid",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("slug", models.SlugField(blank=True, max_length=255, null=True)),
                ("name", models.CharField(max_length=100)),
                (
                    "description",
                    models.TextField(blank=True, default="", max_length=50000),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("plain", "Album"), ("timelapse", "Timelapse")],
                        db_index=True,
                        default="plain",
                        max_length=20,
                    ),
                ),
                ("manual_order", models.BooleanField(default=False)),
                (
                    "cover_image",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="album_covers",
                        to="dashboard.image",
                    ),
                ),
                (
                    "parent_pin",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="albums",
                        to="dashboard.pin",
                    ),
                ),
                (
                    "parent_wiki",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="albums",
                        to="dashboard.wiki",
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="albums",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_albums",
                "ordering": ["name"],
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="AlbumItem",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("order", models.IntegerField(default=0)),
                (
                    "added_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="album_items_added",
                        to="dashboard.profile",
                    ),
                ),
                (
                    "album",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="dashboard.album",
                    ),
                ),
                (
                    "image",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="album_memberships",
                        to="dashboard.image",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_album_items",
                "ordering": ["order", "created"],
                "abstract": False,
            },
        ),
        migrations.AddConstraint(
            model_name="album",
            constraint=models.UniqueConstraint(
                fields=("parent_pin", "slug"), name="uq_album_pin_slug"
            ),
        ),
        migrations.AddConstraint(
            model_name="album",
            constraint=models.UniqueConstraint(
                fields=("parent_wiki", "slug"), name="uq_album_wiki_slug"
            ),
        ),
        migrations.AddConstraint(
            model_name="albumitem",
            constraint=models.UniqueConstraint(
                fields=("album", "image"), name="uq_album_item"
            ),
        ),
        migrations.AddField(
            model_name="image",
            name="quota_exempt_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("external_media", "Cached external media"),
                    ("community", "Community-valued contribution"),
                ],
                db_index=True,
                default="",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="community_photo_quota_bonus_votes",
            field=models.IntegerField(
                default=5,
                help_text="How many other users must mark one of a user's wiki-shared photos as relevant before that photo stops counting against their storage quota. The uploader's own vote never counts. Set to 0 to turn the reward off.",
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(10000),
                ],
                verbose_name="Relevant votes earning a quota bonus",
            ),
        ),
        migrations.RunPython(
            code=_0033_mark_existing_external_media_exempt,
            reverse_code=django.db.migrations.operations.special.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="image",
            name="quota_exempt_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("external_media", "Cached external media"),
                    ("community", "Community-valued contribution"),
                ],
                default="",
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="MapImageOverlay",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "uuid",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("name", models.CharField(blank=True, default="", max_length=100)),
                ("image_url", models.URLField(blank=True, default="", max_length=1000)),
                ("nw_latitude", models.FloatField()),
                ("nw_longitude", models.FloatField()),
                ("ne_latitude", models.FloatField()),
                ("ne_longitude", models.FloatField()),
                ("se_latitude", models.FloatField()),
                ("se_longitude", models.FloatField()),
                ("sw_latitude", models.FloatField()),
                ("sw_longitude", models.FloatField()),
                (
                    "opacity",
                    models.IntegerField(
                        default=70,
                        validators=[
                            django.core.validators.MinValueValidator(0),
                            django.core.validators.MaxValueValidator(100),
                        ],
                    ),
                ),
                ("order", models.IntegerField(default=0)),
                ("default_visible", models.BooleanField(default=True)),
                ("locked", models.BooleanField(default=False)),
                (
                    "image",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="map_overlays",
                        to="dashboard.image",
                    ),
                ),
                (
                    "layer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="image_overlays",
                        to="dashboard.customlayer",
                    ),
                ),
                (
                    "parent_pin",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="image_overlays",
                        to="dashboard.pin",
                    ),
                ),
                (
                    "parent_wiki",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="image_overlays",
                        to="dashboard.wiki",
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="image_overlays",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "ordering": ["order", "id"],
                "abstract": False,
                "indexes": [
                    models.Index(
                        fields=["parent_pin", "order"], name="idx_overlay_pin_order"
                    ),
                    models.Index(
                        fields=["parent_wiki", "order"], name="idx_overlay_wiki_order"
                    ),
                ],
            },
        ),
        migrations.AlterField(
            model_name="image",
            name="source",
            field=models.CharField(
                choices=[
                    ("upload", "Upload"),
                    ("yelp", "Yelp"),
                    ("google_images", "Google Images"),
                    ("google_maps", "Google Maps"),
                    ("wikimedia", "Wikimedia Commons"),
                    ("wikipedia_media", "Wikipedia"),
                    ("smithsonian", "Smithsonian Open Access"),
                    ("library_of_congress", "Library of Congress"),
                    ("internet_archive", "Internet Archive"),
                    ("digital_commonwealth", "Digital Commonwealth"),
                    ("immich", "Immich"),
                    ("flickr", "Flickr"),
                    ("google_photos", "Google Photos"),
                    ("loopnet", "LoopNet"),
                    ("cris", "NY Historic Preservation (CRIS)"),
                    ("external_api", "External app"),
                    ("google_street_view", "Google Street View"),
                    ("google_satellite", "Google Satellite"),
                ],
                default="upload",
                max_length=30,
            ),
        ),
        migrations.RemoveField(model_name="sitesettings", name="search_provider"),
        migrations.RemoveIndex(model_name="comment", name="idxdb_comment_uuid"),
        migrations.RemoveIndex(model_name="image", name="idxdb_image_uuid"),
        migrations.RemoveIndex(model_name="label", name="idxdb_label_uuid"),
        migrations.RemoveIndex(model_name="pin", name="idxdb_pin_uuid"),
        migrations.RemoveIndex(model_name="pinvisit", name="idxdb_pv_uuid"),
        migrations.RemoveIndex(model_name="safetycheckin", name="idxdb_sc_uuid"),
        migrations.RemoveIndex(model_name="trip", name="idxdb_trip_uuid"),
        migrations.RemoveIndex(model_name="wiki", name="idxdb_wiki_uuid"),
        migrations.AlterField(
            model_name="emergencycontactdefault",
            name="email",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True,
                fail_soft=True,
                null=True,
                validators=[django.core.validators.EmailValidator()],
            ),
        ),
        migrations.AlterField(
            model_name="googlecalendaraccount",
            name="google_email",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True,
                fail_soft=True,
                help_text="Email of the connected Google account, for display only.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="googlephotosaccount",
            name="google_email",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True,
                fail_soft=True,
                help_text="Email of the connected Google account, for display only.",
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="area",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True, fail_soft=True, null=True
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="bio",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True,
                fail_soft=True,
                max_length=50000,
                null=True,
                validators=[django.core.validators.MaxLengthValidator(50000)],
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="discord_username",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True, default="", fail_soft=True
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="matrix_handle",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True, default="", fail_soft=True
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="phone_number",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True, default="", fail_soft=True
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="signal_username",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True, default="", fail_soft=True
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="telegram_username",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True, default="", fail_soft=True
            ),
        ),
        migrations.AlterField(
            model_name="profile",
            name="whatsapp_number",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True, default="", fail_soft=True
            ),
        ),
        migrations.AlterField(
            model_name="profileemail",
            name="email",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                fail_soft=True, validators=[django.core.validators.EmailValidator()]
            ),
        ),
        migrations.AlterField(
            model_name="profilenote",
            name="content",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True, default="", fail_soft=True
            ),
        ),
        migrations.RunPython(
            code=_0039_encrypt_existing_contact_and_note_fields,
            reverse_code=_0039_decrypt_existing_contact_and_note_fields,
        ),
        migrations.AlterField(
            model_name="sitesettings",
            name="notify_gotify_token",
            field=urbanlens.dashboard.models.fields.EncryptedTextField(
                blank=True,
                default="",
                fail_soft=True,
                help_text="Gotify application token used to authenticate pushes to the server above. Defaults to the UL_GOTIFY_TOKEN environment variable.",
                verbose_name="Gotify app token",
            ),
        ),
        migrations.AddField(
            model_name="pinimportfailure",
            name="maps_url",
            field=models.URLField(blank=True, default="", max_length=2048),
        ),
        migrations.RunPython(
            code=_0042_merge_duplicate_labels, reverse_code=_0042_noop_reverse
        ),
        migrations.CreateModel(
            name="StripeProcessedRefund",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("stripe_refund_id", models.CharField(max_length=255, unique=True)),
                ("stripe_charge_id", models.CharField(blank=True, max_length=255)),
                (
                    "amount_cents",
                    models.IntegerField(
                        default=0,
                        help_text="The refunded amount applied against the banked balance, in cents.",
                    ),
                ),
            ],
            options={"ordering": ["-created"], "abstract": False},
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="login_ip_max_attempts",
            field=models.IntegerField(
                default=30,
                help_text="Maximum number of failed login attempts from a single IP address (across any accounts) before further attempts from that address are temporarily blocked. Set to 0 to disable the per-IP throttle.",
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(1000),
                ],
                verbose_name="Max failed login attempts per IP",
            ),
        ),
        migrations.AddConstraint(
            model_name="sitesettings",
            constraint=models.CheckConstraint(
                condition=models.Q(("login_ip_max_attempts__gte", 0)),
                name="login_ip_max_attempts_gte_0",
            ),
        ),
        migrations.RunPython(
            code=_0046_drop_duplicate_event_links,
            reverse_code=django.db.migrations.operations.special.RunPython.noop,
        ),
        migrations.RunPython(
            code=_0047_drop_duplicate_links,
            reverse_code=django.db.migrations.operations.special.RunPython.noop,
        ),
        migrations.AddField(
            model_name="profile",
            name="credential_prompt_snoozed_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="webauthncredential",
            name="is_login_factor",
            field=models.BooleanField(default=True),
        ),
        migrations.CreateModel(
            name="E2EEPasskeyWrap",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                ("prf_input", models.CharField(max_length=64)),
                ("wrapped_secret", models.TextField()),
                ("bundle_version", models.PositiveIntegerField()),
                (
                    "bundle",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="passkey_wraps",
                        to="dashboard.messagingkeybundle",
                    ),
                ),
                (
                    "credential",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="e2ee_wrap",
                        to="dashboard.webauthncredential",
                    ),
                ),
            ],
            options={"db_table": "dashboard_e2ee_passkey_wrap", "abstract": False},
        ),
        migrations.AddField(
            model_name="mapimageoverlay",
            name="tile_url_template",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.RemoveIndex(model_name="boundaryvote", name="idxdb_bv_place"),
        migrations.RemoveIndex(model_name="customlayer", name="idxdb_cl_pin"),
        migrations.RemoveIndex(model_name="customlayer", name="idxdb_cl_wiki"),
        migrations.RemoveIndex(model_name="customlayer", name="idxdb_cl_profile"),
        migrations.RemoveIndex(
            model_name="devicescanentry", name="idxdb_scanentry_device"
        ),
        migrations.RemoveIndex(
            model_name="directmessagelocationmention", name="idxdb_dmlocm_message"
        ),
        migrations.RemoveIndex(
            model_name="emergencycontactdefault", name="idxdb_ecd_owner"
        ),
        migrations.RemoveIndex(
            model_name="externalvisitparticipant", name="idxdb_evp_visit"
        ),
        migrations.RemoveIndex(model_name="label", name="idxdb_label_profile"),
        migrations.RemoveIndex(model_name="location", name="idxdb_loc_gplace"),
        migrations.RemoveIndex(model_name="location", name="idxdb_loc_place"),
        migrations.RemoveIndex(model_name="markupmap", name="idxdb_mm_profile"),
        migrations.RemoveIndex(model_name="markupmap", name="idxdb_mm_pin"),
        migrations.RemoveIndex(
            model_name="markupmapshare", name="idxdb_mms_to_profile"
        ),
        migrations.RemoveIndex(
            model_name="markupmapshare", name="idxdb_mms_markup_map"
        ),
        migrations.RemoveIndex(model_name="pin", name="idxdb_pin_profile"),
        migrations.RemoveIndex(model_name="pin", name="idxdb_pin_parent_pin"),
        migrations.RemoveIndex(model_name="pin", name="idxdb_pin_location"),
        migrations.RemoveIndex(model_name="pinalias", name="idxdb_palias_pin"),
        migrations.RemoveIndex(model_name="pinlink", name="idxdb_plink_pin"),
        migrations.RemoveIndex(model_name="pinlist", name="idxdb_pinlist_profile"),
        migrations.RemoveIndex(model_name="pinlistitem", name="idxdb_pli_list"),
        migrations.RemoveIndex(model_name="pinlistitem", name="idxdb_pli_pin"),
        migrations.RemoveIndex(model_name="pinmarkup", name="idxdb_pm_pin"),
        migrations.RemoveIndex(model_name="pinmarkup", name="idxdb_pm_profile"),
        migrations.RemoveIndex(model_name="pinmarkup", name="idxdb_pm_wiki"),
        migrations.RemoveIndex(model_name="pinmarkup", name="idxdb_pm_map"),
        migrations.RemoveIndex(model_name="pinmarkup", name="idxdb_pm_layer"),
        migrations.RemoveIndex(model_name="pinnote", name="idxdb_pn_pin"),
        migrations.RemoveIndex(model_name="pinowner", name="idxdb_pinowner_pin"),
        migrations.RemoveIndex(model_name="pinvisit", name="idxdb_pv_pin"),
        migrations.RemoveIndex(model_name="place", name="idxdb_place_domain_root"),
        migrations.RemoveIndex(model_name="place", name="idxdb_place_parent"),
        migrations.RemoveIndex(model_name="placeaccessgrant", name="idxdb_pag_profile"),
        migrations.RemoveIndex(model_name="reaction", name="idxdb_react_trcomment"),
        migrations.RemoveIndex(model_name="reaction", name="idxdb_react_dm"),
        migrations.RemoveIndex(model_name="reaction", name="idxdb_react_gmsg"),
        migrations.RemoveIndex(model_name="route", name="idxdb_route_profile"),
        migrations.RemoveIndex(
            model_name="safetycheckincontact", name="idxdb_scc_checkin"
        ),
        migrations.RemoveIndex(
            model_name="safetycheckinmessage", name="idxdb_scm_checkin"
        ),
        migrations.RemoveIndex(
            model_name="safetycontactoptout", name="idxdb_scoo_profile"
        ),
        migrations.RemoveIndex(
            model_name="safetycontactoptout", name="idxdb_scoo_owner"
        ),
        migrations.RemoveIndex(
            model_name="safetycontactoptout", name="idxdb_scoo_checkin"
        ),
        migrations.RemoveIndex(
            model_name="savedfilter", name="idxdb_savedfilter_profile"
        ),
        migrations.RemoveIndex(model_name="sociallink", name="idxdb_soc_link_pfile"),
        migrations.RemoveIndex(model_name="tripactivity", name="idxdb_ta_trip"),
        migrations.RemoveIndex(
            model_name="tripactivityrsvp", name="idxdb_taar_activity"
        ),
        migrations.RemoveIndex(
            model_name="tripactivityvote", name="idxdb_tav_activity"
        ),
        migrations.RemoveIndex(model_name="tripcomment", name="idxdb_tc_trip"),
        migrations.RemoveIndex(model_name="tripmembership", name="idxdb_tm_trip"),
        migrations.RemoveIndex(model_name="wiki", name="idxdb_wiki_parent_wiki"),
        migrations.RemoveIndex(model_name="wikialias", name="idxdb_walias_wiki"),
        migrations.RemoveIndex(model_name="wikiedit", name="idxdb_we_wiki"),
        migrations.RemoveIndex(model_name="wikilink", name="idxdb_wlink_wiki"),
        migrations.AlterField(
            model_name="emailsendlog",
            name="email_type",
            field=models.CharField(
                choices=[
                    ("join_invite", "Friend invitation"),
                    ("visit_invite", "Visit participant invitation"),
                    ("email_verification", "Secondary-email verification"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="pin",
            name="buildings_auto_nested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="profile",
            name="auto_create_building_pins",
            field=models.BooleanField(
                default=True,
                help_text="Automatically add child pins for the buildings on a property when they are confidently identified. Ambiguous buildings still wait for your approval.",
            ),
        ),
        migrations.CreateModel(
            name="Floorplan",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "uuid",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("description", models.TextField(blank=True, default="")),
                ("condition", models.CharField(blank=True, default="", max_length=255)),
                ("built_date", models.DateField(blank=True, null=True)),
                ("attributes", models.JSONField(blank=True, default=dict)),
                (
                    "building_ref",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "building_name",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                ("name", models.CharField(blank=True, default="", max_length=255)),
                ("valid_from", models.DateField(blank=True, null=True)),
                ("floor_count", models.IntegerField(blank=True, null=True)),
                (
                    "labels",
                    models.ManyToManyField(
                        blank=True,
                        related_name="floorplan_%(class)ss",
                        to="dashboard.label",
                    ),
                ),
                (
                    "pin",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="floorplans",
                        to="dashboard.pin",
                    ),
                ),
                (
                    "place",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="floorplans",
                        to="dashboard.place",
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="floorplans",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={"db_table": "dashboard_floorplans", "abstract": False},
        ),
        migrations.CreateModel(
            name="FloorplanReference",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "uuid",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("photo", "Photo"),
                            ("pdf", "PDF"),
                            ("video", "Video"),
                            ("document", "Document"),
                            ("model", "3D model / CAD file"),
                            ("other", "Other"),
                        ],
                        default="other",
                        max_length=16,
                    ),
                ),
                ("title", models.CharField(blank=True, default="", max_length=255)),
                ("url", models.URLField(blank=True, default="", max_length=1000)),
                ("description", models.TextField(blank=True, default="")),
                ("attributes", models.JSONField(blank=True, default=dict)),
                (
                    "floorplan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reference_pool",
                        to="dashboard.floorplan",
                    ),
                ),
                (
                    "image",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="floorplan_references",
                        to="dashboard.image",
                    ),
                ),
            ],
            options={"db_table": "dashboard_floorplan_references", "abstract": False},
        ),
        migrations.CreateModel(
            name="FloorplanFloor",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "uuid",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("description", models.TextField(blank=True, default="")),
                ("condition", models.CharField(blank=True, default="", max_length=255)),
                ("built_date", models.DateField(blank=True, null=True)),
                ("attributes", models.JSONField(blank=True, default=dict)),
                ("level", models.SmallIntegerField(default=0)),
                ("name", models.CharField(blank=True, default="", max_length=255)),
                (
                    "geometry",
                    django.contrib.gis.db.models.fields.GeometryField(
                        blank=True, null=True, srid=4326
                    ),
                ),
                ("elevation_meters", models.FloatField(blank=True, null=True)),
                ("height_meters", models.FloatField(blank=True, null=True)),
                (
                    "floorplan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="floors",
                        to="dashboard.floorplan",
                    ),
                ),
                (
                    "labels",
                    models.ManyToManyField(
                        blank=True,
                        related_name="floorplan_%(class)ss",
                        to="dashboard.label",
                    ),
                ),
                (
                    "references",
                    models.ManyToManyField(
                        blank=True,
                        related_name="%(class)ss",
                        to="dashboard.floorplanreference",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_floorplan_floors",
                "ordering": ("level",),
                "abstract": False,
            },
        ),
        migrations.AddField(
            model_name="floorplan",
            name="references",
            field=models.ManyToManyField(
                blank=True, related_name="%(class)ss", to="dashboard.floorplanreference"
            ),
        ),
        migrations.CreateModel(
            name="FloorplanSource",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "uuid",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("title", models.CharField(blank=True, default="", max_length=255)),
                ("url", models.URLField(blank=True, default="", max_length=1000)),
                ("note", models.TextField(blank=True, default="")),
                ("author", models.CharField(blank=True, default="", max_length=255)),
                ("attributes", models.JSONField(blank=True, default=dict)),
                (
                    "file",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="floorplan_sources",
                        to="dashboard.image",
                    ),
                ),
                (
                    "floorplan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="source_pool",
                        to="dashboard.floorplan",
                    ),
                ),
            ],
            options={"db_table": "dashboard_floorplan_sources", "abstract": False},
        ),
        migrations.CreateModel(
            name="FloorplanRoom",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "uuid",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("description", models.TextField(blank=True, default="")),
                ("condition", models.CharField(blank=True, default="", max_length=255)),
                ("built_date", models.DateField(blank=True, null=True)),
                ("attributes", models.JSONField(blank=True, default=dict)),
                ("name", models.CharField(blank=True, default="", max_length=255)),
                (
                    "geometry",
                    django.contrib.gis.db.models.fields.GeometryField(
                        blank=True, null=True, srid=4326
                    ),
                ),
                ("height_meters", models.FloatField(blank=True, null=True)),
                (
                    "floor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rooms",
                        to="dashboard.floorplanfloor",
                    ),
                ),
                (
                    "labels",
                    models.ManyToManyField(
                        blank=True,
                        related_name="floorplan_%(class)ss",
                        to="dashboard.label",
                    ),
                ),
                (
                    "references",
                    models.ManyToManyField(
                        blank=True,
                        related_name="%(class)ss",
                        to="dashboard.floorplanreference",
                    ),
                ),
                (
                    "source",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="dashboard.floorplansource",
                    ),
                ),
                (
                    "parent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="children",
                        to="dashboard.floorplanroom",
                    ),
                ),
                ("sort_order", models.PositiveIntegerField(default=0)),
            ],
            options={
                "db_table": "dashboard_floorplan_rooms",
                "abstract": False,
                "ordering": ("sort_order", "id"),
            },
        ),
        migrations.AddField(
            model_name="floorplanfloor",
            name="source",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="dashboard.floorplansource",
            ),
        ),
        migrations.AddField(
            model_name="floorplan",
            name="source",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="dashboard.floorplansource",
            ),
        ),
        migrations.AlterModelOptions(
            name="floorplan",
            options={
                "ordering": [
                    models.OrderBy(
                        models.F("valid_from"), descending=True, nulls_last=True
                    ),
                    "-created",
                ]
            },
        ),
        migrations.AlterModelOptions(
            name="floorplanfloor", options={"ordering": ("level", "sort_order", "id")}
        ),
        migrations.AddField(
            model_name="floorplan",
            name="sort_order",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="floorplan",
            name="wiki",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="floorplans",
                to="dashboard.wiki",
            ),
        ),
        migrations.CreateModel(
            name="FloorplanElement",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "uuid",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("description", models.TextField(blank=True, default="")),
                ("condition", models.CharField(blank=True, default="", max_length=255)),
                ("built_date", models.DateField(blank=True, null=True)),
                ("attributes", models.JSONField(blank=True, default=dict)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("wall", "Wall"),
                            ("floor", "Floor surface"),
                            ("ceiling", "Ceiling"),
                            ("roof", "Roof"),
                            ("column", "Column"),
                            ("window", "Window"),
                            ("door", "Door"),
                            ("stair", "Stair"),
                            ("fixture", "Fixture"),
                            ("furniture", "Furniture"),
                            ("key", "Key"),
                            ("other", "Other"),
                        ],
                        max_length=16,
                    ),
                ),
                ("name", models.CharField(blank=True, default="", max_length=255)),
                (
                    "geometry",
                    django.contrib.gis.db.models.fields.GeometryField(
                        blank=True, null=True, srid=4326
                    ),
                ),
                ("material", models.CharField(blank=True, default="", max_length=255)),
                ("base_elevation_meters", models.FloatField(blank=True, null=True)),
                ("height_meters", models.FloatField(blank=True, null=True)),
                (
                    "floorplan",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="elements",
                        to="dashboard.floorplan",
                    ),
                ),
                (
                    "labels",
                    models.ManyToManyField(
                        blank=True,
                        related_name="floorplan_%(class)ss",
                        to="dashboard.label",
                    ),
                ),
                (
                    "mounted_on",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="mounted_elements",
                        to="dashboard.floorplanelement",
                    ),
                ),
                (
                    "floor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="elements",
                        to="dashboard.floorplanfloor",
                    ),
                ),
                (
                    "references",
                    models.ManyToManyField(
                        blank=True,
                        related_name="%(class)ss",
                        to="dashboard.floorplanreference",
                    ),
                ),
                (
                    "room",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="elements",
                        to="dashboard.floorplanroom",
                    ),
                ),
                (
                    "source",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="dashboard.floorplansource",
                    ),
                ),
                (
                    "connects_rooms",
                    models.ManyToManyField(
                        blank=True,
                        related_name="connecting_elements",
                        to="dashboard.floorplanroom",
                    ),
                ),
                ("rotation_degrees", models.FloatField(blank=True, null=True)),
                ("sort_order", models.PositiveIntegerField(default=0)),
                (
                    "spans_floors",
                    models.ManyToManyField(
                        blank=True,
                        related_name="spanning_elements",
                        to="dashboard.floorplanfloor",
                    ),
                ),
                ("thickness_meters", models.FloatField(blank=True, null=True)),
            ],
            options={
                "db_table": "dashboard_floorplan_elements",
                "abstract": False,
                "ordering": ("sort_order", "id"),
            },
        ),
        migrations.AddField(
            model_name="floorplanfloor",
            name="sort_order",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.CreateModel(
            name="FloorplanLock",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "uuid",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("description", models.TextField(blank=True, default="")),
                ("condition", models.CharField(blank=True, default="", max_length=255)),
                ("built_date", models.DateField(blank=True, null=True)),
                ("attributes", models.JSONField(blank=True, default=dict)),
                ("name", models.CharField(blank=True, default="", max_length=255)),
                ("key_attributes", models.JSONField(blank=True, default=dict)),
                (
                    "element",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="locks",
                        to="dashboard.floorplanelement",
                    ),
                ),
                (
                    "labels",
                    models.ManyToManyField(
                        blank=True,
                        related_name="floorplan_%(class)ss",
                        to="dashboard.label",
                    ),
                ),
                (
                    "references",
                    models.ManyToManyField(
                        blank=True,
                        related_name="%(class)ss",
                        to="dashboard.floorplanreference",
                    ),
                ),
                (
                    "source",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="dashboard.floorplansource",
                    ),
                ),
                ("sort_order", models.PositiveIntegerField(default=0)),
            ],
            options={
                "db_table": "dashboard_floorplan_locks",
                "abstract": False,
                "ordering": ("sort_order", "id"),
            },
        ),
        migrations.AddField(
            model_name="floorplanroom",
            name="spans_floors",
            field=models.ManyToManyField(
                blank=True, related_name="spanning_rooms", to="dashboard.floorplanfloor"
            ),
        ),
        migrations.RunPython(code=_0055_protect, reverse_code=_0055_unprotect),
        migrations.AddField(
            model_name="profile",
            name="disable_auto_tagging",
            field=models.BooleanField(
                default=False,
                help_text="Turn off automatic tagging of your pins. Individual labels can also be excluded on the Organize page.",
            ),
        ),
        migrations.RunPython(
            code=_0056_clear_empty_image_caches,
            reverse_code=_0056_keep_cleared_caches_cleared,
        ),
        migrations.AddField(
            model_name="friendship",
            name="muted_by_from_profile",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="friendship",
            name="muted_by_to_profile",
            field=models.BooleanField(default=False),
        ),
        migrations.RunSQL(
            sql="UPDATE dashboard_friendships SET muted_by_from_profile = TRUE, muted_by_to_profile = TRUE WHERE muted",
            reverse_sql="UPDATE dashboard_friendships SET muted = (muted_by_from_profile OR muted_by_to_profile)",
        ),
        migrations.RemoveField(model_name="friendship", name="muted"),
        migrations.CreateModel(
            name="FloorplanMarker",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "uuid",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("description", models.TextField(blank=True, default="")),
                ("condition", models.CharField(blank=True, default="", max_length=255)),
                ("built_date", models.DateField(blank=True, null=True)),
                ("attributes", models.JSONField(blank=True, default=dict)),
                ("x", models.FloatField()),
                ("y", models.FloatField()),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("photo", "Photo location"),
                            ("hazard", "Hazard"),
                            ("entrance", "Entrance"),
                            ("stair", "Stair"),
                            ("elevator", "Elevator / shaft"),
                            ("note", "Note"),
                            ("fixture", "Fixture"),
                        ],
                        default="note",
                        max_length=16,
                    ),
                ),
                ("name", models.CharField(blank=True, default="", max_length=255)),
                ("facing_degrees", models.FloatField(blank=True, null=True)),
                (
                    "connector_id",
                    models.CharField(blank=True, default="", max_length=64),
                ),
            ],
            options={
                "db_table": "dashboard_floorplan_markers",
                "ordering": ("sort_order", "id"),
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="FloorplanOpening",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "uuid",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("description", models.TextField(blank=True, default="")),
                ("condition", models.CharField(blank=True, default="", max_length=255)),
                ("built_date", models.DateField(blank=True, null=True)),
                ("attributes", models.JSONField(blank=True, default=dict)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("door", "Door"),
                            ("doorway", "Doorway (no door)"),
                            ("window", "Window"),
                            ("hatch", "Hatch"),
                        ],
                        default="door",
                        max_length=16,
                    ),
                ),
                ("t_start", models.FloatField()),
                ("t_end", models.FloatField()),
                (
                    "swing",
                    models.CharField(
                        choices=[
                            ("none", "Not known"),
                            ("left", "Left"),
                            ("right", "Right"),
                            ("double", "Double"),
                        ],
                        default="none",
                        max_length=8,
                    ),
                ),
                ("sill_meters", models.FloatField(blank=True, null=True)),
            ],
            options={
                "db_table": "dashboard_floorplan_openings",
                "ordering": ("sort_order", "id"),
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="FloorplanRoomSeed",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "uuid",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("description", models.TextField(blank=True, default="")),
                ("condition", models.CharField(blank=True, default="", max_length=255)),
                ("built_date", models.DateField(blank=True, null=True)),
                ("attributes", models.JSONField(blank=True, default=dict)),
                ("x", models.FloatField()),
                ("y", models.FloatField()),
                ("name", models.CharField(blank=True, default="", max_length=255)),
                ("height_meters", models.FloatField(blank=True, null=True)),
            ],
            options={
                "db_table": "dashboard_floorplan_room_seeds",
                "ordering": ("sort_order", "id"),
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="FloorplanWall",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "uuid",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("description", models.TextField(blank=True, default="")),
                ("condition", models.CharField(blank=True, default="", max_length=255)),
                ("built_date", models.DateField(blank=True, null=True)),
                ("attributes", models.JSONField(blank=True, default=dict)),
                ("ax", models.FloatField()),
                ("ay", models.FloatField()),
                ("bx", models.FloatField()),
                ("by", models.FloatField()),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("exterior", "Exterior wall"),
                            ("interior", "Interior wall"),
                            ("virtual", "Virtual (open edge)"),
                            ("collapsed", "Collapsed / ruined"),
                        ],
                        default="interior",
                        max_length=16,
                    ),
                ),
                (
                    "thickness",
                    models.CharField(
                        choices=[
                            ("thin", "Thin"),
                            ("normal", "Normal"),
                            ("thick", "Thick"),
                        ],
                        default="normal",
                        max_length=8,
                    ),
                ),
                ("name", models.CharField(blank=True, default="", max_length=255)),
            ],
            options={
                "db_table": "dashboard_floorplan_walls",
                "ordering": ("sort_order", "id"),
                "abstract": False,
            },
        ),
        migrations.RemoveField(model_name="floorplanroom", name="floor"),
        migrations.RemoveField(model_name="floorplanroom", name="labels"),
        migrations.RemoveField(model_name="floorplanroom", name="parent"),
        migrations.RemoveField(model_name="floorplanroom", name="references"),
        migrations.RemoveField(model_name="floorplanroom", name="source"),
        migrations.RemoveField(model_name="floorplanroom", name="spans_floors"),
        migrations.RemoveField(model_name="floorplanfloor", name="geometry"),
        migrations.AddField(
            model_name="floorplan",
            name="origin_lat",
            field=models.DecimalField(
                blank=True, decimal_places=6, max_digits=9, null=True
            ),
        ),
        migrations.AddField(
            model_name="floorplan",
            name="origin_lng",
            field=models.DecimalField(
                blank=True, decimal_places=6, max_digits=9, null=True
            ),
        ),
        migrations.AddField(
            model_name="floorplan",
            name="rotation_degrees",
            field=models.FloatField(default=0.0),
        ),
        migrations.AlterField(
            model_name="floorplan",
            name="place",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="floorplans",
                to="dashboard.place",
            ),
        ),
        migrations.AddField(
            model_name="floorplanmarker",
            name="floor",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="markers",
                to="dashboard.floorplanfloor",
            ),
        ),
        migrations.AddField(
            model_name="floorplanmarker",
            name="labels",
            field=models.ManyToManyField(
                blank=True, related_name="floorplan_%(class)ss", to="dashboard.label"
            ),
        ),
        migrations.AddField(
            model_name="floorplanmarker",
            name="references",
            field=models.ManyToManyField(
                blank=True, related_name="%(class)ss", to="dashboard.floorplanreference"
            ),
        ),
        migrations.AddField(
            model_name="floorplanmarker",
            name="source",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="dashboard.floorplansource",
            ),
        ),
        migrations.AddField(
            model_name="floorplanopening",
            name="labels",
            field=models.ManyToManyField(
                blank=True, related_name="floorplan_%(class)ss", to="dashboard.label"
            ),
        ),
        migrations.AddField(
            model_name="floorplanopening",
            name="references",
            field=models.ManyToManyField(
                blank=True, related_name="%(class)ss", to="dashboard.floorplanreference"
            ),
        ),
        migrations.AddField(
            model_name="floorplanopening",
            name="source",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="dashboard.floorplansource",
            ),
        ),
        migrations.AddField(
            model_name="floorplanroomseed",
            name="floor",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="rooms",
                to="dashboard.floorplanfloor",
            ),
        ),
        migrations.AddField(
            model_name="floorplanroomseed",
            name="labels",
            field=models.ManyToManyField(
                blank=True, related_name="floorplan_%(class)ss", to="dashboard.label"
            ),
        ),
        migrations.AddField(
            model_name="floorplanroomseed",
            name="references",
            field=models.ManyToManyField(
                blank=True, related_name="%(class)ss", to="dashboard.floorplanreference"
            ),
        ),
        migrations.AddField(
            model_name="floorplanroomseed",
            name="source",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="dashboard.floorplansource",
            ),
        ),
        migrations.AddField(
            model_name="floorplanwall",
            name="floor",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="walls",
                to="dashboard.floorplanfloor",
            ),
        ),
        migrations.AddField(
            model_name="floorplanwall",
            name="labels",
            field=models.ManyToManyField(
                blank=True, related_name="floorplan_%(class)ss", to="dashboard.label"
            ),
        ),
        migrations.AddField(
            model_name="floorplanwall",
            name="references",
            field=models.ManyToManyField(
                blank=True, related_name="%(class)ss", to="dashboard.floorplanreference"
            ),
        ),
        migrations.AddField(
            model_name="floorplanwall",
            name="source",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="dashboard.floorplansource",
            ),
        ),
        migrations.AddField(
            model_name="floorplanopening",
            name="wall",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="openings",
                to="dashboard.floorplanwall",
            ),
        ),
        migrations.RemoveField(model_name="floorplanelement", name="connects_rooms"),
        migrations.RemoveField(model_name="floorplanelement", name="floor"),
        migrations.RemoveField(model_name="floorplanelement", name="floorplan"),
        migrations.RemoveField(model_name="floorplanelement", name="labels"),
        migrations.RemoveField(model_name="floorplanelement", name="mounted_on"),
        migrations.RemoveField(model_name="floorplanelement", name="references"),
        migrations.RemoveField(model_name="floorplanelement", name="room"),
        migrations.RemoveField(model_name="floorplanelement", name="source"),
        migrations.RemoveField(model_name="floorplanelement", name="spans_floors"),
        migrations.DeleteModel(name="FloorplanLock"),
        migrations.DeleteModel(name="FloorplanRoom"),
        migrations.DeleteModel(name="FloorplanElement"),
        migrations.AddConstraint(
            model_name="floorplanopening",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("t_start__gte", 0),
                    ("t_end__lte", 1),
                    ("t_start__lt", models.F("t_end")),
                ),
                name="floorplan_opening_within_wall",
            ),
        ),
        migrations.CreateModel(
            name="FloorplanLock",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "uuid",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("description", models.TextField(blank=True, default="")),
                ("condition", models.CharField(blank=True, default="", max_length=255)),
                ("built_date", models.DateField(blank=True, null=True)),
                ("attributes", models.JSONField(blank=True, default=dict)),
                ("name", models.CharField(blank=True, default="", max_length=255)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("unknown", "Not known"),
                            ("locked", "Locked"),
                            ("unlocked", "Unlocked"),
                        ],
                        default="unknown",
                        max_length=16,
                    ),
                ),
                ("key_attributes", models.JSONField(blank=True, default=dict)),
                (
                    "labels",
                    models.ManyToManyField(
                        blank=True,
                        related_name="floorplan_%(class)ss",
                        to="dashboard.label",
                    ),
                ),
                (
                    "opening",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="locks",
                        to="dashboard.floorplanopening",
                    ),
                ),
                (
                    "references",
                    models.ManyToManyField(
                        blank=True,
                        related_name="%(class)ss",
                        to="dashboard.floorplanreference",
                    ),
                ),
                (
                    "source",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="dashboard.floorplansource",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_floorplan_locks",
                "ordering": ("sort_order", "id"),
                "abstract": False,
            },
        ),
        migrations.AlterField(
            model_name="floorplanmarker",
            name="kind",
            field=models.CharField(
                choices=[
                    ("hazard", "Hazard"),
                    ("stair", "Stair"),
                    ("elevator", "Elevator / shaft"),
                ],
                default="hazard",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="floorplanmarker",
            name="linked_pin",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="floorplan_marker",
                to="dashboard.pin",
            ),
        ),
        migrations.AlterField(
            model_name="pin",
            name="pin_type",
            field=models.CharField(
                choices=[
                    ("location", "Location"),
                    ("parcel", "Property / Parcel"),
                    ("building", "Building"),
                    ("entrance", "Entrance"),
                    ("poi", "Point of Interest"),
                    ("danger", "Danger"),
                    ("stair", "Stair"),
                    ("elevator", "Elevator / shaft"),
                    ("other", "Other"),
                ],
                default="location",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="pinsuggestion",
            name="suggested_pin_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("location", "Location"),
                    ("parcel", "Property / Parcel"),
                    ("building", "Building"),
                    ("entrance", "Entrance"),
                    ("poi", "Point of Interest"),
                    ("danger", "Danger"),
                    ("stair", "Stair"),
                    ("elevator", "Elevator / shaft"),
                    ("other", "Other"),
                ],
                default="",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="wiki",
            name="pin_type",
            field=models.CharField(
                choices=[
                    ("location", "Location"),
                    ("parcel", "Property / Parcel"),
                    ("building", "Building"),
                    ("entrance", "Entrance"),
                    ("poi", "Point of Interest"),
                    ("danger", "Danger"),
                    ("stair", "Stair"),
                    ("elevator", "Elevator / shaft"),
                    ("other", "Other"),
                ],
                default="location",
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="floorplanfloor",
            name="designation",
            field=models.CharField(blank=True, default="", max_length=8),
        ),
        migrations.RunPython(
            code=_0062_clear_generated_names,
            reverse_code=django.db.migrations.operations.special.RunPython.noop,
        ),
        migrations.RunPython(
            code=_0062_renumber_levels,
            reverse_code=django.db.migrations.operations.special.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="floorplanwall",
            name="kind",
            field=models.CharField(
                choices=[
                    ("exterior", "Exterior wall"),
                    ("interior", "Interior wall"),
                    ("fence", "Fence"),
                    ("virtual", "Virtual (open edge)"),
                    ("collapsed", "Collapsed / ruined"),
                ],
                default="interior",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="floorplanopening",
            name="kind",
            field=models.CharField(
                choices=[
                    ("door", "Door"),
                    ("doorway", "Doorway (no door)"),
                    ("gate", "Gate"),
                    ("window", "Window"),
                    ("hatch", "Hatch"),
                ],
                default="door",
                max_length=16,
            ),
        ),
        migrations.AlterModelOptions(
            name="floorplanreference", options={"ordering": ("sort_order", "id")}
        ),
        migrations.AlterModelOptions(
            name="floorplansource", options={"ordering": ("sort_order", "id")}
        ),
        migrations.AddField(
            model_name="floorplanreference",
            name="sort_order",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="floorplansource",
            name="sort_order",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="image",
            name="source_media_url",
            field=models.URLField(blank=True, max_length=500, null=True),
        ),
        migrations.AlterField(
            model_name="floorplanreference",
            name="image",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="floorplan_references",
                to="dashboard.image",
            ),
        ),
        migrations.AlterField(
            model_name="image",
            name="source",
            field=models.CharField(
                choices=[
                    ("upload", "Upload"),
                    ("linked_url", "Linked URL"),
                    ("yelp", "Yelp"),
                    ("google_images", "Google Images"),
                    ("google_maps", "Google Maps"),
                    ("wikimedia", "Wikimedia Commons"),
                    ("wikipedia_media", "Wikipedia"),
                    ("smithsonian", "Smithsonian Open Access"),
                    ("library_of_congress", "Library of Congress"),
                    ("internet_archive", "Internet Archive"),
                    ("digital_commonwealth", "Digital Commonwealth"),
                    ("immich", "Immich"),
                    ("flickr", "Flickr"),
                    ("google_photos", "Google Photos"),
                    ("loopnet", "LoopNet"),
                    ("cris", "NY Historic Preservation (CRIS)"),
                    ("external_api", "External app"),
                    ("google_street_view", "Google Street View"),
                    ("google_satellite", "Google Satellite"),
                ],
                default="upload",
                max_length=30,
            ),
        ),
        migrations.CreateModel(
            name="ImageAttachment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "added_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="image_attachments_added",
                        to="dashboard.profile",
                    ),
                ),
                (
                    "image",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attachments",
                        to="dashboard.image",
                    ),
                ),
                (
                    "pin",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="image_attachments",
                        to="dashboard.pin",
                    ),
                ),
                (
                    "wiki",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="image_attachments",
                        to="dashboard.wiki",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_image_attachments",
                "abstract": False,
                "indexes": [
                    models.Index(fields=["pin"], name="idxdb_imgatt_pin"),
                    models.Index(fields=["wiki"], name="idxdb_imgatt_wiki"),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(("pin__isnull", False), ("wiki__isnull", True)),
                            models.Q(("pin__isnull", True), ("wiki__isnull", False)),
                            _connector="OR",
                        ),
                        name="ck_image_attachment_one_owner",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("pin__isnull", False)),
                        fields=("image", "pin"),
                        name="uq_image_attachment_pin",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("wiki__isnull", False)),
                        fields=("image", "wiki"),
                        name="uq_image_attachment_wiki",
                    ),
                ],
            },
        ),
        migrations.AlterField(
            model_name="image",
            name="exif_data",
            field=urbanlens.dashboard.models.fields.EncryptedJSONField(
                blank=True, fail_soft=True, null=True
            ),
        ),
        migrations.RunPython(
            code=_0066_encrypt_existing_exif_data,
            reverse_code=_0066_decrypt_existing_exif_data,
        ),
        migrations.CreateModel(
            name="ProfileReputation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "total",
                    models.DecimalField(
                        decimal_places=4, default=Decimal(0), max_digits=14
                    ),
                ),
                (
                    "lifetime_earned",
                    models.DecimalField(
                        decimal_places=4, default=Decimal(0), max_digits=14
                    ),
                ),
                ("is_stale", models.BooleanField(db_index=True, default=False)),
                ("computed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "profile",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reputation",
                        to="dashboard.profile",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_profile_reputation",
                "ordering": ["-total"],
                "abstract": False,
            },
        ),
        migrations.CreateModel(
            name="ReputationEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "rule_key",
                    models.CharField(
                        choices=urbanlens.dashboard.models.reputation.model.rule_choices,
                        db_index=True,
                        max_length=64,
                    ),
                ),
                (
                    "target_kind",
                    models.CharField(
                        choices=[
                            ("none", "No target"),
                            ("wiki_edit", "Wiki edit"),
                            ("image", "Photo"),
                            ("comment", "Comment"),
                            ("pin", "Pin"),
                            ("wiki", "Wiki"),
                            ("article_revision", "Article revision"),
                            ("friend_invitation", "Invitation"),
                            ("profile", "Profile"),
                        ],
                        default="none",
                        max_length=32,
                    ),
                ),
                ("target_id", models.IntegerField(blank=True, null=True)),
                (
                    "value",
                    models.DecimalField(
                        blank=True, decimal_places=4, max_digits=12, null=True
                    ),
                ),
                ("inputs", models.JSONField(blank=True, default=dict)),
                (
                    "occurred_at",
                    models.DateTimeField(
                        db_index=True, default=django.utils.timezone.now
                    ),
                ),
                ("period_key", models.CharField(db_index=True, max_length=7)),
                ("retracted", models.BooleanField(default=False)),
                (
                    "retracted_reason",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reputation_events",
                        to="dashboard.profile",
                    ),
                ),
                (
                    "wiki",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="reputation_events",
                        to="dashboard.wiki",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_reputation_events",
                "ordering": ["-occurred_at"],
                "get_latest_by": "occurred_at",
                "abstract": False,
                "indexes": [
                    models.Index(
                        fields=["profile", "-occurred_at"],
                        name="idxdb_repev_profile_date",
                    ),
                    models.Index(
                        fields=["profile", "rule_key", "period_key"],
                        name="idxdb_repev_pf_rule_per",
                    ),
                    models.Index(
                        fields=["profile", "wiki"], name="idxdb_repev_profile_wiki"
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("rule_key", "target_kind", "target_id"),
                        name="uniq_reputation_event_target",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="WikiFieldRevision",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("field_name", models.CharField(db_index=True, max_length=64)),
                ("value", models.TextField(blank=True, default="")),
                ("is_null", models.BooleanField(default=False)),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("user", "User"),
                            ("automatic", "Automatic"),
                            ("system", "System"),
                        ],
                        db_index=True,
                        max_length=16,
                    ),
                ),
                ("recorded_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="dashboard.profile",
                    ),
                ),
                (
                    "target",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="field_revisions",
                        to="dashboard.wiki",
                    ),
                ),
            ],
            options={
                "db_table": "dashboard_wiki_field_revisions",
                "ordering": ["target", "-id"],
                "abstract": False,
                "indexes": [
                    models.Index(
                        fields=["target", "field_name", "-id"],
                        name="idxdb_wikirev_tgt_fld_id",
                    ),
                    models.Index(
                        fields=["target", "source"], name="idxdb_wikirev_tgt_source"
                    ),
                    models.Index(
                        fields=["target", "actor"], name="idxdb_wikirev_tgt_actor"
                    ),
                ],
            },
        ),
        migrations.RemoveField(model_name="wiki", name="officially_created"),
        migrations.RemoveField(model_name="wiki", name="viewed_by_other"),
        migrations.AddField(
            model_name="safetycheckin",
            name="archive_failed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="safetycheckin",
            name="archive_failure_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="notify_safety_checkin_archival_failed_email",
            field=models.BooleanField(
                default=True,
                help_text="Email the admin notification address when a safety check-in gives up on archival after repeated failures.",
                verbose_name="Safety check-in archival failures (email)",
            ),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="notify_safety_checkin_archival_failed_gotify",
            field=models.BooleanField(
                default=False,
                help_text="Send a Gotify push notification when a safety check-in gives up on archival after repeated failures.",
                verbose_name="Safety check-in archival failures (Gotify)",
            ),
        ),
        migrations.AddConstraint(
            model_name="safetycontactoptout",
            constraint=models.UniqueConstraint(
                models.F("contact_profile"),
                models.F("email"),
                models.F("scope"),
                models.F("owner"),
                models.F("checkin"),
                name="uq_safety_contact_optout_target_scope",
                nulls_distinct=False,
            ),
        ),
        migrations.AlterField(
            model_name="image",
            name="quota_exempt_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("external_media", "Cached external media"),
                    ("community", "Community-valued contribution"),
                    ("shared_copy", "Copy of a shared photo"),
                ],
                default="",
                max_length=20,
            ),
        ),
    ]
