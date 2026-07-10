"""Wiki model - the community-editable page for a shared place.

The Wiki holds everything a community collectively knows and edits about a
place: its canonical name, description, security indicators, dates, badges,
aliases, comments, photos, child wikis (community detail markers, via the
self-referential ``parent_wiki``) and edit history.  It links to a
:class:`~urbanlens.dashboard.models.location.model.Location` for its current
address/coordinates via a ``OneToOneField``.

Address and coordinate data never live here - they are read-only proxies that
delegate to ``self.location``.  When a wiki's coordinates or address change we
find-or-create a *different* Location for the new coordinates and repoint
``self.location`` rather than mutating the shared Location row.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from django.db import DatabaseError
from django.db.models import CASCADE, RESTRICT, SET_NULL, ForeignKey, Index, ManyToManyField, OneToOneField
from django.db.models.fields import BooleanField, CharField, DateField, IntegerField, SlugField, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.pin.model import PinType
from urbanlens.dashboard.models.wiki.queryset import WikiManager

if TYPE_CHECKING:
    from decimal import Decimal

    from django.db.models import Manager as DjangoManager

    from urbanlens.dashboard.models.badges.model import Badge
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.markup.model import PinMarkup
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import TripActivity


logger = logging.getLogger(__name__)


class Wiki(abstract.PublicDashboardModel, abstract.SecurityModel, abstract.AddressableModel):
    """Community-editable page describing a shared, real-world place.

    Wiki is the *community* half of the place model:
    - Location - one row per real-world address, shared and treated as immutable.
    - Wiki     - one community page per Location (1:1); everything users edit.

    A Wiki is never user-specific. The security indicators (fences, alarms, ...)
    are inherited from :class:`SecurityModel`. Coordinates and address are read
    from ``self.location`` via the proxy properties below.

    What does NOT belong here:
    - Coordinates / street address / Google place metadata -> Location
    - A single user's personal label, notes, or visit history -> Pin
    """

    # Global uniqueness: each community page has one canonical slug.
    slug = SlugField(max_length=255, null=True, blank=True, unique=True)

    # Canonical community name of the place (was Location.name).
    name = CharField(max_length=255)
    description = TextField(null=True, blank=True)

    date_abandoned = DateField(null=True, blank=True)
    date_last_active = DateField(null=True, blank=True)

    pin_type = CharField(choices=PinType.choices, default=PinType.LOCATION_MARKER, max_length=30)

    # Direct hex color override for this wiki's map marker (e.g. "#F44336").
    # Only meaningful for a child wiki (see parent_wiki below).
    color = CharField(max_length=20, null=True, blank=True)
    icon = CharField(max_length=255, null=True, blank=True)

    # Child-wiki circle styling: background fill and border around the marker
    # icon. Opacity stored as 0-100 integer (percent).
    detail_bg_color = CharField(max_length=20, null=True, blank=True)
    detail_bg_opacity = IntegerField(default=80)
    detail_border_color = CharField(max_length=20, null=True, blank=True)
    detail_border_opacity = IntegerField(default=100)

    # Shared taxonomy - the real-world place's type, visible to all users.
    badges = ManyToManyField(
        "dashboard.Badge",
        blank=True,
        related_name="wikis",
    )

    # The shared address/coordinate row this page describes.
    location = OneToOneField(
        "dashboard.Location",
        on_delete=RESTRICT,
        related_name="wiki",
    )
    # Self-referential FK for community sub-markers ("child wikis") nested
    # within a parent wiki's page - buildings, entrances, points of interest,
    # hazards, etc. Mirrors Pin.parent_pin (see that field's docstring); never
    # allowed to nest into a cycle (see would_create_cycle).
    parent_wiki = ForeignKey(
        "self",
        on_delete=CASCADE,
        null=True,
        blank=True,
        related_name="child_wikis",
    )

    # Attribution only - deleting the creator's profile does not cascade-delete
    # the wiki. Used solely to gate self-service deletion (see can_be_deleted_by).
    created_by = ForeignKey(
        "dashboard.Profile",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="created_wikis",
    )
    # Flips true the first time a profile other than created_by views the
    # wiki page (see LocationWikiView.get). Once true, the wiki is community
    # content and its creator can no longer unilaterally delete it.
    viewed_by_other = BooleanField(default=False)

    if TYPE_CHECKING:
        location_id: int
        parent_wiki_id: int | None
        created_by_id: int | None
        activities: DjangoManager[TripActivity]
        markup_items: DjangoManager[PinMarkup]

    objects = WikiManager()

    # ------------------------------------------------------------------
    # Name/alias invariant
    # ------------------------------------------------------------------

    @classmethod
    def from_db(cls, db, field_names, values) -> Wiki:
        """Track the persisted name so ``save()`` can detect renames.

        Args:
            db: Database alias the row was loaded from.
            field_names: Names of the loaded fields.
            values: Loaded field values.

        Returns:
            The loaded Wiki instance.
        """
        instance = super().from_db(db, field_names, values)
        if "name" in field_names:
            instance._loaded_name = instance.name  # noqa: SLF001
        return instance

    def save(self, *args, **kwargs) -> None:
        """Save the wiki, keeping the alias list in sync with the name.

        The alias list is the full set of names the place has ever been known
        by, including the current one - so whenever a meaningful ``name`` is
        persisted, an alias row for it is ensured. External naming refreshes
        create their attributed official alias rows *before* setting the name,
        so the ``get_or_create`` here finds them instead of mislabelling them
        as user-provided.
        """
        from urbanlens.dashboard.services.locations.naming import is_meaningful_name

        super().save(*args, **kwargs)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "name" not in update_fields:
            return
        if self.name != getattr(self, "_loaded_name", None) and is_meaningful_name(self.name):
            from urbanlens.dashboard.models.aliases.model import WikiAlias

            try:
                WikiAlias.objects.get_or_create(wiki=self, name=(self.name or "").strip())
            except DatabaseError:
                logger.debug("Could not ensure alias for wiki %s name %r", self.pk, self.name, exc_info=True)
        self._loaded_name = self.name

    # ------------------------------------------------------------------
    # Hierarchy
    # ------------------------------------------------------------------

    def would_create_cycle(self, new_parent: Wiki | None) -> bool:
        """Return True if ``new_parent`` becoming this wiki's parent would close a loop.

        Mirrors ``Pin.would_create_cycle``: walks ``new_parent``'s own
        ``parent_wiki`` chain looking for this wiki's pk. A ``visited`` guard
        bounds the walk to the number of distinct wikis actually in the
        chain, so the check still terminates promptly even against data that
        is already corrupted with a pre-existing cycle.

        Args:
            new_parent: The wiki that would be assigned to ``self.parent_wiki``,
                or None (clearing the parent never creates a cycle).

        Returns:
            True if the assignment would make this wiki its own ancestor.
        """
        if new_parent is None:
            return False
        if self.pk is not None and new_parent.pk == self.pk:
            return True
        visited: set[int] = set()
        current: Wiki | None = new_parent
        while current is not None:
            if current.pk is None:
                return False
            if current.pk in visited:
                return False  # pre-existing cycle among ancestors, not involving self
            visited.add(current.pk)
            if self.pk is not None and current.pk == self.pk:
                return True
            current = current.parent_wiki
        return False

    # ------------------------------------------------------------------
    # Self-service deletion
    # ------------------------------------------------------------------

    def can_be_deleted_by(self, profile: Profile) -> bool:
        """Whether ``profile`` may delete this wiki outright.

        Only the profile that created the wiki may do so, and only before
        anyone else has viewed it - once another profile has seen the page,
        it's community content and deletion should go through normal
        moderation rather than a unilateral self-service action.

        Args:
            profile: The profile requesting deletion.

        Returns:
            True if ``profile`` created this wiki and no one else has viewed it.
        """
        return self.created_by_id is not None and self.created_by_id == profile.id and not self.viewed_by_other

    # ------------------------------------------------------------------
    # Badge helpers
    # ------------------------------------------------------------------

    @property
    def categories(self):
        """Badges of kind "category" attached to this wiki."""
        return self.badges.all().categories()

    @property
    def tags(self):
        """Badges of kind "tag" attached to this wiki."""
        return self.badges.all().tags()

    @property
    def statuses(self):
        """Badges of kind "status" attached to this wiki."""
        return self.badges.all().statuses()

    def add_category(self, category_name: str, save: bool = True) -> Badge | None:
        """Attach a category badge to this wiki by name, creating it if needed."""
        from urbanlens.dashboard.models.badges.model import Badge

        category_name = category_name.lower()
        try:
            category, _created = Badge.objects.get_or_create(name=category_name, kind="category", defaults={"profile": None})
            if category:
                self.badges.add(category)
                if save:
                    self.save()
                return category
        except DatabaseError as e:
            logger.exception("failed to add category %s to wiki -> %s", category_name, e)
        return None

    # ------------------------------------------------------------------
    # Derived values
    # ------------------------------------------------------------------

    @property
    def effective_date_last_active(self):
        """Date the place was last active, inferred from date_abandoned if unset."""
        if self.date_last_active is not None:
            return self.date_last_active
        if self.date_abandoned is not None:
            return self.date_abandoned - timedelta(days=1)
        return None

    def get_unique_search_name(self, *, include_country: bool = True) -> str | None:
        """Name to use when searching for this place in external APIs."""
        name = self.official_name or self.name
        if not name:
            return None

        parts = [name]
        if self.address_basic and self.address_basic != name:
            parts.append(self.address_basic)

        if self.city:
            parts.append(self.city)
        elif self.county:
            parts.append(self.county)
        if self.state:
            parts.append(self.state)
        if include_country and self.country:
            parts.append(self.country)
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Serialisation / display
    # ------------------------------------------------------------------

    def __str__(self):
        return self.name or f"Wiki({self.pk})"

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict for this wiki."""
        latitude = self.latitude
        longitude = self.longitude
        return {
            "id": self.id,
            "name": self.name,
            "official_name": self.official_name,
            "place_name": self.place_name,
            "description": self.description,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "latitude": float(latitude) if latitude is not None else None,
            "longitude": float(longitude) if longitude is not None else None,
        }

    def to_detail_json(self) -> dict:
        """Compact serialisation for child-wiki map markers."""
        return {
            "uuid": str(self.uuid),
            "name": self.name,
            "description": self.description or "",
            "pin_type": self.pin_type,
            "latitude": float(self.latitude) if self.latitude is not None else None,
            "longitude": float(self.longitude) if self.longitude is not None else None,
            "icon": self.icon,
            "color": self.color,
            "bg_color": self.detail_bg_color or "",
            "bg_opacity": self.detail_bg_opacity,
            "border_color": self.detail_border_color or "",
            "border_opacity": self.detail_border_opacity,
        }

    def _slugify_base(self) -> str:
        return self.name or "wiki"

    class Meta(abstract.PublicDashboardModel.Meta, abstract.SecurityModel.Meta, abstract.AddressableModel.Meta):
        db_table = "dashboard_wikis"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["uuid"], name="idxdb_wiki_uuid"),
            Index(fields=["name"], name="idxdb_wiki_name"),
            Index(fields=["location"], name="idxdb_wiki_location"),
            Index(fields=["parent_wiki"], name="idxdb_wiki_parent_wiki"),
        ]
