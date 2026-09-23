"""NY SHPO CRIS plugin: Building USN Point data for pinned locations.
The same lookup also returns *site*-level resources (historic districts, National Register listings), cached under a separate ``district`` key."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.geo.geo_boundary import state_boundary
from urbanlens.dashboard.services.locations.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.locations.name_resolution import LocationCacheNameProvider
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource, DocumentPanelSource, DocumentUnavailableError, GalleryMediaSource, PanelApiKind, SourceDocument

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.assets.base import MediaItem
    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.locations.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider
    from urbanlens.dashboard.services.pins.external_data import PanelSource

logger = logging.getLogger(__name__)

#: Eligibility values that mean the surveyed building/structure no longer exists.
_DEMOLISHED_ELIGIBILITY = "Not Eligible - Demolished"

#: Only "building" resources carry the USN Point fields this panel renders; the other CRIS resource
#: types (district/national-register-listing/ archaeological-buffer-area) are out of scope for this
#: specific plugin.
#: REData's provider tag for New York's CRIS.
_PROVIDER = "ny_cris"

_RESOURCE_TYPE = "building"

#: Resource types that describe a whole *site* rather than one structure, in preference order - what
#: a parcel-scope pin should show instead of an arbitrary building from the same lookup (see
#: ``render_context``).
_SITE_RESOURCE_TYPES = ("building_district", "national_register_listing")

#: REData's ``CulturalResourceAttachmentKind`` values, lowercase (they are Django ``TextChoices``
#: values, serialized verbatim by its ModelSerializer).
#: Compared case-insensitively at every use so this plugin keeps working if REData ever normalizes
#: them differently.
_ATTACHMENT_KIND_PHOTO = "photo"
_ATTACHMENT_KIND_DOCUMENT = "document"

#: Marks a cached payload whose attachments really were fetched, as opposed to
#: one written by background enrichment (which fills the info card only).
_ATTACHMENTS_FETCHED_KEY = "attachments_fetched"

#: Gallery tab label / ``MediaItem.source`` for everything this plugin emits.
_SOURCE_NAME = "NY Historic Preservation (CRIS)"

_SUBJECT_BUILDING = "building"
_SUBJECT_SITE = "site"

#: Marks an attachment gathered from another building on the same site, which only a site-scope page lists.
_SITE_BUILDING_KEY = "site_building"

_PDF_CONTENT_TYPE = "application/pdf"


def attachment_kind(attachment: dict) -> str:
    """One attachment's normalized ``kind`` (``"photo"``/``"document"``/``""``)."""
    return str(attachment.get("kind") or "").strip().lower()


def cris_only(resources: list[dict]) -> list[dict]:
    """Keep only the rows CRIS itself answered.

    Args:
        resources: Resource dicts from :meth:`RedataGateway.lookup_cultural_resources`.

    Returns:
        The CRIS-sourced subset, in the order given."""
    return [r for r in resources if r.get("provider") in (None, "", _PROVIDER)]


def site_resource(resources: list[dict]) -> dict | None:
    """Pick the best site-level CRIS resource from a lookup.

    Args:
        resources: The resource dicts from :meth:`RedataGateway.lookup_cultural_resources`.

    Returns:
        The whole resource dict (so its ``uuid`` stays reachable for a detail fetch), or None when the lookup returned no site-level resource."""
    for resource_type in _SITE_RESOURCE_TYPES:
        match = next((r for r in cris_only(resources) if r.get("resource_type") == resource_type), None)
        if match is not None:
            return match
    return None


def site_resource_attributes(resources: list[dict], latitude: float | None = None, longitude: float | None = None) -> dict:
    """Pick the best site-level CRIS resource from a lookup and flatten its attributes.

    Args:
        resources: The resource dicts from :meth:`RedataGateway.lookup_cultural_resources`.
        latitude: The point looked up from, to record whether the site's boundary contains it.
        longitude: The point's longitude.

    Returns:
        The chosen resource's own ``attributes`` dict (the raw ArcGIS layer fields, same shape the building record is flattened into), plus a ``resource_type`` key and, given a point, ``contains_point``; ``{}`` when the lookup returned no site-level resource."""
    match = site_resource(resources)
    if match is None:
        return {}
    district = {**(match.get("attributes") or {}), "resource_type": match.get("resource_type")}
    if latitude is not None and longitude is not None:
        district["contains_point"] = site_contains(match, latitude, longitude)
    return district


