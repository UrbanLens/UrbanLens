"""Pin model - a user's personal record for a location."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from django.contrib.gis.db.models import PointField
from django.contrib.gis.geos import Point
from django.core.exceptions import ObjectDoesNotExist
from django.core.validators import MaxLengthValidator
from django.db import DatabaseError
from django.db.models import (
    CASCADE,
    RESTRICT,
    SET_NULL,
    ForeignKey,
    ImageField,
    Index,
    ManyToManyField,
    Q,
    UniqueConstraint,
)
from django.db.models.fields import BooleanField, CharField, DateField, DateTimeField, DecimalField, IntegerField, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.abstract.choices import TextChoices
from urbanlens.dashboard.models.pin.queryset import PinManager
from urbanlens.dashboard.services.locations.naming import is_meaningful_name, sanitize_name
from urbanlens.dashboard.services.text_limits import MAX_PIN_DESCRIPTION_LENGTH

if TYPE_CHECKING:
    from django.db.models import Manager as DjangoManager

    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.markup.model import PinMarkup
    from urbanlens.dashboard.models.pin.note import PinNote
    from urbanlens.dashboard.models.reviews import Manager as ReviewManager
    from urbanlens.dashboard.models.visits import PinVisit

logger = logging.getLogger(__name__)

_DEDUP_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def _normalize_for_dedup(text: str) -> str:
    """Casefold and strip punctuation/whitespace differences for near-duplicate text comparison."""
    return _DEDUP_NORMALIZE_RE.sub(" ", text.casefold()).strip()


class PinType(TextChoices):
    LOCATION_MARKER = "location", "Location"
    BUILDING = "building", "Building"
    ENTRANCE = "entrance", "Entrance"
    POINT_OF_INTEREST = "poi", "Point of Interest"
    DANGER = "danger", "Danger"
    OTHER = "other", "Other"


class Pin(abstract.PublicDashboardModel, abstract.SecurityModel, abstract.AddressableModel):
    """A user's personal record for a physical location.

    Pin is the *personal* half of the two-model design:
    - Location  - one row per real-world place, shared across all users.
    - Pin       - one row per (user, place) pair; links to a Location via FK.

    A Pin belongs to exactly one Profile (user). Multiple users can each have
    their own Pin that references the same Location. Everything stored here is
    specific to that one user: their custom label, notes, visit history, status,
    priority, and the marker coordinates.
    """

    # True when ``name`` was explicitly typed by the user. External API naming
    # refreshes may replace placeholder/auto-generated labels only while this is False.
    name_is_user_provided = BooleanField(
        default=False,
        help_text="Prevents external API name refreshes from overwriting a user-entered pin name.",
    )

    # User's custom label. None = show location.display_name instead (see effective_name).
    name = CharField(max_length=255, null=True, blank=True)
    icon = CharField(max_length=255, null=True, blank=True)
    # User's personal notes. Unrelated to Location.description (place-level info).
    description = TextField(null=True, blank=True, max_length=MAX_PIN_DESCRIPTION_LENGTH, validators=[MaxLengthValidator(MAX_PIN_DESCRIPTION_LENGTH)])
    priority = IntegerField(default=0)
    vulnerability = IntegerField(default=0)
    danger = IntegerField(default=0)
    last_visited = DateTimeField(null=True, blank=True)
    # Set when the user dismisses this pin from the Memories "log your visits"
    # queue without logging a visit or clearing its visited status - keeps that
    # queue finite without affecting whether the pin counts as visited elsewhere.
    unlogged_visit_dismissed = BooleanField(default=False)
    custom_icon = ImageField(upload_to="pin_custom_icons/", null=True, blank=True)
    pin_type = CharField(choices=PinType.choices, default=PinType.LOCATION_MARKER, max_length=30)

    # Direct hex color override for this pin (e.g. "#F44336"). Used by detail pins
    # when the user explicitly picks a color in the dialog.
    color = CharField(max_length=20, null=True, blank=True)

    # Detail-pin circle styling: background fill and border around the icon.
    # Opacity stored as 0-100 integer (percent).
    detail_bg_color = CharField(max_length=20, null=True, blank=True)
    detail_bg_opacity = IntegerField(default=80)
    detail_border_color = CharField(max_length=20, null=True, blank=True)
    detail_border_opacity = IntegerField(default=100)

    date_abandoned = DateField(null=True, blank=True)
    date_last_active = DateField(null=True, blank=True)

    profile = ForeignKey(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="pins",
    )
    # The shared place this pin points at.
    location = ForeignKey(
        "dashboard.Location",
        on_delete=RESTRICT,
        related_name="pins",
    )
    wiki = ForeignKey(
        "dashboard.Wiki",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="pins",
    )
    labels = ManyToManyField(
        "dashboard.Label",
        blank=True,
        related_name="pins",
    )
    # Self-referential FK for personal detail pins (private to pin owner).
    parent_pin = ForeignKey(
        "self",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="detail_pins",
    )
    # The accepted PinShare this pin was created from, when the owner added it
    # by accepting a friend's share. Links reshares of this pin back into the
    # original share chain (see PinShare.parent_share).
    source_share = ForeignKey(
        "dashboard.PinShare",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="pins_created",
    )
    # Best-effort, heuristic link to a map-detected share that plausibly
    # explains how the owner learned about this location, for pins the owner
    # created themselves rather than by accepting a share. Populated lazily
    # (see services.map_sharing.infer_source_share_for_pin) only when the
    # owner explicitly shares this pin onward, so reshare chains still credit
    # the map that originally revealed it. Never set by _create_pin_from_share
    # - source_share covers that case exactly, and the two are never both
    # meaningful for the same pin.
    inferred_source_share = ForeignKey(
        "dashboard.PinShare",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="pins_inferred",
    )
    # Hero banner photo for the pin detail page. Any Image tied to this pin
    # (its own gallery uploads or a materialized Media-gallery item, see
    # services.media_materialize) is eligible; SET_NULL so deleting the photo
    # just drops the banner rather than the pin.
    cover_photo = ForeignKey(
        "dashboard.Image",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="pin_covers",
    )

    if TYPE_CHECKING:
        profile_id: int
        location_id: int | None
        parent_pin_id: int | None
        source_share_id: int | None
        inferred_source_share_id: int | None
        cover_photo_id: int | None
        reviews: ReviewManager
        notes: DjangoManager[PinNote]
        markup_items: DjangoManager[PinMarkup]
        visit_history: DjangoManager[PinVisit]
        wiki_id: int | None

    objects: PinManager = PinManager()  # pyright: ignore[reportIncompatibleVariableOverride]

    # ------------------------------------------------------------------
    # Name/alias invariant
    # ------------------------------------------------------------------

    @classmethod
    def from_db(cls, db, field_names, values) -> Pin:
        """Track the persisted name so ``save()`` can detect renames.

        Args:
            db: Database alias the row was loaded from.
            field_names: Names of the loaded fields.
            values: Loaded field values.

        Returns:
            The loaded Pin instance.
        """
        instance = super().from_db(db, field_names, values)
        if "name" in field_names:
            instance._loaded_name = instance.name  # noqa: SLF001
        return instance

    def save(self, *args, **kwargs) -> None:
        """Save the pin, keeping the alias list in sync with the name.

        The alias list is the full set of names a pin has ever had, including
        the current one - so whenever a meaningful ``name`` is persisted, an
        alias row for it is ensured. This single enforcement point covers
        every write path (HTMX controllers, REST serializer, Django admin),
        and also sanitizes ``name`` to a strict character set before it's
        persisted (see ``sanitize_name``).
        """
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "name" in update_fields:
            self.name = sanitize_name(self.name)
        super().save(*args, **kwargs)
        if update_fields is not None and "name" not in update_fields:
            return
        if self.name != getattr(self, "_loaded_name", None) and is_meaningful_name(self.name):
            from urbanlens.dashboard.models.aliases.model import PinAlias

            try:
                PinAlias.objects.get_or_create(pin=self, name=(self.name or "").strip())
            except DatabaseError:
                logger.debug("Could not ensure alias for pin %s name %r", self.pk, self.name, exc_info=True)
        self._loaded_name = self.name

    # ------------------------------------------------------------------
    # Hierarchy
    # ------------------------------------------------------------------

    def would_create_cycle(self, new_parent: Pin | None) -> bool:
        """Return True if ``new_parent`` becoming this pin's parent would close a loop.

        Walks ``new_parent``'s own ``parent_pin`` chain looking for this pin's pk.
        A ``visited`` guard bounds the walk to the number of distinct pins actually
        in the chain, so the check still terminates promptly even against data that
        is already corrupted with a pre-existing cycle (mirrors the cycle-safe walk
        in ``Label.get_label_and_descendants``).

        Args:
            new_parent: The pin that would be assigned to ``self.parent_pin``, or
                None (clearing the parent never creates a cycle).

        Returns:
            True if the assignment would make this pin its own ancestor.
        """
        if new_parent is None:
            return False
        if self.pk is not None and new_parent.pk == self.pk:
            return True
        visited: set[int] = set()
        current: Pin | None = new_parent
        while current is not None:
            if current.pk is None:
                return False
            if current.pk in visited:
                return False  # pre-existing cycle among ancestors, not involving self
            visited.add(current.pk)
            if self.pk is not None and current.pk == self.pk:
                return True
            current = current.parent_pin
        return False

    def ancestor_chain(self) -> list[Pin]:
        """Return this pin's ancestors, nearest parent first.

        Walks ``parent_pin`` with a visited guard so a pre-existing corrupted
        cycle terminates instead of looping (mirrors ``would_create_cycle``).

        Returns:
            The ancestor pins in order (parent, grandparent, ...); empty for a
            root pin.
        """
        chain: list[Pin] = []
        seen: set[int] = {self.pk} if self.pk is not None else set()
        current = self.parent_pin
        while current is not None and current.pk not in seen:
            seen.add(current.pk)
            chain.append(current)
            current = current.parent_pin
        return chain

    def descendants(self):
        """Return every pin nested below this one (children, grandchildren, ...).

        Returns:
            A ``PinQuerySet`` over the full subtree, excluding this pin itself.
        """
        return Pin.objects.filter(pk=self.pk).with_descendants().exclude(pk=self.pk)

    def promote_children(self) -> int:
        """Move this pin's direct children up one level, without deleting this pin.

        Children move to this pin's own parent, or become top-level pins if
        this pin has none. In the latter case, a child that shares this pin's
        own Location can't be promoted without violating the
        one-root-pin-per-Location-per-profile constraint (this pin still
        occupies that slot), so it is left in place; that conflict doesn't
        exist when there's a parent to move to instead, since only *root*
        pins are constrained by Location.

        Returns:
            The number of children actually promoted.
        """
        new_parent_id = self.parent_pin_id
        promoted = 0
        for child in Pin.objects.filter(parent_pin=self):
            if new_parent_id is None and child.location_id == self.location_id:
                continue
            if new_parent_id is not None:
                child.parent_pin_id = new_parent_id
            else:
                other_root = Pin.objects.filter(profile_id=self.profile_id, location_id=child.location_id, parent_pin__isnull=True).exclude(pk=child.pk).first()
                child.parent_pin_id = other_root.pk if other_root is not None else None
            child.save(update_fields=["parent_pin", "updated"])
            promoted += 1
        return promoted

    def backfill_wiki_link_slugs(self) -> None:
        """Ensure this pin, its location, and its wiki (if any) all have slugs.

        Legacy rows created before slug generation was automatic can have a
        blank ``Location.slug``, which silently hides the wiki create/view
        link on the pin overview partial (see its
        ``{% if pin.location and pin.location.slug %}`` guard) since the url
        can't be reversed without one. Safe to call on every request - each
        check is a no-op once the slug exists.
        """
        if self.wiki and not self.wiki.slug:
            self.wiki.ensure_slug()
        if not self.slug:
            self.slug = self.ensure_slug()
        if self.location and not self.location.slug:
            self.location.ensure_slug()

    # ------------------------------------------------------------------
    # Effective values
    # ------------------------------------------------------------------

    def icon_source_label(self) -> Label | None:
        """Label supplying the map icon, when the icon is inherited from a label."""
        if self.custom_icon or self.icon:
            return None
        for label in self.labels.exclude(kind="user").order_by("-order"):
            if label.custom_icon and not label.icon_is_overridden:
                return label
            if label.effective_icon:
                return label
        return None

    @property
    def display_label(self) -> str:
        """Human-readable label: pin name when meaningful, otherwise street address."""
        if label := self.meaningful_name:
            return label
        if self.address:
            return self.address
        if self.location and self.location.address:
            return self.location.address
        return f"{self.effective_latitude}, {self.effective_longitude}"

    @property
    def effective_address_basic(self) -> str | None:
        """Pin's own street address, or the location's, when the pin has none of its own."""
        return self.address_basic or (self.location.address_basic if self.location else None)

    @property
    def effective_city(self) -> str | None:
        """Pin's own city, or the location's, when the pin has none of its own."""
        return self.city or (self.location.city if self.location else None)

    @property
    def effective_state(self) -> str | None:
        """Pin's own state, or the location's, when the pin has none of its own."""
        return self.state or (self.location.state if self.location else None)

    @property
    def effective_county(self) -> str | None:
        """Pin's own county, or the location's, when the pin has none of its own."""
        return self.county or (self.location.county if self.location else None)

    @property
    def effective_country(self) -> str | None:
        """Pin's own country, or the location's, when the pin has none of its own."""
        return self.country or (self.location.country if self.location else None)

    @property
    def effective_address(self) -> str | None:
        """Formatted "street, city, state" address, falling back to the location's.

        A Location-linked pin's own address fields are typically blank (see
        ``effective_latitude``), so this reads from ``self.location`` whenever
        the pin doesn't have its own override.
        """
        address_basic = self.effective_address_basic
        if not address_basic:
            return None

        parts = [address_basic]
        if city := self.effective_city:
            parts.append(city)
        if state := self.effective_state:
            parts.append(state)
        return ", ".join(parts)

    @property
    def deduplicated_identity_fields(self) -> list[tuple[str, str]]:
        """(label, value) pairs for Place Name/Official Name/Address, with near-duplicate text collapsed.

        These three fields often carry the same core address text at
        different levels of formatting completeness (e.g. "123 Main St" vs.
        "123 Main St, Springfield, IL 62704, USA") rather than genuinely
        distinct information - a plain equality check only catches an exact
        match, not one string being a formatting-level superset of another.
        Keeps the most detailed version of any duplicated text and drops the
        rest, restoring Place Name / Official Name / Address display order.

        Returns:
            (label, value) pairs to render.
        """
        candidates: list[tuple[str, str]] = []
        if self.has_place_name() and self.place_name:
            candidates.append(("Place Name", self.place_name))
        if self.effective_official_name:
            candidates.append(("Official Name", self.effective_official_name))
        if self.effective_address:
            candidates.append(("Address", self.effective_address))

        kept: list[tuple[str, str]] = []
        kept_normalized: list[str] = []
        for label, value in sorted(candidates, key=lambda pair: len(pair[1]), reverse=True):
            normalized = _normalize_for_dedup(value)
            if any(normalized in existing for existing in kept_normalized):
                continue
            kept.append((label, value))
            kept_normalized.append(normalized)

        display_order = {"Place Name": 0, "Official Name": 1, "Address": 2}
        kept.sort(key=lambda pair: display_order[pair[0]])
        return kept

    def get_unique_search_name(self, *, include_country: bool = True, quote_name: bool = False, include_address: bool = True) -> str | None:
        """Name to use when searching for this location in external APIs.

        Address components fall back to the linked Location's geocoded address
        when the pin has none of its own, since a Location-linked pin's own
        address fields are typically blank (see ``effective_latitude``).

        Args:
            include_country: Whether to append the country to the query.
            quote_name: Whether to wrap the name in quotes for an exact-phrase search.
            include_address: Whether to include the street address. Some search
                engines (e.g. Wikimedia Commons) return nothing for a full
                street address but do match on name + city/state -- callers
                needing a narrower fallback query should pass False here.
        """
        name = self.meaningful_official_name or self.meaningful_name
        if not name:
            return None

        address_basic = self.effective_address_basic
        city = self.effective_city
        county = self.effective_county
        state = self.effective_state
        country = self.effective_country

        parts = [f'"{name}"' if quote_name else name]
        if include_address and address_basic and address_basic != name:
            parts.append(address_basic)

        if city:
            parts.append(city)
        elif county:
            parts.append(county)
        if state:
            parts.append(state)
        if include_country and country:
            parts.append(country)
        return " ".join(parts)

    @property
    def effective_icon(self) -> str | None:
        """Icon to display for this pin following the priority chain."""
        if self.custom_icon:
            return self.custom_icon.url
        if self.icon:
            return self.icon
        if label := self.icon_source_label():
            if label.custom_icon and not label.icon_is_overridden:
                return label.custom_icon.url
            return label.effective_icon
        return None

    @property
    def effective_color(self) -> str | None:
        """Color hex for the map icon circle, when one applies.

        Only an explicit ``pin.color`` or the label that supplies the displayed icon
        may contribute. Other labels on the pin (e.g. a yellow tag when a green
        icon tag has no color) must not produce a circle.

        Prefetch labels (with customizations) when calling in bulk (e.g. get_map_data).
        """
        if self.color:
            return self.color
        if self.custom_icon or self.icon:
            return None
        winning = self.icon_source_label()
        if winning:
            return winning.effective_color
        return None

    @property
    def effective_name(self) -> str:
        """User's custom name, or the place's community/official name."""
        return self.name or (self.location.display_name if self.location else "")

    @property
    def effective_official_name(self) -> str:
        """Externally supplied name for API lookups, falling back to the location."""
        return self.official_name or (self.location.official_name if self.location and self.location.official_name else "")

    @property
    def meaningful_official_name(self) -> str | None:
        """Official name only when it is useful for external API searches."""
        return self.effective_official_name if is_meaningful_name(self.effective_official_name) else None

    @property
    def meaningful_name(self) -> str | None:
        """The pin's name, or the location's canonical name if the pin has no name."""
        return self.effective_name if is_meaningful_name(self.effective_name) else None

    @property
    def effective_latitude(self) -> float:
        """Pin marker latitude."""
        # TODO: Delete this.
        return float(self.location.latitude)

    @property
    def effective_longitude(self) -> float:
        """Pin marker longitude."""
        # TODO: Delete this.
        return float(self.location.longitude)

    @property
    def effective_date_last_active(self):
        """Date the place was last active, inferred from date_abandoned if not set explicitly."""
        from datetime import timedelta

        if self.date_last_active is not None:
            return self.date_last_active
        if self.date_abandoned is not None:
            return self.date_abandoned - timedelta(days=1)
        return None

    @property
    def categories(self):
        """Labels of kind "category" attached to this pin."""
        return self.labels.all().categories()

    @property
    def tags(self):
        """Labels of kind "tag" attached to this pin."""
        return self.labels.all().tags()

    @property
    def statuses(self):
        """Labels of kind "status" attached to this pin."""
        return self.labels.all().statuses()

    @property
    def rating(self) -> int:
        try:
            review = self.reviews.all().latest()
            if review:
                return review.rating
        except ObjectDoesNotExist:
            logger.debug("no rating found for pin %s", self.id)
        return 0

    # ------------------------------------------------------------------
    # Category helpers (personal classification for this pin)
    # ------------------------------------------------------------------

    def change_category(self, category_id: int) -> None:
        # TODO: Assess codebase, but this is probably deprecated since the addition of Labels more generically.

        from urbanlens.dashboard.models.labels.model import Label

        category = Label.objects.get(id=category_id, kind="category")
        self.labels.remove(*self.labels.filter(kind="category"))
        self.labels.add(category)
        self.save()

    def add_category(self, category_name: str, save: bool = True) -> Label | None:
        from urbanlens.dashboard.models.labels.model import Label

        category_name = category_name.lower()
        try:
            category, _ = Label.objects.get_or_create(name=category_name, kind="category", defaults={"profile": None})
            if category:
                self.labels.add(category)
                if save:
                    self.save()
                return category
        except DatabaseError as e:
            logger.exception("failed to add category %s to pin -> %s", category_name, e)
        return None

    # ------------------------------------------------------------------
    # Serialisation / display
    # ------------------------------------------------------------------

    def __str__(self) -> str:
        status_labels = ", ".join(s.name for s in self.labels.filter(kind="status")) if self.pk else "None"

        return f"Name: {self.effective_name}\nDescription: {self.description or ''}\nPriority: {self.priority}\nLast Visited: {self.last_visited}\nStatus: {status_labels}"

    def to_json(self) -> dict[str, Any]:
        return {
            "uuid": str(self.uuid),
            "slug": self.slug or str(self.uuid),
            "name": self.effective_name,
            "official_name": self.effective_official_name,
            "icon": self.effective_icon,
            "place_name": self.place_name,
            "description": self.description,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "priority": self.priority,
            "vulnerability": self.vulnerability,
            "danger": self.danger,
            "last_visited": self.last_visited.isoformat() if self.last_visited else "never",
            "latitude": self.effective_latitude,
            "longitude": self.effective_longitude,
            "statuses": [{"id": s.id, "name": s.name, "color": s.color, "icon": s.icon} for s in self.labels.filter(kind="status")],
            "profile": self.profile.id,
            "name_is_user_provided": self.name_is_user_provided,
            "rating": self.rating,
            "color": self.effective_color,
            "tags": [{"id": t.id, "name": t.name, "color": t.effective_color, "icon": t.effective_icon} for t in self.labels.filter(kind="tag")],
        }

    def to_detail_json(self) -> dict:
        """Compact serialisation for detail-pin map markers."""
        slug = self.slug or str(self.uuid)
        return {
            "id": self.pk,
            "uuid": str(self.uuid),
            "slug": slug,
            "url": f"/dashboard/map/pin/{slug}/",
            "name": self.effective_name,
            "description": self.description or "",
            "pin_type": self.pin_type,
            "latitude": self.effective_latitude,
            "longitude": self.effective_longitude,
            "icon": self.icon or self.effective_icon,
            "color": self.effective_color,
            "bg_color": self.detail_bg_color or "",
            "bg_opacity": self.detail_bg_opacity,
            "border_color": self.detail_border_color or "",
            "border_opacity": self.detail_border_opacity,
        }

    def _slugify_base(self) -> str:
        return self.effective_name or "pin"

    def _slugify_qs(self):
        qs = Pin.objects.filter(profile_id=self.profile_id)
        if self.pk:
            qs = qs.exclude(pk=self.pk)
        return qs

    # A Pin no longer stores its own coordinates or ``point``; they are read from
    # the linked Location (see AddressableModel). Slug generation is handled by
    # PublicDashboardModel.save, so no custom save() is needed here.

    class Meta(abstract.PublicDashboardModel.Meta, abstract.SecurityModel.Meta, abstract.AddressableModel.Meta):
        db_table = "dashboard_user_pins"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["uuid"], name="idxdb_pin_uuid"),
            Index(fields=["profile"], name="idxdb_pin_profile"),
            Index(fields=["profile", "priority"], name="idxdb_pin_pfile_prio"),
            Index(fields=["profile", "last_visited"], name="idxdb_pin_pfile_lvisit"),
            Index(fields=["profile", "updated"], name="idxdb_profile_update"),
            Index(fields=["location"], name="idxdb_pin_location"),
            Index(fields=["parent_pin"], name="idxdb_pin_parent_pin"),
        ]
        constraints = [
            UniqueConstraint(
                fields=["location", "profile"],
                condition=Q(parent_pin__isnull=True),
                name="db_pin_unique_location_per_profile",
            ),
            UniqueConstraint(
                fields=["profile", "slug"],
                condition=Q(slug__isnull=False),
                name="db_pin_unique_slug_per_profile",
            ),
        ]
