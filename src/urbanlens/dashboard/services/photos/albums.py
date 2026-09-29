"""Album membership and ordering for a pin's, wiki's, or Vault's photos.
Albums group photos that already belong to their owner - a place (pin/wiki) or a profile's own Vault; they never widen who can see a photo."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.db.models import Case, Count, Exists, Max, Min, OuterRef, Q, Subquery, When
from django.db.models.functions import Coalesce

from urbanlens.dashboard.models.album.model import Album, AlbumItem
from urbanlens.dashboard.models.album.sort import AlbumSort
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core.capacity import ALBUM_PHOTOS, reserve

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from datetime import datetime

    from django.db.models import QuerySet

    from urbanlens.dashboard.models.album.queryset import AlbumItemQuerySet
    from urbanlens.dashboard.models.album.sort import AlbumSortSpec
    from urbanlens.dashboard.models.images.model import Image


#: How many photo tiles a grid page carries. Sized so a typical desktop
#: viewport plus a small scroll buffer is one request, without dumping a
#: thousand full ``Image`` rows into the first HTML response.
ALBUM_GRID_PAGE_SIZE = 48


@dataclass(frozen=True, slots=True)
class AlbumListEntry:
    """One album on the Photos tab, without hydrating every member photo."""

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

    Args:
        owner: The Pin, Wiki, or Profile (Vault) whose photos to scope to.

    Returns:
        A dict suitable for splatting into an ``Image`` queryset filter."""
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
        ValueError: The album has no parent set, which the create paths never produce."""
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
    # The location/wiki chain is part of the same select because every caller
    # that labels a pin album reads Pin.effective_name, which walks
    # pin -> location -> wiki: two more queries per album without it.
    return Album.objects.filter(query).select_related("cover_image", "parent_pin__location__wiki")


def _owner_conceal(owner: Pin | Wiki | Profile, viewer: Profile | None) -> bool:
    """Whether *viewer* sees the concealed form of *owner*'s wiki.
    Always False for a Pin or a Vault (Profile-owned) album - concealment is a wiki-only concept.

    Args:
        owner: The Pin, Wiki, or Profile whose albums/photos are being resolved.
        viewer: The browsing profile, or None for anonymous.

    Returns:
        Whether album/photo visibility here must also apply concealment."""
    if isinstance(owner, (Pin, Profile)):
        return False
    from urbanlens.dashboard.services.wiki.concealment import concealment_active

    return concealment_active(owner, viewer)


def _visible_items(items: AlbumItemQuerySet, viewer: Profile | None, *, conceal: bool = False) -> AlbumItemQuerySet:
    """Narrow membership rows to the photos *viewer* may see, as SQL.

    The image set is scoped to *items*' own photos before ``visible_to`` runs, because ``visible_to`` resolves
    its allowed-uploader set from whatever queryset it is handed; unscoped, that would be every uploader on
    the site.

    Args:
        items: Membership rows, typically one album's or one owner's.
        viewer: The browsing profile, or None for anonymous.
        conceal: Whether to also narrow to what a concealed viewer of the owning wiki may see.

    Returns:
        *items* restricted to viewer-visible photos.
    """
    from urbanlens.dashboard.models.images.model import Image

    images = Image.objects.filter(pk__in=items.values("image_id")).visible_to(viewer)
    if conceal:
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows

        images = conceal_rows(images, viewer)
    return items.filter(image__in=images.values("pk"))


def visible_album_items(album: Album, viewer: Profile | None, owner: Pin | Wiki | Profile | None = None) -> AlbumItemQuerySet:
    """*album*'s membership rows whose photo *viewer* may see, unordered and unevaluated.

    Args:
        album: The album to read.
        viewer: The browsing profile, for the photo-visibility gate.
        owner: The album's owner, if the caller already resolved it.

    Returns:
        A queryset to page, count or use as a subquery (``.values("image_id")``).
    """
    resolved_owner = owner if owner is not None else album_owner(album)
    return _visible_items(AlbumItem.objects.for_album(album), viewer, conceal=_owner_conceal(resolved_owner, viewer))


def _concealed_wikis(owners: Sequence[Pin | Wiki | Profile], viewer: Profile | None) -> list[Wiki]:
    """Those of *owners* that *viewer* sees the concealed form of."""
    return [owner for owner in owners if isinstance(owner, Wiki) and _owner_conceal(owner, viewer)]


def _conceal_by_wiki[QuerySetT: QuerySet[Any]](queryset: QuerySetT, wiki_field: str, concealed: Sequence[Wiki], owner_count: int, viewer: Profile | None) -> QuerySetT:
    """Narrow the rows of *queryset* that belong to a *concealed* wiki, leaving every other owner's rows alone.

    Args:
        queryset: Rows of several owners.
        wiki_field: The field naming a row's wiki (``parent_wiki`` for an album, ``wiki`` for a photo).
        concealed: The owners' wikis *viewer* is concealed from.
        owner_count: How many owners *queryset* spans; when every one is concealed, the whole set is narrowed.
        viewer: Who is looking.

    Returns:
        *queryset*, narrowed where concealment applies.
    """
    if not concealed:
        return queryset
    from urbanlens.dashboard.services.wiki.concealment import conceal_rows

    if len(concealed) == owner_count:
        return conceal_rows(queryset, viewer)
    in_concealed = Q(**{f"{wiki_field}__in": concealed})
    return queryset.filter(~in_concealed | Q(pk__in=conceal_rows(queryset.filter(in_concealed), viewer).values("pk")))


def _listed_albums(owner: Pin | Wiki | Profile | Sequence[Pin | Wiki | Profile], viewer: Profile | None) -> tuple[QuerySet[Album], set[int]]:
    """The albums a Photos tab lists for *owner*, in listing order, and the ids of the wikis concealed from *viewer*."""
    owners: list[Pin | Wiki | Profile] = [owner] if isinstance(owner, (Pin, Wiki, Profile)) else list(owner)
    concealed = _concealed_wikis(owners, viewer)
    albums_qs = _conceal_by_wiki(albums_for_owners(owners), "parent_wiki", concealed, len(owners), viewer)
    return albums_qs.order_by("name", "pk"), {wiki.pk for wiki in concealed}


def _describe_listed(albums: list[Album], viewer: Profile | None, concealed_wiki_ids: set[int]) -> list[AlbumListEntry]:
    """:func:`describe_albums` for a listing whose owners' wikis may differ in whether they are concealed."""
    hidden = [album for album in albums if album.parent_wiki_id in concealed_wiki_ids]
    if not hidden or len(hidden) == len(albums):
        return describe_albums(albums, viewer, conceal=bool(hidden))
    shown = [album for album in albums if album.parent_wiki_id not in concealed_wiki_ids]
    described = {entry.album.pk: entry for entry in [*describe_albums(hidden, viewer, conceal=True), *describe_albums(shown, viewer)]}
    return [described[album.pk] for album in albums if album.pk in described]


