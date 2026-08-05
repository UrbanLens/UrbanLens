"""NY SHPO CRIS plugin: Building USN Point data for pinned locations. New York only.

Retrieval lives entirely in REData (the standalone service that already owns
property records for this app - see ``plugins.builtin.property_records``):
``RedataGateway.lookup_cultural_resources`` finds resources near the pin's
coordinate (NY's Cultural Resource Information System is REData's only
current provider - everywhere else returns nothing, the "real search, no
matches" shape), and ``fetch_cultural_resource_detail`` fetches the first
"building"-type match's full record, including attachment/photo metadata.
Attachments are exposed to the pin's Media gallery via
:meth:`CrisBuildingPanelSource.media_items`, streamed through
:class:`~urbanlens.dashboard.controllers.pin.PinCrisAttachmentView` so
REData's API key never reaches the browser (same reasoning as every other
authenticated media proxy in this app).

Field names in :meth:`CrisBuildingPanelSource.render_context` (``USNNum``,
``USNName``, ``HouseNum``, ``StreetName``, ``City``, ``Zip``,
``EligibilityDesc``) match the live "Building USN Points" ArcGIS FeatureServer
schema (NYS Office of Parks, Recreation and Historic Preservation) - REData's
lookup response nests these under the resource's own ``attributes`` dict, so
``fetch`` flattens that dict onto the top level of the cached payload,
keeping ``render_context`` unchanged.

The same lookup also returns *site*-level resources (historic districts,
National Register listings), cached under a separate ``district`` key. A pin
covering a whole parcel renders that instead of a building record - see
:meth:`CrisBuildingPanelSource.render_context` and
``services.locations.site_scope``. The cache row itself stays scope-neutral
(it is shared by every user pinning this place, whose own hierarchies differ),
so only rendering branches on scope.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.geo.geo_boundary import state_boundary
from urbanlens.dashboard.services.locations.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.locations.name_resolution import LocationCacheNameProvider
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource, GalleryMediaSource, PanelApiKind

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaItem
    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.locations.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider
    from urbanlens.dashboard.services.pins.external_data import PanelSource

logger = logging.getLogger(__name__)

#: Eligibility values that mean the surveyed building/structure no longer
#: exists. Once real eligibility data starts flowing (it now does, via
#: fetch() below), a payload with this ``EligibilityDesc`` should apply the
#: "Demolished" status label via
#: ``services.labels.statuses.add_demolished_status``/``add_demolished_status_to_wiki``
#: (looking up ``pin.location.wiki``, when present) - not implemented yet;
#: tracked separately from this plugin's media-gallery integration.
_DEMOLISHED_ELIGIBILITY = "Not Eligible - Demolished"

#: Only "building" resources carry the USN Point fields this panel renders;
#: the other CRIS resource types (district/national-register-listing/
#: archaeological-buffer-area) are out of scope for this specific plugin.
_RESOURCE_TYPE = "building"

#: Resource types that describe a whole *site* rather than one structure, in
#: preference order - what a parcel-scope pin should show instead of an
#: arbitrary building from the same lookup (see ``render_context``). The
#: archaeological-buffer-area type is deliberately absent: it marks a
#: sensitivity zone, not a description of the property. These must match
#: ``CulturalResourceType``'s own values in REData exactly - the district one
#: is ``building_district``, not ``district``.
_SITE_RESOURCE_TYPES = ("building_district", "national_register_listing")

#: REData's ``CulturalResourceAttachmentKind`` values, lowercase (they are
#: Django ``TextChoices`` values, serialized verbatim by its ModelSerializer).
#: Compared case-insensitively at every use so this plugin keeps working if
#: REData ever normalizes them differently.
_ATTACHMENT_KIND_PHOTO = "photo"
_ATTACHMENT_KIND_DOCUMENT = "document"

#: Marks a cached payload whose attachments really were fetched, as opposed to
#: one written by background enrichment (which fills the info card only).
_ATTACHMENTS_FETCHED_KEY = "attachments_fetched"

#: Gallery tab label / ``MediaItem.source`` for everything this plugin emits.
_SOURCE_NAME = "NY Historic Preservation (CRIS)"


def attachment_kind(attachment: dict) -> str:
    """One attachment's normalized ``kind`` (``"photo"``/``"document"``/``""``)."""
    return str(attachment.get("kind") or "").strip().lower()


