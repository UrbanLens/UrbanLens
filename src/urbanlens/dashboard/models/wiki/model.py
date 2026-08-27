"""Wiki model - the community-editable page for a shared place.

The Wiki holds everything a community collectively knows and edits about a
place: its canonical name, description, security indicators, dates, labels,
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

from django.core.validators import MaxLengthValidator
from django.db import DatabaseError
from django.db.models import CASCADE, RESTRICT, SET_NULL, ForeignKey, Index, ManyToManyField, OneToOneField
from django.db.models.fields import BooleanField, CharField, DateField, IntegerField, SlugField, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.abstract.choices import IndoorOutdoor
from urbanlens.dashboard.models.pin.model import PinType
from urbanlens.dashboard.models.wiki.queryset import WikiManager
from urbanlens.dashboard.services.core.text_limits import MAX_WIKI_DESCRIPTION_LENGTH

if TYPE_CHECKING:
    from decimal import Decimal

    from django.db.models import Manager as DjangoManager

    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.markup.model import PinMarkup
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import TripActivity


logger = logging.getLogger(__name__)


class Wiki(abstract.VersionedModel, abstract.PublicDashboardModel, abstract.SecurityModel, abstract.AddressableModel, abstract.LabelledModel):
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
    description = TextField(null=True, blank=True, max_length=MAX_WIKI_DESCRIPTION_LENGTH, validators=[MaxLengthValidator(MAX_WIKI_DESCRIPTION_LENGTH)])

    date_abandoned = DateField(null=True, blank=True)
    date_last_active = DateField(null=True, blank=True)

    pin_type = CharField(choices=PinType.choices, default=PinType.LOCATION_MARKER, max_length=30)
    # True when ``pin_type`` was explicitly chosen by an editor - mirrors
    # Pin.pin_type_is_user_provided, and gates the same automatic
    # building/parcel classification (see services.locations.site_scope).
    pin_type_is_user_provided = BooleanField(
        default=False,
        help_text="Prevents automatic building/parcel classification from overwriting an editor-chosen type.",
    )
    # Whether this place is indoors, outdoors, or both (e.g. a building with an
    # outdoor courtyard). Left unset (None) until something actually
    # classifies it - groundwork for a future feature, not yet surfaced in
    # any UI.
    indoor_outdoor = CharField(
        max_length=10,
        choices=IndoorOutdoor.choices,
        null=True,
        blank=True,
        help_text="Whether this place is inside, outside, or both; unset when not yet classified.",
    )

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
    labels = ManyToManyField(
        "dashboard.Label",
        blank=True,
        related_name="wikis",
    )

    # The shared address/coordinate row this page displays at, and routes by
    # (/location/<slug>/wiki/). Coordinates and address are read from here.
    location = OneToOneField(
        "dashboard.Location",
        on_delete=RESTRICT,
        related_name="wiki",
    )
    # The real-world thing this page is *about*, and the unit of dedup: one
    # community page per parcel or building, however many coordinates people
    # pinned it at. Location can't do that job - two users pinning opposite
    # ends of the same property get two Locations and would get two wikis.
    # Null for a coordinate no provider knows, which behaves as it always has.
    place = OneToOneField(
        "dashboard.Place",
        on_delete=SET_NULL,
        null=True,
        blank=True,
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
    # Only meaningful on a child wiki - a detail pin - where it records who
    # placed it, and null means it was mirrored from building data. A
    # top-level page has no creator: every pinned Location gets one
    # automatically (tasks.ensure_wiki_for_location), so there is nobody to
    # attribute it to and nothing about creating one to reward.

    #: Scalar fields whose writes record provenance. Mirrors
    #: services.wiki.wiki_edits.WIKI_EDITABLE_FIELDS - the fields a person can
    #: change - plus pin_type and indoor_outdoor, which enrichment and the
    #: Consensus game both write. Declared rather than inferred so a new column
    #: does not start being versioned by accident.
    #:
    #: The reason this list matters: a concealed viewer is shown automatic
    #: writes plus their own plus their friends', so every field a person can
    #: change has to carry who changed it. See docs/designs/versioned-content.md.
    #: True only on a concealed projection built by
    #: ``services.wiki.concealment.conceal_wiki`` - a copy of this row carrying
    #: the subset of field values one viewer is entitled to see. Declared here
    #: rather than set ad hoc so the distinction is visible from the model, and
    #: so reading it is a plain attribute access with an honest type. A row
    #: loaded from the database is never concealed.
    _ul_concealed: bool = False

    #: Which viewer the projection above was built for, so re-concealing is a
    #: no-op for that viewer and a rebuild for anyone else. None means signed
    #: out, which is why this is a separate attribute rather than a falsy pk.
    _ul_concealed_for: int | None = None

    versioned_fields = (
        "name",
        "description",
        "fences",
        "alarms",
        "cameras",
        "security",
        "signs",
        "vps",
        "plywood",
        "locked",
        "date_abandoned",
        "date_last_active",
        "pin_type",
        "indoor_outdoor",
    )

    #: Where this model's field revisions are stored.
    revision_model = "dashboard.WikiFieldRevision"
    # Hero banner photo for the wiki page. Any Image tied to this wiki
    # (community gallery uploads, or a materialized Media-gallery item, see
    # services.media.media_materialize) is eligible; SET_NULL so deleting the photo
    # just drops the banner rather than the wiki. Display is further gated by
    # each viewer's own Profile.show_wiki_cover_photos preference.
    cover_photo = ForeignKey(
        "dashboard.Image",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="wiki_covers",
    )

    if TYPE_CHECKING:
        location_id: int
        place_id: int | None
        parent_wiki_id: int | None
        created_by_id: int | None
        cover_photo_id: int | None
        activities: DjangoManager[TripActivity]
        markup_items: DjangoManager[PinMarkup]

    objects = WikiManager()

    #: Memoized parcel-vs-building scope for this instance - mirrors
    #: ``Pin._site_scope_cache``; see ``services.locations.site_scope.is_site_scope``.
    _site_scope_cache: bool | None = None

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
        """Save the wiki, adopting its location's place and syncing its aliases.

        A wiki describes whatever its location stands on, so an unset ``place``
        adopts the location's on first save. Skipped when that place already
        has a wiki - the OneToOne is the dedup rule, and the right way to reach
        an existing page is ``Wiki.objects.existing_for_location``, not a
        second row racing it.

        The alias list is the full set of names the place has ever been known
        by, including the current one - so whenever a meaningful ``name`` is
        persisted, an alias row for it is ensured. External naming refreshes
        create their attributed official alias rows *before* setting the name,
        so the ``get_or_create`` here finds them instead of mislabelling them
        as user-provided. ``name`` is also sanitized to a strict character set
        before it's persisted (see ``sanitize_name``).
        """
        from urbanlens.dashboard.services.locations.naming import is_meaningful_name, sanitize_name

        update_fields = kwargs.get("update_fields")
        if update_fields is None or "name" in update_fields:
            self.name = sanitize_name(self.name) or ""
        if self.place_id is None and self.location_id and update_fields is None:
            from urbanlens.dashboard.models.location.model import Location

            place_id = Location.objects.filter(pk=self.location_id).values_list("place_id", flat=True).first()
            if place_id is not None and not type(self).objects.filter(place_id=place_id).exclude(pk=self.pk).exists():
                self.place_id = place_id
        super().save(*args, **kwargs)
        if update_fields is not None and "name" not in update_fields:
            return
        if self.name != getattr(self, "_loaded_name", None) and is_meaningful_name(self.name):
            from urbanlens.dashboard.models.aliases.model import WikiAlias

            new_name = (self.name or "").strip()
            try:
                # Case-insensitive lookup matches the alias uniqueness rule, so
                # renaming to a different casing of an existing alias reuses
                # that row instead of racing the DB constraint.
                # created_by from the write context, not left null. The alias
                # concealment rule reads `source` because created_by is null
                # for the geocoder backfill too - so a rename alias with the
                # default source=USER and no author matched neither branch and
                # was concealed from everyone, including the person who had
                # just renamed the wiki: the name showed with no alias row
                # behind it, and re-adding it by hand hit the uniqueness
                # constraint. The renamer is exactly who authored it.
                from urbanlens.dashboard.models.abstract.versioning import current_write_actor

                WikiAlias.objects.get_or_create(
                    wiki=self,
                    name__iexact=new_name,
                    defaults={"name": new_name, "created_by_id": current_write_actor()},
                )
            except DatabaseError:
                logger.exception("Could not ensure alias for wiki %s name %r", self.pk, self.name)
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

    # ------------------------------------------------------------------
    # Label helpers
    # ------------------------------------------------------------------

    def add_category(self, category_name: str, save: bool = True) -> Label | None:
        """Attach a category label to this wiki by name, creating it if needed."""
        from urbanlens.dashboard.models.labels.model import KIND_CATEGORY, Label

        category_name = category_name.lower()
        try:
            category, _created = Label.objects.get_or_create(
                name__iexact=category_name,
                kind=KIND_CATEGORY,
                # profile=None belongs in the *lookup*, not just defaults: this
                # creates a global category, so the get must find a global one.
                # Without it the get spans every profile's labels and returns
                # MultipleObjectsReturned as soon as two users have a category of
                # the same name - which the case-insensitive match below makes
                # dramatically more likely.
                profile=None,
                # Looked up case-insensitively because the uniqueness constraint is
                # (lower(name), profile, kind): an exact-match get would miss an
                # existing "Factory" while creating "factory", and the insert would
                # then violate the constraint. get_or_create cannot recover from that
                # either - its retry repeats the same exact-match get.
                defaults={"name": category_name},
            )
            if category:
                self.labels.add(category)
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
    def effective_latitude(self) -> float:
        """Wiki marker latitude, as a float - mirrors ``Pin.effective_latitude``.

        Child markers (``services.pins.pin_restructure.match_marker`` and
        friends) are typed as ``Pin | Wiki`` and read this name off whichever
        one they were handed; keeping the same name and float type here is
        what lets that code stay marker-neutral.
        """
        return float(self.location.latitude)

    @property
    def effective_longitude(self) -> float:
        """Wiki marker longitude, as a float. See ``effective_latitude``."""
        return float(self.location.longitude)

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
            Index(fields=["name"], name="idxdb_wiki_name"),
            Index(fields=["location"], name="idxdb_wiki_location"),
        ]
