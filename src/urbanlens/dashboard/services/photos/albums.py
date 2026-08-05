"""Album membership and ordering for a pin's or wiki's photos.

Albums group photos that already belong to a place; they never widen who can
see a photo. Every listing helper here chains ``ImageQuerySet.visible_to`` so
an album can't surface a photo the viewer wouldn't otherwise be shown, and
:func:`eligible_images_for` is the single definition of "which photos may go
in this album" for both the add-photo picker and the add endpoint's own
validation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.album.model import Album, AlbumItem
from urbanlens.dashboard.models.pin.model import Pin

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki


def owner_kwargs(owner: Pin | Wiki) -> dict:
    """Return the Album FK kwargs (``parent_pin``/``parent_wiki``) for *owner*.

    Args:
        owner: The Pin or Wiki that owns the album.

    Returns:
        A dict suitable for splatting into ``Album.objects.create``/``filter``.
    """
    return {"parent_pin": owner} if isinstance(owner, Pin) else {"parent_wiki": owner}


def albums_for_owner(owner: Pin | Wiki) -> QuerySet[Album]:
    """Every album belonging to *owner*, with items prefetched for counts/covers.

    Args:
        owner: The Pin or Wiki whose albums to list.

    Returns:
        The owner's albums, in the model's default (name) order.
    """
    return Album.objects.filter(**owner_kwargs(owner)).select_related("cover_image").prefetch_related("items__image")


def eligible_images_for(owner: Pin | Wiki, viewer: Profile | None) -> QuerySet[Image]:
    """Photos that may be placed in one of *owner*'s albums.

    An album is strictly scoped to its owner: a pin album may only hold that
    pin's photos, and a wiki album only that wiki's. That keeps a private pin
    photo from being pulled onto a shared community surface just by adding it
    to an album there.

    Args:
        owner: The Pin or Wiki that owns the album.
        viewer: The profile browsing, for the standard photo-visibility gate.

    Returns:
        Matching, viewer-visible photos, newest first.
    """
    from urbanlens.dashboard.models.images.model import Image

    scope = {"pin": owner} if isinstance(owner, Pin) else {"wiki": owner}
    return Image.objects.filter(**scope).visible_to(viewer).order_by("-created")


def album_images(album: Album, viewer: Profile | None) -> list[Image]:
    """The photos in *album*, in the album's effective display order.

    Honours ``Album.manual_order``: when set, items follow the explicit order
    users dragged them into; otherwise they fall back to newest-first, the
    same default every other gallery in the app uses.

    Args:
        album: The album to read.
        viewer: The profile browsing, for the standard photo-visibility gate.

    Returns:
        The album's viewer-visible photos, ordered for display. Each carries
        an ``album_item_id`` attribute so templates can address the membership
        row (for removal/reordering) without a second lookup.
    """
    from urbanlens.dashboard.models.images.model import Image

    items = list(AlbumItem.objects.for_album(album).select_related("image"))
    visible_ids = set(Image.objects.filter(pk__in=[item.image_id for item in items]).visible_to(viewer).values_list("pk", flat=True))

    visible_items = [item for item in items if item.image_id in visible_ids]
    if not album.manual_order:
        visible_items.sort(key=lambda item: item.image.created, reverse=True)

    images = []
    for item in visible_items:
        image = item.image
        image.album_item_id = item.pk
        images.append(image)
    return images


def loose_images_for(owner: Pin | Wiki, viewer: Profile | None) -> QuerySet[Image]:
    """*owner*'s photos that aren't in any of its albums yet.

    Args:
        owner: The Pin or Wiki whose photos to list.
        viewer: The profile browsing, for the standard photo-visibility gate.

    Returns:
        Matching photos not referenced by any of this owner's albums, newest first.
    """
    album_ids = Album.objects.filter(**owner_kwargs(owner)).values_list("pk", flat=True)
    filed_image_ids = AlbumItem.objects.filter(album_id__in=album_ids).values_list("image_id", flat=True)
    return eligible_images_for(owner, viewer).exclude(pk__in=filed_image_ids)


def add_images_to_album(album: Album, images: Sequence[Image], added_by: Profile | None) -> int:
    """Add photos to *album*, skipping any already in it.

    New items are appended after the album's current last item, so adding
    never reshuffles an album a user has already ordered by hand.

    Args:
        album: The album to add to.
        images: The photos to add.
        added_by: The profile performing the add, recorded per item so
            community wiki albums keep per-photo attribution.

    Returns:
        How many photos were actually added.
    """
    existing_ids = set(AlbumItem.objects.for_album(album).values_list("image_id", flat=True))
    to_add = [image for image in images if image.pk not in existing_ids]
    if not to_add:
        return 0

    next_order = (AlbumItem.objects.for_album(album).order_by("-order").values_list("order", flat=True).first() or 0) + 1
    AlbumItem.objects.bulk_create(
        [AlbumItem(album=album, image=image, added_by=added_by, order=next_order + offset) for offset, image in enumerate(to_add)]
    )
    return len(to_add)


def remove_images_from_album(album: Album, image_ids: Sequence[int]) -> int:
    """Remove photos from *album*. Photos not in it are ignored.

    Only the membership rows are deleted - the photos themselves survive and
    fall back to the loose-photos section.

    Args:
        album: The album to remove from.
        image_ids: Primary keys of the photos to remove.

    Returns:
        How many membership rows were deleted.
    """
    deleted, _ = AlbumItem.objects.for_album(album).filter(image_id__in=list(image_ids)).delete()
    if album.cover_image_id is not None and album.cover_image_id in set(image_ids):
        Album.objects.filter(pk=album.pk).update(cover_image=None)
    return deleted


def reorder_album_items(album: Album, item_ids: Sequence[int]) -> int:
    """Renumber *album*'s items to follow the order given in *item_ids*.

    Each item's ``order`` becomes its index in *item_ids*. Ids that don't
    belong to this album are ignored rather than rejected, matching
    ``services.pins.pin_list_membership.reorder_list_items``: a drag-and-drop
    UI can submit a stale id for an item removed in another tab, and failing
    the whole reorder over that would be worse than skipping it. Ignored ids
    still consume their index, so the result is sparse but correctly ordered.

    Args:
        album: The album whose items are being reordered.
        item_ids: ``AlbumItem`` primary keys in their new display order.

    Returns:
        How many items were actually renumbered.
    """
    ids = list(item_ids)
    items_by_id = {item.pk: item for item in AlbumItem.objects.for_album(album).filter(pk__in=ids)}

    updated: list[AlbumItem] = []
    for order, item_id in enumerate(ids):
        item = items_by_id.get(item_id)
        if item is None:
            continue
        item.order = order
        updated.append(item)
    if updated:
        AlbumItem.objects.bulk_update(updated, ["order"])
    return len(updated)


def album_cover(album: Album, viewer: Profile | None) -> Image | None:
    """The photo to show as *album*'s cover.

    Prefers the explicitly chosen ``cover_image``, but only when the viewer is
    allowed to see it - otherwise (and when none is set) falls back to the
    first photo in the album's own display order.

    Args:
        album: The album to pick a cover for.
        viewer: The profile browsing, for the standard photo-visibility gate.

    Returns:
        The cover photo, or None for an empty album.
    """
    images = album_images(album, viewer)
    if album.cover_image_id is not None:
        for image in images:
            if image.pk == album.cover_image_id:
                return image
    return images[0] if images else None