def site_contains(site: dict | None, latitude: float, longitude: float) -> bool:
    """Whether a site record's own boundary contains a point; one found only by search radius does not.

    Args:
        site: The site-level resource dict, or None.
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        True when the site publishes an areal boundary containing the point.
    """
    from shapely.geometry import Point

    polygon = site_polygon(site)
    return polygon is not None and bool(polygon.contains(Point(float(longitude), float(latitude))))


def building_position(building: dict) -> dict[str, float]:
    """The building's own published position, kept so naming can tell whether it is on the parcel.

    Args:
        building: A building resource dict.

    Returns:
        ``source_latitude``/``source_longitude``, or ``{}`` when CRIS publishes no point for it.
    """
    latitude, longitude = building.get("source_latitude"), building.get("source_longitude")
    if latitude is None or longitude is None:
        return {}
    return {"source_latitude": float(latitude), "source_longitude": float(longitude)}


def nearest_resource(resources: list[dict], resource_type: str, latitude: float, longitude: float) -> dict | None:
    """The resource of ``resource_type`` closest to a coordinate.

    Args:
        resources: The resource dicts from :meth:`RedataGateway.lookup_cultural_resources`.
        resource_type: The ``resource_type`` to restrict to.
        latitude: WGS-84 latitude of the pin.
        longitude: WGS-84 longitude of the pin.

    Returns:
        The closest matching resource, the first match when none of them publishes a position (REData leaves ``source_*`` null for USN stubs), or None when there is no match at all."""
    from urbanlens.dashboard.services.locations.site_scope import meters_between

    matches = [r for r in cris_only(resources) if r.get("resource_type") == resource_type]
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


def resource_name(resource: dict) -> str:
    """A resource's display name: REData's ``name``, else CRIS's own ``USNName``."""
    return str(resource.get("name") or (resource.get("attributes") or {}).get("USNName") or "")


def site_polygon(site: dict | None) -> BaseGeometry | None:
    """The site record's footprint as a shapely geometry, when it publishes an areal one.

    Args:
        site: The site-level resource dict, or None.

    Returns:
        The polygon, or None when there is no site, no geometry, or the geometry is not an area (a point-only listing says nothing about which buildings it covers).
    """
    from shapely.errors import ShapelyError
    from shapely.geometry import shape

    geometry = (site or {}).get("geometry")
    if not isinstance(geometry, dict):
        return None
    try:
        polygon = shape(geometry)
    except (ShapelyError, ValueError, TypeError, KeyError, AttributeError):
        logger.debug("CRIS site %s has an unreadable geometry", (site or {}).get("uuid"), exc_info=True)
        return None
    return polygon if not polygon.is_empty and polygon.area > 0 else None


def radius_covering(polygon: BaseGeometry, latitude: float, longitude: float) -> float:
    """The search radius, in metres, that reaches every corner of a polygon's bounding box from a point."""
    from urbanlens.dashboard.services.locations.site_scope import meters_between

    min_x, min_y, max_x, max_y = polygon.bounds
    return max(meters_between(latitude, longitude, corner_lat, corner_lng) for corner_lng in (min_x, max_x) for corner_lat in (min_y, max_y))


def is_pdf_document(attachment: dict) -> bool:
    """Whether an attachment is a document CRIS serves as a PDF (or leaves untyped, as it does for scans)."""
    content_type = str(attachment.get("content_type") or "").split(";", 1)[0].strip().lower()
    return attachment_kind(attachment) == _ATTACHMENT_KIND_DOCUMENT and content_type in ("", _PDF_CONTENT_TYPE)


