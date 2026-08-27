"""QuerySets and Managers for Album and AlbumItem."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.album.model import Album
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki


class AlbumQuerySet(abstract.PublicDashboardQuerySet):
    """Custom queryset for Album models."""

    def for_pin(self, pin: Pin | int) -> AlbumQuerySet:
        """Albums belonging to one pin.

        Args:
            pin: The owning pin (accepts a Pin instance or a raw pk).

        Returns:
            Matching albums, in the model's default order.
        """
        return self.filter(parent_pin=pin)

    def for_wiki(self, wiki: Wiki | int) -> AlbumQuerySet:
        """Albums belonging to one wiki.

        Args:
            wiki: The owning wiki (accepts a Wiki instance or a raw pk).

        Returns:
            Matching albums, in the model's default order.
        """
        return self.filter(parent_wiki=wiki)


class AlbumManager(abstract.PublicDashboardManager.from_queryset(AlbumQuerySet)):
    """Custom query manager for Album models."""


class AlbumItemQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for AlbumItem models."""

    def for_album(self, album: Album | int) -> AlbumItemQuerySet:
        """Every item in one album.

        Args:
            album: The album (accepts an Album instance or a raw pk).

        Returns:
            Matching items, in the model's default (order, created) order.
        """
        return self.filter(album=album)

    def membership(self, album: Album | int, image):
        """This image's membership row in this album, if any.

        Args:
            album: The album to check.
            image: The image to check.

        Returns:
            The matching AlbumItem, or None.
        """
        return self.for_album(album).filter(image=image).first()


class AlbumItemManager(abstract.DashboardManager.from_queryset(AlbumItemQuerySet)):
    """Custom query manager for AlbumItem models."""
