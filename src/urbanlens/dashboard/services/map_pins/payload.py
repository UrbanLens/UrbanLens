"""Efficient, bounded map-pin payload generation.

Two paths build the same dict, and the split is deliberate.

`serialize` takes a model instance and is what `services.pins.pin_sync` and
`services.pins.pin_detail` use, because they layer sync-only and detail-only
fields on top and read arbitrary `Pin` properties to do it. Both are bounded -
one pin, or a page clamped to `MAX_LIMIT` - so the object graph costs little.

`page` and `all` build the same dict from a flat projection and never
instantiate a model. Measured at 10,000 pins before this existed: 5.79s wall,
88% of it CPU, constructing 63,240 model objects (a `select_related` companion
per row, and a fresh `Label` per pin-label pair - 128 distinct labels became
~36,000 instances) to emit 10,000 dicts. Postgres was 0.36s of it. The map is
the one caller that serializes a whole account at once, on a gevent worker where
pure-Python work yields to nothing, so it reads columns instead of objects.

What keeps the two honest is that neither owns a rule. The label facts collapse
into :class:`LabelView` and every decision - which icon wins, which colour,
which chips show - is a module function over that, so a change lands on both
paths at once. The place-name and address rules live one level further out, in
`services.locations.display`, shared with the model properties themselves.
`tests.hypothesis.test_map_payload_agreement` holds the two paths to identical
output over generated pins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from django.core.files.storage import default_storage
from django.db.models import Count, OuterRef, Prefetch, QuerySet, Subquery

from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.images.relevance import MediaRelevance
from urbanlens.dashboard.models.labels.customization.model import LabelCustomization
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin import Pin
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.services.locations import display

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from urbanlens.dashboard.models.profile.model import Profile

#: Label kinds shown as chips on a pin. Excludes ``user`` (people labels) and
#: ``media`` (photo-only labels), neither of which describes the place itself.
DISPLAY_LABEL_KINDS = frozenset({"tag", "category", "status"})

#: Pins read per round trip on the unbounded :meth:`MapPinPayloadService.all`
#: path. Bounds the working set and the label lookup's ``IN`` list; it does not
#: bound the total, which is the caller's business.
_BATCH_SIZE = 1000


class _HasKind(Protocol):
    """Read-only, so a frozen `LabelView` satisfies it as well as a `Label` does."""

    @property
    def kind(self) -> str: ...


@dataclass(frozen=True, slots=True)
class LabelView:
    """One label as the payload sees it, with this profile's overrides applied.

    Resolved once per distinct label rather than once per pin that carries it,
    which is the difference between 128 of these and ~36,000 `Label` instances
    on a large map.
    """

    id: int
    kind: str
    name: str
    order: int
    effective_icon: str | None
    effective_color: str | None
    custom_icon_url: str | None
    icon_is_overridden: bool

    @classmethod
    def from_model(cls, label: Label) -> LabelView:
        """Build a view from a `Label`, reading the customization it prefetched.

        Args:
            label: A label from a queryset built by
                :meth:`~urbanlens.dashboard.models.labels.queryset.LabelQuerySet.with_customizations_for`.

        Returns:
            The same facts the projection path derives from columns.
        """
        return cls(
            id=label.id,
            kind=label.kind,
            name=label.name,
            order=label.order,
            effective_icon=label.effective_icon,
            effective_color=label.effective_color,
            custom_icon_url=label.custom_icon.url if label.custom_icon else None,
            icon_is_overridden=label.icon_is_overridden,
        )

    def as_dictionary_entry(self) -> dict[str, Any]:
        """This label as the client's label dictionary holds it.

        Returns:
            The facts a chip needs, plus the `kind` that separates a status from
            a category.
        """
        return {"id": self.id, "kind": self.kind, "name": self.name, "color": self.effective_color, "icon": self.effective_icon}

    @classmethod
    def from_row(cls, row: dict[str, Any], customization: dict[str, Any] | None) -> LabelView:
        """Build a view from a label's columns and this profile's override row.

        Args:
            row: ``id``, ``kind``, ``name``, ``order``, ``icon``, ``color`` and
                ``custom_icon`` for one label.
            customization: The profile's ``LabelCustomization`` columns for that
                label, or None. An override's ``icon``/``color`` count when they
                are set at all, including to the empty string - that is how a
                user clears an inherited icon. Its ``name`` is deliberately not
                read; see the note on the ``name`` field below.

        Returns:
            The same facts :meth:`from_model` derives from an instance.
        """
        override_icon = customization["icon"] if customization else None
        override_color = customization["color"] if customization else None
        return cls(
            id=row["id"],
            kind=row["kind"],
            # The label's own name, not the profile's rename: the payload has
            # always emitted `Label.name` here rather than `effective_name`, so a
            # renamed label still shows its global name on the map even though its
            # recoloured icon does follow the override. Changing that would change
            # the payload, and the client caches it by version.
            name=row["name"],
            order=row["order"],
            effective_icon=override_icon if override_icon is not None else row["icon"],
            effective_color=override_color if override_color is not None else row["color"],
            custom_icon_url=default_storage.url(row["custom_icon"]) if row["custom_icon"] else None,
            icon_is_overridden=override_icon is not None,
        )


def display_label_views[LabelT: _HasKind](labels: Sequence[LabelT]) -> list[LabelT]:
    """The labels that display as chips, in the order given.

    Args:
        labels: The pin's labels, already ordered.

    Returns:
        Only the tag, category and status labels.
    """
    return [label for label in labels if label.kind in DISPLAY_LABEL_KINDS]


def _ordered_location_labels(labels: Sequence[LabelView]) -> list[LabelView]:
    """Labels describing the place, in icon-priority order (highest order first)."""
    return sorted((label for label in labels if label.kind != "user"), key=lambda label: (-label.order, label.name or ""))


def _winning_display_label(*, pin_icon: str | None, pin_custom_icon_url: str | None, labels: Sequence[LabelView]) -> LabelView | None:
    """The label a pin inherits its icon from, or None when the pin has its own."""
    if pin_custom_icon_url or pin_icon:
        return None
    for label in _ordered_location_labels(labels):
        if label.custom_icon_url and not label.icon_is_overridden:
            return label
        if label.effective_icon:
            return label
    return None


def resolve_icon(*, pin_icon: str | None, pin_custom_icon_url: str | None, labels: Sequence[LabelView]) -> str | None:
    """The icon the map draws for a pin: its own, else the winning label's.

    Args:
        pin_icon: The pin's own icon override.
        pin_custom_icon_url: URL of the pin's own uploaded icon, if any.
        labels: The pin's labels.

    Returns:
        An icon name or image URL, or None when nothing supplies one.
    """
    if pin_custom_icon_url:
        return pin_custom_icon_url
    if pin_icon:
        return pin_icon
    winning = _winning_display_label(pin_icon=pin_icon, pin_custom_icon_url=pin_custom_icon_url, labels=labels)
    if not winning:
        return None
    if winning.custom_icon_url and not winning.icon_is_overridden:
        return winning.custom_icon_url
    return winning.effective_icon


def resolve_color(*, pin_color: str | None, pin_icon: str | None, pin_custom_icon_url: str | None, labels: Sequence[LabelView]) -> str | None:
    """The colour the map draws for a pin: its own, else the winning label's.

    A pin carrying its own icon takes no colour from a label - the icon already
    carries the label's identity, and tinting it would misreport which label won.

    Args:
        pin_color: The pin's own colour override.
        pin_icon: The pin's own icon override.
        pin_custom_icon_url: URL of the pin's own uploaded icon, if any.
        labels: The pin's labels.

    Returns:
        A colour, or None.
    """
    if pin_color:
        return pin_color
    if pin_custom_icon_url or pin_icon:
        return None
    winning = _winning_display_label(pin_icon=pin_icon, pin_custom_icon_url=pin_custom_icon_url, labels=labels)
    return winning.effective_color if winning else None


def _build_payload(
    *,
    pk: int,
    uuid: Any,
    slug: str | None,
    name: str,
    description: str | None,
    priority: Any,
    last_visited: Any,
    latitude: Any,
    longitude: Any,
    profile_id: int | None,
    rating: Any,
    address: str | None,
    own_icon: str | None,
    own_custom_icon_url: str | None,
    own_color: str | None,
    child_count: Any,
    cover_photo_url: str | None,
    labels: Sequence[LabelView],
) -> dict[str, Any]:
    """Assemble the payload both paths return.

    The single place the payload's shape is decided. Its key set is pinned to
    the web client's cache version - see
    `tests.hypothesis.test_map_pin_payload_contract`.

    Args:
        name: Already resolved. The two paths reach it differently - one through
            `Pin.effective_name`, one through `services.locations.display` over
            projected columns - so neither the wiki fallback nor the "Unnamed
            Location in {area}" placeholder is decided here.
    """
    chips = display_label_views(labels)
    return {
        "id": pk,
        "uuid": str(uuid),
        "slug": slug or str(uuid),
        "name": name,
        "icon": resolve_icon(pin_icon=own_icon, pin_custom_icon_url=own_custom_icon_url, labels=labels),
        "description": description or "",
        "priority": priority,
        "last_visited": last_visited.isoformat() if last_visited else "never",
        "latitude": float(latitude),
        "longitude": float(longitude),
        "profile": profile_id,
        "rating": rating or 0,
        "color": resolve_color(pin_color=own_color, pin_icon=own_icon, pin_custom_icon_url=own_custom_icon_url, labels=labels),
        # The labels themselves travel once per response, not once per pin that
        # carries them - see `MapPinPayloadService.label_dictionary`. Ordered by
        # `(-order, name)`, so the client's chips keep the server's priority.
        "label_ids": [label.id for label in chips],
        "address": address,
        # The pin's own icon/color overrides, distinct from "icon"/"color" above
        # (which fall back to an inherited label's icon/color for map display).
        # The edit dialog must pre-fill from these, not the effective values -
        # otherwise resaving a pin that merely *displays* a label's icon bakes
        # that icon onto the pin permanently, even though the user never touched it.
        "own_icon": own_icon,
        "own_custom_icon_url": own_custom_icon_url,
        "own_color": own_color,
        "child_count": child_count or 0,
        "cover_photo_url": cover_photo_url,
    }


@dataclass(frozen=True)
class MapPinPage:
    pins: list[dict[str, Any]]
    next_cursor: int | None
    total: int | None = None


#: The payload shape's version, mirrored by `PIN_CACHE_VERSION` in
#: `frontend/ts/shared/pin-cache.ts` and held to it by
#: `test_map_pin_payload_contract.py`. Anything keyed on the shape - a cached
#: document, a client store - includes this so a change orphans the old copies
#: rather than needing them migrated.
PAYLOAD_VERSION = 11


class MapPinPayloadService:
    """Build map pin JSON in small, database-only batches.

    The map endpoint is intentionally different from rich pin-detail serializers:
    it avoids geocoding-backed properties, avoids per-pin review queries, and
    supports keyset pagination so one large user cannot monopolize a worker.
    """

    DEFAULT_LIMIT = 500
    MAX_LIMIT = 1000

    #: Columns the projection path reads. Narrow on purpose: a `Pin` row joined
    #: to its location, wiki and cover photo is ~7 KB, and several of the columns
    #: it would carry cost real work to decode (the image's filename and EXIF are
    #: encrypted, the location's point is built into a geometry object) for values
    #: the payload never emits.
    _PROJECTION_FIELDS = (
        "pk",
        "uuid",
        "slug",
        "name",
        "description",
        "priority",
        "last_visited",
        "icon",
        "custom_icon",
        "color",
        "profile_id",
        "location__latitude",
        "location__longitude",
        "location__official_name",
        "location__street_number",
        "location__route",
        "location__locality",
        "location__administrative_area_level_1",
        "location__country",
        "location__wiki__name",
        "cover_photo_id",
        "cover_photo__thumbnail",
        "cover_photo__image",
        "cover_photo__source_url",
    )

    def __init__(self, profile: Profile):
        self.profile = profile
        self._label_views: dict[int, LabelView] = {}

    def _irrelevant_item_keys_for_profile(self) -> QuerySet[MediaRelevance, dict[str, Any]]:
        """This profile's own "not relevant" votes, as a subquery to embed.

        Only a materialized community-gallery photo (``media_item_key`` set)
        can appear here - see ``services.media.media_relevance.effective_relevance``'s
        own docs on why a plain personal upload is trusted by default instead.

        A subquery rather than the keys themselves. Read into Python and inlined
        as a literal ``IN (...)``, they put roughly 88 bytes of statement text
        into every batch per vote the profile has ever cast - twice, once per
        fallback-photo subquery - so a user's own history decided how large a
        statement their map sent, without bound. Not *correlated*: `MediaRelevance`
        is indexed on ``(profile, location)``, and an `EXISTS` resolved per pin row
        would have no index to use, where this is evaluated once and hashed.

        Returns:
            The profile's not-relevant item keys, as a queryset to embed.
        """
        return MediaRelevance.objects.filter(profile=self.profile, is_relevant=False).values("item_key")

    def _annotations(self) -> dict[str, Any]:
        """The computed columns both paths select.

        ``child_count`` is a scalar subquery rather than ``Count(distinct=True)``:
        aggregating over a join makes the planner sort the whole join product,
        and the fan-out inflates every other column's row count on the way.
        """
        latest_rating = Review.objects.filter(pin_id=OuterRef("pk")).order_by("-created").values("rating")[:1]
        children = Pin.objects.filter(parent_pin_id=OuterRef("pk")).order_by().values("parent_pin_id").annotate(total=Count("pk")).values("total")
        # Fallback cover photo when the pin has none set explicitly: its own
        # earliest photo that this profile hasn't voted irrelevant. Annotated as
        # raw storage paths (not a second query per pin) so page()/all() stay a
        # single query regardless of how many pins are being built.
        fallback_photo = Image.objects.filter(pin_id=OuterRef("pk"), media_type=MediaKind.PHOTO).exclude(media_item_key__in=self._irrelevant_item_keys_for_profile()).order_by("created")
        return {
            "map_rating": Subquery(latest_rating),
            "child_count": Subquery(children),
            "fallback_photo_thumbnail": Subquery(fallback_photo.values("thumbnail")[:1]),
            "fallback_photo_image": Subquery(fallback_photo.values("image")[:1]),
        }

    def prepare_queryset(self, query: QuerySet[Pin]) -> QuerySet[Pin]:
        """Annotate and join *query* for the model-instance path.

        Used by `services.pins.pin_sync` and `services.pins.pin_detail`, which
        need the instances. The map's own paths use :meth:`page` and :meth:`all`,
        which read columns instead.

        Args:
            query: Pins to serialize.

        Returns:
            The queryset with the payload's annotations and joins applied.
        """
        return (
            # location__wiki as well as location: every pin serialized here reads
            # effective_name, which falls through to Location.display_name, which reads
            # the reverse OneToOne `wiki` - one query per pin on the map's own payload
            # unless it is joined in. That property's docstring asks callers to do this.
            query.select_related("location", "location__wiki", "cover_photo").annotate(**self._annotations()).prefetch_related(Prefetch("labels", queryset=Label.objects.with_customizations_for(self.profile))).order_by("pk")
        )

    def _prepare_rows(self, query: QuerySet[Pin]) -> QuerySet[Pin, dict[str, Any]]:
        """The projection the map paths read, ordered for keyset pagination."""
        return query.annotate(**self._annotations()).order_by("pk").values(*self._PROJECTION_FIELDS, *self._annotations())

    def _label_views_for(self, pin_ids: Sequence[int]) -> dict[int, list[LabelView]]:
        """This batch's labels, resolved once per distinct label and shared by pin.

        Reads the through table directly rather than prefetching the relation:
        Django builds a fresh `Label` for every pin-label pair, so a vocabulary
        of a hundred labels becomes tens of thousands of instances across a large
        map. Views for labels already seen by this service instance are reused.

        Args:
            pin_ids: The pins to resolve labels for.

        Returns:
            Each pin's labels in `Label.Meta.ordering` (``-order``, ``name``).
        """
        if not pin_ids:
            return {}
        pairs = list(Pin.labels.through.objects.filter(pin_id__in=pin_ids).values_list("pin_id", "label_id"))
        self._resolve_label_views({label_id for _, label_id in pairs})

        by_pin: dict[int, list[LabelView]] = {}
        for pin_id, label_id in pairs:
            view = self._label_views.get(label_id)
            if view is not None:
                by_pin.setdefault(pin_id, []).append(view)
        for views in by_pin.values():
            views.sort(key=lambda label: (-label.order, label.name or ""))
        return by_pin

    def _resolve_label_views(self, label_ids: set[int]) -> None:
        """Build a :class:`LabelView` for each of these labels not already held.

        Args:
            label_ids: The labels to resolve. Views this service instance has
                already built are reused rather than re-read.
        """
        missing = label_ids - self._label_views.keys()
        if not missing:
            return
        overrides = {row["label_id"]: row for row in LabelCustomization.objects.filter(profile=self.profile, label_id__in=missing).values("label_id", "name", "icon", "color")}
        for row in Label.objects.filter(pk__in=missing).values("id", "kind", "name", "order", "icon", "color", "custom_icon"):
            self._label_views[row["id"]] = LabelView.from_row(row, overrides.get(row["id"]))

    def label_dictionary_for(self, payloads: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """The labels *payloads* name, resolved from what building them already read.

        Free: every view is already in hand from serializing the pins. Prefer
        this wherever the payloads exist before the response is written, which
        is everywhere except the streamed document - whose head goes out before
        its first pin, so it has to ask :meth:`label_dictionary` instead.

        Args:
            payloads: Map payloads carrying ``label_ids``.

        Returns:
            ``{"<id>": {id, kind, name, color, icon}}``, covering exactly the ids
            these payloads use.
        """
        entries: dict[str, dict[str, Any]] = {}
        for payload in payloads:
            for label_id in payload.get("label_ids", ()):
                key = str(label_id)
                if key not in entries and (view := self._label_views.get(label_id)) is not None:
                    entries[key] = view.as_dictionary_entry()
        return entries

    def label_dictionary(self) -> dict[str, dict[str, Any]]:
        """Every label the profile's pins can name, keyed by id as a string.

        One query, and it walks the whole through table for the profile - which
        is why only the streamed document uses it. Keys are strings because the
        client reads them back out of JSON, where an object's keys always are.

        Returns:
            ``{"<id>": {id, kind, name, color, icon}}`` for the profile's
            chip-bearing labels.
        """
        attached = set(
            Pin.labels.through.objects.filter(pin__profile=self.profile).values_list("label_id", flat=True).distinct(),
        )
        self._resolve_label_views(attached)
        return {str(label_id): view.as_dictionary_entry() for label_id in attached if (view := self._label_views.get(label_id)) is not None and view.kind in DISPLAY_LABEL_KINDS}

    def _serialize_rows(self, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Turn one batch of projected rows into payloads."""
        labels_by_pin = self._label_views_for([row["pk"] for row in rows])
        return [
            _build_payload(
                pk=row["pk"],
                uuid=row["uuid"],
                slug=row["slug"],
                name=row["name"]
                or display.display_name(
                    wiki_name=row["location__wiki__name"],
                    official_name=row["location__official_name"],
                    city=row["location__locality"],
                    state=row["location__administrative_area_level_1"],
                    country=row["location__country"],
                ),
                description=row["description"],
                priority=row["priority"],
                last_visited=row["last_visited"],
                latitude=row["location__latitude"],
                longitude=row["location__longitude"],
                profile_id=row["profile_id"],
                rating=row["map_rating"],
                # Pin.address_basic/city/state are properties reading the linked
                # location, not columns of their own, so effective_address_basic's
                # `self.address_basic or self.location.address_basic` is the same
                # value twice - there is only ever the location's. On Location
                # they are properties too, over street_number/route/locality/
                # administrative_area_level_1, which is what the projection reads.
                address=display.formatted_address(
                    address_basic=display.street_address(street_number=row["location__street_number"], route=row["location__route"]),
                    city=row["location__locality"],
                    state=row["location__administrative_area_level_1"],
                ),
                own_icon=row["icon"],
                own_custom_icon_url=default_storage.url(row["custom_icon"]) if row["custom_icon"] else None,
                own_color=row["color"],
                child_count=row["child_count"],
                cover_photo_url=_row_cover_photo_url(row),
                labels=labels_by_pin.get(row["pk"], []),
            )
            for row in rows
        ]

    def page(self, query: QuerySet[Pin], *, cursor: int | None = None, limit: int | None = None, include_total: bool = False) -> MapPinPage:
        """One keyset page of payloads, built without instantiating a model.

        Args:
            query: Pins to serialize, already scoped to the requesting profile.
            cursor: Exclusive lower bound on pin pk, from a previous page.
            limit: Page size, clamped to :attr:`MAX_LIMIT`.
            include_total: Also count every matching row (one extra query).

        Returns:
            The page, and the cursor to continue from when more remain.
        """
        limit = min(max(int(limit or self.DEFAULT_LIMIT), 1), self.MAX_LIMIT)
        if cursor:
            query = query.filter(pk__gt=cursor)
        total = query.count() if include_total else None
        rows = list(self._prepare_rows(query)[: limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = rows[-1]["pk"] if has_more and rows else None
        return MapPinPage(pins=self._serialize_rows(rows), next_cursor=next_cursor, total=total)

    def all(self, query: QuerySet[Pin]) -> list[dict[str, Any]]:
        """Every matching pin's payload, read in bounded batches.

        Unbounded in output by design, for the callers that need the whole
        account at once. A request path should prefer :meth:`page`, or
        :mod:`services.map_pins.document`, which streams batches of it.

        Args:
            query: Pins to serialize, already scoped to the requesting profile.

        Returns:
            One payload per pin, ordered by pk.
        """
        return [payload for batch in self._batched_rows(query) for payload in self._serialize_rows(batch)]

    def _batched_rows(self, query: QuerySet[Pin]) -> Iterator[list[dict[str, Any]]]:
        """Walk the projection by keyset, so no single query holds the account."""
        cursor: int | None = None
        while True:
            scoped = query.filter(pk__gt=cursor) if cursor else query
            rows = list(self._prepare_rows(scoped)[:_BATCH_SIZE])
            if not rows:
                return
            yield rows
            if len(rows) < _BATCH_SIZE:
                return
            cursor = rows[-1]["pk"]

    def display_labels(self, pin: Pin) -> list[Label]:
        """The pin's labels that display as chips, in prefetch order.

        Reads ``pin.labels.all()``, which ``prepare_queryset`` prefetches with
        the profile's per-label customizations applied - so calling this on a
        prepared pin costs no additional query. Shared with
        ``services.pins.pin_sync.serialize_sync_pin``, which needs the same set to
        emit each chip's ``kind``.

        Args:
            pin: The pin whose labels to filter. Should come from a queryset
                prepared by :meth:`prepare_queryset`, or this triggers a query.

        Returns:
            The pin's tag, category, and status labels.
        """
        return display_label_views(list(pin.labels.all()))

    def serialize(self, pin: Pin) -> dict[str, Any]:
        """The payload for one already-loaded pin.

        The model-instance path, for callers that hold instances anyway. The map
        uses :meth:`page`/:meth:`all` instead, which produce the identical dict
        from columns.

        Args:
            pin: A pin from a queryset prepared by :meth:`prepare_queryset`.

        Returns:
            The map payload.
        """
        labels = [LabelView.from_model(label) for label in pin.labels.all()]
        return _build_payload(
            pk=pin.pk,
            uuid=pin.uuid,
            slug=pin.slug,
            name=pin.effective_name,
            description=pin.description,
            priority=pin.priority,
            last_visited=pin.last_visited,
            latitude=pin.location.latitude,
            longitude=pin.location.longitude,
            profile_id=pin.profile_id,
            rating=getattr(pin, "map_rating", None),
            address=pin.effective_address,
            own_icon=pin.icon,
            own_custom_icon_url=pin.custom_icon.url if pin.custom_icon else None,
            own_color=pin.color,
            child_count=getattr(pin, "child_count", 0),
            cover_photo_url=self._cover_photo_url(pin),
            labels=labels,
        )

    @staticmethod
    def _cover_photo_url(pin: Pin) -> str | None:
        """The popup thumbnail: the pin's explicit cover photo, else its fallback (see _annotations)."""
        if pin.cover_photo is not None:
            return pin.cover_photo.thumb_url
        path = getattr(pin, "fallback_photo_thumbnail", None) or getattr(pin, "fallback_photo_image", None)
        return default_storage.url(path) if path else None


def _row_cover_photo_url(row: dict[str, Any]) -> str | None:
    """The projection's form of `Image.thumb_url`, falling back the same way.

    Mirrors `MapPinPayloadService._cover_photo_url`: the pin's own cover photo
    wins, through thumbnail then original then remote source, and only a pin
    without one falls through to the annotated earliest relevant photo.
    """
    if row["cover_photo_id"] is not None:
        # Having a cover photo is decided by the FK, never by whether one of its
        # URL columns is populated: a cover photo with no stored file and no
        # source URL still answers "" rather than falling through to a fallback
        # the pin's own cover photo is meant to override.
        if row["cover_photo__thumbnail"]:
            return default_storage.url(row["cover_photo__thumbnail"])
        if row["cover_photo__image"]:
            return default_storage.url(row["cover_photo__image"])
        return row["cover_photo__source_url"] or ""
    path = row["fallback_photo_thumbnail"] or row["fallback_photo_image"]
    return default_storage.url(path) if path else None
