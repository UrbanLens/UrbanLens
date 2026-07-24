"""Gateway for REData's (not-yet-built) CID -> coordinate resolution endpoint.

See ``docs/redata-cid-resolution.md`` for why this exists (Google Maps CIDs
decoded via the free literal-S2-cell heuristic are wrong ~31% of the time - see
``notes/geocoding-analysis/``), the proof of concept this replaces
(``services.apis.locations.google.scraping.GoogleMapsScraper``), and the
proposed request/response contract implemented below.

This is the same REData account/deployment already used for property records
(``services.apis.property_records.redata_gateway.RedataGateway`` - same
``UL_REDATA_API_URL``/``UL_REDATA_API_KEY`` settings, same bearer-token
convention) hitting a different, new endpoint. Kept as a separate class/service
key rather than a method on ``RedataGateway`` since it's a distinct capability
(place geocoding, not property records) with its own call volume to track.

Do not call this directly - go through
``services.apis.locations.cid_resolution.resolve_cids``, which decides whether
REData or the Google Places fallback should handle a given batch.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import ClassVar

from urbanlens.dashboard.services.gateway import Gateway, GatewayRequestError
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 60


@dataclass(slots=True, kw_only=True)
class RedataCidGateway(Gateway):
    """REST client for REData's CID->coordinate resolution API (proposed contract, not yet live)."""

    service_key: ClassVar[str] = "redata_cid_lookup"
    paid_service: ClassVar[bool] = False

    base_url: str | None = settings.redata_api_url
    api_key: str | None = settings.redata_api_key

    def __post_init__(self) -> None:
        Gateway.__post_init__(self)
        if not self.base_url:
            raise ValueError("UL_REDATA_API_URL must be configured.")
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = f"https://{self.base_url}"
        if not self.api_key:
            raise ValueError("UL_REDATA_API_KEY must be configured.")

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json", "Content-Type": "application/json"}

    def resolve_cids(self, cids: list[int]) -> dict[int, tuple[float, float] | None]:
        """Resolve a batch of Google Maps CIDs to coordinates via REData.

        Args:
            cids: CIDs to resolve (the decimal value after the ``:0x`` in a
                Google Maps place URL's data segment).

        Returns:
            Dict keyed by every requested cid. A value of ``None`` means
            REData confirmed there's no resolvable location for that cid (a
            terminal answer - do not retry). A missing key would indicate a
            malformed REData response and is treated as a request failure
            instead (see raises).

        Raises:
            GatewayRequestError: The request to REData failed outright
                (network error, non-200, unparseable/malformed body). Callers
                should treat the whole batch as transiently unresolved and
                retry later - REData not being reachable says nothing about
                whether any individual cid is resolvable.
        """
        if not cids:
            return {}

        base_url = self.base_url
        if base_url is None:
            # __post_init__ already validates this for the normal construction
            # path; this only narrows the type for mypy.
            raise GatewayRequestError("UL_REDATA_API_URL is not configured.")

        try:
            response = self.session.post(
                f"{base_url.rstrip('/')}/api/v1/places/resolve-cids/",
                json={"cids": cids},
                headers=self._headers,
                timeout=_REQUEST_TIMEOUT,
            )
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach REData: {exc}") from exc

        if response.status_code != 200:
            logger.warning("REData CID resolution failed (%s): %s", response.status_code, response.text[:500])
            raise GatewayRequestError(f"REData request failed with status {response.status_code}.")

        try:
            body = response.json()
            raw_results = dict(body["results"])
        except (ValueError, KeyError, TypeError) as exc:
            raise GatewayRequestError("REData returned an unparseable response.") from exc

        results: dict[int, tuple[float, float] | None] = {}
        for cid in cids:
            entry = raw_results.get(str(cid))
            if entry is None:
                results[cid] = None
                continue
            try:
                results[cid] = (float(entry["lat"]), float(entry["lng"]))
            except (KeyError, TypeError, ValueError):
                logger.warning("REData returned a malformed entry for cid %d: %r", cid, entry)
                results[cid] = None

        return results
