"""Image model for pin and wiki photo uploads."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.db.models import CASCADE, SET_NULL, BigIntegerField, BooleanField, CharField, DateTimeField, DecimalField, ForeignKey, ImageField, Index, JSONField, ManyToManyField, TextField, URLField, UUIDField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.abstract.choices import TextChoices
from urbanlens.dashboard.models.images.queryset import ImageManager

if TYPE_CHECKING:
    from decimal import Decimal


class ImageSource(TextChoices):
    """Where a photo originated - drives the Media section's per-source tabs.

    ``UPLOAD`` is the default for ordinary user uploads (personal galleries).
    The external values are set only on rows materialized from the Media
    gallery's transient provider results (see ``services.external_data`` and
    ``services.media_materialize``) when a user sends one to a wiki or sets it
    as a cover photo - the Media gallery itself renders straight from each
    provider's live results without persisting an ``Image`` row per item.
    """

    UPLOAD = "upload", "Upload"
    YELP = "yelp", "Yelp"
    GOOGLE_IMAGES = "google_images", "Google Images"
    GOOGLE_MAPS = "google_maps", "Google Maps"
    WIKIMEDIA = "wikimedia", "Wikimedia Commons"
    WIKIPEDIA_MEDIA = "wikipedia_media", "Wikipedia"
    SMITHSONIAN = "smithsonian", "Smithsonian Open Access"
    LIBRARY_OF_CONGRESS = "library_of_congress", "Library of Congress"
    INTERNET_ARCHIVE = "internet_archive", "Internet Archive"
    IMMICH = "immich", "Immich"
    FLICKR = "flickr", "Flickr"
    GOOGLE_PHOTOS = "google_photos", "Google Photos"


class MediaKind(TextChoices):
    """What kind of file this Image row actually holds.

    Photos, videos, and documents all share every other field on this model
    (caption, author, location, labels, etc.) - this is only a discriminator
    for upload-time processing (services.videos/services.documents) and
    display (player vs. viewer vs. image tag).
    """

    PHOTO = "photo", "Photo"
    VIDEO = "video", "Video"
    DOCUMENT = "document", "Document"


class Image(abstract.FrontendDashboardModel):
    """A photo, video, or document uploaded by a user, attached to a pin, community wiki, or safety check-in."""

    image = ImageField(upload_to="pin_images/")
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
    copyright = CharField(max_length=255, null=True, blank=True)
    # The photo's own GPS position (EXIF, or user drag-placement on the map).
    # Kept separate from the `location` FK so each photo can scatter at its exact
    # capture point on the map layer; `location` records which shared place the
    # photo belongs to.
    latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
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
    # Full EXIF metadata captured from the original upload BEFORE any
    # downscaling or format conversion, so nothing is lost if the stored file
    # is re-encoded. Keys are human-readable tag names; values are
    # JSON-sanitized (rationals/bytes stringified).
    exif_data = JSONField(null=True, blank=True)
    # Extracted text for a document upload: the PDF's native text layer plus
    # OCR output from any embedded raster images (see services.documents).
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
    def effective_latitude(self) -> Decimal | None:
        """The best-known latitude for this photo.

        Prefers the photo's own GPS position; falls back to the coordinates of
        the shared Location it belongs to.

        Returns:
            The latitude, or None when neither the photo nor its location has one.
        """
        if self.latitude is not None:
            return self.latitude
        location = self.location
        if location is not None and location.latitude is not None:
            return location.latitude
        return None

    @property
    def effective_longitude(self) -> Decimal | None:
        """The best-known longitude for this photo.

        Prefers the photo's own GPS position; falls back to the coordinates of
        the shared Location it belongs to.

        Returns:
            The longitude, or None when neither the photo nor its location has one.
        """
        if self.longitude is not None:
            return self.longitude
        location = self.location
        if location is not None and location.longitude is not None:
            return location.longitude
        return None

    class Meta(abstract.FrontendDashboardModel.Meta):
        db_table = "dashboard_images"
        get_latest_by = "updated"
        indexes = [Index(fields=["uuid"], name="idxdb_image_uuid")]
