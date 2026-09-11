"""Image model for pin and wiki photo uploads."""

from __future__ import annotations

import posixpath
import secrets
from typing import TYPE_CHECKING, ClassVar
from uuid import uuid4

from django.db.models import (
    CASCADE,
    SET_NULL,
    BigIntegerField,
    BooleanField,
    CharField,
    DateTimeField,
    DecimalField,
    FloatField,
    ForeignKey,
    ImageField,
    Index,
    IntegerField,
    ManyToManyField,
    PositiveIntegerField,
    PositiveSmallIntegerField,
    Q,
    TextField,
    URLField,
)

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.abstract.choices import TextChoices
from urbanlens.dashboard.models.fields import EncryptedJSONField, EncryptedTextField
from urbanlens.dashboard.models.images.queryset import ImageManager
from urbanlens.dashboard.services.media.access import declares_media_family

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal

_UPLOAD_EXT_LIMIT = 12

# Randomness for upload directory names; bucket prefix avoids one flat directory.
_UPLOAD_TOKEN_BYTES = 12

_UPLOAD_BUCKET_CHARS = 2


def _random_upload_dir() -> str:
    """Return a fresh ``<bucket>/<token>`` directory for one stored file."""
    token = secrets.token_urlsafe(_UPLOAD_TOKEN_BYTES)
    return f"{token[:_UPLOAD_BUCKET_CHARS]}/{token[_UPLOAD_BUCKET_CHARS:]}"


def anonymized_media_stem(instance: Image | None) -> str:
    """Random filename stem, year-prefixed when a capture date is known.

    The uploaded filename never reaches storage, since device names can leak
    capture details; only the year is kept for downloaded files.
    """
    taken = getattr(instance, "taken_at", None) or getattr(instance, "filename_taken_at", None)
    token = uuid4().hex
    return f"{taken.year}-{token}" if taken is not None else token


@declares_media_family("pin_images")
def pin_image_upload_path(instance: Image, filename: str) -> str:
    """Storage path under a random directory with an opaque name.

    Migrations serialize ``upload_to`` by reference, so this stays public.
    """
    ext = posixpath.splitext(filename)[1]
    return f"pin_images/{_random_upload_dir()}/{anonymized_media_stem(instance)}{ext[:_UPLOAD_EXT_LIMIT]}"


@declares_media_family("pin_images")
def pin_image_thumbnail_path(instance: Image, filename: str) -> str:
    """Storage path for the grid thumbnail under its own random directory."""
    stem, ext = posixpath.splitext(filename)
    return f"pin_images/thumbs/{_random_upload_dir()}/{stem[:100]}{ext[:_UPLOAD_EXT_LIMIT]}"


@declares_media_family("pin_images")
def pin_image_marker_thumbnail_path(instance: Image, filename: str) -> str:
    """Storage path for the tiny map-marker thumbnail."""
    stem, ext = posixpath.splitext(filename)
    return f"pin_images/markers/{_random_upload_dir()}/{stem[:100]}{ext[:_UPLOAD_EXT_LIMIT]}"


@declares_media_family("pin_images")
def pin_image_analysis_thumbnail_path(instance: Image, filename: str) -> str:
    """Storage path for the copy sent to vision models."""
    stem, ext = posixpath.splitext(filename)
    return f"pin_images/analysis/{_random_upload_dir()}/{stem[:100]}{ext[:_UPLOAD_EXT_LIMIT]}"


class ImageSource(TextChoices):
    """Where a photo originated; drives the Media section's per-source tabs."""

    UPLOAD = "upload", "Upload"
    #: Pasted URL whose bytes were fetched and stored like an upload.
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

    @classmethod
    def personal_library(cls) -> frozenset[str]:
        """Sources that mean this profile's own picture."""
        return frozenset({cls.UPLOAD, cls.IMMICH, cls.GOOGLE_PHOTOS, cls.FLICKR})


class MediaKind(TextChoices):
    """What kind of file this Image row holds."""

    PHOTO = "photo", "Photo"
    VIDEO = "video", "Video"
    DOCUMENT = "document", "Document"


class QuotaExemption(TextChoices):
    """Why a stored file's bytes don't count against its uploader's quota."""

    EXTERNAL_MEDIA = "external_media", "Cached external media"
    COMMUNITY_CONTRIBUTION = "community", "Community-valued contribution"
    SHARED_COPY = "shared_copy", "Copy of a shared photo"
    DEDUPLICATED = "deduplicated", "Same file already stored for this user"
    WIKI_COPY = "wiki_copy", "Copy of a wiki photo"


