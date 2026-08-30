"""Album membership and ordering for a pin's, wiki's, or Vault's photos.

Albums group photos that already belong to their owner - a place (pin/wiki)
or a profile's own Vault; they never widen who can see a photo. Every listing
helper here chains ``ImageQuerySet.visible_to`` so an album can't surface a
photo the viewer wouldn't otherwise be shown, and :func:`eligible_images_for`
is the single definition of "which photos may go in this album" for both the
add-photo picker and the add endpoint's own validation.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db.models import Q

from urbanlens.dashboard.models.album.model import Album, AlbumItem
from urbanlens.dashboard.models.album.sort import AlbumSort
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from datetime import datetime

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.images.model import Image


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


def owner_kwargs(owner: Pin | Wiki | Profile) -> dict:
    """Return the Album FK kwargs (``parent_pin``/``parent_wiki``/``parent_profile``) for *owner*.

    Args:
        owner: The Pin, Wiki, or Profile (Vault) that owns the album.

    Returns:
        A dict suitable for splatting into ``Album.objects.create``/``filter``.
    """
    if isinstance(owner, Pin):
        return {"parent_pin": owner}
    if isinstance(owner, Profile):
        return {"parent_profile": owner}
    return {"parent_wiki": owner}


def owner_kwargs_to_image_scope(owner: Pin | Wiki | Profile) -> dict:
    """Return the ``Image`` filter kwargs (``pin``/``wiki``/``profile``) scoping *owner*'s photos.

    Distinct from :func:`owner_kwargs` (which names the ``Album`` FK, not the
    ``Image`` one) - a vault owner's photos are its uploads (``Image.profile``),
    not photos filed to a place.

    Args:
        owner: The Pin, Wiki, or Profile (Vault) whose photos to scope to.

    Returns:
        A dict suitable for splatting into an ``Image`` queryset filter.
    """
    if isinstance(owner, Pin):
        return {"pin": owner}
    if isinstance(owner, Profile):
        return {"profile": owner}
    return {"wiki": owner}


def album_owner(album: Album) -> Pin | Wiki | Profile:
    """Return whichever parent owns *album*.

    Args:
        album: The album to resolve.

    Returns:
        The owning Pin, Wiki, or Profile.

    Raises:
        ValueError: The album has no parent set, which the create paths
            never produce.
    """
    owner = album.parent_pin or album.parent_wiki or album.parent_profile
    if owner is None:
        raise ValueError(f"Album {album.pk} has no parent pin, wiki, or profile.")
    return owner


def albums_for_owner(owner: Pin | Wiki | Profile) -> QuerySet[Album]:
    """Every album belonging to *owner*.

    Args:
        owner: The Pin, Wiki, or Profile whose albums to list.

    Returns:
        The owner's albums, in the model's default (name) order.
    """
    return albums_for_owners([owner])


def albums_for_owners(owners: Sequence[Pin | Wiki | Profile]) -> QuerySet[Album]:
    """Every album belonging to any of *owners*.

    Args:
        owners: Pins, wikis, and/or profiles whose albums to list.

    Returns:
        Those albums, with ``parent_pin`` selected for child-pin labels.
    """
    if not owners:
        return Album.objects.none()
    query = Q()
    for owner in owners:
        query |= Q(**owner_kwargs(owner))
    return Album.objects.filter(query).select_related("cover_image", "parent_pin")


def _owner_conceal(owner: Pin | Wiki | Profile, viewer: Profile | None) -> bool:
    """Whether *viewer* sees the concealed form of *owner*'s wiki.

    Always False for a Pin or a Vault (Profile-owned) album - concealment is a
    wiki-only concept.

    Args:
        owner: The Pin, Wiki, or Profile whose albums/photos are being resolved.
        viewer: The browsing profile, or None for anonymous.

    Returns:
        Whether album/photo visibility here must also apply concealment.
    """
    if isinstance(owner, (Pin, Profile)):
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


def albums_with_images(owner: Pin | Wiki | Profile, viewer: Profile | None) -> list[tuple[Album, list[Image]]]:
    """Every album of *owner* paired with its viewer-visible photos.

    Costs a fixed three queries (albums, memberships, visibility) no matter
    how many albums there are - the Photos tab renders a cover and a count for
    each one, and resolving those per album is an N+1.

    Args:
        owner: The Pin, Wiki, or Profile whose albums to list.
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

    items = list(AlbumItem.objects.filter(album_id__in=[album.pk for album in albums]).select_related("image"))
    visible_ids = _visible_image_ids({item.image_id for item in items}, viewer, conceal=conceal)

    by_album: dict[int, list[AlbumItem]] = defaultdict(list)
    for item in items:
        if item.image_id in visible_ids:
            by_album[item.album_id].append(item)

    result: list[tuple[Album, list[Image]]] = []
    for album in albums:
        images = []
        for item in album.sort_spec.sorted_items(by_album.get(album.pk, [])):
            image = item.image
            image.album_item_id = item.pk
            images.append(image)
        result.append((album, images))
    return result


