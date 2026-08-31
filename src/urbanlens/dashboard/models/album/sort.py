"""How an album's photos are ordered.

``Album.sort`` names the method; adding a method is a one-place change here.
Date and name sorts are live queries over photo metadata, so a later upload or
a caption/EXIF edit moves that photo without rewriting anyone else's
``AlbumItem.order``. Custom order is the exception: ``order`` stays null until
the user drags, at which point every current item is numbered and later
uploads (still null) sort after the numbered ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db.models import F, TextChoices, Value
from django.db.models.functions import Coalesce, Lower

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.album.model import AlbumItem


class AlbumSort(TextChoices):
    """Named photo-order methods an album can use."""

    UPLOADED = "uploaded", "Date uploaded"
    TAKEN = "taken", "Date taken"
    NAME = "name", "Name"
    CUSTOM = "custom", "Custom"


@dataclass(frozen=True, slots=True)
class AlbumSortSpec:
    """One sort method's SQL and in-memory key.

    Attributes:
        sort: The :class:`AlbumSort` this describes.
        label: User-facing name, for a future sort picker.
        order_by: ``QuerySet.order_by`` arguments for an ``AlbumItem``
            queryset. Date/name methods join ``image``; custom does not.
    """

    sort: str
    label: str
    order_by: tuple

    def apply[QS: QuerySet](self, queryset: QS) -> QS:
        """Return *queryset* ordered by this method.

        Generic over the queryset's own type (rather than the plain
        ``QuerySet`` base) so a caller chaining a custom queryset's own
        methods after this one - e.g. ``AlbumItemQuerySet.in_display_order``
        - doesn't lose that type.

        Args:
            queryset: ``AlbumItem`` rows, typically already scoped to one album.

        Returns:
            The same queryset with this method's ``order_by`` applied.
        """
        return queryset.order_by(*self.order_by)

    def sorted_items(self, items: Sequence[AlbumItem]) -> list[AlbumItem]:
        """Sort already-loaded membership rows the same way SQL would.

        Used when several albums are listed in one query and each album may
        use a different method.

        Args:
            items: Membership rows with ``image`` loaded for date/name sorts.

        Returns:
            A new list in display order.
        """
        return sorted(items, key=self._item_key)

    def _item_key(self, item: AlbumItem) -> tuple:
        image = item.image
        if self.sort == AlbumSort.CUSTOM:
            numbered = item.order is not None
            return (not numbered, item.order if numbered else 0, item.created, item.pk)
        if self.sort == AlbumSort.NAME:
            return ((image.caption or "").lower(), image.pk)
        if self.sort == AlbumSort.TAKEN:
            stamp = image.taken_at or image.created
            return (-stamp.timestamp(), -image.pk)
        return (-image.created.timestamp(), -image.pk)


ALBUM_SORT_SPECS: dict[str, AlbumSortSpec] = {
    AlbumSort.UPLOADED: AlbumSortSpec(
        sort=AlbumSort.UPLOADED,
        label="Date uploaded",
        order_by=("-image__created", "-image__pk"),
    ),
    AlbumSort.TAKEN: AlbumSortSpec(
        sort=AlbumSort.TAKEN,
        label="Date taken",
        order_by=(Coalesce("image__taken_at", "image__created").desc(), "-image__pk"),
    ),
    AlbumSort.NAME: AlbumSortSpec(
        sort=AlbumSort.NAME,
        label="Name",
        order_by=(Lower(Coalesce("image__caption", Value(""))), "image__pk"),
    ),
    AlbumSort.CUSTOM: AlbumSortSpec(
        sort=AlbumSort.CUSTOM,
        label="Custom",
        order_by=(F("order").asc(nulls_last=True), "created", "pk"),
    ),
}


def album_sort_spec(sort: str) -> AlbumSortSpec:
    """Return the spec for *sort*, falling back to uploaded for an unknown value.

    Args:
        sort: An :class:`AlbumSort` value.

    Returns:
        The matching :class:`AlbumSortSpec`.
    """
    return ALBUM_SORT_SPECS.get(sort, ALBUM_SORT_SPECS[AlbumSort.UPLOADED])
