"""Gateway for REData's label-suggestion endpoints (``/labels/...``).

REData learns each user's own tag/category taxonomy (arbitrarily nested,
private to its owner) and which of those labels apply to which places, then
suggests labels for a place from that same user's vocabulary. Every
suggestion comes back with ``confidence`` (calibrated, ``[0.01, 0.99]``,
monotone up the hierarchy) and which ``ranker`` produced it (``"heuristic"``
or ``"model"``).

Same REData account/deployment already used for property records, places
resolution, and photo relevance - same ``UL_REDATA_API_URL``/
``UL_REDATA_API_KEY`` settings, same bearer-token convention, hitting
different endpoints. The API key used must carry REData's ``labels:read``/
``labels:write`` scopes.

Do not call this directly - go through ``services.labels.redata_suggestions``,
which decides what is worth syncing (tags and categories only - see that
module's docstring) and when.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, ClassVar

from urbanlens.dashboard.services.core.gateway import Gateway, GatewayRequestError
from urbanlens.UrbanLens.settings.app import settings

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30

#: REData's own per-request caps - callers batch larger lists themselves.
MAX_LABELS_PER_DEFINE_REQUEST = 2000
MAX_LOCATIONS_PER_ASSIGNMENT_REQUEST = 500
DEFAULT_SUGGEST_LIMIT = 10
MAX_SUGGEST_LIMIT = 50


@dataclass(slots=True, kw_only=True)
class RedataLabelsGateway(Gateway):
    """REST client for REData's ``/labels/...`` endpoints."""

    service_key: ClassVar[str] = "redata_labels"
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

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST one REData ``/labels/...`` endpoint and return its decoded JSON body.

        Args:
            path: Path relative to ``base_url`` (leading slash optional).
            body: JSON request body.

        Returns:
            The decoded JSON response body.

        Raises:
            GatewayRequestError: The request itself could not be made (network
                failure), REData returned a non-2xx status, or the response
                body wasn't parseable JSON.
        """
        base_url = self.base_url
        if base_url is None:
            # __post_init__ already validates this for the normal construction
            # path; this only narrows the type for mypy.
            raise GatewayRequestError("UL_REDATA_API_URL is not configured.")
        try:
            response = self.session.post(f"{base_url.rstrip('/')}/{path.lstrip('/')}", json=body, headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach REData: {exc}") from exc

        if response.status_code in (200, 201):
            try:
                return dict(response.json())
            except ValueError as exc:
                raise GatewayRequestError("REData returned an unparseable response.") from exc

        logger.warning("REData request to %s failed (%s): %s", path, response.status_code, response.text[:500])
        raise GatewayRequestError(f"REData request to {path} failed with status {response.status_code}.")

    def _get_json(self, path: str) -> dict[str, Any]:
        """GET one REData endpoint and return its decoded JSON body.

        Args:
            path: Path relative to ``base_url`` (leading slash optional).

        Returns:
            The decoded JSON response body.

        Raises:
            GatewayRequestError: The request could not be made, REData returned
                a non-2xx status, or the body wasn't parseable JSON.
        """
        base_url = self.base_url
        if base_url is None:
            raise GatewayRequestError("UL_REDATA_API_URL is not configured.")
        try:
            response = self.session.get(f"{base_url.rstrip('/')}/{path.lstrip('/')}", headers=self._headers, timeout=_REQUEST_TIMEOUT)
        except OSError as exc:
            raise GatewayRequestError(f"Could not reach REData: {exc}") from exc

        if response.status_code == 200:
            try:
                return dict(response.json())
            except ValueError as exc:
                raise GatewayRequestError("REData returned an unparseable response.") from exc

        logger.warning("REData request to %s failed (%s): %s", path, response.status_code, response.text[:500])
        raise GatewayRequestError(f"REData request to {path} failed with status {response.status_code}.")

    def get_model(self) -> dict[str, Any]:
        """Return what is currently producing label suggestions, and how well.

        Aggregate model metadata only - version, holdout metrics, the baselines
        the promotion decision was made on, and the feature registry. Nothing
        here is about a person: REData hashes user ids with a keyed HMAC on
        arrival and never stores the raw value, and this endpoint reports on
        the model rather than on anyone's labels.

        ``active`` is ``None`` before any model has been promoted, which is a
        normal state - the heuristic ranker answers in the meantime and is a
        real answer, not a degraded one, since every user is cold on their
        first day.

        Returns:
            The decoded ``GET /labels/model/`` body.

        Raises:
            GatewayRequestError: The request to REData failed.
        """
        return self._get_json("/api/v1/labels/model/")

    def define_labels(self, user_id: str, labels: list[dict[str, Any]]) -> dict[str, Any]:
        """Upsert (by ``external_id``) one user's tag/category label definitions.

        Args:
            user_id: UrbanLens profile identifier (see
                ``services.labels.redata_suggestions``'s ``_user_id``).
            labels: Up to :data:`MAX_LABELS_PER_DEFINE_REQUEST` definition
                dicts, each ``{external_id, name, parent_ids?, description?,
                is_active?}``. A partial list is fine - labels not mentioned
                are left alone. Re-sending an identical payload changes
                nothing.

        Returns:
            ``{created, updated, unknown_parents, rejected_edges,
            implied_created, implied_removed, statistics_deferred}``.

        Raises:
            GatewayRequestError: The request failed outright, or REData
                reported a non-2xx status.
        """
        if not labels:
            return {"created": 0, "updated": 0, "unknown_parents": {}, "rejected_edges": [], "implied_created": 0, "implied_removed": 0, "statistics_deferred": False}
        return self._post_json("/api/v1/labels/", {"user_id": user_id, "labels": labels})

    def sync_assignments(self, user_id: str, locations: list[dict[str, Any]]) -> dict[str, Any]:
        """Sync which labels apply to which of one user's places.

        Args:
            user_id: UrbanLens profile identifier.
            locations: Up to :data:`MAX_LOCATIONS_PER_ASSIGNMENT_REQUEST`
                entry dicts, each ``{latitude, longitude, external_id?,
                names?, label_ids?, removed_label_ids?, replace?,
                assigned_at?}``. UrbanLens always sends ``replace: true``
                with the pin's complete current tag/category set - see
                ``services.labels.redata_suggestions``.

        Returns:
            ``{locations_created, locations_updated, assignments_added,
            assignments_removed, implied_created, implied_removed,
            unknown_label_ids, statistics_deferred}``.

        Raises:
            GatewayRequestError: The request failed outright, or REData
                reported a non-2xx status.
        """
        if not locations:
            return {
                "locations_created": 0,
                "locations_updated": 0,
                "assignments_added": 0,
                "assignments_removed": 0,
                "implied_created": 0,
                "implied_removed": 0,
                "unknown_label_ids": [],
                "statistics_deferred": False,
            }
        return self._post_json("/api/v1/labels/assignments/", {"user_id": user_id, "locations": locations})

    def suggest_labels(
        self,
        user_id: str,
        latitude: float,
        longitude: float,
        *,
        names: list[str] | None = None,
        applied_label_ids: list[str] | None = None,
        limit: int | None = None,
        min_confidence: float | None = None,
    ) -> dict[str, Any]:
        """Ask which of this user's own tag/category labels likely apply to a place.

        A POST rather than a GET because the question carries lists; it is
        still a pure read and stores nothing on REData's end.

        Args:
            user_id: UrbanLens profile identifier.
            latitude: Place latitude.
            longitude: Place longitude.
            names: Known alternate names for the place, if any.
            applied_label_ids: Labels already applied - excluded from the
                answer and the strongest single input to it.
            limit: Max results, REData default 10, capped at
                :data:`MAX_SUGGEST_LIMIT`.
            min_confidence: Drop results below this confidence.

        Returns:
            ``{count, results, implied, ranker, model_version,
            scored_candidates, total_candidates, statistics_stale}``.
            ``results`` items are ``{label_id, name, confidence,
            canonical_key}``. An unknown user gets an empty list, not a 404.

        Raises:
            GatewayRequestError: The request failed outright, or REData
                reported a non-2xx status.
        """
        body: dict[str, Any] = {"user_id": user_id, "latitude": latitude, "longitude": longitude}
        if names:
            body["names"] = names
        if applied_label_ids:
            body["applied_label_ids"] = applied_label_ids
        if limit is not None:
            body["limit"] = limit
        if min_confidence is not None:
            body["min_confidence"] = min_confidence
        return self._post_json("/api/v1/labels/suggest/", body)
