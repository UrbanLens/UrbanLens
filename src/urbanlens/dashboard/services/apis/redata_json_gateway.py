"""Shared JSON-over-HTTP base for REData's non-location-context endpoints.

``RedataLabelsGateway`` and ``RedataPhotosGateway`` had byte-identical
configuration, headers and request helpers - roughly ninety lines duplicated
between them, which drifts the moment one is fixed and the other is not. They
now differ only in the endpoints they expose.

Separate from ``RedataLocationContextGateway``: that one models the
"near-a-coordinate" envelope every location endpoint shares, which the label
and photo APIs do not use.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.core.gateway import Gateway, GatewayRequestError
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

#: Matches the other REData gateways; long enough for a cold model read.
_REQUEST_TIMEOUT = 30


class RedataJsonGateway(Gateway):
    """Bearer-authenticated JSON client for one family of REData endpoints."""

    paid_service: ClassVar[bool] = False

    base_url: str | None = settings.redata_api_url
    api_key: str | None = settings.redata_api_key

    def __post_init__(self) -> None:
        """Normalize the base URL and require both settings.

        Raises:
            ValueError: ``UL_REDATA_API_URL`` or ``UL_REDATA_API_KEY`` is unset.
        """
        Gateway.__post_init__(self)
        if not self.base_url:
            raise ValueError("UL_REDATA_API_URL must be configured.")
        if not self.base_url.startswith(("http://", "https://")):
            self.base_url = f"https://{self.base_url}"
        if not self.api_key:
            raise ValueError("UL_REDATA_API_KEY must be configured.")

    @property
    def _headers(self) -> dict[str, str]:
        """Bearer auth plus JSON content negotiation."""
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json", "Content-Type": "application/json"}

    def _url(self, path: str) -> str:
        """Join ``path`` onto the base URL.

        Args:
            path: Path relative to ``base_url`` (leading slash optional).

        Returns:
            The absolute URL.

        Raises:
            GatewayRequestError: The base URL is unset - ``__post_init__``
                already rejects that, so this only narrows the type.
        """
        if self.base_url is None:
            raise GatewayRequestError("UL_REDATA_API_URL is not configured.")
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    def _decode(self, response: Any, path: str) -> dict[str, Any]:
        """Turn a response into a JSON object, or raise.

        Args:
            response: The ``requests`` response.
            path: The path called, for the error message.

        Returns:
            The decoded body.

        Raises:
            GatewayRequestError: Non-2xx status, or a body that is not a JSON
                object. ``dict()`` is not enough on its own - a 200 carrying a
                JSON list raises ``TypeError``, which callers do not expect
                from a gateway.
        """
        if response.status_code in (200, 201):
            try:
                body = response.json()
            except ValueError as exc:
                raise GatewayRequestError("REData returned an unparseable response.") from exc
            if not isinstance(body, dict):
                raise GatewayRequestError(f"REData returned {type(body).__name__}, expected an object.")
            return body

        logger.warning("REData request to %s failed (%s)", path, response.status_code)
        raise GatewayRequestError(f"REData request to {path} failed with status {response.status_code}.")

    def _get_json(self, path: str, params: dict[str, Any] | None = None, *, timeout: int = _REQUEST_TIMEOUT) -> dict[str, Any]:
        """GET one endpoint and return its decoded body.

        Args:
            path: Path relative to ``base_url``.
            params: Optional query parameters.
            timeout: Seconds to wait. Lower it for a call made inline in a page
                render, where the default would let one unresponsive upstream
                hold the whole response open.

        Returns:
            The decoded JSON body.

        Raises:
            GatewayRequestError: The request could not be made, or the response
                was not a usable JSON object.
        """
        try:
            response = self.session.get(self._url(path), params=params or {}, headers=self._headers, timeout=timeout)
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach REData: {exc}") from exc
        return self._decode(response, path)

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST one endpoint and return its decoded body.

        Args:
            path: Path relative to ``base_url``.
            body: JSON request body.

        Returns:
            The decoded JSON body.

        Raises:
            GatewayRequestError: The request could not be made, or the response
                was not a usable JSON object.
        """
        try:
            response = self.session.post(self._url(path), json=body, headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach REData: {exc}") from exc
        return self._decode(response, path)
