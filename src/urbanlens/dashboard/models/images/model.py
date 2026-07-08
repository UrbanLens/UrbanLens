"""Image model for pin and wiki photo uploads."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.db.models import CASCADE, SET_NULL, BooleanField, CharField, DateTimeField, DecimalField, ForeignKey, ImageField, Index, UUIDField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.images.queryset import ImageManager

if TYPE_CHECKING:
    from decimal import Decimal


class Image(abstract.FrontendDashboardModel):
    """A photo uploaded by a user, attached to a pin, community wiki, or safety check-in."""

    image = ImageField(upload_to="pin_images/")
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
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="uploaded_images",
        null=True,
        blank=True,
    )
    caption = CharField(max_length=500, null=True, blank=True)
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
    # Set when the user explicitly clears an unfiled photo out of the Memories
    # "needs attention" organize queue without deleting it (e.g. a photo with no
    # GPS they don't want to tie to a visit). Keeps that queue finite; the photo
    # still appears in the full gallery.
    organize_dismissed = BooleanField(default=False)

    if TYPE_CHECKING:
        pin_id: int | None
        wiki_id: int | None
        location_id: int | None
        safety_checkin_id: int | None
        visit_id: int | None
        profile_id: int | None

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
