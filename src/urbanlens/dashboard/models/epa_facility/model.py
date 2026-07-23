"""EpaFacility model - persistent, project-wide record of EPA ECHO facility data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import IntegrityError, models
from django.utils import timezone

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.epa_facility.queryset import EpaFacilityManager

if TYPE_CHECKING:
    from collections.abc import Iterable


class EpaFacility(abstract.DashboardModel):
    """A single EPA-regulated facility, keyed by its FRS Registry ID and persisted indefinitely.

    Every facility UrbanLens ever discovers via EPA ECHO (whether from a
    nearby-facility search or a full Detailed Facility Report lookup) is
    recorded here, project-wide - not scoped to any one pin or user. A
    facility discovered while checking one pin's exact-site match becomes
    instantly reusable for any OTHER pin near it, without spending any of
    ECHO's tightly rate-limited API budget again (see ``epa_echo.py``'s
    ``_fetch_epa_echo_data``, which routinely exhausts that budget after only
    2-3 calls per fetch). This is reference data, not a time-limited cache: a
    facility's registry ID and physical coordinates don't change, so rows are
    never expired or re-fetched automatically - only enriched further as more
    is learned about them.

    ``latitude``/``longitude`` are only populated once a Detailed Facility
    Report has actually been fetched (``detail_fetched_at`` set) - ECHO's
    nearby-search listing alone includes a latitude but not a longitude (see
    ``EpaEchoGateway.get_nearby_facilities``), so a search-only row can't yet
    support a real distance check. A row with ``detail_fetched_at`` set but
    still-null coordinates is also meaningful: ECHO's DFR genuinely has no
    coordinates for that facility (no Permits data), so it can never be an
    exact-site match - recording that saves re-fetching it to re-rule it out.
    """

    registry_id = models.CharField(max_length=50, unique=True, db_index=True)
    name = models.CharField(max_length=255, blank=True, default="")
    address = models.CharField(max_length=255, blank=True, default="")
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    #: Merged normalized facility fields - whatever combination of search-result
    #: and Detailed Facility Report data is currently known (compliance_status,
    #: significant_violator, programs, ...). See _fetch_epa_echo_data for the
    #: exact shape written here.
    data = models.JSONField(default=dict)
    #: When the Detailed Facility Report (with real coordinates) was last
    #: fetched. None means this row only reflects a nearby-search listing so
    #: far - not a staleness marker, just "do we have real coordinates yet".
    detail_fetched_at = models.DateTimeField(null=True, blank=True)

    objects: EpaFacilityManager = EpaFacilityManager()

    def __str__(self) -> str:
        return f"EpaFacility: {self.name or self.registry_id}"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_epa_facility"
        indexes = [
            models.Index(fields=["latitude", "longitude"], name="idxdb_epafac_lat_lng"),
        ]

    @classmethod
    def known_details_by_registry_id(cls, registry_ids: Iterable[str]) -> dict[str, EpaFacility]:
        """Return already-fetched DFR detail rows (real coordinates known) for the given IDs.

        Args:
            registry_ids: EPA FRS Registry IDs to look up.

        Returns:
            Mapping of registry_id -> EpaFacility for whichever of those IDs
            already have a Detailed Facility Report on file - reusable
            without spending any more of ECHO's rate-limited API budget.
        """
        ids = [rid for rid in registry_ids if rid]
        if not ids:
            return {}
        return {row.registry_id: row for row in cls.objects.filter(registry_id__in=ids, detail_fetched_at__isnull=False)}

    @classmethod
    def record_search_result(cls, registry_id: str, *, name: str, address: str, latitude: float | None, data: dict[str, Any]) -> None:
        """Upsert a facility from a nearby-search result.

        Never overwrites an existing row's coordinates or ``detail_fetched_at``
        - a richer, already-fetched Detailed Facility Report must survive a
        later search-only sighting of the same facility.

        Args:
            registry_id: EPA FRS Registry ID.
            name: Facility name.
            address: Facility address.
            latitude: The search result's FacLat, if present.
            data: Normalized search-result fields to merge into ``data``.
        """
        if not registry_id:
            return
        entry, created = cls._get_or_create_row(registry_id, defaults={"name": name, "address": address, "latitude": latitude, "data": data})
        if created:
            return
        entry.name = name or entry.name
        entry.address = address or entry.address
        if entry.latitude is None and latitude is not None:
            entry.latitude = latitude
        entry.data = {**entry.data, **data}
        entry.save(update_fields=["name", "address", "latitude", "data", "updated"])

    @classmethod
    def record_detail_result(cls, registry_id: str, *, name: str, address: str, latitude: float | None, longitude: float | None, data: dict[str, Any]) -> EpaFacility:
        """Upsert a facility's fetched Detailed Facility Report.

        A DFR without coordinates (ECHO has no Permits data for some
        facilities) is still recorded - ``detail_fetched_at`` marks it as
        "already checked, can never be an exact-site match" so future fetches
        never re-spend rate-limited budget on it - but ``None`` coordinates
        never overwrite real ones already on the row (e.g. a search-derived
        latitude, or a previous richer DFR).

        Args:
            registry_id: EPA FRS Registry ID.
            name: Facility name.
            address: Facility address.
            latitude: The DFR's exact latitude, when it has one.
            longitude: The DFR's exact longitude, when it has one.
            data: Normalized detail fields (e.g. ``programs``) to merge into ``data``.

        Returns:
            The saved EpaFacility instance.
        """
        entry, created = cls._get_or_create_row(
            registry_id,
            defaults={"name": name, "address": address, "latitude": latitude, "longitude": longitude, "data": data, "detail_fetched_at": timezone.now()},
        )
        if created:
            return entry
        entry.name = name or entry.name
        entry.address = address or entry.address
        if latitude is not None:
            entry.latitude = latitude
        if longitude is not None:
            entry.longitude = longitude
        entry.data = {**entry.data, **data}
        entry.detail_fetched_at = timezone.now()
        entry.save(update_fields=["name", "address", "latitude", "longitude", "data", "detail_fetched_at", "updated"])
        return entry

    @classmethod
    def _get_or_create_row(cls, registry_id: str, *, defaults: dict[str, Any]) -> tuple[EpaFacility, bool]:
        """``get_or_create`` hardened against the concurrent-fetch race.

        The two EPA panel sources deliberately share one upstream fetch and can
        briefly run concurrently (see ``epa_echo.py``'s module docstring), so
        two workers may both miss the ``get`` and race the ``create`` - the
        loser's ``IntegrityError`` on the unique ``registry_id`` must resolve
        to the winner's row instead of aborting its whole panel fetch.

        Args:
            registry_id: EPA FRS Registry ID (the unique key raced on).
            defaults: Field values used only when creating.

        Returns:
            Same ``(instance, created)`` tuple as ``get_or_create``.
        """
        try:
            return cls.objects.get_or_create(registry_id=registry_id, defaults=defaults)
        except IntegrityError:
            return cls.objects.get(registry_id=registry_id), False
