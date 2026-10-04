"""Location model - shared, immutable address/coordinate record for a place."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import logging
from typing import TYPE_CHECKING, Any

from django.contrib.gis.db.models import PointField
from django.contrib.gis.geos import Point
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import SET_NULL, ForeignKey, Index
from django.db.models.fields import CharField, DateTimeField, DecimalField, SlugField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.location.queryset import LocationManager
from urbanlens.dashboard.services.locations import display

if TYPE_CHECKING:
    from collections.abc import Collection

    from django.db.models.fetch_modes import FetchMode

    from urbanlens.dashboard.models.wiki.model import Wiki


logger = logging.getLogger(__name__)


class Location(abstract.PublicDashboardModel):
    """Shared, immutable address/coordinate record for a physical place.
    A Location stores only what is derived from the address itself: coordinates, street components (via AddressableMixin), the linked GooglePlace, an external-source ``official_name``, and the cache of address-keyed external API results (``external_cache``).
    Treated as immutable: when a pin's or wiki's coordinates change we find-or-create a *different* Location instead of mutating it. - Wiki - one community page per Location (1:1); everything users edit collectively (name, description, security, labels, aliases, ...). - Pin - one row per (user, place) pair; a user's personal record.
    """

    # Stable URL routing token (each place resolves its wiki via this slug).
    slug = SlugField(max_length=255, null=True, blank=True, unique=True)

    # External-source name for this place (e.g. from Google). User edits never
    # write this field; the community-editable name lives on Wiki.name.
    official_name = CharField(max_length=255, null=True, blank=True)
    # The provider key ``official_name`` came from (a name source such as ``google_places`` or ``wikipedia``).
    # Empty when unknown: such a name is never treated as a provider's, so it mints no slug and names no wiki.
    official_name_source = CharField(max_length=50, blank=True, default="")

    latitude = DecimalField(max_digits=9, decimal_places=6)
    longitude = DecimalField(max_digits=9, decimal_places=6)

    street_number = CharField(max_length=50, null=True, blank=True)
    route = CharField(max_length=80, null=True, blank=True)
    locality = CharField(max_length=80, null=True, blank=True)
    administrative_area_level_1 = CharField(max_length=30, null=True, blank=True)
    administrative_area_level_2 = CharField(max_length=50, null=True, blank=True)
    administrative_area_level_3 = CharField(max_length=50, null=True, blank=True)
    country = CharField(max_length=100, blank=True, default="")
    zipcode = CharField(max_length=10, null=True, blank=True)
    zipcode_suffix = CharField(max_length=10, null=True, blank=True)
    point = PointField(geography=True, default=Point(0, 0))

    google_place = ForeignKey(
        "dashboard.GooglePlace",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    # The real-world parcel or building these coordinates sit on.
    # A *resolved, cached* relationship, never an identity: it is recomputed whenever the containing
    # place's geometry changes, so a provider correcting a boundary can move a location to a
    # different place without any of the pin, share, or wiki provenance keyed off this row being
    place = ForeignKey(
        "dashboard.Place",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="locations",
    )
    place_resolved_at = DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:
        google_place_id: int | None
        place_id: int | None
        wiki: Wiki

    objects = LocationManager()

    # Coordinates are this Location's identity: rows are deduplicated by (latitude, longitude), and
    # when a pin's/wiki's coordinates change we get-or-create a *different* Location rather than
    # mutating an existing one.
    # These are frozen after insert.
    IMMUTABLE_FIELDS: tuple[str, ...] = (
        "latitude",
        "longitude",
    )

    @property
    def address(self) -> str | None:
        """Full address string built from components."""
        parts = []
        if self.street_number:
            parts.append(self.street_number)
        route_has_more_after_it = bool(self.locality or self.administrative_area_level_1 or self.zipcode)
        if self.route:
            parts.append(f"{self.route}," if route_has_more_after_it else self.route)
        locality_has_more_after_it = bool(self.administrative_area_level_1 or self.zipcode)
        if self.locality:
            parts.append(f"{self.locality}," if locality_has_more_after_it else self.locality)
        if self.administrative_area_level_1:
            parts.append(self.administrative_area_level_1)
        if self.zipcode:
            parts.append(self.zipcode)
        return " ".join(parts) or None

    @property
    def address_basic(self) -> str | None:
        """Street number and route only."""
        return display.street_address(street_number=self.street_number, route=self.route)

    @property
    def address_extended(self) -> str | None:
        """Street address with city."""
        parts = []
        if self.street_number:
            parts.append(self.street_number)
        if self.route:
            parts.append(f"{self.route}," if self.locality else self.route)
        if self.locality:
            parts.append(self.locality)
        return " ".join(parts) or None

    @property
    def state(self) -> str | None:
        return self.administrative_area_level_1  # pyright: ignore[reportReturnType]

    @state.setter
    def state(self, value: str) -> None:
        self.administrative_area_level_1 = value

    @property
    def county(self) -> str | None:
        return self.administrative_area_level_2  # pyright: ignore[reportReturnType]

    @county.setter
    def county(self, value: str) -> None:
        self.administrative_area_level_2 = value

    @property
    def city(self) -> str | None:
        return self.locality  # pyright: ignore[reportReturnType]

    @city.setter
    def city(self, value: str) -> None:
        self.locality = value

    @property
    def cached_place_name(self) -> str | None:
        """Google place name from the linked cache row, if any."""
        stub = self.__dict__.get("_google_place_stub")
        if stub is not None and getattr(stub, "cached_place_name", None):
            return stub.cached_place_name
        if self.google_place_id and self.google_place is not None and self.google_place.cached_place_name:
            return self.google_place.cached_place_name
        return None

    @cached_place_name.setter
    def cached_place_name(self, value: str | None) -> None:
        """Assign a cached place name by creating or updating the shared GooglePlace row."""
        from urbanlens.dashboard.services.apis.locations.google.place_info import GooglePlaceService

        if value is None and not self.google_place_id:
            return
        service = GooglePlaceService()
        google_place = service.get_or_create_for_coordinates(
            self.latitude,
            self.longitude,
            place_name=value,
            fetch_if_missing=value is None,
        )
        if self.pk:
            self.__class__.objects.filter(pk=self.pk).update(google_place_id=google_place.pk)
        self.google_place = google_place

    @property
    def cid(self) -> Decimal | None:
        """Google Maps CID from the linked cache row, if any."""
        if self.google_place_id and self.google_place is not None and self.google_place.cid is not None:
            return self.google_place.cid
        return None

    @cid.setter
    def cid(self, value: int | Decimal | None) -> None:
        """Store a Google Maps CID on the shared cache row for these coordinates.
        ``set_cid_for_entity`` defaults to ``fetch_if_missing=True``, which made this plain-looking attribute assignment issue a live Google call - the same class of hidden synchronous request that ``place_name`` above is cache-only to avoid.
        """
        from urbanlens.dashboard.services.apis.locations.google.place_info import GooglePlaceService

        if value is None:
            return
        GooglePlaceService().set_cid_for_entity(self, value, fetch_if_missing=False)

    @property
    def place_name(self) -> str | None:
        """Cached Google place name, or None when nothing has been resolved yet.
        A plain property is the wrong place to make a synchronous external request (it fires on every pin detail page render until the cache warms, with no way to opt out or show a loading state) - see PinOverviewView.get, which dispatches tasks.resolve_location_place_name in the background instead so this stays populated for next time without ever blocking a request.
        """
        return self.cached_place_name

    @property
    def is_usa(self) -> bool:
        """Whether this location's country component identifies the USA.
        An empty country is treated as USA for display purposes: the address backfill frequently omits the country for domestic geocoding results, and the [City, State] form it selects reads fine either way.

        Returns:
            True when the country is blank or a recognized USA spelling.
        """
        return display.is_usa(self.country)

    @property
    def area_label(self) -> str | None:
        """Short human-readable area, e.g.
        ``Albany, NY`` or ``Kyiv, Ukraine``.
        USA locations render as ``City, State``; elsewhere the country replaces the state (``City, Country``), falling back to ``State, Country`` when the city is unknown.

        Returns:
            The area string, or None when no address components are available.
        """
        return display.area_label(city=self.city, state=self.state, country=self.country)

    @property
    def display_name(self) -> str:
        """Best human-readable name: the community wiki name, else the official name.
        Reads the linked Wiki when present (prefetch with ``select_related("wiki")`` in bulk to avoid an extra query per row).
        """
        try:
            wiki = self.wiki
        except ObjectDoesNotExist:
            wiki = None
        return display.display_name(
            wiki_name=wiki.name if wiki is not None else None,
            official_name=self.official_name,
            city=self.city,
            state=self.state,
            country=self.country,
        )

    def get_place_name(self) -> str | None:
        """Fetch the canonical place name from Google and cache it on GooglePlace.
        Blocks on a live API call when nothing is cached yet - safe to call from a Celery task (see tasks.resolve_location_place_name), never from a request/response cycle.
        """
        from urbanlens.dashboard.services.apis.locations.google.place_info import NO_INFORMATION, GooglePlaceService

        if self.latitude is None or self.longitude is None or not (-90 <= float(self.latitude) <= 90) or not (-180 <= float(self.longitude) <= 180):
            return NO_INFORMATION
        service = GooglePlaceService()
        google_place = service.get_or_create_for_coordinates(self.latitude, self.longitude)
        if self.pk and self.google_place_id != google_place.pk:
            self.__class__.objects.filter(pk=self.pk).update(google_place_id=google_place.pk)
            self.google_place_id = google_place.pk
            self.google_place = google_place
        return service.resolve_place_name(google_place)

    def has_place_name(self) -> bool:
        """True when the cached Google place name (if any) is useful for queries."""
        from urbanlens.dashboard.services.locations.naming import is_meaningful_name

        return is_meaningful_name(self.place_name)

    def __str__(self):
        return self.official_name or f"Location({self.pk})"

    def to_json(self) -> dict:
        """
        Returns a dictionary that can be JSON serialized.
        """
        return {
            "id": self.id,
            "official_name": self.official_name,
            "place_name": self.place_name,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "latitude": float(self.latitude),
            "longitude": float(self.longitude),
        }

    @property
    def provider_name(self) -> str | None:
        """``official_name`` when a provider is known to have supplied it, else None.

        The only name that may reach a wiki URL, a wiki's automatic name, or anyone who sees only what providers say.
        """
        return self.name_from_provider(self.official_name, self.official_name_source)

    @staticmethod
    def name_from_provider(official_name: str | None, official_name_source: str | None) -> str | None:
        """What :attr:`provider_name` is for these two column values.

        Args:
            official_name: The stored name.
            official_name_source: The provider key recorded for it.

        Returns:
            The name when a provider is recorded for it, else None.
        """
        return official_name if official_name and official_name_source else None

    def _slugify_base(self) -> str:
        # Wiki URLs are routed by this slug, so it comes from a provider's name or the uuid, never from community text.
        return self.provider_name or str(self.uuid)

    def _slug_is_taken(self, candidate: str) -> bool:
        """A slug is unavailable while another Location holds it now or held it before."""
        from urbanlens.dashboard.models.location.slug_history import LocationSlugHistory

        return super()._slug_is_taken(candidate) or LocationSlugHistory.objects.filter(slug=candidate).exclude(location_id=self.pk).exists()

    def _slug_awaits_provider_name(self) -> bool:
        """Whether a uuid slug should give way to the provider name this Location now has."""
        from urbanlens.dashboard.services.core.slugs import is_uuid_slug

        return bool(self.pk and self.provider_name and (not self.slug or is_uuid_slug(self.slug)))

    def _slug_fits_provider_name(self) -> bool:
        """Whether minting from the provider name, or the uuid when there is none, could have given the slug."""
        from urbanlens.dashboard.services.core.slugs import could_mint, is_uuid_slug

        name = self.provider_name
        if not name:
            return is_uuid_slug(self.slug)
        return could_mint(self.slug or "", name, max_length=self._slug_max_length())

    def _remint_slug(self) -> None:
        """Move the slug onto the provider name, or the uuid when there is none.

        A former slug of this Location's own that the name could have given is taken back rather than a new one
        minted, so a provider flipping between names moves between the same slugs.
        """
        from urbanlens.dashboard.models.location.slug_history import LocationSlugHistory
        from urbanlens.dashboard.services.core.slugs import could_mint

        name = self.provider_name
        if not name:
            self.slug = str(self.uuid)
        else:
            formers = LocationSlugHistory.objects.filter(location_id=self.pk).order_by("-pk").values_list("slug", flat=True)
            taken_back = next((slug for slug in formers if could_mint(slug, name, max_length=self._slug_max_length()) and not self._slug_is_taken(slug)), None)
            if taken_back is None:
                self.regenerate_slug()
                return
            self.slug = taken_back
        self.save(update_fields=["slug"])

    def _sync_wiki_slugs(self) -> None:
        """Re-mint this Location's wiki's slug, and its child wikis', where the provider name no longer gives them."""
        from urbanlens.dashboard.models.wiki.model import Wiki

        # Loaded rather than assigned this instance: assigning would replace the wiki a caller already holds
        # through ``self.wiki`` with this one.
        wiki = Wiki.objects.filter(location_id=self.pk).select_related("location", "parent_wiki__location").first()
        if wiki is None:
            return
        wiki.sync_slug_with_provider_name()
        for child in wiki.child_wikis.select_related("location"):
            child.sync_slug_with_provider_name()

    def _sync_slug_after_save(self, *, wrote_slug: bool, wrote_name: bool) -> None:
        """Keep the slug history current, and re-mint a slug the provider name could no longer have given.

        Args:
            wrote_slug: Whether the save just made wrote the slug column.
            wrote_name: Whether it wrote ``official_name`` or ``official_name_source``.
        """
        from urbanlens.dashboard.models.location.slug_history import LocationSlugHistory

        if wrote_slug:
            persisted = self.__dict__.get("_persisted_slug")
            if persisted is not None and persisted != self.slug:
                LocationSlugHistory.record(self, persisted, self.slug)
            self._persisted_slug = self.slug
        renamed = wrote_name and self.__dict__.get("_persisted_provider_name", self.provider_name) != self.provider_name
        self._persisted_provider_name = self.provider_name
        if self.__dict__.get("_reminting_slug"):
            return
        remint = (renamed and not self._slug_fits_provider_name()) or self._slug_awaits_provider_name()
        if remint:
            # The re-mint saves again, which records the slug given up through this method.
            self._reminting_slug = True
            try:
                self._remint_slug()
            finally:
                self._reminting_slug = False
        if renamed or remint:
            self._sync_wiki_slugs()

    @classmethod
    def from_db(cls, db: str | None, field_names: Collection[str], values: Collection[Any], *, fetch_mode: FetchMode | None = None) -> Location:  # noqa: ARG003
        """Stash the loaded identity-field values so ``save()`` can detect mutation.
        Capturing the originals here means the immutability check normally costs nothing extra - it compares against these instead of re-querying.

        Args:
            db: The database alias the row was loaded from.
            field_names: The field names present in ``values``.
            values: The row values, positionally aligned with ``field_names``.
            fetch_mode: Unused (kept for base-signature compatibility).

        Returns:
            The reconstructed Location instance.
        """
        instance = super().from_db(db, field_names, values)
        ordered_names = list(field_names)
        ordered_values = list(values)
        instance._immutable_originals = {name: ordered_values[ordered_names.index(name)] for name in cls.IMMUTABLE_FIELDS if name in ordered_names}  # noqa: SLF001
        if "slug" in ordered_names:
            instance._persisted_slug = ordered_values[ordered_names.index("slug")]  # noqa: SLF001
        if "official_name" in ordered_names and "official_name_source" in ordered_names:
            instance._persisted_provider_name = instance.provider_name  # noqa: SLF001
        return instance

    @staticmethod
    def _identity_values_differ(old, new) -> bool:
        """Compare two identity-field values, treating equal numbers as unchanged.

        Coordinates are ``Decimal`` columns; a caller may assign a ``float``.
        Normalising both through ``Decimal`` avoids a spurious mismatch (e.g.
        ``Decimal('40.000000')`` vs ``40.0``).
        """

        if isinstance(old, float) or isinstance(new, float):
            try:
                return Decimal(str(old)) != Decimal(str(new))
            except (InvalidOperation, ValueError, TypeError):
                return old != new
        return old != new

    def _assert_identity_unchanged(self, update_fields) -> None:
        """Raise if this save would alter any immutable identity field.

        Args:
            update_fields: The ``update_fields`` passed to ``save()`` (or ``None``
                for a full save). When given, only fields actually being written
                are checked.

        Raises:
            ValueError: If one or more identity fields differ from the persisted row.
        """
        fields = self.IMMUTABLE_FIELDS
        if update_fields is not None:
            update_set = set(update_fields)
            fields = tuple(name for name in fields if name in update_set)
            if not fields:
                return

        originals = getattr(self, "_immutable_originals", None)
        if originals is None:
            # Instance wasn't loaded via from_db (e.g. created, then re-saved):
            # fall back to a single lookup of the current row.
            originals = type(self).objects.filter(pk=self.pk).values(*self.IMMUTABLE_FIELDS).first()
            if originals is None:
                return  # Row no longer exists; let the normal save path handle it.

        changed = [name for name in fields if name in originals and self._identity_values_differ(originals[name], getattr(self, name))]
        if changed:
            raise ValueError(
                f"Location(pk={self.pk}) is immutable; refusing to change identity field(s) {changed}. "
                "Location rows are deduplicated by coordinates - use Location.objects.get_nearby_or_create() "
                "for the new coordinates instead of mutating an existing row.",
            )

    def save(self, *args, **kwargs) -> None:
        """Sanitize the official name, sync the PostGIS point, resolve the place, then mint a routing slug."""
        from urbanlens.dashboard.services.locations.naming import sanitize_name

        if self.pk is not None:
            self._assert_identity_unchanged(kwargs.get("update_fields"))

        update_fields = kwargs.get("update_fields")
        if update_fields is None or "official_name" in update_fields:
            self.official_name = sanitize_name(self.official_name)

        if self.latitude is not None and self.longitude is not None:
            lon = float(self.longitude)
            lat = float(self.latitude)
            self.point = Point(lon, lat, srid=4326)

        # A new coordinate on ground somebody has already fetched resolves immediately, from
        # geometry we hold - no provider call, and no window where a location that plainly stands on
        # a known parcel doesn't say so.
        if self.pk is None and self.place_id is None and self.latitude is not None and self.longitude is not None:
            from urbanlens.dashboard.models.place.model import Place

            self.place = Place.objects.resolve_for_point(self.latitude, self.longitude)

        writes_slug = update_fields is None or "slug" in update_fields or not self.slug
        if self.pk is not None and writes_slug and "_persisted_slug" not in self.__dict__:
            self._persisted_slug = type(self).objects.filter(pk=self.pk).values_list("slug", flat=True).first()
        writes_name = update_fields is None or bool({"official_name", "official_name_source"} & set(update_fields))
        if self.pk is not None and writes_name and "_persisted_provider_name" not in self.__dict__:
            persisted = type(self).objects.filter(pk=self.pk).values_list("official_name", "official_name_source").first()
            self._persisted_provider_name = self.name_from_provider(*persisted) if persisted else None

        super().save(*args, **kwargs)
        self._sync_slug_after_save(wrote_slug=writes_slug, wrote_name=writes_name)

    def __setattr__(self, name: str, value) -> None:
        """Support lightweight GooglePlace doubles on unsaved model instances.
        Django's foreign-key descriptor only accepts real ``GooglePlace`` model instances.
        """
        if name == "google_place" and value is not None:
            from urbanlens.dashboard.models.google_place.model import GooglePlace

            if not isinstance(value, GooglePlace) and hasattr(value, "cached_place_name"):
                self.__dict__["_google_place_stub"] = value
                self.__dict__["google_place_id"] = getattr(value, "pk", None)
                return
        super().__setattr__(name, value)

    class Meta(abstract.PublicDashboardModel.Meta):
        db_table = "dashboard_locations"
        get_latest_by = "updated"
        indexes = [
            Index(fields=["uuid"], name="idxdb_loc_uuid"),
            Index(fields=["latitude", "longitude"], name="idxdb_loc_lat_long"),
            Index(fields=["official_name"], name="idxdb_loc_offname"),
        ]
        unique_together = [
            ["latitude", "longitude"],
        ]
