"""Image model for pin and location photo uploads."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.db.models import CASCADE, SET_NULL, CharField, DateTimeField, DecimalField, ForeignKey, ImageField, Index, UUIDField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.images.queryset import ImageManager


class Image(abstract.FrontendDashboardModel):
    """A photo uploaded by a user, attached to a pin, shared location, or safety check-in."""

    image = ImageField(upload_to="pin_images/")
    pin = ForeignKey(
        "dashboard.Pin",
        on_delete=SET_NULL,
        related_name="images",
        null=True,
        blank=True,
    )
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
    # pin/location gallery - it just loses its visit association.
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
    latitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # EXIF DateTimeOriginal (capture time), when present - distinct from
    # `created`/`updated`, which only track upload time. Null for photos with
    # no EXIF data or that predate this field; consumers should fall back to
    # `created` when absent.
    taken_at = DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:
        pin_id: int | None
        location_id: int | None
        safety_checkin_id: int | None
        visit_id: int | None
        profile_id: int | None

    objects = ImageManager()

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_images"
        get_latest_by = "updated"
        indexes = [Index(fields=["uuid"], name="idxdb_image_uuid")]
