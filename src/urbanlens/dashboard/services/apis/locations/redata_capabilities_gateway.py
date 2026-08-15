"""Gateway for REData's ``/capabilities/`` discovery endpoint.

See ``../REData/docs/api-reference.md``, "GET /capabilities/ - what REData
can answer": every domain (near-point and text-searched), its endpoint and
scope, and its providers with radius bounds and billable flags - generated
from REData's own registries, so a provider registered anywhere appears
without a client release. This is the endpoint that makes a source swappable
without a client change.

Reading it costs REData no external call, but UrbanLens consumers should
still cache the answer (it changes on REData deploys, not per request).
"""

from __future__ import annotations

from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import RedataLocationContextGateway

_CAPABILITIES_PATH = "/api/v1/capabilities/"


class RedataCapabilitiesGateway(RedataLocationContextGateway):
    """REST client for REData's capability index."""

    service_key: ClassVar[str] = "redata_capabilities"

    def get_capabilities(self, *, latitude: float | None = None, longitude: float | None = None) -> dict[str, Any]:
        """Fetch the capability index, optionally scoped to a point.

        Args:
            latitude: With ``longitude``, adds ``applicable_providers`` to each
                near-point domain - which of its sources cover that point,
                from the registries' own cheap bounds test (no external call).
            longitude: See ``latitude``.

        Returns:
            ``{"domains": [...], "text_domains": [...]}``. Near-point domains
            carry ``tag``, ``label``, ``endpoint``, ``scope``, ``prewarmed``
            and ``providers`` (each with ``tag``, ``radius_pinned``,
            ``billable``); ``text_domains`` are the two string-searched
            surfaces, advertised separately because they take a query and a
            limit rather than a coordinate and a radius.

        Raises:
            LocationContextUnavailableError: The request failed.
        """
        params: dict[str, Any] = {}
        if latitude is not None and longitude is not None:
            params = {"lat": latitude, "lng": longitude}
        return self.get_json(_CAPABILITIES_PATH, params)
