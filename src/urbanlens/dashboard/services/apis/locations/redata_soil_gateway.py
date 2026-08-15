"""Gateway for REData's ``/soil/`` endpoint.

See ``../REData/docs/api-reference.md``, "GET /soil/ - USDA soil-survey
components". One provider (``usda_ssurgo``), keyless, USA-only.

A response is a **composition, not one answer**: the survey records that an
area is a *map unit* made of several named soils in stated proportions, each
with its own drainage and hydrologic group, dominant first - and the
minority component is frequently the one that matters (a poorly-drained 5%
inclusion is what a foundation cares about). There is deliberately no
"overall soil" field and no average. A blank field is the survey's own
silence (urban map units are often unrated) - do not read a missing value as
favourable.
"""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope, RedataLocationContextGateway

_SOIL_PATH = "/api/v1/soil/"


class RedataSoilGateway(RedataLocationContextGateway):
    """REST client for REData's USDA soil-survey endpoint."""

    service_key: ClassVar[str] = "redata_soil"

    def get_soil_components(
        self,
        latitude: float,
        longitude: float,
        *,
        force_refresh: bool = False,
    ) -> LocationContextEnvelope:
        """Fetch the soil-survey components of the map unit at a point.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.
            force_refresh: Bypass REData's cache and re-query live.

        Returns:
            The parsed envelope, dominant component first. Entries carry
            ``component_name``, ``component_percent`` (read it first),
            ``drainage_class``, ``hydric_rating`` (``"Yes"``/``"No"``/
            ``"Unranked"`` - the source has three states, not two),
            ``hydrologic_group`` (runoff potential ``A``-``D``, or a dual
            class like ``B/D`` when it depends on drainage), and the
            map-unit facts ``map_unit_name``/``farmland_class`` repeated on
            every row.

        Raises:
            LocationContextUnavailableError: The source failed to answer, or
                the request itself failed.
        """
        return self.near_point(_SOIL_PATH, latitude, longitude, force_refresh=force_refresh)
