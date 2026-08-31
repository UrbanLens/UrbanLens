"""How the Vault's photo/document gallery is ordered.

Mirrors ``models.album.sort`` (``AlbumSort``/``AlbumSortSpec``) - a named method,
a spec carrying its ``order_by``, and a lookup that falls back to the default -
but over a plain ``Image`` queryset rather than ``AlbumItem`` membership rows,
since the Vault gallery has no per-item custom order to fall back to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db.models import TextChoices, Value
from django.db.models.functions import Coalesce, Lower

if TYPE_CHECKING:
    from django.db.models import QuerySet


class GallerySort(TextChoices):
    """Named photo-order methods the Vault gallery can use."""

    RECENT = "recent", "Recent uploads"
    OLDEST = "oldest", "Oldest uploads"
    TAKEN = "taken", "Date taken"
    NAME = "name", "Name"


@dataclass(frozen=True, slots=True)
class GallerySortSpec:
    """One sort method's SQL ordering.

    Attributes:
        sort: The :class:`GallerySort` this describes.
        label: User-facing name, for the sort picker.
        order_by: ``QuerySet.order_by`` arguments for an ``Image`` queryset.
    """

    sort: str
    label: str
    order_by: tuple

    def apply[QS: QuerySet](self, queryset: QS) -> QS:
        """Return *queryset* ordered by this method.

        Generic over the queryset's own type, matching ``AlbumSortSpec.apply``
        - a caller chaining an ``ImageQuerySet`` method after this one keeps
        that type instead of widening to the plain ``QuerySet`` base.

        Args:
            queryset: An ``Image`` queryset.

        Returns:
            The same queryset with this method's ``order_by`` applied.
        """
        return queryset.order_by(*self.order_by)


GALLERY_SORT_SPECS: dict[str, GallerySortSpec] = {
    GallerySort.RECENT: GallerySortSpec(sort=GallerySort.RECENT, label="Recent uploads", order_by=("-created", "-pk")),
    GallerySort.OLDEST: GallerySortSpec(sort=GallerySort.OLDEST, label="Oldest uploads", order_by=("created", "pk")),
    GallerySort.TAKEN: GallerySortSpec(sort=GallerySort.TAKEN, label="Date taken", order_by=(Coalesce("taken_at", "created").desc(), "-pk")),
    GallerySort.NAME: GallerySortSpec(sort=GallerySort.NAME, label="Name", order_by=(Lower(Coalesce("caption", Value(""))), "pk")),
}


def gallery_sort_spec(sort: str) -> GallerySortSpec:
    """Return the spec for *sort*, falling back to recent-uploads for an unknown value.

    Args:
        sort: A :class:`GallerySort` value.

    Returns:
        The matching :class:`GallerySortSpec`.
    """
    return GALLERY_SORT_SPECS.get(sort, GALLERY_SORT_SPECS[GallerySort.RECENT])