def albums_listing(owner: Pin | Wiki | Profile | Sequence[Pin | Wiki | Profile], viewer: Profile | None) -> list[AlbumListEntry]:
    """Every album of *owner* with cover, count, and date range.

    Same visibility rules as :func:`albums_with_images`. Membership rows are
    loaded so each album can be sorted by its own method without an N+1.
    Pass a sequence of pins to include child-pin albums on a parent Photos tab.

    Args:
        owner: The Pin, Wiki, or Profile whose albums to list, or several of them.
        viewer: The browsing profile, for the photo-visibility gate.

    Returns:
        One :class:`AlbumListEntry` per album, in album order.
    """
    from urbanlens.dashboard.models.images.model import Image

    owners: list[Pin | Wiki | Profile] = [owner] if isinstance(owner, (Pin, Wiki, Profile)) else list(owner)
    conceal = _owner_conceal(owners[0], viewer) if len(owners) == 1 else False
    albums_qs = albums_for_owners(owners)
    if conceal:
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows

        albums_qs = conceal_rows(albums_qs, viewer)
    albums = list(albums_qs)
    if not albums:
        return []

    items = list(AlbumItem.objects.filter(album_id__in=[album.pk for album in albums]).select_related("image"))
    visible_ids = _visible_image_ids({item.image_id for item in items}, viewer, conceal=conceal)
    by_album: dict[int, list[AlbumItem]] = defaultdict(list)
    for item in items:
        if item.image_id in visible_ids:
            by_album[item.album_id].append(item)

    cover_ids: list[int] = []
    prepared: list[tuple[Album, int, int | None, datetime | None, datetime | None]] = []
    for album in albums:
        ordered = album.sort_spec.sorted_items(by_album.get(album.pk, []))
        ids = [item.image_id for item in ordered]
        stamps = [item.image.taken_at or item.image.created for item in ordered]
        date_start, date_end = (min(stamps), max(stamps)) if stamps else (None, None)
        cover_id = album.cover_image_id if album.cover_image_id in set(ids) else (ids[0] if ids else None)
        if cover_id is not None:
            cover_ids.append(cover_id)
        prepared.append((album, len(ids), cover_id, date_start, date_end))

    covers = {image.pk: image for image in Image.objects.filter(pk__in=cover_ids)} if cover_ids else {}
    return [AlbumListEntry(album=album, photo_count=count, cover=covers.get(cover_id) if cover_id else None, date_start=date_start, date_end=date_end) for album, count, cover_id, date_start, date_end in prepared]


def eligible_images_for(owner: Pin | Wiki | Profile, viewer: Profile | None) -> QuerySet[Image]:
    """Photos that may be placed in one of *owner*'s albums.

    An album is strictly scoped to its owner: a pin album may only hold that
    pin's photos, a wiki album only that wiki's, and a vault album only its
    owning profile's own uploads. That keeps a private pin photo from being
    pulled onto a shared community surface just by adding it to an album there.

    Args:
        owner: The Pin, Wiki, or Profile (Vault) that owns the album.
        viewer: The profile browsing, for the standard photo-visibility gate.

    Returns:
        Matching, viewer-visible photos, newest first.
    """
    from urbanlens.dashboard.models.images.model import Image

    qs = Image.objects.filter(**owner_kwargs_to_image_scope(owner)).visible_to(viewer).order_by("-created")
    if _owner_conceal(owner, viewer):
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows

        qs = conceal_rows(qs, viewer)
    return qs


