"""Historic-register plugin: what the state and city inventories say about a pin, via REData.
UrbanLens reached exactly one of them, New York's CRIS, and only inside New York."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.pins.external_data import OverviewSummary, PanelPlacement
from urbanlens.dashboard.services.pins.redata_panel import RedataInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: Providers with a panel of their own, so including them here would show the same record twice under
#: a vaguer heading.
#: A fact about *this app's* UI, which is what makes it safe to write down - unlike the provider list
#: itself, which is REData's and is discovered.
_SHOWN_ELSEWHERE: frozenset[str] = frozenset({"ny_cris"})

#: Display names for the registers, because their tags are acronyms that title-case badly ("Md
#: Mihp"). **Never a gate** - a provider missing from here still renders, under a title-cased tag.
_REGISTER_LABELS: dict[str, str] = {
    "nps_nrhp": "National Register of Historic Places",
    "ma_mhc": "Massachusetts MHC Inventory",
    "tx_thc": "Texas Historical Commission",
    "nc_hpo": "North Carolina HPO",
    "wa_dahp": "Washington DAHP",
    "va_dhr": "Virginia DHR",
    "md_mihp": "Maryland MIHP",
    "oh_shpo": "Ohio SHPO Inventory",
    "oh_shpo_bridges": "Ohio SHPO Historic Bridges",
    "in_shaard": "Indiana SHAARD",
    "al_register": "Alabama Register of Landmarks",
    "mpls_hpc": "Minneapolis HPC",
    "denver_lpc": "Denver Landmarks",
    "detroit_local_historic": "Detroit Local Historic Districts",
    "baltimore_chap": "Baltimore CHAP",
    "arc_historic_resources": "Atlanta Regional Commission",
    "la_county_historic": "Los Angeles County Historic Resources",
    "dc_landmarks": "DC Historic Landmarks",
    "syracuse_historic": "Syracuse Historic Properties",
    "fort_myers_historic": "Fort Myers Historic Inventory",
    "sjc_historic_structures": "St. Johns County Historic Structures",
    "chesterfield_landmarks": "Chesterfield County Landmarks",
    "boise_historic": "Boise Historic Landmarks",
    "slc_historic": "Salt Lake City Historic Register",
}

#: The register the Overview names outright, since a listing on it is the one most readers recognize.
_NATIONAL_REGISTER = "nps_nrhp"

#: ``resource_type`` values whose rows describe no property.
#: An archaeological buffer marks a sensitivity zone - REData publishes only an ``OBJECTID`` and a
#: geometry for it, deliberately, so there is nothing to render and naming one would disclose a site
#: location this app has no business surfacing.
_NOT_A_DESCRIPTION: frozenset[str] = frozenset({"archaeological_buffer_area"})

#: Rows shown before the list is truncated. A survey of a large campus can list
#: a hundred structures; the panel is a summary, not the inventory.
_MAX_ROWS = 10

#: Fields kept from each row.
#: The rest of ``CulturalResourceSerializer`` - ``attributes``, ``detail_payload``, ``geometry``, the searched-from
#: coordinate - is either per-provider, large, or both. ``external_id`` is NPS's reference number on a National
#: Register row, and the resource's own point says which building it is.
_KEPT_FIELDS = ("provider", "resource_type", "scope", "name", "status", "year_built", "architectural_style", "use_type", "contains_point", "external_id", "source_latitude", "source_longitude")


def register_label(provider: str) -> str:
    """A human name for a register tag.

    Args:
        provider: REData's provider tag, e.g. ``"md_mihp"``.

    Returns:
        The written-down name when there is one, else the tag with underscores turned to spaces and title-cased."""
    return _REGISTER_LABELS.get(provider) or provider.replace("_", " ").title()


def register_rows(resources: list[Any]) -> list[dict[str, str]]:
    """Normalize REData cultural-resource rows into display rows.
    Reads only the fields REData standardizes across every provider - never the per-provider ``attributes`` blob, which is what ties ``cris_buildings`` to one inventory.

    Args:
        resources: Cached ``CulturalResourceSerializer``-shaped rows.

    Returns:
        ``{"register", "name", "detail", "scope"}`` dicts, plus ``href`` (NPS's record) on a National Register row with a reference number, nearest-first order preserved, skipping rows that would render as an unlabelled blank and rows that describe no property."""
    from urbanlens.dashboard.services.locations.national_register import nps_record_url, reference_number

    rows: list[dict[str, str]] = []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        if str(resource.get("resource_type") or "") in _NOT_A_DESCRIPTION:
            continue
        name = str(resource.get("name") or "").strip()
        if not name:
            continue
        facts = [str(resource.get(key) or "").strip() for key in ("year_built", "use_type", "architectural_style", "status")]
        reference = reference_number(resource)
        if reference:
            facts.append(f"#{reference}")
        row = {
            "register": register_label(str(resource.get("provider") or "")),
            "name": name,
            "detail": ", ".join(fact for fact in facts if fact),
            "scope": str(resource.get("scope") or ""),
        }
        if url := nps_record_url(reference):
            row["href"] = url
        rows.append(row)
    return rows


def _name_words(name: str) -> set[str]:
    """The words of a name worth matching on; short ones ("the", "of") match everything."""
    return {word for word in re.findall(r"[a-z0-9]+", name.lower()) if len(word) >= 4}


def _national_register_listings(pin: Pin, resources: list[dict[str, Any]], *, one_building: bool) -> list[dict[str, Any]]:
    """The National Register rows to choose this place's listing from, with CRIS's site record when REData missed it."""
    from urbanlens.dashboard.services.locations import register_names
    from urbanlens.dashboard.services.locations.national_register import cris_lists_building

    listings = [resource for resource in resources if isinstance(resource, dict) and resource.get("provider") == _NATIONAL_REGISTER and str(resource.get("name") or "").strip()]
    if not any(resource.get("contains_point") is True for resource in listings) and pin.location is not None:
        # REData's near-point search can miss the listing CRIS's site record says holds the pin. One building of
        # that site is the listing's only when CRIS's record of the building says so.
        if (cris_name := register_names.cris_register_listing(pin.location)) and (not one_building or cris_lists_building(pin.location)):
            listings = [{"provider": _NATIONAL_REGISTER, "name": cris_name, "status": "Listed", "scope": "site", "contains_point": True}, *listings]
    return listings


def _best_listing(pin: Pin, listings: list[dict[str, Any]]) -> dict[str, Any]:
    """The listing most plausibly this place, from a non-empty list - see :func:`national_register_note`."""
    place_words = _name_words(pin.location.official_name or "") if pin.location else set()
    return min(
        enumerate(listings),
        key=lambda item: (item[1].get("contains_point") is not True, item[1].get("scope") != "site", -len(place_words & _name_words(str(item[1]["name"]))), item[0]),
    )[1]


def national_register_reference(pin: Pin, resources: list[dict[str, Any]], *, one_building: bool = False) -> str | None:
    """NPS's reference number for the listing :func:`national_register_note` names, when one was published."""
    from urbanlens.dashboard.services.locations.national_register import reference_number

    listings = _national_register_listings(pin, resources, one_building=one_building)
    return reference_number(_best_listing(pin, listings)) if listings else None


def national_register_note(pin: Pin, resources: list[dict[str, Any]], *, one_building: bool = False) -> str | None:
    """Name the National Register listing that is most plausibly this place.

    A near-point search also finds neighbours' listings, so one whose boundary holds the pin wins, then a site-level
    record, then the one sharing the most words with the place's name, then the nearest.

    Args:
        pin: The pin, for its location's name and CRIS site record.
        resources: The cached register rows.
        one_building: Whether the pin stands for one building of a larger site.

    Returns:
        One sentence, or None when no National Register listing is known here.
    """
    listings = _national_register_listings(pin, resources, one_building=one_building)
    if not listings:
        return None
    best = _best_listing(pin, listings)
    place_words = _name_words(pin.location.official_name or "") if pin.location else set()
    name = str(best["name"]).strip()
    status = str(best.get("status") or "").strip()
    register = register_label(_NATIONAL_REGISTER)
    if best.get("contains_point") is False and not place_words & _name_words(name):
        return f"The nearest listing on the {register} is \u201c{name}\u201d"
    if not status or status.lower() == "listed":
        note = f"Listed on the {register} as \u201c{name}\u201d"
    else:
        note = f"On the {register} as \u201c{name}\u201d ({status})"
    others = len(listings) - 1
    if others:
        note += f", with {others} other listing{'s' if others != 1 else ''} nearby"
    return note


def _meta_entry(row: dict[str, str]) -> dict[str, str]:
    """One register row as a card metadata entry, linked when it carries NPS's record."""
    entry = {"label": row["register"], "value": f"{row['name']} - {row['detail']}" if row["detail"] else row["name"]}
    if row.get("href"):
        entry["href"] = row["href"]
    return entry


class HistoricRegisterPanelSource(RedataInfoPanelSource):
    """Every historic register that names this place, from REData's whole registry."""

    key = "redata_historic_registers"
    cache_source = "redata_historic_registers"
    section_id = "historic-registers-section"
    icon = "history_edu"
    title = "Historic Registers"
    placement: ClassVar[PanelPlacement] = PanelPlacement.PROPERTY
    #: New York's CRIS card is shown inside this tab (``CrisBuildingPanelSource.shown_in``).
    tab_label: ClassVar[str] = "Historic Preservation"
    building_level: ClassVar[bool] = True
    tab_order: ClassVar[int] = 40
    payload_key: ClassVar[str] = "resources"
    #: A surveyed block and an unlisted field both fetch successfully; only one
    #: has a tab worth showing.
    inspects_content: ClassVar[bool] = True

    def fetch_envelope(self, latitude: float, longitude: float) -> LocationContextEnvelope:
        """Ask every register covering this point except the ones with their own panel."""
        from urbanlens.dashboard.services.apis.locations.redata_cultural_resources_gateway import RedataCulturalResourcesGateway, applicable_provider_tags

        wanted = [tag for tag in applicable_provider_tags(latitude, longitude) if tag not in _SHOWN_ELSEWHERE]
        if not wanted:
            # Nothing covers this point.
            # An empty envelope rather than an unfiltered request: naming no provider runs every
            # register in the registry, which is the one outcome the capability lookup exists to
            # avoid.
            from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope as Envelope

            return Envelope(count=0, complete=True, results=[], providers=[])

        from urbanlens.dashboard.services.locations.register_names import CONTAINS_POINT_KEY, geometry_contains_point

        envelope = RedataCulturalResourcesGateway().near_resources(latitude, longitude, provider=wanted)
        for row in envelope.results:
            if isinstance(row, dict):
                # Kept in place of the geometry, which is not cached: only a listing containing the point may name it.
                row[CONTAINS_POINT_KEY] = geometry_contains_point(row.get("geometry"), latitude, longitude)
        return envelope

    def transform_rows(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep only the standardized fields this card renders - see :data:`_KEPT_FIELDS`."""
        return [{key: row.get(key) for key in _KEPT_FIELDS} for row in results if isinstance(row, dict)]

    def landed(self, pin: Pin, data: dict) -> None:
        """Link this place's own National Register listings - see :meth:`link_own_listings`."""
        self._link_listings(pin, data)

    def link_own_listings(self, pin: Pin) -> None:
        """Add NPS's record of each National Register listing that is this pin's own to its links and its wiki's.

        Args:
            pin: The pin whose cached rows to read.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        if (row := LocationCache.get_fresh(pin.location, self.cache_source)) is not None:
            self._link_listings(pin, row.data)

    def _link_listings(self, pin: Pin, data: dict | None) -> None:
        from urbanlens.dashboard.services.locations.national_register import containing_listings, link_listings

        one_building = self.one_building_of_a_site(pin)
        resources = self.own_resources(pin, data, one_building=one_building)
        link_listings(pin, resources if one_building else containing_listings(pin.location, resources))

    def one_building_of_a_site(self, pin: Pin) -> bool:
        """Whether the pin stands for one building nested under a site on another location."""
        from urbanlens.dashboard.services.locations.site_scope import is_site_scope

        return not is_site_scope(pin) and self.nesting_site(pin) is not None

    def own_resources(self, pin: Pin, data: dict | None, *, one_building: bool) -> list[dict[str, Any]]:
        """The cached rows describing this pin: for one building of a site, only that building's own records.

        Args:
            pin: The pin being read.
            data: Its cached payload.
            one_building: :meth:`one_building_of_a_site` for the pin.

        Returns:
            The rows, in REData's order.
        """
        from urbanlens.dashboard.services.locations.national_register import building_register_rows

        resources = [row for row in (data or {}).get(self.payload_key) or [] if isinstance(row, dict)]
        return building_register_rows(pin.location, resources) if one_building else resources

    def has_content(self, data: dict | None) -> bool:
        """A row with no name renders nothing worth a tab."""
        return bool(register_rows((data or {}).get(self.payload_key) or []))

    def overview_summary(self, pin: Pin, data: dict) -> OverviewSummary | None:
        """This place's own register listings, for the Property Records Overview.

        Only a record that is the place's own counts, by the rule its links follow: for one building of a site, that
        building's records; otherwise a listing whose boundary holds the place or whose own point stands on it. A
        neighbour inside REData's search radius is not this place's status.
        """
        from urbanlens.dashboard.services.locations.national_register import containing_listings, reference_field
        from urbanlens.dashboard.services.locations.register_names import CONTAINS_POINT_KEY

        if pin.location is None:
            return None
        one_building = self.one_building_of_a_site(pin)
        resources = self.own_resources(pin, data, one_building=one_building)
        own = resources if one_building else [{**row, CONTAINS_POINT_KEY: True} for row in containing_listings(pin.location, resources)]
        note = national_register_note(pin, own, one_building=one_building)
        fields: list[dict[str, str]] = []
        if note and (number := reference_field(national_register_reference(pin, own, one_building=one_building))):
            fields.append(number)
        for row in own:
            if str(row.get("resource_type") or "") in _NOT_A_DESCRIPTION or not str(row.get("name") or "").strip():
                continue
            provider, status = str(row.get("provider") or ""), str(row.get("status") or "").strip()
            label = register_label(provider)
            if provider != _NATIONAL_REGISTER and status and all(existing["label"] != label for existing in fields):
                fields.append({"label": label, "value": status})
        if not (note or fields):
            return None
        return OverviewSummary(fields=fields, notes=[note] if note else [])

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """List what each register says, site-level records first for a parcel pin.
        Structure rows are not dropped, only ordered after: a campus whose only records are its buildings should still show them."""
        from urbanlens.dashboard.services.locations.national_register import nps_record_url
        from urbanlens.dashboard.services.locations.site_scope import is_site_scope

        one_building = self.one_building_of_a_site(pin)
        resources = self.own_resources(pin, data, one_building=one_building)
        rows = register_rows(resources)
        if not rows:
            return None
        note = national_register_note(pin, resources, one_building=one_building)
        note_url = nps_record_url(national_register_reference(pin, resources, one_building=one_building)) if note else None
        if is_site_scope(pin):
            rows = sorted(rows, key=lambda row: row["scope"] != "site")

        by_register: dict[str, int] = {}
        for row in rows:
            by_register[row["register"]] = by_register.get(row["register"], 0) + 1

        chips = [register if count == 1 else f"{register} ({count})" for register, count in sorted(by_register.items(), key=lambda item: (-item[1], item[0]))]
        meta = [_meta_entry(row) for row in rows[:_MAX_ROWS]]
        facts = [{"icon": "account_balance", "text": note, **({"href": note_url} if note_url else {})}] if note else []
        return {"chips": chips, "facts": facts, "meta": meta}


class HistoricRegistersPlugin(UrbanLensPlugin):
    """State, city and national historic-register records for a pin, via REData."""

    name: ClassVar[str] = "redata_historic_registers"
    verbose_name: ClassVar[str] = "Historic Registers"
    description: ClassVar[str] = (
        "What the historic inventories say about a pin - the nationwide National Register plus state SHPO and "
        "city/county registers, from REData's cultural-resources registry. Renders only the fields REData "
        "standardizes across providers, so a register REData adds appears without an UrbanLens release. New "
        "York's CRIS is excluded here because it has its own richer panel."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for REData's cultural-resources endpoint."""
        return {
            "redata_cultural_resources": ServiceDefaults(
                display_name="REData Historic Registers",
                # Shares REData's single lookup pool with geocode/weather/etc.
                calls_per_minute=20,
                calls_per_day=None,
                notes="Historic-register records via GET /cultural-resources/lookup/. See services.apis.locations.redata_cultural_resources_gateway.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the historic-registers pin-detail panel."""
        return [HistoricRegisterPanelSource()]

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute the names of the register listings containing a location."""
        from urbanlens.dashboard.services.locations.register_names import HistoricRegisterNameProvider

        return [HistoricRegisterNameProvider()]