#: A resource's real detail-fetch never runs on every page load - REData caches
#: ``detail_payload``/``attachments`` on the resource itself once fetched, so this only needs to
#: happen again after this TTL, exactly like every other LocationCache-backed panel's own freshness
#: window.
_RADIUS_METERS = 200

#: A site-scope lookup reaches this far before the site record's own footprint is known.
_SITE_RADIUS_METERS = 500
#: Ceiling on the footprint-derived radius, so one sprawling district cannot turn into a county-wide query.
_MAX_SITE_RADIUS_METERS = 1500
#: Campus buildings considered per pass, nearest first.
_MAX_SITE_BUILDINGS = 40
#: Live detail fetches per pass for campus buildings REData has not fetched yet; the bulk queue warms the rest.
_MAX_SITE_DETAIL_FETCHES = 12
#: Campus detail fetches stop once the whole fetch has run this long, leaving the task's 110s soft limit room
#: for one more request's timeout.
_SITE_DETAIL_BUDGET_SECONDS = 50.0


class CrisBuildingPanelSource(CoordinateGatedInfoPanelSource, GalleryMediaSource, DocumentPanelSource):
    """NY SHPO CRIS "Building USN Point" info for the pin's location. New York only.

    A site-scope pin (a campus) also gathers the inventory forms of every CRIS building on the site, for Article > Sources."""

    key = "cris_building"
    cache_source = "cris_building_usn"
    section_id = "cris-building-section"
    icon = "account_balance"
    title = "NY Historic Preservation (CRIS)"
    geo_boundary: ClassVar[GeoBoundary | None] = state_boundary("NY")
    # The one source that is honestly both shapes, and the reason api_kinds is a set rather than a
    # single value: the same cached CRIS record is an eligibility/address card *and* the survey
    # photos and scanned inventory forms attached to it.
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset({PanelApiKind.INFO, PanelApiKind.MEDIA})

    def media_is_ready(self, data: dict) -> bool:
        """True once this row's attachments have actually been fetched.
        Without this check, a location that background enrichment reached first showed an empty Media tab for the whole cache window, even though nothing had ever asked CRIS for its photos and inventory forms."""
        # An empty payload is a real "CRIS has nothing here" answer, not a
        # half-filled row - treating it as unready would poll forever.
        return not data or bool(data.get(_ATTACHMENTS_FETCHED_KEY))

    def fetch(self, pin: Pin) -> None:
        """Find the CRIS resources at this pin and cache their info + attachments.

        Raises:
            PropertyRecordsUnavailableError: Only for a reason in ``TRANSIENT_REASONS``, so an outage is retried rather than cached as "CRIS has nothing here".
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import TRANSIENT_REASONS, PropertyRecordsUnavailableError, RedataGateway
        from urbanlens.dashboard.services.locations.site_scope import is_site_scope

        started = time.monotonic()
        location = pin.location
        lat = float(location.latitude) if location and location.latitude is not None else None
        lng = float(location.longitude) if location and location.longitude is not None else None
        if lat is None or lng is None:
            LocationCache.set(pin.location, self.cache_source, {}, query_key="")
            return

        site_scope = is_site_scope(pin)
        radius: float = _SITE_RADIUS_METERS if site_scope else _RADIUS_METERS
        query_key = f"{lat},{lng}"
        polygon: BaseGeometry | None = None
        try:
            gateway = RedataGateway()
            resources = gateway.lookup_cultural_resources(lat, lng, radius_meters=radius, provider=_PROVIDER)
            site = site_resource(resources)
            polygon = site_polygon(site) if site_scope else None
            if polygon is not None:
                covering = min(radius_covering(polygon, lat, lng), _MAX_SITE_RADIUS_METERS)
                if covering > radius:
                    # Keep the site whose footprint set the radius.
                    radius = covering
                    resources = gateway.lookup_cultural_resources(lat, lng, radius_meters=radius, provider=_PROVIDER)
        except PropertyRecordsUnavailableError as exc:
            if exc.reason in TRANSIENT_REASONS:
                raise
            logger.debug("CrisBuildingPanelSource.fetch: CRIS lookup unavailable for pin %s", pin.pk, exc_info=True)
            LocationCache.set(pin.location, self.cache_source, {}, query_key=query_key)
            return
        except ValueError:
            logger.debug("CrisBuildingPanelSource.fetch: REData is not configured (pin %s)", pin.pk, exc_info=True)
            LocationCache.set(pin.location, self.cache_source, {}, query_key=query_key)
            return

        district = {**(site.get("attributes") or {}), "resource_type": site.get("resource_type"), "contains_point": site_contains(site, lat, lng)} if site else {}
        building = nearest_resource(resources, _RESOURCE_TYPE, lat, lng)
        if building is None and site is None:
            # CRIS genuinely has nothing here (or only an archaeological
            # buffer, which describes no property) - a real answer, cached as
            # the empty payload every other panel uses for it.
            LocationCache.set(pin.location, self.cache_source, {}, query_key=query_key)
            return

        attachments: list[dict] = []
        unextracted: dict[str, list[int]] = {}
        data: dict[str, Any] = {}
        if building is not None:
            resource_uuid = building.get("uuid")
            detail = self._resource_detail(gateway, building)
            # Flatten the resource's own `attributes` (the raw ArcGIS layer
            # feature's fields - USNName, USNNum, HouseNum, ...) onto the top
            # level, matching what render_context already expects.
            data = dict(detail.get("attributes") or {})
            data.update(building_position(building))
            data["resource_uuid"] = detail.get("uuid") or resource_uuid
            own = self._attachments_with_extracted_images(data["resource_uuid"], detail.get("attachments") or [], unextracted)
            attachments.extend(self._tagged(own, subject=resource_name(detail) or resource_name(building), subject_kind=_SUBJECT_BUILDING))

        site_detail: dict = {}
        if site is not None:
            site_detail = self._resource_detail(gateway, site)
            site_uuid = site_detail.get("uuid") or site.get("uuid")
            own = self._attachments_with_extracted_images(site_uuid, site_detail.get("attachments") or [], unextracted)
            attachments.extend(self._tagged(own, subject=resource_name(site_detail) or resource_name(site), subject_kind=_SUBJECT_SITE))
            if site_uuid:
                district["resource_uuid"] = site_uuid

        if site_scope:
            self._queue_site_details(gateway, lat, lng, radius)
            skip = {uuid for uuid in (data.get("resource_uuid"), district.get("resource_uuid")) if uuid}
            candidates = self._campus_candidates(resources, site_detail, polygon, lat, lng, skip=skip)
            attachments.extend(self._campus_attachments(gateway, candidates, started=started))

        data["attachments"] = attachments
        # Records that this row's media half is filled in, distinguishing it
        # from an enrichment-written row that only ever had the info card.
        data[_ATTACHMENTS_FETCHED_KEY] = True
        data["site_scope"] = site_scope
        # Kept beside (not instead of) the flattened building fields: the same lookup already
        # returned it, the name provider and media gallery both read the top level, and a
        # parcel-scope pin needs the district record rather than whichever single building happened
        # to match.
        if district:
            data["district"] = district
        LocationCache.set(pin.location, self.cache_source, data, query_key=query_key)
        self._request_extractions(pin.location.pk, unextracted)

    @staticmethod
    def _request_extractions(location_id: int, unextracted: dict[str, list[int]]) -> None:
        """Queue REData extraction of the documents it has not extracted yet, merged into the cache when done."""
        from urbanlens.dashboard.services.core.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import extract_cris_attachments

        for resource_uuid, attachment_ids in unextracted.items():
            safely_enqueue_task(extract_cris_attachments, location_id, resource_uuid, attachment_ids)

    @staticmethod
    def _tagged(attachments: list[dict], *, subject: str, subject_kind: str, site_building: bool = False) -> list[dict]:
        """Copies of ``attachments``, each naming what it documents."""
        extra: dict[str, Any] = {"subject": subject, "subject_kind": subject_kind}
        if site_building:
            extra[_SITE_BUILDING_KEY] = True
        return [{**attachment, **extra} for attachment in attachments]

    @staticmethod
    def _queue_site_details(gateway, latitude: float, longitude: float, radius: float) -> None:
        """Best-effort ask REData to warm every resource on the site, so later passes find their attachments on the lookup rows."""
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError

        try:
            gateway.queue_cultural_resource_details(latitude, longitude, radius_meters=radius)
        except PropertyRecordsUnavailableError:
            logger.debug("CrisBuildingPanelSource: REData declined the site-wide detail queue", exc_info=True)

    @staticmethod
    def _campus_candidates(resources: list[dict], site_detail: dict, polygon: BaseGeometry | None, latitude: float, longitude: float, *, skip: set[str]) -> list[dict]:
        """The site's other CRIS buildings, nearest first, then any the site record links that the lookup missed.

        Args:
            resources: The lookup's resource dicts.
            site_detail: The site record's detail (its ``linked_resources`` roster), or ``{}``.
            polygon: The site's footprint; without one every building in the lookup counts.
            latitude: The pin's latitude.
            longitude: The pin's longitude.
            skip: Resource uuids already covered (the pin's own building and the site record).

        Returns:
            At most ``_MAX_SITE_BUILDINGS`` resource dicts, each with a ``uuid``.
        """
        from shapely.geometry import Point

        from urbanlens.dashboard.services.locations.site_scope import meters_between

        positioned: list[tuple[float, dict]] = []
        for resource in cris_only(resources):
            lat, lng = resource.get("source_latitude"), resource.get("source_longitude")
            if resource.get("resource_type") != _RESOURCE_TYPE or not resource.get("uuid") or lat is None or lng is None:
                continue
            if polygon is not None and not polygon.intersects(Point(float(lng), float(lat))):
                continue
            positioned.append((meters_between(float(lat), float(lng), latitude, longitude), resource))
        positioned.sort(key=lambda pair: pair[0])
        linked = [ref for ref in site_detail.get("linked_resources") or [] if isinstance(ref, dict) and ref.get("resource_type") == _RESOURCE_TYPE and ref.get("uuid")]

        candidates: list[dict] = []
        seen = set(skip)
        for resource in [resource for _distance, resource in positioned] + linked:
            if resource["uuid"] in seen:
                continue
            seen.add(resource["uuid"])
            candidates.append(resource)
        return candidates[:_MAX_SITE_BUILDINGS]

    def _campus_attachments(self, gateway, candidates: list[dict], *, started: float) -> list[dict]:
        """The campus buildings' attachments, each tagged with its building.

        A lookup row REData has already detailed costs nothing; the rest cost one detail fetch each, capped by
        ``_MAX_SITE_DETAIL_FETCHES`` and ``_SITE_DETAIL_BUDGET_SECONDS``. One building's failure skips only that building.

        Args:
            gateway: The :class:`RedataGateway` to fetch through.
            candidates: From :meth:`_campus_candidates`.
            started: ``time.monotonic()`` when the whole fetch began.

        Returns:
            The attachments, without extracted images (extraction is per-document AI work, spent only on the pin's own building and site).
        """
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError

        attachments: list[dict] = []
        live_fetches = 0
        for resource in candidates:
            record: dict | None = None
            if resource.get("attachments") or resource.get("detail_retrieved_at"):
                record = resource
            elif live_fetches < _MAX_SITE_DETAIL_FETCHES and time.monotonic() - started < _SITE_DETAIL_BUDGET_SECONDS:
                live_fetches += 1
                try:
                    record = gateway.fetch_cultural_resource_detail(resource["uuid"])
                except (PropertyRecordsUnavailableError, ValueError):
                    logger.debug("CrisBuildingPanelSource: no detail for campus building %s", resource["uuid"], exc_info=True)
            if record is None:
                continue
            resource_uuid = record.get("uuid") or resource["uuid"]
            own = [{**attachment, "resource_uuid": resource_uuid} for attachment in record.get("attachments") or [] if isinstance(attachment, dict)]
            attachments.extend(self._tagged(own, subject=resource_name(record) or resource_name(resource), subject_kind=_SUBJECT_BUILDING, site_building=True))
        return attachments

    @staticmethod
    def _resource_detail(gateway, resource: dict) -> dict:
        """One resource's detail record, degrading to the search row on failure.

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
    def _attachments_with_extracted_images(resource_uuid: str | None, attachments: list[dict], unextracted: dict[str, list[int]]) -> list[dict]:
        """The attachments, each document carrying whatever photos REData has already extracted from it.

        Extraction itself is not run here: REData does it synchronously and it can outlast the request, so a
        document never extracted is noted in ``unextracted`` for :func:`tasks.extract_cris_attachments`.

        Args:
            resource_uuid: The resource's REData uuid, or None when it couldn't be resolved (the attachments are
                returned unmodified).
            attachments: The resource's raw attachment list (photo + document kinds).
            unextracted: Resource uuid to the ids of its documents REData has not extracted yet, added to here.

        Returns:
            The same attachments, each carrying the ``resource_uuid`` it belongs to (one payload aggregates
            attachments from more than one resource - see :meth:`fetch`) and each document-kind entry an
            ``extracted_images`` list (possibly empty).
        """
        if not resource_uuid:
            return list(attachments)

        result: list[dict] = []
        for raw_attachment in attachments:
            attachment = dict(raw_attachment)
            attachment["resource_uuid"] = resource_uuid
            attachment_id = attachment.get("id")
            if attachment_kind(attachment) == _ATTACHMENT_KIND_DOCUMENT and attachment_id is not None:
                attachment["extracted_images"] = attachment.get("extracted_images") or []
                if not attachment.get("extracted_at"):
                    unextracted.setdefault(resource_uuid, []).append(attachment_id)
            result.append(attachment)
        return result

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the Building USN Point card from a cached CRIS payload."""
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

        return {"heading_name": usn_name, "meta": meta}

    def media_items(self, data: dict) -> list[MediaItem]:
        """Turn cached CRIS attachments (photos, documents, and extracted images) into gallery items.

        Args:
            data: This source's cached payload (see :meth:`fetch`). Each
                attachment carries the ``resource_uuid`` it belongs to, since
                one payload aggregates the nearest building's attachments and
                the site-level record's.

        Returns:
            One item per attachment of the pin's own building and site, proxied through ``PinCrisAttachmentView`` (never a raw REData URL). The other campus buildings' records are listed under Article > Sources instead.
        """
        from django.urls import reverse

        from urbanlens.dashboard.services.apis.assets.base import MediaItem

        default_uuid = data.get("resource_uuid")

        items: list[MediaItem] = []
        for attachment in data.get("attachments") or []:
            if attachment.get(_SITE_BUILDING_KEY):
                continue
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

    def documents_ready(self, data: dict, *, site_scope: bool) -> bool:
        """True once the attachments were fetched, and at site scope when a site-scope page asks.

        Args:
            data: This source's cached payload.
            site_scope: Whether the page describes a parcel/site rather than one building.

        Returns:
            Whether :meth:`source_documents` can be trusted for this row.
        """
        if not self.media_is_ready(data):
            return False
        return not site_scope or not data or data.get("site_scope") is True

    def source_documents(self, data: dict, *, site_scope: bool) -> list[SourceDocument]:
        """The PDF attachments in a cached payload: the pin's own building and site, plus every campus building at site scope.

        Args:
            data: This source's cached payload.
            site_scope: Whether the page describes a parcel/site rather than one building.

        Returns:
            One document per ``(resource, attachment)``, identified as ``"<resource uuid>.<attachment id>"``.
        """
        default_uuid = data.get("resource_uuid")
        include_campus = site_scope and data.get("site_scope") is True
        documents: list[SourceDocument] = []
        seen: set[str] = set()
        for attachment in data.get("attachments") or []:
            if not is_pdf_document(attachment) or (attachment.get(_SITE_BUILDING_KEY) and not include_campus):
                continue
            attachment_id = attachment.get("id")
            resource_uuid = attachment.get("resource_uuid") or default_uuid
            if attachment_id is None or not resource_uuid:
                continue
            document_id = f"{resource_uuid}.{attachment_id}"
            if document_id in seen:
                continue
            seen.add(document_id)
            title = str(attachment.get("name") or attachment.get("attachment_type") or "CRIS record")
            documents.append(SourceDocument(document_id=document_id, title=title, content_type=_PDF_CONTENT_TYPE, subject=str(attachment.get("subject") or ""), subject_kind=str(attachment.get("subject_kind") or "")))
        return documents

    @staticmethod
    def _attachment_ref(document: SourceDocument) -> tuple[str, int]:
        """Split a document id back into REData's ``(resource uuid, attachment id)``.

        Raises:
            ValueError: The id is not one :meth:`source_documents` minted.
        """
        resource_uuid, _, attachment_id = document.document_id.rpartition(".")
        if not resource_uuid:
            raise ValueError(document.document_id)
        return resource_uuid, int(attachment_id)

    def download_document(self, document: SourceDocument) -> tuple[bytes, str]:
        """Fetch one listed CRIS attachment's bytes from REData.

        Args:
            document: A document :meth:`source_documents` listed.

        Returns:
            ``(content, content_type)`` as REData reported them.

        Raises:
            DocumentUnavailableError: REData or CRIS could not supply it, the file is over the proxy's size limit, or REData is throttled or unconfigured.
        """
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway
        from urbanlens.dashboard.services.core.gateway import GatewayRequestError

        try:
            resource_uuid, attachment_id = self._attachment_ref(document)
            return RedataGateway().download_cultural_resource_attachment(resource_uuid, attachment_id)
        except (GatewayRequestError, ValueError) as exc:
            raise DocumentUnavailableError(document.document_id) from exc

    def document_cache_key(self, document: SourceDocument) -> str:
        """The gallery attachment proxy's own cache key, so a document viewed in either place is fetched once."""
        resource_uuid, _, attachment_id = document.document_id.rpartition(".")
        return f"ul_cris_attachment_{resource_uuid}_{attachment_id}"

    def api_payload(self, pin: Pin) -> dict[str, Any] | None:
        """The CRIS record as both an information card and its attachments.
        Neither inherited ``api_payload`` would do on its own - ``InfoPanelSource``'s would drop the attachments and ``GalleryMediaSource``'s would drop the eligibility card - so this composes both from the *one* cached row rather than reading it twice.

        Args:
            pin: The pin whose panel is being read. ``render_context`` branches
                on it - a parcel-scope pin gets the historic-district record
                rather than an arbitrary building from the same lookup.

        Returns:
            ``{"info": ..., "media": [...]}`` with ``info`` possibly None (a location inside a historic district but with no surveyed building of its own still has attachments worth serving), or None when nothing has landed yet or the record yields neither.
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

    def gate(self) -> bool:
        """Requires REData to be configured - this source has no other backend.
        Without it the cycle picks candidates, every fetch raises, and the run logs one exception per location."""
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured

        return redata_configured()

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Find the CRIS "building" resource nearest this location and return its flattened info.
        Shares ``cache_source`` with :class:`CrisBuildingPanelSource`, so whichever of panel-fetch or background enrichment runs first for a Location fills in for the other."""
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway

        query_key = f"{location.latitude},{location.longitude}"
        try:
            resources = RedataGateway().lookup_cultural_resources(float(location.latitude), float(location.longitude), radius_meters=_RADIUS_METERS, provider=_PROVIDER)
        except (PropertyRecordsUnavailableError, ValueError):
            return None, query_key
        district = site_resource_attributes(resources, float(location.latitude), float(location.longitude))
        building = nearest_resource(resources, _RESOURCE_TYPE, float(location.latitude), float(location.longitude))
        if building is None:
            return ({"district": district} if district else None), query_key
        data = dict(building.get("attributes") or {})
        data.update(building_position(building))
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
