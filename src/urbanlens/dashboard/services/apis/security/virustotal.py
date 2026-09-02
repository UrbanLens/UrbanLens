"""VirusTotal API v3 client: hash-based file report lookups only.

Thin HTTP transport with no opinion about what counts as a "clean" verdict -
see ``services.security.virustotal_scan`` for that policy. Deliberately does
not upload files (``POST /files``): that endpoint returns no immediate
verdict (it has to be polled separately once analysis completes), which buys
nothing for the scan that triggered it - see that module's docstring.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.core.gateway import Gateway, GatewayRequestError
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.virustotal.com/api/v3"
_REQUEST_TIMEOUT = 15


@dataclass(slots=True, kw_only=True)
class VirusTotalGateway(Gateway):
    """Read-only client for VirusTotal's file-hash lookup endpoint (API v3)."""

    service_key: ClassVar[str] = "virustotal"
    paid_service: ClassVar[bool] = False

    api_key: str | None = settings.virustotal_api_key

    def __post_init__(self) -> None:
        """Attach the x-apikey auth header for every request this gateway makes."""
        Gateway.__post_init__(self)
        self.session.headers.update({"x-apikey": self.api_key or "", "Accept": "application/json"})

    def get_file_report(self, sha256: str) -> dict[str, Any] | None:
        """Look up an existing VirusTotal report for a file by its SHA-256 hash.

        Args:
            sha256: The file's SHA-256 hex digest.

        Returns:
            The report's ``data.attributes`` dict (includes
            ``last_analysis_stats``) when VirusTotal has already analyzed a
            file with this hash, or ``None`` on HTTP 404 - not an error, just
            "VirusTotal has never seen this exact file".

        Raises:
            GatewayRequestError: The request could not be made, VirusTotal
                returned a non-2xx status other than 404 (401 bad key, 429
                rate limited, 5xx, ...), or the response body was not the
                expected JSON shape.
        """
        try:
            response = self.session.get(f"{_BASE_URL}/files/{sha256}", timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach VirusTotal: {exc}") from exc

        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise GatewayRequestError(f"VirusTotal file lookup failed with status {response.status_code}.")

        try:
            attributes = response.json()["data"]["attributes"]
        except (ValueError, KeyError, TypeError) as exc:
            raise GatewayRequestError("VirusTotal returned an unparseable file report.") from exc
        if not isinstance(attributes, dict):
            raise GatewayRequestError(f"VirusTotal returned {type(attributes).__name__} attributes, expected an object.")
        return attributes
