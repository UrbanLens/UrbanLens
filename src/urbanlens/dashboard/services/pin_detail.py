"""Full pin-detail payload for the external API's ``GET /pins/{slug}/``.

Builds on top of ``services.pin_sync.serialize_sync_pin`` (the map payload
plus sync-only fields) rather than duplicating it, then layers on everything
a sync client already has but a detail view still needs: description-adjacent
dates, security indicators, personal notes/aliases/links, custom fields, the
property boundary, the cover photo, and the discovered wiki slug.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.external_api.serializers import PinAliasSerializer, PinLinkSerializer, PinNoteSerializer
from urbanlens.dashboard.models.abstract.security import SECURITY_FIELDS
from urbanlens.dashboard.models.boundary.model import Boundary, BoundaryType
from urbanlens.dashboard.models.custom_fields.model import CustomFieldValue
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.map_pins import MapPinPayloadService
from urbanlens.dashboard.services.pin_sync import serialize_sync_pin

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def _isoformat_or_none(value) -> str | None:
    return value.isoformat() if value is not None else None


def _boundary_geojson(pin: Pin) -> dict[str, Any] | None:
    """The pin's effective property boundary as a GeoJSON geometry dict, if any."""
    polygon = Boundary.objects.effective_polygon_for_pin(pin, BoundaryType.PROPERTY)
    return json.loads(polygon.geojson) if polygon is not None else None


def _custom_fields(pin: Pin) -> list[dict[str, Any]]:
    values = CustomFieldValue.objects.filter(pin=pin).select_related("field")
    return [
        {
            "id": value.field_id,
            "name": value.field.name,
            "type": value.field.field_type,
            "value": value.export_value(),
        }
        for value in values
    ]


def build_pin_detail(pin: Pin, profile: Profile) -> dict[str, Any]:
    """Assemble the full detail payload for one pin, owned by *profile*.

    Args:
        pin: The pin to serialize. Caller is responsible for ownership
            checks - this never filters by profile itself.
        profile: The requesting profile, needed for the same
            label-customization-aware serialization the sync feed uses.

    Returns:
        A JSON-serializable dict: every sync-feed field plus detail-only
        fields (dates, security, notes, aliases, links, custom fields,
        boundary, cover photo, wiki slug, counts).
    """
    service = MapPinPayloadService(profile)
    pin = service.prepare_queryset(Pin.objects.filter(pk=pin.pk)).select_related("parent_pin", "wiki", "cover_photo", "location").get()
    payload = serialize_sync_pin(service, pin)

    payload["official_name"] = pin.effective_official_name or None
    # Address components, split out from the combined "address" string the
    # sync payload already carries - geocoded from the pin's Location, never
    # user-writable. Location itself is never exposed directly (it would leak
    # a place's existence to a caller who hasn't pinned it), but reading these
    # through a pin the caller already owns is safe.
    payload["city"] = pin.effective_city
    payload["state"] = pin.effective_state
    payload["county"] = pin.effective_county
    payload["country"] = pin.effective_country
    payload["zipcode"] = pin.location.zipcode if pin.location else None
    payload["date_built"] = _isoformat_or_none(pin.date_built)
    payload["date_abandoned"] = _isoformat_or_none(pin.date_abandoned)
    payload["date_last_active"] = _isoformat_or_none(pin.date_last_active)
    payload["security"] = {field: getattr(pin, field) for field, _label in SECURITY_FIELDS}
    wiki = pin.wiki
    # The slug that actually routes to this pin's wiki. Every wiki-scoped
    # endpoint resolves through services.wiki_access.resolve_visible_wiki,
    # which takes a *Location* slug/uuid - so this, not wiki_slug below, is
    # what a client feeds to /wikis/{location_slug}/.
    payload["location_slug"] = pin.location.ensure_slug()
    # Informational only: Wiki.slug is an independent field from the Location's
    # slug and is NOT accepted by any wiki route. Kept for clients that already
    # read it (and for display), but it must never be used for navigation.
    payload["wiki_slug"] = wiki.slug if wiki is not None and wiki.slug else None
    cover_photo = pin.cover_photo
    payload["cover_photo_url"] = cover_photo.image.url if cover_photo is not None and cover_photo.image else None
    payload["boundary"] = _boundary_geojson(pin)

    notes = list(pin.notes.order_by("-created"))
    aliases = list(pin.aliases.all())
    links = list(pin.links.all())
    # The same serializers the dedicated sub-resource endpoints use, so a pin's
    # nested notes/aliases/links and the ones served by
    # ``pins/{slug}/notes/`` et al. cannot describe the same row differently.
    # ``pin`` goes into the alias context so is_current is resolved without a
    # query per row.
    payload["notes"] = PinNoteSerializer(notes, many=True).data
    payload["aliases"] = PinAliasSerializer(aliases, many=True, context={"pin": pin}).data
    payload["links"] = PinLinkSerializer(links, many=True).data
    payload["custom_fields"] = _custom_fields(pin)

    payload["note_count"] = len(notes)
    payload["alias_count"] = len(aliases)
    payload["link_count"] = len(links)

    return payload
