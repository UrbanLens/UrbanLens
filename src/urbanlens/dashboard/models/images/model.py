"""Image model for pin and location photo uploads."""

from __future__ import annotations

from uuid import uuid4

from django.db.models import CASCADE, SET_NULL, CharField, DateTimeField, DecimalField, ForeignKey, ImageField, Index, UUIDField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.images.queryset import ImageManager


class Image(abstract.Model):
    """A photo uploaded by a user, attached to a pin or shared location."""

    uuid = UUIDField(default=uuid4, unique=True, editable=False)
    image = ImageField(upload_to="pin_images/")
    pin = ForeignKey(
        "dashboard.Pin",
        on_delete=CASCADE,
        related_name="images",
        null=True,
        blank=True,
    )
    location = ForeignKey(
        "dashboard.Location",
        on_delete=CASCADE,
        related_name="images",
        null=True,
        blank=True,
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
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

    objects = ImageManager()

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_images"
        get_latest_by = "updated"
        indexes = [Index(fields=["uuid"], name="idxdb_image_uuid")]
