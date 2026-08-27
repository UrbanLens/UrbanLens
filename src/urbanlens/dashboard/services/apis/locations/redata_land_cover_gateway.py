"""Gateway for REData's ``/land-cover/`` endpoint.

See ``../REData/docs/api-reference.md``, "GET /land-cover/ - NLCD land
cover". One provider (``mrlc_nlcd``), keyless, **conterminous US only** -
elsewhere the provider reports ``not_applicable`` ("no source covers this
place", not "nothing here").

One value covers a 30 m raster pixel (``resolution_meters``): a small urban
parcel sits inside a single pixel, so the answer describes its block rather
than its garden.
"""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_LAND_COVER_PATH = "/api/v1/land-cover/"


class RedataLandCoverGateway(RedataLocationContextGateway):
    """REST client for REData's NLCD land-cover endpoint."""

    service_key: ClassVar[str] = "redata_land_cover"

    def get_land_cover(
        self,
        latitude: float,
        longitude: float,
        *,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch the NLCD land-cover classification at a point.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            The parsed envelope. The (at most one) result carries
            ``class_code`` (NLCD's own legend value), ``class_name``
            (decoded; blank rather than invented for an unrecognised code),
            ``is_developed`` (groups the four developed classes; **null**
            where nothing is classified, which is not the same as
            "undeveloped"), and ``resolution_meters``.

        Raises:
            LocationContextUnavailableError: The source failed to answer, or
                the request itself failed.
        """
        return self.near_point(_LAND_COVER_PATH, latitude, longitude, force_refresh=force_refresh)
