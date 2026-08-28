"""Album membership and ordering for a pin's or wiki's photos.

Albums group photos that already belong to a place; they never widen who can
see a photo. Every listing helper here chains ``ImageQuerySet.visible_to`` so
an album can't surface a photo the viewer wouldn't otherwise be shown, and
:func:`eligible_images_for` is the single definition of "which photos may go
in this album" for both the add-photo picker and the add endpoint's own
validation.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.album.model import Album, AlbumItem
from urbanlens.dashboard.models.pin.model import Pin

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from datetime import datetime

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki


#: How many photo tiles a grid page carries. Sized so a typical desktop
#: viewport plus a small scroll buffer is one request, without dumping a
#: thousand full ``Image`` rows into the first HTML response.
ALBUM_GRID_PAGE_SIZE = 48


@dataclass(frozen=True, slots=True)
class AlbumListEntry:
    """One album on the Photos tab, without hydrating every member photo.

    The list only needs a cover, a count, and a date range. Loading every
    ``Image`` row (and its file field) just to derive those is what made the
    tab expensive on a pin with a large library.
    """

    album: Album
    photo_count: int
    cover: Image | None
    date_start: datetime | None
    date_end: datetime | None


def owner_kwargs(owner: Pin | Wiki) -> dict:
    """Return the Album FK kwargs (``parent_pin``/``parent_wiki``) for *owner*.

    Args:
        owner: The Pin or Wiki that owns the album.

    Returns:
        A dict suitable for splatting into ``Album.objects.create``/``filter``.
    """
    return {"parent_pin": owner} if isinstance(owner, Pin) else {"parent_wiki": owner}


def album_owner(album: Album) -> Pin | Wiki:
    """Return whichever parent owns *album*.

    Args:
        album: The album to resolve.

    Returns:
        The owning Pin or Wiki.

    Raises:
        ValueError: The album has neither parent set, which the create paths
            never produce.
    """
    owner = album.parent_pin or album.parent_wiki
    if owner is None:
        raise ValueError(f"Album {album.pk} has no parent pin or wiki.")
    return owner


def albums_for_owner(owner: Pin | Wiki) -> QuerySet[Album]:
    """Every album belonging to *owner*.

    Args:
        owner: The Pin or Wiki whose albums to list.

    Returns:
        The owner's albums, in the model's default (name) order.
    """
    return Album.objects.filter(**owner_kwargs(owner)).select_related("cover_image")


def _owner_conceal(owner: Pin | Wiki, viewer: Profile | None) -> bool:
    """Whether *viewer* sees the concealed form of *owner*'s wiki.

    Always False for a Pin - concealment is a wiki-only concept.

    Args:
        owner: The Pin or Wiki whose albums/photos are being resolved.
        viewer: The browsing profile, or None for anonymous.

    Returns:
        Whether album/photo visibility here must also apply concealment.
    """
    if isinstance(owner, Pin):
        return False
    from urbanlens.dashboard.services.wiki.concealment import concealment_active

    return concealment_active(owner, viewer)


def _visible_image_ids(image_ids: Collection[int], viewer: Profile | None, *, conceal: bool = False) -> set[int]:
    """Which of *image_ids* this viewer may see.

    Resolved in one query for the whole set - ``visible_to`` computes the
    viewer's allowed-uploader set on every call, so running it per album would
    repeat that work once per album on the Photos tab.

    Args:
        image_ids: Candidate image primary keys.
        viewer: The browsing profile, or None for anonymous.
        conceal: Whether to additionally narrow to what a concealed viewer of
            the owning wiki may see (own/friends' uploads, provider photos) -
            the Photos tab used to bypass this and hand back the gallery's
            full upload set through the album path.

    Returns:
        The subset the viewer is allowed to see.
    """
    from urbanlens.dashboard.models.images.model import Image

    if not image_ids:
        return set()
    qs = Image.objects.filter(pk__in=image_ids).visible_to(viewer)
    if conceal:
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows

        qs = conceal_rows(qs, viewer)
    return set(qs.values_list("pk", flat=True))


def _order_for_display(album: Album, images: list[Image]) -> list[Image]:
    """Apply *album*'s effective ordering to an already-item-ordered list.

    Args:
        album: The album the images belong to.
        images: Its images, in ``AlbumItem`` (order, created) sequence.

    Returns:
        The same images, newest-first unless the album is manually ordered.
    """
    if album.manual_order:
        return images
    return sorted(images, key=lambda image: image.created, reverse=True)


def albums_with_images(owner: Pin | Wiki, viewer: Profile | None) -> list[tuple[Album, list[Image]]]:
    """Every album of *owner* paired with its viewer-visible photos.

    Costs a fixed three queries (albums, memberships, visibility) no matter
    how many albums there are - the Photos tab renders a cover and a count for
    each one, and resolving those per album is an N+1.

    Args:
        owner: The Pin or Wiki whose albums to list.
        viewer: The browsing profile, for the photo-visibility gate.

    Returns:
        ``(album, images)`` pairs in album order, each image carrying an
        ``album_item_id`` attribute for the membership row.
    """
    conceal = _owner_conceal(owner, viewer)
    albums_qs = albums_for_owner(owner)
    if conceal:
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows

        albums_qs = conceal_rows(albums_qs, viewer)
    albums = list(albums_qs)
    if not albums:
        return []

    items = list(AlbumItem.objects.filter(album_id__in=[album.pk for album in albums]).select_related("image").order_by("order", "created"))
    visible_ids = _visible_image_ids({item.image_id for item in items}, viewer, conceal=conceal)

    by_album: dict[int, list[Image]] = defaultdict(list)
    for item in items:
        if item.image_id not in visible_ids:
            continue
        image = item.image
        image.album_item_id = item.pk
        by_album[item.album_id].append(image)

    return [(album, _order_for_display(album, by_album.get(album.pk, []))) for album in albums]


def albums_listing(owner: Pin | Wiki, viewer: Profile | None) -> list[AlbumListEntry]:
    """Every album of *owner* with cover, count, and date range.

    Same visibility rules as :func:`albums_with_images`, but the only
    ``Image`` rows loaded are the covers. Timestamps for the date range and
    newest-first ordering come from a values query, not hydrated models.

    Args:
        owner: The Pin or Wiki whose albums to list.
        viewer: The browsing profile, for the photo-visibility gate.

    Returns:
        One :class:`AlbumListEntry` per album, in album order.
    """
    from urbanlens.dashboard.models.images.model import Image

    conceal = _owner_conceal(owner, viewer)
    albums_qs = albums_for_owner(owner)
    if conceal:
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows

        albums_qs = conceal_rows(albums_qs, viewer)
    albums = list(albums_qs)
    if not albums:
        return []

    items = list(
        AlbumItem.objects.filter(album_id__in=[album.pk for album in albums])
        .order_by("order", "created")
        .values_list("album_id", "image_id")
    )
    visible_ids = _visible_image_ids({image_id for _album_id, image_id in items}, viewer, conceal=conceal)
    by_album: dict[int, list[int]] = defaultdict(list)
    for album_id, image_id in items:
        if image_id in visible_ids:
            by_album[album_id].append(image_id)

    meta = {
        pk: (taken_at, created)
        for pk, taken_at, created in Image.objects.filter(pk__in=visible_ids).values_list("pk", "taken_at", "created")
    }

    cover_ids: list[int] = []
    prepared: list[tuple[Album, int, int | None, datetime | None, datetime | None]] = []
    for album in albums:
        ids = by_album.get(album.pk, [])
        if not album.manual_order:
            ids = [image_id for image_id in ids if image_id in meta]
            ids = sorted(ids, key=lambda image_id: meta[image_id][1], reverse=True)
        stamps = [meta[image_id][0] or meta[image_id][1] for image_id in ids if image_id in meta]
        date_start, date_end = (min(stamps), max(stamps)) if stamps else (None, None)
        cover_id = album.cover_image_id if album.cover_image_id in set(ids) else (ids[0] if ids else None)
        if cover_id is not None:
            cover_ids.append(cover_id)
        prepared.append((album, len(ids), cover_id, date_start, date_end))

    covers = {image.pk: image for image in Image.objects.filter(pk__in=cover_ids)} if cover_ids else {}
    return [
        AlbumListEntry(album=album, photo_count=count, cover=covers.get(cover_id) if cover_id else None, date_start=date_start, date_end=date_end)
        for album, count, cover_id, date_start, date_end in prepared
    ]


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
    qs = Image.objects.filter(**scope).visible_to(viewer).order_by("-created")
    if _owner_conceal(owner, viewer):
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows

        qs = conceal_rows(qs, viewer)
    return qs


def album_images(album: Album, viewer: Profile | None, owner: Pin | Wiki | None = None) -> list[Image]:
    """The photos in *album*, in the album's effective display order.

    Honours ``Album.manual_order``: when set, items follow the explicit order
    users dragged them into; otherwise they fall back to newest-first, the
    same default every other gallery in the app uses.

    Args:
        album: The album to read.
        viewer: The profile browsing, for the standard photo-visibility gate.
        owner: The album's owner, if the caller already resolved it (every
            controller call site does, via ``_resolve_album_owner``) - saves
            re-deriving it through ``album.parent_pin``/``parent_wiki``,
            which isn't select_related on any queryset this is called from.

    Returns:
        The album's viewer-visible photos, ordered for display. Each carries
        an ``album_item_id`` attribute so templates can address the membership
        row (for removal/reordering) without a second lookup.
    """
    pairs = visible_album_item_pairs(album, viewer, owner)
    return _hydrate_album_items(pairs)


def visible_album_item_pairs(
    album: Album,
    viewer: Profile | None,
    owner: Pin | Wiki | None = None,
) -> list[tuple[int, int]]:
    """``(item_id, image_id)`` pairs the viewer may see, in display order."""
    resolved_owner = owner if owner is not None else album_owner(album)
    conceal = _owner_conceal(resolved_owner, viewer)
    items_qs = AlbumItem.objects.for_album(album)
    if album.manual_order:
        items_qs = items_qs.order_by("order", "created")
    else:
        items_qs = items_qs.order_by("-image__created")

    pairs = list(items_qs.values_list("pk", "image_id"))
    visible_ids = _visible_image_ids({image_id for _item_id, image_id in pairs}, viewer, conceal=conceal)
    return [(item_id, image_id) for item_id, image_id in pairs if image_id in visible_ids]


def _hydrate_album_items(pairs: Sequence[tuple[int, int]]) -> list[Image]:
    """Load ``Image`` rows for *pairs*, attaching ``album_item_id`` in that order."""
    if not pairs:
        return []
    page_item_ids = [item_id for item_id, _image_id in pairs]
    items_by_pk = {
        item.pk: item
        for item in AlbumItem.objects.filter(pk__in=page_item_ids).select_related("image")
    }
    images = []
    for item_id, _image_id in pairs:
        item = items_by_pk[item_id]
        image = item.image
        image.album_item_id = item.pk
        images.append(image)
    return images


def album_images_page(
    album: Album,
    viewer: Profile | None,
    owner: Pin | Wiki | None = None,
    *,
    offset: int = 0,
    limit: int = ALBUM_GRID_PAGE_SIZE,
) -> tuple[list[Image], int]:
    """One page of *album*'s photos, plus the un-paged total.

    Resolves visibility against the full membership list (so a page isn't
    padded with photos the viewer can't see), then instantiates only the
    ``Image`` rows on this page - album grids used to dump every file URL
    into the first HTML response.

    Args:
        album: The album to read.
        viewer: The profile browsing, for the photo-visibility gate.
        owner: The album's owner, if already resolved.
        offset: How many visible photos to skip.
        limit: Maximum photos to return.

    Returns:
        ``(page, total)`` where *page* items each carry ``album_item_id``.
    """
    pairs = visible_album_item_pairs(album, viewer, owner)
    return _hydrate_album_items(pairs[offset : offset + limit]), len(pairs)


def album_date_range_for_ids(image_ids: Collection[int]) -> tuple[datetime | None, datetime | None]:
    """Earliest and latest capture date across *image_ids*, without hydrating rows.

    Args:
        image_ids: Primary keys of the album's viewer-visible photos.

    Returns:
        ``(first, last)``, or ``(None, None)`` when *image_ids* is empty.
    """
    from urbanlens.dashboard.models.images.model import Image

    if not image_ids:
        return None, None
    stamps = [taken_at or created for taken_at, created in Image.objects.filter(pk__in=list(image_ids)).values_list("taken_at", "created")]
    if not stamps:
        return None, None
    return min(stamps), max(stamps)


def cover_from_ids(album: Album, visible_ids: Sequence[int]) -> Image | None:
    """Pick *album*'s cover from already-resolved visible image ids.

    Args:
        album: The album to pick a cover for.
        visible_ids: Viewer-visible image primary keys, in display order.

    Returns:
        The cover photo, or None for an empty album.
    """
    from urbanlens.dashboard.models.images.model import Image

    if not visible_ids:
        return None
    wanted = album.cover_image_id if album.cover_image_id in set(visible_ids) else visible_ids[0]
    return Image.objects.filter(pk=wanted).first()


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
    # The read above and the insert below are not atomic, and there are two callers -
    # one of them the Celery task cache_media_item_into_album, which Celery may deliver
    # more than once. Without ignore_conflicts the loser of that race hits uq_album_item
    # and raises, turning a duplicate add into a 500 instead of a no-op.
    before = AlbumItem.objects.filter(album=album).count()
    AlbumItem.objects.bulk_create(
        [AlbumItem(album=album, image=image, added_by=added_by, order=next_order + offset) for offset, image in enumerate(to_add)],
        ignore_conflicts=True,
    )
    # Counted rather than assumed: ignore_conflicts silently drops the rows another
    # process got in first, so len(to_add) would over-report what this call did.
    return AlbumItem.objects.filter(album=album).count() - before


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


def album_date_range(images: Sequence[Image]) -> tuple[datetime | None, datetime | None]:
    """Earliest and latest capture date across *images*.

    Uses ``Image.taken_at`` (the EXIF capture time) and falls back to
    ``created`` for photos that carry no EXIF date - a scan or a screenshot
    still belongs somewhere on the album's timeline, and dropping it would
    make the range silently narrower than the album really is.

    Takes the already-resolved list rather than aggregating in SQL so the
    Photos tab keeps its fixed query count no matter how many albums it shows
    (see :func:`albums_with_images`).

    Args:
        images: The album's viewer-visible photos, in any order.

    Returns:
        ``(first, last)``, or ``(None, None)`` for an empty album.
    """
    stamps = [image.taken_at or image.created for image in images]
    if not stamps:
        return None, None
    return min(stamps), max(stamps)


def cover_from_images(album: Album, images: list[Image]) -> Image | None:
    """Pick *album*'s cover out of an already-resolved image list.

    Prefers the explicitly chosen ``cover_image``, but only when it's actually
    among the photos this viewer can see - otherwise (and when none is set)
    falls back to the first photo in display order. Takes the list rather than
    re-querying so batched callers don't pay per album.

    Args:
        album: The album to pick a cover for.
        images: Its viewer-visible photos, in display order.

    Returns:
        The cover photo, or None for an empty album.
    """
    if album.cover_image_id is not None:
        for image in images:
            if image.pk == album.cover_image_id:
                return image
    return images[0] if images else None


def album_cover(album: Album, viewer: Profile | None) -> Image | None:
    """The photo to show as *album*'s cover.

    Args:
        album: The album to pick a cover for.
        viewer: The profile browsing, for the standard photo-visibility gate.

    Returns:
        The cover photo, or None for an empty album.
    """
    return cover_from_images(album, album_images(album, viewer))
