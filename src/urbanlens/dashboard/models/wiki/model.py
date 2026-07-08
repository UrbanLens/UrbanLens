"""Wiki model - the community-editable page for a shared place.

The Wiki holds everything a community collectively knows and edits about a
place: its canonical name, description, security indicators, dates, badges,
aliases, comments, photos, community detail pins and edit history.  It links
to a :class:`~urbanlens.dashboard.models.location.model.Location` for its
current address/coordinates via a ``OneToOneField``.

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
from django.db.models import RESTRICT, SET_NULL, Index, ManyToManyField, OneToOneField
from django.db.models.fields import CharField, DateField, SlugField, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.wiki.queryset import WikiManager

if TYPE_CHECKING:
    from decimal import Decimal

    from django.db.models import Manager as DjangoManager

    from urbanlens.dashboard.models.badges.model import Badge
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.markup.model import PinMarkup
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

    # Shared taxonomy - the real-world place's type, visible to all users.
    badges = ManyToManyField(
        "dashboard.Badge",
        blank=True,
        related_name="wikis",
    )

    # The shared address/coordinate row this page describes. SET_NULL so a
    # Location can be removed without cascade-deleting community content.
    location = OneToOneField(
        "dashboard.Location",
        on_delete=RESTRICT,
        related_name="wiki",
    )

    if TYPE_CHECKING:
        location_id: int
        activities: DjangoManager[TripActivity]
        markup_items: DjangoManager[PinMarkup]

    objects = WikiManager()

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

    def _slugify_base(self) -> str:
        return self.name or "wiki"
    
    class Meta(abstract.PublicDashboardModel.Meta, abstract.SecurityModel.Meta, abstract.AddressableModel.Meta):
        db_table = "dashboard_wikis"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["uuid"], name="idxdb_wiki_uuid"),
            Index(fields=["name"], name="idxdb_wiki_name"),
            Index(fields=["location"], name="idxdb_wiki_location"),
        ]
