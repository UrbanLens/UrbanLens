"""QuerySets and Managers for Album and AlbumItem."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connections

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.album.model import Album
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
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

    def for_profile(self, profile: Profile | int) -> AlbumQuerySet:
        """Vault albums belonging to one profile directly (not via a pin or wiki).

        Args:
            profile: The owning profile (accepts a Profile instance or a raw pk).

        Returns:
            Matching albums, in the model's default order.
        """
        return self.filter(parent_profile=profile)


class AlbumManager(abstract.PublicDashboardManager.from_queryset(AlbumQuerySet)):
    """Custom query manager for Album models."""


class AlbumItemQuerySet(abstract.DashboardQuerySet):
    """Custom queryset for AlbumItem models."""

    def for_album(self, album: Album | int) -> AlbumItemQuerySet:
        """Every item in one album.

        Args:
            album: The album (accepts an Album instance or a raw pk).

        Returns:
            Matching items. Display order is :meth:`in_display_order`, not this.
        """
        return self.filter(album=album)

    def in_display_order(self, album: Album) -> AlbumItemQuerySet:
        """This album's items in its current sort method. Date and name sorts join ``image`` and read live metadata.

        Args:
            album: The album whose ``sort`` to apply.

        Returns:
            Matching items, ordered for display.
        """
        from urbanlens.dashboard.models.album.sort import album_sort_spec

        return album_sort_spec(album.sort).apply(self.for_album(album))

    def number_in_order(self, album: Album | int, item_ids: Sequence[int]) -> int:
        """Set each of *item_ids*' ``order`` to its index in that sequence, in one statement.

        Joins against the ids as an array rather than a ``CASE`` branch per id, which Postgres evaluates per row and so
        grows with the square of the album's size.

        Args:
            album: The album the items must belong to; ids from any other album are left alone.
            item_ids: ``AlbumItem`` primary keys in their new order.

        Returns:
            How many rows were numbered.
        """
        with connections[self.db].cursor() as cursor:
            cursor.execute(
                'UPDATE dashboard_album_items AS item SET "order" = positions.position - 1 FROM unnest(%s::bigint[]) WITH ORDINALITY AS positions(id, position) WHERE item.id = positions.id AND item.album_id = %s',
                [list(item_ids), album if isinstance(album, int) else album.pk],
            )
            return cursor.rowcount

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
