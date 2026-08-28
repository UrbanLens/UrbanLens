"""Image model for pin and wiki photo uploads."""

from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING
from uuid import uuid4

from django.db.models import CASCADE, SET_NULL, BigIntegerField, BooleanField, CharField, DateTimeField, DecimalField, FloatField, ForeignKey, ImageField, Index, JSONField, ManyToManyField, PositiveIntegerField, TextField, URLField, UUIDField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.abstract.choices import TextChoices
from urbanlens.dashboard.models.fields import EncryptedJSONField
from urbanlens.dashboard.models.images.queryset import ImageManager

if TYPE_CHECKING:
    from decimal import Decimal

# Room for "pin_images/" (11 chars) plus the ~8-character suffix
# Storage.get_available_name appends on a filename collision, comfortably
# inside the field's max_length below. Uploaded filenames are arbitrary and
# unbounded - a browser drag-drop, a device-scan import, or a Media-gallery
# URL can each hand over a name well past 100 characters, which overflowed
# the field's old default max_length (100) outright and raised
# SuspiciousFileOperation instead of storing the file.
_UPLOAD_STEM_LIMIT = 80
_UPLOAD_EXT_LIMIT = 12


def pin_image_upload_path(instance: Image, filename: str) -> str:
    """Storage path for an uploaded Image file, trimming an overlong name to fit.

    Not underscore-prefixed despite being an internal helper - Django
    migrations serialize a callable ``upload_to`` by importable reference, so
    this needs to read as public to both ruff and any future migration.

    Only the stem is trimmed, not the extension, and the trim always comes
    from the end - the filename's *prefix* must survive into storage for
    ``services.media.images.is_camera_generated_filename`` to keep recognizing
    camera-named uploads (e.g. ``PXL_20260709_123456.jpg``) for its
    author-attribution heuristic.
    """
    stem, ext = posixpath.splitext(filename)
    return f"pin_images/{stem[:_UPLOAD_STEM_LIMIT]}{ext[:_UPLOAD_EXT_LIMIT]}"


def pin_image_thumbnail_path(instance: Image, filename: str) -> str:
    """Storage path for an Image's small grid thumbnail.

    Same stem-trimming rules as :func:`pin_image_upload_path`, under a
    ``thumbs/`` prefix so a thumbnail can never collide with the original file
    when both are derived from the same upload name.
    """
    stem, ext = posixpath.splitext(filename)
    return f"pin_images/thumbs/{stem[:_UPLOAD_STEM_LIMIT]}{ext[:_UPLOAD_EXT_LIMIT]}"


class ImageSource(TextChoices):
    """Where a photo originated - drives the Media section's per-source tabs.

    ``UPLOAD`` is the default for ordinary user uploads (personal galleries).
    Most external values are set only on rows materialized from the Media
    gallery's transient provider results (see ``services.pins.external_data`` and
    ``services.media.media_materialize``) when a user sends one to a wiki or sets it
    as a cover photo - the Media gallery itself renders straight from each
    provider's live results without persisting an ``Image`` row per item.
    ``EXTERNAL_API`` is the exception: it's set on candidate photos an
    external-app pin suggestion submits (see ``services.pins.pin_suggestions.attach_suggestion_photos``),
    staged against a ``PinSuggestion`` rather than materialized from the Media gallery.
    """

    UPLOAD = "upload", "Upload"
    #: A URL somebody pasted, whose bytes were then fetched and stored like any
    #: upload. The address itself is kept in ``source_url``.
    LINKED_URL = "linked_url", "Linked URL"
    YELP = "yelp", "Yelp"
    GOOGLE_IMAGES = "google_images", "Google Images"
    GOOGLE_MAPS = "google_maps", "Google Maps"
    WIKIMEDIA = "wikimedia", "Wikimedia Commons"
    WIKIPEDIA_MEDIA = "wikipedia_media", "Wikipedia"
    SMITHSONIAN = "smithsonian", "Smithsonian Open Access"
    LIBRARY_OF_CONGRESS = "library_of_congress", "Library of Congress"
    INTERNET_ARCHIVE = "internet_archive", "Internet Archive"
    DIGITAL_COMMONWEALTH = "digital_commonwealth", "Digital Commonwealth"
    IMMICH = "immich", "Immich"
    FLICKR = "flickr", "Flickr"
    GOOGLE_PHOTOS = "google_photos", "Google Photos"
    LOOPNET = "loopnet", "LoopNet"
    CRIS = "cris", "NY Historic Preservation (CRIS)"
    EXTERNAL_API = "external_api", "External app"
    GOOGLE_STREET_VIEW = "google_street_view", "Google Street View"
    GOOGLE_SATELLITE = "google_satellite", "Google Satellite"