def site_resource(resources: list[dict]) -> dict | None:
    """Pick the best site-level CRIS resource from a lookup.

    Args:
        resources: The resource dicts from
            :meth:`RedataGateway.lookup_cultural_resources`.

    Returns:
        The whole resource dict (so its ``uuid`` stays reachable for a detail
        fetch), or None when the lookup returned no site-level resource.
    """
    for resource_type in _SITE_RESOURCE_TYPES:
        match = next((r for r in resources if r.get("resource_type") == resource_type), None)
        if match is not None:
            return match
    return None


def site_resource_attributes(resources: list[dict]) -> dict:
    """Pick the best site-level CRIS resource from a lookup and flatten its attributes.

    Args:
        resources: The resource dicts from
            :meth:`RedataGateway.lookup_cultural_resources`.

    Returns:
        The chosen resource's own ``attributes`` dict (the raw ArcGIS layer
        fields, same shape the building record is flattened into), plus a
        ``resource_type`` key; ``{}`` when the lookup returned no site-level
        resource.
    """
    match = site_resource(resources)
    if match is None:
        return {}
    return {**(match.get("attributes") or {}), "resource_type": match.get("resource_type")}


def nearest_resource(resources: list[dict], resource_type: str, latitude: float, longitude: float) -> dict | None:
    """The resource of ``resource_type`` closest to a coordinate.

    A CRIS lookup over a campus routinely returns dozens of buildings (REData
    counts 124 for the former Hudson River State Hospital alone), and the
    order they come back in means nothing - so taking the first match hands
    every pin on the site the same arbitrary outbuilding, or a different one
    each refresh. Each resource's *own* published position is
    ``source_latitude``/``source_longitude``; ``latitude``/``longitude`` is
    the point the search ran from and is identical across every row, so it
    cannot be used to rank them.

    Args:
        resources: The resource dicts from
            :meth:`RedataGateway.lookup_cultural_resources`.
        resource_type: The ``resource_type`` to restrict to.
        latitude: WGS-84 latitude of the pin.
        longitude: WGS-84 longitude of the pin.

    Returns:
        The closest matching resource, the first match when none of them
        publishes a position (REData leaves ``source_*`` null for USN stubs),
        or None when there is no match at all.
    """
    from urbanlens.dashboard.services.locations.site_scope import meters_between

    matches = [r for r in resources if r.get("resource_type") == resource_type]
    if not matches:
        return None
    best, best_distance = None, float("inf")
    for resource in matches:
        lat, lng = resource.get("source_latitude"), resource.get("source_longitude")
        if lat is None or lng is None:
            continue
        distance = meters_between(float(lat), float(lng), latitude, longitude)
        if distance < best_distance:
            best, best_distance = resource, distance
    return best if best is not None else matches[0]


#: A resource's real detail-fetch never runs on every page load - REData
#: caches ``detail_payload``/``attachments`` on the resource itself once
#: fetched, so this only needs to happen again after this TTL, exactly like
#: every other LocationCache-backed panel's own freshness window.
_RADIUS_METERS = 200