def albums_listing(owner: Pin | Wiki | Profile | Sequence[Pin | Wiki | Profile], viewer: Profile | None) -> list[AlbumListEntry]:
    """Every album of *owner* with cover, count, and date range.

    Unbounded; a page that renders albums uses :func:`albums_listing_page`.

    Args:
        owner: The Pin, Wiki, or Profile whose albums to list, or several of them.
        viewer: The browsing profile, for the photo-visibility gate.

    Returns:
        One :class:`AlbumListEntry` per album, in album order."""
    albums_qs, concealed_wiki_ids = _listed_albums(owner, viewer)
    return _describe_listed(list(albums_qs), viewer, concealed_wiki_ids)


def albums_listing_page(
    owner: Pin | Wiki | Profile | Sequence[Pin | Wiki | Profile],
    viewer: Profile | None,
    *,
    offset: int = 0,
    limit: int = ALBUM_GRID_PAGE_SIZE,
    name_contains: str = "",
    exclude: Album | None = None,
) -> tuple[list[AlbumListEntry], int]:
    """One page of :func:`albums_listing`, plus the un-paged total.

    Args:
        owner: The Pin, Wiki, or Profile whose albums to list, or several of them.
        viewer: The browsing profile, for the photo-visibility gate.
        offset: How many albums to skip.
        limit: Maximum albums to return.
        name_contains: Case-insensitive name fragment to narrow to.
        exclude: An album to leave out.

    Returns:
        ``(page, total)``, *total* counting every album that matches."""
    albums_qs, concealed_wiki_ids = _listed_albums(owner, viewer)
    if name_contains:
        albums_qs = albums_qs.filter(name__icontains=name_contains)
    if exclude is not None:
        albums_qs = albums_qs.exclude(pk=exclude.pk)
    return _describe_listed(list(albums_qs[offset : offset + limit]), viewer, concealed_wiki_ids), albums_qs.count()


