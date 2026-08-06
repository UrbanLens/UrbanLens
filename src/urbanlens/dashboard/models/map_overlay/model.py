"""Georeferenced image overlays drawn on a pin's or wiki's map.

A user pins a historical map image - a Sanborn fire-insurance sheet, a site
plan, a hand-drawn survey - onto the live map and drags its four corners until
its streets line up with the real ones. Scans are never axis-aligned or square
to north, and a flatbed scan of a century-old sheet is usually a little
trapezoidal, so four independently-placed corners (a full projective
transform) are the honest unit rather than a bounding box plus a rotation.

The corners are stored as WGS-84 coordinates rather than as a transform
matrix: they mean the same thing at every zoom level and after any base-layer
change, and they stay correct if the rendering ever moves off Leaflet. The
matrix is recomputed client-side per frame from those four points.

Attaches to a Pin or a Wiki with the same one-of-two-parents convention
:class:`~urbanlens.dashboard.models.markup.model.PinMarkup` and
:class:`~urbanlens.dashboard.models.markup.model.CustomLayer` use, and may
optionally sit inside a ``CustomLayer`` so it toggles with that layer's other
markup instead of on its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import (
    CASCADE,
    SET_NULL,
    BooleanField,
    CharField,
    FloatField,
    ForeignKey,
    Index,
    IntegerField,
    URLField,
)

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.map_overlay.queryset import MapImageOverlayManager

#: Corner order used everywhere this model is serialized, drawn, or edited:
#: north-west, north-east, south-east, south-west - clockwise from the top
#: left of the *image*, not of the map. "North-west" names the corner's
#: starting position, not a constraint: a rotated overlay legitimately ends up
#: with its ``nw`` corner east of its ``ne`` one.
CORNERS = ("nw", "ne", "se", "sw")


class MapImageOverlay(abstract.FrontendDashboardModel):
    """One image georeferenced onto a pin's or wiki's map by its four corners.

    Attributes:
        uuid: Stable public identifier (used in URLs).
        name: User-supplied label shown in the layers panel.
        image: An ``Image`` row holding the file, when the overlay came from an
            upload or from a Media-gallery item the user picked (gallery items
            are materialized into a real ``Image`` first, so the overlay keeps
            working when the provider's URL rots).
        image_url: An external image URL instead, for a user who would rather
            reference a remote sheet than store a copy. Exactly one of this and
            ``image`` is set.
        nw_latitude: Latitude of the image's top-left corner. Likewise for the
            other three corners, in :data:`CORNERS` order.
        opacity: Percent opacity, so a user can see the real map underneath -
            the whole point of overlaying a historical sheet.
        order: Display order among a pin/wiki's own overlays; higher draws on top.
        default_visible: Whether this overlay starts toggled on. Defaults True,
            unlike ``CustomLayer`` - someone who has georeferenced a sheet onto
            this exact property wants to see it.
        locked: Whether the corner handles are hidden and the overlay can't be
            dragged. Set once the alignment is right, so panning the map
            doesn't nudge a carefully-placed corner.
        layer: Optional ``CustomLayer`` this overlay belongs to; when set it
            shows and hides with that layer instead of having its own toggle.
        parent_pin: The Pin whose detail map this overlay belongs to, if personal.
        parent_wiki: The Wiki whose map this overlay belongs to, if shared.
        profile: The user who created it.
    """

    name = CharField(max_length=100, blank=True, default="")

    image = ForeignKey(
        "dashboard.Image",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="map_overlays",
    )
    image_url = URLField(max_length=1000, blank=True, default="")

    nw_latitude = FloatField()
    nw_longitude = FloatField()
    ne_latitude = FloatField()
    ne_longitude = FloatField()
    se_latitude = FloatField()
    se_longitude = FloatField()
    sw_latitude = FloatField()
    sw_longitude = FloatField()

    opacity = IntegerField(default=70, validators=[MinValueValidator(0), MaxValueValidator(100)])
    order = IntegerField(default=0)
    default_visible = BooleanField(default=True)
    locked = BooleanField(default=False)

    layer = ForeignKey(
        "dashboard.CustomLayer",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="image_overlays",
    )
    parent_pin = ForeignKey(
        "dashboard.Pin",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="image_overlays",
    )
    parent_wiki = ForeignKey(
        "dashboard.Wiki",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="image_overlays",
    )
    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="image_overlays",
    )

    if TYPE_CHECKING:
        image_id: int | None
        layer_id: int | None
        parent_pin_id: int | None
        parent_wiki_id: int | None
        profile_id: int

    objects = MapImageOverlayManager()

    class Meta(abstract.FrontendDashboardModel.Meta):
        ordering = ["order", "id"]
        indexes = [
            Index(fields=["parent_pin", "order"], name="idx_overlay_pin_order"),
            Index(fields=["parent_wiki", "order"], name="idx_overlay_wiki_order"),
        ]

    def __str__(self) -> str:
        return self.name or f"Image overlay {self.uuid}"

    @property
    def source_url(self) -> str:
        """The URL the browser should load the image from.

        Returns:
            The stored file's URL when this overlay owns an ``Image``, else the
            external ``image_url``, else ``""`` (an overlay whose backing image
            was deleted - the map skips it rather than rendering a broken tile).
        """
        if self.image_id and self.image and self.image.image:
            return self.image.image.url
        return self.image_url

    def corners(self) -> list[list[float]]:
        """The four corners as ``[[lat, lng], ...]`` in :data:`CORNERS` order."""
        return [
            [self.nw_latitude, self.nw_longitude],
            [self.ne_latitude, self.ne_longitude],
            [self.se_latitude, self.se_longitude],
            [self.sw_latitude, self.sw_longitude],
        ]

    def set_corners(self, corners: list[list[float]] | list[tuple[float, float]]) -> None:
        """Assign all four corners at once.

        Args:
            corners: Four ``[lat, lng]`` pairs in :data:`CORNERS` order.

        Raises:
            ValueError: ``corners`` isn't exactly four pairs - a three- or
                five-corner overlay has no meaning and would otherwise persist
                as a partial update of some of the eight columns.
        """
        if len(corners) != len(CORNERS):
            raise ValueError(f"Expected {len(CORNERS)} corners, got {len(corners)}.")
        for name, (latitude, longitude) in zip(CORNERS, corners, strict=True):
            setattr(self, f"{name}_latitude", float(latitude))
            setattr(self, f"{name}_longitude", float(longitude))

    def to_json(self) -> dict:
        """Compact serialisation for the map's overlay renderer and edit dialog.

        Returns:
            dict with uuid, name, url, corners, opacity, order, visibility,
            lock state, and the uuid of the custom layer it belongs to (or
            None).
        """
        return {
            "uuid": str(self.uuid),
            "name": self.name,
            "url": self.source_url,
            "corners": self.corners(),
            "opacity": self.opacity,
            "order": self.order,
            "default_visible": self.default_visible,
            "locked": self.locked,
            "layer_uuid": str(self.layer.uuid) if self.layer_id and self.layer else None,
        }