class CrisBuildingPanelSource(CoordinateGatedInfoPanelSource, GalleryMediaSource):
    """NY SHPO CRIS "Building USN Point" info for the pin's location. New York only."""

    key = "cris_building"
    cache_source = "cris_building_usn"
    section_id = "cris-building-section"
    icon = "account_balance"
    title = "NY Historic Preservation (CRIS)"
    geo_boundary: ClassVar[GeoBoundary | None] = state_boundary("NY")
    # The one source that is honestly both shapes, and the reason api_kinds is
    # a set rather than a single value: the same cached CRIS record is an
    # eligibility/address card *and* the survey photos and scanned inventory
    # forms attached to it. Declared explicitly because Python's MRO would
    # otherwise silently pick InfoPanelSource's {INFO} (it comes first in the
    # bases) and drop the media half without any error to notice.
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset({PanelApiKind.INFO, PanelApiKind.MEDIA})

    def media_is_ready(self, data: dict) -> bool:
        """True once this row's attachments have actually been fetched.

        This source shares ``cache_source`` with
        :class:`CrisBuildingEnrichmentSource`, which fills the info-card half
        only - attachments come from a per-resource detail fetch it
        deliberately skips (see its own ``fetch``). Without this check, a
        location that background enrichment reached first showed an empty
        Media tab for the whole cache window, even though nothing had ever
        asked CRIS for its photos and inventory forms.
        """
        # An empty payload is a real "CRIS has nothing here" answer, not a
        # half-filled row - treating it as unready would poll forever.
        return not data or bool(data.get(_ATTACHMENTS_FETCHED_KEY))

    def fetch(self, pin: Pin) -> None:
        """Find the CRIS resources at this pin and cache their info + attachments.

        Fetches detail (and therefore attachments) for the building nearest
        the pin *and* for the site-level record covering it, since the two
        answer different questions and a pin needs whichever matches its own
        scope - see :meth:`render_context` and :meth:`media_items`.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway

        location = pin.location
        lat = float(location.latitude) if location and location.latitude is not None else None
        lng = float(location.longitude) if location and location.longitude is not None else None
        if lat is None or lng is None:
            LocationCache.set(pin.location, self.cache_source, {}, query_key="")
            return

        query_key = f"{lat},{lng}"
        try:
            gateway = RedataGateway()
            resources = gateway.lookup_cultural_resources(lat, lng, radius_meters=_RADIUS_METERS)
        except (PropertyRecordsUnavailableError, ValueError):
            logger.debug("CrisBuildingPanelSource.fetch: CRIS lookup unavailable for pin %s", pin.pk, exc_info=True)
            LocationCache.set(pin.location, self.cache_source, {}, query_key=query_key)
            return

        site = site_resource(resources)
        district = {**(site.get("attributes") or {}), "resource_type": site.get("resource_type")} if site else {}
        building = nearest_resource(resources, _RESOURCE_TYPE, lat, lng)
        if building is None and site is None:
            # CRIS genuinely has nothing here (or only an archaeological
            # buffer, which describes no property) - a real answer, cached as
            # the empty payload every other panel uses for it.
            LocationCache.set(pin.location, self.cache_source, {}, query_key=query_key)
            return

        attachments: list[dict] = []
        data: dict[str, Any] = {}
        if building is not None:
            resource_uuid = building.get("uuid")
            detail = self._resource_detail(gateway, building)
            # Flatten the resource's own `attributes` (the raw ArcGIS layer
            # feature's fields - USNName, USNNum, HouseNum, ...) onto the top
            # level, matching what render_context already expects.
            data = dict(detail.get("attributes") or {})
            data["resource_uuid"] = detail.get("uuid") or resource_uuid
            attachments.extend(self._attachments_with_extracted_images(data["resource_uuid"], detail.get("attachments") or []))

        if site is not None:
            site_detail = self._resource_detail(gateway, site)
            site_uuid = site_detail.get("uuid") or site.get("uuid")
            # A historic district's or National Register listing's own
            # attachments are the nomination forms and survey photographs for
            # the *site* - the only CRIS media a parcel-scope pin should be
            # showing, and previously never fetched at all.
            attachments.extend(self._attachments_with_extracted_images(site_uuid, site_detail.get("attachments") or []))
            if site_uuid:
                district["resource_uuid"] = site_uuid

        data["attachments"] = attachments
        # Records that this row's media half is filled in, distinguishing it
        # from an enrichment-written row that only ever had the info card.
        data[_ATTACHMENTS_FETCHED_KEY] = True
        # Kept beside (not instead of) the flattened building fields: the same
        # lookup already returned it, the name provider and media gallery both
        # read the top level, and a parcel-scope pin needs the district record
        # rather than whichever single building happened to match.
        if district:
            data["district"] = district
        LocationCache.set(pin.location, self.cache_source, data, query_key=query_key)

    @staticmethod
    def _resource_detail(gateway, resource: dict) -> dict:
        """One resource's detail record, degrading to the search row on failure.

        A resource type with no detail path (CRIS's archaeological buffer
        areas) and a transient REData problem both surface as
        ``PropertyRecordsUnavailableError`` here; neither should cost the
        caller the resource's own already-known attributes.

        Args:
            gateway: The :class:`RedataGateway` to fetch through.
            resource: The resource dict from the near-point lookup.

        Returns:
            The detail record, or ``resource`` unchanged.
        """
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError

        resource_uuid = resource.get("uuid")
        if not resource_uuid:
            return resource
        try:
            return gateway.fetch_cultural_resource_detail(resource_uuid)
        except (PropertyRecordsUnavailableError, ValueError):
            logger.debug("CrisBuildingPanelSource: no detail available for resource %s", resource_uuid, exc_info=True)
            return resource

    @staticmethod
    def _attachments_with_extracted_images(resource_uuid: str | None, attachments: list[dict]) -> list[dict]:
        """Best-effort OCR/AI-extract each document attachment's embedded photos.

        A scanned "Building-Structure Inventory Form" often has one or more
        embedded photos alongside its text fields - REData's extract endpoint
        surfaces those independently of whether the text extraction found
        anything (see ``RedataGateway.extract_cultural_resource_attachment``'s
        own docstring). One attachment's extraction failing (not extractable
        yet, or REData/the AI provider being unavailable) must not drop the
        others - each is attempted independently and just keeps
        ``extracted_images: []`` on failure.

        Args:
            resource_uuid: The resource's REData uuid, or None when it
                couldn't be resolved (skips extraction entirely - the
                attachments are still returned unmodified).
            attachments: The resource's raw attachment list (photo + document kinds).

        Returns:
            The same attachments, each carrying the ``resource_uuid`` it
            belongs to (one payload now aggregates attachments from more than
            one resource - see :meth:`fetch`) and each document-kind entry
            augmented with an ``extracted_images`` list (possibly empty).
        """
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway

        if not resource_uuid:
            return list(attachments)

        gateway = RedataGateway()
        result: list[dict] = []
        for raw_attachment in attachments:
            attachment = dict(raw_attachment)
            attachment["resource_uuid"] = resource_uuid
            attachment_id = attachment.get("id")
            if attachment_kind(attachment) == _ATTACHMENT_KIND_DOCUMENT and attachment_id is not None:
                try:
                    extracted = gateway.extract_cultural_resource_attachment(resource_uuid, attachment_id)
                    attachment["extracted_images"] = extracted.get("extracted_images") or []
                except PropertyRecordsUnavailableError:
                    logger.debug("CrisBuildingPanelSource: extraction unavailable for attachment %s of resource %s", attachment_id, resource_uuid, exc_info=True)
                    attachment["extracted_images"] = []
            result.append(attachment)
        return result

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the Building USN Point card from a cached CRIS payload.

        Field names match the live "Building USN Points" ArcGIS FeatureServer
        schema; see the module docstring.

        A parcel-scope pin renders the *district* record from the same lookup
        instead (see ``_SITE_RESOURCE_TYPES``), and nothing at all when CRIS
        has no district here - "TOOL SHED (1937), Building Number 154" is a
        true statement about one structure on a campus and a false one about
        the campus, which is the whole reason scope exists.
        """
        from urbanlens.dashboard.services.locations.site_scope import is_site_scope

        data = data or {}
        if is_site_scope(pin):
            data = data.get("district") or {}

        usn_name = data.get("USNName")
        if not usn_name:
            return None

        address_parts = [part for part in (data.get("HouseNum"), data.get("StreetName")) if part]
        meta = []
        if address_parts:
            meta.append({"label": "Address", "value": " ".join(address_parts)})
        for key, label in (("City", "City"), ("Zip", "ZIP Code"), ("USNNum", "NYSHPO USN Number"), ("EligibilityDesc", "Eligibility Status")):
            value = data.get(key)
            if value:
                meta.append({"label": label, "value": value})

        return {"heading_name": usn_name, "meta": meta, "nested": True}

    def media_items(self, data: dict) -> list[MediaItem]:
        """Turn cached CRIS attachments (photos, documents, and extracted images) into gallery items.

        Args:
            data: This source's cached payload (see :meth:`fetch`). Each
                attachment carries the ``resource_uuid`` it belongs to, since
                one payload aggregates the nearest building's attachments and
                the site-level record's.

        Returns:
            One item per attachment, proxied through
            ``PinCrisAttachmentView`` (never a raw REData URL). Every
            attachment - photo *and* document - gets a ``thumb_url`` pointing
            at that view's preview mode: CRIS photos are frequently TIFFs and
            its documents are scanned inventory forms and nomination PDFs,
            none of which an ``<img>`` can display, and REData reports an
            attachment's ``content_type`` as blank until the file has been
            downloaded once, so this cannot be decided from here. The proxy
            passes an already-displayable file straight through. Plus one item
            per photo OCR/AI-extracted from a document attachment (see
            :meth:`_attachments_with_extracted_images`), proxied through
            ``PinCrisExtractedImageView``.
        """
        from django.urls import reverse

        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        default_uuid = data.get("resource_uuid")

        items: list[MediaItem] = []
        for attachment in data.get("attachments") or []:
            attachment_id = attachment.get("id")
            resource_uuid = attachment.get("resource_uuid") or default_uuid
            if attachment_id is None or not resource_uuid:
                continue
            proxy_url = reverse("pin.cris.attachment", args=[resource_uuid, attachment_id])
            content_type = attachment.get("content_type") or ""
            caption = attachment.get("name") or attachment.get("attachment_type") or ""
            items.append(MediaItem(url=proxy_url, thumb_url=f"{proxy_url}?preview=1", caption=caption, source=_SOURCE_NAME, content_type=content_type))

            for image in attachment.get("extracted_images") or []:
                image_id = image.get("id")
                if image_id is None:
                    continue
                image_proxy_url = reverse("pin.cris.extracted_image", args=[resource_uuid, attachment_id, image_id])
                items.append(MediaItem(url=image_proxy_url, thumb_url=f"{image_proxy_url}?preview=1", caption=caption, source=_SOURCE_NAME))
        return items

    def api_payload(self, pin: Pin) -> dict[str, Any] | None:
        """The CRIS record as both an information card and its attachments.

        Neither inherited ``api_payload`` would do on its own - ``InfoPanelSource``'s
        would drop the attachments and ``GalleryMediaSource``'s would drop the
        eligibility card - so this composes both from the *one* cached row
        rather than reading it twice.

        The media URLs are the same in-app proxy routes ``media_items``
        already builds (``pin.cris.attachment`` / ``pin.cris.extracted_image``),
        so REData's API key stays server-side here exactly as it does on the
        web. They are relative paths; a native client resolves them against its
        API base URL.

        Args:
            pin: The pin whose panel is being read. ``render_context`` branches
                on it - a parcel-scope pin gets the historic-district record
                rather than an arbitrary building from the same lookup.

        Returns:
            ``{"info": ..., "media": [...]}`` with ``info`` possibly None (a
            location inside a historic district but with no surveyed building
            of its own still has attachments worth serving), or None when
            nothing has landed yet or the record yields neither.
        """
        data = self.cached_data(pin)
        if data is None:
            return None
        card = self.api_info(pin, data)
        media = self.api_media(data)
        if card is None and not media:
            return None
        return {PanelApiKind.INFO.value: card, PanelApiKind.MEDIA.value: media}