def describe_albums(albums: Sequence[Album], viewer: Profile | None, *, conceal: bool = False) -> list[AlbumListEntry]:
    """Cover, photo count and date range for each of *albums*, computed in SQL.

    Costs the visibility gate plus two queries however many albums and photos there are: one annotated
    album query and one for the cover rows. No membership row is loaded.

    Args:
        albums: The albums to describe, in the order they should be rendered.
        viewer: The browsing profile, for the photo-visibility gate.
        conceal: Whether wiki concealment applies to *viewer*.

    Returns:
        One :class:`AlbumListEntry` per album, in the given order."""
    from urbanlens.dashboard.models.album.sort import ALBUM_SORT_SPECS
    from urbanlens.dashboard.models.images.model import Image

    albums = list(albums)
    if not albums:
        return []

    album_ids = [album.pk for album in albums]
    per_album = _visible_items(AlbumItem.objects.filter(album_id__in=album_ids), viewer, conceal=conceal).filter(album_id=OuterRef("pk"))
    capture_time = Coalesce("image__taken_at", "image__created")

    def aggregate(expression) -> Subquery:
        return Subquery(per_album.order_by().values("album_id").annotate(value=expression).values("value")[:1])

    def first_image(spec: AlbumSortSpec) -> Subquery:
        return Subquery(spec.apply(per_album).values("image_id")[:1])

    stats = {
        row["pk"]: row
        for row in Album.objects.filter(pk__in=album_ids)
        .annotate(
            visible_count=aggregate(Count("pk")),
            first_taken=aggregate(Min(capture_time)),
            last_taken=aggregate(Max(capture_time)),
            cover_visible=Exists(per_album.filter(image_id=OuterRef("cover_image_id"))),
            first_image_id=Case(
                *[When(sort=sort, then=first_image(spec)) for sort, spec in ALBUM_SORT_SPECS.items()],
                default=first_image(ALBUM_SORT_SPECS[AlbumSort.UPLOADED]),
            ),
        )
        .values("pk", "visible_count", "first_taken", "last_taken", "cover_visible", "cover_image_id", "first_image_id")
    }

    cover_ids = {album_id: row["cover_image_id"] if row["cover_visible"] else row["first_image_id"] for album_id, row in stats.items()}
    wanted = {cover_id for cover_id in cover_ids.values() if cover_id is not None}
    covers = {image.pk: image for image in Image.objects.filter(pk__in=wanted)} if wanted else {}
    entries = []
    for album in albums:
        row = stats.get(album.pk)
        if row is None:
            continue
        cover_id = cover_ids[album.pk]
        entries.append(
            AlbumListEntry(
                album=album,
                photo_count=row["visible_count"] or 0,
                cover=covers.get(cover_id) if cover_id is not None else None,
                date_start=row["first_taken"],
                date_end=row["last_taken"],
            )
        )
    return entries


def eligible_images_for(owner: Pin | Wiki | Profile, viewer: Profile | None) -> QuerySet[Image]:
    """Photos that may be placed in one of *owner*'s albums.
    An album is strictly scoped to its owner: a pin album may only hold that pin's photos, a wiki album only that wiki's, and a vault album only its owning profile's own uploads.

    Args:
        owner: The Pin, Wiki, or Profile (Vault) that owns the album.
        viewer: The profile browsing, for the standard photo-visibility gate.

    Returns:
        Matching, viewer-visible photos, newest first."""
    from urbanlens.dashboard.models.images.model import Image

    qs = Image.objects.filter(**owner_kwargs_to_image_scope(owner)).photos().visible_to(viewer).order_by("-created")
    if _owner_conceal(owner, viewer):
        from urbanlens.dashboard.services.wiki.concealment import conceal_rows

        qs = conceal_rows(qs, viewer)
    return qs


def _hydrate(items: AlbumItemQuerySet) -> list[Image]:
    """The photos behind already-ordered membership rows, each carrying ``album_item_id``."""
    images = []
    for item in items.select_related("image"):
        image = item.image
        image.album_item_id = item.pk
        images.append(image)
    return images


