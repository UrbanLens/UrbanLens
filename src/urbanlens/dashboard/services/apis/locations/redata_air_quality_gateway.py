"""Gateway for REData's ``/air-quality/`` near-a-coordinate endpoint.

See ``../REData/docs/api-reference.md``, "GET /air-quality/ - air-quality
readings". Two keyless providers answering the same question differently,
returned side by side rather than reconciled:

- ``open_meteo_air`` (``source_kind: "modelled"``): Copernicus CAMS
  interpolated worldwide - always answers, never a measurement of that
  address.
- ``sensor_community`` (``source_kind: "sensor"``): crowdsourced instruments
  of unknown calibration, dense in central Europe, sparse elsewhere.

``source_kind`` governs what every number means and the two must not be
mixed, averaged, or ranked against each other. Every concentration is µg/m³
(carbon monoxide included). ``european_aqi`` and ``us_aqi`` are separate
scales, not one field plus a scale name, and neither is a concentration.
"""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_AIR_QUALITY_PATH = "/api/v1/air-quality/"


class RedataAirQualityGateway(RedataLocationContextGateway):
    """REST client for REData's air-quality endpoint."""

    service_key: ClassVar[str] = "redata_air_quality"

    def get_air_quality(
        self,
        latitude: float,
        longitude: float,
        *,
        include_indoor: bool = False,
        limit: int | None = None,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch air-quality readings near a coordinate.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            include_indoor: Also return indoor sensors (excluded by default
                because an indoor particulate reading measures a room, not
                the air at an address). Needs no refetch - they are stored
                either way.
            limit: Maximum number of readings to return.
            force_refresh: Bypass REData's cache and re-query live. Cached
                for one to three hours regardless - the shortest window in
                REData's API.

        Returns:
            The parsed envelope. Entries carry ``source_kind`` (read it
            before any number - see module docstring), per-pollutant
            concentrations in µg/m³ (nullable independently: a device
            measuring only particulates must not imply zero ozone), and the
            separate ``european_aqi``/``us_aqi`` scales.

        Raises:
            LocationContextUnavailableError: Every covering source failed to
                answer, or the request itself failed.
        """
        extra_params: dict[str, Any] = {}
        if include_indoor:
            extra_params["include_indoor"] = "true"
        return self.near_point(
            _AIR_QUALITY_PATH,
            latitude,
            longitude,
            force_refresh=force_refresh,
            limit=limit,
            extra_params=extra_params or None,
        )