class CrisBuildingEnrichmentSource(LocationCacheEnrichmentSource):
    """Background-fills the CRIS Building USN Point cache per Location. New York only."""

    key: ClassVar[str] = "cris_building"
    verbose_name: ClassVar[str] = "NY Historic Preservation (CRIS)"
    cache_source: ClassVar[str] = "cris_building_usn"
    geo_boundary: ClassVar[GeoBoundary | None] = state_boundary("NY")

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Find the CRIS "building" resource nearest this location and return its flattened info.

        Shares ``cache_source`` with :class:`CrisBuildingPanelSource`, so
        whichever of panel-fetch or background enrichment runs first for a
        Location fills in for the other. Attachments are deliberately not
        fetched here - each needs its own live detail round trip, which is a
        poor fit for a bulk backfill - so the row this writes is missing the
        Media half. It therefore leaves ``attachments_fetched`` unset, and
        ``CrisBuildingPanelSource.is_ready`` treats such a row as still
        needing its own fetch rather than as an authoritative "this location
        has no CRIS media".
        """
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway

        query_key = f"{location.latitude},{location.longitude}"
        try:
            resources = RedataGateway().lookup_cultural_resources(float(location.latitude), float(location.longitude), radius_meters=_RADIUS_METERS)
        except (PropertyRecordsUnavailableError, ValueError):
            return None, query_key
        district = site_resource_attributes(resources)
        building = nearest_resource(resources, _RESOURCE_TYPE, float(location.latitude), float(location.longitude))
        if building is None:
            return ({"district": district} if district else None), query_key
        data = dict(building.get("attributes") or {})
        data["resource_uuid"] = building.get("uuid")
        data["attachments"] = building.get("attachments") or []
        if district:
            data["district"] = district
        return data, query_key


class CrisBuildingsPlugin(UrbanLensPlugin):
    """NY State Historic Preservation Office (SHPO) CRIS data for pinned locations. New York only."""

    name: ClassVar[str] = "cris_buildings"
    verbose_name: ClassVar[str] = "NY Historic Preservation (CRIS)"
    description: ClassVar[str] = "Building USN Point data (National Register eligibility, historic districts) and its photos/documents, from NY SHPO's Cultural Resource Information System, via REData. New York State only."
    author: ClassVar[str] = "UrbanLens"

    # No get_service_defaults() override - this plugin calls REData's own API
    # (service key "redata_api"), already registered by plugins.builtin.property_records.

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the CRIS Building USN Point pin-detail panel (also a Media-gallery source)."""
        return [CrisBuildingPanelSource()]

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Contribute the CRIS Building USN Point cache to scheduled background enrichment."""
        return [CrisBuildingEnrichmentSource()]

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute the CRIS-listed property name as a place-name candidate."""
        return [LocationCacheNameProvider(source="cris", cache_source="cris_building_usn", keys=("USNName",), verbose_name="NY SHPO (CRIS)")]