def album_images(album: Album, viewer: Profile | None, owner: Pin | Wiki | Profile | None = None) -> list[Image]:
    """Every photo in *album* the viewer may see, in the album's current sort.

    Unbounded; a page that renders an album uses :func:`album_images_page`.

    Args:
        album: The album to read.
        viewer: The profile browsing, for the standard photo-visibility gate.
        owner: The album's owner, if the caller already resolved it.

    Returns:
        The album's viewer-visible photos, ordered for display."""
    return _hydrate(album.sort_spec.apply(visible_album_items(album, viewer, owner)))


def album_images_page(
    album: Album,
    viewer: Profile | None,
    owner: Pin | Wiki | Profile | None = None,
    *,
    offset: int = 0,
    limit: int = ALBUM_GRID_PAGE_SIZE,
) -> tuple[list[Image], int]:
    """One page of *album*'s photos, plus the un-paged total.

    Args:
        album: The album to read.
        viewer: The profile browsing, for the photo-visibility gate.
        owner: The album's owner, if already resolved.
        offset: How many visible photos to skip.
        limit: Maximum photos to return.

    Returns:
        ``(page, total)`` where *page* items each carry ``album_item_id``."""
    items = visible_album_items(album, viewer, owner)
    return _hydrate(album.sort_spec.apply(items)[offset : offset + limit]), items.count()


def owner_images_for(owner: Pin | Wiki | Profile | Sequence[Pin | Wiki | Profile], viewer: Profile | None) -> QuerySet[Image]:
    """*owner*'s photos, filed in an album or not.

    Args:
        owner: The Pin, Wiki, or Profile whose photos to list, or several of them.
        viewer: The profile browsing, for the standard photo-visibility gate.

    Returns:
        Matching photos, newest first."""
    owners: list[Pin | Wiki | Profile] = [owner] if isinstance(owner, (Pin, Wiki, Profile)) else list(owner)
    query = Q()
    for item in owners:
        query |= Q(**owner_kwargs_to_image_scope(item))
    from urbanlens.dashboard.models.images.model import Image

    qs = Image.objects.filter(query).visible_to(viewer).order_by("-created")
    return _conceal_by_wiki(qs, "wiki", _concealed_wikis(owners, viewer), len(owners), viewer)


def loose_images_for(owner: Pin | Wiki | Profile | Sequence[Pin | Wiki | Profile], viewer: Profile | None) -> QuerySet[Image]:
    """*owner*'s photos that aren't in any of its albums yet.

    Args:
        owner: The Pin, Wiki, or Profile whose photos to list, or several of them.
        viewer: The profile browsing, for the standard photo-visibility gate.

    Returns:
        Matching photos not referenced by any of these owners' albums, newest first."""
    owners: list[Pin | Wiki | Profile] = [owner] if isinstance(owner, (Pin, Wiki, Profile)) else list(owner)
    album_ids = albums_for_owners(owners).values_list("pk", flat=True)
    filed_image_ids = AlbumItem.objects.filter(album_id__in=album_ids).values_list("image_id", flat=True)
    return owner_images_for(owners, viewer).exclude(pk__in=filed_image_ids)


def filed_image_ids(owners: Sequence[Pin | Wiki | Profile], image_ids: Collection[int]) -> set[int]:
    """Which of *image_ids* sit in at least one of *owners*' albums.

    Args:
        owners: The pins, wikis and/or profiles whose albums count.
        image_ids: The photos to ask about.

    Returns:
        The subset of *image_ids* that is filed somewhere."""
    if not image_ids:
        return set()
    album_ids = albums_for_owners(owners).values_list("pk", flat=True)
    return set(AlbumItem.objects.filter(album_id__in=album_ids, image_id__in=image_ids).values_list("image_id", flat=True))


def add_images_to_album(album: Album, images: Sequence[Image], added_by: Profile | None) -> int:
    """Add photos to *album*, skipping any already in it.

    Args:
        album: The album to add to.
        images: The photos to add.
        added_by: The profile performing the add, recorded per item so community wiki albums keep per-photo attribution.

    Returns:
        How many photos were actually added.

    Raises:
        CapacityExceededError: The album has no room for every photo being added."""
    existing_ids = set(AlbumItem.objects.for_album(album).values_list("image_id", flat=True))
    to_add = [image for image in images if image.pk not in existing_ids]
    if not to_add:
        return 0
    with reserve(ALBUM_PHOTOS, album.pk, adding=len(to_add)):
        return _insert_album_items(album, to_add, added_by)