def album_images(album: Album, viewer: Profile | None, owner: Pin | Wiki | Profile | None = None) -> list[Image]:
    """The photos in *album*, in the album's current sort.

    Date and name sorts read live photo metadata. Custom order is only
    written when the user drags; photos added after that have null ``order``
    and appear at the end.

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
    owner: Pin | Wiki | Profile | None = None,
) -> list[tuple[int, int]]:
    """``(item_id, image_id)`` pairs the viewer may see, in display order."""
    resolved_owner = owner if owner is not None else album_owner(album)
    conceal = _owner_conceal(resolved_owner, viewer)
    items_qs = AlbumItem.objects.in_display_order(album)

    pairs = list(items_qs.values_list("pk", "image_id"))
    visible_ids = _visible_image_ids({image_id for _item_id, image_id in pairs}, viewer, conceal=conceal)
    return [(item_id, image_id) for item_id, image_id in pairs if image_id in visible_ids]


def _hydrate_album_items(pairs: Sequence[tuple[int, int]]) -> list[Image]:
    """Load ``Image`` rows for *pairs*, attaching ``album_item_id`` in that order."""
    if not pairs:
        return []
    page_item_ids = [item_id for item_id, _image_id in pairs]
    items_by_pk = {item.pk: item for item in AlbumItem.objects.filter(pk__in=page_item_ids).select_related("image")}
    images = []
    for item_id, _image_id in pairs:
        # *pairs* came from an earlier query, so a membership row removed in
        # between (another tab, the optimistic remove on the grid) is simply
        # gone now. Skipping it renders the album a photo short; indexing it
        # would 500 the whole page over a photo the user just deleted anyway.
        item = items_by_pk.get(item_id)
        if item is None:
            continue
        image = item.image
        image.album_item_id = item.pk
        images.append(image)
    return images


def album_images_page(
    album: Album,
    viewer: Profile | None,
    owner: Pin | Wiki | Profile | None = None,
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


def loose_images_for(owner: Pin | Wiki | Profile | Sequence[Pin | Wiki | Profile], viewer: Profile | None) -> QuerySet[Image]:
    """*owner*'s photos that aren't in any of its albums yet.

    Pass a sequence of pins to include child-pin photos when the parent
    Photos tab is showing descendant details.

    Args:
        owner: The Pin, Wiki, or Profile whose photos to list, or several of them.
        viewer: The profile browsing, for the standard photo-visibility gate.

    Returns:
        Matching photos not referenced by any of these owners' albums, newest first.
    """
    owners: list[Pin | Wiki | Profile] = [owner] if isinstance(owner, (Pin, Wiki, Profile)) else list(owner)
    album_ids = albums_for_owners(owners).values_list("pk", flat=True)
    filed_image_ids = AlbumItem.objects.filter(album_id__in=album_ids).values_list("image_id", flat=True)
    query = Q()
    for item in owners:
        query |= Q(**owner_kwargs_to_image_scope(item))
    from urbanlens.dashboard.models.images.model import Image

    qs = Image.objects.filter(query).visible_to(viewer).order_by("-created").exclude(pk__in=filed_image_ids)
    if len(owners) == 1 and _owner_conceal(owners[0], viewer):
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows

        qs = conceal_rows(qs, viewer)
    return qs


def add_images_to_album(album: Album, images: Sequence[Image], added_by: Profile | None) -> int:
    """Add photos to *album*, skipping any already in it.

    New items are stored with null ``order``. Under date/name sorts they
    slot in by metadata; under custom order they appear after the photos
    the user has already arranged.

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

    # The insert is not atomic with the existence read, and there are two callers -
    # one of them the Celery task cache_media_item_into_album, which Celery may deliver
    # more than once. Without ignore_conflicts the loser of that race hits uq_album_item
    # and raises, turning a duplicate add into a 500 instead of a no-op.
    before = AlbumItem.objects.filter(album=album).count()
    AlbumItem.objects.bulk_create(
        [AlbumItem(album=album, image=image, added_by=added_by, order=None) for image in to_add],
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
    """Freeze *album* into custom order following *item_ids*.

    The first drag (or any later one) numbers every current membership row
    so later uploads can stay null and sort after the arranged photos. Ids
    that don't belong to this album are ignored. A partial list - the grid
    only sending currently loaded tiles - is spliced into the album's
    existing display order rather than dropping the rest.

    Args:
        album: The album whose items are being reordered.
        item_ids: ``AlbumItem`` primary keys in their new display order.

    Returns:
        How many items now have an explicit ``order``.
    """
    current = list(AlbumItem.objects.in_display_order(album).values_list("pk", flat=True))
    if not current:
        return 0

    current_set = set(current)
    incoming = [item_id for item_id in item_ids if item_id in current_set]
    if not incoming:
        return 0
    incoming_set = set(incoming)
    incoming_iter = iter(incoming)
    ordered_ids = [next(incoming_iter) if item_id in incoming_set else item_id for item_id in current]

    items_by_id = {item.pk: item for item in AlbumItem.objects.for_album(album)}
    updated: list[AlbumItem] = []
    for order, item_id in enumerate(ordered_ids):
        item = items_by_id[item_id]
        if item.order != order:
            item.order = order
            updated.append(item)
    if updated:
        AlbumItem.objects.bulk_update(updated, ["order"])
    if album.sort != AlbumSort.CUSTOM:
        Album.objects.filter(pk=album.pk).update(sort=AlbumSort.CUSTOM)
        album.sort = AlbumSort.CUSTOM
    return len(ordered_ids)


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


def pin_tree(pin: Pin) -> list[Pin]:
    """The root pin and every descendant in *pin*'s hierarchy.

    Args:
        pin: Any pin in the tree.

    Returns:
        Every pin in the tree, root first, with ``location`` selected.
    """
    root = pin
    seen: set[int] = set()
    while root.parent_pin_id and root.pk not in seen:
        seen.add(root.pk)
        parent = root.parent_pin
        if parent is None:
            break
        root = parent
    return list(Pin.objects.filter(pk=root.pk).with_descendants().select_related("location"))


def move_album_targets(album: Album) -> list[Pin]:
    """Pins this album can move to: the rest of its parent's tree.

    Wiki albums have no pin tree and return an empty list.

    Args:
        album: The album to consider moving.

    Returns:
        Other pins in the same parent/child tree, excluding the current owner.
    """
    pin = album.parent_pin
    if pin is None:
        return []
    return [candidate for candidate in pin_tree(pin) if candidate.pk != pin.pk]


def move_album_to_pin(album: Album, target: Pin) -> Album:
    """Move *album* onto *target*, re-slug on collision, and take its photos.

    Photos currently attached to the source pin that are in this album are
    re-pointed at *target* so the grouping and the files travel together.
    Photos already on another pin (or a wiki) are left where they are.

    Args:
        album: A pin-owned album.
        target: Another pin in the same tree, owned by the same profile.

    Returns:
        The saved album (slug may have changed).

    Raises:
        ValueError: The album is a wiki album, *target* is the current parent,
            or *target* is not in the same tree / same profile.
    """
    source = album.parent_pin
    if source is None:
        raise ValueError("Community albums stay on their wiki.")
    if source.pk == target.pk:
        raise ValueError("This album is already on that pin.")
    if source.profile_id != target.profile_id:
        raise ValueError("Albums can only move between your own pins.")
    allowed_ids = {pin.pk for pin in pin_tree(source)}
    if target.pk not in allowed_ids:
        raise ValueError("Pick a parent or child pin of this place.")

    taken = set(Album.objects.filter(parent_pin=target).values_list("slug", flat=True))
    album.parent_pin = target
    if album.slug in taken:
        album.slug = ""
    album.save()

    from urbanlens.dashboard.models.images.model import Image

    image_ids = list(AlbumItem.objects.for_album(album).values_list("image_id", flat=True))
    if image_ids:
        Image.objects.filter(pk__in=image_ids, pin=source, profile_id=source.profile_id).update(
            pin=target,
            location=target.location,
        )
    return album