class MediaKind(TextChoices):
    """What kind of file this Image row actually holds.

    Photos, videos, and documents all share every other field on this model
    (caption, author, location, labels, etc.) - this is only a discriminator
    for upload-time processing (services.media.videos/services.media.documents) and
    display (player vs. viewer vs. image tag).
    """

    PHOTO = "photo", "Photo"
    VIDEO = "video", "Video"
    DOCUMENT = "document", "Document"


class QuotaExemption(TextChoices):
    """Why a stored file's bytes don't count against its uploader's quota.

    The empty default means "counts normally".

    ``EXTERNAL_MEDIA`` is a locally cached copy of someone else's photo, kept
    so the gallery doesn't depend on a provider's URL staying alive. The
    person who happened to upvote it didn't author it and shouldn't pay for
    caching it - storage the whole community benefits from.

    ``COMMUNITY_CONTRIBUTION`` is a user's own photo, shared to a wiki, that
    enough other people marked relevant - the quota bonus is the reward for
    contributing it. See ``services.media.quota_rewards``.

    ``SHARED_COPY`` is a recipient's copy of a photo accepted from a pin
    share: it points at the same stored file as the sender's row
    (``services.sharing.pin_sharing.create_pin_from_share``) rather than a
    second copy of the bytes, so it occupies no storage of the recipient's
    own to charge them for.
    """

    EXTERNAL_MEDIA = "external_media", "Cached external media"
    COMMUNITY_CONTRIBUTION = "community", "Community-valued contribution"
    SHARED_COPY = "shared_copy", "Copy of a shared photo"