def _insert_album_items(album: Album, to_add: Sequence[Image], added_by: Profile | None) -> int:

    # The insert is not atomic with the existence read, and there are two callers - one of them the
    # Celery task cache_media_item_into_album, which Celery may deliver more than once.
    # Without ignore_conflicts the loser of that race hits uq_album_item and raises, turning a
    # duplicate add into a 500 instead of a no-op.
    before = AlbumItem.objects.filter(album=album).count()
    AlbumItem.objects.bulk_create(
        [AlbumItem(album=album, image=image, added_by=added_by, order=None) for image in to_add],
        ignore_conflicts=True,
    )
    # Counted rather than assumed: ignore_conflicts silently drops the rows another
    # process got in first, so len(to_add) would over-report what this call did.
    return AlbumItem.objects.filter(album=album).count() - before


def remove_images_from_album(album: Album, image_ids: Sequence[int]) -> int:
    """Remove photos from *album*.

    Args:
        album: The album to remove from.
        image_ids: Primary keys of the photos to remove.

    Returns:
        How many membership rows were deleted."""
    deleted, _ = AlbumItem.objects.for_album(album).filter(image_id__in=list(image_ids)).delete()
    if album.cover_image_id is not None and album.cover_image_id in set(image_ids):
        Album.objects.filter(pk=album.pk).update(cover_image=None)
    return deleted


def reorder_album_items(album: Album, item_ids: Sequence[int]) -> int:
    """Freeze *album* into custom order following *item_ids*.
    The first drag (or any later one) numbers every current membership row so later uploads can stay null and sort after the arranged photos.

    Args:
        album: The album whose items are being reordered.
        item_ids: ``AlbumItem`` primary keys in their new display order.

    Returns:
        How many items now have an explicit ``order``."""
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

    # A row removed since the read above (another tab, a concurrent remove) matches nothing here.
    processed = AlbumItem.objects.for_album(album).number_in_order(ordered_ids)
    if album.sort != AlbumSort.CUSTOM:
        Album.objects.filter(pk=album.pk).update(sort=AlbumSort.CUSTOM)
        album.sort = AlbumSort.CUSTOM
    return processed


def describe_album(album: Album, viewer: Profile | None, owner: Pin | Wiki | Profile | None = None) -> AlbumListEntry:
    """Cover, visible photo count and date range for one album.

    Args:
        album: The album to describe.
        viewer: The browsing profile, for the photo-visibility gate.
        owner: The album's owner, if the caller already resolved it.

    Returns:
        The album's :class:`AlbumListEntry`.
    """
    resolved_owner = owner if owner is not None else album_owner(album)
    entries = describe_albums([album], viewer, conceal=_owner_conceal(resolved_owner, viewer))
    return entries[0] if entries else AlbumListEntry(album=album, photo_count=0, cover=None, date_start=None, date_end=None)


def album_cover(album: Album, viewer: Profile | None) -> Image | None:
    """The photo to show as *album*'s cover: the chosen one when the viewer can see it, else the first in display order.

    Args:
        album: The album to pick a cover for.
        viewer: The profile browsing, for the standard photo-visibility gate.

    Returns:
        The cover photo, or None for an album with nothing visible.
    """
    return describe_album(album, viewer).cover


def pin_tree(pin: Pin) -> list[Pin]:
    """The root pin and every descendant in *pin*'s hierarchy.

    Args:
        pin: Any pin in the tree.

    Returns:
        Every pin in the tree, with ``location`` selected.
    """
    root_id = pin.pk if pin.parent_pin_id is None else Pin.objects.tree_root_id(pin.pk)
    return list(Pin.objects.filter(pk=root_id).with_descendants().select_related("location"))


def move_album_targets(album: Album) -> list[Pin]:
    """Pins this album can move to: the rest of its parent's tree.

    Args:
        album: The album to consider moving.

    Returns:
        Other pins in the same parent/child tree, excluding the current owner."""
    pin = album.parent_pin
    if pin is None:
        return []
    return [candidate for candidate in pin_tree(pin) if candidate.pk != pin.pk]


def move_album_to_pin(album: Album, target: Pin) -> Album:
    """Move *album* onto *target*, re-slug on collision, and take its photos.
    Photos currently attached to the source pin that are in this album are re-pointed at *target* so the grouping and the files travel together.

    Args:
        album: A pin-owned album.
        target: Another pin in the same tree, owned by the same profile.

    Returns:
        The saved album (slug may have changed).

    Raises:
        ValueError: The album is a wiki album, *target* is the current parent, or *target* is not in the same tree / same profile."""
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
