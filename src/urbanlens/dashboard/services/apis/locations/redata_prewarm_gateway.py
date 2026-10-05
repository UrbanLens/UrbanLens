"""Gateway for REData's ``POST /locations/prewarm/``: queue REData's own background fetches for a new point."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from urbanlens.dashboard.services.apis.locations.redata_context_gateway import (
    REASON_SOURCE_ERROR,
    LocationContextBusyError,
    LocationContextUnavailableError,
    RedataLocationContextGateway,
)
from urbanlens.dashboard.services.core.gateway import UpstreamBusyError, upstream_retry_after

_PATH = "/api/v1/locations/prewarm/"
#: REData answers before anything is fetched, so a slow answer means REData is struggling, not working.
_TIMEOUT_SECONDS = 10


class PrewarmNotPermittedError(LocationContextUnavailableError):
    """This key may not prewarm: it lacks the ``locations:prewarm`` scope, or this REData has no such endpoint.

    Settled until the key or the deployment changes, so it is an answer rather than an outage.
    """

    def __init__(self, message: str) -> None:
        super().__init__("forbidden", message, rejected=True)


@dataclass(slots=True, kw_only=True)
class RedataPrewarmGateway(RedataLocationContextGateway):
    """REST client for REData's location prewarm."""

    service_key: ClassVar[str] = "redata_prewarm"

    def prewarm(self, latitude: float, longitude: float) -> dict[str, Any]:
        """Ask REData to fetch every free source for a coordinate in the background.

        Args:
            latitude: WGS-84 latitude.
            longitude: WGS-84 longitude.

        Returns:
            REData's ``202`` body: which sources were queued and how soon each runs.

        Raises:
            PrewarmNotPermittedError: The key lacks the scope (``403``) or REData has no prewarm endpoint (``404``).
            LocationContextBusyError: REData throttled the key.
            LocationContextUnavailableError: REData could not be reached or refused the request.
        """
        base_url = self.base_url or ""
        try:
            response = self.session.post(f"{base_url.rstrip('/')}{_PATH}", json={"lat": latitude, "lng": longitude}, headers=self._headers, timeout=_TIMEOUT_SECONDS)
        except UpstreamBusyError as exc:
            raise LocationContextBusyError(str(exc), retry_after=exc.retry_after) from exc
        except OSError as exc:
            # The exception text can carry the request body's coordinate; its type is enough.
            raise LocationContextUnavailableError(REASON_SOURCE_ERROR, f"Could not reach REData: {type(exc).__name__}") from exc

        if response.status_code in (200, 202):
            try:
                body = response.json()
            except ValueError:
                body = {}
            return body if isinstance(body, dict) else {}
        if response.status_code in (403, 404):
            raise PrewarmNotPermittedError(f"REData refused to prewarm ({response.status_code}).")
        if response.status_code == 429:
            raise LocationContextBusyError("REData throttled this key.", retry_after=upstream_retry_after(response) or 1)
        raise LocationContextUnavailableError(REASON_SOURCE_ERROR, f"REData prewarm failed with status {response.status_code}.", rejected=response.status_code == 400)
