"""Historic-register plugin: what the state and city inventories say about a pin, via REData.

REData's cultural-resources registry holds **25** historic inventories - the
nationwide National Register plus state SHPO layers for Massachusetts, Texas,
North Carolina, Washington, Virginia, Maryland, Ohio, Indiana and Alabama, and
city/county registers for Minneapolis, Denver, Detroit, Baltimore, Atlanta, Los
Angeles County, DC, Syracuse, Fort Myers, Boise, Salt Lake City, St. Johns
County and Chesterfield County. UrbanLens reached exactly one of them, New
York's CRIS, and only inside New York.

That was not a curation decision. ``plugins.builtin.cris_buildings`` renders
CRIS's own raw ArcGIS column names (``USNName``, ``USNNum``, ``EligibilityDesc``,
...), so it *has* to name its provider - handing it an NRHP row blanks the card,
which is a bug that has already happened. Restricting the request was the fix
for that, and the side effect was that every register outside New York stayed
unread.

This panel is the other half: it renders only the fields REData standardizes
across every provider (``name``, ``resource_type``, ``scope``, ``status``,
``year_built``, ``architectural_style``, ``use_type``), so one card covers the
whole registry and a register REData adds appears without an UrbanLens release.
``ny_cris`` is excluded because it has a richer panel of its own; nothing else
is.

For this application these are among the most useful records there are. A
state or city historic inventory is where a derelict building's construction
date, original use, architect and survey photographs live, years before anything
else on the internet says a word about it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.pins.redata_panel import RedataInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: Providers with a panel of their own, so including them here would show the
#: same record twice under a vaguer heading. A fact about *this app's* UI, which
#: is what makes it safe to write down - unlike the provider list itself, which
#: is REData's and is discovered. ``test_redata_historic_registers`` holds every
#: entry to a registered panel key.
_SHOWN_ELSEWHERE: frozenset[str] = frozenset({"ny_cris"})

#: Display names for the registers, because their tags are acronyms that
#: title-case badly ("Md Mihp"). **Never a gate** - a provider missing from here
#: still renders, under a title-cased tag. Reading a name map as a permission
#: list is exactly what made REData's `s2cloudless` invisible in the satellite
#: carousel, and the fallback is what keeps a newly registered inventory
#: readable until somebody writes a better name.
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

#: ``resource_type`` values whose rows describe no property. An archaeological
#: buffer marks a sensitivity zone - REData publishes only an ``OBJECTID`` and a
#: geometry for it, deliberately, so there is nothing to render and naming one
#: would disclose a site location this app has no business surfacing.
_NOT_A_DESCRIPTION: frozenset[str] = frozenset({"archaeological_buffer_area"})

#: Rows shown before the list is truncated. A survey of a large campus can list
#: a hundred structures; the panel is a summary, not the inventory.
_MAX_ROWS = 10

#: Fields kept from each row. The rest of ``CulturalResourceSerializer`` -
#: ``attributes``, ``detail_payload``, ``geometry``, both coordinate pairs - is
#: either per-provider, large, or both, and this card renders none of it. Cached
#: payloads are read on every pin-detail render, so what is dropped here matters
#: as much as what is kept.
_KEPT_FIELDS = ("provider", "resource_type", "scope", "name", "status", "year_built", "architectural_style", "use_type")


def register_label(provider: str) -> str:
    """A human name for a register tag.

    Args:
        provider: REData's provider tag, e.g. ``"md_mihp"``.

    Returns:
        The written-down name when there is one, else the tag with underscores
        turned to spaces and title-cased.
    """
    return _REGISTER_LABELS.get(provider) or provider.replace("_", " ").title()


def register_rows(resources: list[Any]) -> list[dict[str, str]]:
    """Normalize REData cultural-resource rows into display rows.

    Reads only the fields REData standardizes across every provider - never the
    per-provider ``attributes`` blob, which is what ties ``cris_buildings`` to
    one inventory.

    Args:
        resources: Cached ``CulturalResourceSerializer``-shaped rows.

    Returns:
        ``{"register", "name", "detail", "scope"}`` dicts, nearest-first order
        preserved, skipping rows that would render as an unlabelled blank and
        rows that describe no property.
    """
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
        rows.append(
            {
                "register": register_label(str(resource.get("provider") or "")),
                "name": name,
                "detail": ", ".join(fact for fact in facts if fact),
                "scope": str(resource.get("scope") or ""),
            },
        )
    return rows


class HistoricRegisterPanelSource(RedataInfoPanelSource):
    """Every historic register that names this place, from REData's whole registry."""

    key = "redata_historic_registers"
    cache_source = "redata_historic_registers"
    section_id = "historic-registers-section"
    icon = "history_edu"
    title = "Historic Registers"
    payload_key: ClassVar[str] = "resources"
    #: A surveyed block and an unlisted field both fetch successfully; only one
    #: has a tab worth showing.
    inspects_content: ClassVar[bool] = True

    def fetch_envelope(self, latitude: float, longitude: float) -> LocationContextEnvelope:
        """Ask every register covering this point except the ones with their own panel."""
        from urbanlens.dashboard.services.apis.locations.redata_cultural_resources_gateway import RedataCulturalResourcesGateway, applicable_provider_tags

        wanted = [tag for tag in applicable_provider_tags(latitude, longitude) if tag not in _SHOWN_ELSEWHERE]
        if not wanted:
            # Nothing covers this point, or discovery failed. An empty envelope
            # rather than an unfiltered request: naming no provider runs every
            # register in the registry, which is the one outcome the capability
            # lookup exists to avoid.
            from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope as Envelope

            return Envelope(count=0, complete=True, results=[], providers=[])

        return RedataCulturalResourcesGateway().near_resources(latitude, longitude, provider=wanted)

    def transform_rows(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep only the standardized fields this card renders - see :data:`_KEPT_FIELDS`."""
        return [{key: row.get(key) for key in _KEPT_FIELDS} for row in results if isinstance(row, dict)]

    def has_content(self, data: dict | None) -> bool:
        """A row with no name renders nothing worth a tab."""
        return bool(register_rows((data or {}).get(self.payload_key) or []))

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """List what each register says, site-level records first for a parcel pin.

        A parcel-scope pin is the whole property, so a district or National
        Register listing describes it and one surveyed outbuilding does not -
        the same distinction ``cris_buildings`` makes, expressed here through
        REData's own ``scope`` rather than through one provider's resource
        types. Structure rows are not dropped, only ordered after: a campus
        whose only records are its buildings should still show them.
        """
        from urbanlens.dashboard.services.locations.site_scope import is_site_scope

        rows = register_rows((data or {}).get(self.payload_key) or [])
        if not rows:
            return None
        if is_site_scope(pin):
            rows = sorted(rows, key=lambda row: row["scope"] != "site")

        by_register: dict[str, int] = {}
        for row in rows:
            by_register[row["register"]] = by_register.get(row["register"], 0) + 1

        chips = [register if count == 1 else f"{register} ({count})" for register, count in sorted(by_register.items(), key=lambda item: (-item[1], item[0]))]
        meta = [{"label": row["register"], "value": f"{row['name']} - {row['detail']}" if row["detail"] else row["name"]} for row in rows[:_MAX_ROWS]]
        return {"chips": chips, "meta": meta}


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