class Image(abstract.FrontendDashboardModel):
    """A photo, video, or document uploaded by a user, attached to a pin, community wiki, or safety check-in."""

    image = ImageField(upload_to=pin_image_upload_path, max_length=255)
    # Small WebP preview generated after upload for photo grids (albums, galleries).
    # Written by process_image_upload; older rows are backfilled by a beat task.
    # Empty until then; :attr:`thumb_url` falls back to the original.
    thumbnail = ImageField(upload_to=pin_image_thumbnail_path, max_length=255, null=True, blank=True)
    media_type = CharField(max_length=10, choices=MediaKind.choices, default=MediaKind.PHOTO, db_index=True)
    # Provenance for the Media gallery's per-source tabs (see ImageSource). Only
    # meaningful once a row exists; almost every Image row is a plain upload.
    source = CharField(max_length=30, choices=ImageSource.choices, default=ImageSource.UPLOAD)
    pin = ForeignKey(
        "dashboard.Pin",
        on_delete=SET_NULL,
        related_name="images",
        null=True,
        blank=True,
    )
    wiki = ForeignKey(
        "dashboard.Wiki",
        on_delete=SET_NULL,
        related_name="images",
        null=True,
        blank=True,
    )
    # The shared Location this photo belongs to - the canonical "which place is
    # this a photo of" link, set from the pin/wiki it was uploaded to or resolved
    # from its GPS via Location.objects.get_nearby_or_create. Distinct from
    # `latitude`/`longitude` below: Location coordinates are immutable and shared
    # (snapped within ~50m), so this FK cannot carry per-photo GPS precision.
    location = ForeignKey(
        "dashboard.Location",
        on_delete=SET_NULL,
        related_name="images",
        null=True,
        blank=True,
    )
    safety_checkin = ForeignKey(
        "dashboard.SafetyCheckin",
        on_delete=SET_NULL,
        related_name="images",
        null=True,
        blank=True,
    )
    # The specific visit this photo documents, if the user attached it to one.
    # SET_NULL (not CASCADE) so deleting a visit record leaves the photo in the
    # pin/wiki gallery - it just loses its visit association.
    visit = ForeignKey(
        "dashboard.PinVisit",
        on_delete=SET_NULL,
        related_name="images",
        null=True,
        blank=True,
    )
    # The direct message this photo was attached to, if sent as a DM attachment.
    direct_message = ForeignKey(
        "dashboard.DirectMessage",
        on_delete=SET_NULL,
        related_name="images",
        null=True,
        blank=True,
    )
    # Set only while this is a candidate photo the user opted to upload during a
    # local-folder location scan, staged for possible import into a pin's gallery
    # if the pending PinSuggestion is accepted. Cleared (set back to null) once the
    # photo graduates to a real gallery photo on accept; the row itself is deleted
    # (not just unlinked) if the suggestion is rejected or the photo wasn't selected.
    pin_suggestion = ForeignKey(
        "dashboard.PinSuggestion",
        on_delete=SET_NULL,
        related_name="candidate_images",
        null=True,
        blank=True,
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="uploaded_images",
        null=True,
        blank=True,
    )
    caption = CharField(max_length=500, null=True, blank=True)
    # Attribution fields, shown in the lightbox. Auto-populated from EXIF/PNG
    # metadata by process_image_upload when present; when a photo has none of
    # author/source_url/caption/copyright AND its filename matches a common
    # phone/camera auto-naming convention (e.g. PXL_20260709_123456.jpg), the
    # uploader is assumed to be the author. Any other unattributed photo is
    # left blank rather than guessed at.
    author = CharField(max_length=255, null=True, blank=True)
    source_url = URLField(max_length=500, null=True, blank=True)
    #: The address the bytes themselves came from, when that differs from the page
    #: above. Both are kept because they rot independently: a provider's landing
    #: page can be reorganised - or be a feed that simply moves on, as a "recent
    #: photos" page does - while the file stays exactly where it was, and the file
    #: can be replaced while the page still describes the picture. Storing one
    #: threw the other away; materialize_media_item was handed both and persisted
    #: only the page.
    source_media_url = URLField(max_length=500, null=True, blank=True)
    copyright = CharField(max_length=255, null=True, blank=True)
    # Set only on rows materialized from the Media gallery's transient provider
    # results (see services.media.media_materialize) - the *raw* provider panel key
    # (e.g. "wikimedia", "loc") and the sha1 hash of the item's full-resolution
    # url, i.e. exactly the (source, item_key) identity MediaRelevance marks
    # are keyed by (models.images.relevance.media_item_key). `source_url`
    # can't stand in for this: it's set to `page_url or url`, which diverges
    # from the raw `url` that item_key is always hashed from whenever a
    # provider supplies a page_url - these two fields are what let a
    # materialized Image row be reliably joined back to its wiki votes (see
    # services.media.media_relevance.effective_relevance). Deliberately NOT the
    # same value as `source` above, which stores the *translated*
    # ImageSource value and can differ from the raw panel key (see
    # media_materialize._PANEL_KEY_TO_IMAGE_SOURCE).
    media_source_key = CharField(max_length=30, null=True, blank=True)
    media_item_key = CharField(max_length=40, null=True, blank=True)
    # The photo's own GPS position (EXIF, or user drag-placement on the map).
    # Kept separate from the `location` FK so each photo can scatter at its exact
    # capture point on the map layer; `location` records which shared place the
    # photo belongs to.
    #
    # KNOWN OMISSION: one pair of columns holds two different provenances - what
    # EXIF reported, and where a person put it - with nothing in the schema
    # recording which a given row holds. Two consequences worth knowing before
    # writing here:
    #   * The EXIF answer survives only in `exif_data["GPSInfo"]`, and only for
    #     profiles that did not opt out of location metadata (tasks.py pops
    #     GPSInfo when strip_location is set).
    #   * tasks.process_image_upload rewrites these columns unconditionally from
    #     EXIF, and a dozen call sites can re-enqueue it, so a re-run replaces a
    #     manually corrected position with the EXIF one.
    # TODO: add exif_latitude/exif_longitude (or a coordinate_source field)
    # before any NEW writer is introduced. Placing a photo from the floorplan
    # editor is deliberately NOT that writer until this exists.
    latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # Crowd-sourced approximation of this photo's own position, from
    # anonymized SpotGuessr guesses (services.photos.photo_coordinates) - only ever
    # set once a photo has accumulated enough guesses to be worth showing,
    # and always deferred to `latitude`/`longitude` above the moment a real
    # (manual or EXIF) position exists for this photo - see effective_latitude/
    # effective_longitude below. Never treat this as confirmed; it exists
    # specifically to surface still-unplaced photos on maps so a wiki user
    # notices and corrects the exact placement.
    estimated_latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    estimated_longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # Compass bearing (0-360, true or magnetic north per the device's own
    # EXIF GPSImgDirectionRef - not itself preserved, see
    # services.media.images.extract_gps_direction) the camera was facing when this
    # photo was taken. Standardized field for a future "same place, same
    # angle, over time" comparison UI - not read or displayed anywhere yet.
    # Same GPS-IFD-sourced privacy opt-out as latitude/longitude: never
    # extracted for a profile with visit-history tracking off.
    #
    # TODO: EXIF is currently the only writer - there is no UI that sets a
    # heading. The planned first consumer is a photo attached to a floorplan
    # item (FloorplanReference), pointed at the thing it depicts. That UI has to
    # declare its own reference frame, because GPSImgDirectionRef - true versus
    # magnetic north - is not preserved (services.media.images.extract_gps_direction),
    # so a manually set heading and an EXIF one are not directly comparable.
    direction = DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    # SHA-256 hex digest of the uploaded file, used to reject duplicate uploads.
    # Nullable because rows predating this field are backfilled lazily (in
    # process_image_upload) - duplicate checks simply skip unhashed rows.
    checksum = CharField(max_length=64, null=True, blank=True, db_index=True)
    # EXIF DateTimeOriginal (capture time), when present - distinct from
    # `created`/`updated`, which only track upload time. Null for photos with
    # no EXIF data or that predate this field; consumers should fall back to
    # `created` when absent.
    taken_at = DateTimeField(null=True, blank=True)
    # Bytes currently occupied by the stored file - the size after any
    # downscaling/webp conversion, counted against the uploader's storage quota.
    # Nullable because rows predating this field are backfilled lazily by
    # process_image_upload; usage sums simply skip unmeasured rows until then.
    file_size = BigIntegerField(null=True, blank=True)
    # Why this row's bytes don't count against its profile's storage quota
    # (empty = they do). Set at creation by whichever path produced a row that
    # owns no exclusive storage of its own - services.media.media_materialize
    # (EXTERNAL_MEDIA), services.media.quota_rewards (COMMUNITY_CONTRIBUTION),
    # services.sharing.pin_sharing (SHARED_COPY) - see QuotaExemption for what
    # each value means. Materialized rather than recomputed because the community-contribution
    # case is a one-way reward: a photo that earned its exemption keeps it
    # even if voters later change their minds, so a user's stored photos can
    # never retroactively push them over quota.
    quota_exempt_reason = CharField(max_length=20, blank=True, default="", choices=QuotaExemption.choices)
    # Full EXIF metadata captured from the original upload BEFORE any
    # downscaling or format conversion. Keys are human-readable tag names;
    # values are JSON-sanitized (rationals/bytes stringified).
    #
    # Encrypted, and the only copy: the stored file has its EXIF removed on the
    # way in (see services.media.images.downscale_stored_image), so this column
    # holds what the photo no longer carries - camera make, model and serial,
    # and, unless the uploader opted out of location, where the shot was taken.
    # fail_soft because there is nothing to re-fetch it from: a key mismatch
    # must degrade this one field rather than break every gallery that loads a
    # photo row. Never filter on its contents; ciphertext does not compare.
    exif_data = EncryptedJSONField(null=True, blank=True, fail_soft=True)
    # Extracted text for a document upload: the PDF's native text layer plus
    # OCR output from any embedded raster images (see services.media.documents).
    # Searched by the Media section's search box (labels__name, caption, etc.)
    # the same way as every other text field on this model.
    ocr_text = TextField(null=True, blank=True)
    # Set when the user explicitly clears an unfiled photo out of the Memories
    # "needs attention" organize queue without deleting it (e.g. a photo with no
    # GPS they don't want to tie to a visit). Keeps that queue finite; the photo
    # still appears in the full gallery.
    organize_dismissed = BooleanField(default=False)
    # Media (kind='media') labels help the user find this photo/video/document
    # via the main site search; unlike Pin/Wiki labels, media labels have no
    # effect on map icons or filtering.
    labels = ManyToManyField("dashboard.Label", related_name="images", blank=True)
    # Cached relevance score from REData's photo-scoring service
    # (services.photos.redata_relevance) - "how likely is this really a photo of
    # this place", a calibrated probability in [0.02, 0.98]. Never computed
    # locally: set from the response to submitting this photo (POST /photos/)
    # and left untouched afterward - REData caches its own score indefinitely
    # and only ever changes it when a newer model is promoted, which this row
    # doesn't proactively poll for. Null for any photo never submitted (no
    # location, REData not configured, or submission still pending/failed) -
    # ordering queries must treat null as "unknown", not "irrelevant".
    redata_confidence = FloatField(null=True, blank=True)
    # "heuristic" or "model" - which of REData's two scorers produced
    # redata_confidence, kept mostly for admin/debugging visibility.
    redata_scorer = CharField(max_length=10, null=True, blank=True)
    # The trained model version that scored this photo, when redata_scorer is
    # "model" - null both before scoring and whenever the heuristic scorer
    # answered instead.
    redata_model_version = PositiveIntegerField(null=True, blank=True)
    redata_scored_at = DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:
        pin_id: int | None
        wiki_id: int | None
        location_id: int | None
        safety_checkin_id: int | None
        visit_id: int | None
        direct_message_id: int | None
        profile_id: int | None
        pin_suggestion_id: int | None

    objects = ImageManager()

    @property
    def attribution_url(self) -> str:
        """Where to send a person to see this photo in its original context.

        Prefers the provider's page, which is the one meant to be read by a
        human, and falls back to the file itself when there is no page.
        """
        return self.source_url or self.source_media_url or ""

    @property
    def origin_media_url(self) -> str:
        """Where the bytes came from, for re-fetching or comparison.

        The opposite preference to :attr:`attribution_url`: a landing page is no
        use for fetching an image, so the direct address wins when there is one.
        """
        return self.source_media_url or self.source_url or ""

    @property
    def display_url(self) -> str:
        """The URL to render this photo from.

        A row can exist without a stored file - an external gallery item whose
        download failed still carries its ``source_url`` - and reading
        ``image.url`` on one of those raises. Templates and serializers use
        this instead so a single such row can't break a whole grid.

        Returns:
            The stored file's URL, the remote source URL, or "" when neither
            is set.
        """
        if self.image:
            return self.image.url
        return self.source_url or ""

    @property
    def thumb_url(self) -> str:
        """The URL to render this photo from in a grid of many.

        Grids (albums, galleries) should load this instead of
        :attr:`display_url` so a page of dozens of photos does not decode
        full-resolution files. Falls back to the original when no thumbnail
        has been generated yet (upload processing still running, or a row
        the periodic backfill has not reached).

        Returns:
            The thumbnail URL, the original's URL, or "".
        """
        if self.thumbnail:
            return self.thumbnail.url
        return self.display_url

    @property
    def effective_latitude(self) -> Decimal | None:
        """The best-known latitude for this photo.

        Prefers the photo's own real (manual/EXIF) GPS position; then a
        crowd-sourced SpotGuessr estimate, if one exists; then falls back to
        the coordinates of the shared Location it belongs to. A real
        position always wins outright, no matter how many guesses back the
        estimate - see ``estimated_latitude``'s docstring.

        Returns:
            The latitude, or None when the photo has no position of any kind
            and its location doesn't either.
        """
        if self.latitude is not None:
            return self.latitude
        if self.estimated_latitude is not None:
            return self.estimated_latitude
        location = self.location
        if location is not None and location.latitude is not None:
            return location.latitude
        return None

    @property
    def effective_longitude(self) -> Decimal | None:
        """The best-known longitude for this photo.

        Prefers the photo's own real (manual/EXIF) GPS position; then a
        crowd-sourced SpotGuessr estimate, if one exists; then falls back to
        the coordinates of the shared Location it belongs to. A real
        position always wins outright, no matter how many guesses back the
        estimate - see ``estimated_latitude``'s docstring.

        Returns:
            The longitude, or None when the photo has no position of any
            kind and its location doesn't either.
        """
        if self.longitude is not None:
            return self.longitude
        if self.estimated_longitude is not None:
            return self.estimated_longitude
        location = self.location
        if location is not None and location.longitude is not None:
            return location.longitude
        return None

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_images"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["location", "media_source_key", "media_item_key"], name="idxdb_image_media_key"),
            # Serves both halves of quota accounting (used vs. exempt bytes),
            # which always filter by profile first. Composite rather than a
            # standalone index on quota_exempt_reason: that column has three
            # values, so indexing it alone would rarely be chosen by the
            # planner while still costing a write on every photo upload.
            Index(fields=["profile", "quota_exempt_reason"], name="idxdb_image_profile_quota"),
        ]
