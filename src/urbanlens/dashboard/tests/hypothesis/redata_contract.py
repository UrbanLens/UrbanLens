"""What UrbanLens reads from REData: one row per reader and REData operation, with the response fields it uses.

``test_redata_consumer_contract.py`` checks every row against REData's OpenAPI document, vendored at
``VENDORED_SCHEMA`` by ``bin/vendor_redata_schema.py``. When a reader starts using a REData field, add the field to its
row; when it starts calling an operation, add a row and re-vendor.

Field paths are dot-separated keys from the body root. ``key[]`` steps into each item of the array at ``key``, a
leading ``[]`` into a body that is itself an array, and ``{}`` into each value of a free-keyed map. A final ``*`` marks
an object REData publishes untyped (``record_payload``, ``attributes``), whose keys UrbanLens reads without the
schema's say-so.

``gaps`` holds fields a reader looks for that REData does not publish there - a tolerated fallback or a known
mismatch. The test requires each to stay unpublished, so the row is revisited when either side changes.

``optional`` holds top-level fields REData publishes that an older REData UrbanLens still supports does not send; a
reader defaults any of them it reads. The vendoring script leaves them out of the body's ``required``, and the test
holds both that and that REData still publishes them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

VENDORED_SCHEMA = Path(__file__).parent / "fixtures" / "redata_openapi.json"
#: REData's Overture shard boxes (``parcels.services.overture.shards.US_STATE_BBOXES``), vendored by
#: ``bin/vendor_redata_schema.py --shards`` from the same REData as ``VENDORED_SCHEMA``.
VENDORED_OVERTURE_SHARDS = Path(__file__).parent / "fixtures" / "redata_overture_shards.json"


@dataclass(frozen=True, slots=True)
class Read:
    """One UrbanLens reader's use of one REData response.

    Attributes:
        reader: Where the reading happens, relative to ``src/urbanlens/dashboard/``.
        method: The HTTP method, lower case, as OpenAPI keys it.
        path: The operation's path, as REData's schema spells it.
        fields: Field paths the reader uses, all of which REData must publish.
        status: The response status read.
        gaps: Field paths the reader looks for that REData does not publish there.
        optional: Top-level fields an older REData UrbanLens supports does not send, so the reader must default them.
    """

    reader: str
    method: str
    path: str
    fields: tuple[str, ...]
    status: str = "200"
    gaps: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()


def _get(
    reader: str,
    path: str,
    *fields: str,
    status: str = "200",
    gaps: tuple[str, ...] = (),
    optional: tuple[str, ...] = (),
) -> Read:
    return Read(reader, "get", path, fields, status, gaps, optional)


def _post(reader: str, path: str, *fields: str, status: str = "200", gaps: tuple[str, ...] = ()) -> Read:
    return Read(reader, "post", path, fields, status, gaps)


_CONTEXT = "services/apis/locations/redata_context_gateway.py"
_BREAKER = "services/core/upstream_breaker.py"
_PARCELS = "services/apis/property_records/redata_gateway.py"
_PLACES = "services/apis/locations/google/redata_places_gateway.py"
_PROPERTY = "plugins/builtin/property_records.py"
_CRIS = "plugins/builtin/cris_buildings.py"
_ENVELOPE = ("count", "complete", "results", "providers")

#: Near-a-coordinate endpoints read through ``RedataLocationContextGateway``'s envelope.
_ENVELOPED = (
    "/api/v1/air-quality/",
    "/api/v1/buildings/",
    "/api/v1/cultural-resources/lookup/",
    "/api/v1/elevation/",
    "/api/v1/geocode/",
    "/api/v1/geocode/reverse/",
    "/api/v1/hazards/",
    "/api/v1/historical-features/",
    "/api/v1/hydrology/",
    "/api/v1/imagery/",
    "/api/v1/incidents/",
    "/api/v1/land-cover/",
    "/api/v1/media/lookup/",
    "/api/v1/nature-observations/",
    "/api/v1/permits/",
    "/api/v1/points-of-interest/lookup/",
    "/api/v1/reference-documents/search/",
    "/api/v1/soil/",
    "/api/v1/underground/",
    "/api/v1/walkability/",
    "/api/v1/weather/",
)
#: Read through the same envelope, but REData answers them without ``complete``/``providers``, which then default.
_BARE_ENVELOPED = ("/api/v1/parks/nearby/", "/api/v1/weather/history/")
#: Operations whose 503 body both the context gateway and the breaker read.
_CONTEXT_ERRORS = (
    *_ENVELOPED,
    "/api/v1/imagery/timeline/",
    "/api/v1/imagery/{uuid}/download/",
    "/api/v1/parks/{park_code}/alerts/",
    "/api/v1/parks/{park_code}/campgrounds/",
    "/api/v1/parks/{park_code}/visitor-centers/",
    "/api/v1/search/news/",
    "/api/v1/search/web/",
    "/api/v1/street-view/timeline/",
    "/api/v1/weather/history/",
)
#: 503 bodies that name each provider's state, which the breaker reads to trip one provider rather than the pool.
_PER_PROVIDER_503 = frozenset(
    {
        "/api/v1/buildings/",
        "/api/v1/cultural-resources/lookup/",
        "/api/v1/geocode/reverse/",
        "/api/v1/hazards/",
        "/api/v1/imagery/",
        "/api/v1/points-of-interest/lookup/",
        "/api/v1/reference-documents/search/",
        "/api/v1/street-view/timeline/",
    },
)
#: Parcel sub-resources whose 404 ``RedataGateway`` reads. REData sends ``message`` with ``error``, but documents only ``error``.
_PARCEL_404S = (
    "/api/v1/floorplans/{uuid}/",
    "/api/v1/parcels/{parcel_uuid}/assessments/",
    "/api/v1/parcels/{parcel_uuid}/boundaries/",
    "/api/v1/parcels/{parcel_uuid}/buildings/",
    "/api/v1/parcels/{parcel_uuid}/coverage/",
    "/api/v1/parcels/{parcel_uuid}/demographics/",
    "/api/v1/parcels/{parcel_uuid}/floorplans/",
    "/api/v1/parcels/{parcel_uuid}/liens/",
    "/api/v1/parcels/{parcel_uuid}/listings/",
    "/api/v1/parcels/{parcel_uuid}/national-parks/",
    "/api/v1/parcels/{parcel_uuid}/owners/",
    "/api/v1/parcels/{parcel_uuid}/sale-records/",
    "/api/v1/parcels/{parcel_uuid}/sales/",
    "/api/v1/parcels/{parcel_uuid}/tax-payments/",
)
_PARCEL_503S = (
    "/api/v1/cultural-resources/lookup/",
    "/api/v1/parcels/lookup/",
    "/api/v1/parcels/{parcel_uuid}/assessments/",
    "/api/v1/parcels/{parcel_uuid}/demographics/",
    "/api/v1/parcels/{parcel_uuid}/land-use-areas/",
    "/api/v1/parcels/{parcel_uuid}/national-parks/",
    "/api/v1/parcels/{parcel_uuid}/sale-records/",
)
#: Unfiltered parcel calls REData (0.3.7) answers 503 while it computes the parcel, waiting ``retry_after``: the code in
#: ``error`` before 0.3.10, in ``pending`` from it.
_PARCEL_PENDING_503S = ("/api/v1/parcels/{parcel_uuid}/boundaries/", "/api/v1/parcels/{parcel_uuid}/buildings/")
_PLACES_ERRORS = (
    ("/api/v1/places/autocomplete/", "503"),
    ("/api/v1/places/search/nearby/", "400"),
    ("/api/v1/places/search/nearby/", "503"),
    ("/api/v1/places/search/text/", "400"),
    ("/api/v1/places/search/text/", "503"),
    ("/api/v1/places/{place_id}/", "503"),
    ("/api/v1/places/{place_id}/photos/{id}/download/", "503"),
)

#: A building as REData reconciles it, for ``/parcels/{uuid}/buildings/`` rows. ``year_built_basis`` (REData 0.3.6) says
#: whether ``year_built`` is the building's own; UrbanLens reads a row without it as the parcel's.
_BUILDING = ("[].latitude", "[].longitude", "[].name", "[].building_number", "[].year_built", "[].year_built_basis")
#: A cultural resource as ``lookup`` lists it and ``fetch-detail`` returns it under ``resource``.
_RESOURCE = (
    "uuid",
    "provider",
    "resource_type",
    "name",
    "geometry",
    "source_latitude",
    "source_longitude",
    "attributes.*",
    "detail_retrieved_at",
    "attachments[].id",
    "attachments[].kind",
    "attachments[].name",
    "attachments[].attachment_type",
    "attachments[].content_type",
    "attachments[].extracted_at",
    "attachments[].extracted_images[].id",
    "linked_resources[].uuid",
    "linked_resources[].resource_type",
    "linked_resources[].name",
)
#: Linked resources are references; ``is_detailed`` and friends look for detail on them and fall back to a bounded detail fetch.
_RESOURCE_GAPS = (
    "linked_resources[].attachments",
    "linked_resources[].detail_retrieved_at",
    "linked_resources[].source_latitude",
    "linked_resources[].source_longitude",
    "linked_resources[].attributes",
)
_MODEL = (
    "active",
    "active.version",
    "active.algorithm",
    "active.trained_at",
    "active.metrics.*",
    "active.baseline_metrics.*",
    "features[].name",
    "features[].kind",
    "features[].description",
    "feature_schema_fingerprint",
)


def _within(prefix: str, fields: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{prefix}{field}" for field in fields)


READS: tuple[Read, ...] = (
    # Envelopes and error bodies, read the same way for every near-a-coordinate endpoint.
    *(_get(_CONTEXT, path, *_ENVELOPE) for path in _ENVELOPED),
    *(_get(_CONTEXT, path, "count", "results", gaps=("complete", "providers")) for path in _BARE_ENVELOPED),
    *(_get(_CONTEXT, path, "error", "message", status="503") for path in _CONTEXT_ERRORS),
    _post(_CONTEXT, "/api/v1/routes/", "error", "message", status="503"),
    *(
        _get(
            _BREAKER,
            path,
            "error",
            "message",
            *(("providers[].provider", "providers[].status") if path in _PER_PROVIDER_503 else ()),
            status="503",
        )
        for path in _CONTEXT_ERRORS
    ),
    _get(_BREAKER, "/api/v1/maps/georeferences/{uuid}/tiles/{z}/{x}/{y}.png", "error", "message", status="503"),
    _get(_BREAKER, "/api/v1/tiles/{layer}/{z}/{x}/{y}/", "error", "message", status="503"),
    _post(_BREAKER, "/api/v1/routes/", "error", "message", status="503"),
    *(_get(_PARCELS, path, "error", status="404", gaps=("message",)) for path in _PARCEL_404S),
    *(_get(_PARCELS, path, "error", "message", status="503") for path in _PARCEL_503S),
    *(
        _get(_PARCELS, path, "error", "message", "pending", "retry_after", status="503", optional=("pending",))
        for path in _PARCEL_PENDING_503S
    ),
    *(_get(_PLACES, path, "error", status=status) for path, status in _PLACES_ERRORS),
    # Places.
    _get(
        "services/apis/locations/places_resolution.py",
        "/api/v1/places/{place_id}/",
        "name",
        "formatted_address",
        "google_maps_uri",
        "latitude",
        "longitude",
        "photo_records[].id",
    ),
    _get(
        "services/apis/locations/places_resolution.py",
        "/api/v1/places/search/nearby/",
        *_within("results[].", ("place_id", "name", "latitude", "longitude", "formatted_address", "types")),
    ),
    _get(
        "services/apis/locations/places_resolution.py",
        "/api/v1/places/autocomplete/",
        "results[].place_id",
        "results[].main_text",
        "results[].secondary_text",
    ),
    _get(_PLACES, "/api/v1/places/search/text/", "results[]"),
    _post(
        "services/apis/locations/google/redata_cid_gateway.py",
        "/api/v1/places/resolve-cids/",
        "results.{}.lat",
        "results.{}.lng",
        "pending[]",
    ),
    _get(
        "plugins/builtin/redata_place_details.py",
        "/api/v1/places/cid/{cid}/",
        *("cid", "name", "rating", "review_count", "price_level", "hours.*", "phone_number", "website", "category"),
        *("media[].kind", "media[].id", "media[].content_type"),
    ),
    _get(
        "services/demo/locations.py",
        "/api/v1/public-locations/",
        "results[].latitude",
        "results[].longitude",
        "results[].name",
    ),
    # Labels and photo relevance models.
    _post(
        "services/labels/redata_suggestions.py", "/api/v1/labels/suggest/", "results[].label_id", "results[].confidence"
    ),
    _get(
        "controllers/site_admin_models.py",
        "/api/v1/labels/model/",
        *_MODEL,
        "ranker",
        "active.training_rows",
        "active.ranking_metrics.*",
    ),
    _get("controllers/site_admin_models.py", "/api/v1/photos/model/", *_MODEL, "scorer", "active.training_photos"),
    _post(
        "services/photos/redata_relevance.py",
        "/api/v1/photos/",
        *_within("results.{}.", ("confidence", "scored_at", "scorer", "model_version")),
    ),
    _post("tasks.py", "/api/v1/photos/votes/", "unknown_photo_ids[]"),
    # Panels over the near-a-coordinate endpoints.
    _get(
        "plugins/builtin/redata_air_quality.py",
        "/api/v1/air-quality/",
        *_within("results[].", ("source_kind", "us_aqi", "european_aqi", "pm2_5", "ozone", "observed_at")),
    ),
    _get(
        "plugins/builtin/hazard_history.py",
        "/api/v1/hazards/",
        *_within("results[].", ("provider", "event_type", "title", "occurred_at", "magnitude", "url", "attributes.*")),
    ),
    _get(
        "plugins/builtin/usgs_earthquakes.py",
        "/api/v1/hazards/",
        *_within("results[].", ("event_type", "occurred_at", "magnitude", "title", "url")),
    ),
    _get(
        "plugins/builtin/redata_incidents.py",
        "/api/v1/incidents/",
        "results[].category",
        "results[].occurred_at",
        "results[].offense_description",
    ),
    _get(
        "plugins/builtin/redata_permits.py",
        "/api/v1/permits/",
        *_within("results[].", ("kind", "issued_at", "work_type", "status", "estimated_cost", "url", "attributes.*")),
    ),
    _get(
        "plugins/builtin/redata_underground.py",
        "/api/v1/underground/",
        *_within("results[].", ("is_enterable", "kind", "name", "layer", "attributes.*")),
    ),
    _get(
        "plugins/builtin/redata_historical_features.py",
        "/api/v1/historical-features/",
        *_within("results[].", ("start_year", "end_year", "kind", "name", "source_note")),
    ),
    _get(
        "plugins/builtin/redata_hydrology.py",
        "/api/v1/hydrology/",
        *_within("results[].", ("kind", "name", "distance_meters", "area_sq_km", "attributes.*")),
    ),
    _get(
        "plugins/builtin/inaturalist.py",
        "/api/v1/nature-observations/",
        *_within("results[].", ("common_name", "scientific_name", "observed_on", "url", "attributes.*")),
    ),
    _get("plugins/builtin/redata_site_conditions.py", "/api/v1/land-cover/", "results[].class_name"),
    _get(
        "plugins/builtin/redata_site_conditions.py",
        "/api/v1/walkability/",
        "results[].index",
        "results[].band",
        "results[].transit_distance_meters",
    ),
    _get(
        "plugins/builtin/redata_site_conditions.py",
        "/api/v1/soil/",
        *_within(
            "results[].", ("map_unit_name", "component_name", "component_percent", "drainage_class", "hydrologic_group")
        ),
    ),
    _get(
        "plugins/builtin/open_elevation.py",
        "/api/v1/elevation/",
        "results[].elevation_meters",
        "results[].dataset",
        "results[].provider",
    ),
    _get(
        "services/apis/locations/weather_resolution.py",
        "/api/v1/weather/",
        "results[].provider",
        "results[].forecast.*",
        "results[].sun.*",
    ),
    _get(
        "services/locations/visit_weather.py",
        "/api/v1/weather/history/",
        *_within(
            "results[].",
            (
                "date",
                "temperature_max_c",
                "temperature_min_c",
                "temperature_mean_c",
                "precipitation_mm",
                "snowfall_cm",
                "wind_speed_max_kmh",
                "wind_gusts_max_kmh",
            ),
        ),
    ),
    _get(
        "plugins/builtin/redata_aerial_media.py",
        "/api/v1/media/lookup/",
        *_within("results[].", ("is_aerial", "url", "cached_url", "thumbnail_url", "title", "credit")),
    ),
    _get(
        "plugins/builtin/yelp.py",
        "/api/v1/points-of-interest/lookup/",
        *_within("results[].", ("category", "name", "url", "attributes.*")),
    ),
    _get(
        "plugins/builtin/epa_echo.py",
        "/api/v1/points-of-interest/lookup/",
        *_within("results[].", ("external_id", "name", "latitude", "longitude", "attributes.*")),
    ),
    _get(
        "services/apis/locations/boundaries/overture.py",
        "/api/v1/buildings/",
        *_within("results[].", ("name", "geometry", "attributes.*")),
    ),
    _get(
        "services/apis/locations/boundaries/overture.py",
        "/api/v1/points-of-interest/lookup/",
        *_within("results[].", ("name", "category", "latitude", "longitude", "attributes.*")),
    ),
    _get(
        "plugins/builtin/redata_site_features.py",
        "/api/v1/points-of-interest/lookup/",
        *_within("results[].", ("category", "name", "description", "url")),
    ),
    _get(
        "services/apis/locations/redata_reference_documents_gateway.py",
        "/api/v1/reference-documents/search/",
        *_within(
            "results[].", ("url", "thumbnail_url", "title", "description", "latitude", "longitude", "attributes.*")
        ),
    ),
    _get(
        "services/apis/locations/geocode_resolution.py", "/api/v1/geocode/", "results[].latitude", "results[].longitude"
    ),
    _get(
        "plugins/builtin/photon.py",
        "/api/v1/geocode/reverse/",
        *_within("results[].", ("locality", "region", "country", "house_number", "street", "postal_code")),
    ),
    _get(
        "plugins/builtin/redata_historic_registers.py",
        "/api/v1/cultural-resources/lookup/",
        *_within(
            "results[].",
            (
                "provider",
                "resource_type",
                "scope",
                "name",
                "status",
                "year_built",
                "architectural_style",
                "use_type",
                "external_id",
                "source_latitude",
                "source_longitude",
                "geometry.*",
                "attributes.*",
            ),
        ),
    ),
    # Imagery.
    _get(
        "plugins/builtin/satellite_imagery.py",
        "/api/v1/imagery/",
        *_within(
            "results[].",
            (
                "uuid",
                "provider",
                "url",
                "delivery",
                "captured_label",
                "captured_on",
                "attribution",
                "resolution_meters",
                "max_zoom",
                "min_zoom",
                "attributes.*",
            ),
        ),
    ),
    _get(
        "services/locations/imagery_timeline.py",
        "/api/v1/imagery/timeline/",
        "captures[].captured_on",
        "captures[].provider",
        *_within(
            "captures[].asset.",
            (
                "uuid",
                "provider",
                "url",
                "delivery",
                "captured_label",
                "captured_on",
                "attribution",
                "resolution_meters",
                "max_zoom",
                "min_zoom",
                "attributes.*",
            ),
        ),
        *_within(
            "providers_timeline[].",
            (
                "provider",
                "time_series[].intervals[].*",
                "time_series[].continuous",
                "time_series[].time_series_asset_uuid",
            ),
        ),
    ),
    _post(
        "plugins/builtin/satellite_imagery.py",
        "/api/v1/imagery/capture/",
        "uuid",
        "resolution_meters",
        "max_zoom",
        "min_zoom",
        "attributes.*",
    ),
    _get(
        "services/apis/locations/redata_media_gateway.py",
        "/api/v1/street-view/timeline/",
        "dates[].captured_on",
        *_within("dates[].representative.", ("image_url", "thumbnail_url", "heading_degrees", "latitude", "longitude")),
    ),
    # REData's ``attributes.mirror_gone``: a row whose source image is gone is left out (P325).
    _get("services/locations/redata_point_data.py", "/api/v1/media/lookup/", "results[].attributes.*"),
    _get(
        "services/locations/redata_point_data.py",
        "/api/v1/street-view/timeline/",
        "complete",
        "dates[].captured_on",
        "dates[].representative.attributes.*",
        *_within("dates[].captures[].", ("latitude", "longitude", "is_panoramic", "attributes.*")),
    ),
    _get(
        "plugins/builtin/redata_historical_map_media.py",
        "/api/v1/maps/",
        *_within(
            "results[].sheet.",
            ("thumbnail_url", "iiif_info_url", "title", "date_text", "attribution", "landing_page_url"),
        ),
    ),
    _get(
        "controllers/map_overlays.py",
        "/api/v1/maps/",
        "results[].contains_point",
        *_within(
            "results[].sheet.", ("title", "date_text", "kind", "attribution", "thumbnail_url", "landing_page_url")
        ),
        *_within("results[].georeference.", ("uuid", "bounds", "transformation", "rmse_meters", "gcp_count")),
    ),
    _get(
        "services/apis/locations/redata_basemap_tiles_gateway.py",
        "/api/v1/tiles/sources/",
        *_within(
            "sources[].",
            (
                "id",
                "name",
                "url_template",
                "style_url",
                "source_type",
                "attribution",
                "min_zoom",
                "max_zoom",
                "fallback_attribution",
                "fallback_min_zoom",
                "fallback_max_zoom",
            ),
        ),
        gaps=("results",),
    ),
    # Capabilities, parks, routing, search.
    _get(
        "services/apis/locations/redata_capabilities_gateway.py",
        "/api/v1/capabilities/",
        "domains[].tag",
        "domains[].applicable_providers",
    ),
    _get(
        "controllers/site_admin.py",
        "/api/v1/capabilities/",
        *_within(
            "domains[].",
            (
                "label",
                "tag",
                "endpoint",
                "prewarmed",
                "providers[].tag",
                "providers[].billable",
                "providers[].radius_pinned",
            ),
        ),
        *_within("text_domains[].", ("label", "tag", "endpoint", "providers[].tag", "providers[].billable")),
    ),
    _get(
        "plugins/builtin/nps.py",
        "/api/v1/parks/nearby/",
        *_within(
            "results[].", ("park_code", "full_name", "url", "description", "designation", "states", "directions_url")
        ),
        *_within("results[].", ("images.*", "activities.*", "entrance_fees.*", "operating_hours.*")),
    ),
    _get(
        "services/map/nearby_places.py",
        "/api/v1/parks/nearby/",
        *_within("results[].", ("park_code", "full_name", "latitude", "longitude", "description", "url", "states")),
    ),
    _get("plugins/builtin/nps.py", "/api/v1/parks/{park_code}/alerts/", "[].title", "[].category", "[].url"),
    _get("plugins/builtin/nps.py", "/api/v1/parks/{park_code}/visitor-centers/", "[].name"),
    _get("plugins/builtin/nps.py", "/api/v1/parks/{park_code}/campgrounds/", "[].name"),
    _post(
        "services/apis/locations/redata_routing_gateway.py",
        "/api/v1/routes/",
        "route.distance_meters",
        "route.duration_seconds",
    ),
    _get(
        "plugins/builtin/gdelt.py", "/api/v1/search/news/", *_within("results[].", ("link", "date", "title", "snippet"))
    ),
    # REData 0.3.7 says whether GDELT answered; an older one sends only ``results``, read as complete.
    _get(
        "services/apis/locations/redata_search_gateway.py",
        "/api/v1/search/news/",
        "results",
        "complete",
        "providers[].provider",
        "providers[].status",
        optional=("complete", "degraded", "providers"),
    ),
    _get(
        "controllers/pin.py",
        "/api/v1/search/web/",
        *_within("results[].", ("link", "date", "thumbnail", "title", "snippet")),
    ),
    _get(
        "plugins/builtin/google_images.py",
        "/api/v1/search/web/",
        *_within("results[].", ("thumbnail", "title", "link", "snippet")),
    ),
    _get(
        "plugins/builtin/searxng_images.py",
        "/api/v1/search/web/",
        *_within("results[].", ("thumbnail", "title", "link", "snippet")),
    ),
    # Parcels.
    _get(_PARCELS, "/api/v1/parcels/lookup/", "uuid", "record_payload.*", "parcel_geometry"),
    _get(_PARCELS, "/api/v1/parcels/lookup/", "error", "message", "links.{}", status="404"),
    _get(_PROPERTY, "/api/v1/parcels/{parcel_uuid}/coverage/", "{}.available"),
    _get("services/map/land_use_areas.py", "/api/v1/parcels/{parcel_uuid}/land-use-areas/", "{}.name", "{}.geometry"),
    _get(
        _PROPERTY,
        "/api/v1/parcels/{parcel_uuid}/assessments/",
        *_within("results[].", ("parcel_identifier", "total_value", "tax_year", "value_stage")),
        "complete",
        "providers[].provider",
        "providers[].status",
    ),
    _get(
        _PROPERTY,
        "/api/v1/parcels/{parcel_uuid}/sale-records/",
        *_within("results[].", ("situs_address", "sale_date", "sale_price", "attributes.*")),
        "complete",
        "providers[].provider",
        "providers[].status",
    ),
    _get(
        _PROPERTY,
        "/api/v1/parcels/{parcel_uuid}/liens/",
        "next",
        *_within("results[].", ("lien_type", "amount", "filed_date", "status")),
    ),
    _get(
        _PROPERTY, "/api/v1/parcels/{parcel_uuid}/tax-payments/", "next", "results[].tax_year", "results[].delinquent"
    ),
    _get(
        _PROPERTY,
        "/api/v1/parcels/{parcel_uuid}/owners/",
        "next",
        *_within(
            "results[].",
            (
                "source",
                "name",
                "parcels[]",
                "current",
                "company_name",
                "mailing_address",
                "care_of",
                "phone",
                "email",
                "first_observed_at",
                "last_observed_at",
            ),
        ),
    ),
    _get(
        _PROPERTY,
        "/api/v1/parcels/{parcel_uuid}/sales/",
        "next",
        *_within("results[].", ("source", "sale_date", "sale_price", "grantor", "grantee", "doc_type", "doc_number")),
    ),
    _get(
        _PROPERTY,
        "/api/v1/parcels/{parcel_uuid}/demographics/",
        *_within(
            "demographics.",
            (
                "population",
                "median_household_income",
                "median_home_value",
                "median_gross_rent",
                "percent_owner_occupied",
                "percent_renter_occupied",
            ),
        ),
    ),
    _get(_PROPERTY, "/api/v1/parcels/{parcel_uuid}/national-parks/", "containing_park.full_name"),
    _get("plugins/builtin/nps.py", "/api/v1/parcels/{parcel_uuid}/national-parks/", "containing_park.park_code"),
    _get(
        "plugins/builtin/loopnet.py",
        "/api/v1/parcels/{parcel_uuid}/listings/",
        "refresh_queued",
        *_within("results[].", ("uuid", "loopnet_url", "title", "photos[].id")),
    ),
    _get(
        "templates/dashboard/partials/pins/pin_loopnet.html",
        "/api/v1/parcels/{parcel_uuid}/listings/",
        *_within(
            "results[].",
            (
                "title",
                "loopnet_url",
                "market_status",
                "price_text",
                "listing_type",
                "building_sqft",
                "attributes.*",
                "duplicate_links.*",
            ),
        ),
    ),
    _get(
        "plugins/builtin/parcel_buildings.py",
        "/api/v1/parcels/{parcel_uuid}/buildings/",
        *_BUILDING,
        *_within(
            "[].",
            ("ref", "parent_ref", "child_refs", "overlap_refs", "is_on_property", "sources[].source", "geometry.*"),
        ),
    ),
    _get(
        "plugins/builtin/redata_building_attributes.py",
        "/api/v1/parcels/{parcel_uuid}/buildings/",
        *_BUILDING,
        "[].sources[].source",
    ),
    _get(
        "services/apis/locations/boundaries/redata.py",
        "/api/v1/parcels/{parcel_uuid}/buildings/",
        "[].latitude",
        "[].longitude",
        "[].on_parcel",
        "[].match_scope",
    ),
    _get(
        "services/pins/pin_restructure.py",
        "/api/v1/parcels/{parcel_uuid}/buildings/",
        *_BUILDING,
        "[].address",
        "[].geometry.*",
        "[].sources[].attributes.*",
    ),
    _get(
        "services/pins/building_clusters.py",
        "/api/v1/parcels/{parcel_uuid}/buildings/",
        "[].latitude",
        "[].longitude",
        "[].name",
        "[].ref",
        "[].parent_ref",
        "[].overlap_refs[]",
    ),
    _get(
        "services/places/provisioning.py",
        "/api/v1/parcels/{parcel_uuid}/buildings/",
        "[].ref",
        "[].parent_ref",
        "[].building_number",
        gaps=("[].uuid", "[].id", "[].osm_id", "[].osm_type"),
    ),
    _get("services/locations/site_scope.py", "/api/v1/parcels/{parcel_uuid}/buildings/", "[].latitude", "[].longitude"),
    _get(
        "services/trivia/deterministic.py",
        "/api/v1/parcels/{parcel_uuid}/buildings/",
        "[].name",
        "[].year_built",
        "[].year_built_basis",
        "[].building_number",
    ),
    _get(
        "services/pins/build_dates.py",
        "/api/v1/parcels/{parcel_uuid}/buildings/",
        "[].year_built",
        "[].year_built_basis",
    ),
    _get(
        "services/apis/locations/boundaries/redata.py",
        "/api/v1/parcels/{parcel_uuid}/boundaries/",
        "[].geometry.*",
        "[].is_suggested",
        "[].kind",
        "[].confidence",
    ),
    _get("services/floorplans/resolution.py", "/api/v1/parcels/{parcel_uuid}/floorplans/", "results[].uuid"),
    _get(
        "frontend/ts/entries/floorplan-editor.ts",
        "/api/v1/floorplans/{uuid}/",
        "uuid",
        "name",
        "valid_from",
        *_within("floors[].", ("uuid", "level", "name", "height_meters", "elevation_meters")),
        *_within(
            "floors[].rooms[].",
            ("uuid", "name", "description", "condition", "built_date", "references", "attributes.*"),
        ),
        *_within("reference_pool[].", ("uuid", "title", "url")),
        # REData's plan is elements on floors; the editor reads UrbanLens's own document shape. Latent: REData has no plan provider yet.
        gaps=(
            "plan_origin",
            "rotation_degrees",
            "floors[].walls",
            "floors[].markers",
            "floors[].designation",
            "floors[].rooms[].x",
            "floors[].rooms[].y",
            "reference_pool[].image_uuid",
        ),
    ),
    # Cultural resources: the gateway reads the envelope, the CRIS panel and its consumers read the resources.
    _get(_PARCELS, "/api/v1/cultural-resources/lookup/", "results", "complete"),
    _get(
        _CRIS,
        "/api/v1/cultural-resources/lookup/",
        *_within("results[].", (*_RESOURCE, "linked_from[].uuid", "linked_from[].resource_type")),
        gaps=_within("results[].", _RESOURCE_GAPS),
    ),
    _post(
        _CRIS,
        "/api/v1/cultural-resources/{uuid}/fetch-detail/",
        *_within("resource.", _RESOURCE),
        gaps=_within("resource.", _RESOURCE_GAPS),
    ),
    _post(_CRIS, "/api/v1/cultural-resources/fetch-details/", status="202"),
    _post(_CRIS, "/api/v1/cultural-resources/{uuid}/attachments/{id}/extract/", "extracted_images[].id"),
    _post(_PARCELS, "/api/v1/cultural-resources/{uuid}/attachments/{id}/extract/", "error", "message", status="400"),
    _post(_PARCELS, "/api/v1/cultural-resources/{uuid}/attachments/{id}/extract/", "error", "message", status="503"),
    _get(
        "plugins/builtin/parcel_buildings.py",
        "/api/v1/cultural-resources/lookup/",
        *_within(
            "results[].",
            ("resource_type", "external_id", "name", "source_latitude", "source_longitude", "attributes.*"),
        ),
    ),
    _get(
        "services/locations/national_register.py",
        "/api/v1/cultural-resources/lookup/",
        "results[].attributes.*",
        "results[].source_latitude",
        "results[].source_longitude",
    ),
    _get(
        "services/locations/name_tiers.py",
        "/api/v1/cultural-resources/lookup/",
        "results[].source_latitude",
        "results[].source_longitude",
    ),
    _get("services/locations/name_resolution.py", "/api/v1/cultural-resources/lookup/", "results[].attributes.*"),
)
