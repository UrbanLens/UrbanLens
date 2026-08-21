"""Album models - named, optionally-ordered groupings of a place's photos.

An album belongs to exactly one owner: a ``Pin`` (personal, owner-only) or a
``Wiki`` (community, editable by anyone who can see the wiki), mirroring how
``CustomLayer`` scopes its own pin/wiki split. Albums never span the two - a
pin album holds only that pin's photos and a wiki album only that wiki's, so
an album can't be used to pull a private pin photo onto a shared surface.

``kind`` marks albums that carry extra behaviour beyond grouping. Only
``TIMELAPSE`` exists so far and nothing acts on it yet; it's declared now so
the column and its validation rules are in place before the feature that
reads them (a series of shots of the same scene from the same angle, rendered
to video) is built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db.models import CASCADE, SET_NULL, BooleanField, CharField, ForeignKey, Index, IntegerField, TextChoices, TextField
from django.db.models.constraints import UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.album.queryset import AlbumItemManager, AlbumManager
from urbanlens.dashboard.services.core.text_limits import MAX_ALBUM_DESCRIPTION_LENGTH

if TYPE_CHECKING:
    from django.db.models import Manager as DjangoManager


class AlbumKind(TextChoices):
    """What kind of album this is, and therefore what extra behaviour it offers."""

    PLAIN = "plain", "Album"
    TIMELAPSE = "timelapse", "Timelapse"


@dataclass(frozen=True, slots=True)
class AlbumKindSpec:
    """Per-kind presentation and behaviour, so adding a kind is a one-place change.

    Attributes:
        kind: The :class:`AlbumKind` this describes.
        icon: Material Symbols icon name for the album's tile and header.
        label: Human-readable name, used in the create form's type picker.
        help_text: One-line explanation shown beside the type picker.
        badge: Whether the tile shows a kind badge. Plain albums don't - a
            badge on every album would carry no information.
        prefers_manual_order: Whether new albums of this kind start in
            manual-order mode. A timelapse is a sequence, so its order is the
            point; a plain album is a grouping and defaults to newest-first.
    """

    kind: str
    icon: str
    label: str
    help_text: str
    badge: bool
    prefers_manual_order: bool


ALBUM_KIND_SPECS: dict[str, AlbumKindSpec] = {
    AlbumKind.PLAIN: AlbumKindSpec(
        kind=AlbumKind.PLAIN,
        icon="photo_library",
        label="Album",
        help_text="A plain grouping of photos.",
        badge=False,
        prefers_manual_order=False,
    ),
    AlbumKind.TIMELAPSE: AlbumKindSpec(
        kind=AlbumKind.TIMELAPSE,
        icon="timelapse",
        label="Timelapse",
        help_text="Shots of the same scene from the same angle, in sequence, so they can be rendered to video later.",
        badge=True,
        prefers_manual_order=True,
    ),
}


def album_kind_spec(kind: str) -> AlbumKindSpec:
    """Return the spec for *kind*, falling back to plain for an unknown value.

    Args:
        kind: An :class:`AlbumKind` value.

    Returns:
        The matching :class:`AlbumKindSpec`.
    """
    return ALBUM_KIND_SPECS.get(kind, ALBUM_KIND_SPECS[AlbumKind.PLAIN])


class Album(abstract.PublicDashboardModel):
    """A named grouping of photos belonging to one pin or one wiki.

    Attributes:
        name: User-facing album title.
        description: Optional free-text blurb shown on the album's own page.
        kind: Plain grouping, or a special kind carrying extra behaviour
            (see :class:`AlbumKind`).
        manual_order: When True the album's items are shown in the explicit
            order users have dragged them into; when False they fall back to
            newest-photo-first, like every other gallery in the app. Ordering
            is opt-in so an album doesn't silently freeze at whatever order
            photos happened to be added in.
        parent_pin: The Pin this album belongs to, if personal.
        parent_wiki: The Wiki this album belongs to, if community.
        profile: The profile that created the album.
        cover_image: Optional explicit cover; falls back to the first item.
    """

    name = CharField(max_length=100)
    description = TextField(blank=True, default="", max_length=MAX_ALBUM_DESCRIPTION_LENGTH)
    kind = CharField(max_length=20, choices=AlbumKind.choices, default=AlbumKind.PLAIN, db_index=True)
    manual_order = BooleanField(default=False)

    parent_pin = ForeignKey("dashboard.Pin", on_delete=CASCADE, null=True, blank=True, related_name="albums")
    parent_wiki = ForeignKey("dashboard.Wiki", on_delete=CASCADE, null=True, blank=True, related_name="albums")
    profile = ForeignKey("dashboard.Profile", on_delete=CASCADE, related_name="albums")

    # SET_NULL rather than CASCADE - deleting the photo chosen as the cover
    # should drop the album back to its first-item fallback, not delete the album.
    cover_image = ForeignKey("dashboard.Image", on_delete=SET_NULL, null=True, blank=True, related_name="album_covers")

    if TYPE_CHECKING:
        parent_pin_id: int | None
        parent_wiki_id: int | None
        profile_id: int
        cover_image_id: int | None
        items: DjangoManager[AlbumItem]

    objects = AlbumManager()

    @property
    def spec(self) -> AlbumKindSpec:
        """This album's kind spec, so templates don't branch on ``kind`` by hand."""
        return album_kind_spec(self.kind)

    @property
    def photo_count(self) -> int:
        """Number of photos currently in this album.

        ``len(self.items.all())`` rather than ``self.items.count()`` so a
        caller that already ran ``prefetch_related("items")`` reuses that
        cached result instead of issuing a COUNT per album on an index render
        (same reasoning as ``PinList.pin_count``).
        """
        return len(self.items.all())

    def _slugify_base(self) -> str:
        return self.name or "album"

    def _slugify_qs(self):
        """Scope slug uniqueness to this album's own owner.

        Two different pins (or a pin and a wiki) may each have an album named
        "Interior", so uniqueness is per-owner rather than global.
        """
        qs = Album.objects.filter(parent_pin_id=self.parent_pin_id, parent_wiki_id=self.parent_wiki_id)
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        return qs

    def __str__(self) -> str:
        owner = f"pin={self.parent_pin_id}" if self.parent_pin_id else f"wiki={self.parent_wiki_id}"
        return f"{self.name} [{owner}]"

    class Meta(abstract.PublicDashboardModel.Meta):
        db_table = "dashboard_albums"
        ordering = ["name"]
        constraints = [
            UniqueConstraint(fields=["parent_pin", "slug"], name="uq_album_pin_slug"),
            UniqueConstraint(fields=["parent_wiki", "slug"], name="uq_album_wiki_slug"),
        ]
        indexes = []


class AlbumItem(abstract.DashboardModel):
    """One photo's membership in one album.

    A photo may belong to any number of albums at once - the unique constraint
    is on the (album, image) pair, not on the image alone.

    Attributes:
        album: The album the photo belongs to.
        image: The photo.
        order: Display position when the album has ``manual_order`` set.
        added_by: Who put this photo in the album. Kept for community wiki
            albums, where several people curate the same album.
    """

    album = ForeignKey(Album, on_delete=CASCADE, related_name="items")
    image = ForeignKey("dashboard.Image", on_delete=CASCADE, related_name="album_memberships")
    order = IntegerField(default=0)
    added_by = ForeignKey("dashboard.Profile", on_delete=SET_NULL, null=True, blank=True, related_name="album_items_added")

    if TYPE_CHECKING:
        album_id: int
        image_id: int
        added_by_id: int | None

    objects = AlbumItemManager()

    def __str__(self) -> str:
        return f"{self.album_id}:{self.image_id}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_album_items"
        ordering = ["order", "created"]
        constraints = [UniqueConstraint(fields=["album", "image"], name="uq_album_item")]
        indexes = []