class Image(abstract.FrontendDashboardModel):
    """A photo, video, or document uploaded by a user, attached to a pin, community wiki, or safety check-in."""

    image = ImageField(upload_to=pin_image_upload_path, max_length=255)
    # Grid preview; empty until generated.
    thumbnail = ImageField(upload_to=pin_image_thumbnail_path, max_length=255, null=True, blank=True)
    # Map-marker preview; empty until generated.
    marker_thumbnail = ImageField(upload_to=pin_image_marker_thumbnail_path, max_length=255, null=True, blank=True)
    # Separate copy for vision models.
    analysis_thumbnail = ImageField(upload_to=pin_image_analysis_thumbnail_path, max_length=255, null=True, blank=True)
    # True upload filename, kept for attribution and downloads.
    original_filename = EncryptedTextField(blank=True, default="", fail_soft=True)
    # Capture date parsed from the filename when EXIF has none.
    filename_taken_at = DateTimeField(null=True, blank=True)
    media_type = CharField(max_length=10, choices=MediaKind.choices, default=MediaKind.PHOTO, db_index=True)
    # Media-gallery source tab.
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
    # Shared place this photo depicts; per-photo GPS lives on latitude/longitude.
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
    # Visit this photo documents, if attached to one.
    visit = ForeignKey(
        "dashboard.PinVisit",
        on_delete=SET_NULL,
        related_name="images",
        null=True,
        blank=True,
    )
    # DM this photo was sent as an attachment, if any.
    direct_message = ForeignKey(
        "dashboard.DirectMessage",
        on_delete=SET_NULL,
        related_name="images",
        null=True,
        blank=True,
    )
    # Staged candidate photo awaiting PinSuggestion acceptance.
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
    # Provenance for a wiki-to-pin copy; FKs stay to preserve attribution.
    copied_from = ForeignKey(
        "self",
        on_delete=SET_NULL,
        related_name="copies",
        null=True,
        blank=True,
    )
    copied_from_profile = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        related_name="+",
        null=True,
        blank=True,
    )
    copied_from_location = ForeignKey(
        "dashboard.Location",
        on_delete=SET_NULL,
        related_name="+",
        null=True,
        blank=True,
    )
    copied_from_label = CharField(max_length=255, blank=True, default="")
    caption = CharField(max_length=500, null=True, blank=True)
    # Attribution shown in the lightbox; unattributed non-camera filenames stay blank.
    author = CharField(max_length=255, null=True, blank=True)
    source_url = URLField(max_length=500, null=True, blank=True)
    #: Direct file address when it differs from the attribution page above.
    source_media_url = URLField(max_length=500, null=True, blank=True)
    copyright = CharField(max_length=255, null=True, blank=True)
    # Raw provider key plus item hash joining a materialized row back to its votes.
    media_source_key = CharField(max_length=30, null=True, blank=True)
    media_item_key = CharField(max_length=40, null=True, blank=True)
    # Photo's own GPS; separate from the shared Location FK.
    latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # Original camera-reported position, never overwritten once set.
    exif_latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    exif_longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # Crowd-sourced position estimate; defers to real GPS when present.
    estimated_latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    estimated_longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # Compass bearing the camera faced; EXIF-only for now.
    # TODO: no UI sets a heading yet.
    direction = DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    # Pitch/roll from panorama/drone metadata; most rows lack these.
    exif_pitch = DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    exif_roll = DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # Altitude in meters, from EXIF.
    exif_altitude = DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    # Camera/lens ID and exposure, sourced once from EXIF.
    exif_camera_make = CharField(max_length=255, null=True, blank=True)
    exif_camera_model = CharField(max_length=255, null=True, blank=True)
    exif_lens_model = CharField(max_length=255, null=True, blank=True)
    # Shutter speed as a display string (e.g. "1/250").
    exif_shutter_speed = CharField(max_length=32, null=True, blank=True)
    exif_aperture = DecimalField(max_digits=4, decimal_places=1, null=True, blank=True)
    exif_focal_length = DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    # Floor taken on; reserved for future use, currently unset.
    exif_floor = IntegerField(null=True, blank=True)
    # SHA-256 of the file, for duplicate rejection.
    checksum = CharField(max_length=64, null=True, blank=True, db_index=True)
    # EXIF capture time; distinct from upload time. Falls back to ``created``.
    taken_at = DateTimeField(null=True, blank=True)
    # Stored file size after processing, counted against quota.
    file_size = BigIntegerField(null=True, blank=True)
    # Set once upload processing strips EXIF and rewrites the file.
    upload_processed_at = DateTimeField(null=True, blank=True)
    # Set when upload processing definitively fails.
    upload_failed_at = DateTimeField(null=True, blank=True)
    # Recovery-sweep retry count; bounded so poison files stop requeuing.
    upload_sweep_attempts = PositiveSmallIntegerField(default=0)
    # Why this row is exempt from quota; empty means it counts.
    quota_exempt_reason = CharField(max_length=20, blank=True, default="", choices=QuotaExemption.choices)
    # Full EXIF from the original upload; encrypted, never filter on it.
    exif_data = EncryptedJSONField(null=True, blank=True, fail_soft=True)
    # Extracted document text, searched like caption.
    ocr_text = TextField(null=True, blank=True)
    # Cleared from the Memories organize queue without deleting.
    organize_dismissed = BooleanField(default=False)
    # Keeps GPS but omits the photo from map layers.
    map_hidden = BooleanField(default=False)
    # True while the raw upload awaits processing; visible to uploader only.
    pending_scan = BooleanField(default=False)
    # Search-only labels; no effect on map icons.
    labels = ManyToManyField("dashboard.Label", related_name="images", blank=True)
    # Cached external photo-relevance score; null means unknown.
    redata_confidence = FloatField(null=True, blank=True)
    # Which scorer produced the confidence above.
    redata_scorer = CharField(max_length=10, null=True, blank=True)
    # Model version behind the score, when applicable.
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
        copied_from_id: int | None
        copied_from_profile_id: int | None
        copied_from_location_id: int | None
        # Stamped by album helpers; absent otherwise.
        album_item_id: int

    objects = ImageManager()

    def save(self, *args, **kwargs) -> None:
        """Capture the upload's true filename before storage anonymizes it."""
        incoming_name = self.image.name if self.image else None
        if incoming_name and not self.image._committed and not self.original_filename:  # type: ignore[attr-defined]  # noqa: SLF001
            self.original_filename = incoming_name
            if self.filename_taken_at is None:
                from urbanlens.dashboard.services.media.images import extract_filename_taken_at

                self.filename_taken_at = extract_filename_taken_at(incoming_name)
        super().save(*args, **kwargs)

    @property
    def is_own_contribution(self) -> bool:
        """Whether ``profile`` photographed this rather than up-voting it."""
        return self.profile_id is not None and self.source in ImageSource.personal_library() and not self.media_source_key

    @property
    def attribution_url(self) -> str:
        """Provider page for this photo, falling back to the file itself."""
        return self.source_url or self.source_media_url or ""

    @property
    def origin_media_url(self) -> str:
        """Direct file address for this photo, falling back to the page."""
        return self.source_media_url or self.source_url or ""

    @property
    def display_url(self) -> str:
        """Stored file URL, falling back to the remote source."""
        if self.image:
            return self.image.url
        return self.source_url or ""

    @property
    def thumb_url(self) -> str:
        """Grid thumbnail URL, falling back to the original."""
        if self.thumbnail:
            return self.thumbnail.url
        return self.display_url

    @property
    def marker_thumb_url(self) -> str:
        """Marker thumbnail URL, falling back through larger images."""
        if self.marker_thumbnail:
            return self.marker_thumbnail.url
        return self.thumb_url

    @property
    def effective_taken_at(self) -> datetime | None:
        """Best-known capture time: EXIF first, then filename date."""
        return self.taken_at or self.filename_taken_at

    #: Extension -> icon name for document tiles without thumbnails.
    _DOCUMENT_ICONS_BY_EXTENSION: ClassVar[dict[str, str]] = {
        "pdf": "picture_as_pdf",
        "doc": "description",
        "docx": "description",
        "odt": "description",
        "rtf": "article",
        "txt": "article",
        "xls": "table_chart",
        "xlsx": "table_chart",
        "ods": "table_chart",
        "csv": "table_chart",
        "ppt": "slideshow",
        "pptx": "slideshow",
        "odp": "slideshow",
    }

    @property
    def display_caption(self) -> str:
        """Stripped caption, or ``""``; whitespace alone counts as none."""
        return (self.caption or "").strip()

    @property
    def document_icon(self) -> str:
        """Icon name for this document's tile, from its filename extension."""
        name = self.caption or (self.image.name or "" if self.image else "")
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        return self._DOCUMENT_ICONS_BY_EXTENSION.get(extension, "insert_drive_file")

    @property
    def effective_latitude(self) -> Decimal | None:
        """Best-known latitude: real GPS, then estimate, then Location."""
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
        """Best-known longitude: real GPS, then estimate, then Location."""
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
            Index(fields=["profile", "quota_exempt_reason"], name="idxdb_image_profile_quota"),
            Index(fields=["created"], name="idxdb_image_pending_created", condition=Q(pending_scan=True)),
            Index(fields=["profile", "copied_from_profile"], name="idxdb_img_profile_copied_from"),
            Index(fields=["profile", "media_type", "-created", "-id"], name="idxdb_img_profile_kind_recent"),
        ]
